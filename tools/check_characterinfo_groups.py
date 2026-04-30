#!/usr/bin/env python3
"""Find every package group that ships characterinfo.pabgb (or pabgh).
Groups higher in load order would shadow group 0008. If two groups
both contain it, the later one wins — meaning user edits to 0008
get masked.

Also compares the byte-content of the Ogre row across each copy to
detect divergence (e.g., user edited the wrong copy).
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game",
                    default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")
    args = ap.parse_args()

    pkg = os.path.join(args.game, "packages")
    if not os.path.isdir(pkg):
        pkg = args.game
    vfs = VfsManager(pkg)

    print(f"Scanning groups for characterinfo.pab[gh|gb]...")
    found = []
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception as e:
            print(f"  [!] group {group} pamt fail: {e}")
            continue
        for entry in pamt.file_entries:
            p = entry.path.lower()
            if p.endswith("characterinfo.pabgb") or p.endswith("characterinfo.pabgh"):
                found.append((group, entry))

    print(f"\nFound {len(found)} matching entries:")
    for group, entry in found:
        size = entry.orig_size if entry.orig_size else entry.comp_size
        print(f"  group {group}  {entry.path}  paz={os.path.basename(entry.paz_file)}"
              f"  off=0x{entry.offset:08X}  size={size}  flags=0x{entry.flags:08X}")

    # Now read the Ogre row from each pabgb copy and compare.
    pabgb_entries = [(g, e) for g, e in found if e.path.lower().endswith(".pabgb")]
    pabgh_entries = {g: e for g, e in found if e.path.lower().endswith(".pabgh")}

    print(f"\nReading Ogre row from each pabgb copy...")
    for group, b_entry in pabgb_entries:
        h_entry = pabgh_entries.get(group)
        if not h_entry:
            print(f"  group {group}: no matching pabgh, skipping")
            continue
        try:
            data = vfs.read_entry_data(b_entry)
            head = vfs.read_entry_data(h_entry)
            tbl = parse_pabgb(data, head, "characterinfo.pabgb")
        except Exception as e:
            print(f"  group {group} parse error: {e}")
            continue
        ogre = None
        for r in tbl.rows:
            if r.name == "Boss_Ogre_55515":
                ogre = r
                break
        if not ogre:
            print(f"  group {group}: Boss_Ogre_55515 NOT in this copy ({len(tbl.rows)} rows)")
            continue
        # Inspect first 20 fields and the f32 stat fields.
        f32s = [(i, f) for i, f in enumerate(ogre.fields) if f.kind == "f32"]
        # Hash row.raw to compare.
        import hashlib
        h = hashlib.sha256(ogre.raw).hexdigest()[:16]
        print(f"  group {group}: Boss_Ogre_55515 row {ogre.index} sha256={h}"
              f"  size={ogre.data_size}  fields={len(ogre.fields)}  f32_count={len(f32s)}")
        # Print key f32s
        for idx in (18, 24, 29, 161, 174, 176):
            if idx < len(ogre.fields):
                f = ogre.fields[idx]
                print(f"      field[{idx}] kind={f.kind} value={f.value}  raw={f.raw.hex()}")


if __name__ == "__main__":
    main()
