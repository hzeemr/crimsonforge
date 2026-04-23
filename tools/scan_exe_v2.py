"""Broader exe scan — look for every timeout/sheathe/disarm token
and list every float constant in any reasonable timer range."""

from __future__ import annotations

import re
import struct

EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe"


def main() -> None:
    with open(EXE, "rb") as f:
        data = f.read()

    # --- Token scan ---
    # Expanded set. We look for substring, case-sensitive, because the
    # exe is full of UpperCamelCase engine symbols.
    tokens = [
        b"_TimeOut", b"_Timeout", b"_TIMEOUT",
        b"Sheathe", b"sheath", b"Sheath",
        b"Unready", b"UnReady",
        b"Disarm", b"DisArm",
        b"Unequip", b"UnEquip",
        b"Holster",
        b"AutoSheathe", b"AutoSheath",
        b"AutoStand", b"AutoIdle",
        b"AutoWeapon", b"ResetWeapon",
        b"NonBattle", b"NoBattle",
        b"BattleExit", b"CombatExit",
        b"ExitBattle", b"ExitCombat",
        b"LeaveBattle", b"LeaveCombat",
        b"WeaponMode", b"ReadyMode",
        b"WeaponDraw", b"DrawWeapon", b"DrawOut",
        b"ReadyWeapon",
        b"IdleTimer",
        b"BattleTimer", b"CombatTimer",
        b"BattleTime", b"CombatTime",
        b"RequestRide", b"ForceIdle",
        b"_duration", b"_Duration",
        b"Duration_", b"TimeOut_",
    ]

    counts: dict[bytes, int] = {}
    for tok in tokens:
        c = data.count(tok)
        if c > 0:
            counts[tok] = c

    print("Token hits (> 0):")
    for tok, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {tok.decode():25s}  {c}")

    # --- Context dump for interesting narrow tokens ---
    ascii_re = re.compile(rb"[\x20-\x7E]{4,}")
    print()
    print("Context for sheathe/disarm/unready tokens:")
    highlight = (b"Sheath", b"Sheathe", b"sheath",
                 b"Unready", b"UnReady", b"Disarm",
                 b"Unequip", b"Holster", b"AutoStand",
                 b"ExitBattle", b"ExitCombat", b"LeaveBattle",
                 b"CombatTimer", b"BattleTimer")
    seen_contexts: set[int] = set()
    for tok in highlight:
        p = 0
        while True:
            at = data.find(tok, p)
            if at < 0:
                break
            # Round down to 256-byte window to dedupe nearby hits.
            win_key = at // 256
            if win_key not in seen_contexts:
                seen_contexts.add(win_key)
                lo = max(0, at - 120)
                hi = min(len(data), at + 240)
                runs = [m.group(0).decode("ascii", "replace")
                        for m in ascii_re.finditer(data, lo, hi)]
                context = " | ".join(runs)
                print(f"  @{at:#010x}  tok={tok.decode()!r}")
                print(f"     {context[:320]}")
            p = at + 1


if __name__ == "__main__":
    main()
