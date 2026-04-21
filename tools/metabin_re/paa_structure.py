"""Find PAA structure by computing what makes sense given the metadata.

If a PAA file is N bytes with K bones × F frames of animation data:
  * 8 bytes/frame/bone = compressed (int16 quat or smallest-3) -> N*K*8
  * 16 bytes/frame/bone = full int16 quat + position
  * 32 bytes/frame/bone = full float32 quat + position + scale

We compute those for each file then look at what's leftover (header).
"""

import sys
import struct
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Known metadata from earlier parsing (we trust the bone/frame counts
# from a direct game source, not the file itself)
FILES = {
    "cd_phm_cough_00_00_nor_std_idle_01.paa": {
        "size": 94013, "bones": 57, "frames": 197, "duration": 6.57,
    },
    "cd_phm_cough_00_00_nor_std_hello_02.paa": {
        "size": 93438, "bones": 57, "frames": 187, "duration": 6.23,
    },
    "cd_phm_basic_00_00_roofclimb_move_up_m50tom25_m25_ready_01.paa": {
        "size": 51106, "bones": 76, "frames": 77, "duration": 2.57,
    },
    "cd_phm_child_00_00_hot_nor_std_idle_01.paa": {
        "size": 48847, "bones": 56, "frames": 114, "duration": 3.80,
    },
}


def main():
    print(f"{'file':45s} {'size':>7s} {'B':>3s} {'F':>3s}  guess(bytes/frame/bone)")
    for fname, m in FILES.items():
        size = m["size"]; B = m["bones"]; F = m["frames"]
        # Assuming most of the file is per-frame data
        # leftover = size - B*F*X  where X is bytes/frame/bone
        for x in (4, 6, 8, 10, 12, 16, 20, 24, 32, 40):
            leftover = size - B * F * x
            if 50 < leftover < 5000:
                # Plausible header size — flag this
                print(f"  {fname[:45]:45s} {size:>7d} {B:>3d} {F:>3d}  X={x:2d}  header={leftover:5d}  per_frame={B*x:4d}")

    # Also: maybe it's per-bone data (bind pose) + per-frame data
    print("\n=== with bind pose (40 bytes/bone) + per-frame data ===")
    for fname, m in FILES.items():
        size = m["size"]; B = m["bones"]; F = m["frames"]
        bind_bytes = B * 40
        for x in (4, 6, 8, 10, 12, 16):
            leftover = size - bind_bytes - B * F * x
            if 50 < leftover < 5000:
                print(f"  {fname[:45]:45s} bind={bind_bytes}  X={x:2d}  header={leftover}")

    print("\n=== variable per-bone keyframes (sparse) ===")
    # If sparse, bytes per record might be 10 (idx + xyz int16 + w fp16)
    # Total animation bytes ≈ active_bones * keys_per_bone * 10
    for fname, m in FILES.items():
        size = m["size"]; B = m["bones"]; F = m["frames"]
        # If average key density is 80% per bone-frame:
        for density in (0.5, 0.7, 0.8, 0.9, 1.0):
            for stride in (8, 10, 12):
                keys_total = int(B * F * density)
                anim_bytes = keys_total * stride
                leftover = size - anim_bytes - B * 40
                if 0 < leftover < 200:
                    print(f"  {fname[:45]:45s} density={density} stride={stride} bind=B*40 ANIM={anim_bytes:6d} leftover={leftover}")


if __name__ == "__main__":
    main()
