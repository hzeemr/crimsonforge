#!/usr/bin/env python3
"""URGENT: Find every pabgb table that references Boss_Ogre_55515.

Hunts in parallel:
  1. Row whose .name == 'Boss_Ogre_55515'
  2. Row whose row_hash == 0x000F492A (the table-key hash)
  3. Any field (str or u32) inside any row matching either of the above

Writes:
  tools/ogre_pabgb_hits.txt   -- human report
  tools/ogre_pabgb_hits.csv   -- machine-readable hit list
"""
from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
import traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

# Things to search for in every row of every pabgb.
TARGETS_STR = ("Boss_Ogre_55515",)
# Hash of "Boss_Ogre_55515" as known from prior research.
TARGETS_U32 = (0x000F492A,)


def iter_pabgb_pairs(vfs: VfsManager):
    """Yield (group, base_path_no_ext, pabgb_entry, pabgh_entry) for every
    pabgb in every package group. base_path_no_ext is the lowercase path
    inside packages with no extension, used for filename lookups."""
    pabgh_index = {}  # (group, base) -> entry
    pabgb_index = {}
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception:
            continue
        for entry in pamt.file_entries:
            p = entry.path.lower()
            if p.endswith(".pabgb"):
                base = p[: -len(".pabgb")]
                pabgb_index[(group, base)] = entry
            elif p.endswith(".pabgh"):
                base = p[: -len(".pabgh")]
                pabgh_index[(group, base)] = entry

    seen = set()
    for key, b_entry in pabgb_index.items():
        h_entry = pabgh_index.get(key)
        if not h_entry:
            continue
        # Dedupe by path so the same table from multiple groups is only
        # processed once (later group occurrences overwrite the earlier
        # one in dicts, but to be safe we filter here too).
        if key[1] in seen:
            continue
        seen.add(key[1])
        yield key[0], key[1], b_entry, h_entry


def field_matches_target(fld) -> tuple[bool, str]:
    """Return (matches, reason) for a single PabgbField."""
    if fld.kind == "str":
        v = str(fld.value)
        for t in TARGETS_STR:
            if t in v:
                return True, f"string contains '{t}'"
    elif fld.kind in ("u32", "hash"):
        v = int(fld.value) if isinstance(fld.value, int) else 0
        for t in TARGETS_U32:
            if v == t:
                return True, f"u32 == 0x{t:08X}"
    return False, ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--game",
        default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
    )
    p.add_argument("--out", default=str(Path(__file__).parent))
    args = p.parse_args()

    packages_path = os.path.join(args.game, "packages")
    if not os.path.isdir(packages_path):
        # Fall back: maybe args.game already IS the packages dir.
        packages_path = args.game

    print(f"Loading VFS from {packages_path}")
    vfs = VfsManager(packages_path)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "ogre_pabgb_hits.txt"
    csv_path = out_dir / "ogre_pabgb_hits.csv"

    txt_lines: list[str] = []
    csv_rows: list[list] = []

    txt_lines.append("=" * 78)
    txt_lines.append(
        "Boss_Ogre_55515 / hash 0x000F492A — every pabgb table containing it"
    )
    txt_lines.append("=" * 78)

    total_tables = 0
    total_hits = 0

    pairs = list(iter_pabgb_pairs(vfs))
    print(f"Scanning {len(pairs)} pabgb tables...")

    for i, (group, base, b_entry, h_entry) in enumerate(pairs):
        total_tables += 1
        try:
            data = vfs.read_entry_data(b_entry)
            head = vfs.read_entry_data(h_entry)
        except Exception as e:
            txt_lines.append(f"[!] read fail {base} ({group}): {e!r}")
            continue

        try:
            tbl = parse_pabgb(data, head, os.path.basename(base))
        except Exception as e:
            txt_lines.append(f"[!] parse fail {base} ({group}): {e!r}")
            continue

        # 1. Row-name and row-hash hits.
        for r in tbl.rows:
            rname = r.name or ""
            rname_hit = rname in TARGETS_STR
            rhash_hit = r.row_hash in TARGETS_U32
            field_hits = []
            for fi, fld in enumerate(r.fields):
                ok, reason = field_matches_target(fld)
                if ok:
                    field_hits.append((fi, fld, reason))

            if not (rname_hit or rhash_hit or field_hits):
                continue

            total_hits += 1
            txt_lines.append("")
            txt_lines.append(
                f"+++ HIT in {base}.pabgb  (group {group})"
            )
            txt_lines.append(
                f"    row {r.index}  name='{rname}'  row_hash=0x{r.row_hash:08X}  "
                f"size={r.data_size}b  fields={len(r.fields)}"
            )
            if rname_hit:
                txt_lines.append("    ** row.name matches **")
                csv_rows.append(
                    [base, group, r.index, rname, f"0x{r.row_hash:08X}",
                     "row.name", "", ""]
                )
            if rhash_hit:
                txt_lines.append("    ** row_hash matches **")
                csv_rows.append(
                    [base, group, r.index, rname, f"0x{r.row_hash:08X}",
                     "row_hash", "", ""]
                )
            for fi, fld, reason in field_hits:
                v = fld.value
                vs = str(v)[:60]
                txt_lines.append(
                    f"    field [{fi}] @ off {fld.offset} ({fld.kind}): "
                    f"{vs}  -- {reason}"
                )
                csv_rows.append(
                    [base, group, r.index, rname, f"0x{r.row_hash:08X}",
                     f"field[{fi}]", fld.kind, vs]
                )

        # Periodic progress on stderr so we can see scan running.
        if i % 25 == 0:
            print(f"  [{i}/{len(pairs)}] scanned, hits so far: {total_hits}",
                  file=sys.stderr)

    txt_lines.append("")
    txt_lines.append("=" * 78)
    txt_lines.append(f"Tables scanned: {total_tables}")
    txt_lines.append(f"Total hits:     {total_hits}")
    txt_lines.append("=" * 78)

    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["pabgb_path", "group", "row_idx", "row_name", "row_hash",
             "where", "field_kind", "value"]
        )
        w.writerows(csv_rows)

    print(f"Wrote {txt_path}")
    print(f"Wrote {csv_path}  ({len(csv_rows)} hit rows)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
