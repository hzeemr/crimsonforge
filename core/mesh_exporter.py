"""OBJ and binary FBX 7.4 exporter for parsed mesh data.

Exports ParsedMesh objects from mesh_parser to standard 3D formats:
  - OBJ + MTL (Wavefront, universally supported)
  - FBX binary 7.4 (Blender, Maya, 3ds Max, Unity, Unreal Engine)

No external libraries required — pure Python binary FBX writer.
"""

from __future__ import annotations

import io
import os
import struct
import zlib
import math
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.mesh_parser import ParsedMesh, SubMesh
from utils.logger import get_logger

logger = get_logger("core.mesh_exporter")


# ═══════════════════════════════════════════════════════════════════════
#  OBJ EXPORTER
# ═══════════════════════════════════════════════════════════════════════

def export_obj(mesh: ParsedMesh, output_dir: str, name: str = "",
               split_submeshes: bool = False, scale: float = 1.0) -> list[str]:
    """Export mesh to OBJ + MTL files.

    Args:
        mesh: Parsed mesh data.
        output_dir: Directory to write files.
        name: Base filename (without extension). Defaults to mesh path stem.
        split_submeshes: If True, write each submesh as a separate OBJ file.
        scale: Scale factor applied to all vertices.

    Returns:
        List of output file paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    base = name or Path(mesh.path).stem

    if split_submeshes:
        return _export_obj_split(mesh, output_dir, base, scale)

    obj_path = os.path.join(output_dir, f"{base}.obj")
    mtl_path = os.path.join(output_dir, f"{base}.mtl")

    # Write MTL
    _write_mtl(mtl_path, mesh.submeshes)

    # Write OBJ
    lines = [
        f"# Crimson Desert Mesh — {base}",
        f"# {len(mesh.submeshes)} submesh(es), {mesh.total_vertices} verts, {mesh.total_faces} faces",
        f"# Exported by CrimsonForge",
        f"# source_path: {mesh.path}",
        f"# source_format: {mesh.format}",
        f"mtllib {os.path.basename(mtl_path)}",
        "",
    ]

    vert_offset = 1  # OBJ is 1-based
    uv_offset = 1
    normal_offset = 1

    for sm in mesh.submeshes:
        mat = sm.material or sm.name
        lines.append(f"o {sm.name}")
        lines.append(f"usemtl {mat}")

        for x, y, z in sm.vertices:
            lines.append(f"v {x * scale:.6f} {y * scale:.6f} {z * scale:.6f}")

        for u, v in sm.uvs:
            lines.append(f"vt {u:.6f} {1.0 - v:.6f}")

        for nx, ny, nz in sm.normals:
            lines.append(f"vn {nx:.4f} {ny:.4f} {nz:.4f}")

        lines.append("s 1")

        has_uv = bool(sm.uvs)
        has_normals = bool(sm.normals)

        for a, b, c in sm.faces:
            va, vb, vc = a + vert_offset, b + vert_offset, c + vert_offset
            if has_uv and has_normals:
                ta, tb, tc = a + uv_offset, b + uv_offset, c + uv_offset
                na, nb, nc = a + normal_offset, b + normal_offset, c + normal_offset
                lines.append(f"f {va}/{ta}/{na} {vb}/{tb}/{nb} {vc}/{tc}/{nc}")
            elif has_uv:
                ta, tb, tc = a + uv_offset, b + uv_offset, c + uv_offset
                lines.append(f"f {va}/{ta} {vb}/{tb} {vc}/{tc}")
            elif has_normals:
                na, nb, nc = a + normal_offset, b + normal_offset, c + normal_offset
                lines.append(f"f {va}//{na} {vb}//{nb} {vc}//{nc}")
            else:
                lines.append(f"f {va} {vb} {vc}")

        lines.append("")
        vert_offset += len(sm.vertices)
        uv_offset += len(sm.uvs)
        normal_offset += len(sm.normals)

    with open(obj_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("Exported OBJ: %s (%d verts, %d faces)", obj_path,
                mesh.total_vertices, mesh.total_faces)
    return [obj_path, mtl_path]


def _export_obj_split(mesh, output_dir, base, scale):
    """Export each submesh as a separate OBJ file."""
    results = []
    for i, sm in enumerate(mesh.submeshes):
        sub_name = f"{base}_mesh{i:02d}"
        sub_mesh = ParsedMesh(
            path=mesh.path, format=mesh.format,
            bbox_min=mesh.bbox_min, bbox_max=mesh.bbox_max,
            submeshes=[sm],
            total_vertices=len(sm.vertices), total_faces=len(sm.faces),
            has_uvs=bool(sm.uvs),
        )
        results.extend(export_obj(sub_mesh, output_dir, sub_name, scale=scale))
    return results


def _write_mtl(path, submeshes):
    """Write a Wavefront MTL material file."""
    seen = set()
    lines = ["# Crimson Desert Materials — CrimsonForge", ""]
    for sm in submeshes:
        n = sm.material or sm.name
        if n in seen:
            continue
        seen.add(n)
        lines.extend([
            f"newmtl {n}",
            "Ka 1.000 1.000 1.000",
            "Kd 0.800 0.800 0.800",
            "Ks 0.100 0.100 0.100",
            "Ns 50.000",
            "d 1.000",
            "illum 2",
        ])
        if sm.texture:
            lines.append(f"map_Kd {sm.texture}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════
#  FBX BINARY 7.4 EXPORTER
# ═══════════════════════════════════════════════════════════════════════

class _FbxId:
    """Wrapper for FBX unique IDs (always int64)."""
    def __init__(self, val): self.val = val


def _fbx_prop(v):
    """Encode a single FBX property value."""
    if isinstance(v, bool):
        return b"C" + struct.pack("B", int(v))
    if isinstance(v, _FbxId):
        return b"L" + struct.pack("<q", v.val)
    if isinstance(v, int):
        if -2147483648 <= v <= 2147483647:
            return b"I" + struct.pack("<i", v)
        return b"L" + struct.pack("<q", v)
    if isinstance(v, float):
        return b"D" + struct.pack("<d", v)
    if isinstance(v, str):
        e = v.encode("utf-8")
        return b"S" + struct.pack("<I", len(e)) + e
    if isinstance(v, bytes):
        return b"R" + struct.pack("<I", len(v)) + v
    if isinstance(v, list):
        if not v:
            return b"i" + struct.pack("<III", 0, 0, 0)
        if isinstance(v[0], float):
            raw = struct.pack(f"<{len(v)}d", *v)
            cmp = zlib.compress(raw)
            enc = 1 if len(cmp) < len(raw) else 0
            cl = len(cmp) if enc else len(raw)
            return b"d" + struct.pack("<III", len(v), enc, cl) + (cmp if enc else raw)
        # Integer array. Promote to int64 ('l') when any value
        # overflows int32 — the FBX KTime ticks used by animation
        # curves routinely run into the 10-digit range.
        needs_i64 = any((x > 2147483647 or x < -2147483648) for x in v)
        if needs_i64:
            raw = struct.pack(f"<{len(v)}q", *v)
            cmp = zlib.compress(raw)
            enc = 1 if len(cmp) < len(raw) else 0
            cl = len(cmp) if enc else len(raw)
            return b"l" + struct.pack("<III", len(v), enc, cl) + (cmp if enc else raw)
        raw = struct.pack(f"<{len(v)}i", *v)
        cmp = zlib.compress(raw)
        enc = 1 if len(cmp) < len(raw) else 0
        cl = len(cmp) if enc else len(raw)
        return b"i" + struct.pack("<III", len(v), enc, cl) + (cmp if enc else raw)
    raise TypeError(f"Unsupported FBX property type: {type(v)}")


def _fbx_node(buf: io.BytesIO, name: str, props=None, children=None):
    """Write an FBX binary node with correct absolute end offsets.

    Uses placeholder + patch approach: writes a placeholder end_offset,
    then patches it after all children are written to the same buffer.
    """
    nb = name.encode("ascii")
    props = props or []
    children = children or []

    # Serialize properties
    pb = io.BytesIO()
    for p in props:
        pb.write(_fbx_prop(p))
    pb = pb.getvalue()

    # Write node header with placeholder end_offset
    end_pos_loc = buf.tell()  # remember where end_offset is stored
    buf.write(struct.pack("<I", 0))  # placeholder — patched below
    buf.write(struct.pack("<I", len(props)))
    buf.write(struct.pack("<I", len(pb)))
    buf.write(struct.pack("B", len(nb)))
    buf.write(nb)
    buf.write(pb)

    # Write children directly to the SAME buffer (so offsets are absolute)
    for child_fn in children:
        child_fn(buf)
    if children:
        buf.write(b"\x00" * 13)  # null terminator node

    # Patch the end_offset with the actual current position
    end_offset = buf.tell()
    buf.seek(end_pos_loc)
    buf.write(struct.pack("<I", end_offset))
    buf.seek(end_offset)  # restore position


def export_fbx(mesh: ParsedMesh, output_dir: str, name: str = "",
               scale: float = 1.0) -> str:
    """Export mesh to binary FBX 7.4 file.

    Compatible with Blender 2.8+, Maya, 3ds Max, Unity 5+, Unreal Engine 4+.
    """
    os.makedirs(output_dir, exist_ok=True)
    base = name or Path(mesh.path).stem
    fbx_path = os.path.join(output_dir, f"{base}.fbx")

    buf = io.BytesIO()
    W = _fbx_node

    # Header
    buf.write(b"Kaydara FBX Binary  \x00")
    buf.write(b"\x1a\x00")
    buf.write(struct.pack("<I", 7400))  # version

    id_ctr = [3_000_000_000]

    def uid():
        id_ctr[0] += 1
        return _FbxId(id_ctr[0])

    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    # FBXHeaderExtension
    def header_ext(b):
        W(b, "FBXHeaderVersion", [1003])
        W(b, "FBXVersion", [7400])
        W(b, "Creator", ["CrimsonForge Mesh Exporter"])

    W(buf, "FBXHeaderExtension", children=[header_ext])

    # GlobalSettings
    def global_settings(b):
        def props70(b2):
            W(b2, "P", ["UpAxis", "int", "Integer", "", 1])
            W(b2, "P", ["UpAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["FrontAxis", "int", "Integer", "", 2])
            W(b2, "P", ["FrontAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["CoordAxis", "int", "Integer", "", 0])
            W(b2, "P", ["CoordAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["UnitScaleFactor", "double", "Number", "", 1.0])
        W(b, "Properties70", children=[props70])
    W(buf, "GlobalSettings", children=[global_settings])

    # Build mesh/model/material IDs
    mesh_ids = []
    model_ids = []
    mat_ids = []
    for sm in mesh.submeshes:
        mesh_ids.append(uid())
        model_ids.append(uid())
        mat_ids.append(uid())

    root_id = uid()

    # Objects
    def objects(b):
        for idx, sm in enumerate(mesh.submeshes):
            mid = mesh_ids[idx]
            mod_id = model_ids[idx]
            ma_id = mat_ids[idx]

            # Geometry node
            verts_flat = []
            for x, y, z in sm.vertices:
                verts_flat.extend([x * scale, y * scale, z * scale])

            indices_flat = []
            for a, b_idx, c in sm.faces:
                indices_flat.extend([a, b_idx, c ^ -1])  # FBX: last index XOR -1

            normals_flat = []
            for nx, ny, nz in sm.normals:
                normals_flat.extend([nx, ny, nz])

            uvs_flat = []
            uv_indices = []
            for i_v, (u, v) in enumerate(sm.uvs):
                uvs_flat.extend([u, 1.0 - v])
                uv_indices.append(i_v)

            def geom_node(b2, vf=verts_flat, iff=indices_flat, nf=normals_flat,
                          uf=uvs_flat, ui=uv_indices, sm_ref=sm, m=mid):
                def layer_elem_normal(b3, nf_=nf):
                    W(b3, "Version", [101])
                    W(b3, "Name", [""])
                    W(b3, "MappingInformationType", ["ByVertice"])
                    W(b3, "ReferenceInformationType", ["Direct"])
                    W(b3, "Normals", [nf_])

                def layer_elem_uv(b3, uf_=uf, ui_=ui):
                    W(b3, "Version", [101])
                    W(b3, "Name", ["UVMap"])
                    W(b3, "MappingInformationType", ["ByVertice"])
                    W(b3, "ReferenceInformationType", ["Direct"])
                    W(b3, "UV", [uf_])

                def layer0(b3):
                    W(b3, "Version", [100])

                    def le_normal(b4):
                        W(b4, "Type", ["LayerElementNormal"])
                        W(b4, "TypedIndex", [0])
                    W(b3, "LayerElement", children=[le_normal])

                    if uf:
                        def le_uv(b4):
                            W(b4, "Type", ["LayerElementUV"])
                            W(b4, "TypedIndex", [0])
                        W(b3, "LayerElement", children=[le_uv])

                W(b2, "Vertices", [vf])
                W(b2, "PolygonVertexIndex", [iff])

                if nf:
                    W(b2, "LayerElementNormal", [0], children=[layer_elem_normal])
                if uf:
                    W(b2, "LayerElementUV", [0], children=[layer_elem_uv])
                W(b2, "Layer", [0], children=[layer0])

            W(b, "Geometry", [mid, f"{sm.name}\x00\x01Geometry", "Mesh"],
              children=[geom_node])

            # Model node
            def model_node(b2):
                W(b2, "Version", [232])

                def props(b3):
                    W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Rotation", "Lcl Rotation", "", "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Scaling", "Lcl Scaling", "", "A", 1.0, 1.0, 1.0])
                W(b2, "Properties70", children=[props])

            W(b, "Model", [mod_id, f"{sm.name}\x00\x01Model", "Mesh"],
              children=[model_node])

            # Material node
            def mat_node(b2):
                W(b2, "Version", [102])
                W(b2, "ShadingModel", ["phong"])

                def mat_props(b3):
                    W(b3, "P", ["DiffuseColor", "Color", "", "A", 0.8, 0.8, 0.8])
                W(b2, "Properties70", children=[mat_props])

            W(b, "Material", [ma_id, f"{sm.material or sm.name}\x00\x01Material", ""],
              children=[mat_node])

    W(buf, "Objects", children=[objects])

    # Connections
    def connections(b):
        for idx in range(len(mesh.submeshes)):
            # Model → Root
            W(b, "C", ["OO", model_ids[idx], _FbxId(0)])
            # Geometry → Model
            W(b, "C", ["OO", mesh_ids[idx], model_ids[idx]])
            # Material → Model
            W(b, "C", ["OO", mat_ids[idx], model_ids[idx]])

    W(buf, "Connections", children=[connections])

    # Footer
    buf.write(b"\x00" * 13)  # null terminator

    # FBX footer
    buf.write(b"\xfa\xbc\xab\x09\xd0\xc8\xd4\x66\xb1\x76\xfb\x83\x1c\xf7\x26\x7e")  # padding
    buf.write(b"\x00" * 4)
    buf.write(struct.pack("<I", 7400))
    buf.write(b"\x00" * 120)
    buf.write(bytes([
        0xf8, 0x5a, 0x8c, 0x6a, 0xde, 0xf5, 0xd9, 0x7e,
        0xec, 0xe9, 0x0c, 0xe3, 0x75, 0x8f, 0x29, 0x0b,
    ]))

    with open(fbx_path, "wb") as f:
        f.write(buf.getvalue())

    logger.info("Exported FBX: %s (%d verts, %d faces)", fbx_path,
                mesh.total_vertices, mesh.total_faces)
    return fbx_path


def export_fbx_with_skeleton(mesh: ParsedMesh, skeleton, output_dir: str,
                              name: str = "", scale: float = 1.0) -> str:
    """Export mesh + skeleton to FBX with armature hierarchy.

    The skeleton parameter is a Skeleton object from skeleton_parser.
    Bone hierarchy is written as FBX LimbNode models connected to the
    mesh via Skin deformers. Compatible with Blender, Maya, Unity, Unreal.
    """
    from core.skeleton_parser import Skeleton

    os.makedirs(output_dir, exist_ok=True)
    base = name or Path(mesh.path).stem
    fbx_path = os.path.join(output_dir, f"{base}.fbx")

    buf = io.BytesIO()
    W = _fbx_node

    # Header
    buf.write(b"Kaydara FBX Binary  \x00")
    buf.write(b"\x1a\x00")
    buf.write(struct.pack("<I", 7400))

    id_ctr = [3_000_000_000]
    def uid():
        id_ctr[0] += 1
        return _FbxId(id_ctr[0])

    # FBXHeaderExtension
    def header_ext(b):
        W(b, "FBXHeaderVersion", [1003])
        W(b, "FBXVersion", [7400])
        W(b, "Creator", ["CrimsonForge Mesh+Skeleton Exporter"])
    W(buf, "FBXHeaderExtension", children=[header_ext])

    # GlobalSettings
    def global_settings(b):
        def props70(b2):
            W(b2, "P", ["UpAxis", "int", "Integer", "", 1])
            W(b2, "P", ["UpAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["FrontAxis", "int", "Integer", "", 2])
            W(b2, "P", ["FrontAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["CoordAxis", "int", "Integer", "", 0])
            W(b2, "P", ["CoordAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["UnitScaleFactor", "double", "Number", "", 1.0])
        W(b, "Properties70", children=[props70])
    W(buf, "GlobalSettings", children=[global_settings])

    # Build IDs
    mesh_ids, model_ids, mat_ids = [], [], []
    for sm in mesh.submeshes:
        mesh_ids.append(uid())
        model_ids.append(uid())
        mat_ids.append(uid())

    bone_model_ids = {}
    bone_attr_ids = {}
    if skeleton and skeleton.bones:
        for bone in skeleton.bones:
            bone_model_ids[bone.index] = uid()
            bone_attr_ids[bone.index] = uid()

    root_id = uid()

    # Precompute skinning data per submesh.
    # For each submesh, we collect the set of (bone_index, [(vertex_idx, weight), ...])
    # entries. Only bones that actually influence at least one vertex in the
    # submesh get a Cluster — FBX importers tolerate unused bones but empty
    # clusters confuse some importers.
    skin_ids: list[_FbxId | None] = []          # one per submesh; None if no bones used
    cluster_ids: list[dict[int, _FbxId]] = []    # per submesh: {bone_index: cluster_id}
    cluster_data: list[dict[int, list[tuple[int, float]]]] = []  # per submesh: {bone_index: [(vi, w)]}

    for sm in mesh.submeshes:
        per_bone: dict[int, list[tuple[int, float]]] = {}
        if skeleton and skeleton.bones:
            # SubMesh.bone_indices / bone_weights are per-vertex tuples.
            # Missing entries just mean "no skinning on this vertex".
            for vi, (bones, weights) in enumerate(zip(sm.bone_indices, sm.bone_weights)):
                for b_idx, w in zip(bones, weights):
                    if w <= 0.0:
                        continue
                    # Only accept bones the skeleton actually declares.
                    if 0 <= b_idx < len(skeleton.bones):
                        per_bone.setdefault(int(b_idx), []).append((vi, float(w)))
        if per_bone:
            skin_ids.append(uid())
            cluster_ids.append({b_idx: uid() for b_idx in per_bone})
        else:
            skin_ids.append(None)
            cluster_ids.append({})
        cluster_data.append(per_bone)

    # Objects
    def objects(b):
        # Mesh geometry + model + material (same as before)
        for idx, sm in enumerate(mesh.submeshes):
            mid = mesh_ids[idx]
            mod_id = model_ids[idx]
            ma_id = mat_ids[idx]

            verts_flat = []
            for x, y, z in sm.vertices:
                verts_flat.extend([x * scale, y * scale, z * scale])

            indices_flat = []
            for a, b_idx, c in sm.faces:
                indices_flat.extend([a, b_idx, c ^ -1])

            normals_flat = []
            for nx, ny, nz in sm.normals:
                normals_flat.extend([nx, ny, nz])

            def geom_node(b2, vf=verts_flat, iff=indices_flat, nf=normals_flat):
                def layer_elem_normal(b3, nf_=nf):
                    W(b3, "Version", [101])
                    W(b3, "Name", [""])
                    W(b3, "MappingInformationType", ["ByVertice"])
                    W(b3, "ReferenceInformationType", ["Direct"])
                    W(b3, "Normals", [nf_])

                def layer0(b3):
                    W(b3, "Version", [100])
                    def le_normal(b4):
                        W(b4, "Type", ["LayerElementNormal"])
                        W(b4, "TypedIndex", [0])
                    W(b3, "LayerElement", children=[le_normal])

                W(b2, "Vertices", [vf])
                W(b2, "PolygonVertexIndex", [iff])
                if nf:
                    W(b2, "LayerElementNormal", [0], children=[layer_elem_normal])
                W(b2, "Layer", [0], children=[layer0])

            W(b, "Geometry", [mid, f"{sm.name}\x00\x01Geometry", "Mesh"],
              children=[geom_node])

            def model_node(b2):
                W(b2, "Version", [232])
            W(b, "Model", [mod_id, f"{sm.name}\x00\x01Model", "Mesh"],
              children=[model_node])

            def mat_node(b2):
                W(b2, "Version", [102])
                W(b2, "ShadingModel", ["phong"])
            W(b, "Material", [ma_id, f"{sm.material or sm.name}\x00\x01Material", ""],
              children=[mat_node])

        # Bone nodes
        if skeleton and skeleton.bones:
            for bone in skeleton.bones:
                # NodeAttribute (LimbNode)
                def bone_attr(b2, bn=bone):
                    W(b2, "TypeFlags", ["Skeleton"])
                W(b, "NodeAttribute", [bone_attr_ids[bone.index],
                    f"{bone.name}\x00\x01NodeAttribute", "LimbNode"],
                    children=[bone_attr])

                # Model for bone
                def bone_model(b2, bn=bone):
                    W(b2, "Version", [232])
                    def props(b3, _bn=bn):
                        W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A",
                                    float(_bn.position[0] * scale),
                                    float(_bn.position[1] * scale),
                                    float(_bn.position[2] * scale)])
                    W(b2, "Properties70", children=[props])

                W(b, "Model", [bone_model_ids[bone.index],
                    f"{bone.name}\x00\x01Model", "LimbNode"],
                    children=[bone_model])

        # Skin + Cluster deformers — the part that actually ties the
        # vertex weights to the bones on import. Without these, Blender
        # / Maya see the LimbNode armature but the mesh has no skin
        # modifier attached and users report "no export for skin /
        # armature modifiers" (Yoo on community discord).
        identity_matrix = [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]

        def _bone_bind_matrices(bone) -> tuple[list[float], list[float]]:
            """Return (Transform, TransformLink) for an FBX Cluster.

            FBX Cluster semantics:
              TransformLink = bone's world-space bind pose (4x4, column-major)
              Transform     = inverse of TransformLink in geometry space

            The skeleton parser exposes these as ``bone.bind_matrix`` and
            ``bone.inv_bind_matrix``; when they're populated we use them
            verbatim. Missing data falls back to identity which Blender
            treats as "rest pose at origin" — the mesh imports with
            weights intact, just without a custom bind pose.
            """
            if getattr(bone, "inv_bind_matrix", None) and len(bone.inv_bind_matrix) == 16:
                transform = [float(v) for v in bone.inv_bind_matrix]
            else:
                transform = list(identity_matrix)
            if getattr(bone, "bind_matrix", None) and len(bone.bind_matrix) == 16:
                link = [float(v) for v in bone.bind_matrix]
            else:
                link = list(identity_matrix)
            return transform, link

        for idx, sm in enumerate(mesh.submeshes):
            sk_id = skin_ids[idx]
            if sk_id is None:
                continue

            def skin_node(b2, sid=sk_id):
                W(b2, "Version", [101])
                # SkinningType: Linear is the safe, compatible default.
                W(b2, "SkinningType", ["Linear"])

            W(b, "Deformer", [sk_id, f"{sm.name}_Skin\x00\x01Deformer", "Skin"],
              children=[skin_node])

            for b_idx, weight_list in cluster_data[idx].items():
                cl_id = cluster_ids[idx][b_idx]
                bone = skeleton.bones[b_idx]
                indexes = [vi for vi, _ in weight_list]
                weights = [w for _, w in weight_list]
                transform, link = _bone_bind_matrices(bone)

                def cluster_node(b2, _idx=indexes, _w=weights,
                                 _t=transform, _l=link, _bn=bone):
                    W(b2, "Version", [100])
                    W(b2, "UserData", ["", ""])
                    W(b2, "Indexes", [_idx])
                    W(b2, "Weights", [_w])
                    W(b2, "Transform", [_t])
                    W(b2, "TransformLink", [_l])

                W(b, "Deformer",
                  [cl_id, f"{bone.name}_{sm.name}\x00\x01SubDeformer", "Cluster"],
                  children=[cluster_node])

    W(buf, "Objects", children=[objects])

    # Connections
    def connections(b):
        for idx in range(len(mesh.submeshes)):
            W(b, "C", ["OO", model_ids[idx], _FbxId(0)])
            W(b, "C", ["OO", mesh_ids[idx], model_ids[idx]])
            W(b, "C", ["OO", mat_ids[idx], model_ids[idx]])

        # Bone connections
        if skeleton and skeleton.bones:
            for bone in skeleton.bones:
                # NodeAttribute → Bone Model
                W(b, "C", ["OO", bone_attr_ids[bone.index], bone_model_ids[bone.index]])
                # Bone → Parent (or root)
                if bone.parent_index >= 0 and bone.parent_index in bone_model_ids:
                    W(b, "C", ["OO", bone_model_ids[bone.index],
                               bone_model_ids[bone.parent_index]])
                else:
                    W(b, "C", ["OO", bone_model_ids[bone.index], _FbxId(0)])

        # Skin + Cluster connections. Connection topology per submesh:
        #
        #   Geometry  <---OO---  Skin
        #              Skin     <---OO---  Cluster_bone_0
        #                                            ^
        #                      Bone_model_for_bone_0  +---OO (bind)
        #              Skin     <---OO---  Cluster_bone_1
        #                                            ^
        #                      Bone_model_for_bone_1  +---OO
        #              ...
        for idx in range(len(mesh.submeshes)):
            sk_id = skin_ids[idx]
            if sk_id is None:
                continue
            # Skin -> Geometry
            W(b, "C", ["OO", sk_id, mesh_ids[idx]])
            for b_idx, cl_id in cluster_ids[idx].items():
                # Cluster -> Skin
                W(b, "C", ["OO", cl_id, sk_id])
                # Bone Model -> Cluster (this is the link that makes
                # the Cluster "belong" to a bone so FBX importers can
                # rebuild the armature modifier).
                W(b, "C", ["OO", bone_model_ids[b_idx], cl_id])

    W(buf, "Connections", children=[connections])

    # Footer
    buf.write(b"\x00" * 13)
    buf.write(b"\xfa\xbc\xab\x09\xd0\xc8\xd4\x66\xb1\x76\xfb\x83\x1c\xf7\x26\x7e")
    buf.write(b"\x00" * 4)
    buf.write(struct.pack("<I", 7400))
    buf.write(b"\x00" * 120)
    buf.write(bytes([
        0xf8, 0x5a, 0x8c, 0x6a, 0xde, 0xf5, 0xd9, 0x7e,
        0xec, 0xe9, 0x0c, 0xe3, 0x75, 0x8f, 0x29, 0x0b,
    ]))

    with open(fbx_path, "wb") as f:
        f.write(buf.getvalue())

    bone_count = len(skeleton.bones) if skeleton else 0
    logger.info("Exported FBX+Skeleton: %s (%d verts, %d faces, %d bones)",
                fbx_path, mesh.total_vertices, mesh.total_faces, bone_count)
    return fbx_path
