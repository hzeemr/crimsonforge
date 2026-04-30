"""Interactively bisect a list of candidate memory addresses to
find which one is the live game value.

Reads a hits.csv produced by ``tools/scan_game_memory.py``,
ranks the candidates by heuristic "looks like a runtime variable"
(heap addresses + diverse context), then walks you through:

  1. Write the test value (default 1.0) to candidate N.
  2. You look at the game; type Y if you see the change, N if not.
  3. If N → restore the original value, advance to N+1.
  4. If Y → keep the change applied, print the winning address +
     the byte pattern to find the source on disk.

This collapses what would otherwise be 41 manual write/check
cycles into a guided session — about 1-2 minutes of work for
a 41-hit candidate list.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.scan_game_memory import (   # noqa: E402
    PROCESS_QUERY_INFORMATION, PROCESS_VM_OPERATION,
    PROCESS_VM_READ, PROCESS_VM_WRITE,
    _kernel32, find_process, read_region, write_region,
)


def _rank_candidates(rows: list[dict]) -> list[dict]:
    """Re-order hits so the most-likely live runtime variables
    come first.

    Heuristics
    ----------
    * **Heap-y addresses** (0x10000000 and above on Windows
      x64 user space) score higher than low static-data sections.
    * **Diverse context** (lots of distinct byte values around the
      hit) scores higher than uniform repetition (likely embedded
      in a static data table).
    * **Adjacent-to-1.0 / adjacent-to-known-scales** scores
      higher — those bytes (`00 00 80 3F` for 1.0) often mean
      we're inside a character-properties struct.
    """
    def score(row):
        addr = int(row["address_hex"], 16)
        ctx = bytes.fromhex(row["context_hex"])
        s = 0
        # Heap-y: above 0x10000000 = +30
        if addr > 0x10000000:
            s += 30
        # Lots of distinct bytes in context = diverse
        s += min(40, len(set(ctx)))
        # 1.0 nearby (bytes 00 00 80 3F) = likely scale struct
        if b"\x00\x00\x80\x3f" in ctx:
            s += 25
        # 0.0 nearby (00 00 00 00 4-byte run) — common in vec3
        if b"\x00\x00\x00\x00\x00\x00\x00\x00" in ctx:
            s += 5
        # Avoid uniform repetition (.pabgb-table-like data)
        # — penalize if any byte appears > 8 times in context
        from collections import Counter
        most_common_count = Counter(ctx).most_common(1)[0][1]
        if most_common_count > 8:
            s -= 20
        return -s   # higher is better, so negate for ascending sort
    return sorted(rows, key=score)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hits", default="hits.csv",
                   help="hits.csv from scan_game_memory.py")
    p.add_argument("--process", default="CrimsonDesert.exe")
    p.add_argument("--test-value", type=float, default=1.0,
                   help="Value to write while testing (default 1.0)")
    p.add_argument("--original-value", type=float, default=2.3,
                   help="Value to restore if test fails (default 2.3)")
    p.add_argument("--type", default="f32",
                   choices=("f32", "u32"),
                   help="Value type")
    p.add_argument("--start-from", type=int, default=0,
                   help="Resume from candidate index N (0-based)")
    p.add_argument("--auto-restore", action="store_true",
                   help="Automatically restore on N (default: ask)")
    args = p.parse_args(argv)

    if not os.path.isfile(args.hits):
        print(f"hits file not found: {args.hits}")
        return 1

    with open(args.hits, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"Loaded {len(rows)} candidate hits from {args.hits}")
    rows = _rank_candidates(rows)
    print(f"Ranked by 'looks-like-live-variable' heuristic.")

    pid = find_process(args.process)
    if pid is None:
        print(f"Process {args.process} not running. Launch the "
              "game and load the area, then re-run.")
        return 1
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
        False, pid,
    )
    if not handle:
        print(f"OpenProcess failed (last error {ctypes.get_last_error()})")
        return 2
    print(f"Attached to {args.process} pid={pid}")

    if args.type == "f32":
        test_bytes = struct.pack("<f", args.test_value)
        orig_bytes = struct.pack("<f", args.original_value)
        size = 4
    else:
        test_bytes = struct.pack("<I", int(args.test_value) & 0xFFFFFFFF)
        orig_bytes = struct.pack("<I", int(args.original_value) & 0xFFFFFFFF)
        size = 4

    print()
    print("=" * 70)
    print(f"Bisect session — write {args.test_value} to each candidate")
    print(f"Watch the OGRE in-game after each write.")
    print(f"  Y / yes  = ogre changed (this is the winner — stop)")
    print(f"  N / no   = no change (restore + try next)")
    print(f"  S / skip = leave as-is, move to next without restoring")
    print(f"  Q / quit = exit (does NOT restore current write)")
    print("=" * 70)
    print()

    winners = []
    try:
        for idx, row in enumerate(rows):
            if idx < args.start_from:
                continue
            addr = int(row["address_hex"], 16)
            print(f"\n[{idx + 1}/{len(rows)}] addr=0x{addr:016x}")
            ctx = bytes.fromhex(row["context_hex"])
            print(f"  context: {ctx.hex(' ')[:100]}")

            # Read current value to confirm it's still ~2.3
            current = read_region(handle, addr, size)
            if current is None:
                print("  [SKIP] address no longer readable (region freed)")
                continue
            if args.type == "f32":
                cur_v = struct.unpack("<f", current)[0]
                print(f"  current value: {cur_v}")
            else:
                cur_v = struct.unpack("<I", current)[0]
                print(f"  current u32:   0x{cur_v:08x}")

            ok = write_region(handle, addr, test_bytes)
            if not ok:
                print("  [SKIP] write failed (read-only region?)")
                continue
            print(f"  wrote {test_bytes.hex()} ({args.test_value}) — LOOK AT THE GAME")

            ans = input("  did the ogre change? [y/N/s/q] ").strip().lower()
            if ans in ("y", "yes"):
                winners.append((addr, ctx))
                print(f"  *** WINNER: 0x{addr:016x} ***")
                more = input("  Continue testing remaining hits? [y/N] ").strip().lower()
                if more not in ("y", "yes"):
                    break
            elif ans in ("s", "skip"):
                print("  (left at test value, advancing)")
            elif ans in ("q", "quit"):
                print("  (quitting; current write NOT restored)")
                break
            else:
                # Restore the original value
                if args.auto_restore or True:
                    write_region(handle, addr, orig_bytes)
                    print(f"  restored to {args.original_value}")
    finally:
        _kernel32.CloseHandle(handle)

    print()
    print("=" * 70)
    if winners:
        print(f"Found {len(winners)} winning address(es):")
        for addr, ctx in winners:
            print(f"\n  0x{addr:016x}")
            print(f"    context: {ctx.hex(' ')}")
            # The 4 bytes AT the address are the value itself.
            # The 16 bytes BEFORE + 32 AFTER (in scan_for_value) are
            # the surrounding struct. Print a search pattern that
            # excludes the 4 value bytes so we can find the source
            # file containing the SURROUNDING bytes:
            if len(ctx) >= 20:
                # scan_for_value put 16 bytes before then 4 of value then 32 after.
                # The 16 before + 32 after (skipping the 4 value bytes) is the
                # struct-fingerprint we can grep.
                fingerprint = ctx[:16] + ctx[20:]
                print(f"    fingerprint (struct minus value): {fingerprint.hex(' ')}")
                print(f"    use: python tools/find_value_candidates.py "
                      f"--value {fingerprint[:32].hex()} --type bytes")
    else:
        print("No winners found. Try:")
        print("  1. Re-scan with broader tolerance (--tolerance 0.05)")
        print("  2. Look at addresses ALSO near 1.0 / 0.5 — those are character struct")
        print("  3. Bisect by rescanning AFTER making in-game change (Cheat Engine style)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
