"""PAM / PAMLOD / PAC mesh parser for Crimson Desert.

Parses Pearl Abyss 3D mesh files from PAZ archives into an intermediate
representation (vertices, UVs, normals, faces, materials, bones, weights)
that can be exported to OBJ, FBX, or rendered in the 3D preview.

Format overview (all share the 'PAR ' magic):
  PAM     — static meshes (objects, props, world geometry)
  PAMLOD  — LOD variants (5 quality levels per mesh)
  PAC     — skinned character meshes (with bone indices + weights)

Vertex positions are uint16-quantized and dequantized using the per-file
bounding box.  UVs are stored as float16 at vertex offset +8/+10.  Bone
weights (PAC only) follow the UV data.
"""

from __future__ import annotations

import os
import re
import struct
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.mesh_parser")

# ── Constants ────────────────────────────────────────────────────────

PAR_MAGIC = b"PAR "

# PAM header offsets
HDR_MESH_COUNT = 0x10
HDR_BBOX_MIN = 0x14
HDR_BBOX_MAX = 0x20
HDR_GEOM_OFF = 0x3C

# Submesh table
SUBMESH_TABLE = 0x410
SUBMESH_STRIDE = 0x218
SUBMESH_TEX_OFF = 0x10
SUBMESH_MAT_OFF = 0x110

# Global-buffer prefab constants
GLOBAL_VERT_BASE = 3068
PAM_IDX_OFF = 0x19840

# PAMLOD header offsets
PAMLOD_LOD_COUNT = 0x00
PAMLOD_GEOM_OFF = 0x04
PAMLOD_BBOX_MIN = 0x10
PAMLOD_BBOX_MAX = 0x1C
PAMLOD_ENTRY_TABLE = 0x50

# Stride candidates for auto-detection
STRIDE_CANDIDATES = [6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36, 40, 44, 48, 52, 56, 60, 64]


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class MeshVertex:
    """Single vertex with position, UV, and optional bone data."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    u: float = 0.0
    v: float = 0.0
    nx: float = 0.0
    ny: float = 1.0
    nz: float = 0.0
    bone_indices: tuple[int, ...] = ()
    bone_weights: tuple[float, ...] = ()


@dataclass
class SubMesh:
    """A submesh within a PAM/PAC file."""
    name: str = ""
    material: str = ""
    texture: str = ""
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, int, int]] = field(default_factory=list)
    bone_indices: list[tuple[int, ...]] = field(default_factory=list)
    bone_weights: list[tuple[float, ...]] = field(default_factory=list)
    vertex_count: int = 0
    face_count: int = 0
    source_vertex_offsets: list[int] = field(default_factory=list)
    source_index_offset: int = -1
    source_index_count: int = 0
    source_vertex_stride: int = 0
    source_descriptor_offset: int = -1
    source_bbox_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    source_bbox_extent: tuple[float, float, float] = (0.0, 0.0, 0.0)
    source_lod_count: int = 0


@dataclass
class ParsedMesh:
    """Complete parsed mesh file."""
    path: str = ""
    format: str = ""  # "pam", "pamlod", "pac"
    bbox_min: tuple[float, float, float] = (0, 0, 0)
    bbox_max: tuple[float, float, float] = (0, 0, 0)
    submeshes: list[SubMesh] = field(default_factory=list)
    lod_levels: list[list[SubMesh]] = field(default_factory=list)  # PAMLOD only
    total_vertices: int = 0
    total_faces: int = 0
    has_uvs: bool = False
    has_bones: bool = False


@dataclass
class PreviewMesh:
    """Flattened buffers used by the Explorer preview."""
    format: str = ""
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, int, int]] = field(default_factory=list)
    submesh_count: int = 0
    total_vertices: int = 0
    total_faces: int = 0


@dataclass
class PacDescriptor:
    """Per-submesh PAC metadata recovered from section 0."""
    name: str
    material: str
    bbox_min: tuple[float, float, float]
    bbox_extent: tuple[float, float, float]
    vertex_counts: list[int]
    index_counts: list[int]
    palette: tuple[int, ...] = ()
    descriptor_offset: int = 0
    stored_lod_count: int = 0


# ── Utility ──────────────────────────────────────────────────────────

def _dequant_u16(v: int, mn: float, mx: float) -> float:
    """uint16 → float: bbox_min + (v / 65535) * (bbox_max - bbox_min)."""
    return mn + (v / 65535.0) * (mx - mn)


def _dequant_i16(v: int, mn: float, mx: float) -> float:
    """int16 → float (legacy global-buffer format)."""
    return mn + ((v + 32768) / 65536.0) * (mx - mn)


def _compute_face_normal(v0, v1, v2):
    """Compute face normal from 3 vertex positions."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length > 1e-8:
        return (nx / length, ny / length, nz / length)
    return (0.0, 1.0, 0.0)


def _compute_smooth_normals(vertices, faces):
    """Compute per-vertex smooth normals by averaging adjacent face normals."""
    normals = [[0.0, 0.0, 0.0] for _ in range(len(vertices))]
    for a, b, c in faces:
        if a < len(vertices) and b < len(vertices) and c < len(vertices):
            fn = _compute_face_normal(vertices[a], vertices[b], vertices[c])
            for idx in (a, b, c):
                normals[idx][0] += fn[0]
                normals[idx][1] += fn[1]
                normals[idx][2] += fn[2]
    result = []
    for n in normals:
        length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        if length > 1e-8:
            result.append((n[0] / length, n[1] / length, n[2] / length))
        else:
            result.append((0.0, 1.0, 0.0))
    return result


# ── Stride detection ─────────────────────────────────────────────────

def _detect_pac_vertex_stride(data: bytes, vert_start: int, split_off: int) -> int:
    """Detect PAC vertex stride using the constant marker at byte offset +12."""
    vert_region_size = split_off - vert_start
    if vert_region_size <= 0:
        return 40

    best_stride = 40
    best_hits = -1
    candidate_order = [40, 36, 32, 44, 48, 52, 56, 60, 64, 28, 24, 20, 16, 12, 8, 6]

    for stride in candidate_order:
        sample_count = min(64, vert_region_size // stride)
        if sample_count < 4:
            continue

        hits = 0
        for i in range(sample_count):
            rec_off = vert_start + i * stride
            if rec_off + 16 > split_off:
                break
            if struct.unpack_from("<I", data, rec_off + 12)[0] == 0x3C000000:
                hits += 1

        if hits > best_hits or (hits == best_hits and abs(stride - 40) < abs(best_stride - 40)):
            best_stride = stride
            best_hits = hits

    return best_stride


def _find_local_stride(data: bytes, geom_off: int, voff: int, n_verts: int, n_idx: int):
    """Detect vertex stride for per-mesh layout where indices follow vertex data."""
    for stride in STRIDE_CANDIDATES:
        vert_start = geom_off + voff
        idx_off = vert_start + n_verts * stride
        if idx_off + n_idx * 2 > len(data):
            continue
        # Validate: all index values must be < n_verts
        valid = True
        for j in range(min(n_idx, 100)):  # sample first 100 for speed
            val = struct.unpack_from("<H", data, idx_off + j * 2)[0]
            if val >= n_verts:
                valid = False
                break
        if valid:
            # Full validation on remaining
            if n_idx > 100:
                valid = all(
                    struct.unpack_from("<H", data, idx_off + j * 2)[0] < n_verts
                    for j in range(100, n_idx)
                )
            if valid:
                return stride, idx_off
    return None, None


# ── PAM Parser ───────────────────────────────────────────────────────

def parse_pam(data: bytes, filename: str = "") -> ParsedMesh:
    """Parse a .pam static mesh file."""
    if len(data) < 0x40 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAM file: bad magic {data[:4]!r}")

    result = ParsedMesh(path=filename, format="pam")
    result.bbox_min = struct.unpack_from("<fff", data, HDR_BBOX_MIN)
    result.bbox_max = struct.unpack_from("<fff", data, HDR_BBOX_MAX)
    geom_off = struct.unpack_from("<I", data, HDR_GEOM_OFF)[0]
    mesh_count = struct.unpack_from("<I", data, HDR_MESH_COUNT)[0]
    bmin, bmax = result.bbox_min, result.bbox_max

    # Read submesh table
    raw_entries = []
    for i in range(mesh_count):
        off = SUBMESH_TABLE + i * SUBMESH_STRIDE
        if off + SUBMESH_STRIDE > len(data):
            break
        nv = struct.unpack_from("<I", data, off)[0]
        ni = struct.unpack_from("<I", data, off + 4)[0]
        ve = struct.unpack_from("<I", data, off + 8)[0]
        ie = struct.unpack_from("<I", data, off + 12)[0]
        tex = data[off + SUBMESH_TEX_OFF:off + SUBMESH_TEX_OFF + 256].split(b"\x00")[0].decode("ascii", "replace")
        mat = data[off + SUBMESH_MAT_OFF:off + SUBMESH_MAT_OFF + 256].split(b"\x00")[0].decode("ascii", "replace")
        raw_entries.append({"i": i, "nv": nv, "ni": ni, "ve": ve, "ie": ie, "tex": tex, "mat": mat})

    # Detect combined-buffer layout
    is_combined = False
    if mesh_count > 1:
        ve_acc = ie_acc = 0
        is_combined = True
        for r in raw_entries:
            if r["ve"] != ve_acc or r["ie"] != ie_acc:
                is_combined = False
                break
            ve_acc += r["nv"]
            ie_acc += r["ni"]

    if is_combined:
        _parse_combined_buffer(data, raw_entries, geom_off, bmin, bmax, result)
    else:
        _parse_independent_meshes(data, raw_entries, geom_off, bmin, bmax, result)

    primary_total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
    has_invalid_offsets = any(
        off < geom_off or off + 6 > len(data)
        for sm in result.submeshes
        for off in sm.source_vertex_offsets
    )

    # Fallback: scan for vertex+index blocks when the primary table-based parse
    # found no usable geometry, or when it produced impossible vertex offsets.
    # Some extended-layout PAMs need the scan path; others should not be parsed
    # twice, or they end up with duplicated submeshes.
    if mesh_count > 0 and (primary_total_vertices == 0 or has_invalid_offsets):
        result.submeshes.clear()
        _parse_scan_fallback(data, raw_entries, geom_off, bmin, bmax, result)

    # Compute normals for all submeshes
    for sm in result.submeshes:
        sm.normals = _compute_smooth_normals(sm.vertices, sm.faces)

    result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
    result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
    result.has_uvs = any(sm.uvs for sm in result.submeshes)

    logger.info("Parsed PAM %s: %d submeshes, %d verts, %d faces",
                filename, len(result.submeshes), result.total_vertices, result.total_faces)
    return result


def _parse_independent_meshes(data, entries, geom_off, bmin, bmax, result):
    """Parse PAM with per-submesh or global vertex buffers."""
    idx_avail = (len(data) - PAM_IDX_OFF) // 2

    for r in entries:
        i, nv, ni, voff, ioff = r["i"], r["nv"], r["ni"], r["ve"], r["ie"]
        tex, mat = r["tex"], r["mat"]

        # Try local layout first
        stride, idx_off = _find_local_stride(data, geom_off, voff, nv, ni)

        if stride is not None:
            verts, uvs, faces, offsets = _extract_local_mesh(
                data, geom_off, voff, stride, idx_off, nv, ni, bmin, bmax
            )
        elif ioff + ni <= idx_avail:
            verts, uvs, faces, offsets = _extract_global_mesh(data, geom_off, ni, ioff, bmin, bmax)
        else:
            continue

        sm = SubMesh(
            name=f"mesh_{i:02d}_{mat or str(i)}",
            material=mat, texture=tex,
            vertices=verts, uvs=uvs, faces=faces,
            source_vertex_offsets=offsets,
            vertex_count=len(verts), face_count=len(faces),
        )
        result.submeshes.append(sm)


def _parse_scan_fallback(data, entries, geom_off, bmin, bmax, result):
    """Fallback parser: scan for vertex+index blocks in extended-layout PAMs.

    Breakable/destructible PAMs often have extra metadata (physics, destruction
    fragments) between the header and the actual geometry. This scanner probes
    the region after geom_off to locate the real vertex positions (uint16
    quantized) and matching index block.
    """
    total_v = sum(r["nv"] for r in entries)
    total_i = sum(r["ni"] for r in entries)
    if total_v < 3 or total_i < 3:
        return

    search_limit = min(len(data) - 100, geom_off + min(len(data) // 2, 2000000))

    # Scan for a block of u16 values that look like quantized vertex positions
    # (spread across the 0-65535 range), followed by valid indices.
    # Step by 2 in small files, step by 4 in large files for speed.
    step = 2 if (search_limit - geom_off) < 500000 else 4
    for scan_start in range(geom_off, search_limit, step):
        # Quick check: read 10 potential XYZ triples (stride 6)
        if scan_start + 60 > len(data):
            break
        vals = [struct.unpack_from("<H", data, scan_start + j * 2)[0] for j in range(30)]
        spread = max(vals) - min(vals)
        if spread < 5000:
            continue

        # Found candidate vertex data. Try common strides
        for try_stride in [6, 8, 10, 12, 14, 16, 20, 24, 28, 32]:
            test_idx_off = scan_start + total_v * try_stride
            if test_idx_off + total_i * 2 > len(data):
                continue

            # Validate: first 50 indices must be < total_v
            valid = True
            for j in range(min(50, total_i)):
                v = struct.unpack_from("<H", data, test_idx_off + j * 2)[0]
                if v >= total_v:
                    valid = False
                    break
            if not valid:
                continue

            # Full validation on a larger sample
            valid = all(
                struct.unpack_from("<H", data, test_idx_off + j * 2)[0] < total_v
                for j in range(min(total_i, 500))
            )
            if not valid:
                continue

            # Found valid layout! Parse as combined buffer from this offset
            logger.info("Scan fallback: found vertex data at 0x%X stride=%d for %s",
                        scan_start, try_stride, entries[0].get("tex", ""))

            has_uv = try_stride >= 12
            idx_base = test_idx_off

            for r in entries:
                nv, ni = r["nv"], r["ni"]
                vert_base = scan_start + r["ve"] * try_stride
                idx_off = idx_base + r["ie"] * 2

                indices = [struct.unpack_from("<H", data, idx_off + j * 2)[0]
                           for j in range(ni)]
                if not indices:
                    continue

                unique = sorted(set(indices))
                idx_map = {gi: li for li, gi in enumerate(unique)}

                verts, uvs, offsets = [], [], []
                for gi in unique:
                    foff = vert_base + gi * try_stride
                    if foff + 6 > len(data):
                        break
                    xu, yu, zu = struct.unpack_from("<HHH", data, foff)
                    offsets.append(foff)
                    verts.append((_dequant_u16(xu, bmin[0], bmax[0]),
                                  _dequant_u16(yu, bmin[1], bmax[1]),
                                  _dequant_u16(zu, bmin[2], bmax[2])))
                    if has_uv and foff + 12 <= len(data):
                        u = struct.unpack_from("<e", data, foff + 8)[0]
                        v = struct.unpack_from("<e", data, foff + 10)[0]
                        uvs.append((u, v))

                faces = []
                for j in range(0, ni - 2, 3):
                    a, b, c = indices[j], indices[j + 1], indices[j + 2]
                    if a in idx_map and b in idx_map and c in idx_map:
                        faces.append((idx_map[a], idx_map[b], idx_map[c]))

                sm = SubMesh(
                    name=f"mesh_{r['i']:02d}_{r['mat'] or str(r['i'])}",
                    material=r["mat"], texture=r["tex"],
                    vertices=verts, uvs=uvs, faces=faces,
                    source_vertex_offsets=offsets,
                    vertex_count=len(verts), face_count=len(faces),
                )
                result.submeshes.append(sm)

            result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
            result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
            result.has_uvs = any(sm.uvs for sm in result.submeshes)
            return  # Done

    # Second pass: scan BACKWARD from end of file for the index block
    # This handles files where extra per-vertex data creates non-integer strides
    for scan_end_off in range(len(data) - 2, geom_off + total_v * 6, -2):
        test_start = scan_end_off - total_i * 2 + 2
        if test_start < geom_off:
            break

        # Quick check first index
        first_val = struct.unpack_from("<H", data, test_start)[0]
        if first_val >= total_v:
            continue

        # Check first 30 indices
        valid = True
        for j in range(min(30, total_i)):
            v = struct.unpack_from("<H", data, test_start + j * 2)[0]
            if v >= total_v:
                valid = False
                break
        if not valid:
            continue

        # Deeper validation
        valid = all(
            struct.unpack_from("<H", data, test_start + j * 2)[0] < total_v
            for j in range(min(total_i, 300))
        )
        if not valid:
            continue

        # Full validation
        valid = all(
            struct.unpack_from("<H", data, test_start + j * 2)[0] < total_v
            for j in range(total_i)
        )
        if not valid:
            continue

        # Found index block! Calculate vertex region
        vert_region = test_start - geom_off
        # Try common strides that fit
        best_stride = None
        for try_stride in [6, 8, 10, 12, 14, 16, 20, 24, 28, 32]:
            expected_end = geom_off + total_v * try_stride
            # Allow up to 16KB padding between vertex data and index data
            if expected_end <= test_start and (test_start - expected_end) < 16384:
                best_stride = try_stride
                break

        if best_stride is None:
            # Use floor division of vert_region / total_v
            best_stride = vert_region // total_v
            if best_stride < 6:
                best_stride = 6

        has_uv = best_stride >= 12
        idx_base = test_start
        logger.info("Backward scan: found idx at 0x%X stride=%d for %d verts",
                    test_start, best_stride, total_v)

        for r in entries:
            nv, ni = r["nv"], r["ni"]
            vert_base = geom_off + r["ve"] * best_stride
            idx_off = idx_base + r["ie"] * 2

            indices = [struct.unpack_from("<H", data, idx_off + j * 2)[0]
                       for j in range(ni)]
            if not indices:
                continue

            unique = sorted(set(indices))
            idx_map = {gi: li for li, gi in enumerate(unique)}

            verts, uvs, offsets = [], [], []
            for gi in unique:
                foff = vert_base + gi * best_stride
                if foff + 6 > len(data):
                    break
                xu, yu, zu = struct.unpack_from("<HHH", data, foff)
                offsets.append(foff)
                verts.append((_dequant_u16(xu, bmin[0], bmax[0]),
                              _dequant_u16(yu, bmin[1], bmax[1]),
                              _dequant_u16(zu, bmin[2], bmax[2])))
                if has_uv and foff + 12 <= len(data):
                    u = struct.unpack_from("<e", data, foff + 8)[0]
                    v = struct.unpack_from("<e", data, foff + 10)[0]
                    uvs.append((u, v))

            faces = []
            for j in range(0, ni - 2, 3):
                a, b, c = indices[j], indices[j + 1], indices[j + 2]
                if a in idx_map and b in idx_map and c in idx_map:
                    faces.append((idx_map[a], idx_map[b], idx_map[c]))

            sm = SubMesh(
                name=f"mesh_{r['i']:02d}_{r['mat'] or str(r['i'])}",
                material=r["mat"], texture=r["tex"],
                vertices=verts, uvs=uvs, faces=faces,
                source_vertex_offsets=offsets,
                vertex_count=len(verts), face_count=len(faces),
            )
            result.submeshes.append(sm)

        result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
        result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
        result.has_uvs = any(sm.uvs for sm in result.submeshes)
        return

    logger.debug("Scan fallback: no valid vertex block found after 0x%X", geom_off)


def _parse_combined_buffer(data, entries, geom_off, bmin, bmax, result):
    """Parse PAM with shared vertex + index buffer."""
    total_verts = sum(r["nv"] for r in entries)
    total_idx = sum(r["ni"] for r in entries)
    avail = len(data) - geom_off

    target = (avail - total_idx * 2) / total_verts if total_verts else 0
    stride = min(STRIDE_CANDIDATES, key=lambda s: abs(s - target))
    if geom_off + total_verts * stride + total_idx * 2 > len(data):
        return

    idx_base = geom_off + total_verts * stride

    for r in entries:
        nv, ni = r["nv"], r["ni"]
        vert_base = geom_off + r["ve"] * stride
        idx_off = idx_base + r["ie"] * 2
        tex, mat = r["tex"], r["mat"]

        indices = [struct.unpack_from("<H", data, idx_off + j * 2)[0] for j in range(ni)]
        if not indices:
            continue

        unique = sorted(set(indices))
        idx_map = {gi: li for li, gi in enumerate(unique)}
        has_uv = stride >= 12

        verts, uvs, offsets = [], [], []
        for gi in unique:
            foff = vert_base + gi * stride
            if foff + 6 > len(data):
                break
            xu, yu, zu = struct.unpack_from("<HHH", data, foff)
            offsets.append(foff)
            verts.append((_dequant_u16(xu, bmin[0], bmax[0]),
                          _dequant_u16(yu, bmin[1], bmax[1]),
                          _dequant_u16(zu, bmin[2], bmax[2])))
            if has_uv and foff + 12 <= len(data):
                u = struct.unpack_from("<e", data, foff + 8)[0]
                v = struct.unpack_from("<e", data, foff + 10)[0]
                uvs.append((u, v))

        faces = []
        for j in range(0, ni - 2, 3):
            a, b, c = indices[j], indices[j + 1], indices[j + 2]
            if a in idx_map and b in idx_map and c in idx_map:
                faces.append((idx_map[a], idx_map[b], idx_map[c]))

        sm = SubMesh(
            name=f"mesh_{r['i']:02d}_{mat or str(r['i'])}",
            material=mat, texture=tex,
            vertices=verts, uvs=uvs, faces=faces,
            source_vertex_offsets=offsets,
            vertex_count=len(verts), face_count=len(faces),
        )
        result.submeshes.append(sm)


def _extract_local_mesh(data, geom_off, voff, stride, idx_off, nv, ni, bmin, bmax):
    """Extract vertices/uvs/faces from local (per-mesh) layout."""
    indices = [struct.unpack_from("<H", data, idx_off + j * 2)[0] for j in range(ni)]
    unique = sorted(set(indices))
    idx_map = {gi: li for li, gi in enumerate(unique)}
    has_uv = stride >= 12

    verts, uvs, offsets = [], [], []
    for gi in unique:
        foff = geom_off + voff + gi * stride
        if foff + 6 > len(data):
            break
        xu, yu, zu = struct.unpack_from("<HHH", data, foff)
        offsets.append(foff)
        verts.append((_dequant_u16(xu, bmin[0], bmax[0]),
                      _dequant_u16(yu, bmin[1], bmax[1]),
                      _dequant_u16(zu, bmin[2], bmax[2])))
        if has_uv and foff + 12 <= len(data):
            u = struct.unpack_from("<e", data, foff + 8)[0]
            v = struct.unpack_from("<e", data, foff + 10)[0]
            uvs.append((u, v))

    faces = []
    for j in range(0, ni - 2, 3):
        a, b, c = indices[j], indices[j + 1], indices[j + 2]
        if a in idx_map and b in idx_map and c in idx_map:
            faces.append((idx_map[a], idx_map[b], idx_map[c]))

    return verts, uvs, faces, offsets


def _extract_global_mesh(data, geom_off, ni, ioff, bmin, bmax):
    """Extract vertices/uvs/faces from global (prefab) layout."""
    indices = [struct.unpack_from("<H", data, PAM_IDX_OFF + (ioff + j) * 2)[0] for j in range(ni)]
    unique = sorted(set(indices))
    idx_map = {gi: li for li, gi in enumerate(unique)}

    verts = []
    offsets = []
    for gi in unique:
        li = gi - GLOBAL_VERT_BASE
        foff = geom_off + li * 6
        if foff + 6 > len(data):
            break
        xi, yi, zi = struct.unpack_from("<hhh", data, foff)
        offsets.append(foff)
        verts.append((_dequant_i16(xi, bmin[0], bmax[0]),
                      _dequant_i16(yi, bmin[1], bmax[1]),
                      _dequant_i16(zi, bmin[2], bmax[2])))

    faces = []
    for j in range(0, ni - 2, 3):
        a, b, c = indices[j], indices[j + 1], indices[j + 2]
        if a in idx_map and b in idx_map and c in idx_map:
            faces.append((idx_map[a], idx_map[b], idx_map[c]))

    return verts, [], faces, offsets


# ── PAMLOD Parser ────────────────────────────────────────────────────

def parse_pamlod(data: bytes, filename: str = "", lod_level: int = 0) -> ParsedMesh:
    """Parse a .pamlod LOD mesh file. lod_level=0 is highest quality."""
    result = ParsedMesh(path=filename, format="pamlod")

    lod_count = struct.unpack_from("<I", data, PAMLOD_LOD_COUNT)[0]
    geom_off = struct.unpack_from("<I", data, PAMLOD_GEOM_OFF)[0]
    if lod_count == 0 or geom_off == 0 or geom_off >= len(data):
        return result

    result.bbox_min = struct.unpack_from("<fff", data, PAMLOD_BBOX_MIN)
    result.bbox_max = struct.unpack_from("<fff", data, PAMLOD_BBOX_MAX)
    bmin, bmax = result.bbox_min, result.bbox_max

    # Locate LOD entries by scanning for .dds texture strings
    entries = []
    search_region = data[PAMLOD_ENTRY_TABLE:geom_off]
    for m in re.finditer(rb"[^\x00]{1,255}\.dds\x00", search_region):
        tex_start = PAMLOD_ENTRY_TABLE + m.start()
        nv_off = tex_start - 0x10
        if nv_off < PAMLOD_ENTRY_TABLE:
            continue
        nv = struct.unpack_from("<I", data, nv_off)[0]
        ni = struct.unpack_from("<I", data, nv_off + 4)[0]
        if not (1 <= nv <= 131072 and ni > 0 and ni % 3 == 0):
            continue
        voff = struct.unpack_from("<I", data, tex_start - 0x08)[0]
        ioff = struct.unpack_from("<I", data, tex_start - 0x04)[0]
        tex = data[tex_start:tex_start + 256].split(b"\x00")[0].decode("ascii", "replace")
        mat_start = tex_start + 0x100
        mat = data[mat_start:mat_start + 256].split(b"\x00")[0].decode("ascii", "replace") if mat_start < geom_off else ""
        entries.append({"nv": nv, "ni": ni, "voff": voff, "ioff": ioff,
                        "tex_start": tex_start, "tex": tex, "mat": mat})

    entries.sort(key=lambda e: e["tex_start"])

    # Group into LOD levels
    lod_groups = []
    cur_group, ve_acc, ie_acc = [], 0, 0
    for e in entries:
        if e["voff"] == ve_acc and e["ioff"] == ie_acc:
            cur_group.append(e)
            ve_acc += e["nv"]
            ie_acc += e["ni"]
        else:
            if cur_group:
                lod_groups.append(cur_group)
            cur_group = [e]
            ve_acc = e["nv"]
            ie_acc = e["ni"]
    if cur_group:
        lod_groups.append(cur_group)
    lod_groups = lod_groups[:lod_count]

    if not lod_groups:
        return result

    # Parse each LOD level
    cur = geom_off
    for lod_i, group in enumerate(lod_groups):
        total_nv = sum(e["nv"] for e in group)
        total_ni = sum(e["ni"] for e in group)

        # Find stride with padding scan
        found_base = found_stride = found_idx_off = None
        for pad in range(0, 64, 2):
            base = cur + pad
            for stride in STRIDE_CANDIDATES:
                cand = base + total_nv * stride
                if cand + total_ni * 2 > len(data):
                    continue
                if all(struct.unpack_from("<H", data, cand + j * 2)[0] < total_nv
                       for j in range(min(total_ni, 100))):
                    found_base = base
                    found_stride = stride
                    found_idx_off = cand
                    break
            if found_base is not None:
                break

        if found_base is None:
            result.lod_levels.append([])
            cur += 2
            continue

        # Parse submeshes for this LOD
        lod_submeshes = []
        vert_offset = 0
        has_uv = found_stride >= 12

        all_verts, all_uvs, all_faces, all_offsets = [], [], [], []
        for e in group:
            nv_e, ni_e = e["nv"], e["ni"]
            vert_base_e = found_base + e["voff"] * found_stride
            idx_off_e = found_idx_off + e["ioff"] * 2

            indices = [struct.unpack_from("<H", data, idx_off_e + j * 2)[0] for j in range(ni_e)]
            unique = sorted(set(indices))
            idx_map = {gi: li + vert_offset for li, gi in enumerate(unique)}

            for gi in unique:
                foff = vert_base_e + gi * found_stride
                if foff + 6 > len(data):
                    break
                xu, yu, zu = struct.unpack_from("<HHH", data, foff)
                all_offsets.append(foff)
                all_verts.append((_dequant_u16(xu, bmin[0], bmax[0]),
                                  _dequant_u16(yu, bmin[1], bmax[1]),
                                  _dequant_u16(zu, bmin[2], bmax[2])))
                if has_uv and foff + 12 <= len(data):
                    u = struct.unpack_from("<e", data, foff + 8)[0]
                    v = struct.unpack_from("<e", data, foff + 10)[0]
                    all_uvs.append((u, v))

            for j in range(0, ni_e - 2, 3):
                a, b, c = indices[j], indices[j + 1], indices[j + 2]
                if a in idx_map and b in idx_map and c in idx_map:
                    all_faces.append((idx_map[a], idx_map[b], idx_map[c]))

            vert_offset += len(unique)

        mat_name = group[0]["mat"] or f"lod{lod_i}"
        sm = SubMesh(
            name=f"lod{lod_i:02d}_{mat_name}",
            material=mat_name,
            texture=group[0]["tex"],
            vertices=all_verts, uvs=all_uvs, faces=all_faces,
            normals=_compute_smooth_normals(all_verts, all_faces),
            source_vertex_offsets=all_offsets,
            vertex_count=len(all_verts), face_count=len(all_faces),
        )
        lod_submeshes.append(sm)
        result.lod_levels.append(lod_submeshes)
        cur = found_idx_off + total_ni * 2

    # Use requested LOD level as the main submeshes
    if lod_level < len(result.lod_levels) and result.lod_levels[lod_level]:
        result.submeshes = result.lod_levels[lod_level]
    elif result.lod_levels:
        # Fallback to first non-empty LOD
        for lod in result.lod_levels:
            if lod:
                result.submeshes = lod
                break

    result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
    result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
    result.has_uvs = any(sm.uvs for sm in result.submeshes)

    logger.info("Parsed PAMLOD %s: %d LODs, using LOD %d (%d verts, %d faces)",
                filename, len(result.lod_levels), lod_level,
                result.total_vertices, result.total_faces)
    return result


# ── PAC Parser (skinned mesh) ────────────────────────────────────────

def parse_pac(data: bytes, filename: str = "") -> ParsedMesh:
    """Parse a .pac skinned character mesh.

    PAC format (reverse-engineered from binary analysis):
      Header: 80 bytes
        [0x00] 4B: 'PAR ' magic
        [0x04] 4B: version (0x01000903)
        [0x10] 4B: zero
        [0x14] N×8B or N×4B: section sizes (u64 or u32, variable count)

      Section 0: Metadata
        - u32 flags, u8 n_lods
        - n_lods × u32: section start offsets (LOD0 first)
        - n_lods × u32: vertex/index split offsets per section
        - Per submesh descriptor:
            [u8 len][mesh_name] [u8 len][mat_name]
            [u8 flag][2B pad] [8 floats: pivot(2) + bbox(6)]
            [u8 bone_count][bone_indices...]
            [n_lods × u16: vert counts] [n_lods × u32: idx counts]

      Sections 1..N: LOD levels (1=lowest, N=highest/LOD0)
        Part A: 40-byte vertex records (up to split offset)
        Part B: uint16 triangle list indices (after split offset)

      40-byte vertex record:
        [0-5]  3×uint16: quantized XYZ position
        [6-7]  uint16: packed data (normal/tangent)
        [8-11] 2×float16: UV coordinates
        [12-15] constant (0x3C000000)
        [16-19] 4 bytes data
        [20-27] zeros
        [28-31] bone index bytes (0xFF=none)
        [32-35] bone weight bytes
        [36-39] FFFFFFFF terminator

      Per-submesh bounding box for dequantization:
        bbox_min = (float[2], float[3], float[4])
        bbox_max = (float[5], float[6], float[7])
        pivot    = (float[0], float[1])  (bone attachment point)
    """
    if len(data) < 0x50 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAC file: bad magic {data[:4]!r}")

    result = ParsedMesh(path=filename, format="pac")

    # ── Parse section layout using section offset table in section 0 ──
    # Section 0 always starts at byte 80. Its first bytes contain:
    #   [u32 flags] [u8 n_lods] [n_lods × u32 section_offsets] [n_lods × u32 split_offsets]
    # Section offsets are absolute file positions (LOD0 first = largest, descending).
    # This is the most reliable way to determine section boundaries.
    header_size = 80

    if len(data) < header_size + 5:
        return _pac_fallback_pam(data, filename)

    s0_start = header_size
    off = s0_start
    flags = struct.unpack_from("<I", data, off)[0]
    n_lods = data[off + 4]
    off += 5

    if n_lods == 0 or n_lods > 10:
        return _pac_fallback_pam(data, filename)

    # Read section offsets (absolute file positions, LOD0 first = descending)
    lod_offsets = [struct.unpack_from("<I", data, off + i * 4)[0] for i in range(n_lods)]
    off += n_lods * 4
    split_offsets = [struct.unpack_from("<I", data, off + i * 4)[0] for i in range(n_lods)]
    off += n_lods * 4

    # Compute section boundaries from offsets:
    #   sec0: header_size to min(lod_offsets)
    #   LOD sections: between sorted offsets, last one ends at file_end
    sorted_offsets = sorted(lod_offsets)
    boundaries = [header_size] + sorted_offsets + [len(data)]
    sections = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    # Validate: sec0 must have positive size
    if sections[0][1] <= sections[0][0]:
        return _pac_fallback_pam(data, filename)

    s0_end = sections[0][1]

    # ── Find and parse submesh descriptors ──
    # Scan forward for first length-prefixed ASCII string
    scan = off
    while scan < s0_end - 10:
        b = data[scan]
        if 4 < b < 100:
            test = data[scan + 1:scan + 1 + b]
            if len(test) == b and all(32 <= c < 127 for c in test):
                break
        scan += 1
    off = scan

    pac_submeshes = []
    while off < s0_end - 20:
        name_len = data[off]
        if name_len == 0 or name_len > 200 or off + 1 + name_len >= s0_end:
            break
        mesh_name = data[off + 1:off + 1 + name_len].decode("ascii", "replace")
        off += 1 + name_len
        if not all(32 <= ord(c) < 127 for c in mesh_name):
            break

        mat_len = data[off]
        mat_name = data[off + 1:off + 1 + mat_len].decode("ascii", "replace") if mat_len > 0 else ""
        off += 1 + mat_len

        # flag(1) + pad(2) + 8 floats(32) + bone data
        off += 3
        bbox_floats = [struct.unpack_from("<f", data, off + i * 4)[0] for i in range(8)]
        off += 32

        # Bone data: [u8 bone_count] [bone_count × u8 indices]
        # Bone indices are padded to even byte count (odd bc gets +1 pad byte).
        bone_count = data[off]
        off += 1
        bone_palette = tuple(data[off:off + bone_count])
        bones_size = bone_count + (bone_count % 2)  # round up to even
        off += bones_size

        # Per-LOD vertex counts (n_lods × u16) + index counts (n_lods × u32)
        # Some files have fewer idx_counts than n_lods — validate and truncate.
        vert_counts = [struct.unpack_from("<H", data, off + i * 2)[0] for i in range(n_lods)]
        off += n_lods * 2

        idx_counts = []
        max_reasonable_idx = 10_000_000  # no single submesh has 10M indices
        for i in range(n_lods):
            if off + 4 > s0_end:
                break
            val = struct.unpack_from("<I", data, off)[0]
            if val > max_reasonable_idx:
                break  # hit garbage — stop reading idx_counts
            idx_counts.append(val)
            off += 4
        # Pad missing LODs with 0
        while len(idx_counts) < n_lods:
            idx_counts.append(0)

        bmin = (bbox_floats[2], bbox_floats[3], bbox_floats[4])
        bmax = (bbox_floats[5], bbox_floats[6], bbox_floats[7])

        pac_submeshes.append({
            "name": mesh_name, "material": mat_name,
            "bmin": bmin, "bmax": bmax,
            "vert_counts": vert_counts, "idx_counts": idx_counts,
            "bone_palette": bone_palette,
        })

        # Check if next byte starts another submesh name
        if off >= s0_end - 4:
            break
        next_b = data[off]
        if next_b == 0 or next_b > 200:
            break
        peek = data[off + 1:off + 1 + min(next_b, 6)]
        if not all(32 <= c < 127 for c in peek):
            break

    if not pac_submeshes:
        return _pac_fallback_pam(data, filename)

    # ── Extract LOD0 geometry (highest quality = last data section) ──
    lod0_sec_start, lod0_sec_end = sections[-1]
    lod0_split = split_offsets[0] if split_offsets else 0

    # Auto-detect vertex stride from section size:
    #   section = (total_verts × stride) + (total_indices × 2)
    #   stride = (section_size - total_indices × 2) / total_verts
    if lod0_split <= lod0_sec_start or lod0_split > lod0_sec_end:
        lod0_sec_size = lod0_sec_end - lod0_sec_start
        total_lod0_verts = sum(sm["vert_counts"][0] for sm in pac_submeshes)
        total_lod0_indices = sum(sm["idx_counts"][0] for sm in pac_submeshes)

        if total_lod0_verts == 0:
            return _pac_fallback_pam(data, filename)

        vert_stride = (lod0_sec_size - total_lod0_indices * 2) // total_lod0_verts
        lod0_split = lod0_sec_start + total_lod0_verts * vert_stride
    else:
        vert_stride = _detect_pac_vertex_stride(data, lod0_sec_start, lod0_split)

    if vert_stride < 6 or vert_stride > 128:
        logger.debug("PAC %s: computed stride %d out of range, trying PAM fallback",
                     filename, vert_stride)
        return _pac_fallback_pam(data, filename)

    vert_off = lod0_sec_start
    idx_off = lod0_split

    for sm_info in pac_submeshes:
        declared_nv = sm_info["vert_counts"][0]
        ni = sm_info["idx_counts"][0]
        bmin = sm_info["bmin"]
        bmax = sm_info["bmax"]
        bone_palette = sm_info.get("bone_palette", ())

        raw_faces = []
        max_index = -1
        for i in range(0, ni - 2, 3):
            if idx_off + (i + 2) * 2 + 2 > min(len(data), lod0_sec_end):
                break
            a = struct.unpack_from("<H", data, idx_off + i * 2)[0]
            b = struct.unpack_from("<H", data, idx_off + (i + 1) * 2)[0]
            c = struct.unpack_from("<H", data, idx_off + (i + 2) * 2)[0]
            raw_faces.append((a, b, c))
            max_index = max(max_index, a, b, c)

        available_records = max(0, (lod0_split - vert_off) // max(vert_stride, 1))
        actual_nv = max_index + 1 if max_index >= 0 else declared_nv
        if actual_nv > available_records:
            logger.debug(
                "PAC %s submesh %s references %d verts but only %d records fit before split",
                filename, sm_info["name"], actual_nv, available_records,
            )
            actual_nv = available_records

        used_indices = sorted({
            idx for face in raw_faces for idx in face
            if 0 <= idx < actual_nv
        })
        idx_map = {src_idx: dst_idx for dst_idx, src_idx in enumerate(used_indices)}

        verts = []
        uvs = []
        source_offsets = []
        bone_indices = []
        bone_weights = []
        for src_idx in used_indices:
            rec_off = vert_off + src_idx * vert_stride
            if rec_off + 12 > min(len(data), lod0_split):
                break
            xu, yu, zu = struct.unpack_from("<HHH", data, rec_off)
            verts.append((
                _dequant_u16(xu, bmin[0], bmax[0]),
                _dequant_u16(yu, bmin[1], bmax[1]),
                _dequant_u16(zu, bmin[2], bmax[2]),
            ))
            source_offsets.append(rec_off)

            try:
                u = struct.unpack_from("<e", data, rec_off + 8)[0]
                v = struct.unpack_from("<e", data, rec_off + 10)[0]
                uvs.append((u, v) if (not math.isnan(u) and not math.isnan(v)) else (0.0, 0.0))
            except Exception:
                uvs.append((0.0, 0.0))

            packed_bones = ()
            packed_weights = ()
            if rec_off + 36 <= min(len(data), lod0_split):
                raw_slots = struct.unpack_from("<BBBB", data, rec_off + 28)
                raw_weights = struct.unpack_from("<BBBB", data, rec_off + 32)
                mapped_bones = []
                mapped_weights = []
                for slot, weight in zip(raw_slots, raw_weights):
                    if slot == 0xFF or weight == 0:
                        continue
                    mapped_bones.append(bone_palette[slot] if slot < len(bone_palette) else slot)
                    mapped_weights.append(weight / 255.0)
                packed_bones = tuple(mapped_bones)
                packed_weights = tuple(mapped_weights)
            bone_indices.append(packed_bones)
            bone_weights.append(packed_weights)

        faces = []
        for a, b, c in raw_faces:
            if a in idx_map and b in idx_map and c in idx_map:
                faces.append((idx_map[a], idx_map[b], idx_map[c]))

        sm = SubMesh(
            name=sm_info["name"],
            material=sm_info["material"],
            texture="",
            vertices=verts,
            uvs=uvs,
            faces=faces,
            normals=_compute_smooth_normals(verts, faces),
            bone_indices=bone_indices,
            bone_weights=bone_weights,
            vertex_count=len(verts),
            face_count=len(faces),
            source_vertex_offsets=source_offsets,
        )
        result.submeshes.append(sm)

        if any(bone_indices):
            result.has_bones = True

        vert_off += actual_nv * vert_stride
        idx_off += ni * 2

    # Compute overall stats
    if result.submeshes:
        all_verts = [v for sm in result.submeshes for v in sm.vertices]
        if all_verts:
            xs = [v[0] for v in all_verts]
            ys = [v[1] for v in all_verts]
            zs = [v[2] for v in all_verts]
            result.bbox_min = (min(xs), min(ys), min(zs))
            result.bbox_max = (max(xs), max(ys), max(zs))

    result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
    result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
    result.has_uvs = any(sm.uvs for sm in result.submeshes)

    logger.info("Parsed PAC %s: %d submeshes, %d verts, %d faces",
                filename, len(result.submeshes), result.total_vertices, result.total_faces)
    return result


def _parse_par_sections(data: bytes) -> list[dict]:
    """Parse the PAR section table from the 80-byte header."""
    if len(data) < 0x50 or data[:4] != PAR_MAGIC:
        return []

    sections = []
    offset = 0x50
    for i in range(8):
        slot_off = 0x10 + i * 8
        comp_size = struct.unpack_from("<I", data, slot_off)[0]
        decomp_size = struct.unpack_from("<I", data, slot_off + 4)[0]
        stored_size = comp_size if comp_size > 0 else decomp_size
        if decomp_size <= 0:
            continue
        if offset + stored_size > len(data):
            return []
        sections.append({"index": i, "offset": offset, "size": decomp_size})
        offset += stored_size
    return sections


def _find_name_strings(region: bytes, desc_start: int) -> tuple[str, str]:
    """Extract the two length-prefixed ASCII names immediately before a descriptor."""
    names = []
    cursor = desc_start

    for _ in range(2):
        found = False
        for back in range(1, 200):
            pos = cursor - back
            if pos < 0:
                break
            candidate_len = region[pos]
            if candidate_len == 0 or candidate_len != back - 1:
                continue
            name_bytes = region[pos + 1:cursor]
            if not name_bytes or not all(32 <= c < 127 for c in name_bytes):
                continue
            names.append(name_bytes.decode("ascii", "replace"))
            cursor = pos
            found = True
            break
        if not found:
            names.append(f"unknown_{desc_start:x}")

    names.reverse()
    return names[0], names[1]


def _find_pac_descriptors(
    data: bytes,
    sec0_offset: int,
    sec0_size: int,
    n_lods: int,
) -> list[PacDescriptor]:
    """Recover PAC descriptors by matching known 4/3/2-LOD descriptor patterns."""
    region = data[sec0_offset:sec0_offset + sec0_size]
    if not region:
        return []

    found: list[tuple[int, PacDescriptor]] = []
    seen_starts: set[int] = set()
    pad_len = max(4, n_lods)

    def _append_descriptor(idx: int, stored_lod_count: int, vc_off: int, ic_off: int) -> None:
        desc_start = idx - 35
        if desc_start in seen_starts or desc_start < 0:
            return
        if desc_start + ic_off + stored_lod_count * 4 > len(region):
            return
        if region[desc_start] != 0x01:
            return

        try:
            floats = struct.unpack_from("<8f", region, desc_start + 3)
        except struct.error:
            return

        vert_counts = [
            struct.unpack_from("<H", region, desc_start + vc_off + i * 2)[0]
            for i in range(stored_lod_count)
        ]
        idx_counts = [
            struct.unpack_from("<I", region, desc_start + ic_off + i * 4)[0]
            for i in range(stored_lod_count)
        ]

        if not any(v > 0 for v in vert_counts):
            return
        if any(v > 200000 for v in vert_counts):
            return
        if any(i > 20000000 for i in idx_counts):
            return

        name, material = _find_name_strings(region, desc_start)
        palette = tuple(region[idx + 1:idx + 1 + stored_lod_count])
        padded_vc = vert_counts + [0] * max(0, pad_len - stored_lod_count)
        padded_ic = idx_counts + [0] * max(0, pad_len - stored_lod_count)

        found.append((
            desc_start,
            PacDescriptor(
                name=name,
                material=material,
                bbox_min=(floats[2], floats[3], floats[4]),
                bbox_extent=(floats[5], floats[6], floats[7]),
                vertex_counts=padded_vc,
                index_counts=padded_ic,
                palette=palette,
                descriptor_offset=sec0_offset + desc_start,
                stored_lod_count=stored_lod_count,
            ),
        ))
        seen_starts.add(desc_start)

    # PAC section-0 descriptors are not fully uniform. Most use the classic:
    #   4 LODs: 04 00 01 02 03
    #   3 LODs: 03 00 01 02
    #   2 LODs: 02 00 01
    # Some character heads (for example Kliff/Macduff) use a 3-LOD variant
    # with an extra marker byte before the count table:
    #   03 00 01 01 02
    # Support both layouts while deduping by desc_start so we can parse the
    # full head mesh instead of only the eyecover helper submesh.
    pattern_specs = [
        (bytes([0x04, 0x00, 0x01, 0x02, 0x03]), 4, 40, 48,
         lambda idx: True),
        (bytes([0x03, 0x00, 0x01, 0x01, 0x02]), 3, 40, 46,
         lambda idx: True),
        (bytes([0x03, 0x00, 0x01, 0x02]), 3, 40, 46,
         lambda idx: idx < 1 or region[idx - 1] != 0x04),
        (bytes([0x02, 0x00, 0x01]), 2, 40, 44,
         lambda idx: idx < 1 or region[idx - 1] not in (0x03, 0x04)),
    ]

    for pattern, lod_count, vc_off, ic_off, should_accept in pattern_specs:
        pos = 0
        while True:
            idx = region.find(pattern, pos)
            if idx == -1:
                break
            if should_accept(idx):
                _append_descriptor(idx, lod_count, vc_off, ic_off)
            pos = idx + len(pattern)

    found.sort(key=lambda item: item[0])
    return [desc for _, desc in found]


def _decode_pac_position_u16(value: int, bbox_min: float, bbox_extent: float) -> float:
    if abs(bbox_extent) < 1e-8:
        return bbox_min
    return bbox_min + (value / 32767.0) * bbox_extent


def _decode_pac_normal(data: bytes, rec_off: int) -> tuple[float, float, float]:
    try:
        packed = struct.unpack_from("<I", data, rec_off + 16)[0]
    except struct.error:
        return (0.0, 1.0, 0.0)

    nx_raw = (packed >> 0) & 0x3FF
    ny_raw = (packed >> 10) & 0x3FF
    nz_raw = (packed >> 20) & 0x3FF
    nx = ny_raw / 511.5 - 1.0
    ny = nz_raw / 511.5 - 1.0
    nz = nx_raw / 511.5 - 1.0
    return (nx, ny, nz)


def _decode_pac_vertex_record(
    data: bytes,
    rec_off: int,
    desc: PacDescriptor,
) -> tuple[tuple[float, float, float], tuple[float, float], tuple[float, float, float], tuple[int, ...], tuple[float, ...]]:
    xu, yu, zu = struct.unpack_from("<HHH", data, rec_off)
    pos = (
        _decode_pac_position_u16(xu, desc.bbox_min[0], desc.bbox_extent[0]),
        _decode_pac_position_u16(yu, desc.bbox_min[1], desc.bbox_extent[1]),
        _decode_pac_position_u16(zu, desc.bbox_min[2], desc.bbox_extent[2]),
    )

    try:
        u = struct.unpack_from("<e", data, rec_off + 8)[0]
        v = struct.unpack_from("<e", data, rec_off + 10)[0]
        uv = (0.0, 0.0) if math.isnan(u) or math.isnan(v) else (u, v)
    except Exception:
        uv = (0.0, 0.0)

    normal = _decode_pac_normal(data, rec_off)

    packed_bones: tuple[int, ...] = ()
    packed_weights: tuple[float, ...] = ()
    if rec_off + 36 <= len(data):
        raw_slots = struct.unpack_from("<BBBB", data, rec_off + 28)
        raw_weights = struct.unpack_from("<BBBB", data, rec_off + 32)
        mapped_bones = []
        mapped_weights = []
        for slot, weight in zip(raw_slots, raw_weights):
            if slot == 0xFF or weight == 0:
                continue
            mapped_bones.append(desc.palette[slot] if slot < len(desc.palette) else slot)
            mapped_weights.append(weight / 255.0)
        packed_bones = tuple(mapped_bones)
        packed_weights = tuple(mapped_weights)

    return pos, uv, normal, packed_bones, packed_weights


def _read_pac_indices(
    data: bytes,
    section_offset: int,
    section_size: int,
    index_start: int,
    index_count: int,
) -> list[int]:
    """Read a PAC index segment with hard bounds checks."""
    if index_count <= 0:
        return []

    max_count = max(0, min(index_count, (section_size - index_start) // 2))
    base = section_offset + index_start
    return [struct.unpack_from("<H", data, base + i * 2)[0] for i in range(max_count)]


def _find_pac_section_layout(
    data: bytes,
    geom_sec: dict,
    descriptors: list[PacDescriptor],
    lod: int,
    total_indices: int,
) -> tuple[int, int]:
    """Find the vertex/index split inside a decompressed PAC geometry section."""
    sec_off = geom_sec["offset"]
    sec_size = geom_sec["size"]
    total_verts = sum(d.vertex_counts[lod] for d in descriptors)
    primary_bytes = total_verts * 40
    index_bytes = total_indices * 2

    if primary_bytes + index_bytes >= sec_size:
        return 0, primary_bytes

    gap = sec_size - primary_bytes - index_bytes
    if gap <= 0:
        return 0, primary_bytes

    first_desc = next((d for d in descriptors if d.vertex_counts[lod] > 0), None)
    if first_desc is None:
        return 0, primary_bytes

    first_vc = first_desc.vertex_counts[lod]

    def _available_vertices(v_start: int, i_start: int) -> int:
        if i_start <= v_start:
            return 0
        return max(0, (i_start - v_start) // 40)

    def _scan_idx_start(after_verts: int) -> Optional[int]:
        for adj in range(0, sec_size - after_verts, 2):
            trial = after_verts + adj
            if trial + 6 > sec_size:
                break
            v0 = struct.unpack_from("<H", data, sec_off + trial)[0]
            v1 = struct.unpack_from("<H", data, sec_off + trial + 2)[0]
            v2 = struct.unpack_from("<H", data, sec_off + trial + 4)[0]
            if v0 == 0 and v1 < first_vc and v2 < first_vc:
                return trial
        return None

    def _measure_quality(v_start: int, i_start: Optional[int]) -> float:
        if i_start is None or i_start + total_indices * 2 > sec_size:
            return float("inf")

        first_ic = next((d.index_counts[lod] for d in descriptors if d.index_counts[lod] > 0), 0)
        n_tris = first_ic // 3
        if n_tris == 0:
            return 0.0

        sample_step = max(1, n_tris // 30)
        sample_tri_indices = set(range(min(12, n_tris)))
        sample_tri_indices.update(range(0, n_tris, sample_step))
        sample_tris: list[tuple[int, int, int]] = []
        sample_max_idx = -1
        for tri_idx in sorted(sample_tri_indices):
            idx_base = sec_off + i_start + tri_idx * 6
            if idx_base + 6 > len(data):
                return float("inf")
            i0, i1, i2 = struct.unpack_from("<HHH", data, idx_base)
            sample_tris.append((i0, i1, i2))
            sample_max_idx = max(sample_max_idx, i0, i1, i2)

        needed_vc = max(first_vc, sample_max_idx + 1)
        if needed_vc <= 0 or needed_vc > _available_vertices(v_start, i_start):
            return float("inf")

        preview_positions = []
        for i in range(needed_vc):
            rec_off = sec_off + v_start + i * 40
            if rec_off + 40 > len(data):
                return float("inf")
            xu, yu, zu = struct.unpack_from("<HHH", data, rec_off)
            preview_positions.append((
                _decode_pac_position_u16(xu, first_desc.bbox_min[0], first_desc.bbox_extent[0]),
                _decode_pac_position_u16(yu, first_desc.bbox_min[1], first_desc.bbox_extent[1]),
                _decode_pac_position_u16(zu, first_desc.bbox_min[2], first_desc.bbox_extent[2]),
            ))

        total_edge = 0.0
        for i0, i1, i2 in sample_tris:
            if max(i0, i1, i2) >= len(preview_positions):
                return float("inf")
            p0, p1, p2 = preview_positions[i0], preview_positions[i1], preview_positions[i2]
            e0 = math.dist(p0, p1)
            e1 = math.dist(p1, p2)
            e2 = math.dist(p2, p0)
            total_edge += max(e0, e1, e2)
        return total_edge

    secondary_bytes = (gap // 40) * 40
    best_v_start = 0
    best_i_start = primary_bytes + secondary_bytes
    best_quality = _measure_quality(best_v_start, best_i_start)

    for n_secondary in range(0, gap // 40 + 1):
        v_start = n_secondary * 40
        all_verts_end = v_start + primary_bytes
        if all_verts_end >= sec_size:
            break
        idx_start = _scan_idx_start(all_verts_end)
        if idx_start is None or idx_start + total_indices * 2 > sec_size:
            continue
        quality = _measure_quality(v_start, idx_start)
        if quality < best_quality:
            best_quality = quality
            best_v_start = v_start
            best_i_start = idx_start

    return best_v_start, best_i_start


def parse_pac(data: bytes, filename: str = "") -> ParsedMesh:
    """Parse a decompressed PAC skinned mesh file."""
    if len(data) < 0x50 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAC file: bad magic {data[:4]!r}")

    result = ParsedMesh(path=filename, format="pac")

    sections = _parse_par_sections(data)
    sec_by_idx = {s["index"]: s for s in sections}
    sec0 = sec_by_idx.get(0)
    if not sec0:
        return _pac_fallback_pam(data, filename)

    n_lods = data[sec0["offset"] + 4] if sec0["size"] >= 5 else 0
    if n_lods <= 0 or n_lods > 10:
        return _pac_fallback_pam(data, filename)

    descriptors = _find_pac_descriptors(data, sec0["offset"], sec0["size"], n_lods)
    if not descriptors:
        return _pac_fallback_pam(data, filename)

    geom_section_idx = next((i for i in [4, 3, 2, 1] if i in sec_by_idx), None)
    if geom_section_idx is None:
        return _pac_fallback_pam(data, filename)

    geom_sec = sec_by_idx[geom_section_idx]
    lod = 4 - geom_section_idx
    total_indices = sum(d.index_counts[lod] for d in descriptors)
    vert_base, idx_byte_offset = _find_pac_section_layout(data, geom_sec, descriptors, lod, total_indices)
    index_region_start = idx_byte_offset

    desc_vert_offsets = []
    vert_cursor = vert_base
    for desc in descriptors:
        desc_vert_offsets.append(vert_cursor)
        vert_cursor += desc.vertex_counts[lod] * 40

    for di, desc in enumerate(descriptors):
        vc = desc.vertex_counts[lod]
        ic = desc.index_counts[lod]
        if vc == 0 and ic == 0:
            continue

        indices = _read_pac_indices(data, geom_sec["offset"], geom_sec["size"], idx_byte_offset, ic)

        vertex_owner_idx = di
        owner_vc = vc
        max_idx = max(indices) if indices else -1
        if max_idx >= vc:
            partner_idx = next(
                (pj for pj, partner in enumerate(descriptors)
                 if pj != di and partner.vertex_counts[lod] > max_idx),
                None,
            )
            if partner_idx is not None:
                vertex_owner_idx = partner_idx
                owner_vc = descriptors[partner_idx].vertex_counts[lod]
            else:
                available_vc = max(0, (index_region_start - desc_vert_offsets[di]) // 40)
                if max_idx < available_vc:
                    owner_vc = max_idx + 1

        vertex_start = desc_vert_offsets[vertex_owner_idx]
        verts = []
        uvs = []
        normals = []
        source_offsets = []
        bone_indices = []
        bone_weights = []

        for vi in range(owner_vc):
            rec_off = geom_sec["offset"] + vertex_start + vi * 40
            if rec_off + 40 > len(data):
                break
            pos, uv, normal, bones, weights = _decode_pac_vertex_record(data, rec_off, desc)
            verts.append(pos)
            uvs.append(uv)
            normals.append(normal)
            source_offsets.append(rec_off)
            bone_indices.append(bones)
            bone_weights.append(weights)

        faces = []
        for i in range(0, len(indices) - 2, 3):
            a, b, c = indices[i], indices[i + 1], indices[i + 2]
            if a < len(verts) and b < len(verts) and c < len(verts):
                faces.append((a, b, c))

        bbox_max = tuple(desc.bbox_min[i] + desc.bbox_extent[i] for i in range(3))
        sm = SubMesh(
            name=desc.name,
            material=desc.material,
            texture="",
            vertices=verts,
            uvs=uvs if len(uvs) == len(verts) else [],
            normals=normals if len(normals) == len(verts) else _compute_smooth_normals(verts, faces),
            faces=faces,
            bone_indices=bone_indices,
            bone_weights=bone_weights,
            vertex_count=len(verts),
            face_count=len(faces),
            source_vertex_offsets=source_offsets,
            source_index_offset=geom_sec["offset"] + idx_byte_offset,
            source_index_count=len(indices),
            source_vertex_stride=40,
            source_descriptor_offset=desc.descriptor_offset,
            source_bbox_min=desc.bbox_min,
            source_bbox_extent=desc.bbox_extent,
            source_lod_count=desc.stored_lod_count,
        )
        result.submeshes.append(sm)
        result.has_bones = result.has_bones or any(bone_indices)

        if len(result.submeshes) == 1:
            result.bbox_min = desc.bbox_min
            result.bbox_max = bbox_max
        else:
            result.bbox_min = tuple(min(result.bbox_min[i], desc.bbox_min[i]) for i in range(3))
            result.bbox_max = tuple(max(result.bbox_max[i], bbox_max[i]) for i in range(3))

        idx_byte_offset += ic * 2

    if not result.submeshes:
        return _pac_fallback_pam(data, filename)

    result.total_vertices = sum(len(sm.vertices) for sm in result.submeshes)
    result.total_faces = sum(len(sm.faces) for sm in result.submeshes)
    result.has_uvs = any(sm.uvs for sm in result.submeshes)

    logger.info("Parsed PAC %s: %d submeshes, %d verts, %d faces",
                filename, len(result.submeshes), result.total_vertices, result.total_faces)
    return result


def _pac_fallback_pam(data: bytes, filename: str) -> ParsedMesh:
    """Fallback: try parsing PAC as PAM (works for some small PAC files)."""
    try:
        result = parse_pam(data, filename)
        if result.total_vertices > 0:
            return result
    except Exception:
        pass
    logger.debug("PAC %s: unsupported format variant, skipping", filename)
    return ParsedMesh(path=filename, format="pac")


def _flatten_parsed_mesh_for_preview(mesh: ParsedMesh) -> PreviewMesh:
    """Flatten ParsedMesh submeshes into a single preview buffer."""
    preview = PreviewMesh(
        format=mesh.format,
        submesh_count=len(mesh.submeshes),
    )

    vert_offset = 0
    for sm in mesh.submeshes:
        preview.vertices.extend(sm.vertices)
        if sm.normals and len(sm.normals) == len(sm.vertices):
            preview.normals.extend(sm.normals)
        else:
            preview.normals.extend([(0.0, 1.0, 0.0)] * len(sm.vertices))
        preview.faces.extend((a + vert_offset, b + vert_offset, c + vert_offset) for a, b, c in sm.faces)
        vert_offset += len(sm.vertices)

    preview.total_vertices = len(preview.vertices)
    preview.total_faces = len(preview.faces)
    return preview


def _preview_mesh_has_valid_indices(preview: PreviewMesh) -> bool:
    """Check whether a flattened preview buffer is self-consistent."""
    if not preview.vertices or not preview.faces:
        return False
    max_idx = max(max(face) for face in preview.faces)
    return max_idx < len(preview.vertices)


def _build_pac_preview_mesh(data: bytes, filename: str = "") -> PreviewMesh:
    """Build PAC preview buffers using the same flattening strategy as CDMB."""
    sections = _parse_par_sections(data)
    if not sections:
        return _flatten_parsed_mesh_for_preview(parse_pac(data, filename))

    sec_by_idx = {section["index"]: section for section in sections}
    sec0 = sec_by_idx.get(0)
    if sec0 is None:
        return _flatten_parsed_mesh_for_preview(parse_pac(data, filename))

    geom_section_idx = next((idx for idx in [4, 3, 2, 1] if idx in sec_by_idx), None)
    if geom_section_idx is None:
        return _flatten_parsed_mesh_for_preview(parse_pac(data, filename))

    geom_sec = sec_by_idx[geom_section_idx]
    lod = 4 - geom_section_idx
    descriptors = _find_pac_descriptors(data, sec0["offset"], sec0["size"], max(1, len(sections) - 1))
    if not descriptors:
        return _flatten_parsed_mesh_for_preview(parse_pac(data, filename))

    total_indices = sum(desc.index_counts[lod] for desc in descriptors)
    vert_base, idx_byte_offset = _find_pac_section_layout(data, geom_sec, descriptors, lod, total_indices)

    desc_vert_offsets = []
    cursor = vert_base
    for desc in descriptors:
        desc_vert_offsets.append(cursor)
        cursor += desc.vertex_counts[lod] * 40

    preview = PreviewMesh(format="pac")
    desc_output_offset: dict[int, int] = {}
    vert_offset = 0

    for di, desc in enumerate(descriptors):
        vc = desc.vertex_counts[lod]
        ic = desc.index_counts[lod]
        if vc == 0:
            idx_byte_offset += ic * 2
            continue

        vert_byte_offset = desc_vert_offsets[di]
        indices = _read_pac_indices(data, geom_sec["offset"], geom_sec["size"], idx_byte_offset, ic)
        max_idx = max(indices) if indices else 0

        if max_idx >= vc:
            partner_idx = next(
                (pj for pj, partner in enumerate(descriptors) if pj != di and partner.vertex_counts[lod] > max_idx),
                None,
            )

            if partner_idx is not None and partner_idx in desc_output_offset:
                base_offset = desc_output_offset[partner_idx]
                for i in range(0, len(indices) - 2, 3):
                    preview.faces.append((
                        indices[i] + base_offset,
                        indices[i + 1] + base_offset,
                        indices[i + 2] + base_offset,
                    ))
            else:
                source_offset = desc_vert_offsets[partner_idx] if partner_idx is not None else vert_byte_offset
                source_vc = descriptors[partner_idx].vertex_counts[lod] if partner_idx is not None else vc
                desc_output_offset[di] = vert_offset
                emitted = 0
                for vi in range(source_vc):
                    rec_off = geom_sec["offset"] + source_offset + vi * 40
                    if rec_off + 40 > len(data):
                        break
                    pos, _, normal, _, _ = _decode_pac_vertex_record(data, rec_off, desc)
                    preview.vertices.append(pos)
                    preview.normals.append(normal)
                    emitted += 1
                for i in range(0, len(indices) - 2, 3):
                    a, b, c = indices[i], indices[i + 1], indices[i + 2]
                    if a < emitted and b < emitted and c < emitted:
                        preview.faces.append((a + vert_offset, b + vert_offset, c + vert_offset))
                vert_offset += emitted
        else:
            desc_output_offset[di] = vert_offset
            emitted = 0
            for vi in range(vc):
                rec_off = geom_sec["offset"] + vert_byte_offset + vi * 40
                if rec_off + 40 > len(data):
                    break
                pos, _, normal, _, _ = _decode_pac_vertex_record(data, rec_off, desc)
                preview.vertices.append(pos)
                preview.normals.append(normal)
                emitted += 1
            for i in range(0, len(indices) - 2, 3):
                a, b, c = indices[i], indices[i + 1], indices[i + 2]
                if a < emitted and b < emitted and c < emitted:
                    preview.faces.append((a + vert_offset, b + vert_offset, c + vert_offset))
            vert_offset += emitted

        idx_byte_offset += ic * 2

    preview.submesh_count = len([desc for desc in descriptors if desc.vertex_counts[lod] > 0])
    preview.total_vertices = len(preview.vertices)
    preview.total_faces = len(preview.faces)
    expected_faces = total_indices // 3
    if not _preview_mesh_has_valid_indices(preview) or preview.total_faces < expected_faces:
        return _flatten_parsed_mesh_for_preview(parse_pac(data, filename))
    return preview


def build_preview_mesh(data: bytes, filename: str = "") -> PreviewMesh:
    """Build flattened preview buffers for Explorer rendering."""
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pac":
        return _build_pac_preview_mesh(data, filename)
    return _flatten_parsed_mesh_for_preview(parse_mesh(data, filename))


# ── Auto-detect and parse ────────────────────────────────────────────

def parse_mesh(data: bytes, filename: str = "") -> ParsedMesh:
    """Auto-detect file type and parse accordingly."""
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pamlod":
        return parse_pamlod(data, filename)
    elif ext == ".pac":
        return parse_pac(data, filename)
    else:
        return parse_pam(data, filename)


def is_mesh_file(path: str) -> bool:
    """Check if a file path is a supported mesh format."""
    ext = os.path.splitext(path.lower())[1]
    return ext in (".pam", ".pamlod", ".pac")
