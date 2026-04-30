#!/usr/bin/env python3
"""Search every package group's path index for files matching
Mon_Flame_Knight / FlameKnight / Flame_Knight.

Lists by group with a count and prints first 200 hits per category.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager


PATTERNS = (
    "mon_flame_knight",
    "flame_knight",
    "flameknight",
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--game",
        default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
    )
    ap.add_argument("--out", default=str(Path(__file__).parent / "flame_knight"))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    vfs = VfsManager(args.game)

    hits = {p: [] for p in PATTERNS}
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception as e:
            print("skip group", group, e, file=sys.stderr)
            continue
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            for p in PATTERNS:
                if p in pl:
                    hits[p].append((group, entry.path, entry.orig_size))

    out_path = os.path.join(args.out, "vfs_flame_knight_hits.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        for p in PATTERNS:
            f.write(f"=== pattern={p!r} hits={len(hits[p])} ===\n")
            for group, path, size in hits[p][:300]:
                f.write(f"  {group}  {size:>12}  {path}\n")
            f.write("\n")
    for p in PATTERNS:
        print(f"  {p}: {len(hits[p])} hits")
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
