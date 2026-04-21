"""PAB skeleton parser for Crimson Desert.

Parses .pab files to extract bone hierarchies with names, parent indices,
and transform matrices. Used to add armature data to PAC mesh exports.

PAB format (PAR v5.1):
  Header: 20 bytes (magic + version + hash)
  [0x14] uint8: bone_count
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
    """
    if len(data) < 0x16 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAB file: {data[:4]!r}")

    skeleton = Skeleton(path=filename)

    # Bone count at offset 0x14
    bone_count = data[0x14]
    skeleton.bone_count = bone_count

    if bone_count == 0:
        return skeleton

    # Parse bones sequentially after the header
    off = 0x15
    # Skip 2 bytes (padding/flags)
    off += 2

    for i in range(bone_count):
        if off + 8 >= len(data):
            break

        bone = Bone(index=i)

        # Bone hash (4 bytes)
        off += 4

        # Bone name: scan for printable ASCII terminated by non-printable byte
        name_start = off
        name_end = off
        while name_end < min(off + 128, len(data)):
            byte = data[name_end]
            if byte < 0x20 or byte > 0x7E:
                break
            name_end += 1
        bone.name = data[name_start:name_end].decode('ascii', 'replace')
        off = name_end

        # Parent index: 4-byte int immediately follows the name (often
        # after a single null terminator). We scan a small window for
        # -1 or a plausible small int to tolerate varying padding.
        # NOTE: starting the scan at off (name_end) means the null
        # terminator can be picked up as "parent=0". That's a wart of
        # this heuristic parser but can't be strictly fixed without
        # full format reversal — the float validator below catches
        # the downstream damage.
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
            off = name_end + 4  # skip 4 bytes and hope

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

    # Update the true bone count after validation-induced truncation.
    skeleton.bone_count = len(skeleton.bones)
    logger.info("Parsed PAB %s: %d bones", filename, len(skeleton.bones))
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
