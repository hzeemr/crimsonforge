"""Broader scan: find every row whose name contains weapon/combat/
sheathe/unready keywords, dump all numeric fields. We're looking for
the timer regardless of exact value."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pabgb_parser import parse_pabgb
from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

HOT = (
    "sheath", "sheathe", "unready", "unsheath",
    "weapon", "combat", "battle", "fight",
    "attack", "ready", "draw",
    "readymode", "weaponmode",
    "sword", "blade",
    "autosheathe", "auto_sheathe",
)


def _name_is_hot(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in HOT)


def main() -> None:
    vfs = VfsManager(GAME)
    pabgbs = []
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
            for e in pamt.file_entries:
                if e.path.lower().endswith(".pabgb"):
                    pabgbs.append((g, e))
        except Exception:
            pass

    for g, e in pabgbs:
        try:
            data = vfs.read_entry_data(e)
            table = parse_pabgb(data, os.path.basename(e.path))
        except Exception:
            continue

        name = os.path.basename(e.path)
        hot_rows = []
        for row in table.rows:
            strings = [str(f.value) for f in row.fields if f.kind == "str"]
            if _name_is_hot(row.name) or any(_name_is_hot(s) for s in strings):
                hot_rows.append(row)

        if not hot_rows:
            continue

        print(f"=== {name}  ({len(table.rows)} rows)  hot rows: {len(hot_rows)} ===")
        for row in hot_rows[:25]:
            strings = [str(f.value) for f in row.fields if f.kind == "str"]
            nums = []
            for f in row.fields:
                if f.kind == "f32":
                    nums.append(f"f32={f.value:.3f}")
                elif f.kind == "i32":
                    if -100000 <= f.value <= 100000:
                        nums.append(f"i32={f.value}")
                elif f.kind == "u32":
                    v = f.value
                    if 0 < v < 100000:
                        nums.append(f"u32={v}")
            str_preview = " / ".join(s for s in strings[:4] if s)[:100]
            num_preview = " ".join(nums[:8])
            print(f"  row #{row.index:5d}  {row.name!r}")
            if str_preview:
                print(f"     strs: {str_preview}")
            if num_preview:
                print(f"     nums: {num_preview}")
        if len(hot_rows) > 25:
            print(f"  ... and {len(hot_rows) - 25} more")
        print()


if __name__ == "__main__":
    main()
