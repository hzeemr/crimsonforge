"""Find the 'Fly' state (Bambozu's complaint) by searching every .pabgb
for all plausible fly-related tokens.
"""

import sys
import os
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.pabgb_parser import parse_pabgb

# Every possible fly-state name
FLY_TERMS = [
    "fly", "flight", "flying", "glide", "gliding", "aerial",
    "sky", "soar", "flap", "wing", "levitate", "float",
    "hover", "airborne", "jetpack", "parachute",
    "falcon", "bird",
]


def main():
    root = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    all_pabgb = sorted(set(str(p) for p in root.glob("crimsonforge_preview_*/*.pabgb")))

    results = []
    for p in all_pabgb:
        try:
            data = open(p, "rb").read()
            header_path = p[:-1] + "h"
            header_data = open(header_path, "rb").read() if os.path.exists(header_path) else None
            table = parse_pabgb(data, header_data, os.path.basename(p))
        except Exception:
            continue

        hits = []
        for row_idx, row in enumerate(table.rows):
            for f in row.fields:
                if f.kind == "str" and isinstance(f.value, str):
                    val_lower = f.value.lower()
                    for term in FLY_TERMS:
                        if term in val_lower:
                            hits.append((row_idx, f.value, term))
                            break
        if hits:
            results.append((os.path.basename(p), hits))

    if not results:
        print("NO 'fly' matches in any .pabgb table.")
        print("The 'Fly' state Bambozu is looking for is likely:")
        print("  - A compile-time constant in the C++ binary")
        print("  - Named differently (MotionState, ChannelType, etc.)")
        print("  - In a non-.pabgb file (.html state config, .prefab page)")
        return

    for fname, hits in results:
        print(f"\n=== {fname} ({len(hits)} hits) ===")
        for row_idx, val, term in hits[:25]:
            print(f"  row[{row_idx}] [{term}]: {val!r}")
        if len(hits) > 25:
            print(f"  ... ({len(hits) - 25} more)")


if __name__ == "__main__":
    main()
