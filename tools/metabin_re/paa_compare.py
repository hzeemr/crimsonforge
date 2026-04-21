"""Compare the structure of multiple PAA files byte-by-byte to identify
common format fields.

Approach: dump the first 256 bytes of each PAA, then for each 4-byte
offset from 0x14 onwards, show the value across all files. Fields that
ARE constant across files are likely format markers; fields that vary
are likely per-animation data.
"""

import sys
import struct
import os
from pathlib import Path


def main():
    files = sys.argv[1:]
    if not files:
        print("Usage: paa_compare.py <paa1> <paa2> ...")
        sys.exit(1)

    blobs = []
    names = []
    for f in files:
        if not os.path.isfile(f):
            print(f"SKIP {f}")
            continue
        data = open(f, "rb").read()
        blobs.append(data)
        names.append(os.path.basename(f)[:30])

    if not blobs:
        return

    # Print file header summary
    print("Files:")
    for n, b in zip(names, blobs):
        print(f"  {n:30s} size={len(b)}")

    # Check fixed preamble bytes (0x00-0x14)
    print("\n=== FIXED PREAMBLE (0x00-0x14) — should be identical ===")
    for off in range(0x14):
        col = [f"{b[off]:02x}" if off < len(b) else "--" for b in blobs]
        same = "ok" if all(c == col[0] for c in col) else "DIFF"
        print(f"  0x{off:02x}: {' '.join(col)}  {same}")

    # str_len at 0x14
    print("\n=== TAG STRING LENGTH @ 0x14 (uint16 LE) ===")
    for n, b in zip(names, blobs):
        sl = struct.unpack_from("<H", b, 0x14)[0] if len(b) >= 0x16 else None
        print(f"  {n:30s}  str_len = {sl}")

    # Tag content
    print("\n=== TAG STRING CONTENT ===")
    for n, b in zip(names, blobs):
        sl = struct.unpack_from("<H", b, 0x14)[0]
        if 0x16 + sl <= len(b):
            try:
                tag = b[0x16:0x16 + sl].decode("utf-8", "replace")
                print(f"  {n:30s}  '{tag}'")
            except Exception as e:
                print(f"  {n:30s}  decode error: {e}")

    # Body start offset and first 32 bytes
    print("\n=== BODY START (0x16 + str_len) and first 32 bytes ===")
    for n, b in zip(names, blobs):
        sl = struct.unpack_from("<H", b, 0x14)[0]
        body_start = 0x16 + sl
        print(f"  {n:30s}  body @ 0x{body_start:04x}")
        # Show next 32 bytes
        show = b[body_start:body_start + 32]
        hex_part = " ".join(f"{x:02x}" for x in show)
        print(f"    {hex_part}")

    # First float at body_start (could be padding+float or aligned float)
    print("\n=== INTERPRETATIONS of first 16 bytes after tag ===")
    for n, b in zip(names, blobs):
        sl = struct.unpack_from("<H", b, 0x14)[0]
        bs = 0x16 + sl
        if bs + 16 > len(b):
            continue
        print(f"  {n[:30]:30s}")
        for shift in (0, 1, 2, 3):
            try:
                f1 = struct.unpack_from("<f", b, bs + shift)[0]
                f2 = struct.unpack_from("<f", b, bs + shift + 4)[0]
                u1 = struct.unpack_from("<I", b, bs + shift)[0]
                print(f"    shift+{shift}: float={f1:+12.5g}, next_float={f2:+12.5g}, uint={u1}")
            except struct.error:
                pass


if __name__ == "__main__":
    main()
