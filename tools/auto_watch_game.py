"""Background watcher — auto-logs game memory state with no manual intervention.

Workflow
--------
1. You run this ONE TIME (in a terminal you can leave open):

       python -X utf8 tools/auto_watch_game.py

2. You open Crimson Desert. Play normally. Go to the boss. Fight.
   Close the game whenever you want.

3. The watcher detects the game starting + closing. Every 5 seconds
   while the game runs, it scans memory for boss-related strings
   and value patterns and logs everything to disk.

4. After the game closes, you tell me to read the logs and I'll
   give the full picture of what was in memory + when.

Output
------
Logs go to:
    ~/.crimsonforge/auto_watch_logs/<session-timestamp>/
        events.jsonl          — one line per significant event
        scan_<scanN>.json     — per-scan detailed hits
        summary.txt           — human-readable timeline (written on game exit)

Memory footprint
----------------
The watcher reads memory in 16 MB chunks, scans for needles in
each chunk, then discards the chunk. Peak RAM use: ~50 MB. No
files dumped to disk per page. Suitable to run in the background
indefinitely.

Press Ctrl+C in the terminal to stop the watcher. The current
session log is still saved — you can ask me to read it any time.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.scan_game_memory import (
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    _kernel32, find_process, iter_readable_regions, read_region,
)


# ── Watch targets ────────────────────────────────────────────────
#
# Default scan list. Strings the engine almost certainly keeps in
# RAM while the boss is loaded, plus value patterns that change
# meaningfully between "no boss" and "boss visible" states.

DEFAULT_STRINGS = (
    "Boss_Ogre",
    "Boss_Ogre_55515",
    "M0001_00_Ogre",
    "CD_M0001_00_Ogre",
    "cd_m0001_00_ogre",
    "Boss_00",
    "Trigger_00",
    "Trigger_02",
    "BossPhase",
    "set_noDie",
    "set_keepAggroPlayer",
    "DesolateStoneAltar",
    "Knowledge_Faction_Ogre",
    "Legendary_Ogre_Ring",
    # Other bosses for cross-reference
    "Boss_Wild_Imp_Boss_55511",
    "Boss_Hexe_Marie",
    # Player skeleton + bones (for sanity)
    "phm_01.pab",
    "Bip01",
    "B_face_com",
)

DEFAULT_FLOATS = (
    (2.3, 0.001),    # Ogre original scale field 155 candidate
    (1.0, 0.0001),   # standard scale baseline
    (81.0, 0.01),    # Ogre HP candidate
    (32.25, 0.01),   # Ogre damage candidate
    (213.0, 0.01),   # Field 174 candidate
    (5.0, 0.001),    # HorizontalRange (climb sockets)
)


# ── Scanner ──────────────────────────────────────────────────────

def scan_once(handle: int, scan_index: int, out_dir: Path,
              max_region_mb: int = 1024,
              start_addr: int = 0x10000000) -> dict:
    """One full pass — scan all readable regions for every needle.

    Returns a summary dict; also writes a JSON file per scan.
    """
    string_needles = [(s, s.encode("utf-8")) for s in DEFAULT_STRINGS]
    float_needles = []
    for target, tol in DEFAULT_FLOATS:
        if tol > 0:
            patterns = set()
            for i in range(-50, 51):
                v = target + (i / 50) * tol
                patterns.add(struct.pack("<f", v))
            float_needles.append(
                (f"f32:{target}±{tol}", list(patterns))
            )
        else:
            float_needles.append(
                (f"f32:{target}", [struct.pack("<f", target)])
            )

    hits: dict[str, list[tuple[int, str]]] = defaultdict(list)
    bytes_scanned = 0
    region_count = 0
    t0 = time.time()

    for addr, size in iter_readable_regions(handle):
        if size > max_region_mb * 1024 * 1024:
            continue
        if addr + size <= start_addr:
            continue
        region_count += 1
        data = read_region(handle, addr, size)
        if data is None:
            continue
        bytes_scanned += len(data)

        for label, pat in string_needles:
            pos = 0
            while True:
                i = data.find(pat, pos)
                if i < 0:
                    break
                ctx_s = max(0, i - 16)
                ctx_e = min(len(data), i + len(pat) + 64)
                ctx_hex = data[ctx_s:ctx_e].hex()
                hits[f"str:{label}"].append((addr + i, ctx_hex))
                pos = i + 1
                if len(hits[f"str:{label}"]) >= 100:
                    break

        for label, patterns in float_needles:
            for pat in patterns:
                pos = 0
                while True:
                    i = data.find(pat, pos)
                    if i < 0:
                        break
                    ctx_s = max(0, i - 16)
                    ctx_e = min(len(data), i + 4 + 32)
                    ctx_hex = data[ctx_s:ctx_e].hex()
                    hits[label].append((addr + i, ctx_hex))
                    pos = i + 1
                    if len(hits[label]) >= 200:
                        break

    elapsed = time.time() - t0
    summary = {
        "scan_index": scan_index,
        "captured_unix": time.time(),
        "elapsed_seconds": round(elapsed, 2),
        "regions_scanned": region_count,
        "bytes_scanned": bytes_scanned,
        "hits_by_needle": {k: len(v) for k, v in sorted(hits.items())},
        "first_hits_per_needle": {
            k: [{"addr_hex": f"0x{a:016x}", "ctx_hex": c}
                for a, c in v[:200]]   # save up to 200 per needle for bisecting
            for k, v in sorted(hits.items())
        },
    }
    out_path = out_dir / f"scan_{scan_index:04d}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ── Event log ────────────────────────────────────────────────────

class EventLog:
    """Append-only JSONL log of session events."""

    def __init__(self, path: Path):
        self._path = path
        self._fp = path.open("a", encoding="utf-8", buffering=1)

    def event(self, kind: str, **fields: Any) -> None:
        record = {"ts": time.time(), "kind": kind, **fields}
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


# ── Summary writer ──────────────────────────────────────────────

def write_summary(session_dir: Path) -> None:
    """Read every scan_*.json + events.jsonl and write summary.txt."""
    scans = sorted(session_dir.glob("scan_*.json"))
    events_path = session_dir / "events.jsonl"
    out = session_dir / "summary.txt"

    lines: list[str] = []
    lines.append(f"=== Crimson Desert auto-watch session summary ===")
    lines.append(f"session dir: {session_dir}")
    lines.append(f"scans:       {len(scans)}")
    lines.append("")

    if events_path.is_file():
        lines.append("--- Events timeline ---")
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
                kind = e.get("kind", "?")
                rest = ", ".join(f"{k}={v}" for k, v in e.items()
                                 if k not in ("ts", "kind"))
                lines.append(f"  {ts}  {kind}  {rest}")
            except Exception:
                pass
        lines.append("")

    if not scans:
        lines.append("(no memory scans were captured)")
        out.write_text("\n".join(lines), encoding="utf-8")
        return

    # Per-needle timeline: how did the hit count change scan-to-scan?
    timelines: dict[str, list[int]] = defaultdict(lambda: [0] * len(scans))
    needle_first_hit: dict[str, dict] = {}
    for i, scan_path in enumerate(scans):
        try:
            scan = json.loads(scan_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for needle, count in scan.get("hits_by_needle", {}).items():
            timelines[needle][i] = count
            if count and needle not in needle_first_hit:
                # capture sample addresses on first appearance
                needle_first_hit[needle] = {
                    "first_scan": i,
                    "samples": scan.get("first_hits_per_needle", {})
                                   .get(needle, [])[:5],
                }

    lines.append("--- Per-needle timeline (hits per scan) ---")
    lines.append(f"{'needle':<55} {'scans →':<15}")
    for needle in sorted(timelines):
        sparkline = "".join(_spark(c) for c in timelines[needle])
        max_c = max(timelines[needle])
        lines.append(f"  {needle:<53} {sparkline}  max={max_c}")
    lines.append("")

    # First-appearance addresses
    lines.append("--- Where each needle FIRST appeared (likely boss data starts here) ---")
    for needle, info in sorted(needle_first_hit.items()):
        lines.append(f"\n  {needle}  (first seen at scan {info['first_scan']})")
        for sample in info["samples"]:
            lines.append(f"    {sample.get('addr_hex')}  ctx={sample.get('ctx_hex', '')[:80]}")

    out.write_text("\n".join(lines), encoding="utf-8")


_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def _spark(value: int, max_val: int = 100) -> str:
    if value <= 0:
        return _SPARK_CHARS[0]
    idx = min(8, int(value / max_val * 8) + 1)
    return _SPARK_CHARS[idx]


# ── Main loop ────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--process", default="CrimsonDesert.exe")
    p.add_argument("--scan-interval", type=int, default=8,
                   help="Seconds between scans while game is running (default 8)")
    p.add_argument("--poll-interval", type=int, default=2,
                   help="Seconds between game-running checks while idle (default 2)")
    p.add_argument("--max-region-mb", type=int, default=512,
                   help="Skip regions larger than this MB (default 512)")
    p.add_argument("--start-addr", type=str, default="0x140000000",
                   help="Skip regions below this address. Default 0x140000000 "
                        "focuses on the runtime character/world heap (where "
                        "Boss_00, BossPhase, Bip01 actually live). Scans now "
                        "complete in 10-30s. Pass 0x60000000 to also include "
                        "the .pabgb static-data heap.")
    args = p.parse_args(argv)

    log_root = Path.home() / ".crimsonforge" / "auto_watch_logs"
    log_root.mkdir(parents=True, exist_ok=True)
    print(f"Auto-watcher started. Logs: {log_root}")
    print(f"Polling for '{args.process}' every {args.poll_interval}s. Ctrl+C to stop.")
    print()

    start_addr = int(args.start_addr, 0)
    session_dir: Path | None = None
    log: EventLog | None = None
    handle = 0
    scan_index = 0
    current_pid = None

    def _close_session(exit_kind: str = "manual") -> None:
        nonlocal handle, log, session_dir, scan_index, current_pid
        if handle:
            try: _kernel32.CloseHandle(handle)
            except Exception: pass
            handle = 0
        if log:
            log.event("session_end", reason=exit_kind, total_scans=scan_index)
            log.close()
            log = None
        if session_dir is not None:
            try: write_summary(session_dir)
            except Exception as exc:
                print(f"  summary failed: {exc}")
            print(f"Session ended. Summary: {session_dir / 'summary.txt'}")
        session_dir = None
        scan_index = 0
        current_pid = None

    try:
        while True:
            pid = find_process(args.process)
            if pid is None:
                if session_dir is not None:
                    print(f"Game closed (was pid={current_pid}). Finalising session.")
                    _close_session(exit_kind="game_closed")
                time.sleep(args.poll_interval)
                continue

            # Game is running.
            if pid != current_pid:
                # New session — open handle + log.
                ts = time.strftime("%Y%m%d_%H%M%S")
                session_dir = log_root / ts
                session_dir.mkdir(parents=True, exist_ok=True)
                log = EventLog(session_dir / "events.jsonl")
                log.event("session_start", pid=pid, process=args.process)
                handle = _kernel32.OpenProcess(
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid,
                )
                if not handle:
                    log.event("open_process_failed",
                              error=ctypes.get_last_error())
                    print(f"  OpenProcess failed (last error {ctypes.get_last_error()}); "
                          f"will retry on next poll")
                    _close_session(exit_kind="open_failed")
                    time.sleep(args.poll_interval)
                    continue
                print(f"Game started: pid={pid}. Logging to: {session_dir}")
                current_pid = pid
                scan_index = 0

            # Run one scan.
            try:
                summary = scan_once(handle, scan_index, session_dir,
                                    max_region_mb=args.max_region_mb,
                                    start_addr=start_addr)
                # Print compact line.
                interesting = {k: v for k, v in summary["hits_by_needle"].items() if v > 0}
                line = (f"  scan #{scan_index:03d}  "
                        f"{summary['regions_scanned']} regions, "
                        f"{summary['bytes_scanned']/1024/1024:.0f} MB, "
                        f"{summary['elapsed_seconds']:.1f}s")
                if interesting:
                    top = ", ".join(
                        f"{k.split(':')[-1]}={v}"
                        for k, v in sorted(interesting.items(),
                                           key=lambda kv: -kv[1])[:5]
                    )
                    line += f"  hits: {top}"
                print(line)
                log.event("scan", index=scan_index,
                          regions=summary["regions_scanned"],
                          mb=round(summary["bytes_scanned"]/1024/1024, 1),
                          interesting_hits=interesting)
                scan_index += 1
            except Exception as exc:
                # Most likely the process exited mid-scan.
                print(f"  scan #{scan_index} failed: {exc}")
                log.event("scan_failed", index=scan_index, error=str(exc))
                _close_session(exit_kind="scan_failed")
                time.sleep(args.poll_interval)
                continue

            time.sleep(args.scan_interval)
    except KeyboardInterrupt:
        print("\nCtrl+C — closing session and exiting.")
        _close_session(exit_kind="ctrl_c")
        return 0


if __name__ == "__main__":
    sys.exit(main())
