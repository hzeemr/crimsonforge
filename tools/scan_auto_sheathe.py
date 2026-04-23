"""One-off scan: find the 12-second weapon auto-sheathe timer.

We scan every .pabgb in the game for byte patterns that match:
  - fp32 12.0 little-endian  (0x41400000 -> 00 00 40 41)
  - uint32 12                (0x0C000000 -> 0C 00 00 00)
  - common 60fps/30fps/20fps tick equivalents:
      600  (20 tps)  fp32 + int32
      360  (30 tps)  fp32 + int32
      720  (60 tps)  fp32 + int32
      12000 (ms)     fp32 + int32

For each hit we emit a short context dump (the ASCII run around the
byte position) so we can eyeball which field each hit belongs to.
"""

from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"


def _candidate_patterns() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for val in [12.0, 360.0, 600.0, 720.0, 12000.0]:
        out.append((f"fp32 {val}", struct.pack("<f", val)))
    for val in [12, 360, 600, 720, 12000]:
        out.append((f"int32 {val}", struct.pack("<I", val)))
        out.append((f"int16 {val}", struct.pack("<H", val) + b""))
    return out


_ASCII_RE = re.compile(rb"[\x20-\x7E]{3,}")


def _nearest_ascii(data: bytes, pos: int, window: int = 96) -> list[str]:
    """Return short ASCII runs near `pos` (within +/- window bytes)."""
    lo = max(0, pos - window)
    hi = min(len(data), pos + window)
    runs: list[str] = []
    for m in _ASCII_RE.finditer(data, lo, hi):
        runs.append(m.group(0).decode("ascii", "replace"))
    return runs


def main() -> None:
    vfs = VfsManager(GAME)
    groups = vfs.list_package_groups()

    pabgbs: list[tuple[str, object]] = []
    for g in groups:
        try:
            pamt = vfs.load_pamt(g)
            for e in pamt.file_entries:
                if e.path.lower().endswith(".pabgb"):
                    pabgbs.append((g, e))
        except Exception:
            pass

    patterns = _candidate_patterns()
    # Interesting field keywords — if any of these appear near a hit,
    # print the hit with extra emphasis.
    hot_keywords = (
        "sheath", "sheathe", "unready", "combat", "battle", "weapon",
        "idle", "timer", "timeout", "duration", "draw", "exit", "state",
    )

    per_file_hits: dict[str, int] = {}

    print("=" * 70)
    print(f"Scanning {len(pabgbs)} .pabgb files across {len(groups)} groups")
    print("=" * 70)

    for g, e in pabgbs:
        try:
            data = vfs.read_entry_data(e)
        except Exception as ex:
            continue

        name = os.path.basename(e.path)
        hits: dict[str, list[int]] = {}
        for label, needle in patterns:
            offsets = []
            start = 0
            while True:
                at = data.find(needle, start)
                if at < 0:
                    break
                offsets.append(at)
                start = at + 1
            if offsets:
                hits[label] = offsets

        if not hits:
            continue

        # Count ONLY the 12-related patterns for per-file prioritisation.
        primary_count = 0
        for lbl, offs in hits.items():
            if "12" in lbl or "720" in lbl or "360" in lbl:
                primary_count += len(offs)

        per_file_hits[name] = primary_count

        # Only print when at least one PRIMARY pattern matched.
        if primary_count == 0:
            continue

        print()
        print(f"--- {name}  ({len(data):,} bytes)  primary hits: {primary_count}")
        for lbl, offs in sorted(hits.items()):
            if "12" in lbl or "720" in lbl or "360" in lbl:
                print(f"    {lbl}: {len(offs)} hit(s)")

        # For the first 3 12.0f hits, dump nearby ASCII context.
        fp12_hits = hits.get("fp32 12.0", [])
        for off in fp12_hits[:3]:
            runs = _nearest_ascii(data, off)
            hot = [r for r in runs
                   if any(k in r.lower() for k in hot_keywords)]
            flag = " [HOT]" if hot else ""
            print(f"    @ offset {off:#x}{flag}")
            for r in runs[:6]:
                print(f"        \"{r}\"")

    print()
    print("=" * 70)
    print("Top files by primary-pattern hit count:")
    for name, cnt in sorted(per_file_hits.items(), key=lambda x: -x[1])[:15]:
        if cnt > 0:
            print(f"    {cnt:4d}  {name}")


if __name__ == "__main__":
    main()
