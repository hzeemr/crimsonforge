"""Scan PAC files for facial bone name strings embedded in the data.

Face rigs often live INSIDE the mesh file's skinning section
(ReflectObject references + bone name lookup). Hunt for substrings
like Nose, Brow, Eye, Mouth, Jaw, Facial, etc., inside every PAC.
"""

import sys
import os
from pathlib import Path


FACIAL_NEEDLES = [
    b"Nose", b"Brow", b"Eye", b"Cheek", b"Jaw", b"Chin",
    b"Lip", b"Mouth", b"Forehead", b"Ear",
    b"Face", b"Facial",
    b"Head_Sub", b"Head_Top", b"Head_B",
    b"Tongue", b"Teeth",
    b"Eyebrow", b"Eyelid", b"Eyeball",
    b"FacialBone", b"MorphBone", b"BN_Face",
]


def scan(path):
    data = open(path, "rb").read()
    results = []
    for needle in FACIAL_NEEDLES:
        start = 0
        while True:
            idx = data.find(needle, start)
            if idx < 0:
                break
            # Read a short context window to see the full bone name
            left = max(0, idx - 3)
            right = min(len(data), idx + 40)
            # Look backward for length prefix or string boundary
            chunk = data[left:right]
            # Extract the full ASCII run around the needle
            start_pos = idx
            while start_pos > 0 and 32 <= data[start_pos - 1] < 127:
                start_pos -= 1
            end_pos = idx
            while end_pos < len(data) and 32 <= data[end_pos] < 127:
                end_pos += 1
            full_str = data[start_pos:end_pos].decode("ascii", errors="replace")
            results.append((idx, needle.decode(), full_str))
            start = idx + len(needle)
    return results


def main():
    candidates = sorted(Path(r"C:\Users\hzeem\AppData\Local\Temp").glob(
        "crimsonforge_preview_*/cd_ptm_00_*.pac"
    ))
    for path in candidates[:10]:
        name = os.path.basename(str(path))
        print(f"\n=== {name} ({os.path.getsize(path):,} bytes) ===")
        hits = scan(str(path))
        if not hits:
            print("  NO facial-bone name hits")
            continue
        # Dedupe by full_str
        seen = set()
        for idx, needle, full_str in hits:
            if full_str in seen or len(full_str) < 4:
                continue
            seen.add(full_str)
            print(f"  @0x{idx:06x}  [needle={needle}]  {full_str!r}")


if __name__ == "__main__":
    main()
