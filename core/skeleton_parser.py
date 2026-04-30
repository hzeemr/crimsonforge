"""PAB skeleton parser for Crimson Desert.

Parses .pab files to extract bone hierarchies with names, parent indices,
and transform matrices. Used to add armature data to PAC mesh exports.

PAB format (PAR v5.1):
  Header: 20 bytes (magic + version + hash)
  [0x14] uint16 LE: bone_count
  Per bone:
    [4B] bone_hash
    [Nb] bone_name (null-terminated ASCII)
    [4B] parent_index (int32, -1 = root)
    [64B] bind_matrix (4x4 float32)
    [64B] inverse_bind_matrix (4x4 float32)
    [64B] bind_matrix_copy
    [64B] inverse_bind_copy
    [12B] scale (3 float32)
    [16B] rotation_quaternion (4 float32: x, y, z, w)
    [12B] position (3 float32)
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.skeleton_parser")

PAR_MAGIC = b"PAR "


@dataclass
class Bone:
    """A single bone in the skeleton hierarchy."""
    index: int = 0
    name: str = ""
    parent_index: int = -1
    bind_matrix: tuple = ()       # 16 floats (4x4 row-major)
    inv_bind_matrix: tuple = ()   # 16 floats
    scale: tuple = (1.0, 1.0, 1.0)
    rotation: tuple = (0.0, 0.0, 0.0, 1.0)  # quaternion xyzw
    position: tuple = (0.0, 0.0, 0.0)


@dataclass
class Skeleton:
    """Parsed skeleton with bone hierarchy."""
    path: str = ""
    bones: list[Bone] = field(default_factory=list)
    bone_count: int = 0

    def get_bone_by_name(self, name: str) -> Optional[Bone]:
        for b in self.bones:
            if b.name == name:
                return b
        return None

    def get_children(self, bone_index: int) -> list[Bone]:
        return [b for b in self.bones if b.parent_index == bone_index]

    def get_root_bones(self) -> list[Bone]:
        return [b for b in self.bones if b.parent_index == -1]


def parse_pab(data: bytes, filename: str = "") -> Skeleton:
    """Parse a .pab skeleton file.

    Returns a Skeleton with bone names, parent indices, and transforms.

    Format (REVERSE-ENGINEERED v3, 2026-04-29):
        Header:
            [0..3]  magic 'PAR '
            [4..5]  version (0x01 0x05 = PAR v5.1)
            [6..0x13]  payload (~14 bytes)
            [0x14..0x15]  uint16 LE bone_count
            [0x16]        1 byte flags/padding
            [0x17..]  bone records start

        CRITICAL CORRECTION (2026-04-29):
            v2 of this parser read bone_count as data[0x14] (uint8),
            which silently truncates any skeleton with > 255 bones to
            its low byte. phm_01.pab claimed 178 bones (0xb2) but
            actually has 434 (0x01b2). Damian's PAB claimed 192 (0xc0)
            but the mesh references bones up to index 247 — the missing
            bones got identity-stub padding, vertices weighted to them
            collapsed to origin, and Blender drew the missing-bone
            spike explosion.
            Fixing to uint16 LE recovers all bones. Field width was
            confirmed by exhaustively scanning the header for any
            offset/width combination that produced the parsed-cleanly
            count (434 for phm_01) — only u16 LE @ 0x14 matched.

        Per-bone record (305 + name_len bytes):
            [3 bytes]   hash low24 (probably FNV / PA hash trimmed to 3 bytes)
            [1 byte]    name_length (uint8)
            [N bytes]   name (no terminator)
            [4 bytes]   parent_index (int32 LE; -1 = root)
            [64 bytes]  bind_matrix (4x4 fp32, column-major flat)
            [64 bytes]  inv_bind_matrix
            [64 bytes]  bind_matrix_copy (engine cache duplicate)
            [64 bytes]  inv_bind_copy
            [12 bytes]  scale (3 fp32)
            [16 bytes]  rotation_quaternion (4 fp32 xyzw)
            [12 bytes]  position (3 fp32)
            [1 byte]    alignment / record terminator
        => total = 4 + name_len + 4 + 64*4 + 12 + 16 + 12 + 1 = 305 + name_len

    Previous versions of this parser used a printable-ASCII-terminated
    name read with a forward parent search. That works for the first
    ~56 bones, then drifts catastrophically because:
      (a) bone names with parent_index in 0x20..0x7E (printable range)
          have the parent's low byte mistaken for the name's last char
      (b) the heuristic "next-uppercase-letter" advance scan lands in
          arbitrary places once any drift occurs
    The length-prefix-byte at offset 3 of the per-bone "hash" field
    eliminates both — name length is known in advance, no scanning
    needed, no heuristics needed. 226+/246 PAB files parse 100% with
    this approach.
    """
    if len(data) < 0x18 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAB file: {data[:4]!r}")

    skeleton = Skeleton(path=filename)

    # bone_count is uint16 LE at 0x14 (NOT uint8 — see docstring).
    # Reading it as u8 silently truncates any skeleton with > 255 bones,
    # which was the cause of Damian's spike explosion on FBX import:
    # vertices weighted to bones [256..) found no target bone, got
    # identity-stub influence, and visually scattered.
    bone_count = struct.unpack_from('<H', data, 0x14)[0]
    skeleton.bone_count = bone_count
    if bone_count == 0:
        return skeleton

    off = 0x17  # 2-byte count @ 0x14..0x15 + 1 byte flag @ 0x16 = 0x17

    for i in range(bone_count):
        if off + 4 > len(data):
            break
        # Read 4-byte hash field; byte[3] is the name length.
        hash_lo24 = struct.unpack_from('<I', data, off)[0] & 0x00FFFFFF
        name_len = data[off + 3]
        off += 4

        if name_len == 0 or name_len > 80 or off + name_len > len(data):
            logger.warning(
                "PAB %s: bone %d has invalid name_length=%d at off=0x%x — "
                "skeleton truncated",
                filename, i, name_len, off - 4,
            )
            break

        bone = Bone(index=i)
        bone.name = data[off:off + name_len].decode('ascii', 'replace')
        off += name_len

        if off + 4 + 256 + 40 + 1 > len(data):
            logger.warning(
                "PAB %s: bone %d (%r) record truncated at off=0x%x",
                filename, i, bone.name, off,
            )
            break

        bone.parent_index = struct.unpack_from('<i', data, off)[0]
        off += 4

        bone.bind_matrix     = struct.unpack_from('<16f', data, off); off += 64
        bone.inv_bind_matrix = struct.unpack_from('<16f', data, off); off += 64
        # Skip 2 cache-duplicate matrices (engine pre-loads these for
        # GPU upload; we don't need them at parse time).
        off += 128

        bone.scale    = struct.unpack_from('<3f', data, off); off += 12
        bone.rotation = struct.unpack_from('<4f', data, off); off += 16
        bone.position = struct.unpack_from('<3f', data, off); off += 12

        # Per-bone alignment / record terminator.
        off += 1

        # Validate the floats we just read. If anything is nan/inf or
        # absurdly large, the format assumption is wrong for THIS
        # bone — bail rather than emit garbage downstream.
        import math as _math
        def _bad(v):
            return _math.isnan(v) or _math.isinf(v) or abs(v) > 1e5
        if (any(_bad(v) for v in bone.position) or
                any(_bad(v) for v in bone.rotation) or
                any(_bad(v) for v in bone.scale) or
                any(_bad(v) for v in bone.bind_matrix)):
            logger.warning(
                "PAB %s: bone %d (%r) has garbage floats — stopping",
                filename, i, bone.name,
            )
            break

        skeleton.bones.append(bone)

    # Pad any bones we couldn't read to keep mesh weight indices valid
    # (mesh refers to bone indices that must exist even if data is bad).
    parsed_count = len(skeleton.bones)
    if parsed_count < bone_count:
        identity = (
            1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0,
        )
        for stub_idx in range(parsed_count, bone_count):
            skeleton.bones.append(Bone(
                index=stub_idx, name=f"_stub_bone_{stub_idx}",
                parent_index=-1, bind_matrix=identity,
                inv_bind_matrix=identity,
                scale=(1.0, 1.0, 1.0),
                rotation=(0.0, 0.0, 0.0, 1.0),
                position=(0.0, 0.0, 0.0),
            ))
        logger.warning(
            "PAB %s: parsed %d of %d bones; padded %d stubs",
            filename, parsed_count, bone_count,
            bone_count - parsed_count,
        )

    skeleton.bone_count = len(skeleton.bones)
    logger.info("Parsed PAB %s: %d bones",
                filename, len(skeleton.bones))
    return skeleton


def _legacy_parse_pab_unused(data: bytes, filename: str = "") -> Skeleton:
    """Old heuristic parser — kept here only for reference. The new
    length-prefix parser above replaces it entirely.
    """
    skeleton = Skeleton(path=filename)
    # u16 LE — see parse_pab() docstring for the truncation history.
    bone_count = struct.unpack_from('<H', data, 0x14)[0]
    skeleton.bone_count = bone_count
    if bone_count == 0:
        return skeleton
    off = 0x17

    for i in range(bone_count):
        if off + 8 >= len(data):
            break

        bone = Bone(index=i)

        # Bone hash (4 bytes)
        off += 4

        # ── Name + parent read with parent-aware boundary detection ──
        #
        # Bone 56+ in real character skeletons hit a subtle bug: when
        # the bone's parent_index value falls in 0x20..0x7e (printable
        # ASCII range — i.e. parent index 32..126), the *low byte* of
        # the i32 parent value is itself printable and gets eaten by
        # the "while printable" name reader, drifting all subsequent
        # field offsets by 1 byte and cascading into "garbage float"
        # bail-outs.
        #
        # Specifically: bone with parent_index = 32 has bytes
        # `20 00 00 00` after its name. 0x20 is space. The naive
        # printable-loop sticks `20` into the name, name_end drifts
        # by 1, every downstream field is misaligned, parser dies.
        #
        # Fix: read the printable run, then iterate possible name-end
        # positions from greedy-longest down to "first non-letter".
        # At each position, try reading a valid i32 parent_index
        # (-1 or 0..bone_count) immediately after, AND verify the
        # 16 floats following look like a plausible bind matrix
        # (column 0 norm ~= 1.0 within tolerance — rotation-or-scale
        # matrix invariant). The first position that satisfies both
        # is the true name boundary.
        name_start = off
        scan_max = min(off + 128, len(data))
        # Greedy longest printable run.
        greedy_end = off
        while greedy_end < scan_max:
            byte = data[greedy_end]
            if byte < 0x20 or byte > 0x7E:
                break
            greedy_end += 1

        def _is_plausible_bind_col(p):
            """Verify floats at p..p+12 form a unit-ish rotation column.

            A real bind matrix column 0 always has length ~1.0 (or some
            game-specific uniform scale, e.g. 100.0 if the model is
            stored in cm). Accept 0.1..10.0 — covers all realistic
            character bone scales without admitting near-zero garbage
            (which a 1-byte-shifted read would produce).
            """
            try:
                f0, f1, f2, f3 = struct.unpack_from('<4f', data, p)
            except struct.error:
                return False
            import math
            for v in (f0, f1, f2, f3):
                if math.isnan(v) or math.isinf(v) or abs(v) > 1e4:
                    return False
            norm = math.sqrt(f0 * f0 + f1 * f1 + f2 * f2)
            return abs(f3) < 1e-3 and 0.1 < norm < 10.0

        chosen_name_end = -1
        chosen_parent = None
        # Walk back from greedy_end to find the actual name boundary.
        # Try each candidate end position; pick the FIRST one that
        # has a valid parent + plausible matrix immediately after.
        for cand_end in range(greedy_end, name_start - 1, -1):
            if cand_end + 4 + 16 > len(data):
                continue
            try:
                parent_val = struct.unpack_from('<i', data, cand_end)[0]
            except struct.error:
                continue
            if not (parent_val == -1 or 0 <= parent_val < bone_count):
                continue
            # Validate the bind-matrix start at cand_end + 4
            if not _is_plausible_bind_col(cand_end + 4):
                continue
            chosen_name_end = cand_end
            chosen_parent = parent_val
            break

        if chosen_name_end < 0:
            # Fall back to the old behaviour: greedy printable + scan.
            chosen_name_end = greedy_end

        bone.name = data[name_start:chosen_name_end].decode('ascii', 'replace')
        off = chosen_name_end

        if chosen_parent is not None:
            bone.parent_index = chosen_parent
            off = chosen_name_end + 4
            parent_found = True
        else:
            # Old fallback: scan forward 16 bytes for a valid int.
            parent_found = False
            scan_end = min(off + 16, len(data) - 4)
            for scan in range(off, scan_end):
                val = struct.unpack_from('<i', data, scan)[0]
                if val == -1 or (0 <= val < bone_count):
                    bone.parent_index = val
                    off = scan + 4
                    parent_found = True
                    break
            if not parent_found:
                off = chosen_name_end + 4  # skip and hope

        # Transform data: 4 matrices (4x4 float each = 64 bytes) + scale + rotation + position
        # Total: 256 + 40 = 296 bytes minimum
        if off + 64 <= len(data):
            bone.bind_matrix = struct.unpack_from('<16f', data, off)
            off += 64

        if off + 64 <= len(data):
            bone.inv_bind_matrix = struct.unpack_from('<16f', data, off)
            off += 64

        # Skip 2 more matrices (copies)
        if off + 128 <= len(data):
            off += 128

        # Scale (3 floats)
        if off + 12 <= len(data):
            bone.scale = struct.unpack_from('<fff', data, off)
            off += 12

        # Rotation quaternion (4 floats: x, y, z, w)
        if off + 16 <= len(data):
            bone.rotation = struct.unpack_from('<ffff', data, off)
            off += 16

        # Position (3 floats)
        if off + 12 <= len(data):
            bone.position = struct.unpack_from('<fff', data, off)
            off += 12

        # Skip any remaining padding/data to align with next bone hash
        # Validate bone before accepting it. The heuristic-based
        # forward scan for "next uppercase letter" above is unreliable
        # on binary payload data — random floats routinely contain
        # bytes in the 65..90 (A-Z) range, which causes the parser to
        # emit phantom bones with random names and garbage positions.
        # Stop parsing at the first clearly-bogus bone so downstream
        # exporters (FBX etc.) don't trip over inf / NaN / 10^30
        # positions that crash Blender's importer.
        import math as _math
        def _is_bad_float(v):
            return _math.isnan(v) or _math.isinf(v) or abs(v) > 1e5
        if any(_is_bad_float(v) for v in bone.position) or \
           any(_is_bad_float(v) for v in bone.rotation) or \
           any(_is_bad_float(v) for v in bone.scale):
            logger.debug(
                "PAB %s: stopping at bone %d (%r) — garbage float detected",
                filename, i, bone.name,
            )
            break

        # Next bone starts with a 4-byte hash before its name
        # Scan forward for next uppercase letter (bone name start)
        if i < bone_count - 1:
            while off < len(data) - 4:
                # Check if next bone name starts here (uppercase letter)
                if off + 5 < len(data) and 65 <= data[off + 4] <= 90:
                    break
                off += 1

        skeleton.bones.append(bone)

    # ── Pad missing bones up to the header-declared count ──
    # The cumulative-offset reader above bails as soon as it sees
    # garbage floats, but real character skeletons (phw_01: 192 bones,
    # phm_01: 178 bones) typically lose 100+ bones to that bail. The
    # actual PAB per-bone layout post-bone-56 still needs format RE
    # to decode reliably. Until that's done we pad missing bones with
    # identity stubs so mesh weights to those bones don't get silently
    # dropped during FBX export.
    #
    # With stubs in place, vertices skinned to a missing bone collapse
    # to world origin instead of scattering as spikes. That's
    # localized-broken instead of catastrophically-broken — Damian's
    # body looks correct, accessories collapse to origin (visible as
    # a small clump near the character) instead of exploding.
    parsed_count = len(skeleton.bones)
    if parsed_count < bone_count:
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        for stub_idx in range(parsed_count, bone_count):
            skeleton.bones.append(Bone(
                index=stub_idx,
                name=f"_stub_bone_{stub_idx}",
                parent_index=-1,
                bind_matrix=identity,
                inv_bind_matrix=identity,
                scale=(1.0, 1.0, 1.0),
                rotation=(0.0, 0.0, 0.0, 1.0),
                position=(0.0, 0.0, 0.0),
            ))
        logger.warning(
            "PAB %s: still missing %d bones after anchor recovery — "
            "padded as identity stubs",
            filename, bone_count - parsed_count,
        )

    skeleton.bone_count = len(skeleton.bones)
    return skeleton


def find_matching_pab(pac_path: str, pamt_entries) -> Optional[str]:
    """Find a .pab file matching a .pac file path."""
    stem = pac_path.lower().replace('.pac', '')
    for entry in pamt_entries:
        if entry.path.lower().replace('.pab', '') == stem:
            return entry.path
    return None


def is_skeleton_file(path: str) -> bool:
    """Check if a file is a skeleton file."""
    return os.path.splitext(path.lower())[1] == ".pab"
