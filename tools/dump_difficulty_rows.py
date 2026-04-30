#!/usr/bin/env python3
"""Dump exactly the difficulty-related rows we need:

  buffinfo.pabgb  rows 1, 2, 3, 44 (Difficulty, Difficulty_Boss, Difficulty_PC, AIDifficulty)
  statusinfo.pabgb  row 41 (Difficulty)
  skill.pabgb  row 246 (Active_Difficulty_Intro)
  conditioninfo.pabgb  rows 286, 287 (GetDifficultyOption)

We need to see every field with offset, kind, and value so we can spot the
multiplier slots.
"""
from __future__ import annotations
import os, sys, struct
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
OUT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_focused_rows.txt"

TARGETS = {
    "gamedata/buffinfo": [1, 2, 3, 44],
    "gamedata/statusinfo": [41],
    "gamedata/skill": [246],
    "gamedata/conditioninfo": [286, 287],
}

def find_entry(vfs, base):
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        b = h = None
        for e in pamt.file_entries:
            p = e.path.lower()
            if p == base + ".pabgb":
                b = e
            elif p == base + ".pabgh":
                h = e
        if b and h:
            return g, b, h
    return None, None, None

def main():
    vfs = VfsManager(GAME)
    with open(OUT, "w", encoding="utf-8") as out:
        for base, rows in TARGETS.items():
            g, b, h = find_entry(vfs, base)
            if not b:
                out.write(f"!! not found: {base}\n")
                continue
            data = vfs.read_entry_data(b)
            head = vfs.read_entry_data(h)
            tbl = parse_pabgb(data, head, os.path.basename(base))
            out.write(f"\n{'=' * 78}\n")
            out.write(f"{base}.pabgb  group={g}  rows={len(tbl.rows)}  field_count={tbl.field_count}\n")
            out.write(f"{'=' * 78}\n")
            for ri in rows:
                if ri >= len(tbl.rows):
                    out.write(f"  !! row {ri} out of range\n")
                    continue
                r = tbl.rows[ri]
                out.write(f"\nROW {r.index}  name='{r.name}'  hash=0x{r.row_hash:08X}  size={r.data_size}b  fields={len(r.fields)}\n")
                for fi, f in enumerate(r.fields):
                    out.write(f"    [{fi:03d}] off={f.offset:4d} {f.kind:5} = {f.display_value()}\n")
                out.write(f"  raw bytes (first 256):\n")
                rh = r.raw[:256].hex()
                # break into 32-byte rows
                for i in range(0, len(rh), 64):
                    out.write(f"    {rh[i:i+64]}\n")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
