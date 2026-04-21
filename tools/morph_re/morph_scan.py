"""Scan character head PAC files for morph target / shape key data.

Morph targets (blend shapes) are usually stored as named arrays of
per-vertex displacement vectors. Pearl Abyss's PAC wrapper has not
been publicly documented for morphs — we hunt for signatures:

  * String tokens: 'morph', 'shape', 'blend', 'NoseHeight', 'EyeOpen',
    'MouthOpen', 'Brow', 'Cheek', 'Chin', 'Jaw' etc.
  * Repeating blocks of 3 or 6 fp16 values (delta xyz / nxyz per vertex)
  * A small index of named offsets near the end of the section-0 region

We don't try to decode full targets yet — we catalogue WHAT files carry
morph data and WHERE the markers sit, which is enough to guide the
editor's next iteration.
"""

import sys
import os
import struct
from pathlib import Path
from collections import Counter


# Plausible morph-target name substrings
NAME_HINTS = [
    "morph", "Morph", "MORPH",
    "shape", "Shape", "blend", "Blend",
    "BlendShape", "ShapeKey",
    "Nose", "Eye", "Mouth", "Brow", "Chin", "Jaw", "Cheek",
    "Lip", "Teeth", "Forehead", "Ear", "Face",
    "HeadSize", "Smile", "Frown", "Wink",
    "Scale", "Width", "Height", "Length",
]


def scan_strings(data, min_len=4, max_len=80):
    """Find printable ASCII runs."""
    out = []
    i = 0
    while i < len(data):
        if 32 <= data[i] < 127:
            j = i
            while j < len(data) and 32 <= data[j] < 127:
                j += 1
            if min_len <= j - i <= max_len:
                try:
                    s = data[i:j].decode("ascii")
                    out.append((i, s))
                except Exception:
                    pass
            i = j
        else:
            i += 1
    return out


def main():
    if len(sys.argv) < 2:
        samples = [
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_kh6ipr3a\cd_ptm_00_head_0001.pac",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_kh6ipr3a\cd_ptm_00_head_0003.pac",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_kh6ipr3a\cd_ptm_00_head_sub_00_0001.pac",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_kh6ipr3a\cd_ptm_00_head_sub_00_0002.pac",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_kh6ipr3a\cd_ppdm_00_eyeleft_00_0001.pac",
        ]
    else:
        samples = sys.argv[1:]

    for path in samples:
        if not os.path.isfile(path):
            continue
        data = open(path, "rb").read()
        name = os.path.basename(path)
        print(f"\n=== {name}  ({len(data):,} bytes) ===")

        strings = scan_strings(data)
        morph_strings = [(off, s) for off, s in strings
                         if any(h in s for h in NAME_HINTS)]
        print(f"Total strings >=4 chars: {len(strings)}")
        print(f"Morph-hint strings:     {len(morph_strings)}")
        for off, s in morph_strings[:20]:
            print(f"  @0x{off:06x}  {s!r}")
        if len(morph_strings) > 20:
            print(f"  ... ({len(morph_strings) - 20} more)")

        # Also look for repeated short vectors (fp16 triples near each
        # other) — a strong morph-delta signature.
        # Scan for runs where every 6 bytes decodes as 3 small fp16.
        suspicious_spans = _find_fp16_triple_runs(data)
        if suspicious_spans:
            print("Possible per-vertex fp16 xyz delta runs:")
            for start, length, sample in suspicious_spans[:5]:
                print(f"  @0x{start:06x} len={length} sample={sample}")


def _find_fp16_triple_runs(data):
    """Look for sustained runs where every 6 bytes decodes to 3 fp16 in
    [-1.0, +1.0] — typical morph displacement magnitude."""
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

    runs = []
    start = -1
    run_len = 0
    i = 0
    last_sample = None
    while i + 6 <= len(data):
        try:
            hs = struct.unpack_from("<3H", data, i)
            vals = tuple(fp16(h) for h in hs)
            if all(v == v and -1.5 < v < 1.5 for v in vals) and any(abs(v) > 1e-6 for v in vals):
                if start < 0:
                    start = i
                    run_len = 0
                run_len += 1
                last_sample = vals
                i += 6
                continue
        except Exception:
            pass
        if start >= 0 and run_len >= 50:
            runs.append((start, run_len, last_sample))
        start = -1
        run_len = 0
        i += 1
    if start >= 0 and run_len >= 50:
        runs.append((start, run_len, last_sample))
    return runs


if __name__ == "__main__":
    main()
