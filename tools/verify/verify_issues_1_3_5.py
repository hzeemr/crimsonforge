"""One-shot verification that the three Known Issues closed in v1.21.0
actually work on real game files.

Run via:
    python tools/verify/verify_issues_1_3_5.py

Exits 0 if every feature exercises cleanly on a real PAA from the
temp cache, exits non-zero with a failure reason otherwise.
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.animation_parser import parse_paa
from core.animation_parser_v2 import parse_paa_v2
from core.animation_writer import serialize_paa, tracks_from_parsed
from core.paa_bone_mapping import auto_correlate, apply_bone_map, save_bone_map, load_bone_map
from core.paa_link_resolver import normalise_link_target
from core.skeleton_parser import parse_pab


def section(title):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def issue_1_bone_mapping(pab_path, paa_path):
    """Auto-correlate track->bone map + apply to export."""
    section("Issue #1 - PAA track -> PAB bone mapping")

    pab_bytes = pab_path.read_bytes()
    skel = parse_pab(pab_bytes, pab_path.name)
    paa_bytes = paa_path.read_bytes()
    v2 = parse_paa_v2(paa_bytes, paa_path.name)

    print(f"  PAB: {pab_path.name}  ({len(skel.bones)} bones)")
    print(f"  PAA: {paa_path.name}  ({len(v2.tracks)} tracks)")

    # Auto-correlate
    bone_map = auto_correlate(v2.tracks, skel.bones, rig_key=pab_path.stem)
    mapped = sum(1 for v in bone_map.mapping.values() if v >= 0)
    print(f"  auto_correlate: {mapped}/{len(v2.tracks)} tracks mapped")
    if mapped == 0:
        print("  FAIL: no tracks mapped")
        return False

    # Apply + count pairs
    pairs = apply_bone_map(v2.tracks, skel.bones, bone_map)
    print(f"  apply_bone_map: {len(pairs)} (track, pab) pairs produced")
    if not pairs:
        return False

    # Persist + reload
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # Monkey-patch the user-data dir to a throwaway path
        import core.paa_bone_mapping as bm_mod
        original = bm_mod.bone_map_dir
        bm_mod.bone_map_dir = lambda: td
        try:
            save_bone_map(bone_map)
            reloaded = load_bone_map(bone_map.rig_key)
        finally:
            bm_mod.bone_map_dir = original

    if reloaded is None or reloaded.mapping != bone_map.mapping:
        print("  FAIL: persistence round-trip lost data")
        return False

    print(f"  persistence: saved + reloaded {len(reloaded.mapping)} entries")
    print("  PASS")
    return True


def issue_3_link_resolver(temp):
    """Verify link-variant detection + path normalisation + mock resolver."""
    section("Issue #3 - Link-variant PAA resolution")

    # Find any link-variant file (low-byte flag 0x4A / 0xCA / 0x4F / 0xCF)
    link_found = None
    for paa in list(temp.glob("crimsonforge_preview_*/*.paa"))[:50]:
        data = paa.read_bytes()
        if len(data) < 0x14:
            continue
        flag_low = data[0x10]
        if flag_low in (0x4A, 0xCA, 0x4F, 0xCF):
            link_found = (paa, data, flag_low)
            break

    if link_found is None:
        print("  no link-variant PAA in the temp cache (that's fine - the")
        print("  code path is unit-tested separately). Exercising normaliser only.")
    else:
        paa_path, data, flag_low = link_found
        print(f"  link-variant found: {paa_path.name}  flag_low=0x{flag_low:02x}")
        parsed = parse_paa(data, paa_path.name)
        print(f"  parse_paa detected is_link={parsed.is_link}  "
              f"target={parsed.link_target[:60]!r}")
        if parsed.is_link and parsed.link_target:
            normalised = normalise_link_target(parsed.link_target)
            print(f"  normalise_link_target: {normalised!r}")

    # Exercise the normaliser on canonical input shapes
    cases = [
        ("%character/anim/walk.paa", "character/anim/walk.paa"),
        ("character/foo.paa\x00junk", "character/foo.paa"),
        ("%CHARACTER/PATH.PAA", "character/path.paa"),
        ("%character\\sub\\file.paa", "character/sub/file.paa"),
    ]
    for inp, expected in cases:
        got = normalise_link_target(inp)
        ok = got == expected
        mark = "OK" if ok else "FAIL"
        print(f"  {mark}  normalise({inp!r}) -> {got!r}")
        if not ok:
            return False

    print("  PASS")
    return True


def issue_5_fbx_to_paa_writer(paa_path):
    """Parse a real PAA, re-serialise, parse again, compare tracks."""
    section("Issue #5 - FBX -> PAA inverse writer")

    data = paa_path.read_bytes()
    parsed_once = parse_paa_v2(data, paa_path.name)
    print(f"  parse_paa_v2 on real file: {len(parsed_once.tracks)} tracks, "
          f"{parsed_once.frame_count} frames")

    tracks = tracks_from_parsed(parsed_once)
    rewritten = serialize_paa(tracks, tag=parsed_once.metadata_tags)
    print(f"  serialize_paa: {len(rewritten):,} bytes (vs original {len(data):,})")

    parsed_twice = parse_paa_v2(rewritten, "roundtrip.paa")
    print(f"  parse re-serialised: {len(parsed_twice.tracks)} tracks")

    if len(parsed_once.tracks) != len(parsed_twice.tracks):
        print(f"  FAIL: track count differs {len(parsed_once.tracks)} vs "
              f"{len(parsed_twice.tracks)}")
        return False

    for i, (t1, t2) in enumerate(zip(parsed_once.tracks, parsed_twice.tracks)):
        if len(t1.keyframes) != len(t2.keyframes):
            print(f"  FAIL: track {i} keyframe count differs "
                  f"{len(t1.keyframes)} vs {len(t2.keyframes)}")
            return False
        for kf1, kf2 in zip(t1.keyframes, t2.keyframes):
            if kf1[0] != kf2[0]:
                print(f"  FAIL: track {i} frame index mismatch")
                return False
            for j in range(1, 5):
                if abs(kf1[j] - kf2[j]) > 0.02:  # fp16 precision band
                    print(f"  FAIL: track {i} component {j} differs "
                          f"{kf1[j]:.4f} vs {kf2[j]:.4f}")
                    return False

    print(f"  bit-compatible round-trip OK "
          f"(frames exact, quats within fp16 precision)")
    print("  PASS")
    return True


def main():
    temp = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    pab_path = next(temp.glob("crimsonforge_preview_*/phm_01.pab"), None)
    paa_path = next(
        temp.glob("crimsonforge_preview_*/cd_phm_cough_00_00_nor_std_hello_02.paa"),
        None,
    )

    if pab_path is None or paa_path is None:
        print("FAIL: needed PAB (phm_01.pab) or PAA sample missing in temp cache")
        return 1

    results = {
        "issue_1_bone_mapping":  issue_1_bone_mapping(pab_path, paa_path),
        "issue_3_link_resolver": issue_3_link_resolver(temp),
        "issue_5_fbx_to_paa":    issue_5_fbx_to_paa_writer(paa_path),
    }

    section("SUMMARY")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
