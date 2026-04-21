"""Parser for the ``.paa_metabin`` sidecar that ships alongside every
``.paa`` animation file.

Format origin
=============

The metabin is Pearl Abyss's proprietary typed binary serialisation of
an ``AnimationMetaData`` class. The class schema lives in the game's
runtime RTTI tables, which we don't have. What we DO have — from
byte-level analysis of the shipping 149,869-file corpus — is the
following empirical understanding:

Fixed preamble (80 bytes, identical across all 149K metabins)
-------------------------------------------------------------

  [0x00..0x03]  0xFF 0xFF 0x04 0x00       — format magic
  [0x04..0x0D]  10 bytes padding / zeroed — (was likely a file hash
                                             field in an earlier
                                             format revision)
  [0x0E..0x0F]  uint16 = 15               — schema field (constant)
  [0x10..0x11]  uint16 = 0                — padding
  [0x12..0x13]  uint16 = 1                — namespace count
  [0x14..0x17]  uint32 = 17               — length of next string
  [0x18..0x29]  b"AnimationMetaData\x00"   — class name (17 + 1 bytes)
  [0x2A..0x2E]  5 bytes padding
  [0x2F..0x32]  uint32 = 1                — class count
  [0x33..0x36]  uint32 = 0x00000051       — schema field / offset
  [0x37..0x3A]  4 bytes zero
  [0x3B..0x42]  8 bytes of 0xFF           — sentinel "no parent"
  [0x43..0x46]  uint32 = 75               — schema property count
  [0x47..0x4A]  uint32 = 6                — schema sub-field count
  [0x4B..0x4F]  5 bytes padding

Per-file data block (0x50 onwards, variable length)
---------------------------------------------------

Here the schema gets proprietary. Through careful comparison across
files we have identified the following field kinds, in approximate
order of appearance:

  1. Name / category tags (similar to the PAA's embedded Korean text)
  2. Per-bone bounding boxes (sequences of 3–5 float32 values each)
  3. Indexed property records of the shape ``[byte_tag, f32, f32]``
     where ``byte_tag`` increments to address a specific sub-field
  4. Animation duration (float32, appearing 1-5× through the file)
  5. Frame count (often ``frame_count - 1`` as uint32)
  6. Embedded keyframe block (same 10-byte records as in the .paa)

We ship a HEURISTIC parser that extracts the reliably-decodable
subset: duration, approximate animated-bone count, and the raw
per-file data bytes for downstream inspection. The full bone-index
remap table remains unreverse-engineered; the caller falls back to
sequential (track index == skeleton bone index) mapping if the
metabin can't be fully consulted.

Usage
-----

    from core.paa_metabin_parser import parse_metabin

    with open("cd_seq_dem_*.paa_metabin", "rb") as f:
        meta = parse_metabin(f.read())

    print(meta.class_name)   # "AnimationMetaData"
    print(meta.duration)     # 24.833
    print(meta.approx_bone_count)  # 54 (best-effort)
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.paa_metabin_parser")


# Fixed-preamble constants (validated against all 149,869 shipping files).
_MAGIC = b"\xff\xff\x04\x00"
_CLASS_NAME = b"AnimationMetaData"
_PREAMBLE_SIZE = 0x50
_CLASS_NAME_OFFSET = 0x18


@dataclass
class ParsedMetabin:
    """Parsed ``.paa_metabin`` sidecar."""
    path: str = ""
    valid: bool = False
    class_name: str = ""

    # Heuristic-extracted fields.
    duration: float = 0.0
    approx_bone_count: int = 0
    approx_frame_count: int = 0
    frequent_floats: list[tuple[float, int]] = field(default_factory=list)

    # Raw per-file data block (everything after the 80-byte preamble).
    variable_block: bytes = b""

    # If we find an embedded keyframe block, this is its byte offset.
    embedded_keyframe_offset: Optional[int] = None

    # Tagged record walk results (from Apr 2026 runtime-DLL findings).
    # Each entry: {"type", "tag", "offset", "raw_bytes"}. Unknown tags
    # are returned as raw bytes for caller inspection.
    tagged_records: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "class_name": self.class_name,
            "duration": self.duration,
            "approx_bone_count": self.approx_bone_count,
            "approx_frame_count": self.approx_frame_count,
            "variable_block_size": len(self.variable_block),
            "embedded_keyframe_offset": self.embedded_keyframe_offset,
            "tagged_record_count": len(self.tagged_records),
            "tagged_record_types": sorted({r["tag"] for r in self.tagged_records}),
        }


def _validate_preamble(data: bytes) -> bool:
    if len(data) < _PREAMBLE_SIZE:
        return False
    if data[:4] != _MAGIC:
        return False
    if data[_CLASS_NAME_OFFSET:_CLASS_NAME_OFFSET + len(_CLASS_NAME)] != _CLASS_NAME:
        return False
    return True


# ---------------------------------------------------------------------------
# Tagged record walker (Apr 2026 — based on runtime DLL injection findings)
# ---------------------------------------------------------------------------
#
# The runtime helper DLL (tools/metabin_re/helper_dll) captured the
# AnimationMetaData class during normal gameplay and showed that its
# internal data is stored as tagged records:
#
#     [record_type = 0x05] [field_tag] [typed_data...]
#
# Cross-referencing with raw metabin bytes confirms the same structure
# lives in the .paa_metabin file starting at offset 0x50:
#
#   0x50: 00 05 05 00 00 08 00 41 44 e4 3f 77 77 2b 41 00
#         ^^---^^ record_type | tag_and_data ...
#
# Field tag values we've enumerated from runtime hits: 0, 1, 2, 3, 4,
# 5, 6, 7, 8, 9, 0xB, 0xE. Each tag has a different payload size.
# The parser below is best-effort: tags we've confirmed have structured
# extractors; unknown tags fall through without claiming to decode them.


def _walk_tagged_records(data: bytes, start: int = 0x50) -> list[dict]:
    """Walk the metabin's tagged-record block.

    Records in the metabin compact form are:

        [0x00] [0x05] [subtype:uint16] [tag:uint8] [data...]

    In the runtime expanded form (captured via DLL injection) they are:

        [pad:3] [0x05] [pad:5] [tag:uint8] [pad:5+] [data]

    This walker targets the compact form (metabin file). It returns a
    list of ``{"type", "tag", "offset", "raw_bytes"}`` dicts without
    claiming to decode unknown tags.
    """
    records: list[dict] = []
    pos = start
    while pos + 5 < len(data):
        # Compact-form marker: [0x00, 0x05]
        if data[pos] != 0x00 or data[pos + 1] != 0x05:
            pos += 1
            continue
        record_start = pos
        # After the marker we have: subtype(2 bytes) + pad(1 byte) +
        # tag(1 byte). Tag is at record_start + 5.
        tag = data[record_start + 5]
        # Sanity check: tag should fit a sensible range (0..0x40).
        # Reject anything wildly out of that to avoid matching random
        # 00 05 patterns inside float data.
        if tag > 0x40:
            pos += 1
            continue
        raw_end = min(record_start + 32, len(data))
        records.append({
            "type": 0x05,
            "tag": tag,
            "offset": record_start,
            "raw_bytes": bytes(data[record_start:raw_end]),
        })
        pos = record_start + 6
    return records


def _scan_float_frequencies(data: bytes, lo: float = 0.01, hi: float = 1000.0) -> dict[float, int]:
    """Return every plausible float32 in the byte range, keyed by
    its 3-decimal-digit rounding, value => occurrence count.
    """
    counts: dict[float, int] = {}
    for off in range(len(data) - 4):
        try:
            v = struct.unpack_from("<f", data, off)[0]
        except struct.error:
            continue
        if not math.isfinite(v):
            continue
        if lo < v < hi:
            key = round(v, 3)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _extract_duration(data: bytes) -> float:
    """Heuristic duration extractor. Scans for float32 values in the
    plausible animation-length range and picks the LARGEST one that
    appears at least twice.

    Rationale: Pearl Abyss stores both the full animation duration
    AND per-event timestamps in the metabin. We can't distinguish
    them without full schema knowledge, but the full animation
    duration is usually the largest value, and it's typically
    referenced by at least two fields (e.g., as start + end of the
    master track).

    This is a HEURISTIC — on files where an event timestamp happens
    to exceed the animation length, or where the duration is only
    mentioned once, it may return the wrong value. Callers should
    treat the returned value as a hint, not ground truth.
    """
    counts = _scan_float_frequencies(data, 0.1, 60.0)
    if not counts:
        return 0.0
    # Prefer values with ≥2 occurrences; fall back to any value.
    repeated = [v for v, c in counts.items() if c >= 2]
    if repeated:
        return max(repeated)
    return max(counts.keys())


def _find_frame_count(data: bytes, duration: float) -> int:
    """Try to recover the frame count as an ``uint32`` value that is
    consistent with ``duration × 30`` (±20% tolerance). Most Crimson
    Desert animations run at 30 fps.
    """
    if duration <= 0:
        return 0
    expected = int(round(duration * 30.0))
    # Search uint32 values near ``expected`` or ``expected - 1``.
    tolerance = max(5, int(expected * 0.2))
    best_match = 0
    for off in range(len(data) - 4):
        v = struct.unpack_from("<I", data, off)[0]
        if abs(v - expected) <= tolerance and v > best_match:
            best_match = v
    return best_match


def _approx_bone_count(data: bytes) -> int:
    """Rough estimate of the animated-bone count based on the
    indexed-pair records we observe in the data block.

    Heuristic: count sequences of the form ``[byte 0..100] [f32] [f32]``
    where the byte serves as a monotonically-progressing index. This
    underestimates in complex cases but is better than nothing until
    the full schema is reversed.
    """
    # This heuristic is approximate — see module docstring for context.
    # Scan 9-byte windows looking for (idx_byte + 2 floats) where
    # idx_byte is in [0..255] and both floats are in [0, 1000].
    hits = 0
    last_idx = -1
    pos = 0
    while pos + 9 <= len(data):
        idx = data[pos]
        if idx < 200 and idx >= max(0, last_idx - 5):
            try:
                f1 = struct.unpack_from("<f", data, pos + 1)[0]
                f2 = struct.unpack_from("<f", data, pos + 5)[0]
            except struct.error:
                pos += 1
                continue
            if math.isfinite(f1) and math.isfinite(f2) and -100 < f1 < 100 and -100 < f2 < 100:
                hits += 1
                last_idx = idx
                pos += 9
                continue
        pos += 1
    # Cap the heuristic result.
    return min(hits, 256)


def _find_embedded_keyframe_block(data: bytes, min_run: int = 8) -> Optional[int]:
    """Scan the variable block for a run of 5+ consecutive 10-byte
    records carrying monotonically-increasing ``uint16 idx`` at offset
    ``+8``. This signature identifies the PAA-style embedded keyframe
    block we see in some metabin files (roofclimb, seq_dem, etc.).
    """
    stride = 10
    idx_off = 8
    for off in range(len(data) - min_run * stride):
        idxs = [struct.unpack_from("<H", data, off + k * stride + idx_off)[0] for k in range(min_run)]
        if (all(idxs[i + 1] > idxs[i] for i in range(min_run - 1))
                and idxs[0] == 0 and idxs[-1] < 2000):
            return off
    return None


def parse_metabin(data: bytes, path: str = "") -> ParsedMetabin:
    """Decode the readable subset of a ``.paa_metabin`` sidecar.

    Returns a ``ParsedMetabin`` whose ``valid`` flag reflects whether
    the 80-byte preamble signature was recognised. Even invalid files
    return a populated object (with best-effort empty values) so the
    caller can differentiate "malformed" from "missing".
    """
    result = ParsedMetabin(path=path)
    if not _validate_preamble(data):
        return result
    result.valid = True
    result.class_name = "AnimationMetaData"
    result.variable_block = bytes(data[_PREAMBLE_SIZE:])

    if not result.variable_block:
        return result

    # Duration is the most reliable heuristically-recoverable field.
    result.duration = _extract_duration(result.variable_block)

    # Frame count from duration × 30 fps consistency check.
    result.approx_frame_count = _find_frame_count(result.variable_block, result.duration)

    # Bone count heuristic.
    result.approx_bone_count = _approx_bone_count(result.variable_block)

    # Locate any embedded keyframe block.
    kf_off = _find_embedded_keyframe_block(result.variable_block)
    if kf_off is not None:
        # Offsets are file-relative (include the 80-byte preamble).
        result.embedded_keyframe_offset = kf_off + _PREAMBLE_SIZE

    # Top 5 most-frequent floats are useful for UI preview.
    freq = _scan_float_frequencies(result.variable_block, 0.0, 1000.0)
    result.frequent_floats = sorted(freq.items(), key=lambda x: -x[1])[:5]

    # Walk the tagged-record block — format identified Apr 2026 via
    # runtime DLL injection (tools/metabin_re/helper_dll). Each record
    # is [0x00, 0x05, tag, ...data...]; tags enumerated so far
    # are 0..0x20 with different payload types per tag.
    result.tagged_records = _walk_tagged_records(data, start=_PREAMBLE_SIZE)

    logger.info(
        "Parsed metabin %s: duration=%.3fs, ~bones=%d, ~frames=%d, keyframe@=%s",
        path, result.duration, result.approx_bone_count,
        result.approx_frame_count,
        f"0x{result.embedded_keyframe_offset:x}" if result.embedded_keyframe_offset else "none",
    )
    return result


def is_metabin(data: bytes) -> bool:
    """Quick structural check for metabin format."""
    return _validate_preamble(data)
