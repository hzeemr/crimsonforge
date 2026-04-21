"""Dump every bone's position from a PAB file so we can see
whether the parser is actually returning real positions or all zeros.
"""

import sys
from pathlib import Path

# Make the repo root importable
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab


def main():
    if len(sys.argv) < 2:
        print("Usage: python pab_dump.py <path to .pab>")
        sys.exit(1)
    pab_path = sys.argv[1]
    data = open(pab_path, "rb").read()
    skel = parse_pab(data, Path(pab_path).name)
    print(f"Parsed {len(skel.bones)} bones from {pab_path}")
    print()
    print(f"{'idx':>3s} {'parent':>6s}  {'name':30s} {'pos':30s} {'rot':40s} {'scale':25s}")
    for b in skel.bones:
        pos_s = f"({b.position[0]:+.4f},{b.position[1]:+.4f},{b.position[2]:+.4f})"
        rot_s = f"({b.rotation[0]:+.3f},{b.rotation[1]:+.3f},{b.rotation[2]:+.3f},{b.rotation[3]:+.3f})"
        sc_s = f"({b.scale[0]:+.3f},{b.scale[1]:+.3f},{b.scale[2]:+.3f})"
        print(f"{b.index:3d} {b.parent_index:6d}  {b.name[:30]:30s} {pos_s:30s} {rot_s:40s} {sc_s}")

    # Stats
    import math
    zero_count = 0
    max_abs = 0.0
    total_dist = 0.0
    for b in skel.bones:
        mag = math.sqrt(b.position[0]**2 + b.position[1]**2 + b.position[2]**2)
        if mag < 1e-9:
            zero_count += 1
        max_abs = max(max_abs, mag)
        total_dist += mag
    print()
    print(f"Bones with zero position : {zero_count} / {len(skel.bones)}")
    print(f"Max |pos| : {max_abs:.4f}")
    print(f"Avg |pos| : {total_dist / max(len(skel.bones), 1):.4f}")


if __name__ == "__main__":
    main()
