"""Scan every XML-ish file in the game for weapon/combat/sheathe
tokens. Pearl Abyss XML is often encrypted/compressed in PAZ so we
need to read it via the VFS. We look for BOTH plain-text tokens AND
the fp32 12.0 byte pattern."""

from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vfs_manager import VfsManager

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"

TOKENS = (
    b"sheathe", b"Sheathe", b"SHEATHE",
    b"Unready", b"UnReady", b"UNREADY",
    b"AutoSheath", b"AutoStand",
    b"CombatExit", b"BattleExit", b"ExitBattle",
    b"WeaponDraw", b"DrawWeapon",
    b"WeaponReady", b"ReadyWeapon",
    b"IdleTime", b"IdleDuration",
    b"WeaponMode", b"ReadyMode", b"NonBattleMode",
    b"ResetWeapon", b"PutAwayWeapon",
    b"Holster",
)

FP32_12 = struct.pack("<f", 12.0)


def main() -> None:
    vfs = VfsManager(GAME)

    # Target extensions most likely to hold gameplay config in text form.
    target_exts = {
        ".xml", ".paac", ".paacdesc", ".paasmt", ".pabc",
        ".motionblending", ".paproj", ".paprojdesc",
    }

    entries = []
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
            for e in pamt.file_entries:
                ext = os.path.splitext(e.path)[1].lower()
                if ext in target_exts:
                    entries.append((g, e, ext))
        except Exception:
            pass
    print(f"Scanning {len(entries)} candidate files...")

    # Group by extension for per-class reporting.
    hits_by_ext: dict[str, int] = {}
    sample_hits: list[tuple[str, str, str, bytes]] = []   # (ext, path, hit_kind, ctx)

    for g, e, ext in entries:
        try:
            data = vfs.read_entry_data(e)
        except Exception:
            continue
        if not data:
            continue

        # Token search
        token_hits = []
        for tok in TOKENS:
            p = 0
            while True:
                at = data.find(tok, p)
                if at < 0:
                    break
                token_hits.append((tok, at))
                p = at + 1
                if len(token_hits) > 3:
                    break

        # fp32 12.0 search
        fp_hits = []
        p = 0
        while True:
            at = data.find(FP32_12, p)
            if at < 0:
                break
            fp_hits.append(at)
            p = at + 1
            if len(fp_hits) > 3:
                break

        if token_hits or fp_hits:
            hits_by_ext[ext] = hits_by_ext.get(ext, 0) + 1
            # Keep just a few examples per extension
            if sum(1 for s in sample_hits if s[0] == ext) < 5:
                if token_hits:
                    tok, at = token_hits[0]
                    lo = max(0, at - 60)
                    hi = min(len(data), at + 120)
                    sample_hits.append((ext, e.path, f"tok={tok!r}", data[lo:hi]))
                elif fp_hits:
                    at = fp_hits[0]
                    lo = max(0, at - 80)
                    hi = min(len(data), at + 80)
                    sample_hits.append((ext, e.path, f"fp32@{at:#x}", data[lo:hi]))

    print()
    print("Files with token or fp32=12.0 hits, by extension:")
    for ext, cnt in sorted(hits_by_ext.items(), key=lambda x: -x[1]):
        print(f"  {ext:20s}  {cnt} files")

    print()
    print("Sample hits (first 5 per extension):")
    for ext, path, kind, ctx in sample_hits:
        print()
        print(f"[{ext}] {path}  {kind}")
        # Try to extract printable context
        printable = re.findall(rb"[\x20-\x7E\n]{4,}", ctx)
        for run in printable[:5]:
            text = run.decode("ascii", "replace").strip()
            if text:
                print(f"    {text[:200]}")


if __name__ == "__main__":
    main()
