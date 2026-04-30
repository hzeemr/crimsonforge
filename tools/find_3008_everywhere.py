#!/usr/bin/env python3
"""Exhaustive byte search for 3008.0 fp32 (and related values) across every
file in every PAZ.

Searches for:
  * 3008.0  fp32 LE  (bytes 00 00 3C 45)
  * -3008.0 fp32 LE  (bytes 00 00 BC 45)
  * Some near-3008 patterns ('00 70 BC 44')
  * row-hash 0x000F492A in LE (2A 49 0F 00) and BE (00 0F 49 2A)
  * literal string 'Boss_Ogre'

Logs every hit to stdout summary AND a CSV (tools/3008_hits_all_files.csv)
with: pattern_name, file_path, ext, offset, surrounding_hex.

Also dumps the .pabgh header for gamedata/characterinfo.pabgb.

And lists every row in characterinfo.pabgb whose name starts with 'Boss_Ogre'.
"""
from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
import traceback
from collections import defaultdict
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb


# ---------------------------------------------------------------------------
# Patterns to search for
# ---------------------------------------------------------------------------
# Each entry: (label, byte_pattern_to_search)
PATTERNS: list[tuple[str, bytes]] = [
    ("fp32_+3008.0",   struct.pack("<f", 3008.0)),     # 00 00 3C 45
    ("fp32_-3008.0",   struct.pack("<f", -3008.0)),    # 00 00 BC 45
    ("fp32_+1504.0",   struct.pack("<f", 1504.0)),     # half of 3008
    ("fp32_+6016.0",   struct.pack("<f", 6016.0)),     # 2x of 3008
    ("u32_3008",       struct.pack("<I", 3008)),       # integer 3008
    ("u32_30080",      struct.pack("<I", 30080)),      # 3008 * 10
    ("hash_LE_000F492A", struct.pack("<I", 0x000F492A)),  # 2A 49 0F 00
    ("hash_BE_000F492A", struct.pack(">I", 0x000F492A)),  # 00 0F 49 2A
    ("str_Boss_Ogre",  b"Boss_Ogre"),
]


def search_all(data: bytes, pattern: bytes) -> list[int]:
    """Find every occurrence of pattern in data."""
    if not pattern:
        return []
    hits = []
    i = 0
    while True:
        j = data.find(pattern, i)
        if j < 0:
            break
        hits.append(j)
        i = j + 1   # allow overlapping
    return hits


def hex_window(data: bytes, off: int, before: int = 8, after: int = 24) -> str:
    s = max(0, off - before)
    e = min(len(data), off + after)
    return data[s:e].hex()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--game",
        default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
    )
    p.add_argument("--out", default=str(Path(__file__).parent))
    p.add_argument("--max-hits-per-file", type=int, default=64,
                   help="cap hits logged per (file, pattern) to keep CSV sane")
    p.add_argument("--limit-files", type=int, default=0,
                   help="(debug) only scan this many files; 0 = all")
    args = p.parse_args()

    packages_path = os.path.join(args.game, "packages")
    if not os.path.isdir(packages_path):
        packages_path = args.game

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "3008_hits_all_files.csv"
    summary_path = out_dir / "3008_hits_summary.txt"

    print(f"Loading VFS from {packages_path}")
    vfs = VfsManager(packages_path)

    # ------------------------------------------------------------------
    # 1) Dump characterinfo.pabgh raw bytes  (job item #3)
    # ------------------------------------------------------------------
    pabgh_dump_path = out_dir / "characterinfo_pabgh_first200.txt"
    pabgh_entry = None
    pabgb_entry = None
    for grp in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(grp)
        except Exception:
            continue
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl == 'gamedata/characterinfo.pabgh':
                pabgh_entry = entry
            elif pl == 'gamedata/characterinfo.pabgb':
                pabgb_entry = entry
        if pabgh_entry and pabgb_entry:
            break

    if pabgh_entry:
        try:
            head_data = vfs.read_entry_data(pabgh_entry)
            with open(pabgh_dump_path, "w", encoding="utf-8") as f:
                f.write(f"characterinfo.pabgh size={len(head_data)} bytes\n")
                f.write("first 200 bytes hex:\n")
                f.write(head_data[:200].hex())
                f.write("\n\n")
                # Parse simple parts: first u16 = row count
                if len(head_data) >= 2:
                    n = struct.unpack_from("<H", head_data, 0)[0]
                    f.write(f"row_count (u16 LE @0) = {n}\n")
                    # Find Boss_Ogre row by hash 0x000F492A in header.
                    # Hashed flavour = 8 bytes per descriptor at offset 2:
                    # [hash:4] [data_offset:4]
                    desc_off = 2
                    found = False
                    for i in range(n):
                        if desc_off + 8 > len(head_data):
                            break
                        h, doff = struct.unpack_from("<II", head_data,
                                                    desc_off)
                        if h == 0x000F492A:
                            f.write(
                                f"Boss_Ogre_55515 descriptor:\n"
                                f"  index = {i}\n"
                                f"  header_offset = {desc_off}\n"
                                f"  hash = 0x{h:08X}\n"
                                f"  data_offset = {doff} (0x{doff:X})\n"
                            )
                            # Show next descriptor for size calc
                            if desc_off + 16 <= len(head_data):
                                h2, doff2 = struct.unpack_from(
                                    "<II", head_data, desc_off + 8
                                )
                                f.write(
                                    f"  next_desc hash=0x{h2:08X}  "
                                    f"data_offset={doff2} "
                                    f"-> implied row size = {doff2 - doff}\n"
                                )
                            found = True
                            break
                        desc_off += 8
                    if not found:
                        f.write(
                            "Boss_Ogre_55515 hash NOT in pabgh header "
                            "(checked hashed flavour)\n"
                        )
            print(f"Wrote {pabgh_dump_path}")
        except Exception as e:
            print(f"Failed to dump pabgh: {e!r}", file=sys.stderr)
    else:
        print("characterinfo.pabgh not found in any group", file=sys.stderr)

    # ------------------------------------------------------------------
    # 2) Boss_Ogre* row enumeration in characterinfo.pabgb (job item #6)
    # ------------------------------------------------------------------
    boss_ogre_rows_path = out_dir / "boss_ogre_rows_in_characterinfo.txt"
    if pabgb_entry and pabgh_entry:
        try:
            data = vfs.read_entry_data(pabgb_entry)
            head = vfs.read_entry_data(pabgh_entry)
            tbl = parse_pabgb(data, head, 'characterinfo.pabgb')
            lines = ["Rows in characterinfo.pabgb whose name starts with "
                     "'Boss_Ogre':\n"]
            count = 0
            for r in tbl.rows:
                rn = r.name or ""
                if rn.startswith("Boss_Ogre"):
                    lines.append(
                        f"  row {r.index}  name='{rn}'  "
                        f"hash=0x{r.row_hash:08X}  size={r.data_size}b  "
                        f"fields={len(r.fields)}"
                    )
                    count += 1
            lines.append(f"\nTotal Boss_Ogre* rows: {count}")
            with open(boss_ogre_rows_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"Wrote {boss_ogre_rows_path}  ({count} rows)")
        except Exception as e:
            print(f"Failed to enumerate Boss_Ogre rows: {e!r}",
                  file=sys.stderr)

    # ------------------------------------------------------------------
    # 3) Walk every entry in every PAZ and search.
    # ------------------------------------------------------------------
    # Per-pattern stats:
    #   ext_counts[pattern_label][ext] = #files with at least 1 hit
    #   ext_hit_totals[pattern_label][ext] = total hits across files
    ext_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ext_hit_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    # Per-file aggregate (most-hit files) for fp32 +3008
    files_with_3008: list[tuple[int, str, str]] = []  # (count, ext, path)

    csv_rows: list[list] = []
    csv_rows.append([
        "pattern", "path", "ext", "group",
        "file_size", "offset", "hex_window"
    ])

    # Build (group, entry) list, dedup by lowercase path (an entry can
    # appear in multiple groups; we want first one we see).
    seen_paths: set[str] = set()
    work: list[tuple[str, object]] = []
    for grp in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(grp)
        except Exception:
            continue
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl in seen_paths:
                continue
            seen_paths.add(pl)
            work.append((grp, entry))

    if args.limit_files > 0:
        work = work[:args.limit_files]

    print(f"Scanning {len(work)} unique files for {len(PATTERNS)} patterns...")

    failures = 0
    for i, (grp, entry) in enumerate(work):
        try:
            blob = vfs.read_entry_data(entry)
        except Exception as e:
            failures += 1
            if failures <= 10:
                print(f"  read fail [{i}] {entry.path}: {e!r}",
                      file=sys.stderr)
            continue

        ext = os.path.splitext(entry.path.lower())[1] or "<noext>"
        size = len(blob)

        for label, pat in PATTERNS:
            hits = search_all(blob, pat)
            if not hits:
                continue
            ext_counts[label][ext] += 1
            ext_hit_totals[label][ext] += len(hits)
            if label == "fp32_+3008.0":
                files_with_3008.append((len(hits), ext, entry.path))
            cap = args.max_hits_per_file
            for off in hits[:cap]:
                csv_rows.append([
                    label, entry.path, ext, grp,
                    size, off, hex_window(blob, off)
                ])

        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{len(work)}] scanned, "
                  f"3008-hit files so far: {len(files_with_3008)}",
                  file=sys.stderr)

    # ------------------------------------------------------------------
    # 4) Write outputs
    # ------------------------------------------------------------------
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(csv_rows)
    print(f"Wrote {csv_path}  ({len(csv_rows)-1} hit rows)")

    # Write a focused summary
    files_with_3008.sort(key=lambda t: -t[0])
    sum_lines: list[str] = []
    sum_lines.append("=" * 78)
    sum_lines.append("3008.0 hunt — summary")
    sum_lines.append("=" * 78)
    sum_lines.append(f"Files scanned: {len(work)}")
    sum_lines.append(f"Read failures: {failures}")
    sum_lines.append("")

    for label, _ in PATTERNS:
        sum_lines.append(f"--- pattern {label} ---")
        if not ext_counts[label]:
            sum_lines.append("  (no hits)")
            sum_lines.append("")
            continue
        # by ext, sorted by file count desc
        items = sorted(ext_counts[label].items(), key=lambda kv: -kv[1])
        for ext, fc in items[:30]:
            tot = ext_hit_totals[label][ext]
            sum_lines.append(f"  {ext:18s}  files={fc:6d}  total_hits={tot}")
        sum_lines.append("")

    sum_lines.append("--- top 30 files by # of fp32_+3008.0 hits ---")
    for cnt, ext, path in files_with_3008[:30]:
        sum_lines.append(f"  {cnt:5d}  {ext:18s}  {path}")
    sum_lines.append("")

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sum_lines))
    print(f"Wrote {summary_path}")

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
