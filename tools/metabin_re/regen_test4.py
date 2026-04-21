"""Regenerate 4 test FBX files using the CLEAN v2 parser.

Replaces the heuristic animation_parser.py with the byte-level
reverse-engineered animation_parser_v2.py — no fallbacks, real
quaternions decoded directly from fp16 + uint16 records.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.animation_parser import AnimationKeyframe, ParsedAnimation
from core.animation_parser_v2 import parse_paa_v2, densify_track
from core.skeleton_parser import parse_pab
from core.animation_fbx_exporter import export_animation_fbx


TEMP_ROOT = Path(r"C:\Users\hzeem\AppData\Local\Temp")
OUT_DIR = Path(r"C:\Users\hzeem\Pictures\er_test4")

CANDIDATES = [
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.paa"),
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_cough_00_00_nor_std_hello_02.paa"),
    ("crimsonforge_preview_9ffbu3wb",
     "cd_phm_cough_00_00_nor_std_idle_01.paa"),
    ("crimsonforge_preview_mq8sf8e4",
     "cd_phm_child_00_00_hot_nor_std_idle_01.paa"),
]


def _v2_to_parsed_animation(v2_anim, paa_path):
    """Convert ParsedAnimationV2 (bone-major) -> ParsedAnimation (frame-major).

    The FBX exporter expects ``keyframes[i].bone_rotations[j]`` =
    quaternion for bone j at frame i. v2 returns per-bone sparse
    keyframes, so we densify each track and transpose.
    """
    anim = ParsedAnimation(
        path=str(paa_path),
        duration=v2_anim.duration,
        frame_count=v2_anim.frame_count,
        bone_count=len(v2_anim.tracks),
    )
    if v2_anim.frame_count == 0 or not v2_anim.tracks:
        return anim
    # Densify each track to per-frame quaternions
    dense_per_bone = [densify_track(t, v2_anim.frame_count) for t in v2_anim.tracks]
    for f in range(v2_anim.frame_count):
        kf = AnimationKeyframe(frame_index=f)
        for bone_dense in dense_per_bone:
            kf.bone_rotations.append(bone_dense[f])
        anim.keyframes.append(kf)
    return anim


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.fbx"):
        old.unlink()
    for old in OUT_DIR.glob("*.png"):
        old.unlink()
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    skel_path = next(TEMP_ROOT.glob("crimsonforge_preview_*/phm_01.pab"))
    skel = parse_pab(skel_path.read_bytes(), skel_path.name)
    print(f"Loaded skeleton: {len(skel.bones)} bones from {skel_path}")

    for subdir, paa_name in CANDIDATES:
        paa_path = TEMP_ROOT / subdir / paa_name
        if not paa_path.exists():
            print(f"SKIP {paa_path}")
            continue
        data = paa_path.read_bytes()
        v2 = parse_paa_v2(data, paa_name)
        print(f"  v2: tracks={len(v2.tracks)} frames={v2.frame_count} duration={v2.duration:.2f}s")
        anim = _v2_to_parsed_animation(v2, paa_path)
        fbx = export_animation_fbx(anim, skel, str(OUT_DIR), name=paa_path.stem)
        print(f"Wrote {fbx}")


if __name__ == "__main__":
    main()
