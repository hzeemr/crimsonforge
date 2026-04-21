"""Clean PAA parser v2 — based on byte-level reverse engineering.

Discovered structure:

  HEADER (variable size, ends with header section trailer)
    - 0x00: PAR magic
    - 0x10: format flags (uint32 LE)
    - 0x14: tag string length (uint16) for tagged variants
    - 0x16: UTF-8 tag string (Korean metadata)
    - body_start = 0x16 + str_len

  GLOBAL HEADER (after tag block, untagged variants)
    - several bytes of duration/scale floats
    - more separators / padding
    - eventually `3c 00 3c 00 3c` = bone block separator

  PER-BONE BLOCK
    [5 B] separator '3c 00 3c 00 3c'
    [4 B] uint32 LE = some count (165, 11, ...)
    [6 B] 3 fp16 = bind-pose xyz delta (W implicit = sqrt(1 - |xyz|^2))
    [N x 10 B] keyframe records:
        - [2 B] fp16 W component of quaternion
        - [2 B] uint16 LE frame index
        - [6 B] 3 fp16 = xyz components
    [pad] alignment padding to next bone block

The xyz + W form a unit quaternion: |q|^2 = W^2 + x^2 + y^2 + z^2 = 1.
"""

import sys
import struct
from dataclasses import dataclass, field


SEP = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])


def fp16(h):
    sign = (h >> 15) & 1
    exp = (h >> 10) & 0x1F
    mant = h & 0x3FF
    if exp == 0:
        v = (mant / 1024.0) * (2.0 ** -14) if mant else 0.0
    elif exp == 0x1F:
        v = float("inf") if mant == 0 else float("nan")
    else:
        v = (1.0 + mant / 1024.0) * (2.0 ** (exp - 15))
    return -v if sign else v


@dataclass
class BoneTrack:
    block_start: int
    header_count: int
    bind_xyz: tuple
    bind_w: float
    keyframes: list = field(default_factory=list)  # [(frame, qx, qy, qz, qw)]


def parse_bone_block(data, start, end):
    """Parse one bone block from `start` to `end`."""
    if data[start:start + 5] != SEP:
        return None
    body = data[start + 5: end]
    if len(body) < 10:
        return None
    # 4 bytes uint32 + 6 bytes bind xyz
    header_count = struct.unpack_from("<I", body, 0)[0]
    bind_h = struct.unpack_from("<3H", body, 4)
    bind_xyz = (fp16(bind_h[0]), fp16(bind_h[1]), fp16(bind_h[2]))
    bind_w_sq = 1.0 - sum(c * c for c in bind_xyz)
    bind_w = (max(0.0, bind_w_sq)) ** 0.5

    track = BoneTrack(
        block_start=start, header_count=header_count,
        bind_xyz=bind_xyz, bind_w=bind_w,
    )

    # 10-byte records starting at body offset 10
    rec_start = 10
    while rec_start + 10 <= len(body):
        rec = body[rec_start: rec_start + 10]
        w_raw, frame, x_raw, y_raw, z_raw = struct.unpack("<HH3H", rec)
        w = fp16(w_raw)
        x = fp16(x_raw); y = fp16(y_raw); z = fp16(z_raw)
        mag2 = w * w + x * x + y * y + z * z
        # Validate: must be a unit quaternion within tolerance
        if not (0.90 < mag2 < 1.10):
            # Probably hit padding or wrong alignment — stop here
            break
        # Frame index must be reasonable (< 10000)
        if frame > 10000:
            break
        track.keyframes.append((frame, x, y, z, w))
        rec_start += 10

    return track


def parse_paa(path):
    data = open(path, "rb").read()
    if data[:4] != b"PAR ":
        raise ValueError("not a PAR file")

    # Find ALL separators
    seps = []
    i = 0
    while True:
        i = data.find(SEP, i)
        if i < 0:
            break
        seps.append(i)
        i += 1

    # Each "real" bone block is one with a non-trivial size. Header
    # often has 8-byte short blocks too (just '3c 00 3c 00 3c c4 00 00')
    # which we skip.
    bone_blocks = []
    for j, s in enumerate(seps):
        end = seps[j + 1] if j + 1 < len(seps) else len(data)
        size = end - s
        # Skip blocks too small to hold even a header
        if size < 30:
            continue
        track = parse_bone_block(data, s, end)
        if track is not None and len(track.keyframes) > 0:
            bone_blocks.append(track)

    return bone_blocks


def main():
    if len(sys.argv) < 2:
        print("Usage: paa_parse_v2.py <paa>")
        sys.exit(1)
    path = sys.argv[1]
    tracks = parse_paa(path)
    print(f"\nFile: {path}")
    print(f"Bone tracks: {len(tracks)}")
    for i, t in enumerate(tracks[:8]):
        last_frame = t.keyframes[-1][0] if t.keyframes else 0
        print(f"  bone[{i}]: header={t.header_count} keyframes={len(t.keyframes)} "
              f"first_frame={t.keyframes[0][0]} last_frame={last_frame}")
        print(f"    bind_quat = ({t.bind_xyz[0]:+.4f}, {t.bind_xyz[1]:+.4f}, {t.bind_xyz[2]:+.4f}, {t.bind_w:+.4f})")
        for k in t.keyframes[:3]:
            f, x, y, z, w = k
            print(f"    f={f:4d}  q=({x:+.4f}, {y:+.4f}, {z:+.4f}, {w:+.4f})")
        if len(t.keyframes) > 3:
            print(f"    ... ({len(t.keyframes)-3} more keyframes)")

    # Total stats
    total_keys = sum(len(t.keyframes) for t in tracks)
    max_frame = max((t.keyframes[-1][0] for t in tracks if t.keyframes), default=0)
    print(f"\nTotal keyframes: {total_keys}")
    print(f"Max frame index: {max_frame}")


if __name__ == "__main__":
    main()
