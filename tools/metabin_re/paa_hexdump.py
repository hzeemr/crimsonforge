"""Pure-byte exploration of a PAA file. NO assumptions, NO fallbacks.

Run via:
    python paa_hexdump.py <paa>

Prints:
  * a hex dump of the first 256 bytes
  * any 4-byte little-endian unsigned ints in [1, 1000] (candidate bone
    counts / frame counts / sizes)
  * any 4-byte little-endian floats in plausible ranges (rotation,
    position, scale)
  * any ASCII or UTF-8 substrings
  * a guess at the bind-pose start by looking for a unit quaternion
    (4 floats summing roughly to 1.0 in squared magnitude)
"""

import sys
import struct


def main():
    if len(sys.argv) < 2:
        print("Usage: paa_hexdump.py <paa>")
        sys.exit(1)
    data = open(sys.argv[1], "rb").read()
    print(f"FILE: {sys.argv[1]}  size: {len(data)} bytes\n")

    # Pretty hex dump of first 512 bytes
    print("=== HEX DUMP (first 512 bytes) ===")
    for off in range(0, min(512, len(data)), 16):
        chunk = data[off:off + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {off:04x}  {hex_part:<48s}  {asc_part}")

    # ASCII / UTF-8 strings
    print("\n=== ASCII STRINGS (>=4 chars) ===")
    cur = b""
    cur_off = 0
    for i, b in enumerate(data):
        if 32 <= b < 127:
            if not cur:
                cur_off = i
            cur += bytes([b])
        else:
            if len(cur) >= 4:
                print(f"  @0x{cur_off:04x}: {cur.decode('ascii', 'replace')!r}")
            cur = b""
    if len(cur) >= 4:
        print(f"  @0x{cur_off:04x}: {cur.decode('ascii', 'replace')!r}")

    # UTF-8 multi-byte (Korean tags etc)
    print("\n=== UTF-8 STRINGS ===")
    # Simple heuristic: look for 0xE0-0xEF lead bytes (3-byte UTF-8 = Korean)
    for i, b in enumerate(data):
        if 0xE0 <= b <= 0xEF and i + 9 < len(data):
            try:
                s = data[i:i + 30].decode("utf-8", "strict")
                if any(0x1100 <= ord(c) <= 0xFFFF for c in s[:5]):
                    print(f"  @0x{i:04x}: {s!r}")
                    break
            except UnicodeDecodeError:
                continue

    # Candidate uint32 values
    print("\n=== uint32 LE values in plausible ranges (range, not all) ===")
    print("  [1..200] = bone/frame counts; [200..10000] = vertex counts; etc.")
    seen = set()
    for off in range(0, min(64, len(data) - 4), 4):
        v = struct.unpack_from("<I", data, off)[0]
        if 1 <= v <= 200 and v not in seen:
            print(f"  @0x{off:04x}: {v}")
            seen.add(v)
        elif 200 < v <= 10000:
            print(f"  @0x{off:04x}: {v}  (mid range)")

    # Candidate float values
    print("\n=== first 64 float32 values @4-byte aligned offsets ===")
    for off in range(0, min(256, len(data) - 4), 4):
        f = struct.unpack_from("<f", data, off)[0]
        flag = ""
        if -10.0 <= f <= 10.0 and f != 0.0:
            flag = "  <-- plausible scale/rotation/position"
        elif abs(f) < 1e-30 and f != 0.0:
            flag = "  (denormal — probably not a float)"
        print(f"  @0x{off:04x}: {f!r:25s}{flag}")

    # Look for unit quaternions in the first 2KB
    print("\n=== unit-quaternion candidates (4 floats with mag^2 ~= 1.0) ===")
    found = 0
    for off in range(0x14, min(2048, len(data) - 16), 1):
        try:
            qx, qy, qz, qw = struct.unpack_from("<4f", data, off)
            if all(-1.5 < x < 1.5 for x in (qx, qy, qz, qw)):
                mag2 = qx*qx + qy*qy + qz*qz + qw*qw
                if 0.95 < mag2 < 1.05:
                    print(f"  @0x{off:04x}: ({qx:+.4f}, {qy:+.4f}, {qz:+.4f}, {qw:+.4f}) mag2={mag2:.4f}")
                    found += 1
                    if found > 30:
                        print("  ... (more)")
                        break
        except struct.error:
            break


if __name__ == "__main__":
    main()
