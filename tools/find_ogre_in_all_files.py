#!/usr/bin/env python3
"""URGENT companion to find_ogre_in_all_pabgb.py.

Scans every NON-pabgb file in the VFS for either of:
   - 'Boss_Ogre_55515' as ASCII text (UTF-8 / UTF-16-LE)
   - the bytes 0x2A,0x49,0x0F,0x00 (LE) for hash 0x000F492A

Targets specifically: .paseqc, .pastage, .pasm (state machine), .pac_xml,
.app_xml, .prefabdata_xml, .xml, .pa* of any kind. Skips media to keep it
fast (.dds .ogg .wav .pkfx .havok .pgg .ptex .ptxc .palod .pamesh).

For every hit, dumps:
  tools/ogre_file_hits.txt    -- ranked report
  tools/ogre_file_hits/<safe_path>  -- full processed file bytes for the
                                      hit file so we can inspect inline
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager

NEEDLES_BYTES: list[bytes] = [
    b"Boss_Ogre_55515",                 # plain ASCII
    "Boss_Ogre_55515".encode("utf-16-le"),
    b"\x2A\x49\x0F\x00",                # 0x000F492A LE
]

# Only scan extensions that plausibly contain spawn config / scripts /
# state machines. The full game is huge; this keeps the scan focused.
SCAN_EXTS = {
    ".paseqc", ".pastage", ".pasm", ".pasms", ".paseq",
    ".pac", ".pac_xml", ".app", ".app_xml",
    ".prefabdata", ".prefabdata_xml",
    ".xml", ".paloc", ".thtml", ".html", ".css",
    ".pami", ".uianiminit", ".mi", ".txt",
    ".paac",                              # action chart
    ".paef", ".paaf", ".pafx",            # animation/action data
    ".paeb",                              # event blueprint?
    ".pahnk",                             # havok script
    ".pavfx",
    ".paachart",
    ".paspawnchart",
    ".pacharttable",
    ".paspawn",
    ".paspawnpoint",
    ".paspawnsetting",
    ".paspawnerinfo",
    ".paaiblueprint",
    ".paaibehavior",
    ".pabt",                              # behavior tree?
    ".pasf",
    ".pakw",                              # keyword
    ".pacomp",
    ".paquest",
    ".paregion",
    ".pacond",
    ".paskill",
    ".paphase",
    ".panav",
    ".pasplinepath",
    ".paedge",
    ".panode",
    ".paseqcomp",
    ".pastagecomp",
}

# Skip these noisy/binary blobs entirely.
SKIP_EXTS = {
    ".dds", ".ogg", ".wav", ".pkfx", ".havok", ".hkx",
    ".pgg", ".ptex", ".ptxc", ".palod", ".pamesh",
    ".paskel", ".paani", ".paphys", ".panav2",
    ".pat", ".pasnd", ".paaudio", ".paspl",
    ".bnk", ".pck", ".bik", ".webm", ".mp4",
    ".pabgb", ".pabgh",   # handled by sibling script
    ".pak", ".paz",
}


def safe_name(path: str) -> str:
    return path.replace("/", "__").replace("\\", "__").replace(":", "_")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--game",
        default=r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
    )
    p.add_argument("--out", default=str(Path(__file__).parent))
    p.add_argument(
        "--all",
        action="store_true",
        help="Scan every file regardless of extension (slow; only use if "
             "the targeted scan finds nothing).",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Stop after N files for a quick first pass (0 = no limit).",
    )
    args = p.parse_args()

    packages_path = os.path.join(args.game, "packages")
    if not os.path.isdir(packages_path):
        packages_path = args.game

    print(f"Loading VFS from {packages_path}")
    vfs = VfsManager(packages_path)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "ogre_file_hits.txt"
    dump_dir = out_dir / "ogre_file_hits"
    dump_dir.mkdir(parents=True, exist_ok=True)

    txt_lines: list[str] = []
    txt_lines.append("=" * 78)
    txt_lines.append(
        "Boss_Ogre_55515 / hash 0x000F492A — non-pabgb file matches"
    )
    txt_lines.append("=" * 78)

    n_scanned = 0
    n_hits = 0

    # Walk every PAMT in every group.
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except Exception as e:
            txt_lines.append(f"[!] PAMT load fail {group}: {e!r}")
            continue

        for entry in pamt.file_entries:
            ext = os.path.splitext(entry.path.lower())[1]
            if ext in SKIP_EXTS:
                continue
            if not args.all and ext not in SCAN_EXTS:
                continue

            n_scanned += 1
            if args.max_files and n_scanned > args.max_files:
                break

            try:
                data = vfs.read_entry_data(entry)
            except Exception as e:
                txt_lines.append(
                    f"[!] read fail {entry.path} ({group}): {e!r}"
                )
                continue

            hit_for_this_file = []
            for needle in NEEDLES_BYTES:
                idx = data.find(needle)
                while idx != -1:
                    hit_for_this_file.append(
                        (idx, needle, data[max(0, idx - 32):idx + len(needle) + 64])
                    )
                    if len(hit_for_this_file) >= 8:
                        break
                    idx = data.find(needle, idx + 1)
                if len(hit_for_this_file) >= 8:
                    break

            if not hit_for_this_file:
                continue

            n_hits += 1
            txt_lines.append("")
            txt_lines.append(f"+++ HIT  {entry.path}  (group {group}, ext {ext})")
            for (off, needle, ctx) in hit_for_this_file:
                # Show context as escaped ASCII for readability.
                pretty = "".join(
                    chr(b) if 32 <= b < 127 else "."
                    for b in ctx
                )
                txt_lines.append(
                    f"    @ off 0x{off:08X} (needle {needle!r}): {pretty!r}"
                )

            # Dump full file bytes for inspection.
            dump_path = dump_dir / safe_name(entry.path)
            try:
                dump_path.write_bytes(data)
                txt_lines.append(f"    -> dumped to {dump_path}")
            except Exception as e:
                txt_lines.append(f"    -> dump fail: {e!r}")

        if n_scanned % 500 == 0:
            print(
                f"  group {group}: {n_scanned} scanned, {n_hits} hits",
                file=sys.stderr,
            )

    txt_lines.append("")
    txt_lines.append("=" * 78)
    txt_lines.append(f"Files scanned: {n_scanned}")
    txt_lines.append(f"Files w/ hit:  {n_hits}")
    txt_lines.append("=" * 78)
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")
    print(f"Wrote {txt_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
