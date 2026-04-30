"""Offline scan: find every byte location in any game file that
contains a target fp32 value AND mentions a target string within
N bytes (e.g. find every "2.3" within 16 KB of "Boss_Ogre").

Companion to ``tools/scan_game_memory.py``. The memory scanner
finds where the engine stores the value at runtime; this script
finds where the value LIVES on disk so you know which file to
edit. Run both, cross-reference, and you've located the actual
source-of-truth field for any visible game property.

Example
-------
    python tools/find_value_candidates.py \\
        --value 2.3 --type f32 \\
        --near "Boss_Ogre" --window 16384

Prints every (file, offset, surrounding-bytes) where the byte
pattern of fp32 2.3 appears within ``window`` bytes of any
occurrence of "Boss_Ogre" in the same file.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager   # noqa: E402


def _hex_dump(b: bytes) -> str:
    h = " ".join(f"{x:02x}" for x in b)
    a = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
    return f"{h}  |  {a}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--game", default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
                   help="Game install root (containing packages/)")
    p.add_argument("--value", required=True, help="Target value, e.g. '2.3'")
    p.add_argument("--type", choices=("f32", "u32", "i32", "bytes"), default="f32")
    p.add_argument("--tolerance", type=float, default=0.0,
                   help="For f32 only: ±tolerance for value match")
    p.add_argument("--near", default=None,
                   help="ASCII string that must appear in the same file within --window bytes")
    p.add_argument("--window", type=int, default=16384,
                   help="Max distance (bytes) between --near hit and --value hit")
    p.add_argument("--include-ext", default=".pabgb,.pabc,.pabv,.hkx,.xml,.app_xml,.prefabdata_xml,.pac_xml",
                   help="Comma-separated extensions to scan (default: most-likely scale-bearing)")
    p.add_argument("--max-hits-per-file", type=int, default=30)
    args = p.parse_args(argv)

    if args.type == "f32":
        target_f = float(args.value)
        if args.tolerance > 0:
            # Pre-build a small set of candidate byte patterns within
            # tolerance — cheaper than a sliding window match for large
            # corpora.
            candidates = set()
            steps = 50
            for i in range(-steps, steps + 1):
                v = target_f + (i / steps) * args.tolerance
                candidates.add(struct.pack("<f", v))
            needles = list(candidates)
            print(f"Tolerance ±{args.tolerance}: {len(needles)} candidate byte patterns.")
        else:
            needles = [struct.pack("<f", target_f)]
    elif args.type == "u32":
        needles = [struct.pack("<I", int(args.value, 0) & 0xFFFFFFFF)]
    elif args.type == "i32":
        needles = [struct.pack("<i", int(args.value, 0))]
    else:
        needles = [bytes.fromhex(args.value.replace(" ", ""))]

    near_bytes = args.near.encode("utf-8") if args.near else None
    extensions = tuple(e.strip().lower() for e in args.include_ext.split(",") if e.strip())

    print(f"Scanning game at: {args.game}")
    vfs = VfsManager(args.game)

    # Walk every entry in every PAMT
    total_files = 0
    total_hits = 0
    matched_files = 0
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception:
            continue
        for entry in pamt.file_entries:
            ext = os.path.splitext(entry.path.lower())[1]
            if ext not in extensions:
                continue
            try:
                data = vfs.read_entry_data(entry)
            except Exception:
                continue
            total_files += 1

            # If --near requested, only proceed if file contains it.
            if near_bytes:
                near_idx = data.find(near_bytes)
                if near_idx < 0:
                    continue
            else:
                near_idx = -1

            # Find every value match in the file.
            file_hits = []
            for needle in needles:
                pos = 0
                while True:
                    j = data.find(needle, pos)
                    if j < 0:
                        break
                    # If we have a near anchor, require proximity.
                    if near_idx >= 0:
                        # The closest occurrence of 'near' to j.
                        # Cheap version: just check this one near_idx
                        # occurrence; could be smarter if needed.
                        if abs(j - near_idx) > args.window:
                            # Try looking for OTHER near occurrences.
                            # Find next 'near' after j-window:
                            scan_from = max(0, j - args.window)
                            next_near = data.find(near_bytes, scan_from, j + args.window + len(near_bytes))
                            if next_near < 0:
                                pos = j + 1
                                continue
                    file_hits.append(j)
                    if len(file_hits) >= args.max_hits_per_file:
                        break
                    pos = j + 1
                if len(file_hits) >= args.max_hits_per_file:
                    break

            if file_hits:
                matched_files += 1
                total_hits += len(file_hits)
                print(f"\n=== {entry.path} ({len(file_hits)} hit(s)) ===")
                for j in file_hits[:8]:
                    s = max(0, j - 16)
                    e = min(len(data), j + 32)
                    print(f"  @0x{j:08x}  {_hex_dump(data[s:e])}")
                if len(file_hits) > 8:
                    print(f"  ... +{len(file_hits) - 8} more in this file")

    print()
    print(f"Done. {matched_files} files contained the value (out of {total_files} scanned). "
          f"Total hits: {total_hits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
