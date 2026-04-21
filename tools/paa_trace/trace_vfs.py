"""Diagnostic walk over every .paa file in a package group.

Run via:

    python -m tools.paa_trace.trace_vfs \
        "C:/Program Files (x86)/Steam/steamapps/common/Crimson Desert" \
        --group 0009 --limit 500

Output:

    Groups     : 0009
    Scanned    : 500
    Parsed OK  : 432 (86.4%)
    Zero-track : 68  (13.6%)

    Flag distribution (low byte):
      0x0f : 187  untagged, full animation
      0x9f :  89  untagged, pose-only
      0xca :  54  link -> phm_01.pab
      ...

The trace is the first thing to run before batch-export. It
pinpoints which flag variants still produce zero tracks so we can
extend the parser instead of silently emitting broken FBXs.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from collections import Counter
from pathlib import Path

# Allow running as a plain script from the repo root ("python tools/...").
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.animation_parser import parse_paa  # noqa: E402
from core.vfs_manager import VfsManager  # noqa: E402


# ----------------------------- CLI --------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_root", help="game install root (has packages/, bin64/, ...)")
    ap.add_argument("--group", default="0009", help="package group containing character/*.paa (default 0009)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (0 = scan all)")
    ap.add_argument("--json", action="store_true", help="emit the final summary as JSON instead of text")
    return ap.parse_args()


# ----------------------------- main -------------------------------

def main() -> int:
    args = _parse_args()

    # Cp1252 console chokes on the Korean metadata tags logged by the
    # parser. Force UTF-8 so piping to a file or a modern terminal
    # doesn't raise UnicodeEncodeError every few lines.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    vfs = VfsManager(args.game_root)
    vfs.load_papgt()
    pamt = vfs.load_pamt(args.group)

    paa_entries = sorted(
        [e for e in pamt.file_entries if e.path.lower().endswith(".paa")],
        key=lambda e: e.path,
    )
    if args.limit and args.limit < len(paa_entries):
        # Even spread across the corpus beats picking the first N
        # (which are all from the same author/scene/character).
        step = max(1, len(paa_entries) // args.limit)
        paa_entries = paa_entries[::step][:args.limit]

    print(f"# PAA trace over group {args.group}  ({len(paa_entries)} files)")

    flag_counter: Counter[str] = Counter()
    variant_counter: Counter[str] = Counter()
    zero_flag_counter: Counter[str] = Counter()
    parse_errors: list[tuple[str, str]] = []
    link_targets: Counter[str] = Counter()
    track_buckets: Counter[str] = Counter()
    frame_buckets: Counter[str] = Counter()
    scanned = parsed_ok = zero_track = 0

    for e in paa_entries:
        try:
            data = vfs.read_entry_data(e)
        except Exception as ex:
            parse_errors.append((e.path, f"read: {ex}"))
            continue
        scanned += 1
        if len(data) < 0x14:
            parse_errors.append((e.path, "too small"))
            continue
        flags = struct.unpack_from("<I", data, 0x10)[0]
        flag_counter[f"0x{flags & 0xFF:02x}"] += 1
        try:
            anim = parse_paa(data, e.path)
        except Exception as ex:
            parse_errors.append((e.path, f"parse: {type(ex).__name__} {ex}"))
            continue
        parsed_ok += 1
        variant_counter[anim.format_variant] += 1
        if anim.is_link and anim.link_target:
            # Normalise: strip the leading '%', extract the skeleton filename.
            tgt = anim.link_target.lstrip("%")
            base = os.path.basename(tgt)
            link_targets[base] += 1
        if anim.bone_count == 0:
            zero_track += 1
            zero_flag_counter[f"0x{flags & 0xFF:02x}"] += 1
            track_buckets["0"] += 1
        elif anim.bone_count <= 5:
            track_buckets["1-5"] += 1
        elif anim.bone_count <= 25:
            track_buckets["6-25"] += 1
        elif anim.bone_count <= 75:
            track_buckets["26-75"] += 1
        else:
            track_buckets["76+"] += 1
        if anim.frame_count == 0:
            frame_buckets["0"] += 1
        elif anim.frame_count == 1:
            frame_buckets["1 (static)"] += 1
        elif anim.frame_count <= 30:
            frame_buckets["2-30"] += 1
        elif anim.frame_count <= 120:
            frame_buckets["31-120"] += 1
        else:
            frame_buckets["121+"] += 1

    # --- summary ---
    print()
    print(f"Scanned  : {scanned}")
    print(f"Parsed OK: {parsed_ok} ({(parsed_ok / max(1, scanned)) * 100:5.1f}%)")
    print(f"Non-zero : {scanned - zero_track} ({((scanned - zero_track) / max(1, scanned)) * 100:5.1f}%)")
    print(f"Zero     : {zero_track} ({(zero_track / max(1, scanned)) * 100:5.1f}%)")
    if parse_errors:
        print(f"Errors   : {len(parse_errors)}")

    print()
    print("Flag distribution (low byte):")
    for k, v in flag_counter.most_common():
        print(f"  {k}: {v}")

    print()
    print("Variant distribution:")
    for k, v in variant_counter.most_common():
        print(f"  {k}: {v}")

    if zero_flag_counter:
        print()
        print("Zero-track files by flag:")
        for k, v in zero_flag_counter.most_common():
            print(f"  {k}: {v}")

    if link_targets:
        print()
        print("Top link targets (skeleton PABs referenced):")
        for k, v in link_targets.most_common(10):
            print(f"  {k}: {v}")

    if track_buckets:
        print()
        print("Track count distribution:")
        for k in ("0", "1-5", "6-25", "26-75", "76+"):
            if k in track_buckets:
                print(f"  {k:6s}: {track_buckets[k]}")

    if frame_buckets:
        print()
        print("Frame count distribution:")
        for k in ("0", "1 (static)", "2-30", "31-120", "121+"):
            if k in frame_buckets:
                print(f"  {k:12s}: {frame_buckets[k]}")

    if parse_errors:
        print()
        print("First 10 parse errors:")
        for path, msg in parse_errors[:10]:
            print(f"  {path}  ->  {msg}")

    return 0 if scanned else 1


if __name__ == "__main__":
    sys.exit(main())
