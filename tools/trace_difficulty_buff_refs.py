#!/usr/bin/env python3
"""Find every pabgb row that references the difficulty buff hashes:
   0x000F4354 = BuffLevel_Difficulty
   0x000F4355 = BuffLevel_Difficulty_Boss
   0x000F4356 = BuffLevel_Difficulty_PC
   0x000F4278 = BuffLevel_AIDifficulty

Also references to Status row 'Difficulty' = 0x000F4287, and the
Active_Difficulty_Intro skill 0x00016389.

The number of rows that mention these tells us how the buff is applied:
  - If only a few skill/AI rows reference them, the engine pushes the buff
    by name from C++.
  - If thousands of monster rows reference them, the data drives application.
"""
from __future__ import annotations
import os, sys, struct
from pathlib import Path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
OUT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_buff_xrefs.txt"

TARGET_HASHES = {
    0x000F4354: "BuffLevel_Difficulty",
    0x000F4355: "BuffLevel_Difficulty_Boss",
    0x000F4356: "BuffLevel_Difficulty_PC",
    0x000F4278: "BuffLevel_AIDifficulty",
    0x000F4287: "Status_Difficulty",
    0x00016389: "Active_Difficulty_Intro",
}

def main():
    vfs = VfsManager(GAME)
    pabgb_index = {}
    pabgh_index = {}
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        for e in pamt.file_entries:
            p = e.path.lower()
            if p.endswith(".pabgb"):
                pabgb_index[(g, p[:-6])] = e
            elif p.endswith(".pabgh"):
                pabgh_index[(g, p[:-6])] = e

    pairs = []
    seen = set()
    for (g, base), b in pabgb_index.items():
        h = pabgh_index.get((g, base))
        if not h or base in seen:
            continue
        seen.add(base)
        pairs.append((g, base, b, h))

    print(f"Scanning {len(pairs)} tables for {len(TARGET_HASHES)} buff hashes...")
    counts = {h: 0 for h in TARGET_HASHES}
    by_table = {h: {} for h in TARGET_HASHES}  # h -> {base: count}

    for i, (g, base, b, h) in enumerate(pairs):
        if i % 100 == 0:
            print(f"  [{i}/{len(pairs)}]", file=sys.stderr)
        try:
            data = vfs.read_entry_data(b)
            head = vfs.read_entry_data(h)
        except Exception:
            continue

        # Quick raw-byte search FIRST — if no hash byte present, skip parsing.
        skip = True
        for hash_id in TARGET_HASHES:
            patt = struct.pack("<I", hash_id)
            if patt in data:
                skip = False
                break
        if skip:
            continue

        try:
            tbl = parse_pabgb(data, head, os.path.basename(base))
        except Exception:
            continue

        for r in tbl.rows:
            for f in r.fields:
                if f.kind in ("u32", "hash") and isinstance(f.value, int):
                    if f.value in TARGET_HASHES:
                        counts[f.value] += 1
                        by_table[f.value].setdefault(base, 0)
                        by_table[f.value][base] += 1

    with open(OUT, "w", encoding="utf-8") as out:
        out.write("=== Difficulty buff/status xref counts across all pabgb tables ===\n\n")
        for h, name in TARGET_HASHES.items():
            out.write(f"--- {name}  (hash 0x{h:08X})  total xrefs: {counts[h]}\n")
            for base, c in sorted(by_table[h].items(), key=lambda kv: -kv[1])[:30]:
                out.write(f"    {c:6d}  {base}.pabgb\n")
            out.write("\n")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
