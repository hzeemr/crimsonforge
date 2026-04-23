"""Structured scan: parse .pabgb rows and find ones whose row-name
or nearby string fields match combat/weapon/idle/timer keywords AND
have a numeric field equal to 12.0f.

This is MUCH more precise than the raw byte scan — we only flag a
row when the STRING context of the row itself looks like a combat
timer, not just because the bytes 00 00 40 41 happen to appear
inside a random GUID or item ID.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pabgb_parser import parse_pabgb
from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

# Keywords that signal the row is about weapon / combat / idle state.
HOT = (
    "sheath", "sheathe", "unready", "unsheath",
    "weapon", "combat", "battle", "idle",
    "draw", "exit", "enter",
    "timer", "timeout", "duration", "delay",
    "readymode", "weaponmode", "sword", "blade",
)

TARGETS = (11.0, 11.5, 12.0, 12.5, 13.0, 15.0, 10.0, 14.0, 8.0)


def _name_is_hot(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in HOT)


def _row_has_target_float(row) -> list[tuple[str, float, int]]:
    """Return list of (kind, value, offset) for fields matching TARGETS."""
    hits = []
    for f in row.fields:
        if f.kind == "f32":
            try:
                v = float(f.value)
            except Exception:
                continue
            for t in TARGETS:
                if abs(v - t) < 0.0001:
                    hits.append(("f32", v, f.offset))
                    break
    return hits


def _row_strings(row) -> list[str]:
    return [str(f.value) for f in row.fields if f.kind == "str"]


def main() -> None:
    vfs = VfsManager(GAME)
    groups = vfs.list_package_groups()
    print(f"Scanning {len(groups)} package groups for structured hits...")

    pabgbs = []
    for g in groups:
        try:
            pamt = vfs.load_pamt(g)
            for e in pamt.file_entries:
                if e.path.lower().endswith(".pabgb"):
                    pabgbs.append((g, e))
        except Exception:
            pass
    print(f"Total .pabgb: {len(pabgbs)}")

    # Categorise findings:
    # 1. HOT_NAME  — row name itself contains a HOT keyword
    # 2. HOT_STRING — a string field in the row contains a HOT keyword
    # 3. TARGET_FLOAT — row has a float field ~12.0
    # Intersection of categories is what we care most about.
    for g, e in pabgbs:
        try:
            data = vfs.read_entry_data(e)
        except Exception:
            continue
        try:
            table = parse_pabgb(data, os.path.basename(e.path))
        except Exception:
            continue

        name = os.path.basename(e.path)
        hot_rows = []
        for row in table.rows:
            name_hot = _name_is_hot(row.name)
            strings = _row_strings(row)
            string_hot = any(_name_is_hot(s) for s in strings)
            targets = _row_has_target_float(row)

            if (name_hot or string_hot) and targets:
                hot_rows.append((row, targets, name_hot, string_hot, strings))

        if not hot_rows:
            continue

        print()
        print(f"=== {name}  ({len(table.rows)} rows)  hot+target hits: {len(hot_rows)} ===")
        for row, targets, nh, sh, strings in hot_rows[:20]:
            tag = []
            if nh: tag.append("NAME")
            if sh: tag.append("STR")
            target_str = ", ".join(f"{t[0]}={t[1]}" for t in targets)
            print(f"  row #{row.index}  name={row.name!r}  [{'+'.join(tag)}]  {target_str}")
            for s in strings[:5]:
                if _name_is_hot(s):
                    print(f"     HOT STR: {s!r}")


if __name__ == "__main__":
    main()
