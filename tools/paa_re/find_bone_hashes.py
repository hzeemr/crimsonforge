"""Hunt for the PAA bone-mapping hash table.

v1.18.0 decoded each PAA bone block's keyframes (fp16 quats, sparse
frame indices) but couldn't identify WHICH PAB bone each block
corresponded to. The resulting FBX had correct rotation magnitudes
but the wrong bones animating.

Hypothesis: somewhere in the global header (0x3D..first_separator)
there's a hash table of uint32 bone hashes, one per track, matching
the PAB's per-bone hash field (first 4 bytes before each bone name
in the skeleton file).

Approach:
  1. Dump the PAB's bone hashes + names
  2. Dump the PAA's global header bytes
  3. Look for 4-byte runs in the header that match PAB bone hashes
  4. Verify ordering against the bone-block order inside the PAA
"""

import os
import sys
import struct
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab


def extract_pab_bone_hashes(pab_bytes: bytes, filename: str = "") -> list[tuple[int, str]]:
    """Walk the real skeleton via parse_pab() then recover each bone's
    4-byte hash by finding the hash+name adjacency in the raw file.

    The canonical parser already handles the garbage-bone truncation
    (float-validity guard) and padding wart. We use it to get the
    correct bone NAMES, then scan the raw bytes for each
    [uint32 hash][ASCII name][non-printable] triple.
    """
    skel = parse_pab(pab_bytes, filename)
    results: list[tuple[int, str]] = []
    for bone in skel.bones:
        if not bone.name:
            continue
        # Find the name bytes in the file; the 4 bytes directly
        # preceding the ASCII name are the uint32 hash.
        name_bytes = bone.name.encode("ascii", "replace")
        search_from = 0
        while True:
            idx = pab_bytes.find(name_bytes, search_from)
            if idx < 0:
                break
            # The byte AFTER the name must be non-printable (name
            # terminator). This rules out substring hits inside longer
            # bone names.
            after = pab_bytes[idx + len(name_bytes)] if idx + len(name_bytes) < len(pab_bytes) else 0
            if after < 0x20 or after > 0x7E:
                if idx >= 4:
                    bhash = struct.unpack_from("<I", pab_bytes, idx - 4)[0]
                    # Hash should not be 0 or a trivial value
                    if bhash != 0 and bhash != 0xFFFFFFFF:
                        results.append((bhash, bone.name))
                        break
            search_from = idx + 1
    return results


def find_uint32_matches(header: bytes, hash_set: set[int]) -> list[tuple[int, int]]:
    """Find every offset in `header` where a uint32 LE matches a hash."""
    hits = []
    for off in range(0, len(header) - 4):
        val = struct.unpack_from("<I", header, off)[0]
        if val in hash_set:
            hits.append((off, val))
    return hits


def main():
    temp = Path(r"C:\Users\hzeem\AppData\Local\Temp")

    # Pair the phm_01.pab skeleton with its PAA animations
    pab_path = next(temp.glob("crimsonforge_preview_*/phm_01.pab"))
    print(f"PAB: {pab_path}")
    pab_bytes = pab_path.read_bytes()
    bones = extract_pab_bone_hashes(pab_bytes, pab_path.name)
    print(f"  {len(bones)} bones extracted")
    for i, (h, n) in enumerate(bones[:5]):
        print(f"    [{i:2d}] hash=0x{h:08x}  {n!r}")
    hashes = {h for h, _ in bones}

    paa_paths = sorted(temp.glob("crimsonforge_preview_9ffbu3wb/cd_phm_cough_*.paa"))[:2]
    for paa_path in paa_paths:
        data = paa_path.read_bytes()
        print(f"\nPAA: {paa_path.name} ({len(data):,} bytes)")
        # Header region = 0x00 .. first `3c 00 3c 00 3c`
        SEP = bytes([0x3c, 0x00, 0x3c, 0x00, 0x3c])
        first_sep = data.find(SEP)
        if first_sep < 0:
            print("  (no separator — skipping)")
            continue
        header = data[:first_sep]
        print(f"  header size: {first_sep} bytes")

        hits = find_uint32_matches(header, hashes)
        print(f"  PAB-hash matches in header: {len(hits)}")
        for off, val in hits[:20]:
            name = next(n for h, n in bones if h == val)
            print(f"    @0x{off:04x}  0x{val:08x}  {name!r}")

        # Also scan the ENTIRE file for hash matches
        total_hits = find_uint32_matches(data, hashes)
        print(f"  PAB-hash matches anywhere in file: {len(total_hits)}")


if __name__ == "__main__":
    main()
