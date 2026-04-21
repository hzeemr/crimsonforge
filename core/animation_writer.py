"""PAA animation writer — the inverse of core.animation_parser_v2.

Round-trips a ParsedAnimationV2 (or equivalently-shaped data) back
into a valid Pearl Abyss PAA binary. The target format is the
"tagged 0xC000000F" variant documented in
:mod:`core.animation_parser_v2`:

  Header (fixed 0x16 bytes)
    [0x00..0x03]   PAR magic            b"PAR "
    [0x04..0x0F]   sentinel bytes       b"\\x02\\x03\\x00\\x01\\x02\\x03\\x04\\x05\\x06\\x07\\x08\\x09"
    [0x10..0x13]   flags uint32 LE      0xC000000F by default
    [0x14..0x15]   tag_length uint16 LE

  [0x16..0x16+tag_len]   UTF-8 tag string + optional trailing null

  [after_tags..first_separator]
    A small global header we don't fully understand. We emit a
    conservative placeholder that the v2 parser (and every Pearl
    Abyss tool we tested) accepts: `6c 14 bb 50 02 00 00 00 00`
    as the trailing marker (see real samples).

  Per-bone blocks
    [5 B]    separator  b"\\x3c\\x00\\x3c\\x00\\x3c"
    [4 B]    uint32 LE keyframe count
    [6 B]    3 fp16 bind xyz (W implicit = sqrt(1 - x² - y² - z²))
    [N × 10 B] keyframe records:
      [2 B]  fp16 quaternion W
      [2 B]  uint16 LE frame index
      [6 B]  3 fp16 quaternion XYZ

The current implementation handles the 99%-case the community
asked for (Oblivionknight's "i couldnt get FBX back in") and
round-trips V2-parsed animations byte-for-byte-comparable. Tags +
file hashes may differ from the source because we regenerate them
from the input rather than preserving the original bytes — a
bit-exact preserve-original-header mode is a follow-up.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger("core.animation_writer")


# ── Constants ──────────────────────────────────────────────────────────

PAR_MAGIC = b"PAR "
PAR_SENTINEL = bytes([0x02, 0x03, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
                      0x06, 0x07, 0x08, 0x09])
DEFAULT_FLAGS_TAGGED = 0xC000000F
BONE_BLOCK_SEPARATOR = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])
GLOBAL_HEADER_MARKER = bytes([0x6c, 0x14, 0xbb, 0x50,
                              0x02, 0x00, 0x00, 0x00, 0x00])


# ── fp16 encoding ─────────────────────────────────────────────────────

def _f32_to_fp16(v: float) -> int:
    """Encode a float32 to a 16-bit half-precision word.

    Uses struct's ``e`` format (IEEE 754 half-precision, Python
    3.6+). Handles sign, exponent range, denormals correctly.
    """
    # Clamp to fp16 range to avoid NaN/Inf when we write
    if v > 65504.0:
        v = 65504.0
    elif v < -65504.0:
        v = -65504.0
    packed = struct.pack("<e", v)
    return struct.unpack("<H", packed)[0]


# ── Data structures ───────────────────────────────────────────────────

@dataclass
class WriterTrack:
    """Single bone's keyframe stream for :func:`serialize_paa`."""
    bind_quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    keyframes: list[tuple[int, float, float, float, float]] = field(default_factory=list)
    # (frame_index, qx, qy, qz, qw) per keyframe


# ── Writer entrypoints ────────────────────────────────────────────────

def serialize_paa(
    tracks: list[WriterTrack],
    *,
    tag: str = "",
    flags: int = DEFAULT_FLAGS_TAGGED,
    hash1: int = 0,
    hash2: int = 0,
) -> bytes:
    """Return a new PAA byte stream carrying the given tracks.

    Args:
        tracks: list of :class:`WriterTrack`. One per animated bone.
        tag: UTF-8 metadata tag string (Korean category tokens + asset
            id in real files; any text is fine for mod usage).
        flags: uint32 stored at offset 0x10. Default 0xC000000F is the
            "tagged, all tracks" variant our readers handle.
        hash1, hash2: ignored in this release. Future revisions may
            regenerate valid integrity hashes from the file contents.

    Returns:
        bytes of a valid PAA file that round-trips back through
        :func:`core.animation_parser_v2.parse_paa_v2` into equivalent
        tracks.
    """
    out = bytearray()

    # Header
    out.extend(PAR_MAGIC)
    out.extend(PAR_SENTINEL)
    out.extend(struct.pack("<I", flags))

    # Tag string (length-prefixed UTF-8, null-terminated)
    tag_bytes = tag.encode("utf-8") + b"\x00" if tag else b""
    out.extend(struct.pack("<H", len(tag_bytes)))
    out.extend(tag_bytes)

    # Global header — 4 zero-ish pad bytes + marker. We don't
    # preserve the original file's first-float data (scale / duration);
    # downstream consumers that care should carry it explicitly.
    out.extend(b"\x00" * 7)
    out.extend(GLOBAL_HEADER_MARKER)

    # Per-bone blocks
    for track in tracks:
        out.extend(BONE_BLOCK_SEPARATOR)
        # 4 bytes: keyframe count
        out.extend(struct.pack("<I", len(track.keyframes)))
        # 6 bytes: 3 fp16 bind xyz (W reconstructed by reader)
        bx, by, bz, _bw = track.bind_quat
        out.extend(struct.pack("<H", _f32_to_fp16(bx)))
        out.extend(struct.pack("<H", _f32_to_fp16(by)))
        out.extend(struct.pack("<H", _f32_to_fp16(bz)))
        # N × 10-byte keyframe records
        for frame_idx, qx, qy, qz, qw in track.keyframes:
            # Guard: clamp frame index to uint16 range
            fi = max(0, min(0xFFFF, int(frame_idx)))
            out.extend(struct.pack("<H", _f32_to_fp16(qw)))
            out.extend(struct.pack("<H", fi))
            out.extend(struct.pack("<H", _f32_to_fp16(qx)))
            out.extend(struct.pack("<H", _f32_to_fp16(qy)))
            out.extend(struct.pack("<H", _f32_to_fp16(qz)))

    logger.info(
        "Serialized PAA: %d bytes, %d tracks, %d total keyframes, tag=%r",
        len(out), len(tracks), sum(len(t.keyframes) for t in tracks), tag,
    )
    return bytes(out)


def tracks_from_parsed(parsed) -> list[WriterTrack]:
    """Convert a :class:`ParsedAnimationV2` (or V3) into writer tracks.

    ``parsed.tracks[i]`` has a ``bind_quat`` (xyzw) and
    ``keyframes`` list of ``(frame, qx, qy, qz, qw)`` tuples which is
    EXACTLY the WriterTrack shape. This adapter is tiny but it makes
    round-trip tests explicit: parse → tracks_from_parsed →
    serialize_paa → parse → must equal original tracks.
    """
    return [
        WriterTrack(
            bind_quat=t.bind_quat,
            keyframes=list(t.keyframes),
        )
        for t in parsed.tracks
    ]


# ---------------------------------------------------------------------------
# Untagged (0x0000000F) variant writer
# ---------------------------------------------------------------------------

DEFAULT_FLAGS_UNTAGGED = 0x0000000F


def serialize_paa_untagged(
    tracks: list[WriterTrack],
    *,
    flags: int = DEFAULT_FLAGS_UNTAGGED,
) -> bytes:
    """Write the untagged variant (flag high byte 0x00, no Korean tag
    string). Same record format as the tagged version but with
    ``tag_len == 0``.
    """
    out = bytearray()
    out.extend(PAR_MAGIC)
    out.extend(PAR_SENTINEL)
    out.extend(struct.pack("<I", flags))
    out.extend(struct.pack("<H", 0))   # tag_len = 0
    out.extend(b"\x00" * 7)
    out.extend(GLOBAL_HEADER_MARKER)

    for track in tracks:
        out.extend(BONE_BLOCK_SEPARATOR)
        out.extend(struct.pack("<I", len(track.keyframes)))
        bx, by, bz, _bw = track.bind_quat
        out.extend(struct.pack("<H", _f32_to_fp16(bx)))
        out.extend(struct.pack("<H", _f32_to_fp16(by)))
        out.extend(struct.pack("<H", _f32_to_fp16(bz)))
        for frame_idx, qx, qy, qz, qw in track.keyframes:
            fi = max(0, min(0xFFFF, int(frame_idx)))
            out.extend(struct.pack("<H", _f32_to_fp16(qw)))
            out.extend(struct.pack("<H", fi))
            out.extend(struct.pack("<H", _f32_to_fp16(qx)))
            out.extend(struct.pack("<H", _f32_to_fp16(qy)))
            out.extend(struct.pack("<H", _f32_to_fp16(qz)))

    return bytes(out)


# ---------------------------------------------------------------------------
# SRT-float / v3 variant writer (no bone-block separator, reordered XYZW)
# ---------------------------------------------------------------------------

# In v3 we only need the 4-byte portion of the marker;
# serialize_paa's 9-byte GLOBAL_HEADER_MARKER was designed for the
# tagged variant where the extra padding makes the block count sit
# at a known offset. v3 parsers scan for the first valid block so
# 4 bytes is enough.
_V3_MARKER = GLOBAL_HEADER_MARKER[:4]


def serialize_paa_v3(
    tracks: list[WriterTrack],
    *,
    tag: str = "",
    flags: int = DEFAULT_FLAGS_TAGGED,
    stride: int = 10,
) -> bytes:
    """Write the SRT-float / child_idle variant.

    Layout differs from the tagged/untagged writer:
      * NO ``3c 00 3c 00 3c`` between bone blocks — blocks concatenate
      * Block header is ``[uint16 keyframe_count]`` (not uint32)
      * Records reorder to ``[uint16 frame][3 fp16 xyz][fp16 W]``
      * Supports stride=10 (explicit W) or stride=8 (W implicit) per
        caller preference. The real game mixes strides per-bone; this
        writer picks ONE stride for the whole file for simplicity —
        callers that need mixed strides have to manually emit blocks.

    Args:
        stride: 10 (explicit W) or 8 (W implicit, reconstructed from xyz)
    """
    if stride not in (8, 10):
        raise ValueError(f"stride must be 8 or 10, got {stride}")

    out = bytearray()
    out.extend(PAR_MAGIC)
    out.extend(PAR_SENTINEL)
    out.extend(struct.pack("<I", flags))
    tag_bytes = tag.encode("utf-8") + b"\x00" if tag else b""
    out.extend(struct.pack("<H", len(tag_bytes)))
    out.extend(tag_bytes)
    # v3 header: just the 4-byte marker immediately before the first
    # bone block. The parser's _find_first_bone_block() scans a 64-byte
    # window after the marker for the first valid [uint16 count] so a
    # minimal layout is fine.
    out.extend(b"\x00" * 4)   # small pre-marker pad
    out.extend(_V3_MARKER)     # 4 bytes only (not the full 9-byte
                               # GLOBAL_HEADER_MARKER used by tagged)

    for track in tracks:
        # uint16 count (v3 format uses 2-byte count, not 4)
        out.extend(struct.pack("<H", len(track.keyframes)))
        for frame_idx, qx, qy, qz, qw in track.keyframes:
            fi = max(0, min(0xFFFF, int(frame_idx)))
            out.extend(struct.pack("<H", fi))
            out.extend(struct.pack("<H", _f32_to_fp16(qx)))
            out.extend(struct.pack("<H", _f32_to_fp16(qy)))
            out.extend(struct.pack("<H", _f32_to_fp16(qz)))
            if stride == 10:
                out.extend(struct.pack("<H", _f32_to_fp16(qw)))
            # stride 8: W is implicit, dropped from output

    return bytes(out)


# ---------------------------------------------------------------------------
# Unified entry point — dispatches by variant
# ---------------------------------------------------------------------------

def serialize_paa_for_variant(
    tracks: list[WriterTrack],
    variant: str,
    *,
    tag: str = "",
    stride: int = 10,
) -> bytes:
    """Route to the correct serializer by variant name.

    ``variant`` values:
      * ``"tagged"``   — flag 0xC000000F, with Korean tag (serialize_paa)
      * ``"untagged"`` — flag 0x0000000F, no tag (serialize_paa_untagged)
      * ``"v3"`` or ``"srt"`` — child_idle layout (serialize_paa_v3)
    """
    v = variant.lower()
    if v == "tagged":
        return serialize_paa(tracks, tag=tag)
    if v == "untagged":
        return serialize_paa_untagged(tracks)
    if v in ("v3", "srt", "srt-float", "child_idle"):
        return serialize_paa_v3(tracks, tag=tag, stride=stride)
    raise ValueError(f"unknown variant {variant!r}")
