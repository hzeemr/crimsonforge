"""Correlate PAA track bind quaternions against PAB bone bind quats.

If PAA track[i]'s bind closely matches PAB bone[j]'s bind, that's
strong evidence they're the same bone. Using quaternion angular
distance, we can compute the full cost matrix and pick the best
assignment (Hungarian algorithm) — or, simpler, greedy best-match
with a confidence threshold.
"""

import os
import sys
import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab
from core.animation_parser_v2 import parse_paa_v2


def quat_angle_deg(q1, q2):
    """Return angular distance between two unit quaternions (degrees)."""
    dot = q1[0] * q2[0] + q1[1] * q2[1] + q1[2] * q2[2] + q1[3] * q2[3]
    # Double-cover: flip if negative
    dot = abs(dot)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def main():
    temp = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    pab_path = next(temp.glob("crimsonforge_preview_*/phm_01.pab"))
    pab_bytes = pab_path.read_bytes()
    skel = parse_pab(pab_bytes, pab_path.name)
    pab_bones = [(i, b.name, b.rotation) for i, b in enumerate(skel.bones)]
    print(f"PAB: {len(pab_bones)} bones")

    paa_paths = [
        next(temp.glob("crimsonforge_preview_*/cd_phm_cough_00_00_nor_std_hello_02.paa")),
        next(temp.glob("crimsonforge_preview_*/cd_phm_cough_00_00_nor_std_idle_01.paa")),
    ]

    for paa_path in paa_paths:
        print(f"\n{'=' * 80}")
        print(f"PAA: {paa_path.name}")
        print('=' * 80)
        data = paa_path.read_bytes()
        v2 = parse_paa_v2(data, paa_path.name)

        # For each PAA track, find the PAB bone with the smallest
        # angular distance between bind quaternions.
        print(f"PAA {len(v2.tracks)} tracks  vs  PAB {len(pab_bones)} bones")
        print()
        print(f"{'track':>5s} {'best_pab':<25s} {'dist_deg':>8s}  next_2_runners_up")

        assignments_by_pab: dict[int, list[tuple[int, float]]] = {}
        assignments_perfect = 0  # tracks within 5°
        for track_idx, track in enumerate(v2.tracks):
            paa_bind = track.bind_quat
            candidates = []
            for pab_idx, name, pab_rot in pab_bones:
                d = quat_angle_deg(paa_bind, pab_rot)
                candidates.append((pab_idx, name, d))
            candidates.sort(key=lambda x: x[2])
            best_idx, best_name, best_dist = candidates[0]
            if best_dist < 5.0:
                assignments_perfect += 1
            runners = " ".join(f"{n[:12]}:{d:.0f}" for _, n, d in candidates[1:3])
            print(f"  {track_idx:3d}  {best_name[:25]:<25s} {best_dist:>7.1f}   {runners}")
            assignments_by_pab.setdefault(best_idx, []).append((track_idx, best_dist))

        print()
        print(f"Summary: {assignments_perfect}/{len(v2.tracks)} tracks have a PAB bone within 5°")

        # Which PAB bones are claimed by multiple tracks?
        conflicts = {k: v for k, v in assignments_by_pab.items() if len(v) > 1}
        print(f"PAB bones claimed by multiple tracks: {len(conflicts)}")
        for pab_idx, tracks in list(conflicts.items())[:5]:
            name = pab_bones[pab_idx][1]
            print(f"  {name}: tracks {[(t, round(d, 1)) for t, d in tracks]}")

        # Does PAA track order match PAB bone order?
        # If track[i].bind ≈ pab[i].bind for many i, order matches.
        ordered_hits = 0
        for i, track in enumerate(v2.tracks):
            if i >= len(pab_bones):
                break
            d = quat_angle_deg(track.bind_quat, pab_bones[i][2])
            if d < 10.0:
                ordered_hits += 1
        print(f"Tracks where track[i] bind ≈ pab[i] bind (within 10°): "
              f"{ordered_hits}/{min(len(v2.tracks), len(pab_bones))}")


if __name__ == "__main__":
    main()
