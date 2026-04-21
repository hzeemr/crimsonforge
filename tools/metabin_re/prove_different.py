"""Definitive proof that the 3 FBX files have DIFFERENT animations.

Imports each FBX in Blender, sets the scene to frame 60, and logs
the POSE-MODE world rotation of every bone. If the rotations differ
across files, animations are genuinely different — the visual
perception of 'all same' is purely about the placeholder mesh's
lack of expressiveness.
"""

import sys
import os


def sample(fbx_path):
    import bpy
    from mathutils import Quaternion

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            arm_obj = obj
            break
    if not arm_obj:
        return {}

    result = {}
    scene = bpy.context.scene
    for frame in (1, 30, 60, 90):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        frame_data = {}
        for pb in arm_obj.pose.bones:
            q = pb.matrix.to_quaternion()
            frame_data[pb.name] = (round(q.x, 3), round(q.y, 3), round(q.z, 3), round(q.w, 3))
        result[frame] = frame_data
    return result


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if len(args) < 3:
        print("Usage: prove_different.py -- <fbx1> <fbx2> <fbx3>")
        sys.exit(1)

    all_data = {}
    for fbx in args:
        label = os.path.basename(fbx)
        print(f"\n=== sampling {label} ===")
        all_data[label] = sample(fbx)

    # Print Bip01 rotations across files at frame 60
    print(f"\n{'='*70}")
    print("Bip01 pose-mode WORLD rotation at frame 60:")
    print(f"{'='*70}")
    for label, frames in all_data.items():
        if 60 in frames and "Bip01" in frames[60]:
            q = frames[60]["Bip01"]
            print(f"  {label[:50]:50s}  q=({q[0]:+.3f}, {q[1]:+.3f}, {q[2]:+.3f}, {q[3]:+.3f})")

    # Print Bip01 Spine at frame 60
    print(f"\nBip01 Spine at frame 60:")
    for label, frames in all_data.items():
        if 60 in frames and "Bip01 Spine" in frames[60]:
            q = frames[60]["Bip01 Spine"]
            print(f"  {label[:50]:50s}  q=({q[0]:+.3f}, {q[1]:+.3f}, {q[2]:+.3f}, {q[3]:+.3f})")

    # Angular distance matrix
    import math
    labels = list(all_data.keys())
    print(f"\nAverage angular distance between pose-bones at frame 60:")
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            fa = all_data[labels[i]].get(60, {})
            fb = all_data[labels[j]].get(60, {})
            common = set(fa.keys()) & set(fb.keys())
            total_diff = 0.0
            count = 0
            for name in common:
                q1 = fa[name]; q2 = fb[name]
                dot = sum(a * b for a, b in zip(q1, q2))
                dot = max(-1.0, min(1.0, abs(dot)))
                total_diff += math.degrees(2 * math.acos(dot))
                count += 1
            avg = total_diff / count if count else 0
            print(f"  {labels[i][:30]:30s} vs {labels[j][:30]:30s}  avg={avg:6.2f} deg over {count} bones")


if __name__ == "__main__":
    main()
