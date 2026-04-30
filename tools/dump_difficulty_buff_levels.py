#!/usr/bin/env python3
"""Decode the level-list inside BuffLevel_Difficulty_Boss.

The buff row is structured as:  [base header] [level0] [level1] [level2] ...
We hunt the level-block boundaries by walking the raw bytes and looking for
the repeated pattern signature.
"""
from __future__ import annotations
import os, sys, struct
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
OUT = r"C:\Users\hzeem\Desktop\crimsonforge\tools\difficulty_buff_levels.txt"

TARGET_NAMES = {
    "BuffLevel_Difficulty",
    "BuffLevel_Difficulty_Boss",
    "BuffLevel_Difficulty_PC",
    "BuffLevel_AIDifficulty",
}

def find_table(vfs, base):
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        b = h = None
        for e in pamt.file_entries:
            p = e.path.lower()
            if p == base + ".pabgb": b = e
            elif p == base + ".pabgh": h = e
        if b and h:
            return g, b, h
    return None, None, None

def main():
    vfs = VfsManager(GAME)
    g, b, h = find_table(vfs, "gamedata/buffinfo")
    data = vfs.read_entry_data(b)
    head = vfs.read_entry_data(h)
    tbl = parse_pabgb(data, head, "buffinfo.pabgb")

    with open(OUT, "w", encoding="utf-8") as out:
        for r in tbl.rows:
            if r.name not in TARGET_NAMES:
                continue
            out.write(f"\n{'=' * 78}\n")
            out.write(f"{r.name}  hash=0x{r.row_hash:08X}  size={r.data_size}b\n")
            out.write(f"{'=' * 78}\n")
            raw = r.raw

            # Hex dump split into 16-byte rows w/ ASCII
            for i in range(0, len(raw), 16):
                chunk = raw[i:i+16]
                hexs = " ".join(f"{b_:02x}" for b_ in chunk)
                ascii_ = "".join(chr(b_) if 32 <= b_ < 127 else "." for b_ in chunk)
                out.write(f"  {i:04x}  {hexs:<48}  {ascii_}\n")

            # All fp32 values in the row that fall in plausible multiplier ranges
            out.write(f"\n  fp32 candidates (|v| in 0.05..200.0):\n")
            for off in range(0, len(raw) - 3, 1):
                v = struct.unpack_from("<f", raw, off)[0]
                if 0.05 <= abs(v) <= 200.0 and v != 1.0:
                    # also try u32 — skip if it looks like a small int
                    u = struct.unpack_from("<I", raw, off)[0]
                    if 1 <= u <= 65535:
                        continue
                    out.write(f"    off={off:4d}  fp32={v:>10.4f}    u32=0x{u:08X}\n")

            # All u32 values that look like buff-info hashes (0x000F4xxx range)
            out.write(f"\n  u32 in 0x000F4000..0x000F5FFF (likely buff/status hash refs):\n")
            for off in range(0, len(raw) - 3, 4):
                u = struct.unpack_from("<I", raw, off)[0]
                if 0x000F4000 <= u <= 0x000F5FFF:
                    out.write(f"    off={off:4d}  u32=0x{u:08X}\n")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
