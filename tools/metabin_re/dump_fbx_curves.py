"""Dump the actual Lcl Rotation curve values from a written FBX file.

Proves whether different .fbx files have different rotation curves.
If the curves differ across files but the rendered bones look the
same, the bug is in Blender's IMPORT, not our export. If the curves
are the same, the bug is in our export.
"""

import sys
import struct
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Use the same FBX reader the tests use
from tests.test_fbx_skinning import _parse_fbx_binary, _collect_by_name, _find_first


def dump_curves(fbx_path):
    print(f"\n=== {os.path.basename(fbx_path)} ===")
    nodes = _parse_fbx_binary(fbx_path)
    curves = _collect_by_name(nodes, "AnimationCurve")
    # Group curves by name to find bones
    for c in curves[:15]:
        name = c["props"][1].split("\x00", 1)[0]
        kv = _find_first(c["children"], "KeyValueFloat")
        kt = _find_first(c["children"], "KeyTime")
        if kv and kt:
            vals = list(kv["props"][0])
            times = list(kt["props"][0])
            # Sample at 5 frames evenly spread
            step = max(1, len(vals) // 5)
            samples = vals[::step][:5]
            print(f"  {name}  {len(vals)} keys  sample values (deg): "
                  f"{[round(v, 2) for v in samples]}")


def main():
    # 3 working FBX files
    files = [
        r"C:\Users\hzeem\Pictures\er_test4\cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.fbx",
        r"C:\Users\hzeem\Pictures\er_test4\cd_phm_cough_00_00_nor_std_hello_02.fbx",
        r"C:\Users\hzeem\Pictures\er_test4\cd_phm_cough_00_00_nor_std_idle_01.fbx",
    ]
    for f in files:
        if os.path.exists(f):
            dump_curves(f)


if __name__ == "__main__":
    main()
