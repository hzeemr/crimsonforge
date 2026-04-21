"""Second attempt at the PAA bone-mapping. PAB hashes don't appear
directly in PAA bytes; try alternatives:

  Hypothesis A — Name strings embedded.
    Grep for 'Bip01' or other PAB bone names as ASCII in the PAA.

  Hypothesis B — Simple name hash (crc32 / FNV / custom).
    Compute those hashes of PAB bone names and scan PAA bytes.

  Hypothesis C — No per-bone identity at all; tracks are in PAB
    ORDER. Check whether the PAA bone-block COUNT matches the
    number of bones that actually animate.

  Hypothesis D — Indexed mapping table sits adjacent to the
    separator markers themselves (not in the global header).
"""

import os
import sys
import struct
import zlib
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab
from core.animation_parser_v2 import parse_paa_v2


def _h32(s: str) -> int:
    """One-at-a-time Jenkins hash (common in PA engines)."""
    h = 0
    for c in s:
        h += ord(c)
        h &= 0xFFFFFFFF
        h += (h << 10) & 0xFFFFFFFF
        h &= 0xFFFFFFFF
        h ^= (h >> 6)
    h += (h << 3) & 0xFFFFFFFF
    h &= 0xFFFFFFFF
    h ^= (h >> 11)
    h += (h << 15) & 0xFFFFFFFF
    h &= 0xFFFFFFFF
    return h


def _fnv32(s: str) -> int:
    h = 0x811c9dc5
    for c in s:
        h ^= ord(c)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _djb2(s: str) -> int:
    h = 5381
    for c in s:
        h = (((h << 5) + h) + ord(c)) & 0xFFFFFFFF
    return h


def _sdbm(s: str) -> int:
    h = 0
    for c in s:
        h = (ord(c) + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
    return h


def scan_for_hash(paa: bytes, hash_values: set[int]) -> int:
    """Return count of matches."""
    n = 0
    for off in range(0, len(paa) - 4):
        v = struct.unpack_from("<I", paa, off)[0]
        if v in hash_values:
            n += 1
    return n


def main():
    temp = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    pab_path = next(temp.glob("crimsonforge_preview_*/phm_01.pab"))
    pab_bytes = pab_path.read_bytes()
    skel = parse_pab(pab_bytes, pab_path.name)
    bone_names = [b.name for b in skel.bones]
    print(f"PAB: {len(bone_names)} bones. First 5: {bone_names[:5]}")

    paa_paths = sorted(temp.glob("crimsonforge_preview_9ffbu3wb/cd_phm_cough_*.paa"))[:2]
    for paa_path in paa_paths:
        data = paa_path.read_bytes()
        print(f"\nPAA: {paa_path.name} ({len(data):,} bytes)")

        # Hypothesis A — any bone name as ASCII in PAA?
        name_hits = 0
        for name in bone_names:
            if name.encode("ascii") in data:
                name_hits += 1
        print(f"  Hyp A — ASCII bone names present: {name_hits} / {len(bone_names)}")

        # Hypothesis B — trial various string hashes
        for algo, fn in (
            ("jenkins_OAT", _h32),
            ("fnv32", _fnv32),
            ("djb2", _djb2),
            ("sdbm", _sdbm),
            ("crc32", lambda s: zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF),
        ):
            hashes = {fn(n) for n in bone_names}
            matches = scan_for_hash(data, hashes)
            print(f"  Hyp B — {algo:14s}: {matches} matches in PAA")

        # Hypothesis C — per-bone-block count match
        v2 = parse_paa_v2(data, paa_path.name)
        print(f"  Hyp C — v2 parser: {len(v2.tracks)} tracks vs {len(bone_names)} skeleton bones")

        # Hypothesis D — dump the bytes of the global header (0..first_sep)
        SEP = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])
        first_sep = data.find(SEP)
        hdr = data[:first_sep]
        print(f"  Global header ({first_sep} bytes):")
        for i in range(0, len(hdr), 16):
            chunk = hdr[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"    {i:04x}  {hex_part:<48s}  {asc}")


if __name__ == "__main__":
    main()
