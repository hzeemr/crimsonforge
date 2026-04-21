"""Compare quaternion data across PAA files using v2 parser."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.animation_parser_v2 import parse_paa_v2, densify_track


def quat_distance(q1, q2):
    """Angular distance between two quaternions (in degrees)."""
    import math
    dot = sum(a * b for a, b in zip(q1, q2))
    dot = max(-1.0, min(1.0, abs(dot)))
    angle_rad = 2 * math.acos(dot)
    return math.degrees(angle_rad)


FILES = [
    "cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.paa",
    "cd_phm_cough_00_00_nor_std_hello_02.paa",
    "cd_phm_cough_00_00_nor_std_idle_01.paa",
]


def main():
    parsed = {}
    for fn in FILES:
        for sub in ("crimsonforge_preview_9ffbu3wb",):
            p = Path(rf"C:\Users\hzeem\AppData\Local\Temp\{sub}") / fn
            if p.exists():
                data = p.read_bytes()
                v2 = parse_paa_v2(data, fn)
                # Densify each track to a common frame range
                max_frames = v2.frame_count
                tracks = [densify_track(t, max_frames) for t in v2.tracks]
                parsed[fn] = (v2, tracks)
                break

    # For each pair of files and each bone, compute average angular
    # distance between corresponding quaternions across frames.
    print(f"\n{'file_A':45s} {'file_B':45s} {'bone':>4s} avg_diff(deg)")
    fns = list(parsed.keys())
    for i in range(len(fns)):
        for j in range(i + 1, len(fns)):
            v2a, ta = parsed[fns[i]]
            v2b, tb = parsed[fns[j]]
            # Align frame counts (use min)
            common_frames = min(v2a.frame_count, v2b.frame_count)
            common_bones = min(len(ta), len(tb))
            for b in range(min(8, common_bones)):
                if not ta[b] or not tb[b]:
                    continue
                diffs = []
                for f in range(common_frames):
                    if f < len(ta[b]) and f < len(tb[b]):
                        diffs.append(quat_distance(ta[b][f], tb[b][f]))
                if diffs:
                    avg = sum(diffs) / len(diffs)
                    max_d = max(diffs)
                    print(f"  {fns[i][:45]:45s} {fns[j][:45]:45s} {b:>4d} avg={avg:6.2f} max={max_d:6.2f}")


if __name__ == "__main__":
    main()
