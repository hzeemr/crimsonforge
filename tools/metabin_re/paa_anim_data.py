"""Dump the animation data section of a PAA file.

Hypothesis: file structure is
  [0..H]    header (Korean tag, bone names, bind pose, etc.)
  [H..end]  per-frame x per-bone 8-byte records

So animation data starts at: file_size - (frames * bones * 8)

Dump those 8-byte records and try to decode them as:
  * 4 x int16  (xyzw quaternion / 32768)
  * smallest-three (10 bits per component + 2 bit dropped index)
  * 4 x fp16   (xyzw quaternion in half-float)
"""

import sys
import struct
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def fp16_to_f32(h):
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


def decode_int16_quat(rec):
    # 4 int16 / 32768 -> raw quaternion
    qx, qy, qz, qw = struct.unpack("<4h", rec)
    return (qx / 32768.0, qy / 32768.0, qz / 32768.0, qw / 32768.0)


def decode_fp16_quat(rec):
    h0, h1, h2, h3 = struct.unpack("<4H", rec)
    return (fp16_to_f32(h0), fp16_to_f32(h1),
            fp16_to_f32(h2), fp16_to_f32(h3))


def decode_smallest3(rec):
    # First 2 bits = which axis is the largest (and reconstructed from
    # sqrt(1 - other^2)). Remaining 62 bits = 3 components × ~21 bits.
    # We try a 16/16/16 + 16-trail layout where the last 16 contain
    # the index in the top 2 bits and a precision component below.
    val = struct.unpack("<Q", rec)[0]
    drop = val & 0x3
    a = ((val >> 2) & 0x3FFFF) / 0x1FFFF * 2 - 1   # 18-bit signed -1..1
    b = ((val >> 20) & 0x3FFFF) / 0x1FFFF * 2 - 1
    c = ((val >> 38) & 0x3FFFF) / 0x1FFFF * 2 - 1
    sqsum = a * a + b * b + c * c
    largest = math.sqrt(max(0.0, 1.0 - sqsum))
    if drop == 0: return (largest, a, b, c)
    if drop == 1: return (a, largest, b, c)
    if drop == 2: return (a, b, largest, c)
    return (a, b, c, largest)


def main():
    if len(sys.argv) < 4:
        print("Usage: paa_anim_data.py <paa> <bones> <frames>")
        sys.exit(1)
    path = sys.argv[1]
    bones = int(sys.argv[2])
    frames = int(sys.argv[3])
    data = open(path, "rb").read()

    anim_total = bones * frames * 8
    header_size = len(data) - anim_total
    print(f"file size : {len(data)}")
    print(f"bones     : {bones}")
    print(f"frames    : {frames}")
    print(f"anim total: {anim_total}  ({bones} * {frames} * 8)")
    print(f"header    : {header_size}  -> animation starts at offset 0x{header_size:04x}")

    # Show a slice of the anim data
    anim_start = header_size
    print(f"\n=== HEX DUMP of first 64 bytes of animation data ===")
    chunk = data[anim_start:anim_start + 64]
    for i in range(0, len(chunk), 16):
        hex_part = " ".join(f"{b:02x}" for b in chunk[i:i + 16])
        print(f"  {anim_start + i:04x}  {hex_part}")

    # Decode first 6 records as different formats
    print(f"\n=== DECODE ATTEMPTS for first 6 records (8 bytes each) ===")
    for r in range(6):
        rec = data[anim_start + r * 8: anim_start + (r + 1) * 8]
        if len(rec) < 8:
            break
        print(f"  rec{r}: {rec.hex()}")
        i16 = decode_int16_quat(rec)
        fp16 = decode_fp16_quat(rec)
        print(f"    int16/32768  : ({i16[0]:+.4f}, {i16[1]:+.4f}, {i16[2]:+.4f}, {i16[3]:+.4f}) "
              f"|q|^2={sum(x*x for x in i16):.4f}")
        print(f"    fp16         : ({fp16[0]:+.4f}, {fp16[1]:+.4f}, {fp16[2]:+.4f}, {fp16[3]:+.4f}) "
              f"|q|^2={sum(x*x for x in fp16):.4f}")
        try:
            s3 = decode_smallest3(rec)
            print(f"    smallest-3   : ({s3[0]:+.4f}, {s3[1]:+.4f}, {s3[2]:+.4f}, {s3[3]:+.4f}) "
                  f"|q|^2={sum(x*x for x in s3):.4f}")
        except Exception:
            pass

    # Now check: are records frame-major or bone-major?
    # Frame-major: rec[i] corresponds to frame i//bones, bone i%bones
    # Bone-major: rec[i] corresponds to bone i//frames, frame i%frames
    #
    # Heuristic: in frame-major, bone 0 in frame 0 should be similar to
    # bone 0 in frame 1 (smooth animation), so rec[0] vs rec[bones]
    # should be MUCH MORE similar than rec[0] vs rec[1].
    print("\n=== FRAME-MAJOR vs BONE-MAJOR test ===")
    rec0 = decode_int16_quat(data[anim_start:anim_start + 8])
    rec1_frame_major = decode_int16_quat(data[anim_start + bones * 8:anim_start + bones * 8 + 8])
    rec1_bone_major = decode_int16_quat(data[anim_start + 8:anim_start + 16])

    def dist(a, b):
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    print(f"  rec0 (bone0/frame0): {tuple(round(x,4) for x in rec0)}")
    print(f"  if frame-major, rec[bones=N] = next frame, same bone")
    print(f"    -> rec[{bones}]: {tuple(round(x,4) for x in rec1_frame_major)} dist={dist(rec0, rec1_frame_major):.4f}")
    print(f"  if bone-major, rec[1] = same frame, next bone")
    print(f"    -> rec[1]      : {tuple(round(x,4) for x in rec1_bone_major)} dist={dist(rec0, rec1_bone_major):.4f}")
    print(f"  Smaller dist -> that's the right interpretation (smooth animation hypothesis)")


if __name__ == "__main__":
    main()
