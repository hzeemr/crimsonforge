"""Render the FBX placeholder mesh at multiple frames so we can SEE
the body deforming with the animation.

Run via:
    blender --background --python render_mesh.py -- <fbx>

Renders 3 PNGs per FBX (frame 1, frame mid, frame end) so the user
can directly compare poses across files. Output written next to the
FBX with .frame_<n>.png suffix.
"""

import sys
import os


def render_one(fbx_path, frames=(1, None, None)):
    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    # Find mesh and armature
    mesh_obj = None
    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            mesh_obj = obj
        elif obj.type == "ARMATURE":
            arm_obj = obj

    if not mesh_obj:
        print(f"  no mesh object in {fbx_path}")
        return
    print(f"  mesh: {mesh_obj.name}, vertices: {len(mesh_obj.data.vertices)}")
    print(f"  modifiers on mesh: {[m.type for m in mesh_obj.modifiers]}")
    if arm_obj:
        print(f"  armature bones: {len(arm_obj.data.bones)}")

    scene = bpy.context.scene
    print(f"  scene frame range: [{scene.frame_start}, {scene.frame_end}]")

    # Resolve which frames to render
    frame_end = scene.frame_end
    actual_frames = []
    for f in frames:
        if f is None:
            continue
        if f <= frame_end:
            actual_frames.append(f)
    # Add mid + end if not already there
    mid = max(2, frame_end // 2)
    if mid not in actual_frames and mid <= frame_end:
        actual_frames.append(mid)
    if frame_end not in actual_frames and frame_end >= 1:
        actual_frames.append(frame_end)

    # Camera + light positioned to look at chest height
    bpy.ops.object.camera_add(location=(2.5, -3.0, 1.0))
    cam = bpy.context.object
    cam.data.lens = 50
    look_at = Vector((0, 0, 0.85))
    direction = look_at - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam

    bpy.ops.object.light_add(type="SUN", location=(3, -3, 5))
    bpy.context.object.data.energy = 4.0

    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.type = "SOLID"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = True
    scene.render.resolution_x = 800
    scene.render.resolution_y = 600
    scene.render.image_settings.file_format = "PNG"

    for f in sorted(set(actual_frames)):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        out_path = fbx_path + f".frame_{f:03d}.png"
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
        print(f"  wrote {out_path}")


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if not args:
        print("Usage: blender --background --python render_mesh.py -- <fbx>")
        sys.exit(1)
    for fbx in args:
        if os.path.isfile(fbx):
            print(f"\n=== {os.path.basename(fbx)} ===")
            render_one(fbx)
        else:
            print(f"SKIP: {fbx}")


if __name__ == "__main__":
    main()
