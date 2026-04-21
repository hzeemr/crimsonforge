"""Scan every .pabgb table in the temp cache for state-machine strings.

Looking for tokens like 'Fly', 'Combat', 'Swim', 'Ride', 'Climb',
'Fight', 'Idle', 'Attack', 'Death', 'Hit', 'Dodge', 'State', etc.
Every hit tells us which table participates in that state.
"""

import sys
import os
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.pabgb_parser import parse_pabgb

STATE_TOKENS = [
    "Fly", "Flight", "Glide",
    "Combat", "Fight", "Battle", "Attack",
    "Swim", "Water", "Dive",
    "Ride", "Mount", "Horse", "Vehicle",
    "Climb", "Roofclimb", "Parkour",
    "Idle", "Walk", "Run", "Sprint", "Dash",
    "Death", "Dead", "Die",
    "Hit", "Stagger", "Knockdown",
    "Dodge", "Roll", "Parry", "Block",
    "Fall", "Jump", "Crouch", "Stealth",
    "Interact", "Gather", "Fish", "Craft",
    "Cutscene", "Scripted",
    "State", "StateMachine",
]


def scan(path):
    try:
        data = open(path, "rb").read()
        # Try to find the matching header
        header_path = path[:-1] + "h"
        header_data = None
        if os.path.exists(header_path):
            header_data = open(header_path, "rb").read()
        table = parse_pabgb(data, header_data, os.path.basename(path))
    except Exception as e:
        return None

    hits = Counter()
    for row in table.rows:
        for f in row.fields:
            if f.kind == "str" and isinstance(f.value, str):
                val = f.value
                for tok in STATE_TOKENS:
                    if tok.lower() in val.lower():
                        hits[tok] += 1
                        break
    return hits, len(table.rows)


def main():
    root = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    all_pabgb = sorted(set(str(p) for p in root.glob("crimsonforge_preview_*/*.pabgb")))
    print(f"Scanning {len(all_pabgb)} .pabgb files for state-machine tokens\n")

    per_file = {}
    for p in all_pabgb:
        result = scan(p)
        if result is None:
            continue
        hits, row_count = result
        if sum(hits.values()) > 0:
            per_file[os.path.basename(p)] = (hits, row_count, p)

    # Sort by total hits
    print(f"{'table':40s} {'rows':>6s} {'hits':>6s} {'top_tokens':50s}")
    print("-" * 110)
    ranked = sorted(
        per_file.items(),
        key=lambda x: -sum(x[1][0].values()),
    )
    for fname, (hits, row_count, path) in ranked[:25]:
        total = sum(hits.values())
        top = ", ".join(f"{t}:{n}" for t, n in hits.most_common(5))
        print(f"{fname:40s} {row_count:>6d} {total:>6d} {top[:50]:50s}")

    # Which tokens are MOST represented overall?
    print("\n=== Tokens across all tables ===")
    all_tokens = Counter()
    for fname, (hits, _, _) in per_file.items():
        all_tokens.update(hits)
    for tok, n in all_tokens.most_common(20):
        print(f"  {tok:25s} {n}")


if __name__ == "__main__":
    main()
