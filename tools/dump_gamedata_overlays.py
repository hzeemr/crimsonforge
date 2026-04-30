#!/usr/bin/env python3
"""List the single-file 'gamedata' overlay groups (0019, 0021-0032)
to find what they shadow. If any of them contains characterinfo.*,
that's the authoritative copy."""
from __future__ import annotations
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager

vfs = VfsManager(r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")

groups = vfs.list_package_groups()
for g in groups:
    try:
        pamt = vfs.load_pamt(g)
    except Exception as e:
        print(f"[{g}] load fail: {e}")
        continue
    n = len(pamt.file_entries)
    if n <= 5:
        print(f"\n=== group {g} ({n} files) ===")
        for e in pamt.file_entries:
            sz = e.orig_size or e.comp_size
            print(f"  {e.path}  paz={os.path.basename(e.paz_file)}"
                  f"  off=0x{e.offset:08X}  size={sz}")
