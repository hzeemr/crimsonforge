#!/usr/bin/env python3
"""Cross-reference: which characterinfo rows reference 'flameknight'
or 'flame_knight' inline as ASCII data?

Also which rows fall in the same name-space (Boss_Flame*, Mon_Flame*,
Boss_Knight*, etc.) so we can pick a stats baseline.
"""
from __future__ import annotations
import argparse, os, sys, re
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb


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

    # Locate characterinfo
    table = group = None
    for grp in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(grp)
        except Exception:
            continue
        pabgb = pabgh = None
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl == "gamedata/characterinfo.pabgb":
                pabgb = entry
            elif pl == "gamedata/characterinfo.pabgh":
                pabgh = entry
        if not (pabgb and pabgh):
            continue
        try:
            d = vfs.read_entry_data(pabgb)
            hd = vfs.read_entry_data(pabgh)
            table = parse_pabgb(d, hd, "characterinfo.pabgb")
            group = grp
            break
        except Exception:
            continue
    if not table:
        print("characterinfo not found", file=sys.stderr)
        return 2

    print(f"characterinfo: group={group} rows={len(table.rows)}")

    # 1. Rows whose raw bytes contain 'flameknight' or 'flame_knight'
    needles = (b"flameknight", b"flame_knight", b"FlameKnight", b"Flame_Knight",
               b"FlameKnights", b"Flame_Knights")
    raw_hits = []
    for r in table.rows:
        # Reconstruct raw bytes from fields
        raw = b"".join(getattr(f, "raw", b"") for f in r.fields)
        for n in needles:
            if n in raw:
                raw_hits.append((r.index, r.name, n.decode(errors="replace"),
                                 r.data_size, len(r.fields)))
                break

    print(f"\nrows whose bytes contain a flame/knight string: {len(raw_hits)}")
    for h in raw_hits[:40]:
        print(f"  idx={h[0]:>5}  size={h[3]:>5}  fields={h[4]:>4}  match={h[2]!r:<20}  {h[1]}")

    # 2. Rows whose name matches relevant stems
    name_hits = []
    for r in table.rows:
        n = (r.name or "").lower()
        if any(s in n for s in ("flame", "knight", "boss_flame", "boss_knight",
                                 "mon_flame", "mon_knight", "swds")):
            name_hits.append((r.index, r.name, r.data_size, len(r.fields)))
    print(f"\nrows with flame/knight/swds in name: {len(name_hits)}")
    for h in name_hits[:50]:
        print(f"  idx={h[0]:>5}  size={h[2]:>5}  fields={h[3]:>4}  {h[1]}")

    out_path = os.path.join(args.out, "characterinfo_xref.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# raw byte hits for flame/knight (n={len(raw_hits)})\n")
        for h in raw_hits:
            f.write(f"  idx={h[0]:>5}  size={h[3]:>5}  fields={h[4]:>4}  match={h[2]!r:<20}  {h[1]}\n")
        f.write(f"\n# name hits (n={len(name_hits)})\n")
        for h in name_hits:
            f.write(f"  idx={h[0]:>5}  size={h[2]:>5}  fields={h[3]:>4}  {h[1]}\n")
    print("\nwrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
