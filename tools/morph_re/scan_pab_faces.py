"""Scan every PAB skeleton for facial bones.

The face-morph hypothesis is that Pearl Abyss puts tiny 'morph bones'
(scale/translate rig controls) inside the character skeleton. If
that's true, we should see bones named things like:

  Nose, Brow, Eye, Cheek, Jaw, Chin, Lip, Mouth, Forehead, Ear,
  Face_*, Facial_*, BN_Face_*, Head_Sub_*

against the normal body-bone names (Bip01, Spine, Thigh, Shoulder).

Scanning all PAB files prints a report per-skeleton of bones that
match the facial-region pattern.
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab

FACIAL_HINTS = [
    "nose", "brow", "eye", "cheek", "jaw", "chin", "lip", "mouth",
    "forehead", "ear", "face", "facial",
    "head_sub", "head_top", "head_b",
    "tongue", "teeth", "neck",
    "eyebrow", "eyelid", "eyeball",
]


def scan(path):
    try:
        data = open(path, "rb").read()
        skel = parse_pab(data, os.path.basename(path))
    except Exception as e:
        print(f"  ERROR {path}: {e}")
        return

    facial = []
    other = []
    for b in skel.bones:
        name_l = b.name.lower()
        if any(hint in name_l for hint in FACIAL_HINTS):
            facial.append(b)
        else:
            other.append(b)

    print(f"\n=== {os.path.basename(path)} ({len(skel.bones)} bones) ===")
    if facial:
        print(f"FACIAL BONES: {len(facial)}")
        for b in facial:
            print(f"  [{b.index:3d}] parent={b.parent_index:3d}  {b.name:35s}  "
                  f"pos=({b.position[0]:+.3f},{b.position[1]:+.3f},{b.position[2]:+.3f})  "
                  f"scale=({b.scale[0]:.3f},{b.scale[1]:.3f},{b.scale[2]:.3f})")
    else:
        print("NO facial bones matched in this skeleton")

    # Show all bones as well for the smallest skeleton
    if len(skel.bones) <= 30:
        print(f"ALL bone names: {[b.name for b in skel.bones]}")


def main():
    root = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    pabs = sorted(set(str(p) for p in root.glob("crimsonforge_preview_*/*.pab")))
    print(f"Scanning {len(pabs)} PAB files for facial bones\n")
    for p in pabs:
        scan(p)


if __name__ == "__main__":
    main()
