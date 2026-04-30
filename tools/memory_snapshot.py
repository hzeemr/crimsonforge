"""Three-phase memory snapshot + diff for live game RE.

Captures the entire readable memory of the running Crimson Desert
process at multiple checkpoints, then diffs them to identify
exactly which pages changed when an in-game event happens (e.g.
the boss appears, the fight starts, a phase transition).

Workflow
--------
The standard RE recipe for "what controls X":

  1. Reach the in-game state BEFORE X happens (e.g. just entered
     the area but boss not visible yet)::

       python tools/memory_snapshot.py snapshot --tag before

  2. Trigger X (e.g. walk forward until boss intro plays). When X
     is on screen, snapshot again::

       python tools/memory_snapshot.py snapshot --tag during

  3. (Optional) During the active fight::

       python tools/memory_snapshot.py snapshot --tag fight

  4. Diff the snapshots to find what NEWLY appeared::

       python tools/memory_snapshot.py diff --a before --b during

     This prints every memory page (4 KB region) that changed
     between the two checkpoints — these are the pages the engine
     allocated / wrote to in order to bring the boss into the
     world. The boss's runtime state, its scale, its HP, its
     bone matrices — they ALL live in these changed pages.

  5. Search inside the diff for likely candidates::

       python tools/memory_snapshot.py search --tag during \\
           --string "Boss_Ogre" --or-bytes "33331340"

Storage model
-------------
Each snapshot is a directory on disk. Inside:

  manifest.json   — list of every region (address, size, protect, hash)
  page_hashes.bin — SHA-256 of every 4 KB page across every region
  pages/          — only the FIRST snapshot dumps full bytes for every
                    page. Subsequent snapshots dump ONLY pages whose
                    hash differs from the previous snapshot's. This
                    keeps disk usage down (typical: 50-500 MB per
                    extra snapshot vs 4-20 GB for naive full-dump).

Limits
------
Skips regions larger than 256 MB (huge texture / audio caches that
rarely change). Skips guard pages and NOACCESS pages. Same-user
process attach — no admin, no DLL injection.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Reuse Win32 plumbing from scan_game_memory.
from tools.scan_game_memory import (   # noqa: E402
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    _kernel32, find_process, iter_readable_regions, read_region,
)

PAGE_SIZE = 4096
SNAPSHOT_ROOT = Path.home() / ".crimsonforge" / "memory_snapshots"


# ── Snapshot ─────────────────────────────────────────────────────

def take_snapshot(handle: int, out_dir: Path,
                  max_region_mb: int = 256,
                  manifest_save_every: int = 25,
                  progress_every: int = 50) -> dict:
    """Walk every readable region in the process, hash every 4 KB
    page, store full bytes for each page.

    Survives interruption by writing the manifest to disk every
    ``manifest_save_every`` regions. If the game closes mid-scan
    or the user hits Ctrl+C, the partial manifest still reflects
    everything captured so far + the diff/search commands work
    against the partial snapshot.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    manifest = {
        "captured_unix": time.time(),
        "regions": [],
        "page_size": PAGE_SIZE,
        "max_region_mb": max_region_mb,
        "complete": False,   # flipped to True only on clean finish
    }
    manifest_path = out_dir / "manifest.json"

    def _save_manifest(complete: bool = False) -> None:
        manifest["complete"] = complete
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_pages = 0
    total_bytes = 0
    skipped_large = 0
    region_count = 0
    t0 = time.time()

    # Seed an empty manifest so even a 1-second crash leaves a
    # discoverable directory.
    _save_manifest(complete=False)

    try:
        for addr, size in iter_readable_regions(handle):
            region_count += 1
            if size > max_region_mb * 1024 * 1024:
                skipped_large += 1
                if region_count % progress_every == 0:
                    print(f"  [{region_count}] skipped large region "
                          f"@0x{addr:016x} ({size/1024/1024:.0f} MB)")
                continue
            data = read_region(handle, addr, size)
            if data is None:
                continue

            page_hashes = []
            for off in range(0, len(data), PAGE_SIZE):
                page = data[off : off + PAGE_SIZE]
                h = hashlib.sha256(page).hexdigest()
                page_hashes.append(h)
                page_path = pages_dir / f"{addr + off:016x}_{h[:16]}.bin"
                page_path.write_bytes(page)
                total_pages += 1
                total_bytes += len(page)

            manifest["regions"].append({
                "address": addr,
                "size": size,
                "page_hashes": page_hashes,
            })

            # Periodic checkpoint + progress.
            if region_count % manifest_save_every == 0:
                manifest["total_pages"] = total_pages
                manifest["total_bytes"] = total_bytes
                manifest["skipped_large_regions"] = skipped_large
                _save_manifest(complete=False)

            if region_count % progress_every == 0:
                elapsed = time.time() - t0
                print(f"  [{region_count}] regions, "
                      f"{total_pages:,} pages, "
                      f"{total_bytes/1024/1024:.0f} MB, "
                      f"latest @0x{addr:016x}, {elapsed:.0f}s elapsed")

        # Clean finish.
        manifest["total_pages"] = total_pages
        manifest["total_bytes"] = total_bytes
        manifest["skipped_large_regions"] = skipped_large
        _save_manifest(complete=True)
    except KeyboardInterrupt:
        print("  [interrupted] saving partial manifest...")
        manifest["total_pages"] = total_pages
        manifest["total_bytes"] = total_bytes
        manifest["skipped_large_regions"] = skipped_large
        _save_manifest(complete=False)
        raise
    return manifest


def take_diff_snapshot(handle: int, out_dir: Path,
                      baseline_dir: Path, max_region_mb: int = 256) -> dict:
    """Like ``take_snapshot`` but only dumps pages whose hash
    differs from the baseline snapshot. Saves disk space.
    """
    baseline = json.loads((baseline_dir / "manifest.json").read_text())
    baseline_pages: dict[int, str] = {}   # absolute_addr → hash
    for region in baseline["regions"]:
        addr = region["address"]
        for i, h in enumerate(region["page_hashes"]):
            baseline_pages[addr + i * PAGE_SIZE] = h

    out_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    manifest = {
        "captured_unix": time.time(),
        "baseline_dir": str(baseline_dir),
        "regions": [],
        "page_size": PAGE_SIZE,
        "max_region_mb": max_region_mb,
    }
    new_pages = 0
    changed_pages = 0
    same_pages = 0

    for addr, size in iter_readable_regions(handle):
        if size > max_region_mb * 1024 * 1024:
            continue
        data = read_region(handle, addr, size)
        if data is None:
            continue

        page_hashes = []
        for off in range(0, len(data), PAGE_SIZE):
            page = data[off : off + PAGE_SIZE]
            h = hashlib.sha256(page).hexdigest()
            page_hashes.append(h)
            abs_addr = addr + off
            base_h = baseline_pages.get(abs_addr)
            if base_h is None:
                # Newly-allocated region the baseline didn't see.
                new_pages += 1
                page_path = pages_dir / f"{abs_addr:016x}_{h[:16]}.bin"
                page_path.write_bytes(page)
            elif base_h != h:
                changed_pages += 1
                page_path = pages_dir / f"{abs_addr:016x}_{h[:16]}.bin"
                page_path.write_bytes(page)
            else:
                same_pages += 1

        manifest["regions"].append({
            "address": addr,
            "size": size,
            "page_hashes": page_hashes,
        })

    manifest["pages_new"] = new_pages
    manifest["pages_changed"] = changed_pages
    manifest["pages_same"] = same_pages

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    return manifest


# ── Diff (offline analysis of two existing snapshots) ───────────

def diff_snapshots(a_dir: Path, b_dir: Path) -> dict:
    """Compute the set of pages that differ between two snapshots
    without re-attaching to the game.
    """
    a = json.loads((a_dir / "manifest.json").read_text())
    b = json.loads((b_dir / "manifest.json").read_text())

    a_pages = {}
    for r in a["regions"]:
        addr = r["address"]
        for i, h in enumerate(r["page_hashes"]):
            a_pages[addr + i * PAGE_SIZE] = h
    b_pages = {}
    for r in b["regions"]:
        addr = r["address"]
        for i, h in enumerate(r["page_hashes"]):
            b_pages[addr + i * PAGE_SIZE] = h

    only_in_a = sorted(a_pages.keys() - b_pages.keys())
    only_in_b = sorted(b_pages.keys() - a_pages.keys())
    differ = sorted(addr for addr in a_pages.keys() & b_pages.keys()
                    if a_pages[addr] != b_pages[addr])

    return {
        "only_in_a_count": len(only_in_a),
        "only_in_b_count": len(only_in_b),
        "differ_count": len(differ),
        "only_in_a": only_in_a[:50],   # print sample
        "only_in_b": only_in_b[:50],
        "differ": differ[:50],
        "all_only_in_b": only_in_b,    # full list for downstream tools
        "all_differ": differ,
    }


# ── Search inside a snapshot ────────────────────────────────────

def search_snapshot(
    snapshot_dir: Path,
    needle_bytes: bytes | None = None,
    needle_string: str | None = None,
    only_changed_from: Path | None = None,
) -> list[tuple[int, int]]:
    """Search every dumped page in ``snapshot_dir`` for a byte or
    string match. Returns ``[(absolute_address, offset_in_page)]``
    pairs.

    If ``only_changed_from`` is given, restrict the search to pages
    that changed between that baseline and this snapshot.
    """
    if needle_bytes is None and needle_string is None:
        raise ValueError("provide --bytes or --string")
    if needle_bytes is None:
        needle_bytes = needle_string.encode("utf-8")

    pages_dir = snapshot_dir / "pages"
    if not pages_dir.is_dir():
        return []

    # Restrict to changed pages if requested.
    only_addrs: set[int] | None = None
    if only_changed_from:
        d = diff_snapshots(only_changed_from, snapshot_dir)
        only_addrs = set(d["all_only_in_b"]) | set(d["all_differ"])

    hits = []
    for page_path in pages_dir.iterdir():
        # Filename format: <16-hex-addr>_<hashprefix>.bin
        try:
            addr_str = page_path.stem.split("_", 1)[0]
            addr = int(addr_str, 16)
        except (ValueError, IndexError):
            continue
        if only_addrs is not None and addr not in only_addrs:
            continue
        try:
            data = page_path.read_bytes()
        except OSError:
            continue
        pos = 0
        while True:
            i = data.find(needle_bytes, pos)
            if i < 0:
                break
            hits.append((addr + i, i))
            pos = i + 1
    return hits


# ── CLI ────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="Capture a memory snapshot")
    snap.add_argument("--process", default="CrimsonDesert.exe")
    snap.add_argument("--tag", required=True,
                      help="Short tag (e.g. 'before', 'during', 'fight')")
    snap.add_argument("--baseline-tag", default=None,
                      help="If given, only dump pages that differ from this prior snapshot (saves disk)")
    snap.add_argument("--max-region-mb", type=int, default=2048,
                      help="Skip regions larger than this many MB (default 2048 — big enough for most game heaps)")

    repair = sub.add_parser("repair-manifest",
                            help="Rebuild manifest.json from page filenames "
                                 "(use after an interrupted snapshot)")
    repair.add_argument("--tag", required=True)

    diff = sub.add_parser("diff", help="Diff two snapshots")
    diff.add_argument("--a", required=True, help="Baseline tag (e.g. 'before')")
    diff.add_argument("--b", required=True, help="Newer tag (e.g. 'during')")

    search = sub.add_parser("search", help="Search inside a snapshot")
    search.add_argument("--tag", required=True)
    search.add_argument("--bytes", default=None,
                        help="Hex bytes to search for")
    search.add_argument("--string", default=None,
                        help="ASCII string to search for")
    search.add_argument("--only-changed-from", default=None,
                        help="Restrict to pages that changed since this baseline tag")
    search.add_argument("--max-hits", type=int, default=200)

    listcmd = sub.add_parser("list", help="List all snapshots")

    args = p.parse_args(argv)

    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)

    if args.cmd == "repair-manifest":
        snap_dir = SNAPSHOT_ROOT / args.tag
        pages_dir = snap_dir / "pages"
        if not pages_dir.is_dir():
            print(f"snapshot '{args.tag}' has no pages/ — nothing to repair")
            return 1
        # Group page files by contiguous addresses to reconstruct regions.
        page_files = sorted(pages_dir.iterdir())
        pages_by_addr = []
        for fp in page_files:
            try:
                addr_str, hash_str = fp.stem.split("_", 1)
                addr = int(addr_str, 16)
            except (ValueError, IndexError):
                continue
            pages_by_addr.append((addr, hash_str))
        pages_by_addr.sort()
        # Coalesce contiguous addresses (step = PAGE_SIZE) into regions.
        regions = []
        cur_start = None
        cur_hashes: list[str] = []
        cur_next_addr = 0
        for addr, h in pages_by_addr:
            if cur_start is None or addr != cur_next_addr:
                if cur_start is not None:
                    regions.append({
                        "address": cur_start,
                        "size": len(cur_hashes) * PAGE_SIZE,
                        "page_hashes": cur_hashes,
                    })
                cur_start = addr
                cur_hashes = [h]
                cur_next_addr = addr + PAGE_SIZE
            else:
                cur_hashes.append(h)
                cur_next_addr += PAGE_SIZE
        if cur_start is not None:
            regions.append({
                "address": cur_start,
                "size": len(cur_hashes) * PAGE_SIZE,
                "page_hashes": cur_hashes,
            })
        manifest = {
            "captured_unix": time.time(),
            "regions": regions,
            "page_size": PAGE_SIZE,
            "total_pages": len(pages_by_addr),
            "total_bytes": len(pages_by_addr) * PAGE_SIZE,
            "complete": False,
            "repaired": True,
        }
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )
        print(f"Repaired manifest for '{args.tag}': "
              f"{len(pages_by_addr):,} pages in {len(regions)} contiguous region(s)")
        return 0

    if args.cmd == "list":
        for d in sorted(SNAPSHOT_ROOT.iterdir()):
            if not d.is_dir(): continue
            mf = d / "manifest.json"
            if not mf.is_file(): continue
            try:
                m = json.loads(mf.read_text())
                ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(m.get("captured_unix", 0)))
                print(f"  {d.name:<25} {ts}  pages={m.get('total_pages', 0)}")
            except Exception:
                pass
        return 0

    if args.cmd == "snapshot":
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
        out_dir = SNAPSHOT_ROOT / args.tag
        if out_dir.is_dir():
            print(f"Tag '{args.tag}' already exists at {out_dir}.")
            print("Delete it or pick a new tag.")
            return 3
        try:
            print(f"Snapshotting {args.process} (pid={pid}) → {out_dir}")
            t0 = time.time()
            if args.baseline_tag:
                baseline_dir = SNAPSHOT_ROOT / args.baseline_tag
                if not baseline_dir.is_dir():
                    print(f"Baseline tag '{args.baseline_tag}' not found.")
                    return 4
                m = take_diff_snapshot(handle, out_dir, baseline_dir,
                                       max_region_mb=args.max_region_mb)
                print(f"Done in {time.time()-t0:.1f}s")
                print(f"  pages new       : {m['pages_new']:,}")
                print(f"  pages changed   : {m['pages_changed']:,}")
                print(f"  pages unchanged : {m['pages_same']:,}")
            else:
                m = take_snapshot(handle, out_dir,
                                  max_region_mb=args.max_region_mb)
                print(f"Done in {time.time()-t0:.1f}s")
                print(f"  total pages: {m['total_pages']:,}")
                print(f"  total bytes: {m['total_bytes']:,} "
                      f"({m['total_bytes']/1024/1024:.1f} MB)")
                print(f"  regions: {len(m['regions'])}")
                print(f"  skipped (>{args.max_region_mb}MB): {m['skipped_large_regions']}")
        finally:
            _kernel32.CloseHandle(handle)
        return 0

    if args.cmd == "diff":
        a_dir = SNAPSHOT_ROOT / args.a
        b_dir = SNAPSHOT_ROOT / args.b
        if not (a_dir.is_dir() and b_dir.is_dir()):
            print(f"snapshot tags must exist: {args.a}, {args.b}")
            return 1
        d = diff_snapshots(a_dir, b_dir)
        print(f"Diff '{args.a}' → '{args.b}':")
        print(f"  pages only in '{args.a}' (freed by then): {d['only_in_a_count']:,}")
        print(f"  pages only in '{args.b}' (NEWLY allocated): {d['only_in_b_count']:,}")
        print(f"  pages present in both but DIFFERENT      : {d['differ_count']:,}")
        print()
        print("First 20 NEWLY-allocated page addresses (most likely contain boss data):")
        for addr in d["all_only_in_b"][:20]:
            print(f"  0x{addr:016x}")
        print()
        print("First 20 CHANGED page addresses (existing struct mutated):")
        for addr in d["all_differ"][:20]:
            print(f"  0x{addr:016x}")
        return 0

    if args.cmd == "search":
        snap_dir = SNAPSHOT_ROOT / args.tag
        if not snap_dir.is_dir():
            print(f"snapshot '{args.tag}' not found")
            return 1
        nb = bytes.fromhex(args.bytes) if args.bytes else None
        ns = args.string
        if not nb and not ns:
            print("provide --bytes or --string")
            return 1
        only_from = (
            SNAPSHOT_ROOT / args.only_changed_from
            if args.only_changed_from else None
        )
        hits = search_snapshot(snap_dir, nb, ns, only_from)
        print(f"Found {len(hits)} hit(s)")
        for addr, off in hits[:args.max_hits]:
            print(f"  0x{addr:016x}  (in-page offset {off})")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
