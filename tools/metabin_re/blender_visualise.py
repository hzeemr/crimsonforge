"""Render an FBX as a PNG by converting bones to visible mesh geometry.

Run via:

    blender --background --python blender_visualise.py -- <fbx>

Workbench engine doesn't draw armature overlays in headless mode and
OpenGL render needs a GPU context that background Blender lacks. This
script side-steps the problem by:

  1. Importing the FBX
  2. For every bone, creating a thin cylinder mesh from head -> tail
  3. Adding a sphere at the head as a visible joint
  4. Rendering with Workbench (which DOES render meshes)

The result is a rasterised picture showing exactly where each bone is.
Useful for diagnosing "spike at origin" vs "proper human skeleton"
problems without needing a GPU-equipped Blender session.
"""

import sys
import os
import math


def visualise(fbx_path):
    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx_path)

    arm_obj = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            arm_obj = obj
            break
    if not arm_obj:
        print(f"  no armature in {fbx_path}")
        return

    # Advance to a mid-animation frame so we capture the POSED skeleton,
    # not the rest pose. The user sees identical bones across all 4 FBX
    # files at rest pose because they share the phm_01.pab skeleton —
    # the differentiation happens during animation.
    scene = bpy.context.scene
    target_frame = 30
    if scene.frame_end >= target_frame:
        scene.frame_set(target_frame)
        print(f"  posing at frame {target_frame}")
    else:
        scene.frame_set(scene.frame_end)
        print(f"  posing at last frame {scene.frame_end}")
    bpy.context.view_layer.update()

    # Read POSE bones (animated transforms) instead of edit_bones (rest)
    bone_data = []
    for pb in arm_obj.pose.bones:
        # pb.head/pb.tail are in armature LOCAL space; transform to world
        head_w = arm_obj.matrix_world @ pb.head
        tail_w = arm_obj.matrix_world @ pb.tail
        bone_data.append((pb.name, head_w, tail_w))

    # Compute bbox so we can scale the joint spheres relative to size
    all_pts = [hw for _, hw, _ in bone_data] + [tw for _, _, tw in bone_data]
    if not all_pts:
        return
    min_x = min(p.x for p in all_pts); max_x = max(p.x for p in all_pts)
    min_y = min(p.y for p in all_pts); max_y = max(p.y for p in all_pts)
    min_z = min(p.z for p in all_pts); max_z = max(p.z for p in all_pts)
    extent = max(max_x - min_x, max_y - min_y, max_z - min_z)
    sphere_r = max(extent * 0.012, 0.01)
    cyl_r = max(extent * 0.004, 0.003)

    # Use a FIXED bbox center across all files so framing is identical
    # — that way the visual difference between files is the SHAPE of
    # the rig, not the camera framing.
    cx = 0.0
    cy = 0.0
    cz = 0.85   # roughly chest height for a 1.7m human
    extent = max(extent, 1.5)

    print(f"  bbox X=[{min_x:.3f},{max_x:.3f}] Y=[{min_y:.3f},{max_y:.3f}] Z=[{min_z:.3f},{max_z:.3f}] bones={len(bone_data)}")

    # Materials per bone-class for color coding
    mat_root = bpy.data.materials.new("RootBone")
    mat_root.diffuse_color = (1.0, 0.2, 0.2, 1.0)
    mat_normal = bpy.data.materials.new("NormalBone")
    mat_normal.diffuse_color = (0.3, 0.6, 1.0, 1.0)
    mat_ik = bpy.data.materials.new("IKBone")
    mat_ik.diffuse_color = (1.0, 1.0, 0.0, 1.0)

    for name, head_w, tail_w in bone_data:
        # Joint sphere at head
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=sphere_r, location=head_w, segments=8, ring_count=8,
        )
        sphere = bpy.context.object
        sphere.name = f"joint_{name}"
        if name.startswith("B_TL_") or name.startswith("B_MoveControl"):
            sphere.data.materials.append(mat_ik)
        elif name == "Bip01":
            sphere.data.materials.append(mat_root)
        else:
            sphere.data.materials.append(mat_normal)

        # Cylinder along bone direction
        diff = tail_w - head_w
        length = diff.length
        if length < 1e-5:
            continue
        midpoint = head_w + diff * 0.5
        bpy.ops.mesh.primitive_cylinder_add(
            radius=cyl_r, depth=length, location=midpoint, vertices=6,
        )
        cyl = bpy.context.object
        cyl.name = f"bone_{name}"
        # Rotate cylinder to align with diff vector. Default cylinder is
        # along +Z; quaternion that rotates +Z onto diff/length.
        z_axis = Vector((0, 0, 1))
        quat = z_axis.rotation_difference(diff.normalized())
        cyl.rotation_mode = "QUATERNION"
        cyl.rotation_quaternion = quat
        cyl.data.materials.append(mat_normal if not name.startswith("B_TL_") else mat_ik)

    # Hide the original armature
    arm_obj.hide_render = True

    # Camera + light
    scene = bpy.context.scene
    cam_loc = (cx + extent * 1.2, cy - extent * 1.5, cz + extent * 0.3)
    bpy.ops.object.camera_add(location=cam_loc)
    cam = bpy.context.object
    cam.data.lens = 50
    direction = Vector((cx, cy, cz)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam

    bpy.ops.object.light_add(type="SUN", location=(cx + extent, cy - extent, cz + extent))
    bpy.context.object.data.energy = 5.0

    # Render
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.type = "SOLID"
    scene.display.shading.color_type = "MATERIAL"
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 768
    scene.render.image_settings.file_format = "PNG"
    out_path = fbx_path + ".visual.png"
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
        print("Usage: blender --background --python blender_visualise.py -- <fbx>")
        sys.exit(1)
    for fbx in args:
        if os.path.isfile(fbx):
            print(f"\n=== {os.path.basename(fbx)} ===")
            visualise(fbx)
        else:
            print(f"SKIP: {fbx}")


if __name__ == "__main__":
    main()
