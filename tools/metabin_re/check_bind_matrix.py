"""Check if PAB bind_matrix represents world bind transforms.

If bone.bind_matrix is the WORLD bind transform, then the matrix's
translation component (last column or last row depending on layout)
should match the cascaded parent positions we'd compute manually.
"""

import sys
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.skeleton_parser import parse_pab


def matrix_to_translation(m, layout="row"):
    """Extract translation from a 16-float matrix.

    FBX/Maya convention is column-major (translation in last 3 of last col).
    Pearl Abyss might be row-major (translation in last 3 of last row).
    Print both and let the data tell us.
    """
    if layout == "row":
        return (m[12], m[13], m[14])  # row-major: tx,ty,tz at positions 12,13,14
    else:
        return (m[3], m[7], m[11])    # column-major


def main():
    pab_path = "C:/Users/hzeem/AppData/Local/Temp/crimsonforge_preview_v26_gvp9/phm_01.pab"
    data = open(pab_path, "rb").read()
    skel = parse_pab(data, "phm_01.pab")

    print(f"{'idx':>3s} {'name':30s} {'local_pos':30s} {'bind_T_row':30s} {'bind_T_col':30s}")
    for b in skel.bones[:15]:
        local = b.position
        if b.bind_matrix and len(b.bind_matrix) == 16:
            t_row = matrix_to_translation(b.bind_matrix, "row")
            t_col = matrix_to_translation(b.bind_matrix, "column")
        else:
            t_row = ("-", "-", "-")
            t_col = ("-", "-", "-")
        print(f"{b.index:3d} {b.name[:30]:30s} {local!s:30s} {t_row!s:30s} {t_col!s:30s}")

    # Also check if bind_matrix is a proper rotation+translation (orthonormal upper 3x3)
    if skel.bones[0].bind_matrix and len(skel.bones[0].bind_matrix) == 16:
        m = skel.bones[0].bind_matrix
        print(f"\nFull bind_matrix for {skel.bones[0].name}:")
        for r in range(4):
            print(f"  {[round(m[r*4+c], 4) for c in range(4)]}")

    # If bind_matrix is world space, Bip01's translation should be (0, 0.97, 0)
    # If it's the LOCAL bind matrix (relative to parent), it'd be its local pos


if __name__ == "__main__":
    main()
