"""Fast streaming memory search — no dumping to disk.

Scans the entire live game memory for a list of strings / byte
patterns / fp32 values in one pass. Reports every hit with
surrounding context. Typical total run time: 30-90 seconds for
a 4-8 GB process.

Compared to ``memory_snapshot.py``:
  * Doesn't dump pages to disk (so no 2+ GB cache).
  * Doesn't hash every 4 KB block (so no SHA-256 overhead).
  * Streams region-by-region: read region → search → discard →
    move on. Memory usage stays under 256 MB.
  * Only reports HITS, not the whole memory snapshot.

Use this when you want to "where is Boss_Ogre stored in memory?"
or "find every fp32 == 2.3 right now". For multi-checkpoint diffs,
use ``memory_snapshot.py`` instead.

Examples
--------
Find the boss's data location::

    python tools/quick_memory_search.py \\
        --strings Boss_Ogre,M0001_00_Ogre,Boss_00,DesolateStoneAltar

Find every fp32 == 2.3 + show 64 bytes context per hit::

    python tools/quick_memory_search.py --float 2.3 --tolerance 0.001

Combined: find Boss_Ogre AND grab 256 bytes after each hit (the
struct that follows the name string)::

    python tools/quick_memory_search.py \\
        --strings Boss_Ogre --context-after 256
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import os
import struct
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.scan_game_memory import (
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    _kernel32, find_process, iter_readable_regions, read_region,
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--process", default="CrimsonDesert.exe")
    p.add_argument("--strings", default="",
                   help="Comma-separated list of ASCII strings to search for")
    p.add_argument("--bytes", default="",
                   help="Comma-separated list of hex byte patterns to search for")
    p.add_argument("--float", type=float, default=None,
                   help="fp32 value to search for")
    p.add_argument("--tolerance", type=float, default=0.0,
                   help="For --float: ±tolerance for matching")
    p.add_argument("--max-region-mb", type=int, default=2048,
                   help="Skip regions larger than this (default 2 GB)")
    p.add_argument("--context-before", type=int, default=32,
                   help="Bytes of context to capture before each hit")
    p.add_argument("--context-after", type=int, default=64,
                   help="Bytes of context to capture after each hit")
    p.add_argument("--max-hits-per-needle", type=int, default=200,
                   help="Cap hits per needle to keep output manageable")
    p.add_argument("--out", default="quick_search_hits.csv",
                   help="CSV file to write all hits to")
    p.add_argument("--start-addr", type=str, default="0x10000000",
                   help="Skip regions below this address (default 0x10000000 = "
                        "skip DLLs / static data, focus on heap)")
    p.add_argument("--end-addr", type=str, default="",
                   help="Skip regions above this address")
    args = p.parse_args(argv)

    # Build needle list.
    needles: list[tuple[str, bytes]] = []   # (label, bytes)
    if args.strings:
        for s in args.strings.split(","):
            s = s.strip()
            if s:
                needles.append((f"str:{s}", s.encode("utf-8")))
    if args.bytes:
        for h in args.bytes.split(","):
            h = h.strip().replace(" ", "")
            if h:
                try:
                    needles.append((f"bytes:{h}", bytes.fromhex(h)))
                except ValueError as exc:
                    print(f"Bad hex '{h}': {exc}")
                    return 1
    if args.float is not None:
        if args.tolerance > 0:
            # Build a small set of candidate fp32 patterns within tolerance.
            target = args.float
            tol = args.tolerance
            seen = set()
            for i in range(-100, 101):
                v = target + (i / 100) * tol
                seen.add(struct.pack("<f", v))
            for pat in seen:
                needles.append((f"f32:~{target}±{tol}", pat))
        else:
            needles.append((f"f32:{args.float}", struct.pack("<f", args.float)))

    if not needles:
        print("Provide at least one of: --strings, --bytes, --float")
        return 1

    print(f"Searching for {len(needles)} needle(s) in {args.process}...")

    pid = find_process(args.process)
    if pid is None:
        print(f"Process {args.process} not running.")
        return 1
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid,
    )
    if not handle:
        print(f"OpenProcess failed (last error {ctypes.get_last_error()})")
        return 2

    start_addr = int(args.start_addr, 0)
    end_addr = int(args.end_addr, 0) if args.end_addr else 0xFFFFFFFFFFFFFFFF

    # hits: list of (label, address, context_hex)
    hits: list[tuple[str, int, bytes]] = []
    hits_per_needle: dict[str, int] = {label: 0 for label, _ in needles}

    region_count = 0
    bytes_scanned = 0
    t0 = time.time()
    last_print = t0

    try:
        for addr, size in iter_readable_regions(handle):
            if size > args.max_region_mb * 1024 * 1024:
                continue
            if addr + size <= start_addr or addr >= end_addr:
                continue
            region_count += 1

            data = read_region(handle, addr, size)
            if data is None:
                continue
            bytes_scanned += len(data)

            # Search every needle in this region.
            for label, pat in needles:
                if hits_per_needle[label] >= args.max_hits_per_needle:
                    continue
                pos = 0
                while True:
                    i = data.find(pat, pos)
                    if i < 0:
                        break
                    s = max(0, i - args.context_before)
                    e = min(len(data), i + len(pat) + args.context_after)
                    hits.append((label, addr + i, data[s:e]))
                    hits_per_needle[label] += 1
                    if hits_per_needle[label] >= args.max_hits_per_needle:
                        break
                    pos = i + 1

            # Periodic progress.
            now = time.time()
            if now - last_print > 3:
                print(f"  [{region_count}] regions, "
                      f"{bytes_scanned/1024/1024:.0f} MB scanned, "
                      f"{len(hits)} hits, {now-t0:.0f}s")
                last_print = now
    finally:
        _kernel32.CloseHandle(handle)

    elapsed = time.time() - t0
    print()
    print(f"Done in {elapsed:.1f}s. {region_count} regions, "
          f"{bytes_scanned/1024/1024:.0f} MB scanned, {len(hits)} hits.")
    print()

    # Group hits by needle label.
    from collections import defaultdict
    by_label = defaultdict(list)
    for label, addr, ctx in hits:
        by_label[label].append((addr, ctx))

    # Print summary.
    print("=== Hit summary ===")
    for label, _ in needles:
        n = len(by_label.get(label, []))
        capped = "+" if n >= args.max_hits_per_needle else ""
        print(f"  {label:<60} {n:>5}{capped}")

    # Print first few hits per needle.
    print()
    print("=== Sample hits (first 5 per needle) ===")
    for label, items in by_label.items():
        if not items:
            continue
        print(f"\n--- {label} ({len(items)} hit(s)) ---")
        for addr, ctx in items[:5]:
            ascii_view = bytes(b if 32 <= b < 127 else 0x2e for b in ctx)
            print(f"  0x{addr:016x}")
            print(f"    hex:   {ctx.hex(' ')[:140]}")
            print(f"    ascii: {ascii_view.decode('latin-1', errors='replace')[:80]}")

    # Save full CSV.
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["needle", "address_hex", "address_dec", "context_hex", "context_ascii"])
        for label, addr, ctx in hits:
            ascii_view = bytes(b if 32 <= b < 127 else 0x2e for b in ctx).decode("latin-1", errors="replace")
            w.writerow([label, f"0x{addr:016x}", addr, ctx.hex(), ascii_view])
    print()
    print(f"Wrote {len(hits)} hits to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
