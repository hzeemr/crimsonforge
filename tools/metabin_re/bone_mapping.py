"""Critical check: does PAA track order match PAB bone order?

If not, rotations get applied to the WRONG bones in the FBX, which
would explain "all animations look same" even with correct v2 data.

Method: compare bind-pose quaternions from PAA (track.bind_quat) with
PAB bone rotations. If they match index-for-index, mapping is correct.
"""

import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.animation_parser_v2 import parse_paa_v2
from core.skeleton_parser import parse_pab


def quat_dist_deg(q1, q2):
    dot = sum(a * b for a, b in zip(q1, q2))
    dot = max(-1.0, min(1.0, abs(dot)))
    return math.degrees(2 * math.acos(dot))


def main():
    skel_path = Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_v26_gvp9\phm_01.pab")
    paa_path = Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_9ffbu3wb\cd_phm_cough_00_00_nor_std_idle_01.paa")

    skel = parse_pab(skel_path.read_bytes(), skel_path.name)
    v2 = parse_paa_v2(paa_path.read_bytes(), paa_path.name)

    print(f"PAB bones: {len(skel.bones)}")
    print(f"PAA tracks: {len(v2.tracks)}")
    print()
    print(f"{'idx':>3s}  {'PAB bone name':30s}  {'PAB bind rot':40s}  {'PAA track bind':40s}  dist(deg)")

    mismatches = []
    for i in range(min(len(skel.bones), len(v2.tracks))):
        bone = skel.bones[i]
        track = v2.tracks[i]
        pab_rot = bone.rotation                    # (x,y,z,w)
        paa_bind = track.bind_quat                 # (x,y,z,w)
        dist = quat_dist_deg(pab_rot, paa_bind)
        match = "  MATCH" if dist < 30 else "  -- MISMATCH --"
        print(f"{i:3d}  {bone.name[:30]:30s}  "
              f"({pab_rot[0]:+.3f},{pab_rot[1]:+.3f},{pab_rot[2]:+.3f},{pab_rot[3]:+.3f})  "
              f"({paa_bind[0]:+.3f},{paa_bind[1]:+.3f},{paa_bind[2]:+.3f},{paa_bind[3]:+.3f})  "
              f"{dist:6.1f}{match}")
        if dist > 30:
            mismatches.append((i, bone.name, dist))

    print()
    print(f"Total mismatches (>30 deg): {len(mismatches)} / {min(len(skel.bones), len(v2.tracks))}")
    if mismatches:
        print()
        print("If mismatches are common, PAA track order does NOT match PAB bone order.")
        print("Sequential mapping in the FBX exporter would then apply rotations to WRONG bones.")


if __name__ == "__main__":
    main()
