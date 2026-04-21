"""Reverse engineer the child_idle (0xC000000F-SRT-float-no-separator) PAA variant.

v1.18.0 discovered this variant decodes to zero tracks through the
v2 parser because there's no ``3c 00 3c 00 3c`` separator between
bone blocks. Deep inspection of cd_phm_child_00_00_hot_nor_std_idle_01.paa
this release revealed:

  * 10-byte records per keyframe, same stride as the tagged variant
  * BUT the XYZW component order is DIFFERENT from the tagged variant:
        tagged variant:    [fp16 W][uint16 frame][3 fp16 xyz]
        child_idle:        [uint16 frame][3 fp16 xyz][fp16 W]
  * The byte `0x3b` that APPEARS to act as a separator is actually
    the UPPER byte of W when W is near 1.0 (fp16 values close to 1
    always have 0x3B or 0x3C as the high byte — that's why it
    misleads a naive scanner)
  * Bone-block delimiter might be the `6c 14 bb 50` marker

This script validates the layout by walking records starting at the
first frame-number-like uint16 and confirming every 10-byte record
decodes as a valid unit quaternion (|q|² within [0.9, 1.1]).
"""

import sys
import struct
from pathlib import Path


def fp16(h):
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


def try_decode_records(data, start, count, layout="xyzw_end"):
    """Try to walk `count` 10-byte records starting at `start`.

    Returns (records, validity_score). validity_score is the fraction
    of records that decode as unit quaternions (|q|² within [0.9, 1.1]).
    """
    records = []
    valid = 0
    for i in range(count):
        off = start + i * 10
        if off + 10 > len(data):
            break
        if layout == "xyzw_end":
            frame, xh, yh, zh, wh = struct.unpack_from("<HHHHH", data, off)
            x = fp16(xh); y = fp16(yh); z = fp16(zh); w = fp16(wh)
        elif layout == "wxyz_start":
            wh, frame, xh, yh, zh = struct.unpack_from("<HHHHH", data, off)
            x = fp16(xh); y = fp16(yh); z = fp16(zh); w = fp16(wh)
        else:
            raise ValueError("unknown layout")
        mag2 = x * x + y * y + z * z + w * w
        records.append((frame, x, y, z, w, mag2))
        if 0.85 < mag2 < 1.15 and -10000 < frame < 10000:
            valid += 1
    score = valid / max(len(records), 1)
    return records, score


def main():
    path = Path(
        r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_mq8sf8e4\cd_phm_child_00_00_hot_nor_std_idle_01.paa"
    )
    data = path.read_bytes()
    print(f"FILE: {path.name}  {len(data):,} bytes\n")

    # Find the earliest block of consecutive 10-byte records that
    # validate as unit quaternions under BOTH layout hypotheses.
    print("=== Searching for best record-block offset ===")
    best = None
    for start in range(0x20, 0x200):
        for layout in ("xyzw_end", "wxyz_start"):
            recs, score = try_decode_records(data, start, 50, layout)
            if score > 0.7 and len(recs) == 50:
                if best is None or score > best[2]:
                    best = (start, layout, score)

    if best is None:
        print("  NO VIABLE START FOUND (first 50 records under either layout)")
        return 1

    start, layout, score = best
    print(f"  BEST: start=0x{start:04x}  layout={layout}  score={score:.2%}")

    # Dump the first 20 records so we can eyeball them
    records, _ = try_decode_records(data, start, 20, layout)
    print(f"\n=== First 20 records at 0x{start:04x} ({layout}) ===")
    for i, (frame, x, y, z, w, mag2) in enumerate(records):
        ok = "OK" if 0.85 < mag2 < 1.15 and -10000 < frame < 10000 else "??"
        print(f"  [{i:3d}] {ok}  frame={frame:5d}  "
              f"xyz=({x:+.4f}, {y:+.4f}, {z:+.4f})  w={w:+.4f}  "
              f"|q|²={mag2:.4f}")

    # Now look for the bone-block separator
    print(f"\n=== Looking for bone-block separator pattern ===")
    # The '6c 14 bb 50' marker appears in the global header; check if
    # it ALSO separates bone blocks
    marker = bytes([0x6c, 0x14, 0xbb, 0x50])
    positions = []
    i = 0
    while True:
        j = data.find(marker, i)
        if j < 0:
            break
        positions.append(j)
        i = j + 1
    print(f"  '6c 14 bb 50' appears {len(positions)} times")
    for p in positions[:10]:
        print(f"    @0x{p:06x}")
    if len(positions) > 10:
        print(f"    ... and {len(positions) - 10} more")


if __name__ == "__main__":
    sys.exit(main())
