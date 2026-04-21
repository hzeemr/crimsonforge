"""Regenerate 4 test FBX via the MAIN parse_paa() entry point.

This is what the UI uses. If results differ from regen_test4.py,
the integration between v2 and parse_paa is broken.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.animation_parser import parse_paa
from core.skeleton_parser import parse_pab
from core.animation_fbx_exporter import export_animation_fbx


TEMP_ROOT = Path(r"C:\Users\hzeem\AppData\Local\Temp")
OUT_DIR = Path(r"C:\Users\hzeem\Pictures\er_test4_via_ui")

CANDIDATES = [
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.paa"),
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_cough_00_00_nor_std_hello_02.paa"),
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_cough_00_00_nor_std_idle_01.paa"),
]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.fbx"):
        old.unlink()

    skel_path = next(TEMP_ROOT.glob("crimsonforge_preview_*/phm_01.pab"))
    skel = parse_pab(skel_path.read_bytes(), skel_path.name)
    print(f"Loaded skeleton: {len(skel.bones)} bones")

    for subdir, paa_name in CANDIDATES:
        paa_path = TEMP_ROOT / subdir / paa_name
        if not paa_path.exists():
            continue
        data = paa_path.read_bytes()
        # THIS IS WHAT THE UI CALLS
        anim = parse_paa(data, paa_name)
        print(f"\n  {paa_name[:50]}")
        print(f"    variant={anim.format_variant}  bones={anim.bone_count}  "
              f"frames={anim.frame_count}  duration={anim.duration:.2f}s")

        # Dump Bip01 rotation at frames 1, 30, 60
        if anim.keyframes:
            print(f"    Bip01 (bone 0) rotations across frames:")
            for f_idx in (0, 10, 30, 60):
                if f_idx < len(anim.keyframes):
                    kf = anim.keyframes[f_idx]
                    if kf.bone_rotations:
                        q = kf.bone_rotations[0]
                        print(f"      f={f_idx:3d}  q=({q[0]:+.4f}, {q[1]:+.4f}, {q[2]:+.4f}, {q[3]:+.4f})")

        fbx = export_animation_fbx(anim, skel, str(OUT_DIR), name=paa_path.stem)
        print(f"    -> {fbx}")


if __name__ == "__main__":
    main()
