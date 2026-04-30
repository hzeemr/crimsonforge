#!/usr/bin/env python3
"""List the 24 characterinfo rows that carry BuffLevel_Difficulty_Boss
(hash 0x000F4355), and check whether the Ogre is one of them."""
from __future__ import annotations
import os, sys, struct
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
OUT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_boss_carriers.txt"

TARGETS = {
    0x000F4354: "BuffLevel_Difficulty",
    0x000F4355: "BuffLevel_Difficulty_Boss",
    0x000F4356: "BuffLevel_Difficulty_PC",
    0x000F4278: "BuffLevel_AIDifficulty",
}

def find_table(vfs, base):
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        b = h = None
        for e in pamt.file_entries:
            p = e.path.lower()
            if p == base + ".pabgb": b = e
            elif p == base + ".pabgh": h = e
        if b and h:
            return g, b, h
    return None, None, None

def main():
    vfs = VfsManager(GAME)
    g, b, h = find_table(vfs, "gamedata/characterinfo")
    data = vfs.read_entry_data(b)
    head = vfs.read_entry_data(h)
    tbl = parse_pabgb(data, head, "characterinfo.pabgb")

    # Search each row for the difficulty hashes; record the field offset
    with open(OUT, "w", encoding="utf-8") as out:
        out.write(f"=== characterinfo rows carrying difficulty buff hashes ===\n")
        out.write(f"total rows: {len(tbl.rows)}\n\n")
        carriers = {h: [] for h in TARGETS}
        for r in tbl.rows:
            for fi, f in enumerate(r.fields):
                if f.kind in ("u32", "hash") and isinstance(f.value, int):
                    if f.value in TARGETS:
                        carriers[f.value].append((r.index, r.name, fi, f.offset))
        for hash_id, name in TARGETS.items():
            lst = carriers[hash_id]
            out.write(f"\n--- {name}  0x{hash_id:08X}  count: {len(lst)}\n")
            ogre_present = any("Ogre" in n for _, n, _, _ in lst)
            out.write(f"   Ogre in list: {ogre_present}\n")
            for idx, n, fi, off in lst[:60]:
                out.write(f"    row {idx:6d}  field[{fi:3d}] off={off:4d}  name='{n}'\n")
            if len(lst) > 60:
                out.write(f"    ... ({len(lst) - 60} more)\n")

    # Now: also check Ogre's row specifically and report its difficulty-related fields
    with open(OUT, "a", encoding="utf-8") as out:
        out.write("\n\n=== Ogre row specifically (any 0x000F4xxx u32 fields) ===\n")
        for r in tbl.rows:
            if r.name == "Boss_Ogre_55515":
                for fi, f in enumerate(r.fields):
                    if f.kind in ("u32", "hash") and isinstance(f.value, int):
                        if 0x000F4000 <= f.value <= 0x000F5FFF:
                            out.write(f"  field[{fi}] off={f.offset} u32=0x{f.value:08X}\n")
                break
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
