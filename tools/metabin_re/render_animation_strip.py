"""Render ONE FBX at MANY frames to prove animation plays (or doesn't).

If frames 1, 20, 40, 60 of the SAME file look different -> animation works.
If all frames look the same -> animation data isn't applied.
"""

import sys
import os


def render_strip(fbx_path, frames):
    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    mesh_obj = None
    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "MESH": mesh_obj = obj
        elif obj.type == "ARMATURE": arm_obj = obj
    if not mesh_obj:
        return

    scene = bpy.context.scene

    # Camera at a FIXED position looking at a human-height center
    bpy.ops.object.camera_add(location=(2.5, -3.5, 1.0))
    cam = bpy.context.object
    cam.data.lens = 50
    target = Vector((0, 0, 0.85))
    direction = target - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam

    bpy.ops.object.light_add(type="SUN", location=(3, -3, 5))
    bpy.context.object.data.energy = 4.0

    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.type = "SOLID"
    scene.display.shading.color_type = "MATERIAL"
    scene.render.resolution_x = 400
    scene.render.resolution_y = 500
    scene.render.image_settings.file_format = "PNG"

    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        out_path = fbx_path + f".strip_f{f:03d}.png"
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
        print(f"  wrote {out_path}  (mesh bb z={mesh_obj.bound_box[0][2]:.2f}..{mesh_obj.bound_box[6][2]:.2f})")

    # Also print pose bone rotation of Bip01 at each frame to prove
    # the animation IS reaching Blender's pose system
    if arm_obj:
        bip01 = None
        for pb in arm_obj.pose.bones:
            if pb.name == "Bip01":
                bip01 = pb
                break
        if bip01:
            print("\n  Bip01 rotation quaternion per frame:")
            for f in frames:
                scene.frame_set(f)
                bpy.context.view_layer.update()
                q = bip01.rotation_quaternion
                print(f"    f={f:4d}  q=({q.x:+.4f}, {q.y:+.4f}, {q.z:+.4f}, {q.w:+.4f})")


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if not args:
        print("Usage: blender --background --python render_animation_strip.py -- <fbx>")
        sys.exit(1)
    for fbx in args:
        print(f"\n=== {os.path.basename(fbx)} ===")
        render_strip(fbx, frames=[1, 20, 40, 60])


if __name__ == "__main__":
    main()
