#!/usr/bin/env python3
"""Scan CrimsonDesert.exe for difficulty-related strings (ASCII + UTF-16LE).

Writes hits with byte offsets to tools/difficulty_strings.txt and a summary
of fp32 multipliers found near the strings.
"""
from __future__ import annotations
import os, re, struct, sys

EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe"
OUT_TXT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_strings.txt"
OUT_F32 = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_fp32.txt"

TOKENS = [
    # English
    b"Difficulty", b"DifficultyLevel", b"DifficultyType", b"DifficultyMultiplier",
    b"GameDifficulty", b"BossDifficulty", b"MonsterDifficulty",
    b"EnemyScale", b"LevelScale", b"HardModeMultiplier", b"EasyModeHpRatio",
    b"GameLevel", b"GamePlayLevel", b"PlayDifficulty",
    b"PlayLevel", b"GameMode",
    b"_difficulty", b"_gameDifficulty", b"_playDifficulty",
    # Class-name shaped
    b"DifficultyInfo", b"GameDifficultyInfo", b"BossDifficultyInfo",
    b"DifficultyMultiplierInfo", b"GameLevelInfo", b"GamePlayLevelInfo",
    b"GameConfigInfo", b"GameOptionInfo",
    # Enum slot names PA games tend to use
    b"VeryEasy", b"Easy", b"Normal", b"Hard", b"VeryHard", b"Extreme", b"Hell",
    b"Beginner", b"Intermediate", b"Advanced", b"Expert", b"Master", b"Legendary",
    # Korean PA conventions
    b"NPC_NORMAL", b"NPC_ELITE", b"NPC_BOSS",
    # Specific known phrases
    b"BattleMode", b"DifficultyData", b"DifficultySetting",
]

print(f"[scan] reading {EXE}")
with open(EXE, "rb") as f:
    blob = f.read()
print(f"[scan] {len(blob):,} bytes")

# ASCII pass
hits = []   # (offset, token, encoding, context)
for tok in TOKENS:
    start = 0
    while True:
        i = blob.find(tok, start)
        if i < 0: break
        # context: 32 bytes before, 64 after, printable-ish
        a = max(0, i - 32)
        b_ = min(len(blob), i + len(tok) + 64)
        ctx = blob[a:b_]
        # printable extract
        printable = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
        hits.append((i, tok.decode(), "ascii", printable))
        start = i + 1

# UTF-16LE pass
for tok in TOKENS:
    u16 = tok.decode().encode("utf-16-le")
    start = 0
    while True:
        i = blob.find(u16, start)
        if i < 0: break
        a = max(0, i - 32)
        b_ = min(len(blob), i + len(u16) + 96)
        ctx = blob[a:b_]
        printable = ''.join(chr(c) if 32 <= c < 127 else '.' for c in ctx)
        hits.append((i, tok.decode(), "utf16", printable))
        start = i + 2

print(f"[scan] {len(hits)} raw hits")

# Filter: drop noise — keep all hits to the longer / more specific tokens,
# and for short ones (Easy/Hard/Normal) keep only those near a Difficulty hit.
diff_offsets = sorted(o for (o, t, e, c) in hits if "ifficulty" in t.lower() or "ifficulty" in t)
def near_difficulty(off, window=4096):
    # binary-search-ish; small list
    for d in diff_offsets:
        if abs(d - off) < window:
            return True
    return False

short = {"Easy", "Normal", "Hard", "VeryHard", "VeryEasy", "Extreme", "Hell",
         "Beginner", "Intermediate", "Advanced", "Expert", "Master", "Legendary"}
filtered = []
for h in hits:
    off, tok, enc, ctx = h
    if tok in short and not near_difficulty(off, 8192):
        continue
    filtered.append(h)

print(f"[scan] {len(filtered)} filtered hits")

filtered.sort(key=lambda h: (h[0], h[1]))
with open(OUT_TXT, "w", encoding="utf-8") as out:
    out.write(f"# CrimsonDesert.exe difficulty-string scan\n")
    out.write(f"# total raw hits: {len(hits)}, filtered: {len(filtered)}\n\n")
    for off, tok, enc, ctx in filtered:
        out.write(f"0x{off:08X}  [{enc:5}] {tok!r}\n    ctx: {ctx}\n\n")

print(f"[scan] wrote {OUT_TXT}")

# --- fp32 hunt around top difficulty offsets ---
# Look for clusters of 4-byte little-endian floats in plausible range
# (0.1 .. 10.0) within +/- 1024 bytes of each unique difficulty string offset.
seen = set()
fp_hits = []
for off, tok, enc, ctx in filtered:
    if "ifficulty" not in tok.lower():
        continue
    bucket = off // 1024
    if bucket in seen:
        continue
    seen.add(bucket)
    a = max(0, off - 4096)
    b_ = min(len(blob), off + 4096)
    region = blob[a:b_]
    floats = []
    for p in range(0, len(region) - 3, 4):
        v = struct.unpack_from("<f", region, p)[0]
        if 0.1 <= v <= 10.0 and not (v == 1.0):
            floats.append((a + p, v))
    if floats:
        fp_hits.append((off, tok, floats[:60]))

with open(OUT_F32, "w", encoding="utf-8") as out:
    out.write(f"# fp32 candidates (0.1 < v <= 10.0, !=1.0) near difficulty strings\n\n")
    for off, tok, floats in fp_hits:
        out.write(f"=== near 0x{off:08X} {tok!r} ===\n")
        for p, v in floats:
            out.write(f"  0x{p:08X}  {v:.4f}\n")
        out.write("\n")

print(f"[scan] wrote {OUT_F32}")
