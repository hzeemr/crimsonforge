"""Render 3 FBX files at the SAME frame in a single image.

Puts 3 armatures in one scene, offset horizontally. This way the
user can compare poses side-by-side at identical moments — if the
animations ARE different, the 3 figures will have clearly different
body poses in the final render.
"""

import sys
import os


def main():
    import bpy
    from mathutils import Vector

    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if len(args) < 3:
        print("Usage: side_by_side.py -- <fbx1> <fbx2> <fbx3>")
        sys.exit(1)

    fbx_files = args[:3]
    out_path = args[3] if len(args) > 3 else r"C:\Users\hzeem\Pictures\er_test4\side_by_side.png"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    offsets = [(-3.5, 0, 0), (0, 0, 0), (3.5, 0, 0)]

    for i, fbx in enumerate(fbx_files):
        # Clear selection
        for obj in bpy.data.objects:
            obj.select_set(False)
        before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(filepath=fbx)
        new_objs = [o for o in bpy.data.objects if o not in before]
        # Offset all imported objects
        for obj in new_objs:
            obj.location.x += offsets[i][0]
            obj.location.y += offsets[i][1]
            obj.location.z += offsets[i][2]

    # Advance to mid-animation frame
    scene.frame_set(60)
    bpy.context.view_layer.update()

    # Camera looking at the row of characters
    bpy.ops.object.camera_add(location=(0, -6, 1.2))
    cam = bpy.context.object
    cam.data.lens = 35
    cam.rotation_euler = (1.3, 0, 0)
    scene.camera = cam

    bpy.ops.object.light_add(type="SUN", location=(3, -3, 5))
    bpy.context.object.data.energy = 5.0

    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.type = "SOLID"
    scene.display.shading.color_type = "MATERIAL"
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 600
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = out_path
    bpy.ops.render.render(write_still=True)
    print(f"\n  wrote {out_path}")


if __name__ == "__main__":
    main()
