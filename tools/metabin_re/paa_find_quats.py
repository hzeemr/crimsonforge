"""Scan a PAA file for runs of fp16 unit quaternions.

The discovery: 8-byte records = 4 fp16 quaternion components. We
need to find where in the file the long uninterrupted run of unit
quaternions starts. That's the animation track.
"""

import sys
import struct
import os


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


def is_unit_quat_at(data, off, tolerance=0.05):
    if off + 8 > len(data):
        return False, 0.0
    h0, h1, h2, h3 = struct.unpack_from("<4H", data, off)
    q = (fp16(h0), fp16(h1), fp16(h2), fp16(h3))
    # Check finite + bounded
    for c in q:
        if not (-1.5 < c < 1.5):
            return False, 0.0
    mag2 = sum(x * x for x in q)
    if abs(mag2 - 1.0) < tolerance:
        return True, mag2
    return False, mag2


def main():
    if len(sys.argv) < 2:
        print("Usage: paa_find_quats.py <paa>")
        sys.exit(1)
    data = open(sys.argv[1], "rb").read()
    print(f"file: {sys.argv[1]}  size: {len(data)}")

    # For each starting offset, count how many CONSECUTIVE 8-byte
    # records (with that offset alignment) are unit quaternions.
    # The longest consecutive run identifies the animation track.
    best_offset = 0
    best_run = 0

    for start in range(0x14, min(2048, len(data) - 100)):
        run = 0
        off = start
        while off + 8 <= len(data):
            ok, mag2 = is_unit_quat_at(data, off)
            if not ok:
                break
            run += 1
            off += 8
        if run > best_run:
            best_run = run
            best_offset = start

    print(f"\nLongest run of consecutive fp16 unit quaternions:")
    print(f"  starts at offset 0x{best_offset:04x}")
    print(f"  run length: {best_run} records ({best_run * 8} bytes)")

    # Total expected = bones * frames. If we know that, we can verify.
    print(f"\nFile structure inference:")
    print(f"  header: 0x{best_offset:04x} = {best_offset} bytes")
    print(f"  animation: {best_run} records of 8 bytes")
    if best_run > 0:
        # Try to factor into bones * frames using metadata:
        for B, F, label in [(57, 197, "idle_01"), (57, 187, "hello_02"),
                            (76, 77, "roofclimb"), (56, 114, "child_idle")]:
            if best_run == B * F:
                print(f"  matches: {B} bones x {F} frames ({label})")

    # Show first 8 records as quaternions
    print(f"\nFirst 12 records starting at 0x{best_offset:04x}:")
    for i in range(12):
        off = best_offset + i * 8
        if off + 8 > len(data):
            break
        h0, h1, h2, h3 = struct.unpack_from("<4H", data, off)
        q = (fp16(h0), fp16(h1), fp16(h2), fp16(h3))
        mag2 = sum(x * x for x in q)
        print(f"  [{i:3d}] @ 0x{off:04x}  hex={data[off:off+8].hex()}  "
              f"q=({q[0]:+.4f}, {q[1]:+.4f}, {q[2]:+.4f}, {q[3]:+.4f})  |q|^2={mag2:.4f}")


if __name__ == "__main__":
    main()
