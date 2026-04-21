"""Complete byte-level debug of a PAA file.

Dumps EVERY field with its offset and interpretation. Finds the
bone-name/hash table that identifies which track goes to which bone.
"""

import sys
import struct
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


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


def hexdump_range(data, start, end, label):
    print(f"\n--- {label} (0x{start:04x}..0x{end:04x}) ---")
    for off in range(start, min(end, len(data)), 16):
        chunk = data[off:off + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {off:04x}  {hex_part:<48s}  {asc}")


def get_pab_hashes():
    """Load PAB bone names and compute various hashes to cross-check."""
    from core.skeleton_parser import parse_pab
    skel_path = Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_v26_gvp9\phm_01.pab")
    data = skel_path.read_bytes()
    skel = parse_pab(data, skel_path.name)

    # PAB embeds bone_hash field too — let's extract those raw
    hashes = []
    off = 0x17  # after preamble
    # Walk bones to find their hashes
    for i, bone in enumerate(skel.bones):
        hashes.append((bone.name, bone.rotation, bone.position))
    return skel, hashes


def main():
    if len(sys.argv) < 2:
        path = r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_9ffbu3wb\cd_phm_cough_00_00_nor_std_idle_01.paa"
    else:
        path = sys.argv[1]
    data = open(path, "rb").read()
    print(f"FILE: {path}")
    print(f"SIZE: {len(data)} bytes\n")

    # ============================================================
    # PART 1: FIXED HEADER (0x00-0x13)
    # ============================================================
    print("=" * 70)
    print("PART 1: FIXED HEADER (0x00-0x13)")
    print("=" * 70)
    hexdump_range(data, 0x00, 0x14, "Fixed preamble")
    magic = data[:4]
    version = struct.unpack("<I", data[4:8])[0]
    sentinel = struct.unpack("<Q", data[8:0x10])[0]
    flags = struct.unpack("<I", data[0x10:0x14])[0]
    print(f"  magic    : {magic!r}")
    print(f"  version  : 0x{version:08x}")
    print(f"  sentinel : 0x{sentinel:016x}")
    print(f"  flags    : 0x{flags:08x}")
    print(f"    high byte 0x{(flags >> 24) & 0xFF:02x} = {'tagged' if (flags >> 24) & 0xFF == 0xC0 else 'untagged'}")

    # ============================================================
    # PART 2: TAG STRING
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 2: TAG STRING (0x14..)")
    print("=" * 70)
    tag_len = struct.unpack("<H", data[0x14:0x16])[0]
    print(f"  tag_len (uint16 LE @ 0x14): {tag_len}")
    tag_bytes = data[0x16:0x16 + tag_len]
    try:
        tag = tag_bytes.decode("utf-8").rstrip("\x00")
        print(f"  tag: {tag!r}")
    except Exception:
        print(f"  tag (raw): {tag_bytes.hex()}")
    body_start = 0x16 + tag_len
    print(f"  body starts @ 0x{body_start:04x}")

    # ============================================================
    # PART 3: GLOBAL HEADER (between tag and first '3c 00 3c 00 3c')
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 3: GLOBAL HEADER (between tag and first separator)")
    print("=" * 70)
    SEP = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])
    first_sep = data.find(SEP, body_start)
    print(f"  first '3c 00 3c 00 3c' @ 0x{first_sep:04x}")
    print(f"  global header size: {first_sep - body_start} bytes")

    hexdump_range(data, body_start, first_sep, "Global header bytes")

    # Decode as sequential fields
    print("\n  Sequential decode attempts:")
    off = body_start
    # Try float, float, ...
    print(f"    floats:")
    for i in range(min(8, (first_sep - body_start) // 4)):
        f = struct.unpack_from("<f", data, off + i * 4)[0]
        u = struct.unpack_from("<I", data, off + i * 4)[0]
        print(f"      @0x{off + i*4:04x}: float={f:+.5f} uint32={u}")

    # Find marker patterns
    marker = bytes([0x6c, 0x14, 0xbb, 0x50])
    m = data.find(marker, body_start)
    if m >= 0 and m < first_sep:
        print(f"\n  Found marker '6c 14 bb 50' @ 0x{m:04x}")
        # What's after it?
        after = data[m + 4: first_sep]
        print(f"  bytes after marker (until first separator): {after.hex()}")

    # ============================================================
    # PART 4: BONE BLOCK INDEX TABLE (maybe?)
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 4: SEARCHING FOR BONE INDEX TABLE")
    print("=" * 70)
    # Find ALL separators
    seps = []
    i = 0
    while True:
        i = data.find(SEP, i)
        if i < 0: break
        seps.append(i); i += 1
    print(f"  Total separators: {len(seps)}")

    # Look at the first 15 bytes after each separator
    print(f"\n  First 15 bytes after each of the first 20 separators:")
    for s_i, s in enumerate(seps[:20]):
        chunk = data[s + 5: s + 20]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        # Decode uint32 and fp16 values
        if s + 9 <= len(data):
            u = struct.unpack_from("<I", data, s + 5)[0]
        else:
            u = "-"
        print(f"    sep[{s_i:2d}] @0x{s:04x}: {hex_part}  uint32={u}")

    # ============================================================
    # PART 5: Cross-reference with PAB bones
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 5: PAB BONE TABLE (for cross-reference)")
    print("=" * 70)
    try:
        skel, hashes = get_pab_hashes()
        print(f"  PAB has {len(skel.bones)} bones:")
        for i, b in enumerate(skel.bones[:15]):
            print(f"    [{i:2d}] {b.name:30s}  bind_rot=({b.rotation[0]:+.3f},{b.rotation[1]:+.3f},{b.rotation[2]:+.3f},{b.rotation[3]:+.3f})")
    except Exception as e:
        print(f"  PAB load failed: {e}")

    # ============================================================
    # PART 6: LIST BONE BLOCK SIZES (find big ones — those are real bones)
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 6: BONE BLOCK SIZES (to identify real bones vs metadata)")
    print("=" * 70)
    seps_ext = seps + [len(data)]
    block_sizes = [(j, seps[j], seps_ext[j + 1] - seps[j]) for j in range(len(seps))]
    block_sizes.sort(key=lambda x: -x[2])  # biggest first
    print(f"  Top 15 biggest blocks (likely real bone tracks):")
    for j, s, size in block_sizes[:15]:
        # First 6 bytes of bind pose after 4-byte header
        if s + 15 < len(data):
            hdr_count = struct.unpack_from("<I", data, s + 5)[0]
            bind_h = struct.unpack_from("<3H", data, s + 9)
            bind = (fp16(bind_h[0]), fp16(bind_h[1]), fp16(bind_h[2]))
            bind_w_sq = 1.0 - sum(c * c for c in bind)
            bind_w = (max(0.0, bind_w_sq)) ** 0.5
            print(f"    block[{j:3d}] @0x{s:04x} size={size:5d}  count={hdr_count:4d}  "
                  f"bind=({bind[0]:+.3f},{bind[1]:+.3f},{bind[2]:+.3f},{bind_w:+.3f})")


if __name__ == "__main__":
    main()
