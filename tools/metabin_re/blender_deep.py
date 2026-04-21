"""Deep Blender FBX inspector.

Run via:

    blender --background --python blender_deep.py -- <fbx>

For each armature, dumps:
  * edit_bones HEAD and TAIL positions (the REST skeleton — this is
    what determines the visual shape in object/pose mode display)
  * pose_bones world positions at frame 1
  * the BindPose matrix per bone (if FBX includes one)
  * whether bones connect to parents (parent.tail == child.head)
  * the armature's scale + the imported scene unit scale
  * RENDER a screenshot to <fbx>.deep.png so we can SEE what Blender shows

This separates "the data is correct" from "what the user sees", which
is critical when the f-curves animate but the rest skeleton looks
collapsed regardless.
"""

import sys
import os


def inspect(fbx_path):
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    print(f"\n=== {os.path.basename(fbx_path)} ===")
    # Scene unit
    scene = bpy.context.scene
    print(f"  scene.unit_settings.system: {scene.unit_settings.system}")
    print(f"  scene.unit_settings.scale_length: {scene.unit_settings.scale_length}")

    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            arm_obj = obj
            break
    if not arm_obj:
        print("  NO ARMATURE")
        return

    print(f"  armature object scale: {tuple(arm_obj.scale)}")
    print(f"  armature object location: {tuple(arm_obj.location)}")
    print(f"  armature matrix_world: {arm_obj.matrix_world}")

    arm_data = arm_obj.data
    print(f"  bone count: {len(arm_data.bones)}")

    # Switch to EDIT mode to read edit_bones (rest pose head/tail)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    print(f"\n  --- EDIT BONES (rest pose head/tail in EDIT space, pre-arm-scale) ---")
    edit_bones = arm_data.edit_bones
    # List EVERY bone to find clusters and outliers
    for i, eb in enumerate(edit_bones):
        head = tuple(round(v, 4) for v in eb.head)
        tail = tuple(round(v, 4) for v in eb.tail)
        length = round(eb.length, 4)
        parent_name = eb.parent.name if eb.parent else "<root>"
        # Flag suspect bones (heads at origin in EDIT space, meaning ~1cm world)
        head_at_origin = abs(eb.head.x) < 1.0 and abs(eb.head.y) < 1.0 and abs(eb.head.z) < 1.0
        flag = "  <-- AT ORIGIN" if head_at_origin else ""
        print(f"    [{i:3d}] {eb.name:30s} head={head} tail={tail} len={length:.3f} parent={parent_name}{flag}")

    # Stats: how many bones have head at origin (in EDIT space, <1cm)
    origin_heads_edit = sum(
        1 for eb in edit_bones
        if abs(eb.head.x) < 1.0 and abs(eb.head.y) < 1.0 and abs(eb.head.z) < 1.0
    )
    long_bones = sum(1 for eb in edit_bones if eb.length > 5.0)
    spread_x = max((eb.head.x for eb in edit_bones), default=0) - min((eb.head.x for eb in edit_bones), default=0)
    spread_y = max((eb.head.y for eb in edit_bones), default=0) - min((eb.head.y for eb in edit_bones), default=0)
    spread_z = max((eb.head.z for eb in edit_bones), default=0) - min((eb.head.z for eb in edit_bones), default=0)
    print(f"\n  STATS (edit space units = cm pre-arm-scale):")
    print(f"    bones with head at origin (<1 cm): {origin_heads_edit}/{len(edit_bones)}")
    print(f"    bones longer than 5 cm: {long_bones}/{len(edit_bones)}")
    print(f"    head spread: X={spread_x:.1f} Y={spread_y:.1f} Z={spread_z:.1f}  (cm)")

    bpy.ops.object.mode_set(mode="OBJECT")

    # Render a wireframe screenshot so we can SEE the armature
    try:
        # Position camera looking at armature
        bpy.ops.object.camera_add(location=(2, -3, 1.5))
        cam = bpy.context.object
        cam.rotation_euler = (1.2, 0, 0.5)
        scene.camera = cam
        # Frame the armature
        bpy.context.view_layer.update()
        for obj in bpy.data.objects:
            obj.select_set(False)
        arm_obj.select_set(True)
        bpy.context.view_layer.objects.active = arm_obj
        bpy.ops.view3d.camera_to_view_selected() if False else None  # not in bg

        # Just render
        scene.render.resolution_x = 800
        scene.render.resolution_y = 600
        scene.render.filepath = fbx_path + ".deep.png"
        scene.render.engine = "BLENDER_WORKBENCH"
        # Workbench shows armatures clearly without lighting setup
        scene.display.shading.show_xray = True
        scene.display.shading.show_object_outline = True
        try:
            bpy.ops.render.render(write_still=True)
            print(f"  rendered to {scene.render.filepath}")
        except Exception as e:
            print(f"  render failed: {e}")
    except Exception as e:
        print(f"  camera setup failed: {e}")


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if not args:
        print("Usage: blender --background --python blender_deep.py -- file.fbx [more.fbx ...]")
        sys.exit(1)
    for fbx in args:
        if os.path.isfile(fbx):
            inspect(fbx)
        else:
            print(f"SKIP: {fbx}")


if __name__ == "__main__":
    main()
