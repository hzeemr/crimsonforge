"""Blender animation diff script (v2).

Run via:

    blender --background --python blender_animdiff.py -- <fbx1> <fbx2> ...

For each FBX:
  * lists every bone and its world-space position at frames 1, 20, 40, 60, 78
  * flags which bones MOVE within the file (animation actually applies)
Then compares across files to PROVE whether the animations are distinct.
"""

import sys
import os


def sample_fbx(fbx_path, frames=(1, 20, 40, 60, 78)):
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    # Find armature + action
    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            arm_obj = obj
            break
    if arm_obj is None:
        return None

    # Make sure the action is assigned
    action = None
    if arm_obj.animation_data and arm_obj.animation_data.action:
        action = arm_obj.animation_data.action
    elif bpy.data.actions:
        action = bpy.data.actions[0]
        if not arm_obj.animation_data:
            arm_obj.animation_data_create()
        arm_obj.animation_data.action = action

    print(f"  armature: {arm_obj.name}, action: {action.name if action else 'NONE'}")
    if action and hasattr(action, 'fcurves'):
        # List first 5 f-curve data_paths
        fcurves = list(action.fcurves)
        print(f"  fcurves count={len(fcurves)}")
        for fc in fcurves[:5]:
            print(f"    fc: {fc.data_path}[{fc.array_index}]")

    pbones = list(arm_obj.pose.bones)
    print(f"  pose bones count={len(pbones)}")
    print(f"  first 5 bone names: {[pb.name for pb in pbones[:5]]}")

    scene = bpy.context.scene
    results = {}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        snapshot = {}
        for pb in pbones:
            mw = arm_obj.matrix_world @ pb.matrix
            loc = mw.translation
            snapshot[pb.name] = (round(loc.x, 4), round(loc.y, 4), round(loc.z, 4))
        results[f] = snapshot
    return results


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if len(args) < 1:
        print("Usage: blender --background --python blender_animdiff.py -- file1.fbx [file2.fbx ...]")
        sys.exit(1)

    all_samples = {}
    for fbx in args:
        if not os.path.isfile(fbx):
            print(f"SKIP (missing): {fbx}")
            continue
        print(f"\n=== Sampling {os.path.basename(fbx)} ===")
        samples = sample_fbx(fbx)
        all_samples[os.path.basename(fbx)] = samples
        if not samples:
            print("  NO ARMATURE FOUND")
            continue

        # Identify bones that move WITHIN this file (across frames)
        bone_names = list(samples[1].keys())
        moving_bones = []
        for name in bone_names:
            positions = {samples[f][name] for f in (1, 20, 40, 60, 78)}
            if len(positions) > 1:
                moving_bones.append(name)
        print(f"  bones that MOVE during animation: {len(moving_bones)} / {len(bone_names)}")
        if moving_bones:
            print(f"    sample moving bones: {moving_bones[:10]}")
            # Show trajectory of first 3 moving bones
            for name in moving_bones[:3]:
                print(f"    {name}:")
                for f in (1, 20, 40, 60, 78):
                    print(f"      frame {f}: {samples[f][name]}")
        else:
            print("  !!! NO BONES ANIMATE - f-curves are not driving the rig !!!")

    # Cross-file diff (first 5 bones that exist in all files)
    print("\n\n===== CROSS-FILE DIFF =====")
    file_names = list(all_samples.keys())
    if len(file_names) < 2:
        return

    # Find bones common to all files
    common = None
    for fname in file_names:
        if all_samples[fname] is None:
            continue
        snap = all_samples[fname][1]
        names = set(snap.keys())
        common = names if common is None else common & names
    if not common:
        print("No common bones found across files")
        return

    common_list = sorted(common)[:10]
    for frame in (1, 20, 40, 60, 78):
        print(f"\n--- Frame {frame} ---")
        ref_fname = file_names[0]
        ref_snap = all_samples[ref_fname][frame]
        all_identical = True
        for bone_name in common_list:
            ref_pos = ref_snap.get(bone_name)
            if ref_pos is None:
                continue
            row = f"  {bone_name:25s} {ref_fname[:40]:40s} {ref_pos}"
            diffs = []
            for fname in file_names[1:]:
                snap = all_samples[fname][frame]
                other_pos = snap.get(bone_name)
                if other_pos and other_pos != ref_pos:
                    diffs.append(f"{fname[:40]}:{other_pos}")
                    all_identical = False
            if diffs:
                row += "  DIFF: " + " | ".join(diffs)
            print(row)
        if all_identical:
            print(f"  ALL FILES IDENTICAL AT FRAME {frame}")
        else:
            print(f"  FILES DIFFER AT FRAME {frame}")


if __name__ == "__main__":
    main()
