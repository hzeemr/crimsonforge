"""Blender import-and-inspect script.

Run via:

    blender --background --python blender_inspect.py -- <fbx_path>

Imports the FBX, then dumps what Blender actually read:
  * armatures + bone counts
  * animation actions + f-curve counts
  * first frame / last frame / total keyframes
  * any import warnings

If the output shows zero f-curves, Blender silently dropped our
animation data.
"""

import sys, os


def main():
    import bpy

    # argv after '--'
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if not args:
        print("ERROR: no FBX path supplied")
        sys.exit(1)
    fbx_path = args[0]
    if not os.path.isfile(fbx_path):
        print(f"ERROR: {fbx_path} does not exist")
        sys.exit(1)

    # Clean up default scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    print(f"=== Importing {fbx_path} ===")
    try:
        bpy.ops.import_scene.fbx(filepath=fbx_path)
    except Exception as e:
        print(f"IMPORT ERROR: {e}")
        sys.exit(2)

    # Report what got loaded
    print("\n--- Scene objects ---")
    for obj in bpy.data.objects:
        print(f"  {obj.type:10s}  {obj.name!r}")

    print("\n--- Armatures ---")
    for arm in bpy.data.armatures:
        print(f"  {arm.name!r}: {len(arm.bones)} bones")

    print("\n--- Actions (animations) ---")
    for act in bpy.data.actions:
        # Blender 4.4+ action API: f-curves live in layers/slots.
        fcurves = []
        if hasattr(act, "fcurves"):
            fcurves = list(act.fcurves)
        elif hasattr(act, "layers"):
            for layer in act.layers:
                for strip in layer.strips:
                    if hasattr(strip, "channelbag"):
                        cb = strip.channelbag(act.slots[0] if act.slots else None)
                        if cb:
                            fcurves.extend(cb.fcurves)
        try:
            fr = act.frame_range
            print(f"  {act.name!r}: {len(fcurves)} f-curves, frame_range={fr[0]:.1f} .. {fr[1]:.1f}")
        except Exception:
            print(f"  {act.name!r}: {len(fcurves)} f-curves")
        for fc in fcurves[:3]:
            kp = list(fc.keyframe_points)
            print(f"    {fc.data_path}[{fc.array_index}]: {len(kp)} keyframes")
            if kp:
                print(f"      first 3: {[(p.co[0], round(p.co[1], 2)) for p in kp[:3]]}")

    print("\n--- Animation data on objects ---")
    for obj in bpy.data.objects:
        if obj.animation_data and obj.animation_data.action:
            print(f"  {obj.name} -> action {obj.animation_data.action.name!r}")
        else:
            print(f"  {obj.name} -> NO animation data" if obj.type == "ARMATURE" else "")


if __name__ == "__main__":
    main()
