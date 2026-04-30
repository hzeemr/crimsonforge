#!/usr/bin/env python3
"""Find every .pabgb table whose path or rows mention difficulty.

Three passes:
  1. Path filter — list every pabgb whose path contains a difficulty keyword.
  2. Row-name filter — for every table, scan row.name for difficulty keywords.
  3. Field-string filter — scan every string field for difficulty keywords
     (catches tables that store difficulty as data not metadata).

Writes results to tools/difficulty_pabgb_hits.txt and dumps the first
3 rows of each candidate table to tools/difficulty_table_rows.txt.
"""
from __future__ import annotations
import os, sys, traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

KEYS_PATH = ["difficulty", "difficult", "gamelevel", "playlevel",
             "gameconfig", "gamemode", "battlemode", "scaleinfo",
             "monsterscale", "bossscale", "stagelevel", "modifierscale"]

KEYS_NAME = ["Difficulty", "DIFFICULTY", "difficulty",
             "GameLevel", "PlayLevel", "GameMode",
             "Easy", "Normal", "Hard", "VeryHard", "Extreme",
             "EASY", "NORMAL", "HARD", "VERYHARD",
             "Beginner", "Intermediate", "Expert", "Master",
             "scale", "Scale", "SCALE",
             "multiplier", "Multiplier",
             "ratio", "Ratio"]

# Build (group, base) -> entry maps for pabgb + pabgh
def build_index(vfs):
    pabgb, pabgh = {}, {}
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        for e in pamt.file_entries:
            p = e.path.lower()
            if p.endswith(".pabgb"):
                pabgb[(g, p[:-6])] = e
            elif p.endswith(".pabgh"):
                pabgh[(g, p[:-6])] = e
    return pabgb, pabgh

def main():
    out_dir = Path(__file__).parent
    hits_path = out_dir / "difficulty_pabgb_hits.txt"
    rows_path = out_dir / "difficulty_table_rows.txt"

    print(f"Loading VFS from {GAME}")
    vfs = VfsManager(GAME)
    pabgb, pabgh = build_index(vfs)

    print(f"Indexed {len(pabgb)} pabgb / {len(pabgh)} pabgh files")

    # PASS 1 — path filter
    path_hits = []
    for (g, base), entry in pabgb.items():
        for k in KEYS_PATH:
            if k in base:
                path_hits.append((g, base, k))
                break

    print(f"\n[pass1] {len(path_hits)} tables with difficulty-shaped name")

    # Dedupe by base across groups
    seen_bases = set()
    unique_pairs = []
    for (g, base), b in pabgb.items():
        h = pabgh.get((g, base))
        if not h or base in seen_bases:
            continue
        seen_bases.add(base)
        unique_pairs.append((g, base, b, h))

    # PASS 2 + 3 — scan every table's rows for difficulty-shaped names/strings
    row_hits = []   # (group, base, row_idx, row_name, kind)
    for i, (g, base, b, h) in enumerate(unique_pairs):
        if i % 100 == 0:
            print(f"  [{i}/{len(unique_pairs)}] row scan", file=sys.stderr)
        try:
            data = vfs.read_entry_data(b)
            head = vfs.read_entry_data(h)
            tbl = parse_pabgb(data, head, os.path.basename(base))
        except Exception:
            continue
        for r in tbl.rows:
            rn = r.name or ""
            for k in KEYS_NAME:
                if k in rn:
                    row_hits.append((g, base, r.index, rn, "row.name", k))
                    break
            else:
                # check string fields too
                for fi, f in enumerate(r.fields):
                    if f.kind == "str" and isinstance(f.value, str):
                        v = f.value
                        for k in KEYS_NAME:
                            if k in v and k not in v.replace(k, "", 1):
                                # only meaningful if it looks like an identifier, not a sentence
                                if len(v) <= 80:
                                    row_hits.append((g, base, r.index, rn, f"field[{fi}]={v}", k))
                                    break
                        else:
                            continue
                        break

    print(f"\n[pass2/3] {len(row_hits)} row-level hits")

    # write reports
    with open(hits_path, "w", encoding="utf-8") as f:
        f.write("=== PASS 1: pabgb files with difficulty-shaped paths ===\n\n")
        for g, base, k in sorted(set(path_hits)):
            f.write(f"  [{g}] {base}.pabgb   matches '{k}'\n")
        f.write(f"\nTotal: {len(set(path_hits))}\n\n")

        f.write("=== PASS 2/3: row-level hits ===\n\n")
        # group by base
        from collections import defaultdict
        by_base = defaultdict(list)
        for h in row_hits:
            by_base[h[1]].append(h)
        for base in sorted(by_base):
            entries = by_base[base]
            f.write(f"\n+++ {base}.pabgb  ({len(entries)} hits)\n")
            for g, _, idx, rn, where, key in entries[:20]:
                f.write(f"    row {idx}  name='{rn}'   {where}  match='{key}'\n")
            if len(entries) > 20:
                f.write(f"    ... ({len(entries) - 20} more)\n")

    print(f"Wrote {hits_path}")

    # Dump first few rows of high-value candidates: anything in path_hits
    # plus any base whose row_hits count >= 2
    candidates = set(b for _, b, _ in path_hits)
    from collections import Counter
    base_counts = Counter(h[1] for h in row_hits)
    for b, c in base_counts.items():
        if c >= 2 and "ifficulty" in b.lower():
            candidates.add(b)
        elif c >= 4 and any(k in b for k in ["scale", "level", "stat", "info"]):
            candidates.add(b)

    print(f"\nDumping {len(candidates)} candidate tables")
    pair_lookup = {base: (g, b, h) for g, base, b, h in unique_pairs}
    with open(rows_path, "w", encoding="utf-8") as f:
        for base in sorted(candidates):
            t = pair_lookup.get(base)
            if not t:
                continue
            g, b, h = t
            try:
                data = vfs.read_entry_data(b)
                head = vfs.read_entry_data(h)
                tbl = parse_pabgb(data, head, os.path.basename(base))
            except Exception as ex:
                f.write(f"\n!!! {base} parse fail: {ex!r}\n")
                continue
            f.write(f"\n{'=' * 78}\n")
            f.write(f"=== {base}.pabgb   group={g}   rows={len(tbl.rows)}   simple={tbl.is_simple}   row_size={tbl.row_size}   field_count={tbl.field_count}\n")
            f.write(f"{'=' * 78}\n")
            for r in tbl.rows[:10]:
                f.write(f"\n  ROW {r.index}  name='{r.name}'  hash=0x{r.row_hash:08X}  size={r.data_size}b\n")
                for fi, fld in enumerate(r.fields[:24]):
                    f.write(f"    [{fi:02d}] off={fld.offset:3d} {fld.kind:5} = {fld.display_value()}\n")
                if len(r.fields) > 24:
                    f.write(f"    ... ({len(r.fields) - 24} more fields)\n")
            if len(tbl.rows) > 10:
                f.write(f"\n  ... ({len(tbl.rows) - 10} more rows)\n")

    print(f"Wrote {rows_path}")

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
