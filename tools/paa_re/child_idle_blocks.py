"""Find per-bone block boundaries in the child_idle variant.

Records are 10-byte `[uint16 frame][3 fp16 xyz][fp16 w]` entries.
There's no `3c 00 3c 00 3c` separator between bones. Two candidate
delimiters:

  A. frame-index resets to 0 mark new bones
  B. a small non-record header sits between blocks

Walk the file under layout=xyzw_end and detect both cases.
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


def decode_record(data, off):
    """Return (frame, x, y, z, w, mag2) or None if invalid."""
    if off + 10 > len(data):
        return None
    frame, xh, yh, zh, wh = struct.unpack_from("<HHHHH", data, off)
    x = fp16(xh); y = fp16(yh); z = fp16(zh); w = fp16(wh)
    mag2 = x * x + y * y + z * z + w * w
    return (frame, x, y, z, w, mag2)


def main():
    path = Path(
        r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_mq8sf8e4"
        r"\cd_phm_child_00_00_hot_nor_std_idle_01.paa"
    )
    data = path.read_bytes()
    print(f"FILE: {len(data):,} bytes")

    start = 0x008d
    # Walk records in a while loop. When a record is INVALID or its
    # frame jumps suddenly (e.g. huge number or reset to 0), treat
    # that as a block boundary.
    off = start
    block_starts = [start]
    last_frame = -1
    valid_count = 0
    while off + 10 <= len(data):
        rec = decode_record(data, off)
        if rec is None:
            break
        frame, x, y, z, w, mag2 = rec
        if not (0.85 < mag2 < 1.15) or frame > 10000:
            # Invalid record -> block boundary. Try to find the next
            # valid record (might be a small header offsetting us).
            resume = None
            for skip in range(1, 32):
                try_off = off + skip
                if try_off + 10 > len(data):
                    break
                r = decode_record(data, try_off)
                if r is None:
                    continue
                f, _x, _y, _z, _w, m = r
                if 0.85 < m < 1.15 and 0 <= f < 10000:
                    resume = try_off
                    break
            if resume is None:
                break
            block_starts.append(resume)
            last_frame = -1
            off = resume
            continue
        if last_frame >= 0 and frame < last_frame and frame == 0:
            # Frame reset — new bone block
            block_starts.append(off)
            last_frame = 0
        else:
            last_frame = frame
        valid_count += 1
        off += 10

    print(f"Valid records walked: {valid_count}")
    print(f"Detected {len(block_starts)} bone blocks")
    print("First 15 block starts + size + first frame + last frame:")
    for i, s in enumerate(block_starts[:15]):
        # Count records until next block (or end)
        if i + 1 < len(block_starts):
            block_len = (block_starts[i + 1] - s) // 10
        else:
            block_len = (off - s) // 10
        first = decode_record(data, s)
        last_off = s + (block_len - 1) * 10
        last = decode_record(data, last_off) if block_len > 0 else None
        f0 = first[0] if first else "?"
        fN = last[0] if last else "?"
        print(f"  block[{i:3d}] @0x{s:06x}  {block_len:4d} records  "
              f"frames {f0}..{fN}")

    # Total byte budget check
    total_rec_bytes = valid_count * 10
    print(f"\n{valid_count} records × 10 bytes = {total_rec_bytes:,} bytes")
    print(f"File remaining after records: {len(data) - (start + total_rec_bytes)} bytes")


if __name__ == "__main__":
    sys.exit(main())
