#!/usr/bin/env python3
"""Search every pabgb table's PAMT path list for files matching
keywords like npc, monster, mercenary, boss, fight, stat, status.
Prints a clean list so we know exactly which Info tables exist
on disk vs which are merely classes in the EXE."""
from __future__ import annotations
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager

vfs = VfsManager(r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")

KEYWORDS = ["npc", "monster", "mercenary", "boss", "fight",
            "battle", "phase", "stage", "stat", "status", "regen",
            "buff", "skill", "drop", "loot", "faction", "ally",
            "enemy", "wanted", "mission", "quest", "info"]

paths = []
for g in vfs.list_package_groups():
    try:
        pamt = vfs.load_pamt(g)
    except Exception:
        continue
    for e in pamt.file_entries:
        p = e.path.lower()
        if p.endswith(".pabgb"):
            paths.append((g, p))

print(f"All .pabgb files: {len(paths)}")
print()
for g, p in sorted(set(paths)):
    print(f"  [{g}] {p}")

print()
print("Filtered by keyword:")
for kw in KEYWORDS:
    for g, p in sorted(set(paths)):
        if kw in p:
            print(f"  [{g}] {p}  -- matches '{kw}'")
