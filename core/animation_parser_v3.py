"""PAA animation parser v3 — child_idle / SRT-float variant.

Reverse-engineering context (Apr 2026, later than v2)
-----------------------------------------------------

v2 (core.animation_parser_v2) decodes the "tagged 0xC000000F"
variant with ``3c 00 3c 00 3c`` bone-block separators. A minority
of PAAs (sample: ``cd_phm_child_00_00_hot_nor_std_idle_01.paa``)
use the SAME flag value but have no such separator — v2 returns
zero tracks on them. These were tracked as Known Issue #2 in the
v1.18.0 -> v1.21.0 FBX-animation audit.

Deep RE this release (``tools/paa_re/child_idle_*``) found the
actual format:

  Header (same as v2):
    0x00..0x0F  PAR magic + sentinel
    0x10..0x13  flags 0xC000000F
    0x14..0x15  tag_len uint16
    0x16..      UTF-8 tags

  Global header (no separator here — marker byte pattern only):
    ...scale floats + duration float + file size + `6c 14 bb 50`
       marker + padding bytes...

  Per-bone blocks (each concatenated directly after the previous):
    [4 B]  uint32 LE keyframe count
    [6 B]  3 fp16 bind xyz (W implicit = sqrt(1 - |xyz|^2))
    [N x 10 B] keyframe records:
        [2 B]  uint16 LE frame index (DENSE — 0, 1, 2, ... in v3)
        [6 B]  3 fp16 xyz
        [2 B]  fp16 W
    NOTE: W is at the END of the record in v3, but at the START in
    v2. That's the only structural difference — the byte order flip
    is the reason v2's scanner failed (it was reading the xyz as
    a W prefix that happened to not be a unit quaternion).

This parser walks bone blocks in order without separators. When a
block's header (count + bind) decodes to plausible values AND the
following N records all validate as unit quaternions, we accept the
block and advance. Any break in validity is treated as end-of-stream.

No heuristic fallback — if the format doesn't match, we return an
empty track list.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger("core.animation_parser_v3")


PAR_MAGIC = b"PAR "
GLOBAL_HEADER_MARKER = bytes([0x6c, 0x14, 0xbb, 0x50])
# These variant identifiers match the flag low-byte that we have
# seen associated with the SRT-float-no-separator form:
#   0x0F with no 3c00 separators
SRT_VARIANT_FLAGS = (0xC000000F,)


# ── Data structures (mirror v2 shape so callers can share code) ───────

@dataclass
class V3Track:
    """One bone's keyframe stream in the v3 (SRT-float) layout."""
    bind_quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    keyframes: list[tuple[int, float, float, float, float]] = field(
        default_factory=list
    )


@dataclass
class ParsedAnimationV3:
    path: str = ""
    flags: int = 0
    metadata_tags: str = ""
    duration: float = 0.0
    frame_count: int = 0
    tracks: list[V3Track] = field(default_factory=list)


# ── fp16 decode ────────────────────────────────────────────────────────

def _fp16(h: int) -> float:
    sign = (h >> 15) & 1
    exp = (h >> 10) & 0x1F
    mant = h & 0x3FF
    if exp == 0:
        v = (mant / 1024.0) * (2.0 ** -14) if mant else 0.0
    elif exp == 0x1F:
        return float("nan")
    else:
        v = (1.0 + mant / 1024.0) * (2.0 ** (exp - 15))
    return -v if sign else v


# ── Record + block walkers ────────────────────────────────────────────

def _decode_record(data: bytes, off: int) -> tuple[int, float, float, float, float, float] | None:
    """Decode one 10-byte v3 keyframe record.

    Layout: ``[uint16 frame][3 fp16 xyz][fp16 W]`` (10 bytes).
    Returns ``(frame, x, y, z, w, |q|²)`` or ``None`` on overflow.
    """
    if off + 10 > len(data):
        return None
    frame, xh, yh, zh, wh = struct.unpack_from("<HHHHH", data, off)
    x = _fp16(xh); y = _fp16(yh); z = _fp16(zh); w = _fp16(wh)
    mag2 = x * x + y * y + z * z + w * w
    return (frame, x, y, z, w, mag2)


def _is_valid_record(rec: tuple) -> bool:
    if rec is None:
        return False
    frame, x, y, z, w, mag2 = rec
    if frame > 10000:
        return False
    if not (0.85 < mag2 < 1.15):
        return False
    # Reject NaN
    for v in (x, y, z, w):
        if v != v:
            return False
    return True


def _decode_record_8(data: bytes, off: int) -> tuple | None:
    """Decode an 8-byte record: ``[uint16 frame][3 fp16 xyz]`` with W
    reconstructed from ``sqrt(1 - |xyz|^2)``. Used by blocks that
    store only the rotation delta (W near ±1 is implied).
    """
    if off + 8 > len(data):
        return None
    frame, xh, yh, zh = struct.unpack_from("<HHHH", data, off)
    x = _fp16(xh); y = _fp16(yh); z = _fp16(zh)
    w_sq = 1.0 - x * x - y * y - z * z
    if w_sq < -0.15 or w_sq > 1.15:
        return None
    w = (max(0.0, w_sq)) ** 0.5
    mag2 = x * x + y * y + z * z + w * w
    return (frame, x, y, z, w, mag2)


def _try_parse_bone_block(
    data: bytes, off: int,
    *,
    max_frames: int = 8192,
) -> tuple[V3Track, int] | None:
    """Try to parse one bone block starting at ``off``.

    v3 supports two per-bone record strides:
        stride 10 — uint16 frame + 3 fp16 xyz + fp16 W  (explicit W)
        stride  8 — uint16 frame + 3 fp16 xyz           (W implicit)

    Different bones within a single file can use different strides.
    Block 1 in cd_phm_child_00_00_hot_nor_std_idle_01.paa (root
    rotation, often extreme) uses stride 10; block 2 (less extreme
    rotation) uses stride 8.

    We try stride 10 first and fall back to 8. Either way the block
    header is ``[uint16 count]`` (2 bytes).

    Returns ``(track, next_offset)`` on success.
    """
    if off + 2 + 8 > len(data):
        return None

    count = struct.unpack_from("<H", data, off)[0]
    if count == 0 or count > max_frames:
        return None

    rec_start = off + 2

    # Attempt stride 10 first.
    def _walk(stride: int, decode):
        cursor = rec_start
        records = []
        for _ in range(count):
            rec = decode(data, cursor)
            if rec is None:
                return None
            if not (0.85 < rec[5] < 1.15) or rec[0] > 10000:
                return None
            records.append(rec[:5])
            cursor += stride
        return records, cursor

    # Stride 10
    walked = _walk(10, _decode_record)
    if walked is not None:
        records, next_off = walked
        _, bx, by, bz, bw = records[0]
        return V3Track(bind_quat=(bx, by, bz, bw), keyframes=records), next_off

    # Stride 8 fallback (implicit W)
    walked = _walk(8, _decode_record_8)
    if walked is not None:
        records, next_off = walked
        _, bx, by, bz, bw = records[0]
        return V3Track(bind_quat=(bx, by, bz, bw), keyframes=records), next_off

    return None


def _find_first_bone_block(data: bytes, start: int, search_end: int) -> int | None:
    """Scan ``data[start:search_end]`` for the first offset that
    successfully parses as a bone block. Returns the offset or None.

    The upper bound is ``search_end`` clamped to a position that
    still leaves room for a minimal block (2-byte count + 8-byte
    record). On tiny files this reduces to roughly len(data) - 10
    instead of the previous - 20, which let small synthetic fixtures
    slip through the scan window.
    """
    upper = min(search_end, max(start + 1, len(data) - 10))
    for off in range(start, upper):
        got = _try_parse_bone_block(data, off)
        if got is not None:
            return off
    return None


# ── Public entry point ────────────────────────────────────────────────

def parse_paa_v3(data: bytes, filename: str = "") -> ParsedAnimationV3:
    """Parse the SRT-float / child_idle variant.

    Raises ``ValueError`` only for wrong-magic files. Returns an
    empty animation for non-v3 variants rather than trying to
    recover — callers should only reach this path after v2 returns
    zero tracks.
    """
    if len(data) < 0x14 or data[:4] != PAR_MAGIC:
        raise ValueError(f"not a PAR file: magic={data[:4]!r}")

    result = ParsedAnimationV3(path=filename)
    flags = struct.unpack_from("<I", data, 0x10)[0]
    result.flags = flags

    if flags not in SRT_VARIANT_FLAGS:
        return result   # not our variant — empty tracks

    # Tag string
    str_len = struct.unpack_from("<H", data, 0x14)[0]
    if 0x16 + str_len <= len(data):
        try:
            result.metadata_tags = data[0x16:0x16 + str_len].decode(
                "utf-8", "replace"
            )
        except Exception:
            pass
    after_tags = 0x16 + str_len

    # Find the `6c 14 bb 50` marker to bound the global header.
    marker_off = data.find(GLOBAL_HEADER_MARKER, after_tags)
    if marker_off < 0:
        return result
    # Records start AFTER the marker + small padding. Try offsets in a
    # small window for the first bone-block header.
    first_block = _find_first_bone_block(
        data, marker_off + 4, marker_off + 64,
    )
    if first_block is None:
        return result

    # Walk the stream of bone blocks until we can't decode any more.
    # Minimum block size = 2-byte count + 8-byte record (stride 8) = 10.
    cursor = first_block
    while cursor + 10 <= len(data):
        got = _try_parse_bone_block(data, cursor)
        if got is None:
            # Try to recover by skipping up to 16 bytes (inter-block
            # padding / alignment). If nothing validates, stop.
            recovered = None
            for skip in range(1, 16):
                attempt = _try_parse_bone_block(data, cursor + skip)
                if attempt is not None:
                    recovered = (attempt, cursor + skip)
                    break
            if recovered is None:
                break
            (track, next_off), _start = recovered
            cursor = next_off
            result.tracks.append(track)
            continue

        track, next_off = got
        result.tracks.append(track)
        cursor = next_off

    # Frame count = max frame across tracks + 1
    max_frame = 0
    for t in result.tracks:
        for kf in t.keyframes:
            if kf[0] > max_frame:
                max_frame = kf[0]
    result.frame_count = max_frame + 1 if max_frame > 0 else 0
    if result.frame_count > 0:
        result.duration = result.frame_count / 30.0   # assumed 30 fps

    logger.info(
        "Parsed PAA v3 %s: tracks=%d frames=%d",
        filename, len(result.tracks), result.frame_count,
    )
    return result
