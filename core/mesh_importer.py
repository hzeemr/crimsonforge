"""OBJ importer and PAC/PAM binary builder for round-trip mesh modding.

Pipeline: Export .pac → edit in Blender → save .obj → import_obj() → build_pac() → repack

The OBJ file must have been exported by CrimsonForge (contains source_path
and source_format comments). The original PAC/PAM binary is needed to
preserve metadata (names, materials, bones, flags) that OBJ cannot store.
"""

from __future__ import annotations

import copy
import os
import struct
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.mesh_parser import (
    ParsedMesh,
    SubMesh,
    parse_pac,
    parse_pam,
    parse_pamlod,
    _find_pac_descriptors,
    _parse_par_sections,
    _compute_smooth_normals,
    _find_local_stride,
    STRIDE_CANDIDATES,
)
from utils.logger import get_logger

logger = get_logger("core.mesh_importer")


def _resolve_obj_index(raw_index: str, item_count: int) -> int:
    """Resolve a Wavefront OBJ index token to a zero-based Python index."""
    value = int(raw_index)
    if value > 0:
        return value - 1
    if value < 0:
        return item_count + value
    raise ValueError("OBJ indices are 1-based and cannot be zero")


# ═══════════════════════════════════════════════════════════════════════
#  OBJ IMPORTER
# ═══════════════════════════════════════════════════════════════════════

def import_obj(obj_path: str) -> ParsedMesh:
    """Import an OBJ file back into a ParsedMesh.

    Reads CrimsonForge metadata comments (source_path, source_format)
    to identify the original game file.

    Returns:
        ParsedMesh with vertices, UVs, normals, faces per submesh.
    """
    source_path = ""
    source_format = ""
    submeshes: list[SubMesh] = []

    # Current submesh being built
    current_name = ""
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    normals: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    # Global vertex/uv/normal arrays (OBJ uses global indices)
    all_verts: list[tuple[float, float, float]] = []
    all_uvs: list[tuple[float, float]] = []
    all_normals: list[tuple[float, float, float]] = []

    # Per-submesh: track which global indices belong to each submesh
    submesh_list: list[dict] = []
    current_faces_global: list[tuple] = []
    current_material = ""

    with open(obj_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # Parse metadata comments
            if line.startswith("# source_path:"):
                source_path = line.split(":", 1)[1].strip()
                continue
            if line.startswith("# source_format:"):
                source_format = line.split(":", 1)[1].strip()
                continue
            if line.startswith("#") or not line:
                continue

            parts = line.split()
            if not parts:
                continue

            if parts[0] == "v" and len(parts) >= 4:
                all_verts.append((float(parts[1]), float(parts[2]), float(parts[3])))

            elif parts[0] == "vt" and len(parts) >= 3:
                u = float(parts[1])
                v = 1.0 - float(parts[2])  # flip V back (OBJ export flipped it)
                all_uvs.append((u, v))

            elif parts[0] == "vn" and len(parts) >= 4:
                all_normals.append((float(parts[1]), float(parts[2]), float(parts[3])))

            elif parts[0] == "o":
                # New object/submesh — save previous
                if current_name and current_faces_global:
                    submesh_list.append({
                        "name": current_name,
                        "material": current_material,
                        "faces_global": current_faces_global,
                    })
                current_name = parts[1] if len(parts) > 1 else f"submesh_{len(submesh_list)}"
                current_faces_global = []
                current_material = ""

            elif parts[0] == "usemtl":
                current_material = parts[1] if len(parts) > 1 else ""

            elif parts[0] == "f" and len(parts) >= 4:
                # Parse face indices (supports v, v/vt, v/vt/vn, v//vn) and
                # triangulate polygons by fan because Blender commonly exports quads.
                face_verts = []
                for fp in parts[1:]:
                    indices = fp.split("/")
                    vi = _resolve_obj_index(indices[0], len(all_verts))
                    ti = _resolve_obj_index(indices[1], len(all_uvs)) if len(indices) > 1 and indices[1] else -1
                    ni = _resolve_obj_index(indices[2], len(all_normals)) if len(indices) > 2 and indices[2] else -1
                    face_verts.append((vi, ti, ni))
                if len(face_verts) < 3:
                    continue
                for tri_idx in range(1, len(face_verts) - 1):
                    current_faces_global.append(
                        (face_verts[0], face_verts[tri_idx], face_verts[tri_idx + 1])
                    )

    # Save last submesh
    if current_name and current_faces_global:
        submesh_list.append({
            "name": current_name,
            "material": current_material,
            "faces_global": current_faces_global,
        })

    # Convert global indices to per-submesh local indices.
    # Key: keep ALL vertices in each submesh's range (not just face-referenced ones).
    # Some meshes have unused vertices that must be preserved for correct rebuild.

    # First, determine vertex ownership: each submesh "owns" a contiguous range
    # based on the order vertices appear in the OBJ (submesh 0 first, etc.)
    vert_offset = 0
    for sm_data in submesh_list:
        # Count vertices that belong to this submesh in the OBJ
        # (vertices appear between 'o' markers, counted during parse above)
        # We stored them in all_verts in order — need to find this submesh's range
        pass

    # Build vertex ranges from the OBJ structure:
    # Vertices between successive 'o' markers belong to that submesh
    # Re-parse to find vertex counts per submesh
    sm_vert_counts = []
    sm_uv_counts = []
    sm_normal_counts = []
    current_v = current_vt = current_vn = 0

    with open(obj_path, "r", encoding="utf-8") as f:
        in_submesh = False
        for line in f:
            line = line.strip()
            if line.startswith("o "):
                if in_submesh:
                    sm_vert_counts.append(current_v)
                    sm_uv_counts.append(current_vt)
                    sm_normal_counts.append(current_vn)
                current_v = current_vt = current_vn = 0
                in_submesh = True
            elif line.startswith("v ") and not line.startswith("vt") and not line.startswith("vn"):
                current_v += 1
            elif line.startswith("vt "):
                current_vt += 1
            elif line.startswith("vn "):
                current_vn += 1
        if in_submesh:
            sm_vert_counts.append(current_v)
            sm_uv_counts.append(current_vt)
            sm_normal_counts.append(current_vn)

    # Now build each submesh using the FULL vertex range (not just face-referenced).
    # Blender may remap/deduplicate vt/vn indices independently from position indices,
    # so we must honor the face-level vi/ti/ni tuples instead of assuming vi==ti==ni.
    v_offset = 0
    vt_offset = 0
    vn_offset = 0

    for si, sm_data in enumerate(submesh_list):
        nv = sm_vert_counts[si] if si < len(sm_vert_counts) else 0
        nvt = sm_uv_counts[si] if si < len(sm_uv_counts) else 0
        nvn = sm_normal_counts[si] if si < len(sm_normal_counts) else 0

        # Preserve the original exported vertex slots, including any unused vertices,
        # then split only when the same position is referenced with multiple UV/normal
        # pairs after Blender re-export.
        base_verts = [
            all_verts[v_offset + i] if (v_offset + i) < len(all_verts) else (0.0, 0.0, 0.0)
            for i in range(nv)
        ]
        base_uvs = [
            all_uvs[vt_offset + i] if i < nvt and (vt_offset + i) < len(all_uvs) else (0.0, 0.0)
            for i in range(nv)
        ]
        base_normals = [
            all_normals[vn_offset + i] if i < nvn and (vn_offset + i) < len(all_normals) else (0.0, 1.0, 0.0)
            for i in range(nv)
        ]

        local_verts = list(base_verts)
        local_uvs = list(base_uvs)
        local_normals = list(base_normals)

        assigned_uvs: list[tuple[float, float] | None] = [None] * nv
        assigned_normals: list[tuple[float, float, float] | None] = [None] * nv
        split_vertex_map: dict[tuple[int, int, int], int] = {}

        def _resolve_corner_index(vi: int, ti: int, ni: int) -> int:
            local_vi = vi - v_offset
            if not (0 <= local_vi < nv):
                return 0

            local_ti = ti - vt_offset if ti >= 0 else -1
            local_ni = ni - vn_offset if ni >= 0 else -1
            key = (local_vi, local_ti, local_ni)
            existing_idx = split_vertex_map.get(key)
            if existing_idx is not None:
                return existing_idx

            uv_value = (
                all_uvs[ti]
                if 0 <= ti < len(all_uvs)
                else (base_uvs[local_vi] if local_vi < len(base_uvs) else (0.0, 0.0))
            )
            normal_value = (
                all_normals[ni]
                if 0 <= ni < len(all_normals)
                else (base_normals[local_vi] if local_vi < len(base_normals) else (0.0, 1.0, 0.0))
            )

            current_uv = assigned_uvs[local_vi]
            current_normal = assigned_normals[local_vi]
            if current_uv is None and current_normal is None:
                assigned_uvs[local_vi] = uv_value
                assigned_normals[local_vi] = normal_value
                local_uvs[local_vi] = uv_value
                local_normals[local_vi] = normal_value
                split_vertex_map[key] = local_vi
                return local_vi

            if current_uv == uv_value and current_normal == normal_value:
                split_vertex_map[key] = local_vi
                return local_vi

            clone_idx = len(local_verts)
            local_verts.append(base_verts[local_vi])
            local_uvs.append(uv_value)
            local_normals.append(normal_value)
            split_vertex_map[key] = clone_idx
            return clone_idx

        local_faces = []
        for face in sm_data["faces_global"]:
            local_face = []
            for vi, ti, ni in face:
                local_face.append(_resolve_corner_index(vi, ti, ni))
            if len(local_face) == 3:
                local_faces.append(tuple(local_face))

        sm = SubMesh(
            name=sm_data["name"],
            material=sm_data["material"],
            vertices=local_verts,
            uvs=local_uvs if len(local_uvs) == len(local_verts) else [],
            normals=local_normals if len(local_normals) == len(local_verts) else [],
            faces=local_faces,
            vertex_count=len(local_verts),
            face_count=len(local_faces),
        )
        submeshes.append(sm)

        v_offset += nv
        vt_offset += nvt
        vn_offset += nvn

    result = ParsedMesh(
        path=source_path,
        format=source_format,
        submeshes=submeshes,
        total_vertices=sum(len(s.vertices) for s in submeshes),
        total_faces=sum(len(s.faces) for s in submeshes),
        has_uvs=any(s.uvs for s in submeshes),
    )

    if result.submeshes:
        all_v = [v for s in submeshes for v in s.vertices]
        if all_v:
            xs, ys, zs = zip(*all_v)
            result.bbox_min = (min(xs), min(ys), min(zs))
            result.bbox_max = (max(xs), max(ys), max(zs))

    logger.info("Imported OBJ %s: %d submeshes, %d verts, %d faces, source=%s (%s)",
                obj_path, len(submeshes), result.total_vertices,
                result.total_faces, source_path, source_format)
    return result


# ═══════════════════════════════════════════════════════════════════════
#  QUANTIZATION UTILITIES
# ═══════════════════════════════════════════════════════════════════════

def _quantize_u16(value: float, vmin: float, vmax: float) -> int:
    """Float → uint16 quantized: inverse of dequantize."""
    if abs(vmax - vmin) < 1e-10:
        return 32768
    t = (value - vmin) / (vmax - vmin)
    t = max(0.0, min(1.0, t))
    return min(65535, max(0, round(t * 65535)))


def _compute_bbox(vertices: list[tuple[float, float, float]]):
    """Compute tight bounding box from vertex list."""
    if not vertices:
        return (0, 0, 0), (1, 1, 1)
    xs, ys, zs = zip(*vertices)
    # Add tiny epsilon to avoid zero-size bbox
    eps = 1e-6
    bmin = (min(xs) - eps, min(ys) - eps, min(zs) - eps)
    bmax = (max(xs) + eps, max(ys) + eps, max(zs) + eps)
    return bmin, bmax


def _reorder_submeshes_to_match_original(original_mesh: ParsedMesh, imported_mesh: ParsedMesh) -> None:
    """Restore original submesh and vertex slot order for PAM/PAMLOD rebuilds."""
    if len(original_mesh.submeshes) != len(imported_mesh.submeshes):
        raise ValueError(
            "PAM/PAMLOD import requires the same submesh count as the original mesh."
        )

    orig_names = [sm.name for sm in original_mesh.submeshes]
    imp_names = [sm.name for sm in imported_mesh.submeshes]
    if orig_names != imp_names:
        name_to_submesh = {}
        for sm in imported_mesh.submeshes:
            if not sm.name or sm.name in name_to_submesh:
                break
            name_to_submesh[sm.name] = sm
        if len(name_to_submesh) == len(imported_mesh.submeshes) and set(name_to_submesh) == set(orig_names):
            imported_mesh.submeshes = [name_to_submesh[name] for name in orig_names]

    for sm_idx, (orig_sm, imp_sm) in enumerate(zip(original_mesh.submeshes, imported_mesh.submeshes)):
        if len(orig_sm.vertices) != len(imp_sm.vertices):
            raise ValueError(
                f"Submesh {sm_idx} changed vertex count "
                f"({len(orig_sm.vertices)} -> {len(imp_sm.vertices)}). "
                "PAM/PAMLOD import currently requires keeping the same topology."
            )
        if len(orig_sm.faces) != len(imp_sm.faces):
            raise ValueError(
                f"Submesh {sm_idx} changed face count "
                f"({len(orig_sm.faces)} -> {len(imp_sm.faces)}). "
                "PAM/PAMLOD import currently requires keeping the same topology."
            )

        if imp_sm.faces == orig_sm.faces:
            continue

        mapping: dict[int, int] = {}
        reverse: dict[int, int] = {}
        mapping_ok = True

        for orig_face, imp_face in zip(orig_sm.faces, imp_sm.faces):
            if len(orig_face) != len(imp_face):
                mapping_ok = False
                break
            for orig_idx, imp_idx in zip(orig_face, imp_face):
                prev_orig = mapping.get(imp_idx)
                prev_imp = reverse.get(orig_idx)
                if (prev_orig is not None and prev_orig != orig_idx) or (
                    prev_imp is not None and prev_imp != imp_idx
                ):
                    mapping_ok = False
                    break
                mapping[imp_idx] = orig_idx
                reverse[orig_idx] = imp_idx
            if not mapping_ok:
                break

        if (not mapping_ok or
                len(mapping) != len(orig_sm.vertices) or
                len(reverse) != len(orig_sm.vertices)):
            raise ValueError(
                f"Submesh {sm_idx} no longer matches the original triangle order. "
                "PAM/PAMLOD import can handle vertex renumbering, but it still "
                "requires preserving the original triangle list."
            )

        reordered_vertices = [None] * len(orig_sm.vertices)
        reordered_uvs = [None] * len(orig_sm.vertices) if len(imp_sm.uvs) == len(imp_sm.vertices) else None
        reordered_normals = [None] * len(orig_sm.vertices) if len(imp_sm.normals) == len(imp_sm.vertices) else None

        for imp_idx, orig_idx in mapping.items():
            reordered_vertices[orig_idx] = imp_sm.vertices[imp_idx]
            if reordered_uvs is not None:
                reordered_uvs[orig_idx] = imp_sm.uvs[imp_idx]
            if reordered_normals is not None:
                reordered_normals[orig_idx] = imp_sm.normals[imp_idx]

        imp_sm.vertices = reordered_vertices
        imp_sm.uvs = reordered_uvs if reordered_uvs is not None else imp_sm.uvs
        imp_sm.normals = reordered_normals if reordered_normals is not None else imp_sm.normals
        imp_sm.faces = list(orig_sm.faces)
        imp_sm.vertex_count = len(imp_sm.vertices)
        imp_sm.face_count = len(imp_sm.faces)


def _resolve_pam_alias_vertex(
    byte_off: int,
    refs: list[tuple[tuple[float, float, float], tuple[float, float, float], int, int]],
    eps: float = 1e-6,
    allow_average_conflicts: bool = False,
) -> tuple[float, float, float]:
    """Choose one final position for a shared vertex byte offset."""
    changed: list[tuple[tuple[float, float, float], int, int]] = []
    for orig_v, new_v, sm_idx, vert_idx in refs:
        if math.dist(orig_v, new_v) > eps:
            changed.append((new_v, sm_idx, vert_idx))

    if not changed:
        return refs[0][1]

    chosen = changed[0][0]
    for new_v, sm_idx, vert_idx in changed[1:]:
        if math.dist(new_v, chosen) > eps:
            if allow_average_conflicts:
                xs = [pos[0][0] for pos in changed]
                ys = [pos[0][1] for pos in changed]
                zs = [pos[0][2] for pos in changed]
                return (
                    sum(xs) / len(xs),
                    sum(ys) / len(ys),
                    sum(zs) / len(zs),
                )
            raise ValueError(
                "Mesh import detected linked vertices that share the same source bytes, "
                f"but they were edited differently (offset 0x{byte_off:X}, "
                f"submesh {sm_idx} vertex {vert_idx}). "
                "Edit all linked copies to the same position, or keep the topology "
                "and overlapping pieces unchanged."
            )
    return chosen


def _make_temp_mesh(path: str, fmt: str, submeshes: list[SubMesh]) -> ParsedMesh:
    """Build a lightweight ParsedMesh wrapper for helper operations."""
    return ParsedMesh(
        path=path,
        format=fmt,
        submeshes=submeshes,
        total_vertices=sum(len(sm.vertices) for sm in submeshes),
        total_faces=sum(len(sm.faces) for sm in submeshes),
        has_uvs=any(sm.uvs for sm in submeshes),
    )


def _expand_bbox_to_vertices(
    orig_bmin: tuple[float, float, float],
    orig_bmax: tuple[float, float, float],
    vertices: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Expand an existing bbox to include all provided vertices."""
    if not vertices:
        return orig_bmin, orig_bmax
    xs, ys, zs = zip(*vertices)
    bmin = (
        min(orig_bmin[0], min(xs)),
        min(orig_bmin[1], min(ys)),
        min(orig_bmin[2], min(zs)),
    )
    bmax = (
        max(orig_bmax[0], max(xs)),
        max(orig_bmax[1], max(ys)),
        max(orig_bmax[2], max(zs)),
    )
    return bmin, bmax


def _collect_vertex_offset_refs(
    original_data: bytes,
    original_mesh: ParsedMesh,
    new_mesh: ParsedMesh,
    orig_bmin: tuple[float, float, float],
    orig_bmax: tuple[float, float, float],
    search_start: int = 0,
) -> dict[int, list[tuple[tuple[float, float, float], tuple[float, float, float], int, int]]]:
    """Map source byte offsets to original/new vertex pairs."""
    _reorder_submeshes_to_match_original(original_mesh, new_mesh)

    offset_refs: dict[int, list[tuple[tuple[float, float, float], tuple[float, float, float], int, int]]] = {}
    search_cursor = search_start

    for sm_idx, (orig_sm, new_sm) in enumerate(zip(original_mesh.submeshes, new_mesh.submeshes)):
        n = min(len(orig_sm.vertices), len(new_sm.vertices))
        sm_offsets = list(orig_sm.source_vertex_offsets) if (
            len(orig_sm.source_vertex_offsets) == len(orig_sm.vertices)
        ) else []

        if not sm_offsets:
            for vi in range(len(orig_sm.vertices)):
                vx, vy, vz = orig_sm.vertices[vi]
                xu = _quantize_u16(vx, orig_bmin[0], orig_bmax[0])
                yu = _quantize_u16(vy, orig_bmin[1], orig_bmax[1])
                zu = _quantize_u16(vz, orig_bmin[2], orig_bmax[2])
                target = struct.pack("<HHH", xu, yu, zu)

                found = -1
                for scan in range(search_cursor, len(original_data) - 6):
                    if original_data[scan:scan + 6] == target:
                        found = scan
                        search_cursor = scan + 6
                        break

                sm_offsets.append(found)

        for vi in range(n):
            if vi >= len(sm_offsets) or sm_offsets[vi] < 0:
                continue
            byte_off = sm_offsets[vi]
            offset_refs.setdefault(byte_off, []).append(
                (orig_sm.vertices[vi], new_sm.vertices[vi], sm_idx, vi)
            )

    return offset_refs


def _apply_quantized_vertex_patches(
    result: bytearray,
    offset_refs: dict[int, list[tuple[tuple[float, float, float], tuple[float, float, float], int, int]]],
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
    allow_average_conflicts: bool = False,
) -> int:
    """Patch quantized XYZ values at the collected byte offsets."""
    patched_offsets = 0
    for byte_off, refs in offset_refs.items():
        if byte_off + 6 > len(result):
            continue

        vx, vy, vz = _resolve_pam_alias_vertex(
            byte_off, refs, allow_average_conflicts=allow_average_conflicts
        )
        xu = _quantize_u16(vx, bmin[0], bmax[0])
        yu = _quantize_u16(vy, bmin[1], bmax[1])
        zu = _quantize_u16(vz, bmin[2], bmax[2])
        struct.pack_into("<HHH", result, byte_off, xu, yu, zu)
        patched_offsets += 1

    return patched_offsets


def _align_submesh_order_like_original(original_mesh: ParsedMesh, new_mesh: ParsedMesh) -> None:
    """Align submesh order by name when possible without enforcing topology."""
    if len(original_mesh.submeshes) != len(new_mesh.submeshes):
        return

    orig_names = [sm.name for sm in original_mesh.submeshes]
    if [sm.name for sm in new_mesh.submeshes] == orig_names:
        return

    name_to_submesh: dict[str, SubMesh] = {}
    for sm in new_mesh.submeshes:
        if not sm.name or sm.name in name_to_submesh:
            return
        name_to_submesh[sm.name] = sm

    if set(name_to_submesh) == set(orig_names):
        new_mesh.submeshes = [name_to_submesh[name] for name in orig_names]


def _submesh_uvs_match(orig_sm: SubMesh, new_sm: SubMesh, eps: float = 1e-6) -> bool:
    """Check whether two submeshes have equivalent UV payloads."""
    orig_has_uv = len(orig_sm.uvs) == len(orig_sm.vertices)
    new_has_uv = len(new_sm.uvs) == len(new_sm.vertices)
    if orig_has_uv != new_has_uv:
        return False
    if not orig_has_uv:
        return True
    return all(
        abs(ou - nu) <= eps and abs(ov - nv) <= eps
        for (ou, ov), (nu, nv) in zip(orig_sm.uvs, new_sm.uvs)
    )


def _pam_needs_full_rebuild(original_mesh: ParsedMesh, new_mesh: ParsedMesh) -> bool:
    """Return True when edits go beyond in-place XYZ patching."""
    if len(original_mesh.submeshes) != len(new_mesh.submeshes):
        return True

    for orig_sm, new_sm in zip(original_mesh.submeshes, new_mesh.submeshes):
        if len(orig_sm.vertices) != len(new_sm.vertices):
            return True
        if len(orig_sm.faces) != len(new_sm.faces):
            return True
        if orig_sm.faces != new_sm.faces:
            return True
        if not _submesh_uvs_match(orig_sm, new_sm):
            return True

    return False


def _inspect_pam_layout(original_data: bytes) -> dict:
    """Inspect whether the PAM uses a standard layout we can serialize."""
    hdr_geom_off = 0x3C
    hdr_mesh_count = 0x10
    submesh_table = 0x410
    submesh_stride = 0x218
    pam_idx_off = 0x19840

    if not original_data or original_data[:4] != b"PAR ":
        return {"kind": "unsupported", "reason": "missing PAM header"}

    geom_off = struct.unpack_from("<I", original_data, hdr_geom_off)[0]
    mesh_count = struct.unpack_from("<I", original_data, hdr_mesh_count)[0]
    if mesh_count <= 0:
        return {"kind": "unsupported", "reason": "mesh table is empty"}

    entries = []
    for i in range(mesh_count):
        desc_off = submesh_table + i * submesh_stride
        if desc_off + submesh_stride > len(original_data):
            return {"kind": "unsupported", "reason": "submesh table is truncated"}
        nv = struct.unpack_from("<I", original_data, desc_off)[0]
        ni = struct.unpack_from("<I", original_data, desc_off + 4)[0]
        ve = struct.unpack_from("<I", original_data, desc_off + 8)[0]
        ie = struct.unpack_from("<I", original_data, desc_off + 12)[0]
        entries.append({
            "desc_off": desc_off,
            "nv": nv,
            "ni": ni,
            "ve": ve,
            "ie": ie,
        })

    is_combined = mesh_count > 1
    if is_combined:
        ve_acc = ie_acc = 0
        for entry in entries:
            if entry["ve"] != ve_acc or entry["ie"] != ie_acc:
                is_combined = False
                break
            ve_acc += entry["nv"]
            ie_acc += entry["ni"]

    total_nv = sum(entry["nv"] for entry in entries)
    total_ni = sum(entry["ni"] for entry in entries)

    def detect_forward_scan_layout() -> Optional[dict]:
        if total_nv <= 0 or total_ni <= 0:
            return None

        search_limit = min(len(original_data) - 100, geom_off + min(len(original_data) // 2, 2_000_000))
        step = 2 if (search_limit - geom_off) < 500_000 else 4
        scan_candidates = [6, 8, 10, 12, 14, 16, 20, 24, 28, 32]

        for scan_start in range(geom_off, search_limit, step):
            if scan_start + 60 > len(original_data):
                break
            vals = [struct.unpack_from("<H", original_data, scan_start + j * 2)[0] for j in range(30)]
            if max(vals) - min(vals) < 5000:
                continue

            for stride in scan_candidates:
                idx_base = scan_start + total_nv * stride
                if idx_base + total_ni * 2 > len(original_data):
                    continue

                valid = True
                for j in range(min(50, total_ni)):
                    val = struct.unpack_from("<H", original_data, idx_base + j * 2)[0]
                    if val >= total_nv:
                        valid = False
                        break
                if not valid:
                    continue

                valid = all(
                    struct.unpack_from("<H", original_data, idx_base + j * 2)[0] < total_nv
                    for j in range(min(total_ni, 500))
                )
                if not valid:
                    continue

                return {
                    "kind": "scan_combined",
                    "geom_off": geom_off,
                    "scan_start": scan_start,
                    "entries": entries,
                    "stride": stride,
                    "old_geom_end": idx_base + total_ni * 2,
                }
        return None

    def detect_backward_scan_layout() -> Optional[dict]:
        if total_nv <= 0 or total_ni <= 0:
            return None

        scan_candidates = [6, 8, 10, 12, 14, 16, 20, 24, 28, 32]
        for scan_end_off in range(len(original_data) - 2, geom_off + total_nv * 6, -2):
            idx_base = scan_end_off - total_ni * 2 + 2
            if idx_base < geom_off:
                break

            first_val = struct.unpack_from("<H", original_data, idx_base)[0]
            if first_val >= total_nv:
                continue

            valid = True
            for j in range(min(30, total_ni)):
                val = struct.unpack_from("<H", original_data, idx_base + j * 2)[0]
                if val >= total_nv:
                    valid = False
                    break
            if not valid:
                continue

            valid = all(
                struct.unpack_from("<H", original_data, idx_base + j * 2)[0] < total_nv
                for j in range(min(total_ni, 300))
            )
            if not valid:
                continue

            valid = all(
                struct.unpack_from("<H", original_data, idx_base + j * 2)[0] < total_nv
                for j in range(total_ni)
            )
            if not valid:
                continue

            vert_region = idx_base - geom_off
            stride = None
            for try_stride in scan_candidates:
                expected_end = geom_off + total_nv * try_stride
                if expected_end <= idx_base and (idx_base - expected_end) < 16384:
                    stride = try_stride
                    break
            if stride is None:
                stride = max(6, vert_region // max(total_nv, 1))

            vertex_end = geom_off + total_nv * stride
            if vertex_end > idx_base or vertex_end > len(original_data):
                continue

            return {
                "kind": "backward_scan_combined",
                "geom_off": geom_off,
                "entries": entries,
                "stride": stride,
                "idx_base": idx_base,
                "vertex_end": vertex_end,
                "old_geom_end": idx_base + total_ni * 2,
            }
        return None

    if is_combined:
        if total_nv <= 0:
            return {"kind": "unsupported", "reason": "combined PAM has no vertices"}
        avail = len(original_data) - geom_off
        target_stride = (avail - total_ni * 2) / total_nv
        stride = min(STRIDE_CANDIDATES, key=lambda s: abs(s - target_stride))
        idx_base = geom_off + total_nv * stride
        if idx_base + total_ni * 2 <= len(original_data):
            return {
                "kind": "combined",
                "geom_off": geom_off,
                "entries": entries,
                "stride": stride,
                "old_geom_end": idx_base + total_ni * 2,
            }

        scan_layout = detect_forward_scan_layout()
        if scan_layout is not None:
            return scan_layout

        backward_layout = detect_backward_scan_layout()
        if backward_layout is not None:
            return backward_layout

        return {"kind": "unsupported", "reason": "combined PAM geometry block is truncated"}

    idx_avail = max(0, (len(original_data) - pam_idx_off) // 2)
    local_entries = []
    uses_global = False
    old_geom_end = geom_off
    for entry in entries:
        stride, idx_off = _find_local_stride(
            original_data, geom_off, entry["ve"], entry["nv"], entry["ni"]
        )
        if stride is not None:
            entry = dict(entry)
            entry["stride"] = stride
            entry["idx_off"] = idx_off
            local_entries.append(entry)
            old_geom_end = max(old_geom_end, idx_off + entry["ni"] * 2)
            continue

        if entry["ie"] + entry["ni"] <= idx_avail:
            uses_global = True
        else:
            scan_layout = detect_forward_scan_layout()
            if scan_layout is not None:
                return scan_layout

            backward_layout = detect_backward_scan_layout()
            if backward_layout is not None:
                return backward_layout

            return {"kind": "unsupported", "reason": "PAM uses scan-fallback geometry layout"}

    if uses_global:
        backward_layout = detect_backward_scan_layout()
        if backward_layout is not None:
            return backward_layout

        return {"kind": "unsupported", "reason": "global-buffer PAM rebuild is not implemented yet"}

    return {
        "kind": "local",
        "geom_off": geom_off,
        "entries": local_entries,
        "old_geom_end": old_geom_end,
    }


def _make_vertex_template_record(
    original_data: bytes,
    base_off: int,
    stride: int,
    index: int,
    fallback_count: int,
) -> bytearray:
    """Copy a template vertex record from the original file when possible."""
    if fallback_count > 0:
        src_idx = min(index, fallback_count - 1)
        rec_off = base_off + src_idx * stride
        if rec_off + stride <= len(original_data):
            return bytearray(original_data[rec_off:rec_off + stride])
    return bytearray(stride)


def _pack_static_vertex_record(
    rec: bytearray,
    stride: int,
    vertex: tuple[float, float, float],
    uv: Optional[tuple[float, float]],
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> bytearray:
    """Write XYZ and optional UVs into a static-mesh vertex record."""
    if len(rec) < stride:
        rec.extend(b"\x00" * (stride - len(rec)))

    xu = _quantize_u16(vertex[0], bmin[0], bmax[0])
    yu = _quantize_u16(vertex[1], bmin[1], bmax[1])
    zu = _quantize_u16(vertex[2], bmin[2], bmax[2])
    struct.pack_into("<HHH", rec, 0, xu, yu, zu)

    if stride >= 12 and uv is not None:
        try:
            struct.pack_into("<e", rec, 8, uv[0])
            struct.pack_into("<e", rec, 10, uv[1])
        except (OverflowError, ValueError):
            struct.pack_into("<e", rec, 8, 0.0)
            struct.pack_into("<e", rec, 10, 0.0)

    return rec


def _static_alignment_match_cost(
    orig_vertex: tuple[float, float, float],
    new_vertex: tuple[float, float, float],
    orig_idx: int,
    new_idx: int,
    diag: float,
    max_count: int,
) -> float:
    """Score how likely an imported static vertex maps to an original slot."""
    dist = math.dist(orig_vertex, new_vertex)
    if orig_idx == new_idx:
        dist *= 0.75
    elif abs(orig_idx - new_idx) <= 2:
        dist *= 0.85

    order_penalty = (
        abs(orig_idx - new_idx) / max(max_count, 1)
    ) * max(diag * 0.05, 0.01)
    return dist + order_penalty


def _align_static_vertex_sequences(
    orig_vertices: list[tuple[float, float, float]],
    new_vertices: list[tuple[float, float, float]],
) -> list[int]:
    """Align original/new static vertex order while allowing inserted vertices."""
    orig_count = len(orig_vertices)
    new_count = len(new_vertices)
    aligned = [-1] * new_count
    if orig_count == 0 or new_count == 0:
        return aligned

    bbox_min, bbox_max = _compute_bbox(orig_vertices)
    diag = math.dist(bbox_min, bbox_max)
    gap_penalty = max(diag * 0.02, 0.01)
    band = max(128, abs(orig_count - new_count) + 128)
    max_states = (orig_count + 1) * min(new_count + 1, band * 2 + 1)
    if max_states > 3_000_000:
        raise ValueError(
            f"Static vertex alignment too large ({orig_count}x{new_count}, band={band})"
        )

    prev_row = {j: j * gap_penalty for j in range(0, min(new_count, band) + 1)}
    backtrack: dict[tuple[int, int], str] = {}
    for j in range(1, min(new_count, band) + 1):
        backtrack[(0, j)] = "left"

    max_count = max(orig_count, new_count)
    for i in range(1, orig_count + 1):
        j_start = max(0, i - band)
        j_end = min(new_count, i + band)
        curr_row: dict[int, float] = {}
        if j_start == 0:
            curr_row[0] = i * gap_penalty
            backtrack[(i, 0)] = "up"

        for j in range(max(1, j_start), j_end + 1):
            best_cost = float("inf")
            best_move = ""

            diag_prev = prev_row.get(j - 1)
            if diag_prev is not None:
                cost = diag_prev + _static_alignment_match_cost(
                    orig_vertices[i - 1],
                    new_vertices[j - 1],
                    i - 1,
                    j - 1,
                    diag,
                    max_count,
                )
                if cost < best_cost:
                    best_cost = cost
                    best_move = "diag"

            up_prev = prev_row.get(j)
            if up_prev is not None:
                cost = up_prev + gap_penalty
                if cost < best_cost:
                    best_cost = cost
                    best_move = "up"

            left_prev = curr_row.get(j - 1)
            if left_prev is not None:
                cost = left_prev + gap_penalty
                if cost < best_cost:
                    best_cost = cost
                    best_move = "left"

            if best_move:
                curr_row[j] = best_cost
                backtrack[(i, j)] = best_move

        prev_row = curr_row

    if new_count not in prev_row:
        raise ValueError("Static vertex alignment band did not reach the final state")

    i = orig_count
    j = new_count
    while i > 0 or j > 0:
        move = backtrack.get((i, j))
        if move == "diag":
            aligned[j - 1] = i - 1
            i -= 1
            j -= 1
        elif move == "left":
            j -= 1
        elif move == "up":
            i -= 1
        else:
            # Recover gracefully if a rare boundary state is missing.
            if j > 0 and i > 0:
                aligned[j - 1] = i - 1
                i -= 1
                j -= 1
            elif j > 0:
                j -= 1
            else:
                i -= 1

    return aligned


def _choose_static_donor_indices(orig_sm: SubMesh, new_sm: SubMesh) -> list[int]:
    """Choose donor records for a topology-changing static mesh rebuild."""
    orig_vertices = list(orig_sm.vertices)
    new_vertices = list(new_sm.vertices)
    if not new_vertices:
        return []
    if not orig_vertices:
        return [0] * len(new_vertices)

    try:
        donor_indices = _align_static_vertex_sequences(orig_vertices, new_vertices)
    except Exception as exc:
        logger.debug(
            "Static donor alignment fallback for %s: %s",
            getattr(new_sm, "name", "") or getattr(orig_sm, "name", "") or "<submesh>",
            exc,
        )
        donor_indices = [-1] * len(new_vertices)

    rounded_map: dict[tuple[int, int, int], list[int]] = {}
    for orig_idx, vertex in enumerate(orig_vertices):
        key = (
            round(vertex[0] * 100000),
            round(vertex[1] * 100000),
            round(vertex[2] * 100000),
        )
        rounded_map.setdefault(key, []).append(orig_idx)

    cell_size, grid = _build_spatial_hash(orig_vertices)
    for new_idx, vertex in enumerate(new_vertices):
        if 0 <= donor_indices[new_idx] < len(orig_vertices):
            continue

        key = (
            round(vertex[0] * 100000),
            round(vertex[1] * 100000),
            round(vertex[2] * 100000),
        )
        exact_hits = rounded_map.get(key)
        if exact_hits:
            donor_indices[new_idx] = min(
                exact_hits,
                key=lambda orig_idx: abs(orig_idx - new_idx),
            )
            continue

        donor_indices[new_idx] = _nearest_point_index(vertex, orig_vertices, cell_size, grid)

    return donor_indices


def _replace_all_in_region(
    data: bytearray,
    start: int,
    end: int,
    old: bytes,
    new: bytes,
) -> int:
    """Replace all occurrences of a fixed-size pattern inside a bounded region."""
    if not old or old == new or start >= end:
        return 0

    hits = 0
    cursor = start
    while True:
        pos = data.find(old, cursor, end)
        if pos < 0:
            break
        data[pos:pos + len(old)] = new
        hits += 1
        cursor = pos + len(new)
    return hits


def _sync_pam_header_mirrors(
    result: bytearray,
    original_mesh: ParsedMesh,
    new_mesh: ParsedMesh,
    geom_off: int,
) -> int:
    """Update mirrored PAM metadata between the main table and geometry block."""
    def _bbox_close(candidate: tuple[float, float, float, float, float, float], reference: tuple[float, float, float, float, float, float], tol: float = 1e-3) -> bool:
        return all(math.isfinite(value) and abs(value - target) <= tol for value, target in zip(candidate, reference))

    mesh_count = min(len(original_mesh.submeshes), len(new_mesh.submeshes))
    region_start = 0x410 + mesh_count * 0x218
    region_end = min(max(geom_off, region_start), len(result))
    if region_start >= region_end:
        return 0

    patched = 0

    for orig_sm, new_sm in zip(original_mesh.submeshes, new_mesh.submeshes):
        orig_nv = len(orig_sm.vertices)
        orig_ni = len(orig_sm.faces) * 3
        new_nv = len(new_sm.vertices)
        new_ni = len(new_sm.faces) * 3

        if orig_sm.vertices:
            oxs, oys, ozs = zip(*orig_sm.vertices)
            old_bbox = (
                min(oxs), min(oys), min(ozs),
                max(oxs), max(oys), max(ozs),
            )
        else:
            old_bbox = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        if new_sm.vertices:
            nxs, nys, nzs = zip(*new_sm.vertices)
            new_bbox = (
                min(nxs), min(nys), min(nzs),
                max(nxs), max(nys), max(nzs),
            )
        else:
            new_bbox = old_bbox

        old_bbox_bytes = struct.pack("<6f", *old_bbox)
        new_bbox_bytes = struct.pack("<6f", *new_bbox)

        patched += _replace_all_in_region(
            result,
            region_start,
            region_end,
            struct.pack("<I", orig_ni) + old_bbox_bytes,
            struct.pack("<I", new_ni) + new_bbox_bytes,
        )
        patched += _replace_all_in_region(
            result,
            region_start,
            region_end,
            old_bbox_bytes,
            new_bbox_bytes,
        )

        for off in range(region_start, max(region_start, region_end - 28) + 1, 4):
            count_and_bbox = result[off:off + 28]
            if len(count_and_bbox) < 28:
                break
            count = struct.unpack_from("<I", count_and_bbox, 0)[0]
            bbox = struct.unpack_from("<6f", count_and_bbox, 4)
            if count == orig_ni and _bbox_close(bbox, old_bbox):
                struct.pack_into("<I", result, off, new_ni)
                struct.pack_into("<6f", result, off + 4, *new_bbox)
                patched += 1

        for off in range(region_start, max(region_start, region_end - 24) + 1, 4):
            bbox_bytes = result[off:off + 24]
            if len(bbox_bytes) < 24:
                break
            bbox = struct.unpack_from("<6f", bbox_bytes, 0)
            if _bbox_close(bbox, old_bbox):
                struct.pack_into("<6f", result, off, *new_bbox)
                patched += 1

        old_pair = struct.pack("<II", orig_nv, orig_ni)
        new_pair = struct.pack("<II", new_nv, new_ni)
        if old_pair == new_pair:
            continue

        anchor_names = []
        if orig_sm.texture:
            anchor_names.append(orig_sm.texture.encode("ascii", "ignore"))
        if orig_sm.material:
            anchor_names.append(orig_sm.material.encode("ascii", "ignore"))

        for anchor in anchor_names:
            if not anchor:
                continue
            cursor = region_start
            while True:
                pos = result.find(anchor, cursor, region_end)
                if pos < 0:
                    break
                pair_off = pos - 8
                if pair_off >= region_start and bytes(result[pair_off:pair_off + 8]) == old_pair:
                    result[pair_off:pair_off + 8] = new_pair
                    patched += 1
                cursor = pos + len(anchor)

    return patched


def _sync_pam_geom_size_header(
    result: bytearray,
    original_data: bytes,
    geom_off: int,
    old_geom_end: int,
    new_geom_end: int,
) -> bool:
    """Refresh PAM header geometry-size field when it mirrors the geometry block length."""
    header_geom_size_off = 0x40
    if (
        len(result) < header_geom_size_off + 4
        or len(original_data) < header_geom_size_off + 4
        or geom_off <= 0
        or old_geom_end < geom_off
        or new_geom_end < geom_off
    ):
        return False

    original_geom_len = old_geom_end - geom_off
    original_header_geom_len = struct.unpack_from("<I", original_data, header_geom_size_off)[0]
    if original_header_geom_len != original_geom_len:
        return False

    struct.pack_into("<I", result, header_geom_size_off, new_geom_end - geom_off)
    return True


def _serialize_pam_combined_layout(
    mesh: ParsedMesh,
    original_mesh: ParsedMesh,
    original_data: bytes,
    layout: dict,
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> bytes:
    """Rebuild a standard combined-buffer PAM from scratch."""
    hdr_bbox_min = 0x14
    hdr_bbox_max = 0x20

    geom_off = layout["geom_off"]
    stride = layout["stride"]
    entries = layout["entries"]
    old_geom_end = layout["old_geom_end"]
    result = bytearray(original_data[:geom_off])

    struct.pack_into("<fff", result, hdr_bbox_min, *bmin)
    struct.pack_into("<fff", result, hdr_bbox_max, *bmax)

    geom_data = bytearray()
    index_data = bytearray()
    vert_cursor = 0
    idx_cursor = 0

    for sm_idx, (sm, orig_sm, entry) in enumerate(zip(mesh.submeshes, original_mesh.submeshes, entries)):
        struct.pack_into("<I", result, entry["desc_off"], len(sm.vertices))
        struct.pack_into("<I", result, entry["desc_off"] + 4, len(sm.faces) * 3)
        struct.pack_into("<I", result, entry["desc_off"] + 8, vert_cursor)
        struct.pack_into("<I", result, entry["desc_off"] + 12, idx_cursor)

        orig_vert_base = geom_off + entry["ve"] * stride
        orig_nv = entry["nv"]
        uv_data = sm.uvs if len(sm.uvs) == len(sm.vertices) else []
        donor_indices = _choose_static_donor_indices(orig_sm, sm)

        for vi, vertex in enumerate(sm.vertices):
            donor_idx = donor_indices[vi] if vi < len(donor_indices) else vi
            rec = _make_vertex_template_record(original_data, orig_vert_base, stride, donor_idx, orig_nv)
            uv = uv_data[vi] if uv_data else None
            geom_data.extend(_pack_static_vertex_record(rec, stride, vertex, uv, bmin, bmax))

        for a, b, c in sm.faces:
            index_data.extend(struct.pack("<HHH", a + vert_cursor, b + vert_cursor, c + vert_cursor))

        vert_cursor += len(sm.vertices)
        idx_cursor += len(sm.faces) * 3

    result.extend(geom_data)
    result.extend(index_data)
    new_geom_end = geom_off + len(geom_data) + len(index_data)
    _sync_pam_geom_size_header(result, original_data, geom_off, old_geom_end, new_geom_end)
    result.extend(original_data[old_geom_end:])
    mirror_patches = _sync_pam_header_mirrors(result, original_mesh, mesh, geom_off)
    logger.info(
        "Built PAM %s with full combined rebuild: %d submeshes, %d verts, %d faces (%d mirrored header patches)",
        mesh.path, len(mesh.submeshes), sum(len(sm.vertices) for sm in mesh.submeshes),
        sum(len(sm.faces) for sm in mesh.submeshes), mirror_patches,
    )
    return bytes(result)


def _serialize_pam_scan_combined_layout(
    mesh: ParsedMesh,
    original_mesh: ParsedMesh,
    original_data: bytes,
    layout: dict,
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> bytes:
    """Rebuild a scan-fallback PAM whose real geometry starts after geom_off."""
    hdr_bbox_min = 0x14
    hdr_bbox_max = 0x20

    scan_start = layout["scan_start"]
    stride = layout["stride"]
    entries = layout["entries"]
    old_geom_end = layout["old_geom_end"]
    result = bytearray(original_data[:scan_start])

    struct.pack_into("<fff", result, hdr_bbox_min, *bmin)
    struct.pack_into("<fff", result, hdr_bbox_max, *bmax)

    geom_data = bytearray()
    index_data = bytearray()
    vert_cursor = 0
    idx_cursor = 0

    for sm, orig_sm, entry in zip(mesh.submeshes, original_mesh.submeshes, entries):
        struct.pack_into("<I", result, entry["desc_off"], len(sm.vertices))
        struct.pack_into("<I", result, entry["desc_off"] + 4, len(sm.faces) * 3)
        struct.pack_into("<I", result, entry["desc_off"] + 8, vert_cursor)
        struct.pack_into("<I", result, entry["desc_off"] + 12, idx_cursor)

        orig_vert_base = scan_start + entry["ve"] * stride
        orig_nv = entry["nv"]
        uv_data = sm.uvs if len(sm.uvs) == len(sm.vertices) else []
        donor_indices = _choose_static_donor_indices(orig_sm, sm)

        for vi, vertex in enumerate(sm.vertices):
            donor_idx = donor_indices[vi] if vi < len(donor_indices) else vi
            rec = _make_vertex_template_record(original_data, orig_vert_base, stride, donor_idx, orig_nv)
            uv = uv_data[vi] if uv_data else None
            geom_data.extend(_pack_static_vertex_record(rec, stride, vertex, uv, bmin, bmax))

        for a, b, c in sm.faces:
            index_data.extend(struct.pack("<HHH", a + vert_cursor, b + vert_cursor, c + vert_cursor))

        vert_cursor += len(sm.vertices)
        idx_cursor += len(sm.faces) * 3

    result.extend(geom_data)
    result.extend(index_data)
    new_geom_end = layout["geom_off"] + len(geom_data) + len(index_data)
    _sync_pam_geom_size_header(result, original_data, layout["geom_off"], old_geom_end, new_geom_end)
    result.extend(original_data[old_geom_end:])
    mirror_patches = _sync_pam_header_mirrors(result, original_mesh, mesh, layout["geom_off"])
    logger.info(
        "Built PAM %s with full scan-combined rebuild: %d submeshes, %d verts, %d faces (%d mirrored header patches)",
        mesh.path, len(mesh.submeshes), sum(len(sm.vertices) for sm in mesh.submeshes),
        sum(len(sm.faces) for sm in mesh.submeshes), mirror_patches,
    )
    return bytes(result)


def _serialize_pam_backward_scan_combined_layout(
    mesh: ParsedMesh,
    original_mesh: ParsedMesh,
    original_data: bytes,
    layout: dict,
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> bytes:
    """Rebuild a backward-scan PAM with padding between vertices and indices."""
    hdr_bbox_min = 0x14
    hdr_bbox_max = 0x20

    geom_off = layout["geom_off"]
    stride = layout["stride"]
    idx_base = layout["idx_base"]
    vertex_end = layout["vertex_end"]
    entries = layout["entries"]
    old_geom_end = layout["old_geom_end"]
    result = bytearray(original_data[:geom_off])

    struct.pack_into("<fff", result, hdr_bbox_min, *bmin)
    struct.pack_into("<fff", result, hdr_bbox_max, *bmax)

    geom_data = bytearray()
    index_data = bytearray()
    vert_cursor = 0
    idx_cursor = 0

    for sm, orig_sm, entry in zip(mesh.submeshes, original_mesh.submeshes, entries):
        struct.pack_into("<I", result, entry["desc_off"], len(sm.vertices))
        struct.pack_into("<I", result, entry["desc_off"] + 4, len(sm.faces) * 3)
        struct.pack_into("<I", result, entry["desc_off"] + 8, vert_cursor)
        struct.pack_into("<I", result, entry["desc_off"] + 12, idx_cursor)

        orig_vert_base = geom_off + entry["ve"] * stride
        orig_nv = entry["nv"]
        uv_data = sm.uvs if len(sm.uvs) == len(sm.vertices) else []
        donor_indices = _choose_static_donor_indices(orig_sm, sm)

        for vi, vertex in enumerate(sm.vertices):
            donor_idx = donor_indices[vi] if vi < len(donor_indices) else vi
            rec = _make_vertex_template_record(original_data, orig_vert_base, stride, donor_idx, orig_nv)
            uv = uv_data[vi] if uv_data else None
            geom_data.extend(_pack_static_vertex_record(rec, stride, vertex, uv, bmin, bmax))

        for a, b, c in sm.faces:
            index_data.extend(struct.pack("<HHH", a + vert_cursor, b + vert_cursor, c + vert_cursor))

        vert_cursor += len(sm.vertices)
        idx_cursor += len(sm.faces) * 3

    result.extend(geom_data)
    result.extend(original_data[vertex_end:idx_base])
    result.extend(index_data)
    new_geom_end = geom_off + len(geom_data) + (idx_base - vertex_end) + len(index_data)
    _sync_pam_geom_size_header(result, original_data, geom_off, old_geom_end, new_geom_end)
    result.extend(original_data[old_geom_end:])
    mirror_patches = _sync_pam_header_mirrors(result, original_mesh, mesh, geom_off)
    logger.info(
        "Built PAM %s with full backward-scan rebuild: %d submeshes, %d verts, %d faces (%d mirrored header patches)",
        mesh.path, len(mesh.submeshes), sum(len(sm.vertices) for sm in mesh.submeshes),
        sum(len(sm.faces) for sm in mesh.submeshes), mirror_patches,
    )
    return bytes(result)


def _serialize_pam_local_layout(
    mesh: ParsedMesh,
    original_mesh: ParsedMesh,
    original_data: bytes,
    layout: dict,
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> bytes:
    """Rebuild a single-submesh local-layout PAM from scratch."""
    hdr_bbox_min = 0x14
    hdr_bbox_max = 0x20

    geom_off = layout["geom_off"]
    entries = layout["entries"]
    old_geom_end = layout["old_geom_end"]
    result = bytearray(original_data[:geom_off])

    struct.pack_into("<fff", result, hdr_bbox_min, *bmin)
    struct.pack_into("<fff", result, hdr_bbox_max, *bmax)

    geom_data = bytearray()
    current_voff = 0

    for sm, orig_sm, entry in zip(mesh.submeshes, original_mesh.submeshes, entries):
        stride = entry["stride"]
        struct.pack_into("<I", result, entry["desc_off"], len(sm.vertices))
        struct.pack_into("<I", result, entry["desc_off"] + 4, len(sm.faces) * 3)
        struct.pack_into("<I", result, entry["desc_off"] + 8, current_voff)
        struct.pack_into("<I", result, entry["desc_off"] + 12, 0)

        orig_vert_base = geom_off + entry["ve"]
        orig_nv = entry["nv"]
        uv_data = sm.uvs if len(sm.uvs) == len(sm.vertices) else []
        donor_indices = _choose_static_donor_indices(orig_sm, sm)

        for vi, vertex in enumerate(sm.vertices):
            donor_idx = donor_indices[vi] if vi < len(donor_indices) else vi
            rec = _make_vertex_template_record(original_data, orig_vert_base, stride, donor_idx, orig_nv)
            uv = uv_data[vi] if uv_data else None
            geom_data.extend(_pack_static_vertex_record(rec, stride, vertex, uv, bmin, bmax))

        for a, b, c in sm.faces:
            geom_data.extend(struct.pack("<HHH", a, b, c))

        current_voff += len(sm.vertices) * stride + len(sm.faces) * 6

    result.extend(geom_data)
    new_geom_end = geom_off + len(geom_data)
    _sync_pam_geom_size_header(result, original_data, geom_off, old_geom_end, new_geom_end)
    result.extend(original_data[old_geom_end:])
    mirror_patches = _sync_pam_header_mirrors(result, original_mesh, mesh, geom_off)
    logger.info(
        "Built PAM %s with full local rebuild: %d submeshes, %d verts, %d faces (%d mirrored header patches)",
        mesh.path, len(mesh.submeshes), sum(len(sm.vertices) for sm in mesh.submeshes),
        sum(len(sm.faces) for sm in mesh.submeshes), mirror_patches,
    )
    return bytes(result)


def _spatial_cell_key(point: tuple[float, float, float], cell_size: float) -> tuple[int, int, int]:
    return (
        int(math.floor(point[0] / cell_size)),
        int(math.floor(point[1] / cell_size)),
        int(math.floor(point[2] / cell_size)),
    )


def _build_spatial_hash(points: list[tuple[float, float, float]]) -> tuple[float, dict[tuple[int, int, int], list[int]]]:
    """Create a simple spatial hash for nearest-vertex transfer."""
    if not points:
        return 1.0, {}

    xs, ys, zs = zip(*points)
    extent = max(
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
        1e-5,
    )
    cell_size = max(extent / max(round(len(points) ** (1.0 / 3.0)), 1), 1e-5)

    grid: dict[tuple[int, int, int], list[int]] = {}
    for idx, point in enumerate(points):
        grid.setdefault(_spatial_cell_key(point, cell_size), []).append(idx)
    return cell_size, grid


def _nearest_point_index(
    point: tuple[float, float, float],
    source_points: list[tuple[float, float, float]],
    cell_size: float,
    grid: dict[tuple[int, int, int], list[int]],
) -> int:
    """Find the nearest source point using the spatial hash."""
    if not source_points:
        raise ValueError("Cannot transfer displacement from an empty source mesh.")

    base = _spatial_cell_key(point, cell_size)
    best_idx = -1
    best_d2 = float("inf")

    for radius in range(0, 8):
        found_any = False
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    cell = (base[0] + dx, base[1] + dy, base[2] + dz)
                    for idx in grid.get(cell, ()):
                        found_any = True
                        sx, sy, sz = source_points[idx]
                        d2 = ((sx - point[0]) ** 2 +
                              (sy - point[1]) ** 2 +
                              (sz - point[2]) ** 2)
                        if d2 < best_d2:
                            best_d2 = d2
                            best_idx = idx
        if found_any and best_idx >= 0:
            return best_idx

    for idx, src in enumerate(source_points):
        d2 = ((src[0] - point[0]) ** 2 +
              (src[1] - point[1]) ** 2 +
              (src[2] - point[2]) ** 2)
        if d2 < best_d2:
            best_d2 = d2
            best_idx = idx

    return best_idx


def _nearby_point_indices(
    point: tuple[float, float, float],
    source_points: list[tuple[float, float, float]],
    cell_size: float,
    grid: dict[tuple[int, int, int], list[int]],
    radius: float,
) -> list[int]:
    """Return source points within the given radius."""
    if not source_points:
        return []

    base = _spatial_cell_key(point, cell_size)
    cell_radius = max(1, int(math.ceil(radius / max(cell_size, 1e-6))))
    radius_sq = radius * radius
    candidates: list[int] = []

    for dx in range(-cell_radius, cell_radius + 1):
        for dy in range(-cell_radius, cell_radius + 1):
            for dz in range(-cell_radius, cell_radius + 1):
                cell = (base[0] + dx, base[1] + dy, base[2] + dz)
                for idx in grid.get(cell, ()):
                    sx, sy, sz = source_points[idx]
                    d2 = ((sx - point[0]) ** 2 +
                          (sy - point[1]) ** 2 +
                          (sz - point[2]) ** 2)
                    if d2 <= radius_sq:
                        candidates.append(idx)

    return candidates


def _percentile(values: list[float], pct: float) -> float:
    """Return a simple percentile from a non-empty list."""
    if not values:
        return 0.0
    clamped = max(0.0, min(1.0, pct))
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * clamped))
    return ordered[idx]


def transfer_pam_edit_to_pamlod_mesh(
    edited_pam_mesh: ParsedMesh,
    original_pam_data: bytes,
    original_pamlod_data: bytes,
    pamlod_path: str,
) -> ParsedMesh:
    """Project a PAM edit onto the paired PAMLOD levels via nearest displacement."""
    original_pam_mesh = parse_pam(original_pam_data, edited_pam_mesh.path)
    editable_pam_mesh = copy.deepcopy(edited_pam_mesh)
    _align_submesh_order_like_original(original_pam_mesh, editable_pam_mesh)

    source_orig = [v for sm in original_pam_mesh.submeshes for v in sm.vertices]
    source_new = [v for sm in editable_pam_mesh.submeshes for v in sm.vertices]
    if not source_orig or not source_new:
        raise ValueError("PAM to PAMLOD transfer requires non-empty source geometry.")

    if len(source_orig) == len(source_new):
        paired_points = zip(source_orig, source_new)
    else:
        # Topology edits cannot be transferred one-to-one, so approximate the
        # deformation field by matching each original PAM vertex to its nearest
        # edited-space vertex. This keeps paired PAMLOD patching alive for
        # sculpt/retopo-style edits instead of failing outright.
        edit_cell_size, edit_grid = _build_spatial_hash(source_new)
        nearest_points = [
            source_new[_nearest_point_index(orig_v, source_new, edit_cell_size, edit_grid)]
            for orig_v in source_orig
        ]
        paired_points = zip(source_orig, nearest_points)

    changed_points: list[tuple[float, float, float]] = []
    changed_displacements: list[tuple[float, float, float]] = []
    for orig_v, new_v in paired_points:
        disp = (new_v[0] - orig_v[0], new_v[1] - orig_v[1], new_v[2] - orig_v[2])
        if math.sqrt(disp[0] ** 2 + disp[1] ** 2 + disp[2] ** 2) > 1e-6:
            changed_points.append(orig_v)
            changed_displacements.append(disp)

    pamlod_mesh = parse_pamlod(original_pamlod_data, pamlod_path)
    if not changed_points:
        return pamlod_mesh

    cell_size, grid = _build_spatial_hash(changed_points)
    transferred = copy.deepcopy(pamlod_mesh)

    for lod_level in transferred.lod_levels:
        lod_vertices = [vertex for sm in lod_level for vertex in sm.vertices]
        if not lod_vertices:
            continue

        target_cell_size, target_grid = _build_spatial_hash(lod_vertices)
        sample_step = max(1, len(changed_points) // 512)
        target_distances = []
        for idx in range(0, len(changed_points), sample_step):
            source_vertex = changed_points[idx]
            nearest_idx = _nearest_point_index(
                source_vertex, lod_vertices, target_cell_size, target_grid
            )
            target_distances.append(math.dist(source_vertex, lod_vertices[nearest_idx]))

        influence_radius = max(
            _percentile(target_distances, 0.75) * 1.25,
            1e-4,
        )

        for sm in lod_level:
            new_vertices = []
            for vertex in sm.vertices:
                nearby = _nearby_point_indices(
                    vertex, changed_points, cell_size, grid, influence_radius
                )
                if not nearby:
                    new_vertices.append(vertex)
                    continue

                exact_disp = None
                acc_x = acc_y = acc_z = 0.0
                weight_sum = 0.0
                for idx in nearby:
                    src = changed_points[idx]
                    disp = changed_displacements[idx]
                    dist = math.dist(vertex, src)
                    if dist <= 1e-8:
                        exact_disp = disp
                        break
                    weight = (1.0 - min(dist / influence_radius, 1.0)) ** 2
                    if weight <= 0.0:
                        continue
                    acc_x += disp[0] * weight
                    acc_y += disp[1] * weight
                    acc_z += disp[2] * weight
                    weight_sum += weight

                if exact_disp is not None:
                    dx, dy, dz = exact_disp
                elif weight_sum > 0.0:
                    dx = acc_x / weight_sum
                    dy = acc_y / weight_sum
                    dz = acc_z / weight_sum
                else:
                    dx = dy = dz = 0.0

                new_vertices.append((vertex[0] + dx, vertex[1] + dy, vertex[2] + dz))
            sm.vertices = new_vertices
            sm.vertex_count = len(new_vertices)
            sm.normals = _compute_smooth_normals(sm.vertices, sm.faces)

    if transferred.lod_levels:
        for lod_level in transferred.lod_levels:
            if lod_level:
                transferred.submeshes = lod_level
                break

    transferred.total_vertices = sum(len(sm.vertices) for sm in transferred.submeshes)
    transferred.total_faces = sum(len(sm.faces) for sm in transferred.submeshes)
    transferred.has_uvs = any(sm.uvs for sm in transferred.submeshes)
    return transferred


# ═══════════════════════════════════════════════════════════════════════
#  PAM BUILDER
# ═══════════════════════════════════════════════════════════════════════

def build_pam(mesh: ParsedMesh, original_data: bytes) -> bytes:
    """Rebuild a PAM binary from a modified mesh.

    Standard combined/local PAM layouts can be fully reserialized so UV
    edits and same-submesh topology edits survive round-trip. More exotic
    scan-fallback/global layouts still fall back to the older position-only
    patch path.
    """
    if not original_data or original_data[:4] != b"PAR ":
        raise ValueError("Original PAM data required for rebuild")

    HDR_BBOX_MIN = 0x14
    HDR_BBOX_MAX = 0x20
    HDR_GEOM_OFF = 0x3C

    result = bytearray(original_data)

    # Read original bbox — use for quantization, expand only if needed
    orig_bmin = struct.unpack_from("<fff", original_data, HDR_BBOX_MIN)
    orig_bmax = struct.unpack_from("<fff", original_data, HDR_BBOX_MAX)
    original_mesh = parse_pam(original_data, mesh.path)
    working_mesh = copy.deepcopy(mesh)
    _align_submesh_order_like_original(original_mesh, working_mesh)

    all_v = [v for s in working_mesh.submeshes for v in s.vertices]
    if all_v:
        bmin, bmax = _compute_bbox(all_v)
    else:
        bmin, bmax = orig_bmin, orig_bmax

    if _pam_needs_full_rebuild(original_mesh, working_mesh):
        if len(original_mesh.submeshes) != len(working_mesh.submeshes):
            raise ValueError(
                "PAM import currently requires keeping the same submesh count as the original mesh."
            )

        layout = _inspect_pam_layout(original_data)
        if layout["kind"] == "combined":
            return _serialize_pam_combined_layout(
                working_mesh, original_mesh, original_data, layout, bmin, bmax
            )
        if layout["kind"] == "scan_combined":
            return _serialize_pam_scan_combined_layout(
                working_mesh, original_mesh, original_data, layout, bmin, bmax
            )
        if layout["kind"] == "backward_scan_combined":
            return _serialize_pam_backward_scan_combined_layout(
                working_mesh, original_mesh, original_data, layout, bmin, bmax
            )
        if layout["kind"] == "local":
            return _serialize_pam_local_layout(
                working_mesh, original_mesh, original_data, layout, bmin, bmax
            )
        raise ValueError(
            "This PAM layout currently supports position-only patching. "
            f"Topology/UV edits are not supported for it yet ({layout.get('reason', 'unknown layout')})."
        )

    if not original_mesh.submeshes:
        return bytes(result)

    bmin, bmax = _expand_bbox_to_vertices(orig_bmin, orig_bmax, all_v)
    struct.pack_into("<fff", result, HDR_BBOX_MIN, *bmin)
    struct.pack_into("<fff", result, HDR_BBOX_MAX, *bmax)

    geom_off = struct.unpack_from("<I", original_data, HDR_GEOM_OFF)[0]
    offset_refs = _collect_vertex_offset_refs(
        original_data, original_mesh, working_mesh, orig_bmin, orig_bmax, search_start=geom_off
    )
    patched_offsets = _apply_quantized_vertex_patches(result, offset_refs, bmin, bmax)

    total_patched = patched_offsets
    logger.info("Built PAM %s: %d bytes (patched %d verts in-place)",
                mesh.path, len(result), total_patched)
    return bytes(result)


def build_pamlod(mesh: ParsedMesh, original_data: bytes) -> bytes:
    """Rebuild a PAMLOD binary by patching vertex positions in-place."""
    if not original_data or len(original_data) < 0x20:
        raise ValueError("Original PAMLOD data required for rebuild")

    HDR_BBOX_MIN = 0x10
    HDR_BBOX_MAX = 0x1C

    result = bytearray(original_data)
    orig_bmin = struct.unpack_from("<fff", original_data, HDR_BBOX_MIN)
    orig_bmax = struct.unpack_from("<fff", original_data, HDR_BBOX_MAX)

    orig_mesh = parse_pamlod(original_data, mesh.path)
    if not orig_mesh.lod_levels:
        return bytes(result)

    target_lod_levels = copy.deepcopy(orig_mesh.lod_levels)
    if mesh.lod_levels:
        for lod_idx, lod_level in enumerate(mesh.lod_levels):
            if lod_idx < len(target_lod_levels) and lod_level:
                target_lod_levels[lod_idx] = copy.deepcopy(lod_level)
    elif mesh.submeshes:
        replace_idx = next((i for i, lod in enumerate(target_lod_levels) if lod), 0)
        target_lod_levels[replace_idx] = copy.deepcopy(mesh.submeshes)

    all_vertices = [
        v
        for lod_level in target_lod_levels
        for sm in lod_level
        for v in sm.vertices
    ]
    bmin, bmax = _expand_bbox_to_vertices(orig_bmin, orig_bmax, all_vertices)
    struct.pack_into("<fff", result, HDR_BBOX_MIN, *bmin)
    struct.pack_into("<fff", result, HDR_BBOX_MAX, *bmax)

    offset_refs: dict[int, list[tuple[tuple[float, float, float], tuple[float, float, float], int, int]]] = {}
    for lod_idx, orig_level in enumerate(orig_mesh.lod_levels):
        if lod_idx >= len(target_lod_levels):
            break
        new_level = target_lod_levels[lod_idx]
        if not orig_level or not new_level:
            continue

        level_orig_mesh = _make_temp_mesh(orig_mesh.path, "pamlod", orig_level)
        level_new_mesh = _make_temp_mesh(mesh.path or orig_mesh.path, "pamlod", new_level)
        level_refs = _collect_vertex_offset_refs(
            original_data, level_orig_mesh, level_new_mesh, orig_bmin, orig_bmax, search_start=0
        )
        for byte_off, refs in level_refs.items():
            offset_refs.setdefault(byte_off, []).extend(refs)

    patched_offsets = _apply_quantized_vertex_patches(
        result, offset_refs, bmin, bmax, allow_average_conflicts=True
    )
    logger.info("Built PAMLOD %s: %d bytes (patched %d verts in-place)",
                mesh.path, len(result), patched_offsets)
    return bytes(result)


# ═══════════════════════════════════════════════════════════════════════
#  AUTO-DETECT AND BUILD
# ═══════════════════════════════════════════════════════════════════════

def _quantize_pac_u16(value: float, bbox_min: float, bbox_extent: float) -> int:
    """Float -> PAC uint16 quantized using bbox min/extent encoding."""
    if abs(bbox_extent) < 1e-10:
        return 0
    t = (value - bbox_min) / bbox_extent
    t = max(0.0, min(1.0, t))
    return min(32767, max(0, round(t * 32767.0)))


def _patch_pac_descriptor_bounds(
    data: bytearray,
    descriptor_offset: int,
    bbox_min: tuple[float, float, float],
    bbox_extent: tuple[float, float, float],
) -> None:
    """Update a PAC descriptor's bbox min/extent floats in section 0."""
    if descriptor_offset < 0 or descriptor_offset + 35 > len(data):
        return

    floats_off = descriptor_offset + 3
    struct.pack_into("<f", data, floats_off + 2 * 4, bbox_min[0])
    struct.pack_into("<f", data, floats_off + 3 * 4, bbox_min[1])
    struct.pack_into("<f", data, floats_off + 4 * 4, bbox_min[2])
    struct.pack_into("<f", data, floats_off + 5 * 4, bbox_extent[0])
    struct.pack_into("<f", data, floats_off + 6 * 4, bbox_extent[1])
    struct.pack_into("<f", data, floats_off + 7 * 4, bbox_extent[2])


def _pac_submesh_match_score(imported_sm: SubMesh, original_sm: SubMesh) -> float:
    """Score how likely an imported PAC object maps back to an original slot."""
    imp_center = tuple((mn + mx) * 0.5 for mn, mx in zip(*_compute_bbox(imported_sm.vertices)))
    orig_center = tuple((mn + mx) * 0.5 for mn, mx in zip(*_compute_bbox(original_sm.vertices)))
    center_dist = math.dist(imp_center, orig_center)

    vert_ratio = abs(math.log((len(imported_sm.vertices) + 1) / (len(original_sm.vertices) + 1)))
    face_ratio = abs(math.log((len(imported_sm.faces) + 1) / (len(original_sm.faces) + 1)))
    return center_dist + vert_ratio * 0.75 + face_ratio * 0.75


def _merge_partial_pac_import(
    original_mesh: ParsedMesh,
    imported_mesh: ParsedMesh,
) -> ParsedMesh:
    """Merge a partial PAC OBJ import onto the original submesh set by name.

    Blender exports sometimes omit hidden or unselected PAC objects. In that
    case we still want to apply the edited submeshes while preserving the
    untouched original ones.
    """
    if len(imported_mesh.submeshes) >= len(original_mesh.submeshes):
        return imported_mesh

    original_names = [sm.name for sm in original_mesh.submeshes]
    imported_by_name: dict[str, SubMesh] = {}
    unknown_named: list[SubMesh] = []
    unnamed: list[SubMesh] = []

    for sm in imported_mesh.submeshes:
        if sm.name:
            if sm.name in original_names:
                if sm.name in imported_by_name:
                    raise ValueError(
                        f"PAC import contains duplicate submesh name '{sm.name}'. "
                        "Keep unique object names when exporting OBJ from Blender."
                    )
                imported_by_name[sm.name] = copy.deepcopy(sm)
            else:
                unknown_named.append(copy.deepcopy(sm))
        else:
            unnamed.append(copy.deepcopy(sm))

    heuristic_by_name: dict[str, SubMesh] = {}
    unmatched_originals = [
        copy.deepcopy(sm)
        for sm in original_mesh.submeshes
        if sm.name not in imported_by_name
    ]
    for imported_unknown in sorted(unknown_named, key=lambda sm: len(sm.vertices), reverse=True):
        if not unmatched_originals:
            raise ValueError(
                "PAC import contains more renamed submeshes than the original mesh can match."
            )
        best_original = min(
            unmatched_originals,
            key=lambda original_sm: _pac_submesh_match_score(imported_unknown, original_sm),
        )
        imported_unknown.name = best_original.name
        if not imported_unknown.material:
            imported_unknown.material = best_original.material
        heuristic_by_name[best_original.name] = imported_unknown
        unmatched_originals = [sm for sm in unmatched_originals if sm.name != best_original.name]

    merged_submeshes: list[SubMesh] = []
    unnamed_iter = iter(unnamed)
    used_named = 0
    for original_sm in original_mesh.submeshes:
        replacement = imported_by_name.get(original_sm.name)
        if replacement is None:
            replacement = heuristic_by_name.get(original_sm.name)
        if replacement is not None:
            merged_submeshes.append(replacement)
            used_named += 1
            continue

        try:
            merged_submeshes.append(next(unnamed_iter))
        except StopIteration:
            merged_submeshes.append(copy.deepcopy(original_sm))

    try:
        extra_unnamed = next(unnamed_iter)
    except StopIteration:
        extra_unnamed = None
    if extra_unnamed is not None:
        raise ValueError(
            "PAC import contains extra unnamed submeshes that could not be matched to the original mesh."
        )

    if used_named == 0 and imported_mesh.submeshes and len(imported_mesh.submeshes) != len(original_mesh.submeshes):
        raise ValueError(
            "PAC import only contained a partial mesh without recognizable original submesh names."
        )

    merged = copy.deepcopy(imported_mesh)
    merged.submeshes = merged_submeshes
    merged.total_vertices = sum(len(sm.vertices) for sm in merged_submeshes)
    merged.total_faces = sum(len(sm.faces) for sm in merged_submeshes)
    merged.has_uvs = any(sm.uvs for sm in merged_submeshes)
    merged.has_bones = any(sm.bone_indices for sm in merged_submeshes)
    return merged


def _pack_pac_normal(normal: tuple[float, float, float], existing_packed: int = 0) -> int:
    """Pack a float normal back into the PAC 10:10:10 layout."""

    def _enc(value: float) -> int:
        value = max(-1.0, min(1.0, value))
        return max(0, min(1023, round((value + 1.0) * 511.5)))

    nx, ny, nz = normal
    packed = _enc(nz) | (_enc(nx) << 10) | (_enc(ny) << 20)
    return (existing_packed & 0xC0000000) | packed


def _choose_pac_donor_indices(orig_sm: SubMesh, new_sm: SubMesh) -> list[int]:
    """Choose the closest original PAC vertex record to clone for each new vertex.

    PAC vertex records carry bone indices, bone weights, packed normals and a
    handful of engine-internal bytes that the OBJ round-trip cannot preserve.
    For every new vertex we need a *donor* — the original vertex whose record
    we clone before overwriting position/UV/normal. Exact position matches win
    when available (the common "user only moved a few verts" case); otherwise
    we fall back to the spatially nearest donor.

    Algorithm:
      1. Exact lookup via a dict keyed on positions rounded to 1e-5.
      2. For misses, a uniform spatial hash returns O(1) candidates on average
         and guarantees correctness by expanding the search shell until a
         donor is found. This replaces the previous O(n²) linear scan, which
         was tolerable for 500-vert weapons but catastrophic on 20k-vert
         character bodies.
    """
    n_orig = len(orig_sm.vertices)
    n_new = len(new_sm.vertices)
    if n_orig == 0:
        return [0] * n_new

    exact_map: dict[tuple[int, int, int], int] = {}
    for orig_idx, pos in enumerate(orig_sm.vertices):
        key = (round(pos[0] * 100000), round(pos[1] * 100000), round(pos[2] * 100000))
        # First writer wins — matches previous exact_map[...].append + [0] behaviour.
        exact_map.setdefault(key, orig_idx)

    # Short-circuit the linear scan for very small meshes where the spatial
    # index overhead isn't worth it.
    if n_orig <= 64:
        donor_indices: list[int] = []
        for new_pos in new_sm.vertices:
            key = (round(new_pos[0] * 100000), round(new_pos[1] * 100000), round(new_pos[2] * 100000))
            exact = exact_map.get(key)
            if exact is not None:
                donor_indices.append(exact)
                continue
            best_idx = 0
            best_dist = float("inf")
            for orig_idx in range(n_orig):
                ox, oy, oz = orig_sm.vertices[orig_idx]
                dx = new_pos[0] - ox
                dy = new_pos[1] - oy
                dz = new_pos[2] - oz
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq < best_dist:
                    best_dist = dist_sq
                    best_idx = orig_idx
            donor_indices.append(best_idx)
        return donor_indices

    # Build a uniform spatial hash. Cell size is set so the mean occupancy is
    # around the cube-root of the vertex count, which balances lookup cost
    # (cells per shell) against candidate cost (verts per cell).
    xs = [v[0] for v in orig_sm.vertices]
    ys = [v[1] for v in orig_sm.vertices]
    zs = [v[2] for v in orig_sm.vertices]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)

    extent = max(max_x - min_x, max_y - min_y, max_z - min_z, 1e-6)
    # Target ~8 verts per cell at equilibrium.
    target_cells_per_axis = max(2, int(round((n_orig / 8.0) ** (1.0 / 3.0))))
    cell_size = extent / target_cells_per_axis
    if cell_size < 1e-6:
        cell_size = 1e-6
    inv_cell = 1.0 / cell_size

    def _cell_key(x: float, y: float, z: float) -> tuple[int, int, int]:
        return (
            int((x - min_x) * inv_cell),
            int((y - min_y) * inv_cell),
            int((z - min_z) * inv_cell),
        )

    grid: dict[tuple[int, int, int], list[int]] = {}
    for orig_idx in range(n_orig):
        ox, oy, oz = orig_sm.vertices[orig_idx]
        grid.setdefault(_cell_key(ox, oy, oz), []).append(orig_idx)

    donor_indices = []
    max_shell = target_cells_per_axis + 1  # guarantees coverage of the whole mesh

    for new_pos in new_sm.vertices:
        key = (round(new_pos[0] * 100000), round(new_pos[1] * 100000), round(new_pos[2] * 100000))
        exact = exact_map.get(key)
        if exact is not None:
            donor_indices.append(exact)
            continue

        cx, cy, cz = _cell_key(new_pos[0], new_pos[1], new_pos[2])
        best_idx = 0
        best_dist = float("inf")

        shell = 0
        while shell <= max_shell:
            # Scan all cells in the current shell (surface of a cube of radius `shell`
            # centred on the query cell). Shell 0 is just the query cell itself.
            lo, hi = cx - shell, cx + shell
            for ix in range(lo, hi + 1):
                for iy in range(cy - shell, cy + shell + 1):
                    for iz in range(cz - shell, cz + shell + 1):
                        # Only evaluate the cube's surface on shells > 0 to avoid
                        # re-scanning interior cells from earlier shells.
                        if shell > 0 and (
                            lo < ix < hi
                            and cy - shell < iy < cy + shell
                            and cz - shell < iz < cz + shell
                        ):
                            continue
                        bucket = grid.get((ix, iy, iz))
                        if not bucket:
                            continue
                        for orig_idx in bucket:
                            ox, oy, oz = orig_sm.vertices[orig_idx]
                            dx = new_pos[0] - ox
                            dy = new_pos[1] - oy
                            dz = new_pos[2] - oz
                            dist_sq = dx * dx + dy * dy + dz * dz
                            if dist_sq < best_dist:
                                best_dist = dist_sq
                                best_idx = orig_idx

            # Termination test. We just completed shell `s`. The nearest point in
            # any cell at chebyshev distance `s+1` sits at euclidean distance
            # at least `s * cell_size` from the query (geometry of unit cubes).
            # So we can stop once best_dist < (s * cell_size)^2.
            #
            # Note: for s == 0 this condition is `best_dist < 0`, which never
            # holds — we always advance to shell 1 even when shell 0 had a hit,
            # because a chebyshev-1 cell can still contain an arbitrarily close
            # point when the query sits near its own cell's boundary.
            if best_dist < float("inf") and (shell * cell_size) ** 2 > best_dist:
                break
            shell += 1

        donor_indices.append(best_idx)

    return donor_indices


def _pac_needs_full_rebuild(original_mesh: ParsedMesh, working_mesh: ParsedMesh) -> bool:
    """Return True when the PAC import changed topology or needs a fresh serializer."""
    if len(original_mesh.submeshes) != len(working_mesh.submeshes):
        return True

    for orig_sm, new_sm in zip(original_mesh.submeshes, working_mesh.submeshes):
        if len(orig_sm.vertices) != len(new_sm.vertices):
            return True
        if len(orig_sm.faces) != len(new_sm.faces):
            return True
        if orig_sm.source_vertex_stride < 12:
            return True
        if len(orig_sm.source_vertex_offsets) != len(orig_sm.vertices):
            return True
        if orig_sm.source_descriptor_offset < 0:
            return True
    return False


def _build_pac_in_place(
    original_mesh: ParsedMesh,
    working_mesh: ParsedMesh,
    original_data: bytes,
) -> bytes:
    """Patch a PAC binary in place while preserving its existing layout."""
    result = bytearray(original_data)
    vertex_updates: dict[int, bytes] = {}
    index_updates: dict[int, bytes] = {}

    for sm_idx, (orig_sm, new_sm) in enumerate(zip(original_mesh.submeshes, working_mesh.submeshes)):
        if len(orig_sm.vertices) != len(new_sm.vertices):
            raise ValueError(
                f"PAC submesh {sm_idx} changed vertex count "
                f"({len(orig_sm.vertices)} -> {len(new_sm.vertices)}). "
                "Keep the same topology when importing OBJ for PAC meshes."
            )
        if len(orig_sm.faces) != len(new_sm.faces):
            raise ValueError(
                f"PAC submesh {sm_idx} changed face count "
                f"({len(orig_sm.faces)} -> {len(new_sm.faces)}). "
                "Keep the same topology when importing OBJ for PAC meshes."
            )
        if orig_sm.source_vertex_stride < 12:
            raise ValueError(
                f"PAC submesh {sm_idx} is missing source vertex metadata and cannot be rebuilt safely."
            )

        bmin, bmax = _compute_bbox(new_sm.vertices)
        extent = tuple(bmax[i] - bmin[i] for i in range(3))
        _patch_pac_descriptor_bounds(result, orig_sm.source_descriptor_offset, bmin, extent)

        new_uvs = new_sm.uvs if len(new_sm.uvs) == len(new_sm.vertices) else []

        for vi, rec_off in enumerate(orig_sm.source_vertex_offsets):
            if rec_off < 0 or rec_off + orig_sm.source_vertex_stride > len(result):
                raise ValueError(
                    f"PAC vertex record {vi} for submesh {sm_idx} points outside the file."
                )

            rec = bytearray(result[rec_off:rec_off + orig_sm.source_vertex_stride])
            vx, vy, vz = new_sm.vertices[vi]
            struct.pack_into(
                "<HHH",
                rec,
                0,
                _quantize_pac_u16(vx, bmin[0], extent[0]),
                _quantize_pac_u16(vy, bmin[1], extent[1]),
                _quantize_pac_u16(vz, bmin[2], extent[2]),
            )

            if new_uvs:
                try:
                    struct.pack_into("<e", rec, 8, new_uvs[vi][0])
                    struct.pack_into("<e", rec, 10, new_uvs[vi][1])
                except (OverflowError, ValueError):
                    struct.pack_into("<e", rec, 8, 0.0)
                    struct.pack_into("<e", rec, 10, 0.0)

            payload = bytes(rec)
            prev = vertex_updates.get(rec_off)
            if prev is not None and prev != payload:
                raise ValueError(
                    "PAC import edited a shared vertex buffer inconsistently across submeshes. "
                    "Apply the same change to every linked PAC submesh before reimport."
                )
            vertex_updates[rec_off] = payload

        if orig_sm.source_index_offset >= 0:
            for fi, (a, b, c) in enumerate(new_sm.faces):
                if a >= len(new_sm.vertices) or b >= len(new_sm.vertices) or c >= len(new_sm.vertices):
                    raise ValueError(f"PAC face {fi} in submesh {sm_idx} references an out-of-range vertex.")
                face_off = orig_sm.source_index_offset + fi * 6
                if face_off + 6 > len(result):
                    raise ValueError(
                        f"PAC face record {fi} for submesh {sm_idx} points outside the file."
                    )
                payload = struct.pack("<HHH", a, b, c)
                prev = index_updates.get(face_off)
                if prev is not None and prev != payload:
                    raise ValueError(
                        "PAC import edited a shared index buffer inconsistently across submeshes."
                    )
                index_updates[face_off] = payload

    for rec_off, payload in vertex_updates.items():
        result[rec_off:rec_off + len(payload)] = payload
    for face_off, payload in index_updates.items():
        result[face_off:face_off + len(payload)] = payload

    logger.info(
        "Built PAC %s with in-place patching: %d submeshes, %d verts, %d faces",
        working_mesh.path,
        len(working_mesh.submeshes),
        sum(len(sm.vertices) for sm in working_mesh.submeshes),
        sum(len(sm.faces) for sm in working_mesh.submeshes),
    )
    return bytes(result)


def _build_pac_full_rebuild(
    original_mesh: ParsedMesh,
    working_mesh: ParsedMesh,
    original_data: bytes,
) -> bytes:
    """Rebuild PAC geometry sections from scratch for topology-changing imports."""
    sections = _parse_par_sections(original_data)
    sec_by_idx = {sec["index"]: sec for sec in sections}
    sec0 = sec_by_idx.get(0)
    if not sec0:
        raise ValueError("PAC section table is missing section 0.")

    n_lods = original_data[sec0["offset"] + 4] if sec0["size"] >= 5 else 0
    if n_lods <= 0 or n_lods > 10:
        raise ValueError(f"Invalid PAC LOD count: {n_lods}")

    descriptors = _find_pac_descriptors(original_data, sec0["offset"], sec0["size"], n_lods)
    if len(descriptors) < len(working_mesh.submeshes):
        raise ValueError("PAC descriptor count does not match the parsed submesh set.")

    sec0_data = bytearray(original_data[sec0["offset"]:sec0["offset"] + sec0["size"]])
    preserved_sections = {
        sec["index"]: original_data[sec["offset"]:sec["offset"] + sec["size"]]
        for sec in sections
        if sec["index"] > n_lods
    }

    prepared_submeshes = []
    for sm_idx, (orig_sm, new_sm, desc) in enumerate(zip(original_mesh.submeshes, working_mesh.submeshes, descriptors)):
        if not orig_sm.source_vertex_offsets or orig_sm.source_vertex_stride < 12:
            raise ValueError(
                f"PAC submesh {sm_idx} is missing source vertex metadata for a full rebuild."
            )

        donor_records = []
        for rec_off in orig_sm.source_vertex_offsets:
            if rec_off < 0 or rec_off + orig_sm.source_vertex_stride > len(original_data):
                raise ValueError(
                    f"PAC vertex record for submesh {sm_idx} points outside the file."
                )
            donor_records.append(original_data[rec_off:rec_off + orig_sm.source_vertex_stride])

        donor_indices = _choose_pac_donor_indices(orig_sm, new_sm)
        normals = (
            new_sm.normals
            if len(new_sm.normals) == len(new_sm.vertices)
            else _compute_smooth_normals(new_sm.vertices, new_sm.faces)
        )
        new_uvs = new_sm.uvs if len(new_sm.uvs) == len(new_sm.vertices) else []
        bmin, bmax = _compute_bbox(new_sm.vertices)
        extent = tuple(bmax[i] - bmin[i] for i in range(3))
        stored_lod_count = max(1, min(n_lods, orig_sm.source_lod_count or desc.stored_lod_count or n_lods))

        rel_desc_off = desc.descriptor_offset - sec0["offset"]
        if rel_desc_off < 0 or rel_desc_off + 40 > len(sec0_data):
            raise ValueError(f"PAC descriptor {sm_idx} points outside section 0.")

        _patch_pac_descriptor_bounds(sec0_data, rel_desc_off, bmin, extent)
        vc_off = rel_desc_off + 40
        ic_off = vc_off + desc.stored_lod_count * 2
        new_vert_count = len(new_sm.vertices)
        new_index_count = len(new_sm.faces) * 3
        for lod_idx in range(desc.stored_lod_count):
            struct.pack_into("<H", sec0_data, vc_off + lod_idx * 2, new_vert_count)
            struct.pack_into("<I", sec0_data, ic_off + lod_idx * 4, new_index_count)

        prepared_submeshes.append({
            "submesh": new_sm,
            "donor_records": donor_records,
            "donor_indices": donor_indices,
            "normals": normals,
            "uvs": new_uvs,
            "bbox_min": bmin,
            "bbox_extent": extent,
            "stored_lod_count": stored_lod_count,
        })

    lod_payloads: dict[int, bytes] = {}
    lod_split_bytes: dict[int, int] = {}
    for sec_idx in range(1, n_lods + 1):
        lod_idx = n_lods - sec_idx
        verts_buf = bytearray()
        idx_buf = bytearray()

        for sm_idx, prepared in enumerate(prepared_submeshes):
            if lod_idx >= prepared["stored_lod_count"]:
                continue

            sm = prepared["submesh"]
            donor_records = prepared["donor_records"]
            donor_indices = prepared["donor_indices"]
            normals = prepared["normals"]
            new_uvs = prepared["uvs"]
            bbox_min = prepared["bbox_min"]
            bbox_extent = prepared["bbox_extent"]

            for vi, vertex in enumerate(sm.vertices):
                donor_rec = bytearray(donor_records[donor_indices[vi]])
                struct.pack_into(
                    "<HHH",
                    donor_rec,
                    0,
                    _quantize_pac_u16(vertex[0], bbox_min[0], bbox_extent[0]),
                    _quantize_pac_u16(vertex[1], bbox_min[1], bbox_extent[1]),
                    _quantize_pac_u16(vertex[2], bbox_min[2], bbox_extent[2]),
                )

                if len(donor_rec) >= 12:
                    if new_uvs:
                        try:
                            struct.pack_into("<e", donor_rec, 8, new_uvs[vi][0])
                            struct.pack_into("<e", donor_rec, 10, new_uvs[vi][1])
                        except (OverflowError, ValueError):
                            struct.pack_into("<e", donor_rec, 8, 0.0)
                            struct.pack_into("<e", donor_rec, 10, 0.0)

                if len(donor_rec) >= 20:
                    existing_normal = struct.unpack_from("<I", donor_rec, 16)[0]
                    struct.pack_into(
                        "<I",
                        donor_rec,
                        16,
                        _pack_pac_normal(normals[vi], existing_normal),
                    )

                verts_buf.extend(donor_rec)

            for face in sm.faces:
                a, b, c = face
                if a >= len(sm.vertices) or b >= len(sm.vertices) or c >= len(sm.vertices):
                    raise ValueError(f"PAC face in submesh {sm_idx} references an out-of-range vertex.")
                idx_buf.extend(struct.pack("<HHH", a, b, c))

        lod_split_bytes[sec_idx] = len(verts_buf)
        lod_payloads[sec_idx] = bytes(verts_buf + idx_buf)

    section_payloads: dict[int, bytes] = {0: bytes(sec0_data)}
    section_payloads.update(lod_payloads)
    section_payloads.update(preserved_sections)

    header = bytearray(original_data[:0x50])
    for slot in range(8):
        struct.pack_into("<I", header, 0x10 + slot * 8, 0)
        struct.pack_into("<I", header, 0x10 + slot * 8 + 4, 0)

    section_offsets = {0: 0x50}
    next_offset = 0x50 + len(section_payloads[0])
    for slot in range(1, 8):
        payload = section_payloads.get(slot)
        if payload is None:
            continue
        section_offsets[slot] = next_offset
        next_offset += len(payload)

    off = 5
    for lod_idx in range(n_lods):
        sec_idx = n_lods - lod_idx
        struct.pack_into("<I", sec0_data, off + lod_idx * 4, section_offsets[sec_idx])
    off += n_lods * 4
    for lod_idx in range(n_lods):
        sec_idx = n_lods - lod_idx
        split_abs = section_offsets[sec_idx] + lod_split_bytes.get(sec_idx, 0)
        struct.pack_into("<I", sec0_data, off + lod_idx * 4, split_abs)
    section_payloads[0] = bytes(sec0_data)

    assembled = bytearray(header)
    for slot in range(8):
        payload = section_payloads.get(slot)
        if payload is None:
            continue
        struct.pack_into("<I", assembled, 0x10 + slot * 8, 0)
        struct.pack_into("<I", assembled, 0x10 + slot * 8 + 4, len(payload))
        assembled.extend(payload)

    logger.info(
        "Built PAC %s with full rebuild: %d bytes, %d submeshes, %d verts, %d faces",
        working_mesh.path,
        len(assembled),
        len(working_mesh.submeshes),
        sum(len(sm.vertices) for sm in working_mesh.submeshes),
        sum(len(sm.faces) for sm in working_mesh.submeshes),
    )
    return bytes(assembled)


def build_pac(mesh: ParsedMesh, original_data: bytes) -> bytes:
    """Rebuild a PAC binary from a modified mesh."""
    if not original_data or original_data[:4] != b"PAR ":
        raise ValueError("Original PAC data required for rebuild")

    original_mesh = parse_pac(original_data, mesh.path)
    if not original_mesh.submeshes:
        raise ValueError("Original PAC could not be parsed into usable geometry")

    working_mesh = copy.deepcopy(mesh)
    working_mesh = _merge_partial_pac_import(original_mesh, working_mesh)
    _align_submesh_order_like_original(original_mesh, working_mesh)

    if len(original_mesh.submeshes) != len(working_mesh.submeshes):
        raise ValueError(
            "PAC import currently requires the same submesh count as the original mesh."
        )

    if _pac_needs_full_rebuild(original_mesh, working_mesh):
        return _build_pac_full_rebuild(original_mesh, working_mesh, original_data)
    return _build_pac_in_place(original_mesh, working_mesh, original_data)


def build_mesh(mesh: ParsedMesh, original_data: bytes) -> bytes:
    """Auto-detect format and rebuild binary from modified mesh.

    Args:
        mesh: Modified ParsedMesh (from import_obj or manual modification).
        original_data: Original binary data (needed for metadata preservation).

    Returns:
        New binary data ready for repack.
    """
    fmt = mesh.format.lower()
    if fmt == "pac":
        return build_pac(mesh, original_data)
    elif fmt == "pam":
        return build_pam(mesh, original_data)
    elif fmt == "pamlod":
        return build_pamlod(mesh, original_data)
    else:
        raise ValueError(f"Unsupported mesh format for rebuild: {fmt}")
