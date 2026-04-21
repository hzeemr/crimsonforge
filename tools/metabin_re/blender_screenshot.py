"""Render an FBX as a PNG so we can SEE what Blender shows.

Run via:

    blender --background --python blender_screenshot.py -- <fbx>

Renders an OpenGL viewport screenshot of the armature, framed to fit
all bones, written next to the FBX with .preview.png suffix.
"""

import sys
import os


def render_fbx(fbx_path):
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

    # Make sure pose-mode display shows the bones
    arm_obj.data.display_type = "OCTAHEDRAL"
    arm_obj.show_in_front = True

    # Compute bone bounding box in WORLD space so we can frame the camera
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = arm_obj.data.edit_bones
    points_world = []
    for eb in edit_bones:
        head_world = arm_obj.matrix_world @ Vector(eb.head)
        tail_world = arm_obj.matrix_world @ Vector(eb.tail)
        points_world.append(head_world)
        points_world.append(tail_world)
    bpy.ops.object.mode_set(mode="OBJECT")

    if not points_world:
        return

    min_x = min(p.x for p in points_world)
    max_x = max(p.x for p in points_world)
    min_y = min(p.y for p in points_world)
    max_y = max(p.y for p in points_world)
    min_z = min(p.z for p in points_world)
    max_z = max(p.z for p in points_world)

    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    cz = (min_z + max_z) / 2
    extent = max(max_x - min_x, max_y - min_y, max_z - min_z)
    print(f"  bbox center=({cx:.3f},{cy:.3f},{cz:.3f}) extent={extent:.3f}m")
    print(f"  bbox X=[{min_x:.3f},{max_x:.3f}] Y=[{min_y:.3f},{max_y:.3f}] Z=[{min_z:.3f},{max_z:.3f}]")

    # Set up the scene
    scene = bpy.context.scene

    # Camera at front-side viewpoint
    bpy.ops.object.camera_add(
        location=(cx + extent * 1.5, cy - extent * 1.5, cz + extent * 0.5)
    )
    cam = bpy.context.object
    cam.data.lens = 35
    # Point camera at center
    direction = Vector((cx, cy, cz)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam

    # Light at same place
    bpy.ops.object.light_add(
        type="SUN", location=(cx + extent, cy - extent, cz + extent)
    )

    # Use workbench engine - works without proper materials
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.type = "SOLID"

    # Render
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 768
    scene.render.image_settings.file_format = "PNG"
    out_path = fbx_path + ".preview.png"
    scene.render.filepath = out_path

    # Try OpenGL viewport render — this DOES draw armatures (which the
    # raw render engine ignores because armatures are overlays). Falls
    # back to standard render if OpenGL isn't available in headless mode.
    try:
        # Need a temporary 3D viewport for OpenGL render
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                with bpy.context.temp_override(area=area):
                    bpy.ops.render.opengl(write_still=True, view_context=True)
                break
        else:
            # No 3D view (truly headless) — write coordinates as a text
            # fallback so we at least know what's there
            print("  no VIEW_3D area in background mode; writing data dump instead")
            fallback = fbx_path + ".preview.txt"
            with open(fallback, "w") as f:
                f.write(f"Bone bbox: X=[{min_x:.3f},{max_x:.3f}] "
                        f"Y=[{min_y:.3f},{max_y:.3f}] "
                        f"Z=[{min_z:.3f},{max_z:.3f}]\n")
                f.write(f"Bone count: {len(arm_obj.data.bones)}\n")
            return
    except Exception as e:
        print(f"  opengl render failed: {e}")
        bpy.ops.render.render(write_still=True)
    print(f"  wrote {out_path}")


def main():
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    if not args:
        print("Usage: blender --background --python blender_screenshot.py -- <fbx> [more]")
        sys.exit(1)
    for fbx in args:
        if os.path.isfile(fbx):
            print(f"\n=== {os.path.basename(fbx)} ===")
            render_fbx(fbx)
        else:
            print(f"SKIP: {fbx}")


if __name__ == "__main__":
    main()
