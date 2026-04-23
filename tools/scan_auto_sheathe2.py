"""Narrow scan: fp32 12.0 only, in small/combat-related .pabgb files."""

from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

# Filenames most likely to hold combat/weapon/idle timings.
SHORT_LIST = {
    "actionpointinfo.pabgb",
    "actionrestrictionorderinfo.pabgb",
    "aiactionattributeinfo.pabgb",
    "aieventtableinfo.pabgb",
    "aimovespeedinfo.pabgb",
    "allygroupinfo.pabgb",
    "battleinfo.pabgb",
    "buffinfo.pabgb",
    "categoryinfo.pabgb",
    "characterchange.pabgb",
    "combatinfo.pabgb",
    "conditioninfo.pabgb",
    "detectinfo.pabgb",
    "detectdetailinfo.pabgb",
    "detectreactioninfo.pabgb",
    "eventinfo.pabgb",
    "failmessageinfo.pabgb",
    "gimmickinfo.pabgb",
    "gimmickgroupinfo.pabgb",
    "playermovementinfo.pabgb",
    "skill.pabgb",
    "skillgroupinfo.pabgb",
    "statusinfo.pabgb",
    "stageinfo.pabgb",
    "transitioninfo.pabgb",
    "weaponinfo.pabgb",
}

FP32_12 = struct.pack("<f", 12.0)
_ASCII_RE = re.compile(rb"[\x20-\x7E]{4,}")


def _context(data: bytes, pos: int, window: int = 160) -> list[str]:
    lo = max(0, pos - window)
    hi = min(len(data), pos + window)
    return [m.group(0).decode("ascii", "replace")
            for m in _ASCII_RE.finditer(data, lo, hi)]


def main() -> None:
    vfs = VfsManager(GAME)
    print("Groups:", len(vfs.list_package_groups()))

    # Show every .pabgb whose name contains combat/weapon tokens, even
    # if not in SHORT_LIST, so we don't miss less-obvious names.
    tokens = ("weapon", "combat", "battle", "action", "skill", "idle",
              "sheath", "unready", "move", "sprint", "walk", "run",
              "stamina", "breath")

    interesting = []
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        for e in pamt.file_entries:
            base = os.path.basename(e.path).lower()
            if not base.endswith(".pabgb"):
                continue
            if base in SHORT_LIST or any(t in base for t in tokens):
                interesting.append((g, e))

    print(f"Interesting .pabgb files: {len(interesting)}")
    print()

    for g, e in interesting:
        try:
            data = vfs.read_entry_data(e)
        except Exception:
            continue
        name = os.path.basename(e.path)

        # fp32 12.0 hits only
        offsets = []
        start = 0
        while True:
            at = data.find(FP32_12, start)
            if at < 0:
                break
            offsets.append(at)
            start = at + 1

        if not offsets:
            continue

        print(f"=== {name}  ({len(data):,} bytes)  fp32=12.0 hits: {len(offsets)} ===")
        for off in offsets[:15]:   # first 15 hits per file
            ctx = _context(data, off)
            pretty = " | ".join(ctx[:8])
            print(f"  @{off:#06x}  {pretty[:200]}")
        if len(offsets) > 15:
            print(f"  ... and {len(offsets) - 15} more")
        print()


if __name__ == "__main__":
    main()
