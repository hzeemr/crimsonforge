"""One-shot auto-bisect for the ogre's live scale value.

Workflow
--------
1. Open Crimson Desert. Walk to the ogre. Make him visible.
2. In a terminal, run::

       python tools/auto_bisect_ogre_scale.py

3. The tool waits for the game process, then:
   * Scans the heap (0x140000000+) for the byte pattern
     ``33 33 13 40 33 33 13 40`` (two adjacent fp32 ``2.3`` values
     — the strongest signature we've found for the boss scale
     struct).
   * Filters out hits that look like material-shader constants
     (those live around ``0x000003c0xxxxxxx`` and contain ASCII
     strings like ``texture``, ``material``, ``noiss``).
   * Walks each remaining candidate one at a time:
       - Writes ``1.0 1.0`` over the two 2.3 values
       - Prompts you to look at the game
       - Y → records winner, restores nothing (so you can keep
              the change for screenshots)
       - N → restores ``2.3 2.3``, advances to next
       - S → leaves write applied, advances (for testing combos)
       - Q → quits cleanly

The whole loop typically takes 1-3 minutes total.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import struct
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.scan_game_memory import (   # noqa: E402
    PROCESS_QUERY_INFORMATION, PROCESS_VM_OPERATION,
    PROCESS_VM_READ, PROCESS_VM_WRITE,
    _kernel32, find_process, iter_readable_regions,
    read_region, write_region,
)


# Two consecutive fp32 ``2.3`` values, little-endian.
DOUBLE_2_3 = struct.pack("<ff", 2.3, 2.3)


def _wait_for_game(process: str, timeout: int = 300) -> int | None:
    """Poll for the game process. Returns its PID or None on timeout."""
    print(f"Waiting for {process} to be running…")
    t0 = time.time()
    while time.time() - t0 < timeout:
        pid = find_process(process)
        if pid is not None:
            print(f"  found {process} pid={pid}")
            return pid
        time.sleep(2)
    return None


def _scan_for_pattern(handle: int, pattern: bytes,
                      start_addr: int = 0x140000000,
                      max_region_mb: int = 1024) -> list[tuple[int, bytes]]:
    """Scan all readable regions starting at ``start_addr`` for
    ``pattern``. Returns ``[(absolute_address, 64-byte context)]``.
    """
    hits: list[tuple[int, bytes]] = []
    for addr, size in iter_readable_regions(handle):
        if addr + size <= start_addr:
            continue
        if size > max_region_mb * 1024 * 1024:
            continue
        data = read_region(handle, addr, size)
        if data is None:
            continue
        pos = 0
        while True:
            i = data.find(pattern, pos)
            if i < 0:
                break
            ctx_s = max(0, i - 16)
            ctx_e = min(len(data), i + len(pattern) + 48)
            hits.append((addr + i, data[ctx_s:ctx_e]))
            pos = i + 1
    return hits


def _is_material_buffer(ctx: bytes) -> bool:
    """Heuristic — material parameter blocks contain ASCII strings
    like 'texture', 'material', 'noiss', '.dds', '.material'.
    These are NOT character scale, they're shader constants for
    repeated-tile UVs etc. Skip them.
    """
    bad = (b"texture", b"material", b"noiss", b".dds", b".material",
           b"mat.", b"_mat", b"shader")
    return any(b in ctx for b in bad)


def _bisect(handle: int, candidates: list[tuple[int, bytes]],
            test_value: float = 1.0,
            original_value: float = 2.3) -> list[tuple[int, bytes]]:
    """Walk each candidate, writing ``test_value`` over the
    matched 8 bytes. Prompts for user confirmation each step.
    Returns the list of confirmed winners.
    """
    test_bytes = struct.pack("<ff", test_value, test_value)
    orig_bytes = struct.pack("<ff", original_value, original_value)
    winners: list[tuple[int, bytes]] = []

    print()
    print("=" * 70)
    print(f"Bisect — write [{test_value}, {test_value}] to each candidate.")
    print("Look at the OGRE in-game after each write.")
    print("  Y / yes  = ogre changed (winner — kept; advances)")
    print("  N / no   = no change (restored to 2.3; advances)")
    print("  S / skip = leave as-is (no restore, advances)")
    print("  Q / quit = exit (current write NOT restored)")
    print("=" * 70)

    for idx, (addr, ctx) in enumerate(candidates):
        ctx_ascii = bytes(c if 32 <= c < 127 else 0x2e for c in ctx)
        print(f"\n[{idx + 1}/{len(candidates)}] addr=0x{addr:016x}")
        print(f"  context: {ctx_ascii.decode('latin-1', errors='replace')[:80]}")
        # Verify the value is still 2.3.
        current = read_region(handle, addr, 8)
        if current != orig_bytes:
            cur_str = current.hex() if current else "(unreadable)"
            print(f"  [SKIP] expected 2.3,2.3 bytes but got {cur_str}")
            continue
        if not write_region(handle, addr, test_bytes):
            print(f"  [SKIP] write failed (read-only?)")
            continue
        print(f"  wrote {test_value},{test_value} — LOOK AT THE GAME")
        ans = input("  did the ogre change? [y/N/s/q] ").strip().lower()
        if ans in ("y", "yes"):
            winners.append((addr, ctx))
            print(f"  *** WINNER: 0x{addr:016x} — change kept ***")
        elif ans in ("s", "skip"):
            print("  (kept at test value, advancing)")
        elif ans in ("q", "quit"):
            print("  (quit; current write NOT restored)")
            return winners
        else:
            write_region(handle, addr, orig_bytes)
            print("  restored to 2.3,2.3")

    return winners


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--process", default="CrimsonDesert.exe")
    p.add_argument("--start-addr", type=str, default="0x140000000")
    p.add_argument("--test-value", type=float, default=1.0)
    p.add_argument("--max-candidates", type=int, default=20,
                   help="Stop after testing this many heap-range candidates")
    args = p.parse_args(argv)

    pid = _wait_for_game(args.process)
    if pid is None:
        print("Timeout — game not detected.")
        return 1

    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
        False, pid,
    )
    if not handle:
        print(f"OpenProcess failed (last error {ctypes.get_last_error()})")
        return 2

    try:
        start_addr = int(args.start_addr, 0)
        print(f"Scanning heap from 0x{start_addr:x}+ for double-2.3 pattern…")
        t0 = time.time()
        all_hits = _scan_for_pattern(handle, DOUBLE_2_3, start_addr=start_addr)
        elapsed = time.time() - t0
        print(f"  scan took {elapsed:.1f}s, {len(all_hits)} raw hits")

        # Filter material buffer hits.
        candidates = [(a, c) for a, c in all_hits if not _is_material_buffer(c)]
        print(f"  after skipping material buffers: {len(candidates)} candidates")

        if not candidates:
            print("No candidates left after filtering. Try lowering --start-addr "
                  "or open the boss area in-game first.")
            return 0

        # Sort by address (lowest first — character heap usually before world data).
        candidates.sort(key=lambda x: x[0])
        if len(candidates) > args.max_candidates:
            print(f"  capping at {args.max_candidates} (use --max-candidates "
                  f"to test more)")
            candidates = candidates[:args.max_candidates]

        winners = _bisect(handle, candidates, test_value=args.test_value)

        print()
        print("=" * 70)
        if winners:
            print(f"FOUND {len(winners)} WINNING ADDRESS(ES):")
            for addr, ctx in winners:
                ctx_ascii = bytes(c if 32 <= c < 127 else 0x2e for c in ctx)
                print(f"\n  0x{addr:016x}")
                print(f"    context: {ctx_ascii.decode('latin-1', errors='replace')}")
                print(f"    To restore later:")
                print(f"      python -X utf8 tools/scan_game_memory.py "
                      f"--write-address 0x{addr:016x} "
                      f"--write-value 2.3 --type f32")
        else:
            print("No address caused a visible change.")
            print("Possible reasons:")
            print("  - The scale is computed at draw time from another source")
            print("  - The write happens but the engine overwrites it on next frame")
            print("  - The boss has multiple scale fields that need writing together")
            print()
            print("Try:")
            print("  --max-candidates 50  (test more)")
            print("  --start-addr 0x60000000  (also include .pabgb static heap)")
            print("  --test-value 0.1  (more dramatic change)")
        return 0
    finally:
        _kernel32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())
