#!/usr/bin/env python3
"""Find Mon_Flame_Knight row(s) in characterinfo.pabgb and dump them.

Also enumerates any Flame/Knight-adjacent rows, foreign keys to other
*Info tables, mesh/skeleton/animation/prefab/paseqc files referenced
or named after Mon_Flame_Knight.
"""
from __future__ import annotations
import argparse, csv, os, sys, json
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb


KEYS_OF_INTEREST = ("Mon_Flame_Knight", "Flame_Knight", "FlameKnight")


def load_characterinfo(vfs):
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception:
            continue
        pabgb_entry = pabgh_entry = None
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl == "gamedata/characterinfo.pabgb":
                pabgb_entry = entry
            elif pl == "gamedata/characterinfo.pabgh":
                pabgh_entry = entry
        if not (pabgb_entry and pabgh_entry):
            continue
        try:
            d = vfs.read_entry_data(pabgb_entry)
            hd = vfs.read_entry_data(pabgh_entry)
            return parse_pabgb(d, hd, "characterinfo.pabgb"), group
        except Exception as e:
            print("parse error in group", group, e, file=sys.stderr)
            continue
    return None, None


def find_rows(table, keywords):
    out = []
    for r in table.rows:
        nm = (r.name or "")
        if any(k.lower() in nm.lower() for k in keywords):
            out.append(r)
    return out


def dump_row_txt(row, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"=== {row.display_name} ===\n")
        f.write(f"Index: {row.index}\n")
        f.write(f"Hash:  0x{row.row_hash:08X}\n")
        f.write(f"Size:  {row.data_size} bytes\n")
        f.write(f"Fields: {len(row.fields)}\n\n")
        tc = {}
        for fld in row.fields:
            tc[fld.kind] = tc.get(fld.kind, 0) + 1
        for k in sorted(tc):
            f.write(f"{k}: {tc[k]}\n")
        f.write("\n")
        for i, fld in enumerate(row.fields):
            if fld.kind == "str":
                f.write(f"[{i:>4}] str @ {fld.offset:>5}: {fld.value!r}\n")
            elif fld.kind == "f32":
                try:
                    v = float(fld.value)
                except Exception:
                    v = fld.value
                f.write(f"[{i:>4}] f32 @ {fld.offset:>5}: {v}\n")
            elif fld.kind == "hash":
                try:
                    v = int(fld.value)
                except Exception:
                    v = fld.value
                f.write(f"[{i:>4}] hash@ {fld.offset:>5}: 0x{v:08X}\n")
            else:
                f.write(f"[{i:>4}] {fld.kind:<4}@ {fld.offset:>5}: {fld.value}\n")


def dump_row_csv(row, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["field_idx", "offset", "size", "kind", "raw_hex",
                    "value_u32", "value_f32", "value_str", "value_hash"])
        for i, fld in enumerate(row.fields):
            v_u32 = v_f32 = v_str = v_hash = ""
            if fld.kind == "u32" and isinstance(fld.value, int):
                v_u32 = str(fld.value)
            elif fld.kind == "f32" and isinstance(fld.value, (int, float)):
                v_f32 = f"{float(fld.value):.6f}"
            elif fld.kind == "str":
                v_str = str(fld.value)
            elif fld.kind == "hash" and isinstance(fld.value, int):
                v_hash = f"0x{fld.value:08X}"
            w.writerow([i, fld.offset, fld.size, fld.kind, fld.raw.hex(),
                        v_u32, v_f32, v_str, v_hash])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--game",
        default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
    )
    ap.add_argument("--out", default=str(Path(__file__).parent / "flame_knight"))
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="just list matches, don't dump full rows",
    )
    ap.add_argument(
        "--list-flame",
        action="store_true",
        help="enumerate every row whose name contains 'Flame' or 'Knight'",
    )
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("Loading VFS...")
    vfs = VfsManager(args.game)

    print("Reading characterinfo.pabgb...")
    table, group = load_characterinfo(vfs)
    if table is None:
        print("ERROR: characterinfo.pabgb not found", file=sys.stderr)
        return 2
    print(f"  group={group}  rows={len(table.rows)}")

    if args.list_flame:
        out = []
        for r in table.rows:
            n = r.name or ""
            if "flame" in n.lower() or "knight" in n.lower():
                out.append((r.index, n, r.data_size, len(r.fields)))
        out.sort()
        with open(os.path.join(args.out, "flame_knight_candidates.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"# rows with 'flame' or 'knight' in name: {len(out)}\n")
            for idx, n, sz, fc in out:
                f.write(f"  idx={idx:>5}  size={sz:>5}  fields={fc:>4}  {n}\n")
        print(f"  wrote {len(out)} candidates")

    rows = find_rows(table, KEYS_OF_INTEREST)
    print(f"Mon_Flame_Knight matches: {len(rows)}")
    for r in rows:
        print(f"  idx={r.index}  size={r.data_size}b  fields={len(r.fields)}  {r.name}")

    if args.list_only:
        return 0

    summary = []
    for r in rows:
        safe = r.name.replace("/", "_").replace("\\", "_")
        csv_p = os.path.join(args.out, f"{safe}_row_dump.csv")
        txt_p = os.path.join(args.out, f"{safe}_row_dump.txt")
        dump_row_csv(r, csv_p)
        dump_row_txt(r, txt_p)
        tc = {}
        for fld in r.fields:
            tc[fld.kind] = tc.get(fld.kind, 0) + 1
        summary.append({
            "name": r.name,
            "index": r.index,
            "hash_hex": f"0x{r.row_hash:08X}",
            "size_bytes": r.data_size,
            "field_count": len(r.fields),
            "kinds": tc,
            "csv": os.path.basename(csv_p),
            "txt": os.path.basename(txt_p),
        })
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nWrote {len(rows)} dumps to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
