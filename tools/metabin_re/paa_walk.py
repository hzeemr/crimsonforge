"""Walk through the PAA file properly:

  * find `3c 00 3c 00 3c` separator
  * after each separator, read a small bone header
  * then read 10-byte keyframe records: [marker:2][frame:2][xyz:6]

The record marker bytes vary — `fa 3b` is one common one, but there
are others. Let's look at all of them and figure out the actual rules.
"""

import sys
import struct
import os
from pathlib import Path


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


SEPARATOR = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])


def main():
    if len(sys.argv) < 2:
        print("Usage: paa_walk.py <paa>")
        sys.exit(1)
    data = open(sys.argv[1], "rb").read()
    print(f"file: {sys.argv[1]}  size: {len(data)}")

    # Find ALL `3c 00 3c 00 3c` separator positions
    seps = []
    i = 0
    while True:
        i = data.find(SEPARATOR, i)
        if i < 0:
            break
        seps.append(i)
        i += 1
    print(f"\nseparator '3c 00 3c 00 3c' found at {len(seps)} positions:")
    for s in seps[:20]:
        print(f"  0x{s:04x}")
    if len(seps) > 20:
        print(f"  ... ({len(seps) - 20} more)")

    # The IDLE file has 197 frames * 57 bones - if separators delimit bones,
    # we'd expect ~57 separators (one per bone). Let's count.
    print(f"\nWith 57 bones, we'd expect ~57 separators if they delimit bones.")
    print(f"Actually found: {len(seps)}")

    # Look at what's between successive separators
    print("\n=== STRUCTURE BETWEEN SEPARATORS (first 4 bones) ===")
    for i in range(min(4, len(seps) - 1)):
        start = seps[i]
        end = seps[i + 1]
        size = end - start
        print(f"\nbone block {i}: 0x{start:04x} -> 0x{end:04x}  size={size} bytes")
        chunk = data[start:start + min(80, size)]
        for off in range(0, len(chunk), 16):
            hex_part = " ".join(f"{b:02x}" for b in chunk[off:off + 16])
            print(f"  {start + off:04x}  {hex_part}")
        # Try to identify keyframe records inside this block.
        # Records begin with a 2-byte marker. Empirically `fa 3b` is one
        # common marker. Check what comes AFTER the separator (5 bytes)
        # plus any small header.
        body_start = start + 5  # skip separator
        # Look for first occurrence of a "record-like" pattern: 2-byte
        # marker followed by 2-byte frame_idx + 6 bytes payload, repeated.
        # Show records starting at increasing offsets.
        for rec_start in (body_start, body_start + 2, body_start + 4,
                          body_start + 6, body_start + 8, body_start + 10):
            if rec_start + 30 > end:
                break
            print(f"  trying records starting at 0x{rec_start:04x}:")
            for r in range(min(3, (end - rec_start) // 10)):
                off = rec_start + r * 10
                rec = data[off:off + 10]
                if len(rec) < 10:
                    break
                marker = struct.unpack("<H", rec[:2])[0]
                frame = struct.unpack("<H", rec[2:4])[0]
                hxyz = struct.unpack("<3H", rec[4:10])
                xyz = (fp16(hxyz[0]), fp16(hxyz[1]), fp16(hxyz[2]))
                w_sq = 1 - sum(c * c for c in xyz)
                w = (max(0, w_sq)) ** 0.5
                print(f"    @0x{off:04x}: marker=0x{marker:04x} frame={frame:4d} "
                      f"xyz=({xyz[0]:+.4f},{xyz[1]:+.4f},{xyz[2]:+.4f}) w={w:.4f}")
            break  # only first viable alignment


if __name__ == "__main__":
    main()
