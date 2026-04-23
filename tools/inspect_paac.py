"""Inspect a .paac Action Chart for strings + numeric fields.

Pearl Abyss Action Charts are binary with strings spread throughout.
We dump every ASCII run + every fp32 value in a sane range, so we
can eyeball which numeric value is the auto-sheathe timer.
"""

from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

# Files to inspect — the most suspicious candidates from the .paac scan.
TARGETS = [
    "actionchart/auxweapon_onehand_lower.paac",
    "actionchart/auxweapon_twohand_lower.paac",
    "actionchart/auxweapon_dualblade_lower.paac",
    "actionchart/auxweapon_bow_lower.paac",
    "actionchart/basic_lower.paac",
    "actionchart/basic_upper.paac",
    "actionchart/basic_combat.paac",
    "actionchart/weapon_base.paac",
    "actionchart/battle.paac",
    "actionchart/common.paac",
]


def main() -> None:
    vfs = VfsManager(GAME)

    # Build an index across all groups.
    index = {}
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
            for e in pamt.file_entries:
                p = e.path.lower().replace("\\", "/")
                index[p] = (g, e)
        except Exception:
            pass

    ascii_re = re.compile(rb"[\x20-\x7E]{5,}")

    for target in TARGETS:
        key = target.lower()
        if key not in index:
            print(f"[miss] {target}")
            continue
        g, e = index[key]
        try:
            data = vfs.read_entry_data(e)
        except Exception as ex:
            print(f"[read-fail] {target}: {ex}")
            continue

        print()
        print("=" * 72)
        print(f"{target}  ({len(data):,} bytes)")
        print("=" * 72)

        # Print every printable ASCII run (4+ chars) — gives the
        # "schema" of the file: state names, transition names, etc.
        strings = [m.group(0).decode("ascii", "replace")
                   for m in ascii_re.finditer(data)]
        print(f"  distinct strings: {len(strings)}")
        for s in strings[:40]:
            print(f"    {s}")
        if len(strings) > 40:
            print(f"    ... +{len(strings) - 40} more")

        # Find fp32 values in a reasonable timer range (0.5 to 60.0 seconds).
        print("  fp32 values in [0.5, 60.0] (potential timers):")
        shown = 0
        for off in range(0, len(data) - 4, 4):
            v = struct.unpack_from("<f", data, off)[0]
            if 0.5 <= v <= 60.0 and not (v == int(v) and int(v) % 1 == 0 and int(v) > 30):
                # Check that the adjacent fp32 at +4 isn't also in the range
                # (otherwise it's probably 3D vector data, not a timer field).
                # Soft heuristic only.
                print(f"    @{off:#06x}  = {v:.4f}")
                shown += 1
                if shown >= 30:
                    break


if __name__ == "__main__":
    main()
