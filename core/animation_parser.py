"""PAA animation parser for Crimson Desert.

PAA (PAR ‑ Pearl Abyss aRchive) is Pearl Abyss's proprietary per-bone
animation container. This module parses it down to the level we need
for FBX export.

PAA format — reverse engineered Apr 2026
========================================

The file has TWO major variants (selected by the flag byte at 0x13):

  * ``0x00`` variant  — 242,722 shipping files, gimmick / object poses.
                       Very short; typically 1-2 "keyframes" with a
                       compact record format.
  * ``0xC0`` variant  — 52,075 shipping files, the proper character
                       / NPC animation tracks. This is what the FBX
                       exporter targets.

Both share the same 22-byte preamble:

  [0x00..0x03]  magic    "PAR " (0x20524150 LE)
  [0x04..0x07]  version  0x02030001 (observed constant across 294,805
                         files)
  [0x08..0x0F]  sentinel bytes 02,03,04,05,06,07,08,09 (sequential —
                         likely a layout-invariant format marker)
  [0x10..0x13]  flags    uint32 little-endian. Upper byte is the
                         format selector (0x00 / 0xC0). Low nibble
                         hints at content (0xF = rotation + translation
                         tracks, 0x2F = trivial "attached-to-prop"
                         poses, 0x4F = extended, etc.).
  [0x14..0x15]  str_len  uint16 length of the metadata tag string
                         that follows (0 on files with no tags).

  [0x16..0x16+str_len]   metadata_tags
      UTF-8 text, semicolon-separated, typically Korean category
      tokens plus a numeric asset ID. Example values seen:

        "남자;맨손"                  (Male; Barehand)
        "남자;낚시;629900298;"        (Male; Fishing; <asset-id>;)
        "맥더프; 스테이트머신; 제작; 629923033;"
                                    (McDuff; StateMachine; Creation; <id>;)
        "남자;맨손;일반;대화;여자:작은물건;"
                                    (Male; Barehand; Normal; Conversation;
                                     Female:SmallObject;)

      The tag block sits BEFORE the binary body in the same file; the
      old parser assumed a fixed 0x20 data-start offset and therefore
      read garbage from this region.

Binary body (0xC0 variant, 4-byte-aligned after the tag string)
---------------------------------------------------------------

  Bind-pose block
      Two 40-byte SRT records (10 float32 each):
          [scale.x, scale.y, scale.z,
           rot.x,   rot.y,   rot.z,   rot.w,
           trans.x, trans.y, trans.z]
      Verified against cd_phm_basic_00_00_roofclimb_* : bone 0 rot
      decodes to a unit quaternion ``(0.1736, 0, 0, 0.9848)`` exactly
      (20° around X) — which is the known rest pose of the phm rig.

  Sparse keyframe track
      After a ~28-byte internal header we see a run of 10-byte
      records aligned to 2 bytes:
          int16  axis_x
          int16  axis_y
          int16  axis_z
          fp16   w          (quaternion real component, half float)
          uint16 frame_idx  (sparse — gaps indicate constant between
                             adjacent keys)

      The 3 int16s are scaled /32768. Across a 425-frame roofclimb
      animation the frame_idx field progresses 0,1,2,3,4,5,6,7,9,10,
      11,12,… which confirms sparse sampling.

Known unknowns
--------------

  * We have not yet fully characterised the per-record normalisation
    constraint: raw ``|xyz|²`` plus ``w²`` does not sum to 1 directly.
    The exported quaternion is normalised to unit length on read,
    which yields bounded Euler output downstream (and no longer the
    thousand-degree spans that prompted this rewrite) but the result
    is not yet round-trip-faithful to the in-game playback.
  * The internal header between bind pose and first keyframe (~28
    bytes) carries per-track metadata (track count? bone ids?
    sampling rate?). The current parser skips it and locates the
    keyframe block by scanning for the first 5 consecutive
    incrementing ``frame_idx`` values.
  * The ``0x00`` (idle / gimmick) variant uses a different layout we
    haven't fully reversed — those are passed through with best-effort
    int16 quaternion decoding.

Downstream contract
-------------------

``parse_paa`` returns a :class:`ParsedAnimation` whose ``keyframes``
list is dense on the declared ``frame_count`` grid; gaps in the
sparse encoding are filled by repeating the most recent keyframe so
that downstream consumers (FBX exporter, Blender) see a uniform
per-frame stream.
"""

from __future__ import annotations

import os
import struct
import math
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.animation_parser")

PAR_MAGIC = b"PAR "


@dataclass
class SrtTransform:
    """Scale-Rotation-Translation bind-pose entry for one bone."""
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class AnimationKeyframe:
    """A single keyframe with per-bone quaternion rotations."""
    frame_index: int = 0
    bone_rotations: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class ParsedAnimation:
    """Parsed animation data."""
    path: str = ""
    duration: float = 0.0
    frame_count: int = 0
    bone_count: int = 0
    keyframes: list[AnimationKeyframe] = field(default_factory=list)
    raw_quaternions: list[tuple[float, float, float, float]] = field(default_factory=list)

    # New fields populated by the Apr 2026 reverse-engineering work:
    metadata_tags: str = ""
    bind_pose: list[SrtTransform] = field(default_factory=list)
    format_variant: str = ""   # "tagged" | "untagged" | "link" | "unknown_0x??"
    flags: int = 0             # raw flags@0x10 for format-variant research
    is_link: bool = False      # file embeds a path reference to another asset
    link_target: str = ""      # extracted file-path reference (if is_link)


# ---------------------------------------------------------------------------
# Half-float decoder — Python stdlib has no fp16 until 3.12's ``struct.unpack``
# supports the ``'e'`` format. We provide our own for 3.11 compatibility.
# ---------------------------------------------------------------------------

def _fp16_to_float32(h: int) -> float:
    sign = (h >> 15) & 1
    exp = (h >> 10) & 0x1F
    mant = h & 0x3FF
    if exp == 0:
        if mant == 0:
            v = 0.0
        else:
            v = (mant / 1024.0) * (2.0 ** -14)
    elif exp == 0x1F:
        v = float("inf") if mant == 0 else float("nan")
    else:
        v = (1.0 + mant / 1024.0) * (2.0 ** (exp - 15))
    return -v if sign else v


# ---------------------------------------------------------------------------
# Header + metadata tag parsing
# ---------------------------------------------------------------------------

def _parse_header(data: bytes) -> dict:
    """Decode the 16-byte fixed preamble plus the variable metadata
    tag string.

    Two sub-variants exist, distinguished by whether the PAA ships
    embedded Korean category tags:

      * **Tagged (0xC0 high byte)**: the uint16 at 0x14 is the length
        of a UTF-8 tag string that starts at 0x16. The binary body
        begins at 0x16 + str_len (no further alignment — observed
        data starts on an odd offset in seq_dem_village_*).

      * **Untagged (0x00 high byte)**: there is no tag string. The
        bytes at 0x14 are already part of the binary body — the
        first four are the bone-0 scale.x float. This is what
        roofclimb-style animations do: header is effectively 20
        bytes and the bind pose runs immediately after.

    We disambiguate by inspecting the two candidate entry points:
    if the bytes at 0x14 decode as a plausible scale component (a
    float close to 1.0 or within a normal scale range), we pick
    the untagged layout; otherwise we treat the uint16 as str_len
    and parse the tag block.
    """
    if len(data) < 0x16 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAA file: magic={data[:4]!r}")
    flags = struct.unpack_from("<I", data, 0x10)[0]

    # Robust variant classification. The flag field has 6+ empirical
    # variants across the shipping corpus (surveyed Apr 2026):
    #
    #   0x0000000f (2320)  — untagged, all tracks
    #   0xc000000f (1973)  — tagged,   all tracks
    #   0x000000ca (1316)  — link-style file reference
    #   0xc000004f (1253)  — tagged,   partial tracks
    #   0xc00000cf  (960)  — tagged,   link + tracks
    #   0x000000cf  (446)  — untagged, link + tracks
    #   0xc0000007  (345)  — tagged,   few tracks
    #   0x0000004a  (335)  — link-style reference
    #   0x0000004f  (300)  — untagged, partial tracks
    #   0x00000007  (299)  — untagged, few tracks
    #   0x00000000  (288)  — empty / placeholder
    #   0xc0000002  (107)  — tagged,   minimal
    #   0x00000002   (57)  — untagged, minimal
    #
    # We classify into three robust variants:
    #   - "tagged"   : high byte = 0xC0, carries Korean metadata tag string
    #   - "untagged" : high byte = 0x00, no tag string
    #   - "link"     : low byte in {0x4A, 0xCA, 0x4F, 0xCF} AND the body
    #                  at 0x14 starts with a file-path string ("%char..."
    #                  or similar). These files reference another PAA.
    #
    # Regardless of variant, _parse_bind_pose / multi-track walkers below
    # are tolerant and skip the file cleanly if the data doesn't match
    # any known shape.

    high_byte = (flags >> 24) & 0xFF
    low_byte = flags & 0xFF

    # Primary variant from high byte.
    if high_byte == 0xC0:
        variant = "tagged"
    elif high_byte == 0x00:
        variant = "untagged"
    else:
        variant = f"unknown_0x{high_byte:02X}"

    # Link-style override: files that embed a '%character/...pab' or
    # '%character/...paa' reference to another asset. The `%` byte can
    # sit anywhere in the first ~256 bytes depending on the flag
    # variant (0xCA places it at 24, 0xDA at 28, 0xDF at 108, etc.).
    # A bounded scan is cheap and catches every variant we see in the
    # shipping corpus.
    is_link = False
    link_pct_offset = -1
    scan_end = min(256, len(data) - 16)
    for pct in range(0x14, scan_end):
        if data[pct] != 0x25:  # '%'
            continue
        # Must be followed by 'character/' or another plausible ASCII
        # path prefix. This filters out '%' bytes that happen inside
        # float data.
        tail = data[pct + 1: pct + 11]
        if tail.startswith(b"character/") or tail.startswith(b"effect/") \
                or tail.startswith(b"_character") or tail.startswith(b"map/") \
                or tail.startswith(b"pc/"):
            is_link = True
            link_pct_offset = pct
            break

    str_len = 0
    tags = ""
    body_start = 0x14
    if variant == "tagged":
        str_len = struct.unpack_from("<H", data, 0x14)[0]
        # Sanity-check the length against the file size before trusting it.
        # If the claimed string length is absurd (> 4KB) treat it as a
        # non-string variant and fall back to default body_start.
        if 0 <= str_len < 4096 and 0x16 + str_len <= len(data):
            tag_end = 0x16 + str_len
            try:
                tags = data[0x16:tag_end].rstrip(b"\x00").decode("utf-8", errors="replace")
            except Exception:
                tags = ""
            body_start = tag_end
        else:
            # Invalid str_len — treat as if no tag string.
            str_len = 0
            body_start = 0x14

    if is_link:
        variant = "link"

    return {
        "flags": flags,
        "format_variant": variant,
        "high_byte": high_byte,
        "low_byte": low_byte,
        "str_len": str_len,
        "tags": tags,
        "body_start": body_start,
        "is_link": is_link,
        "link_pct_offset": link_pct_offset,
    }


# ---------------------------------------------------------------------------
# Bind pose extraction
# ---------------------------------------------------------------------------

_SRT_STRIDE = 10 * 4   # 10 float32s per bone record

def _parse_bind_pose(data: bytes, offset: int, max_bones: int = 256) -> tuple[list[SrtTransform], int]:
    """Walk SRT records starting at ``offset`` and stop at the first
    entry that fails the "plausible SRT" check (scale ≈ 1 or unit quat
    constraint broken).

    Several flag variants (0x9F, 0x92, 0xDF, …) insert a 4-byte hash
    field between the header and the first SRT record — so the bind
    pose actually starts at offset + 4, not offset. Rather than
    branching on flags here (which leaks variant logic into the
    low-level walker), we try the given offset first and fall back
    to offset+4 if that decodes zero bones.
    """
    best_bones: list[SrtTransform] = []
    best_after = offset
    for candidate_offset in (offset, offset + 4):
        bones = _walk_srt(data, candidate_offset, max_bones)
        if len(bones) > len(best_bones):
            best_bones = bones
            best_after = candidate_offset + len(bones) * _SRT_STRIDE
        # Any non-trivial win short-circuits — avoids picking up an
        # accidental longer run that starts inside garbage data.
        if len(best_bones) >= 4:
            break
    return best_bones, best_after


def _walk_srt(data: bytes, offset: int, max_bones: int) -> list[SrtTransform]:
    """Inner walker — no fallback, no retries, just decode until the
    plausibility check trips.
    """
    bones: list[SrtTransform] = []
    pos = offset
    while len(bones) < max_bones and pos + _SRT_STRIDE <= len(data):
        vals = struct.unpack_from("<10f", data, pos)
        sx, sy, sz, qx, qy, qz, qw, tx, ty, tz = vals
        # Plausibility heuristics matched against a sample of 30+
        # shipping files:
        #   * Scale magnitudes in [0.01, 10.0]
        #   * Quaternion magnitude in [0.9, 1.1]
        #   * Translation magnitudes within [-1000, 1000] (units are
        #     metres or centimetres depending on source; we don't need
        #     to decide which here)
        scale_ok = all(0.01 <= abs(v) <= 10.0 for v in (sx, sy, sz)) or (sx == 0 and sy == 0 and sz == 0)
        quat_mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        quat_ok = 0.9 <= quat_mag <= 1.1
        trans_ok = all(abs(v) <= 1000.0 for v in (tx, ty, tz))
        if not (scale_ok and quat_ok and trans_ok):
            break
        bones.append(SrtTransform(
            scale=(sx, sy, sz),
            rotation=(qx, qy, qz, qw),
            translation=(tx, ty, tz),
        ))
        pos += _SRT_STRIDE
    return bones


# ---------------------------------------------------------------------------
# Multi-track sparse keyframe extraction
# ---------------------------------------------------------------------------
#
# The 0xC0 format carries ONE rotation track per animated bone. Each
# track is introduced by a 16-byte pair marker:
#
#   Marker 1 (8 bytes):  00 3c 00 3c 00 3c XX 00   (frame_count - 1, usually)
#   Marker 2 (8 bytes):  00 3c 00 3c 00 3c YY 00   (bone-related metadata)
#
# followed by a run of 10-byte keyframe records. The record layout is:
#
#   [0x00..0x01]  uint16  frame_idx   (sparse — not every frame is present)
#   [0x02..0x03]  int16   quat.x      (scale /32768; normalise later)
#   [0x04..0x05]  int16   quat.y
#   [0x06..0x07]  int16   quat.z
#   [0x08..0x09]  fp16    quat.w      (half-precision)
#
# Across the roofclimb corpus we confirmed 77 pairs = 77 animated
# bones. Adjacent-frame |dot| products on a typical pair reach 0.9999
# — truly smooth motion.

_KEY_STRIDE = 10
_PAIR_MARKER = b"\x00\x3c\x00\x3c\x00\x3c"


def _find_track_pairs(data: bytes, search_start: int) -> list[tuple[int, int]]:
    """Locate every 16-byte track-start pair after ``search_start``.

    Markers are 8-byte records whose first 6 bytes spell out three
    fp16 values of 1.0 (the byte signature ``00 3c 00 3c 00 3c``).
    Tracks carry a PAIR of such markers spaced exactly 8 bytes apart.
    A single unpaired marker is a false positive (fp16=1.0 appears
    incidentally as a keyframe value on static bones) and is skipped.
    """
    positions: list[int] = []
    pos = search_start
    while True:
        i = data.find(_PAIR_MARKER, pos)
        if i < 0:
            break
        positions.append(i)
        pos = i + 1
    pairs: list[tuple[int, int]] = []
    i = 0
    while i < len(positions):
        if i + 1 < len(positions) and positions[i + 1] - positions[i] == 8:
            pairs.append((positions[i], positions[i + 1]))
            i += 2
        else:
            # Orphan marker (static-bone incidental). Skip.
            i += 1
    return pairs


def _decode_track(
    data: bytes, kf_start: int, kf_end: int, max_frame_idx: int,
    idx_at_start: bool = True,
) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Decode a single bone's track as 10-byte records.

    ``idx_at_start=True`` (default, untagged-variant layout):
        [uint16 idx, int16 x, int16 y, int16 z, fp16 w]

    ``idx_at_start=False`` (tagged-variant layout, seq_*/encount_*):
        [int16 x, int16 y, int16 z, fp16 w, uint16 idx]

    The stream terminates early if ``frame_idx`` decreases below a
    previously-seen index (next track boundary) or exceeds
    ``max_frame_idx`` (garbage).
    """
    out: list[tuple[int, tuple[float, float, float, float]]] = []
    last_idx = -1
    pos = kf_start
    while pos + _KEY_STRIDE <= kf_end:
        if idx_at_start:
            idx = struct.unpack_from("<H", data, pos)[0]
            ix = struct.unpack_from("<h", data, pos + 2)[0]
            iy = struct.unpack_from("<h", data, pos + 4)[0]
            iz = struct.unpack_from("<h", data, pos + 6)[0]
            fw = struct.unpack_from("<H", data, pos + 8)[0]
        else:
            ix = struct.unpack_from("<h", data, pos)[0]
            iy = struct.unpack_from("<h", data, pos + 2)[0]
            iz = struct.unpack_from("<h", data, pos + 4)[0]
            fw = struct.unpack_from("<H", data, pos + 6)[0]
            idx = struct.unpack_from("<H", data, pos + 8)[0]
        if idx > max_frame_idx + 4 or (last_idx >= 0 and idx < last_idx):
            break
        qx = ix / 32768.0
        qy = iy / 32768.0
        qz = iz / 32768.0
        qw = _fp16_to_float32(fw)
        mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        if mag > 1e-6:
            qx /= mag; qy /= mag; qz /= mag; qw /= mag
        out.append((idx, (qx, qy, qz, qw)))
        last_idx = idx
        pos += _KEY_STRIDE
    return out


def _find_first_monotonic_run(
    data: bytes, start: int, end: int, stride: int, idx_offset: int,
    max_first_idx: int = 2, require_consecutive: bool = True,
) -> int | None:
    """Locate the first offset where five consecutive records at the
    given stride carry incrementing idx values.

    ``max_first_idx``: the first record's idx must be <= this value.
    Tracks beginning at frame 0/1/2 (fresh bones) use the default of 2.
    Later sparse tracks where the first keyframe sits at some non-zero
    frame relax this to the animation's frame_count.

    ``require_consecutive``: if True, idx must strictly increment by 1
    between adjacent records (dense block). If False, just require
    strict monotonic increase (allows sparse tracks with gaps).
    """
    max_scan = min(end, len(data) - 5 * stride)
    for off in range(start, max_scan):
        idxs = [struct.unpack_from("<H", data, off + k * stride + idx_offset)[0] for k in range(5)]
        if idxs[0] > max_first_idx:
            continue
        if require_consecutive:
            if all(idxs[i + 1] == idxs[i] + 1 for i in range(4)):
                return off
        else:
            if all(idxs[i + 1] > idxs[i] for i in range(4)) and idxs[-1] < 2000:
                return off
    return None


def _decode_record(data: bytes, pos: int, stride: int) -> tuple[int, tuple[float, float, float, float]]:
    """Decode one 8-byte (smallest-3) or 10-byte (explicit fp16 w) record.

    Both variants put idx at the END of the record.
    """
    if stride == 10:
        ix = struct.unpack_from("<h", data, pos)[0]
        iy = struct.unpack_from("<h", data, pos + 2)[0]
        iz = struct.unpack_from("<h", data, pos + 4)[0]
        fw = struct.unpack_from("<H", data, pos + 6)[0]
        idx = struct.unpack_from("<H", data, pos + 8)[0]
        qx = ix / 32768.0; qy = iy / 32768.0; qz = iz / 32768.0
        qw = _fp16_to_float32(fw)
    else:  # stride == 8 (smallest-3)
        ix = struct.unpack_from("<h", data, pos)[0]
        iy = struct.unpack_from("<h", data, pos + 2)[0]
        iz = struct.unpack_from("<h", data, pos + 4)[0]
        idx = struct.unpack_from("<H", data, pos + 6)[0]
        qx = ix / 32768.0; qy = iy / 32768.0; qz = iz / 32768.0
        sq = qx * qx + qy * qy + qz * qz
        qw = math.sqrt(max(0.0, 1.0 - sq))
    mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if mag > 1e-6:
        qx /= mag; qy /= mag; qz /= mag; qw /= mag
    return idx, (qx, qy, qz, qw)


def _enumerate_track_starts(
    data: bytes, start: int, stride: int, idx_offset: int,
) -> list[int]:
    """Return every offset at or after ``start`` where a fresh track
    begins — identified by 5 consecutive records whose ``frame_idx``
    values form a strictly-increasing sparse sequence starting at 0,
    AND whose first record decodes to a non-trivial quaternion.

    Filters:
      * ``idx[0] == 0``           — track begins at frame 0
      * ``idx[-1] - idx[0] < 100``— sparse span cap (real tracks have
                                     spans of 4..50; bogus w_hi-byte
                                     reads produce 100+).
      * First record must have at least one non-zero component
        (rejects all-zero "records" inside padding regions).
    """
    starts: list[int] = []
    end = len(data) - 5 * stride - 2
    off = start
    while off < end:
        idxs = [struct.unpack_from("<H", data, off + k * stride + idx_offset)[0] for k in range(5)]
        if (all(idxs[i + 1] > idxs[i] for i in range(4))
                and idxs[0] == 0
                and idxs[-1] - idxs[0] < 100):
            # Verify the first record has non-zero rotation components.
            # Raw bytes of an all-zero record are always (0, 0, 0, 0)
            # after decode, which is a sentinel for "padding", not a
            # real keyframe.
            first_rec_nonzero = any(data[off + i] != 0 for i in range(stride - 2))
            if first_rec_nonzero:
                starts.append(off)
                off += stride
                continue
        off += 1
    return starts


def _walk_tagged_multitrack(
    data: bytes, start: int, max_frame_idx: int,
) -> list[list[tuple[int, tuple[float, float, float, float]]]]:
    """Walk tagged-variant multi-track keyframe region.

    Tagged files (seq_*/encount_*) do NOT carry the ``00 3c`` pair
    markers. Tracks are separated only by frame_idx resets. We
    empirically find ~53 tracks per seq_* file using stride=10 and 2
    more using stride=8 (mixed-stride layout — the majority are
    stride=10).

    Algorithm:
      * Enumerate every offset where ``idx[0]=0`` begins a monotonic
        run, at both 10-byte and 8-byte strides.
      * Sort the combined list by offset.
      * Walk each track from its start until either idx resets,
        exceeds the plausible frame count, or we reach the next
        recognised start.
    """
    # Collect all possible track starts at both strides.
    starts10 = [(off, 10) for off in _enumerate_track_starts(data, start, 10, 8)]
    starts8 = [(off, 8) for off in _enumerate_track_starts(data, start, 8, 6)]
    all_starts = sorted(set(starts10 + starts8))
    if not all_starts:
        return []

    # Resolve overlaps: if two candidates with different strides are
    # within a few bytes of each other, prefer the first. The walker
    # won't re-enter a region it has already consumed.
    tracks: list[list[tuple[int, tuple[float, float, float, float]]]] = []
    consumed_until = 0
    for off, stride in all_starts:
        if off < consumed_until:
            continue
        idx_off = stride - 2
        pos = off
        track: list[tuple[int, tuple[float, float, float, float]]] = []
        last_idx = -1
        while pos + stride <= len(data):
            idx = struct.unpack_from("<H", data, pos + idx_off)[0]
            if idx > max_frame_idx + 4:
                break
            if last_idx >= 0 and idx < last_idx:
                break
            idx_val, quat = _decode_record(data, pos, stride)
            track.append((idx_val, quat))
            last_idx = idx_val
            pos += stride
        if track:
            tracks.append(track)
            consumed_until = pos
    return tracks


def _find_keyframe_block(data: bytes, start: int) -> int | None:
    """Legacy single-track finder kept for backward compatibility.

    The new multi-track walker ``_find_track_pairs`` supersedes this
    for 0xC0 files. Retained so the previously-passing tests continue
    to work.
    """
    limit = min(len(data) - 5 * _KEY_STRIDE, start + 4096)
    for off in range(start, limit, 2):
        idxs = [struct.unpack_from("<H", data, off + k * _KEY_STRIDE + 8)[0] for k in range(5)]
        if all(idxs[i + 1] == idxs[i] + 1 for i in range(4)) and idxs[0] < 65530:
            return off
    return None


def _decode_keyframes(data: bytes, start: int) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Legacy single-track decoder (idx-at-END variant). Only used by
    the untagged fallback path now that the primary path is the
    multi-track walker.
    """
    out: list[tuple[int, tuple[float, float, float, float]]] = []
    pos = start
    last_idx = -1
    while pos + _KEY_STRIDE <= len(data):
        ix = struct.unpack_from("<h", data, pos)[0]
        iy = struct.unpack_from("<h", data, pos + 2)[0]
        iz = struct.unpack_from("<h", data, pos + 4)[0]
        fw = struct.unpack_from("<H", data, pos + 6)[0]
        idx = struct.unpack_from("<H", data, pos + 8)[0]
        if idx != 0 and idx < last_idx:
            break
        qx = ix / 32768.0
        qy = iy / 32768.0
        qz = iz / 32768.0
        qw = _fp16_to_float32(fw)
        mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        if mag > 1e-6:
            qx /= mag; qy /= mag; qz /= mag; qw /= mag
        out.append((idx, (qx, qy, qz, qw)))
        last_idx = idx
        pos += _KEY_STRIDE
    return out


def _densify_sparse(
    keyframes: list[tuple[int, tuple[float, float, float, float]]],
    frame_count: int,
) -> list[tuple[float, float, float, float]]:
    """Fill gaps by repeating the previous keyframe (step interpolation).

    PAA's sparse encoding implies the runtime interpolates between
    held keys; the FBX curve cache is already dense, so this step
    interpolation is the simplest correct thing to do. A future
    iteration can swap in slerp.
    """
    if not keyframes:
        return [(0.0, 0.0, 0.0, 1.0)] * frame_count
    dense: list[tuple[float, float, float, float]] = []
    next_key = 0
    last_quat = keyframes[0][1]
    for f in range(frame_count):
        while next_key < len(keyframes) and keyframes[next_key][0] <= f:
            last_quat = keyframes[next_key][1]
            next_key += 1
        dense.append(last_quat)
    return dense


# ---------------------------------------------------------------------------
# Legacy int16 fallback (pre-Apr-2026 behaviour) — still needed for the
# 0x00 variant and for test / debugging synthetic data.
# ---------------------------------------------------------------------------

def _legacy_int16_decode(
    data: bytes, offset: int, bone_count_hint: int
) -> tuple[list[tuple[float, float, float, float]], int, int]:
    total_quats = max(0, (len(data) - offset) // 8)
    quats: list[tuple[float, float, float, float]] = []
    for i in range(total_quats):
        off = offset + i * 8
        x, y, z, w = struct.unpack_from("<hhhh", data, off)
        qx = x / 32767.0; qy = y / 32767.0; qz = z / 32767.0; qw = w / 32767.0
        length = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        if length > 1e-4:
            qx /= length; qy /= length; qz /= length; qw /= length
        quats.append((qx, qy, qz, qw))
    # Pick a bone count that divides evenly if possible.
    if bone_count_hint > 0 and total_quats % bone_count_hint == 0:
        bc = bone_count_hint
    else:
        bc = 0
        for cand in [1, 2, 4, 8, 15, 16, 32, 47, 64, 111, 128, 160, 169, 192, 218, 256]:
            if cand > 0 and total_quats % cand == 0:
                bc = cand
                break
        if bc == 0 and bone_count_hint > 0:
            bc = bone_count_hint
    frames = (total_quats // bc) if bc else 0
    return quats, bc, frames


# ---------------------------------------------------------------------------
# Single-pose SRT emitter
# ---------------------------------------------------------------------------
#
# Many small variants (0x9F, 0x92, 0xDF, 0xDA, 0xCA) carry a compact
# pose instead of a real animation: a sequence of 40-byte SRT records
# (scale.xyz + rot.xyzw + trans.xyz as float32) with no keyframe
# tracks. Without this emitter these files decode to zero bones and
# downstream FBX export has nothing to show — before the fix, the
# reference sample of 40 shipping PAAs came back with 52% zero-track
# files even though the pose data is right there in the header.

def _emit_single_pose_frame(
    result: ParsedAnimation,
    data: bytes,
    pose_start: int,
    max_bones: int = 256,
) -> None:
    """Parse a run of 40-byte SRT records and emit them as a single
    pose keyframe. No-op if the data at ``pose_start`` doesn't look
    like a valid SRT block.

    Writes ``bind_pose``, populates ``keyframes`` with a single frame
    whose rotations come from each bone's bind quaternion, and sets
    ``bone_count`` / ``frame_count`` / ``duration`` accordingly.
    """
    bones, _ = _parse_bind_pose(data, pose_start, max_bones=max_bones)
    if not bones:
        return
    result.bind_pose = bones
    result.bone_count = len(bones)
    result.frame_count = 1
    result.duration = 1.0 / 30.0
    kf = AnimationKeyframe(frame_index=0)
    for b in bones:
        kf.bone_rotations.append(b.rotation)
    result.keyframes.append(kf)
    result.raw_quaternions = [b.rotation for b in bones]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_paa(data: bytes, filename: str = "", expected_bone_count: int = 0) -> ParsedAnimation:
    """Decode a Crimson Desert .paa animation file.

    Apr 2026: this entry point now dispatches to the byte-level
    reverse-engineered :mod:`core.animation_parser_v2` parser FIRST,
    which decodes the bone-major fp16-quaternion format directly.
    The legacy heuristic path below only runs when v2 returns zero
    tracks (e.g. variants we haven't yet fully reverse engineered).

    See module docstring for the detailed format description.
    """
    if len(data) < 32 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAA file: {data[:4]!r}")

    # Try the clean v2 parser first — it decodes the actual fp16
    # quaternion stream (no heuristic guesses). For files where v2
    # finds no bone tracks, fall through to v3 (SRT-float/child_idle
    # variant) and then the legacy path.
    from core.animation_parser_v2 import parse_paa_v2, densify_track
    v2 = parse_paa_v2(data, filename)
    if not v2.tracks:
        # Try v3 (0xC000000F-SRT-float variant, no 3c separator).
        # If v3 finds tracks, reshape them into a V2-compatible result.
        try:
            from core.animation_parser_v3 import parse_paa_v3
            v3 = parse_paa_v3(data, filename)
        except Exception as ex:
            logger.debug("v3 parser failed for %s: %s", filename, ex)
            v3 = None
        if v3 is not None and v3.tracks:
            # Reshape V3 tracks into the V2 format so the rest of this
            # function treats them identically.
            from core.animation_parser_v2 import ParsedAnimationV2, BoneTrack as V2BoneTrack
            v2 = ParsedAnimationV2(
                path=filename,
                flags=v3.flags,
                metadata_tags=v3.metadata_tags,
                duration=v3.duration,
                frame_count=v3.frame_count,
                tracks=[
                    V2BoneTrack(bind_quat=t.bind_quat,
                                keyframes=list(t.keyframes))
                    for t in v3.tracks
                ],
            )
            logger.info(
                "PAA %s routed through v3 SRT-float parser: %d tracks",
                filename, len(v2.tracks),
            )
    if v2.tracks:
        result = ParsedAnimation(
            path=filename,
            metadata_tags=v2.metadata_tags,
            duration=v2.duration,
            frame_count=v2.frame_count,
            bone_count=len(v2.tracks),
            flags=v2.flags,
            format_variant="v2",
        )
        if v2.frame_count > 0:
            dense_per_bone = [
                densify_track(t, v2.frame_count) for t in v2.tracks
            ]
            for f in range(v2.frame_count):
                kf = AnimationKeyframe(frame_index=f)
                for bone_dense in dense_per_bone:
                    kf.bone_rotations.append(bone_dense[f])
                result.keyframes.append(kf)
            # Legacy raw_quaternions field (flat list of bone-0 quats)
            result.raw_quaternions = [
                kf.bone_rotations[0] for kf in result.keyframes
            ]
        # Bind pose from each track's bind quaternion
        for t in v2.tracks:
            result.bind_pose.append(SrtTransform(
                rotation=t.bind_quat,
                translation=(0.0, 0.0, 0.0),
                scale=(1.0, 1.0, 1.0),
            ))
        logger.info(
            "Parsed PAA %s via v2: tracks=%d frames=%d duration=%.2fs",
            filename, len(v2.tracks), v2.frame_count, v2.duration,
        )
        return result

    # v2 found nothing — fall through to the legacy heuristic path
    # for variants we haven't yet reverse engineered.
    result = ParsedAnimation(path=filename)

    header = _parse_header(data)
    result.metadata_tags = header["tags"]
    result.format_variant = header["format_variant"]
    result.flags = header["flags"]
    result.is_link = header.get("is_link", False)

    body_start = header["body_start"]
    flags = header["flags"]

    # --- Link variant: extract the embedded file-path reference ---
    # These files point at another PAA / skeleton / mesh asset. They
    # don't carry their own animation data — the referenced file is
    # the actual animation.
    if header["format_variant"] == "link":
        # The header walker already located the exact offset of the
        # '%' byte; fall back to scanning the first 256 bytes when
        # that field is missing (pre-fix callers).
        scan_start = header.get("link_pct_offset", -1)
        if scan_start < 0:
            scan_start = data.find(b"%character/", 0x14, min(256, len(data)))
        if scan_start >= 0:
            end = scan_start
            # Link path is an ASCII file path that ends at a Pearl
            # Abyss asset extension (.pab / .paa / .pac / .pam /
            # .pamlod). Anything past that is non-path payload
            # (floats / hashes / SRT bytes) that happen to decode
            # as printable ASCII. The old extractor walked every
            # printable byte and produced "phm_01.pabVUuA>" style
            # garbage.
            while end < min(len(data), scan_start + 1024):
                b = data[end]
                if b < 0x20 or b > 0x7E:
                    break
                end += 1
            raw = data[scan_start:end].decode("ascii", errors="replace")
            # Truncate at the first known extension.
            for ext in (".pab", ".paa", ".pac", ".pam", ".pamlod",
                        ".pabc", ".pabgb"):
                idx = raw.lower().find(ext)
                if idx >= 0:
                    raw = raw[: idx + len(ext)]
                    break
            result.link_target = raw
        logger.info(
            "Parsed PAA %s: variant=link flags=0x%08x target=%r",
            filename, flags, result.link_target,
        )
        # Link files also carry embedded SRT pose data after the path
        # string — emit a single-frame animation so downstream FBX
        # export has something visible to show. Walk backwards from
        # the end of the path to find the start of the SRT block.
        pose_start = 0
        if scan_start >= 0:
            path_len = len(result.link_target)
            pose_start = scan_start + path_len
            # 4-byte align
            pose_start = (pose_start + 3) & ~3
        else:
            pose_start = 0x14
        _emit_single_pose_frame(result, data, pose_start)
        # Link-only files with no SRT payload (pure skeleton refs)
        # still need a stub frame so the FBX exporter has something
        # to hang the skeleton on. One identity-pose keyframe is
        # enough to keep the export pipeline's duration-resolution
        # step happy without pretending the file has real data.
        if result.frame_count == 0:
            result.bone_count = 1
            result.frame_count = 1
            result.duration = 1.0 / 30.0
            kf = AnimationKeyframe(frame_index=0)
            kf.bone_rotations.append((0.0, 0.0, 0.0, 1.0))
            result.keyframes.append(kf)
            result.raw_quaternions = [(0.0, 0.0, 0.0, 1.0)]
        return result

    # --- Both variants: parse bind pose + multi-track sparse keyframes ---
    # (The earlier "only 0xC0 has SRT data" assumption was wrong — the
    # untagged roofclimb-style files also use the SRT + sparse layout,
    # just without the tag string header. Variants with unusual flag
    # values fall through this block too — the bind-pose + track walkers
    # are tolerant to absent data.)
    if header["format_variant"] in ("tagged", "untagged") or header["format_variant"].startswith("unknown"):
        bind_pose, after_bind = _parse_bind_pose(data, body_start)
        result.bind_pose = bind_pose

        # Dispatch to the variant-specific multi-track walker.
        # Untagged: pair-marker-driven (each `00 3c`-pair is one track).
        # Tagged:   idx-reset-driven, idx-at-END record layout.
        max_frame_idx = 2000

        tracks: list[list[tuple[int, tuple[float, float, float, float]]]] = []
        if header["format_variant"] == "untagged":
            pairs = _find_track_pairs(data, after_bind)
            for pi, (_m1, m2) in enumerate(pairs):
                kf_start = m2 + 8
                kf_end = pairs[pi + 1][0] if pi + 1 < len(pairs) else len(data)
                track = _decode_track(data, kf_start, kf_end, max_frame_idx, idx_at_start=True)
                tracks.append(track)
        elif header["format_variant"] == "tagged":
            tracks = _walk_tagged_multitrack(data, after_bind, max_frame_idx)

        # Global frame count = max frame_idx across all tracks + 1.
        max_observed_idx = 0
        for track in tracks:
            for idx, _q in track:
                if idx > max_observed_idx:
                    max_observed_idx = idx
        if max_observed_idx > 0:
            result.frame_count = max_observed_idx + 1

        # Densify each track and assemble per-frame keyframes. Bone
        # count comes from the track count (each pair = one bone); if
        # we've also got a bind pose it should match or be smaller.
        if tracks:
            result.bone_count = len(tracks)
            dense_tracks = [_densify_sparse(t, result.frame_count) for t in tracks]
            for f in range(result.frame_count):
                kf = AnimationKeyframe(frame_index=f)
                for dense in dense_tracks:
                    kf.bone_rotations.append(dense[f] if f < len(dense) else (0.0, 0.0, 0.0, 1.0))
                result.keyframes.append(kf)
            # Preserve a flat raw-quat list for legacy consumers.
            result.raw_quaternions = [kf.bone_rotations[0] for kf in result.keyframes]

        if not result.keyframes and result.bind_pose:
            # No tracks decoded — fall back to a single-frame bind pose.
            result.bone_count = max(1, len(result.bind_pose))
            result.frame_count = 1
            kf = AnimationKeyframe(frame_index=0)
            for b in result.bind_pose:
                kf.bone_rotations.append(b.rotation)
            result.keyframes.append(kf)
            result.raw_quaternions = [b.rotation for b in result.bind_pose]

        # Last-resort identity-pose stub — shipping corpus contains
        # tiny 32-byte "duration only" files with flag=0x20 that
        # have no bind pose AND no tracks. Without this stub the
        # export pipeline raises because it can't determine a
        # duration; with it the FBX comes out as the skeleton's
        # rest pose and the user can still preview the skeleton.
        if not result.keyframes:
            result.bone_count = 1
            result.frame_count = 1
            kf = AnimationKeyframe(frame_index=0)
            kf.bone_rotations.append((0.0, 0.0, 0.0, 1.0))
            result.keyframes.append(kf)
            result.raw_quaternions = [(0.0, 0.0, 0.0, 1.0)]

        # Default duration if not supplied: assume 30 fps.
        if result.duration <= 0 and result.frame_count > 0:
            result.duration = result.frame_count / 30.0

        logger.info(
            "Parsed PAA %s: variant=%s tags=%r bind_pose_bones=%d animated_bones=%d frames=%d duration=%.2fs",
            filename, header["format_variant"], result.metadata_tags,
            len(result.bind_pose), len(tracks), result.frame_count, result.duration,
        )
        return result

    # --- 0x00 variant / unknown: legacy fallback ---
    quats, bc, frames = _legacy_int16_decode(data, body_start, expected_bone_count)
    result.raw_quaternions = quats
    result.bone_count = bc or expected_bone_count
    result.frame_count = frames if bc else 0
    if result.bone_count > 0 and result.frame_count > 0:
        for f in range(result.frame_count):
            kf = AnimationKeyframe(frame_index=f)
            for b in range(result.bone_count):
                qi = f * result.bone_count + b
                if qi < len(quats):
                    kf.bone_rotations.append(quats[qi])
            result.keyframes.append(kf)

    logger.info(
        "Parsed PAA %s: variant=%s tags=%r %d quats, %d bones, %d frames",
        filename, header["format_variant"], result.metadata_tags,
        len(quats), result.bone_count, result.frame_count,
    )
    return result


def parse_paa_with_resolution(
    data: bytes,
    filename: str = "",
    expected_bone_count: int = 0,
    vfs=None,
    max_hops: int = 5,
) -> ParsedAnimation:
    """Parse a PAA; if it's a link-variant that points at another
    asset, follow the reference through ``vfs`` until we reach a
    real animation (or we give up).

    This is the production entry point the export pipeline should
    use — ``parse_paa`` alone returns an empty animation for the
    19% of the corpus that uses link-variant references.

    When ``vfs`` is None we fall back to plain :func:`parse_paa`.
    """
    result = parse_paa(data, filename, expected_bone_count)
    if not result.is_link or not result.link_target or vfs is None:
        return result

    from core.paa_link_resolver import resolve_link
    logger.info("Link-variant PAA %s -> resolving %r",
                filename, result.link_target)

    for hop in range(max_hops):
        target_bytes = resolve_link(result.link_target, vfs, max_hops=1)
        if target_bytes is None:
            logger.warning(
                "Link-variant target %r not found in VFS; returning "
                "the shell animation", result.link_target,
            )
            return result
        resolved = parse_paa(
            target_bytes,
            os.path.basename(result.link_target),
            expected_bone_count,
        )
        if not resolved.is_link:
            logger.info(
                "Link-variant resolved to real animation "
                "(%d tracks, %d frames) after %d hop(s)",
                resolved.bone_count, resolved.frame_count, hop + 1,
            )
            return resolved
        result = resolved  # keep chasing
    logger.warning("link-variant max_hops exceeded for %r", filename)
    return result


def is_animation_file(path: str) -> bool:
    """Check if a file is an animation file."""
    return os.path.splitext(path.lower())[1] == ".paa"
