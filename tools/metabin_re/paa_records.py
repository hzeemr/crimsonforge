"""Walk one bone-block and inventory ALL keyframe records.

Hypothesis: bone block layout =
   [5B separator '3c 00 3c 00 3c']
   [4B uint32 = something — keyframe count?]
   [6B = 3 fp16 = bind-pose xyz delta from identity]
   [10B records: marker fp16 (=W) + uint16 frame + 3 fp16 xyz, repeated]

We dump every 10-byte slot and validate quaternion magnitude.
"""

import sys
import struct


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


SEP = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])


def main():
    if len(sys.argv) < 2:
        print("Usage: paa_records.py <paa> [bone_block_index=1]")
        sys.exit(1)
    data = open(sys.argv[1], "rb").read()
    block_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    # Find separators
    seps = []
    i = 0
    while True:
        i = data.find(SEP, i)
        if i < 0:
            break
        seps.append(i)
        i += 1
    seps.append(len(data))   # sentinel

    if block_idx >= len(seps) - 1:
        print(f"only {len(seps) - 1} blocks available")
        sys.exit(1)

    start = seps[block_idx]
    end = seps[block_idx + 1]
    block = data[start:end]
    print(f"BONE BLOCK {block_idx}:  0x{start:04x} -> 0x{end:04x}  size={len(block)}")
    print(f"first 32 bytes: {block[:32].hex()}")

    # Skip 5-byte separator
    body = block[5:]

    # Try multiple header sizes: 0, 4, 6, 8, 10
    for hdr_size in (0, 4, 6, 8, 10):
        if hdr_size > len(body):
            continue
        rest = body[hdr_size:]
        # Records are 10 bytes
        n_records = len(rest) // 10
        leftover = len(rest) % 10
        if leftover != 0:
            continue

        # Decode all records and check quaternion validity
        valid = 0
        invalid = 0
        frames = []
        first_w = None
        for r in range(n_records):
            rec = rest[r * 10: (r + 1) * 10]
            w_raw, f_idx, x_raw, y_raw, z_raw = struct.unpack("<HH3H", rec[:10])
            w = fp16(w_raw)
            x = fp16(x_raw)
            y = fp16(y_raw)
            z = fp16(z_raw)
            mag2 = w * w + x * x + y * y + z * z
            if 0.95 < mag2 < 1.05:
                valid += 1
            else:
                invalid += 1
            frames.append(f_idx)
            if r == 0:
                first_w = w

        print(f"\nheader={hdr_size:2d}: {n_records:3d} records, "
              f"valid_quat={valid}, invalid={invalid}, "
              f"frames=[{frames[0]}..{frames[-1]}]" if frames else "no records")
        if valid > invalid and n_records > 5:
            # Show first 6 records
            print("  first 6 records:")
            for r in range(min(6, n_records)):
                rec = rest[r * 10: (r + 1) * 10]
                w_raw, f_idx, x_raw, y_raw, z_raw = struct.unpack("<HH3H", rec[:10])
                w = fp16(w_raw)
                x = fp16(x_raw); y = fp16(y_raw); z = fp16(z_raw)
                mag2 = w * w + x * x + y * y + z * z
                print(f"    [{r:3d}] hex={rec.hex()} W={w:+.4f} f={f_idx:4d} "
                      f"xyz=({x:+.4f},{y:+.4f},{z:+.4f}) |q|^2={mag2:.4f}")
            print("  last 4 records:")
            for r in range(max(0, n_records - 4), n_records):
                rec = rest[r * 10: (r + 1) * 10]
                w_raw, f_idx, x_raw, y_raw, z_raw = struct.unpack("<HH3H", rec[:10])
                w = fp16(w_raw)
                x = fp16(x_raw); y = fp16(y_raw); z = fp16(z_raw)
                mag2 = w * w + x * x + y * y + z * z
                print(f"    [{r:3d}] hex={rec.hex()} W={w:+.4f} f={f_idx:4d} "
                      f"xyz=({x:+.4f},{y:+.4f},{z:+.4f}) |q|^2={mag2:.4f}")


if __name__ == "__main__":
    main()
