"""Patch the HP=2675 i32 inside questinfo.pabgb row 0
(LevelSequencerSpawn_AllSchedule_m05_m01) and write it back to the
PAZ. This is the focused test for "is Ogre's HP actually controlled
by this byte sequence in questinfo.pabgb?"

If after patching the boss enters the fight with reduced HP, we've
found the real source file. If HP is still 2675 in-game, the byte
we found is a different character's HP that happens to be 2675 too,
and we need to keep searching.

Usage
-----
    python -X utf8 tools/patch_quest_hp.py --new-hp 1
    python -X utf8 tools/patch_quest_hp.py --new-hp 1 --apply

Without --apply, just shows what WOULD be written. With --apply, the
patch lands on disk and is wrapped back into the PAZ via the same
VfsManager pipeline the in-app editor uses.

Always close Crimson Desert FIRST.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb, serialize_pabgb

GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
HP_OFFSET_IN_ROW = 0x226154   # found by tools/find_3008_everywhere.py + manual scan
EXPECTED_OLD_HP  = 2675


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new-hp", type=int, default=1,
                    help="value to write at the HP slot (default 1)")
    ap.add_argument("--search-for", type=int, default=None,
                    help="i32 value to find in the file. defaults to 2675 "
                         "(original Ogre HP). For revert, pass the value "
                         "you previously wrote, e.g. --search-for 1 "
                         "--new-hp 2675")
    ap.add_argument("--apply",  action="store_true",
                    help="actually write back to the PAZ; without this it "
                         "just dry-runs and prints the diff.")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=None,
                    help="patch this exact file offset instead of "
                         "search-and-replace. Use after first patch when "
                         "you know the offset (e.g. 0x226163).")
    args = ap.parse_args()
    if args.search_for is None:
        args.search_for = 2675   # default: original value

    vfs = VfsManager(GAME)
    vfs.load_papgt()

    qb_e = qh_e = None
    for g in vfs.list_package_groups():
        pamt = vfs.load_pamt(g)
        for e in pamt.file_entries:
            p = e.path.lower()
            if p == "gamedata/questinfo.pabgb":
                qb_e = e
            elif p == "gamedata/questinfo.pabgh":
                qh_e = e
        if qb_e and qh_e:
            break
    if not (qb_e and qh_e):
        print("ERROR: questinfo.pabgb / pabgh not found")
        return 1

    data = vfs.read_entry_data(qb_e)
    print(f"questinfo.pabgb loaded: {len(data):,} bytes")

    # Direct byte-level edit -- DON'T use serialize_pabgb because the
    # parser mis-detects this file's row layout (3 rows reported with
    # sizes that sum to 4.7 MB > 2.27 MB file). The on-disk bytes are
    # correct, only the parser model is wrong, so we modify raw bytes
    # in place and write them back unchanged elsewhere.

    if args.offset is not None:
        # Direct offset mode (no search) -- useful for re-patching after
        # a previous edit changed the value we'd otherwise grep for.
        target_offset = args.offset
        actual = struct.unpack_from("<I", data, target_offset)[0]
        print(f"direct offset mode: file offset 0x{target_offset:x} "
              f"currently holds i32 {actual}")
    else:
        # Search for the expected value
        needle = struct.pack("<I", args.search_for)
        positions = []
        i = 0
        while True:
            i = data.find(needle, i)
            if i == -1: break
            positions.append(i); i += 1
        print(f"found {len(positions)} occurrences of i32={args.search_for} "
              f"in the raw file")
        if not positions:
            print("ABORT: search value not found anywhere.")
            print(f"  If you previously patched it to a different number,")
            print(f"  use --offset 0x226163 (the known HP slot) "
                  f"plus --new-hp <desired value>")
            print(f"  Or use --search-for <previous value> --new-hp 2675 "
                  f"to revert.")
            return 1

        target_offset = positions[0]
        print(f"patching first occurrence at file offset 0x{target_offset:x}")

    # Show 32 bytes around the patch point for context
    start = max(0, target_offset - 16)
    end   = min(len(data), target_offset + 32)
    print(f"\ncontext (rel offsets):")
    for off in range(start, end - 3, 4):
        word = data[off:off+4]
        rel = off - target_offset
        u32 = struct.unpack("<I", word)[0]
        marker = "  <<< HP" if rel == 0 else ""
        print(f"  +{rel:+5d}  {word.hex()}  u32={u32:>10d}{marker}")

    # Build the patched bytes
    new_data = bytearray(data)
    new_data[target_offset : target_offset + 4] = struct.pack(
        "<I", args.new_hp
    )
    print(f"\npatch: i32 {EXPECTED_OLD_HP} -> {args.new_hp} "
          f"({struct.pack('<I', args.new_hp).hex()})")
    print(f"file size unchanged: {len(new_data):,} bytes")

    if not args.apply:
        print("\n(dry run -- pass --apply to actually write)")
        return 0

    new_data = bytes(new_data)
    if len(new_data) != len(data):
        print(f"INTERNAL ERROR: size changed -- abort")
        return 1

    # Use the same RepackEngine pipeline the in-app editor uses.
    print("\nrepacking PAZ via RepackEngine...")
    from core.repack_engine import RepackEngine, ModifiedFile
    grp = "0008"   # questinfo lives in 0008
    pamt = vfs.load_pamt(grp)
    mf = ModifiedFile(
        data=new_data, entry=qb_e, pamt_data=pamt, package_group=grp,
    )
    papgt = str(Path(GAME) / "meta" / "0.papgt")
    result = RepackEngine(GAME).repack([mf], papgt_path=papgt)
    if not result.success:
        print(f"REPACK FAILED: {result.errors}")
        return 1
    print(f"OK -- patched. launch the game and fight the Ogre.")
    print(f"Original size: {len(data):,}  New size: {len(new_data):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
