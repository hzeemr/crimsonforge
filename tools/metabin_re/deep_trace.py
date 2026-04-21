"""DEEP TRACE: where does the animation data actually go wrong?

Pipeline stages:
  1. PAA bytes
  2. v2 parser -> tracks[bone][frame] = quaternion
  3. Densify -> per-frame array for each track
  4. FBX exporter -> Lcl Rotation curves (Euler XYZ degrees)
  5. FBX file on disk
  6. Blender import -> pose_bones[bone].rotation_quaternion

We dump each stage side-by-side for 2 files to see WHERE they
stop differing.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def trace(paa_path, label, bone_idx=5):
    """Trace one bone's animation through all pipeline stages."""
    from core.animation_parser_v2 import parse_paa_v2, densify_track
    import math

    data = open(paa_path, "rb").read()
    v2 = parse_paa_v2(data, paa_path)

    print(f"\n{'='*70}")
    print(f"{label}: {paa_path}")
    print(f"{'='*70}")
    print(f"  tracks: {len(v2.tracks)}  frames: {v2.frame_count}  duration: {v2.duration:.2f}s")

    if bone_idx >= len(v2.tracks):
        print(f"  bone_idx {bone_idx} out of range (only {len(v2.tracks)} tracks)")
        return None

    track = v2.tracks[bone_idx]
    print(f"\n  TRACK[{bone_idx}]: {len(track.keyframes)} keyframes")
    print(f"    bind: ({track.bind_quat[0]:+.4f}, {track.bind_quat[1]:+.4f}, "
          f"{track.bind_quat[2]:+.4f}, {track.bind_quat[3]:+.4f})")
    # First 5 + last 3 keyframes
    for k in track.keyframes[:5]:
        f, x, y, z, w = k
        print(f"    f={f:4d}  q=({x:+.4f}, {y:+.4f}, {z:+.4f}, {w:+.4f})")
    if len(track.keyframes) > 5:
        print(f"    ...")
        for k in track.keyframes[-3:]:
            f, x, y, z, w = k
            print(f"    f={f:4d}  q=({x:+.4f}, {y:+.4f}, {z:+.4f}, {w:+.4f})")

    # Densify to per-frame and show at f=10, 50, 100
    dense = densify_track(track, v2.frame_count)
    print(f"\n  DENSE per-frame samples (bone {bone_idx}):")
    for f in (1, 10, 30, 50, 100):
        if f < len(dense):
            q = dense[f]
            print(f"    f={f:4d}  q=({q[0]:+.4f}, {q[1]:+.4f}, {q[2]:+.4f}, {q[3]:+.4f})")

    # Convert dense quats to Euler XYZ degrees (matches FBX exporter logic)
    def quat_to_euler(q):
        x, y, z, w = q
        sinp = 2.0 * (w * y - z * x)
        sinp = max(-1.0, min(1.0, sinp))
        pitch = math.asin(sinp)
        sinr_cosp = 2.0 * (w * x + y * z)
        cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp)
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)
        return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))

    print(f"\n  EULER XYZ degrees (what FBX writes):")
    for f in (1, 10, 30, 50, 100):
        if f < len(dense):
            e = quat_to_euler(dense[f])
            print(f"    f={f:4d}  euler=({e[0]:+7.2f}, {e[1]:+7.2f}, {e[2]:+7.2f})")

    return dense


def main():
    files = [
        (Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_9ffbu3wb\cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.paa"),
         "roofclimb"),
        (Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_9ffbu3wb\cd_phm_cough_00_00_nor_std_hello_02.paa"),
         "hello"),
        (Path(r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_9ffbu3wb\cd_phm_cough_00_00_nor_std_idle_01.paa"),
         "idle"),
    ]
    # Pick bone 5 (which showed ~107° avg difference)
    all_dense = {}
    for p, label in files:
        if p.exists():
            d = trace(p, label, bone_idx=5)
            all_dense[label] = d
            d2 = trace(p, label, bone_idx=15)
            all_dense[label + "_b15"] = d2

    # Cross-file comparison
    if len(all_dense) >= 2:
        print(f"\n{'='*70}")
        print("CROSS-FILE COMPARISON at frame 50 (bone 5)")
        print(f"{'='*70}")
        for label, dense in all_dense.items():
            if dense and len(dense) > 50 and not label.endswith("_b15"):
                q = dense[50]
                print(f"  {label:15s}  q=({q[0]:+.4f}, {q[1]:+.4f}, {q[2]:+.4f}, {q[3]:+.4f})")


if __name__ == "__main__":
    main()
