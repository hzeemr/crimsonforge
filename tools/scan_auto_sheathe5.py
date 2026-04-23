"""Scan every STRING field across all .pabgb tables for tokens
that could name the auto-sheathe mechanic. Print the row where
each matching string was found so we can identify which table
holds the timer."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pabgb_parser import parse_pabgb
from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

# Possible names for the auto-sheathe mechanic — English, camelCase,
# and Pearl Abyss internal styles we've seen across their games.
TOKENS = (
    "sheath",        # "AutoSheathe", "SheatheWeapon", etc.
    "unready",       # "UnReady", "UnReadyTime"
    "unequip",
    "holster",
    "resheath",
    "weapontime",
    "weaponduration",
    "combatexit",
    "battleexit",
    "exitbattle",
    "exitcombat",
    "idletime",
    "idleduration",
    "battleidle",
    "combatidle",
    "readytime",
    "readyduration",
    "weaponready",
    "autoweapon",
    "draw",          # "DrawWeapon" but will have false positives
    "fighterstand",
    "warriorstand",
    "autostand",
    "standtime",
    "battleready",
    "combatready",
    "nonbattle",     # "NonBattle" is a real Pearl Abyss token
    "readyweapon",
)


def _scan_strings(s: str, tokens: tuple) -> list[str]:
    low = s.lower()
    matches = [t for t in tokens if t in low]
    return matches


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

    print(f"Scanning {len(pabgbs)} .pabgb files for token strings...")

    grand_total = 0
    for g, e in pabgbs:
        try:
            data = vfs.read_entry_data(e)
            table = parse_pabgb(data, os.path.basename(e.path))
        except Exception:
            continue

        file_hits: list[tuple] = []
        for row in table.rows:
            for f in row.fields:
                if f.kind != "str":
                    continue
                s = str(f.value)
                matches = _scan_strings(s, TOKENS)
                if matches:
                    file_hits.append((row.index, row.name, s, matches))

        if not file_hits:
            continue

        name = os.path.basename(e.path)
        print()
        print(f"=== {name}  ({len(file_hits)} hits) ===")
        for idx, rname, s, matches in file_hits[:30]:
            tok = "/".join(matches)
            row_tag = rname if rname else "?"
            # Find numeric fields on the same row
            row_obj = next((r for r in table.rows if r.index == idx), None)
            nums = []
            if row_obj is not None:
                for ff in row_obj.fields:
                    if ff.kind == "f32" and 0.1 < ff.value < 10000:
                        nums.append(f"f32={ff.value:.2f}")
                    elif ff.kind in ("i32", "u32") and 0 < ff.value < 100000:
                        nums.append(f"{ff.kind}={ff.value}")
            print(f"  row #{idx:5d}  [{tok}]  row={row_tag!r}")
            print(f"     val: {s!r}")
            if nums:
                print(f"     nums: {' '.join(nums[:10])}")
        if len(file_hits) > 30:
            print(f"  ... and {len(file_hits) - 30} more")
        grand_total += len(file_hits)

    print()
    print(f"=== Grand total token hits: {grand_total} ===")


if __name__ == "__main__":
    main()
