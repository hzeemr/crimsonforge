"""Scan the game exe for sheathe/unready strings and nearby
fp32 12.0 constants.

Pearl Abyss engines regularly hardcode timing values in the C++
game logic. The symbol / string table in the exe is the most
reliable place to find the mechanism name, even when the value
is in a separate .rdata section.
"""

from __future__ import annotations

import os
import re
import struct

EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe"

TOKENS = (
    b"Sheathe", b"sheathe", b"SHEATHE",
    b"Unready", b"UnReady", b"UNREADY", b"UnReadyWeapon",
    b"AutoSheathe", b"AutoStand", b"AutoUnready",
    b"CombatExit", b"BattleExit", b"ExitBattle", b"ExitCombat",
    b"WeaponDraw", b"DrawWeapon",
    b"WeaponReady", b"ReadyWeapon", b"ReadyToIdle",
    b"IdleTime", b"IdleDuration", b"IdleToNonBattle",
    b"WeaponMode", b"ReadyMode", b"NonBattle", b"NonBattleMode",
    b"ResetWeapon", b"PutAwayWeapon",
    b"Holster", b"Holstered",
    b"LeaveBattle", b"LeaveCombat",
    b"CombatTimeout", b"BattleTimeout",
)

FP32_12 = struct.pack("<f", 12.0)


def main() -> None:
    print(f"Scanning {EXE} ...")
    with open(EXE, "rb") as f:
        data = f.read()
    print(f"  size: {len(data):,} bytes")

    # --- Step 1: find every token string ---
    token_hits = []
    for tok in TOKENS:
        p = 0
        while True:
            at = data.find(tok, p)
            if at < 0:
                break
            token_hits.append((tok, at))
            p = at + 1

    print()
    print(f"Token hits: {len(token_hits)}")
    # Group by token for a summary count
    by_token: dict[bytes, int] = {}
    for tok, _ in token_hits:
        by_token[tok] = by_token.get(tok, 0) + 1
    for tok, cnt in sorted(by_token.items(), key=lambda x: -x[1]):
        print(f"  {tok.decode():30s}  {cnt}")

    # --- Step 2: for the most promising tokens, dump nearby ASCII
    # context and any nearby fp32 candidate values ---
    ascii_re = re.compile(rb"[\x20-\x7E]{4,}")
    priority = (b"Sheathe", b"sheathe", b"Unready", b"UnReady",
                b"AutoSheathe", b"AutoStand", b"NonBattle",
                b"IdleToNonBattle", b"WeaponMode", b"ReadyMode",
                b"ExitBattle", b"ExitCombat", b"CombatTimeout",
                b"BattleTimeout")

    print()
    print("High-priority token contexts:")
    shown = 0
    for tok, off in token_hits:
        if tok not in priority:
            continue
        lo = max(0, off - 64)
        hi = min(len(data), off + 200)
        # Surrounding strings (4+ ASCII runs)
        runs = [m.group(0).decode("ascii", "replace")
                for m in ascii_re.finditer(data, lo, hi)]
        nearby = " | ".join(runs)[:250]
        print(f"  @{off:#010x}  tok={tok.decode()!r}")
        print(f"     {nearby}")
        shown += 1
        if shown >= 30:
            print("  (truncated)")
            break

    # --- Step 3: find fp32 12.0 hits that are WITHIN 4 KB of a token ---
    fp_hits = []
    p = 0
    while True:
        at = data.find(FP32_12, p)
        if at < 0:
            break
        fp_hits.append(at)
        p = at + 1
    print()
    print(f"fp32 12.0 total hits: {len(fp_hits)}")

    # Which fp32 hits sit near a token hit?
    near = []
    token_offsets = sorted({off for _, off in token_hits if _ in priority})
    for fp_off in fp_hits:
        for tok_off in token_offsets:
            if abs(fp_off - tok_off) < 8 * 1024:
                # Find what token it is
                matching_tok = next(
                    (t for t, o in token_hits if o == tok_off), b"?"
                )
                near.append((fp_off, tok_off, matching_tok))
                break
    print(f"fp32 12.0 hits within 8 KB of a priority token: {len(near)}")
    for fp_off, tok_off, tok in near[:30]:
        dist = fp_off - tok_off
        print(f"  fp32@{fp_off:#010x}  tok={tok.decode()!r}@{tok_off:#010x}  (delta={dist:+d})")


if __name__ == "__main__":
    main()
