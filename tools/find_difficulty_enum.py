#!/usr/bin/env python3
"""Find the EnumOptionGameDifficultyOption enum slot list in the EXE.

PA's reflection writes enum types as a chunk like:
    EnumOptionGameDifficultyOption\0
    [count: u32]
    [slot0_name_len][slot0_name]\0
    [slot0_value: u32]
    ... etc
or it may register slots with a sequential prefix like '0. Easy', '1. Normal'
(we already saw '2. Hard.0. Point.....1. ShowSelected.0. Easy.3. HideAlways' which is the
graphics-quality enum, NOT difficulty).

This script:
  1. Finds every occurrence of EnumOptionGameDifficultyOption (ascii + utf16)
  2. Dumps 1024 bytes around each for inspection.
  3. Also looks for adjacent strings like 'Easy.', 'Normal.', 'Hard.', 'VeryHard.'
     (numbered prefix style PA uses in graphics-quality enums).
"""
from __future__ import annotations
import struct, re

EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe"
OUT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_enum_dump.txt"

print(f"reading {EXE}")
with open(EXE, "rb") as f:
    blob = f.read()

# Look for EnumOptionGameDifficultyOption (ascii) and EnumSelectGameDifficultyOption
TARGETS = [b"EnumOptionGameDifficultyOption",
           b"EnumSelectGameDifficultyOption",
           b"GameDifficultyOption",
           b"_gameDifficultyOption",
           b"_gameDifficultyBuffInfo",
           b"_gameDifficultyBuffLevelList",
           b"_balanceDifficultyLevel",
           b"_isApplyGameBalanceLevel",
           b"GameDifficultyInfo",
           b"BalanceLevelInfo",
           b"DifficultyBuffInfo",
           b"TrocTrChangeGameDifficultyReq",
           b"GetDifficultyOption",
           b"ConditionData_GetDifficultyOption",
           b"_reviveInPlaceHardDifficulty",
           ]

lines = []
for tok in TARGETS:
    pos = 0
    while True:
        i = blob.find(tok, pos)
        if i < 0: break
        a = max(0, i - 64)
        b_ = min(len(blob), i + 512)
        ctx = blob[a:b_]
        ascii_ = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
        lines.append(f"\n=== 0x{i:08X}  '{tok.decode()}' ===")
        lines.append(f"  ascii: {ascii_}")
        pos = i + 1

# Also: find numbered enum slot patterns
# PA serializes enums-with-display-strings as something like
# `\x01\x00\x00\x000. Normal\x01\x00\x00\x002. Hard\x00`.
# Let's look for "0. Easy" / "1. Normal" / "2. Hard" near difficulty regions.
ENUM_PATTERNS = [b"0. Easy", b"1. Normal", b"2. Hard", b"3. VeryHard",
                 b"0. Easy ", b"0. Normal", b"1. Hard",
                 b"0.Easy", b"1.Normal", b"2.Hard",
                 b"Easy\x00", b"Normal\x00", b"Hard\x00", b"VeryHard\x00"]
for p in ENUM_PATTERNS:
    pos = 0
    n = 0
    while True:
        i = blob.find(p, pos)
        if i < 0 or n > 5: break
        a = max(0, i - 64)
        b_ = min(len(blob), i + 256)
        ctx = blob[a:b_]
        ascii_ = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
        lines.append(f"\n=== 0x{i:08X}  pattern={p!r} ===")
        lines.append(f"  ascii: {ascii_}")
        pos = i + 1
        n += 1

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"wrote {OUT}")
