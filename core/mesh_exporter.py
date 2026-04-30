"""OBJ and binary FBX 7.4 exporter for parsed mesh data.

Exports ParsedMesh objects from mesh_parser to standard 3D formats:
  - OBJ + MTL (Wavefront, universally supported)
  - FBX binary 7.4 (Blender, Maya, 3ds Max, Unity, Unreal Engine)

Optional spike-vertex filter (FBX skin export only):
  Pearl Abyss meshes embed unskinned helper geometry — foot-shadow
  decals, UV-unwrap helpers, anchor-point markers — that the engine
  hides via shader logic but Blender renders as visible "spike"
  triangles. Setting ``filter_unskinned_outliers=True`` on
  ``export_fbx_with_skeleton`` drops those vertices from the FBX
  geometry while preserving them in the .cfmeta.json sidecar (schema
  v2 ``filtered_vertices`` block) so the round-trip rebuilds the
  full PAC with no data loss.

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

    Also writes a ``<base>.cfmeta.json`` sidecar that records the skin
    weights OBJ can't carry (bone indices + weights per vertex, plus
    an identity vertex→source map the re-importer uses to rebuild
    PAC donor records). The sidecar is optional; re-import falls back
    gracefully to positional donor matching when it's missing.

    Args:
        mesh: Parsed mesh data.
        output_dir: Directory to write files.
        name: Base filename (without extension). Defaults to mesh path stem.
        split_submeshes: If True, write each submesh as a separate OBJ file.
        scale: Scale factor applied to all vertices.

    Returns:
        List of output file paths (OBJ, MTL, and sidecar if any skin data).
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

    output_paths = [obj_path, mtl_path]
    sidecar_path = _write_cfmeta_sidecar(mesh, obj_path)
    if sidecar_path:
        output_paths.append(sidecar_path)

    logger.info("Exported OBJ: %s (%d verts, %d faces)", obj_path,
                mesh.total_vertices, mesh.total_faces)
    return output_paths


# ═══════════════════════════════════════════════════════════════════════
#  SPIKE-VERTEX DETECTION (engine helper geometry filter)
# ═══════════════════════════════════════════════════════════════════════

def _compute_spike_indices(sm: SubMesh,
                           min_outlier_count: int = 16) -> set[int]:
    """Identify per-submesh 'spike' vertices that should be filtered.

    A vertex is a spike if BOTH:
      (a) It has no bone weights (unskinned).
      (b) Its distance from the submesh centroid is greater than 2× the
          median distance — i.e. it's a clear outlier vs the bulk of
          the mesh.

    Pearl Abyss embeds helper geometry of this shape for foot-shadow
    decals, footstep markers, and other engine-internal effects that
    the game renders with a special shader (or hides via material).
    Without that shader Blender draws them as ugly visible triangles.
    Filtering them preserves the visible character mesh while keeping
    full data via the sidecar's filtered_vertices block.

    Returns an empty set if the outlier population is below
    ``min_outlier_count`` — small numbers of outliers may be legitimate
    extremities (raised hand mid-pose, etc.) and filtering them would
    surprise the user.

    Bounds: only filters UNSKINNED outliers. Anything weighted to a
    bone is part of the deformable rig and stays — even if it's far
    from centroid (e.g. a bone with a long extension geometry like
    cape, weapon, or hair).
    """
    if not sm.vertices:
        return set()

    # Median centroid (robust to outliers)
    xs = sorted(v[0] for v in sm.vertices)
    ys = sorted(v[1] for v in sm.vertices)
    zs = sorted(v[2] for v in sm.vertices)
    n = len(sm.vertices)
    cx, cy, cz = xs[n // 2], ys[n // 2], zs[n // 2]

    # Per-vertex distance from median centroid
    dists: list[float] = []
    for x, y, z in sm.vertices:
        dx, dy, dz = x - cx, y - cy, z - cz
        dists.append(math.sqrt(dx * dx + dy * dy + dz * dz))

    median_dist = sorted(dists)[len(dists) // 2]
    threshold = max(2.0 * median_dist, 1.0)  # 1m absolute floor

    # Build set of unskinned outlier indices
    spikes: set[int] = set()
    for vi, d in enumerate(dists):
        if d <= threshold:
            continue
        # Only filter unskinned vertices.
        bones = sm.bone_indices[vi] if vi < len(sm.bone_indices) else ()
        weights = sm.bone_weights[vi] if vi < len(sm.bone_weights) else ()
        has_skin = bool(bones) and any(
            (w is not None and w > 0.0) for w in weights
        )
        if has_skin:
            continue
        spikes.add(vi)

    if len(spikes) < min_outlier_count:
        return set()
    return spikes


def _filtered_submesh_view(sm: SubMesh,
                            spike_indices: set[int]):
    """Build a view of a submesh with the spike vertices removed.

    Returns a tuple of:
      - filtered_verts:  list[(x,y,z)] — the kept vertices in new order
      - filtered_uvs:    list[(u,v)]
      - filtered_normals: list[(nx,ny,nz)]
      - filtered_faces:  list[(a,b,c)] — face indices into filtered_verts;
                         faces touching ANY spike vertex are dropped
      - filtered_bone_indices: list[tuple[int,...]]
      - filtered_bone_weights: list[tuple[float,...]]
      - new_to_old:      list[int] — new_to_old[i] is the original index
                         of filtered_verts[i] within the source submesh
      - dropped_donor_records: list[dict] — verbatim copies of the spike
                         vertex data, keyed by their ORIGINAL index, so
                         the sidecar can preserve them for round-trip

    If spike_indices is empty, returns the submesh as-is with identity
    new_to_old mapping and no dropped_donor_records.
    """
    if not spike_indices:
        new_to_old = list(range(len(sm.vertices)))
        return (
            list(sm.vertices),
            list(sm.uvs) if sm.uvs else [],
            list(sm.normals) if sm.normals else [],
            list(sm.faces),
            [tuple(b) for b in sm.bone_indices],
            [tuple(w) for w in sm.bone_weights],
            new_to_old,
            [],   # dropped donor records
            [],   # dropped faces
        )

    # old_idx → new_idx map (only for kept verts)
    old_to_new: dict[int, int] = {}
    new_to_old: list[int] = []
    f_verts = []
    f_uvs = []
    f_normals = []
    f_bi: list[tuple[int, ...]] = []
    f_bw: list[tuple[float, ...]] = []
    has_uvs = bool(sm.uvs)
    has_normals = bool(sm.normals)

    for old_idx in range(len(sm.vertices)):
        if old_idx in spike_indices:
            continue
        old_to_new[old_idx] = len(f_verts)
        new_to_old.append(old_idx)
        f_verts.append(sm.vertices[old_idx])
        if has_uvs and old_idx < len(sm.uvs):
            f_uvs.append(sm.uvs[old_idx])
        if has_normals and old_idx < len(sm.normals):
            f_normals.append(sm.normals[old_idx])
        bi = sm.bone_indices[old_idx] if old_idx < len(sm.bone_indices) else ()
        bw = sm.bone_weights[old_idx] if old_idx < len(sm.bone_weights) else ()
        f_bi.append(tuple(bi))
        f_bw.append(tuple(bw))

    # Faces: drop any face touching a spike vert; remap the rest.
    # Track dropped faces SEPARATELY so the sidecar can carry them
    # through the round-trip and the importer can re-add them — the
    # rebuilt PAC then has identical face count to the source.
    f_faces: list[tuple[int, int, int]] = []
    dropped_faces: list[tuple[int, int, int]] = []
    for a, b, c in sm.faces:
        if a in spike_indices or b in spike_indices or c in spike_indices:
            dropped_faces.append((a, b, c))
            continue
        f_faces.append((old_to_new[a], old_to_new[b], old_to_new[c]))

    # Donor records (the dropped verts) keyed by ORIGINAL index so the
    # sidecar can preserve them for round-trip. We store position, uv,
    # normal so the importer can reinsert them at the correct PAC slot
    # with no data loss.
    dropped: list[dict] = []
    for old_idx in sorted(spike_indices):
        rec: dict = {
            "source_index": int(old_idx),
            "position": [float(v) for v in sm.vertices[old_idx]],
        }
        if has_uvs and old_idx < len(sm.uvs):
            rec["uv"] = [float(v) for v in sm.uvs[old_idx]]
        if has_normals and old_idx < len(sm.normals):
            rec["normal"] = [float(v) for v in sm.normals[old_idx]]
        bi = sm.bone_indices[old_idx] if old_idx < len(sm.bone_indices) else ()
        bw = sm.bone_weights[old_idx] if old_idx < len(sm.bone_weights) else ()
        if bi:
            rec["bone_indices"] = [int(b) for b in bi]
            rec["bone_weights"] = [float(w) for w in bw]
        dropped.append(rec)

    return (f_verts, f_uvs, f_normals, f_faces,
            f_bi, f_bw, new_to_old, dropped, dropped_faces)


def _write_cfmeta_sidecar(mesh: ParsedMesh, obj_path: str) -> str | None:
    """Write the ``<obj>.cfmeta.json`` sidecar with skin data.

    The sidecar is a stable, forward-compatible JSON format keyed by
    ``schema_version`` so older releases of CrimsonForge can refuse
    to consume newer sidecars rather than silently corrupting a
    rebuild. We only write the sidecar when at least one submesh
    carries bone indices — there's nothing to preserve otherwise.
    """
    has_skin = any(sm.bone_indices for sm in mesh.submeshes)
    if not has_skin:
        return None

    import json

    submeshes_json = []
    for sm in mesh.submeshes:
        submeshes_json.append({
            "name": sm.name,
            "vertex_count": len(sm.vertices),
            "bone_indices": [list(b) for b in sm.bone_indices],
            "bone_weights": [list(w) for w in sm.bone_weights],
        })

    payload = {
        "schema_version": 1,
        "tool": "CrimsonForge",
        "source_path": mesh.path,
        "source_format": mesh.format,
        "submeshes": submeshes_json,
    }

    sidecar_path = obj_path + ".cfmeta.json"
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
    except OSError as e:
        logger.warning("Failed to write cfmeta sidecar %s: %s", sidecar_path, e)
        return None
    return sidecar_path


def _write_cfmeta_sidecar_v2(
    mesh: ParsedMesh,
    base_path: str,
    per_submesh_view: list[dict],
) -> str | None:
    """Write a schema-v2 sidecar that supports filtered (spike) vertices.

    v2 adds two per-submesh fields beyond v1:
      - ``source_vertex_map``: list[int] — for each FBX vertex (after
        filtering), the index into the ORIGINAL parsed submesh (before
        filter). Used by import_fbx to map edited verts back to PAC
        slots.
      - ``filtered_vertices``: list[dict] — verbatim donor records for
        the spike verts that were removed from the FBX. Each entry has
        ``source_index`` (original submesh slot), ``position``, and
        optionally ``uv`` / ``normal`` / ``bone_indices`` / ``bone_weights``.
        On round-trip the importer reinserts these at their original
        slots so the rebuilt PAC has identical vertex count + content
        to the source.

    ``per_submesh_view`` items are dicts produced from
    ``_filtered_submesh_view``; one per submesh in mesh.submeshes
    order, each with keys ``new_to_old`` and ``dropped``.

    Always writes (even with zero filtered verts) so import_fbx can
    rely on its presence to detect the v2 schema.
    """
    import json

    submeshes_json = []
    for sm, view in zip(mesh.submeshes, per_submesh_view):
        # bone_indices/weights here are POST-FILTER (matching the FBX).
        # Use the view fields, not sm.* (sm has the full pre-filter list).
        f_bi = view.get("bone_indices_post", [])
        f_bw = view.get("bone_weights_post", [])
        sm_json: dict = {
            "name": sm.name,
            "vertex_count": len(view["new_to_old"]),
            "bone_indices": [list(b) for b in f_bi],
            "bone_weights": [list(w) for w in f_bw],
            "source_vertex_map": list(view["new_to_old"]),
        }
        if view["dropped"]:
            sm_json["filtered_vertices"] = view["dropped"]
        if view.get("dropped_faces"):
            # Faces stored with ORIGINAL vertex indices (pre-filter).
            # The importer remaps them after expanding the vertex list.
            sm_json["filtered_faces"] = [
                [int(a), int(b), int(c)] for a, b, c in view["dropped_faces"]
            ]
        submeshes_json.append(sm_json)

    payload = {
        "schema_version": 2,
        "tool": "CrimsonForge",
        "source_path": mesh.path,
        "source_format": mesh.format,
        "submeshes": submeshes_json,
    }

    sidecar_path = base_path + ".cfmeta.json"
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
    except OSError as e:
        logger.warning("Failed to write cfmeta v2 sidecar %s: %s", sidecar_path, e)
        return None
    return sidecar_path


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

# ── COORDINATE SYSTEM CONVERSION ──
#
# Pearl Abyss stores everything in Y-UP right-handed coords (Maya convention).
# Blender's scene is Z-UP right-handed.  Blender's FBX importer applies
# the Y-up→Z-up conversion to bone Lcl T values but NOT to raw geometry
# vertex coords nor to cluster TransformLink matrices.  That inconsistency
# left the bones in Z-up scene coords while the mesh and cluster TLs stayed
# in Y-up — the cluster math then mixed coord systems and produced the
# 1.34m X-flipped drift (vertices weighted to chin bone got pulled along
# (chin_yup - chin_zup) ≈ (0, 1.5, -1.6)).
#
# Fix: pre-convert ALL of our data to Z-up at export time, set UpAxis=2
# in GlobalSettings so Blender DOESN'T try to convert again.
#
# The conversion: rotate +90° around X axis.  For a column vector:
#     (x, y, z)_yup → (x, -z, y)_zup
# For a column-major-flat 4×4 matrix M_yup transforming local→world both
# in Y-up coords, the equivalent in Z-up coords is:
#     M_zup = R · M_yup · R⁻¹
# where R = +90° X rotation.

def _yup_to_zup_vec3(v):
    """Convert a 3-vector (column form) from Y-up to Z-up."""
    x, y, z = v
    return (x, -z, y)


def _yup_to_zup_quat(q):
    """Convert a quaternion (xyzw) from Y-up to Z-up frame.

    Composing q_zup = r * q_yup * r⁻¹ where r is the +90° X rotation
    quaternion (x=sin(45°), y=0, z=0, w=cos(45°)).
    """
    import math
    s = math.sqrt(0.5)
    rx, ry, rz, rw = s, 0.0, 0.0, s
    qx, qy, qz, qw = q

    # r * q  (Hamilton product)
    ax = rw*qx + rx*qw + ry*qz - rz*qy
    ay = rw*qy - rx*qz + ry*qw + rz*qx
    az = rw*qz + rx*qy - ry*qx + rz*qw
    aw = rw*qw - rx*qx - ry*qy - rz*qz

    # (r*q) * r⁻¹  where r⁻¹ = (-rx, -ry, -rz, rw) for unit r
    bx = aw*(-rx) + ax*rw + ay*(-rz) - az*(-ry)
    by = aw*(-ry) - ax*(-rz) + ay*rw + az*(-rx)
    bz = aw*(-rz) + ax*(-ry) - ay*(-rx) + az*rw
    bw = aw*rw - ax*(-rx) - ay*(-ry) - az*(-rz)
    return (bx, by, bz, bw)


def _yup_to_zup_mat4(m):
    """Convert a column-major-flat 4×4 transformation matrix Y-up → Z-up.

    Computes R · M · R⁻¹ where R is the rotation +90° around X (the
    Y-up to Z-up basis change).

    R column-major flat:
        [1, 0, 0, 0,    0, 0, 1, 0,    0,-1, 0, 0,    0, 0, 0, 1]
    R⁻¹ = R transposed (for pure rotation):
        [1, 0, 0, 0,    0, 0,-1, 0,    0, 1, 0, 0,    0, 0, 0, 1]
    """
    R   = [1.0, 0.0, 0.0, 0.0,   0.0, 0.0, 1.0, 0.0,   0.0,-1.0, 0.0, 0.0,   0.0, 0.0, 0.0, 1.0]
    Rinv= [1.0, 0.0, 0.0, 0.0,   0.0, 0.0,-1.0, 0.0,   0.0, 1.0, 0.0, 0.0,   0.0, 0.0, 0.0, 1.0]
    # Use the existing _mat4_mul once it's defined; declared after this
    # function but Python late-binds inside calls, so this is fine.
    return _mat4_mul(_mat4_mul(R, list(m)), Rinv)


def _lcl_from_bind_matrix(m, scale: float = 1.0):
    """Decompose an FBX bind matrix into Lcl TRS using **Blender's**
    intrinsic XYZ Euler convention.

    BLENDER USES INTRINSIC XYZ — NOT EXTRINSIC XYZ.
    --------------------------------------------------
    The previous implementation used extrinsic XYZ (R = Rx · Ry · Rz),
    which is what Maya/MotionBuilder document for "RotationOrder=XYZ".
    But Blender's mathutils and FBX importer interpret RotationOrder=XYZ
    as INTRINSIC XYZ, which is mathematically equivalent to extrinsic
    ZYX:  R = Rz · Ry · Rx.  Different convention, different matrix
    from the same euler triplet.

    Concrete proof from the user's L Hand bone (matrix_local euler in
    Blender = (114.48°, 77.35°, 45.69°)):
      Extrinsic XYZ  Y-axis = Rx·Ry·Rz @ (0,1,0) = (-0.157, -0.925, 0.346)
      Intrinsic XYZ  Y-axis = Rz·Ry·Rx @ (0,1,0) = (+0.918, +0.345, +0.200)
    The actual tail−head direction in Blender is (0.917, 0.346, 0.199)
    — a perfect match for intrinsic XYZ, contradicting extrinsic XYZ.

    So when we decomposed using extrinsic XYZ and Blender recomposed
    using intrinsic XYZ, the recomposed matrix had a totally different
    rotation than our intent. Bones ended up oriented wrong → cluster
    math (matrix_local × inv(TransformLink)) ≠ identity at rest pose →
    every weighted vertex drifted along the rotation error.

    Layout
    ------
    FBX stores 4×4 matrices column-major, translation at m[12..14].
    Read R[row][col] = m[col*4 + row].

    Decomposition for R = Rz(γ) · Ry(β) · Rx(α) (column vectors):
        R[2][0] = -sin(β)
        R[2][1] =  cos(β) sin(α)
        R[2][2] =  cos(β) cos(α)
        R[0][0] =  cos(β) cos(γ)
        R[1][0] =  cos(β) sin(γ)

    Recover:
        β = -asin(R[2][0])
        α = atan2(R[2][1], R[2][2])    (cb cancels in atan2 ratio)
        γ = atan2(R[1][0], R[0][0])

    Gimbal lock at |R[2][0]| ≈ 1: γ degenerates with α; force γ=0,
    recover α from R[0][1] and R[1][1] (= sin/cos of α-γ at sb=±1).
    """
    import math

    tx, ty, tz = float(m[12]) * scale, float(m[13]) * scale, float(m[14]) * scale

    # 3x3 rotation block, column-vector convention: R[row][col] = m[col*4+row]
    R = [[float(m[col * 4 + row]) for col in range(3)] for row in range(3)]

    # Per-axis scale = column lengths
    sx = math.sqrt(R[0][0] ** 2 + R[1][0] ** 2 + R[2][0] ** 2) or 1.0
    sy = math.sqrt(R[0][1] ** 2 + R[1][1] ** 2 + R[2][1] ** 2) or 1.0
    sz = math.sqrt(R[0][2] ** 2 + R[1][2] ** 2 + R[2][2] ** 2) or 1.0

    R[0][0] /= sx; R[1][0] /= sx; R[2][0] /= sx
    R[0][1] /= sy; R[1][1] /= sy; R[2][1] /= sy
    R[0][2] /= sz; R[1][2] /= sz; R[2][2] /= sz

    # β from R[2][0] = -sin β
    neg_sin_b = max(-1.0, min(1.0, R[2][0]))
    sin_b = -neg_sin_b

    GIMBAL_THRESHOLD = 0.999999

    if abs(sin_b) < GIMBAL_THRESHOLD:
        beta = math.asin(sin_b)
        # α from R[2][1] = cb·sa, R[2][2] = cb·ca → atan2 cancels cb (cb > 0)
        alpha = math.atan2(R[2][1], R[2][2])
        # γ from R[1][0] = cb·sγ, R[0][0] = cb·cγ → atan2 cancels cb
        gamma = math.atan2(R[1][0], R[0][0])
    else:
        # Gimbal lock at β = ±π/2. cb = 0 collapses α and γ into a
        # single combined angle. Force γ=0 and recover α with the
        # SIGN-AWARE formula:
        #   sb = +1, γ=0: R[0][1] = sin α, R[1][1] = cos α  →  α = atan2( R[0][1], R[1][1])
        #   sb = -1, γ=0: R[0][1] = -sin α, R[1][1] = cos α →  α = atan2(-R[0][1], R[1][1])
        # The unified form uses sin_b as the sign factor:
        #   α = atan2(sin_b · R[0][1], R[1][1])
        # (Bip01 Pelvis at β = -90° was mis-decomposed by the unsigned
        # version, which gave α = -90° → recomposed Y axis (+1,0,0) →
        # 2.0 worst element error → 1.6m skin drift in Blender.)
        beta = math.copysign(math.pi / 2, sin_b)
        alpha = math.atan2(sin_b * R[0][1], R[1][1])
        gamma = 0.0

    return (tx, ty, tz,
            math.degrees(alpha), math.degrees(beta), math.degrees(gamma),
            sx, sy, sz)


def _mat4_from_lcl_trs(tx, ty, tz, rx_deg, ry_deg, rz_deg, sx, sy, sz):
    """Recompose a 4x4 column-major matrix from Lcl T/R/S values using
    Blender's INTRINSIC XYZ convention.

    R = Rz(γ) · Ry(β) · Rx(α)            (column-vector convention)

    Inverse of ``_lcl_from_bind_matrix``. See that function's docstring
    for why intrinsic XYZ (≡ extrinsic ZYX) is required to match
    Blender's importer — using extrinsic XYZ produced bones with
    spectacularly wrong orientations.

    Composed transform: M = T · R · S.
    """
    import math
    a = math.radians(rx_deg)
    b = math.radians(ry_deg)
    c = math.radians(rz_deg)
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cc, sc = math.cos(c), math.sin(c)

    # R = Rz · Ry · Rx, intrinsic XYZ for column vectors.
    # Verified against the L Hand bone empirical Y-axis match.
    R00 =  cc * cb
    R01 = -sc * ca + cc * sb * sa
    R02 =  sc * sa + cc * sb * ca
    R10 =  sc * cb
    R11 =  cc * ca + sc * sb * sa
    R12 = -cc * sa + sc * sb * ca
    R20 = -sb
    R21 =  cb * sa
    R22 =  cb * ca

    # M = T · R · S, column-major flat: m[col*4+row].
    return [
        R00 * sx, R10 * sx, R20 * sx, 0.0,    # column 0 (X axis × sx)
        R01 * sy, R11 * sy, R21 * sy, 0.0,    # column 1 (Y axis × sy)
        R02 * sz, R12 * sz, R22 * sz, 0.0,    # column 2 (Z axis × sz)
        tx,       ty,       tz,       1.0,    # column 3 (translation)
    ]


# ── 4x4 matrix helpers (column-major flat, FBX-on-disk layout) ──

def _mat4_mul(a, b):
    """C = A × B for two column-major-flat 4x4 matrices.

    C[row][col] = Σ_k A[row][k] * B[k][col]
    In column-major flat: M[row][col] = m[col*4 + row].
    """
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            s = 0.0
            for k in range(4):
                s += a[k * 4 + row] * b[col * 4 + k]
            out[col * 4 + row] = s
    return out


def _mat4_inverse(m):
    """Inverse of a 4x4 affine matrix in column-major flat layout.

    Bind matrices in this codebase are affine — the linear 3×3 block
    is rotation × (possibly non-uniform) scale, the bottom row is
    [0 0 0 1], and the translation lives in column 3 (indices 12,13,14).

    For ``M = [A t; 0 1]`` the inverse is ``[A⁻¹ −A⁻¹·t; 0 1]``. We
    compute the 3×3 inverse via cofactor / adjugate (only nine entries
    so it's auditable), then apply the affine formula. Returns identity
    if the linear part is singular (caller's safety net).
    """
    a = [float(v) for v in m]

    # 3x3 linear-part entries: A[i][j] = a[j*4+i] (column-major).
    a00, a10, a20 = a[0],  a[1],  a[2]
    a01, a11, a21 = a[4],  a[5],  a[6]
    a02, a12, a22 = a[8],  a[9],  a[10]

    # Cofactor matrix entries (transposed = adjugate of A).
    c00 =  (a11 * a22 - a12 * a21)
    c01 = -(a01 * a22 - a02 * a21)
    c02 =  (a01 * a12 - a02 * a11)
    c10 = -(a10 * a22 - a12 * a20)
    c11 =  (a00 * a22 - a02 * a20)
    c12 = -(a00 * a12 - a02 * a10)
    c20 =  (a10 * a21 - a11 * a20)
    c21 = -(a00 * a21 - a01 * a20)
    c22 =  (a00 * a11 - a01 * a10)

    det = a00 * c00 + a01 * c10 + a02 * c20
    if abs(det) < 1e-12:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
    inv_det = 1.0 / det

    # A⁻¹[i][j] = adj(A)[i][j] / det = cofactor(j, i) / det.
    inv00 = c00 * inv_det
    inv01 = c01 * inv_det
    inv02 = c02 * inv_det
    inv10 = c10 * inv_det
    inv11 = c11 * inv_det
    inv12 = c12 * inv_det
    inv20 = c20 * inv_det
    inv21 = c21 * inv_det
    inv22 = c22 * inv_det

    # Translation of inverse: −A⁻¹ · t   (t is column 3 of M).
    tx, ty, tz = a[12], a[13], a[14]
    inv_tx = -(inv00 * tx + inv01 * ty + inv02 * tz)
    inv_ty = -(inv10 * tx + inv11 * ty + inv12 * tz)
    inv_tz = -(inv20 * tx + inv21 * ty + inv22 * tz)

    # Pack column-major flat.
    return [
        inv00, inv10, inv20, 0.0,
        inv01, inv11, inv21, 0.0,
        inv02, inv12, inv22, 0.0,
        inv_tx, inv_ty, inv_tz, 1.0,
    ]


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
            # 100 cm per file unit = file is in METERS (Pearl Abyss
            # native units). See export_fbx_with_skeleton for full
            # explanation of why UnitScaleFactor=1.0 broke skinned
            # bones.
            W(b2, "P", ["UnitScaleFactor", "double", "Number", "", 100.0])
            W(b2, "P", ["OriginalUnitScaleFactor", "double", "Number", "", 100.0])
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
            for u, v in sm.uvs:
                uvs_flat.extend([u, 1.0 - v])

            def geom_node(b2, vf=verts_flat, iff=indices_flat, nf=normals_flat, uf=uvs_flat):
                def layer_elem_normal(b3, nf_=nf):
                    W(b3, "Version", [101])
                    W(b3, "Name", [""])
                    W(b3, "MappingInformationType", ["ByVertice"])
                    W(b3, "ReferenceInformationType", ["Direct"])
                    W(b3, "Normals", [nf_])

                def layer_elem_uv(b3, uf_=uf):
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
                    W(b3, "P", ["Lcl Rotation",    "Lcl Rotation",    "", "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Scaling",     "Lcl Scaling",     "", "A", 1.0, 1.0, 1.0])
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
            W(b, "C", ["OO", model_ids[idx], _FbxId(0)])
            W(b, "C", ["OO", mesh_ids[idx], model_ids[idx]])
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
                              name: str = "", scale: float = 1.0,
                              filter_unskinned_outliers: bool = False,
                              animation=None,
                              fps: float = 30.0) -> str:
    """Export mesh + skeleton (+ optional animation) to FBX.

    The skeleton parameter is a Skeleton object from skeleton_parser.
    Bone hierarchy is written as FBX LimbNode models connected to the
    mesh via Skin deformers. Compatible with Blender, Maya, Unity, Unreal.

    ``filter_unskinned_outliers`` defaults to **False** because the
    "unskinned + far from centroid" heuristic is too aggressive — it
    will misidentify legitimate static-mesh extremities like foot
    soles and similar rigid geometry as engine-helper "spike" vertices
    and delete them, leaving holes in the visible mesh. When False
    (default), all vertices are exported 1:1 with the source PAC and
    the round-trip is trivially lossless.

    Set to True ONLY if you've verified for THIS specific character
    that the unskinned outliers are truly engine helper geometry
    (e.g. via the blender_find_spike_verts.py probe + visual check
    that the affected vertices are not part of the silhouette). The
    sidecar v2 ``filtered_vertices`` block preserves the dropped
    data so a later ``build_pac`` reconstructs the full source mesh
    even when the filter was on.

    When ``animation`` is a ``ParsedAnimation`` (from
    ``core.animation_parser``), per-bone rotation animation curves are
    written into the same FBX:
      - AnimationStack + AnimationLayer (one each)
      - AnimationCurveNode per animated bone
      - 3 AnimationCurves per animated bone (X/Y/Z Euler)
      - Connections wiring curves -> CurveNode -> Bone.Lcl Rotation
    All keyframe quaternions are axis-converted Y-up→Z-up to match the
    bind matrices, then decomposed to intrinsic XYZ Euler (Blender's
    convention). The result is a single FBX with skinned mesh +
    posable armature + animated rest pose. ``fps`` controls keyframe
    timing when the PAA didn't carry an explicit duration.

    Writes a verbose ``<base>.fbx.debug.txt`` companion file with every
    bone, every cluster, every weight summary so the export can be
    inspected after the fact without reverse-engineering the binary.
    """
    from core.skeleton_parser import Skeleton

    os.makedirs(output_dir, exist_ok=True)
    base = name or Path(mesh.path).stem
    fbx_path = os.path.join(output_dir, f"{base}.fbx")
    debug_path = os.path.join(output_dir, f"{base}.fbx.debug.txt")

    # ── DEBUG LOG ── verbose write-as-we-go log
    debug_lines: list[str] = []
    def D(msg: str = ""):
        debug_lines.append(msg)
        logger.info("[FBX] %s", msg)

    D("=" * 70)
    D(f"FBX EXPORT START — {base}")
    D(f"Output : {fbx_path}")
    D(f"Debug  : {debug_path}")
    D("=" * 70)
    D(f"Mesh    : {len(mesh.submeshes)} submeshes, "
      f"{mesh.total_vertices} verts, {mesh.total_faces} faces")
    D(f"Skeleton: {len(skeleton.bones) if skeleton else 0} bones")
    D(f"Scale   : {scale}")
    D("")
    D("--- Per-submesh ---")
    for i, sm in enumerate(mesh.submeshes):
        n_with_skin = sum(1 for bi in (sm.bone_indices or [])
                          if bi and any(b is not None for b in bi))
        D(f"  [{i}] {sm.name!r}: {len(sm.vertices)} verts, "
          f"{len(sm.faces)} faces, {n_with_skin} skinned")

    # ── COMPUTE SPIKE FILTER per submesh (engine helper geometry) ──
    # If filter_unskinned_outliers is False, all spike sets are empty
    # so the views are pass-through (identity new_to_old, no dropped).
    submesh_views: list[dict] = []
    total_filtered = 0
    if filter_unskinned_outliers:
        D("")
        D("--- Spike-vertex filter (unskinned outliers removed) ---")
    for i, sm in enumerate(mesh.submeshes):
        spikes = (_compute_spike_indices(sm)
                  if filter_unskinned_outliers else set())
        (f_verts, f_uvs, f_normals, f_faces,
         f_bi, f_bw, new_to_old, dropped,
         dropped_faces) = _filtered_submesh_view(sm, spikes)
        view = {
            "verts": f_verts,
            "uvs": f_uvs,
            "normals": f_normals,
            "faces": f_faces,
            "bone_indices_post": f_bi,
            "bone_weights_post": f_bw,
            "new_to_old": new_to_old,
            "dropped": dropped,
            "dropped_faces": dropped_faces,
            "spikes_count": len(spikes),
        }
        submesh_views.append(view)
        total_filtered += len(spikes)
        if filter_unskinned_outliers:
            kept = len(f_verts)
            orig = len(sm.vertices)
            kept_faces = len(f_faces)
            orig_faces = len(sm.faces)
            D(f"  [{i}] {sm.name!r}: filtered {len(spikes)} spike verts  "
              f"({kept}/{orig} kept)  faces {kept_faces}/{orig_faces}")
    if filter_unskinned_outliers:
        D(f"  Total filtered: {total_filtered} verts "
          f"(preserved verbatim in .cfmeta.json sidecar v2 for round-trip)")

    if skeleton and skeleton.bones:
        D("")
        D("--- First 10 bones (sanity check) ---")
        for b in skeleton.bones[:10]:
            bm = b.bind_matrix or (1.0,)*16
            D(f"  [{b.index:3d}] {b.name!r:<32s} parent={b.parent_index:>4} "
              f"world_pos=({bm[12]:>7.3f}, {bm[13]:>7.3f}, {bm[14]:>7.3f})")
        D("")
        D("--- Last 10 bones (recovery check) ---")
        for b in skeleton.bones[-10:]:
            bm = b.bind_matrix or (1.0,)*16
            D(f"  [{b.index:3d}] {b.name!r:<32s} parent={b.parent_index:>4} "
              f"world_pos=({bm[12]:>7.3f}, {bm[13]:>7.3f}, {bm[14]:>7.3f})")

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
            # Z-UP scene (matches Blender). All our geometry/bone data
            # is pre-converted from Pearl Abyss's native Y-up to Z-up
            # at export time (see _yup_to_zup_* helpers). Declaring
            # UpAxis=2 here tells Blender "no conversion needed" — if
            # we left UpAxis=1 (Y-up), Blender's importer would apply
            # the Y-up→Z-up rotation to bone Lcl T values but NOT to
            # mesh vertex coords or cluster TransformLinks, mixing
            # coord systems and causing the spike-shard explosion
            # we chased through five rounds of bug-fixing.
            #
            # FBX axis encoding: UpAxis 0=X 1=Y 2=Z, FrontAxis 0=X 1=Y 2=Z,
            # CoordAxis 0=X 1=Y 2=Z. Z-up right-handed convention:
            #   Up = +Z, Front = -Y, Right = +X.
            W(b2, "P", ["UpAxis", "int", "Integer", "", 2])
            W(b2, "P", ["UpAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["FrontAxis", "int", "Integer", "", 1])
            W(b2, "P", ["FrontAxisSign", "int", "Integer", "", -1])
            W(b2, "P", ["CoordAxis", "int", "Integer", "", 0])
            W(b2, "P", ["CoordAxisSign", "int", "Integer", "", 1])
            # 100 cm per file unit = file is in METERS.
            W(b2, "P", ["UnitScaleFactor", "double", "Number", "", 100.0])
            W(b2, "P", ["OriginalUnitScaleFactor", "double", "Number", "", 100.0])
            # Animation timing — only meaningful when animation is
            # present. TimeMode 11 = 30 fps. TimeSpan tells the importer
            # the animation playback range. Compute the final tick
            # inline since the per-bone Euler series isn't built yet at
            # this point in the export.
            if animation is not None:
                _ktps = 46_186_158_000  # KTime ticks per second
                _total_frames = max(animation.frame_count,
                                    len(animation.keyframes))
                _dur = animation.duration
                if _dur > 0 and _total_frames > 1:
                    _seconds = _dur
                else:
                    _seconds = (max(_total_frames, 1) - 1) / max(fps, 1.0)
                _final_tick = int(_seconds * _ktps)
                W(b2, "P", ["TimeMode", "enum", "", "", 11])
                W(b2, "P", ["TimeSpanStart", "KTime", "Time", "", 0])
                W(b2, "P", ["TimeSpanStop", "KTime", "Time", "",
                            _final_tick])
                W(b2, "P", ["CustomFrameRate", "double", "Number", "",
                            float(fps)])
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
    pose_id = uid()

    # ── ARMATURE NULL (only when `animation` is provided) ──
    # Blender's FBX importer needs an explicit "Null" Model node as the
    # common parent of all root bones to recognize the bone tree as
    # ONE armature object — and crucially, to know where to attach the
    # Action animation data on import. Without it, the bones get
    # imported and form an armature visually, but the AnimationStack
    # isn't bound to anything → outliner shows no "Animation" entry
    # under the armature, Spacebar does nothing.
    #
    # Mesh-only exports work fine WITHOUT this Null because Blender
    # creates an implicit armature. But the moment you add animation
    # curves, Blender requires the explicit Null to wire them up.
    armature_null_id = uid() if animation is not None else None

    # ── DOCUMENTS + DEFINITIONS section (animation only) ──
    # Blender's FBX importer needs both:
    #   - Documents.ActiveAnimStackName tells it which stack to play
    #   - Definitions ObjectType counts let it allocate animation slots
    # Without these, AnimationStack/Layer/Curve nodes are parsed but
    # silently dropped — the user sees an armature with no Action data
    # attached.
    if animation is not None and skeleton and skeleton.bones:
        doc_root_id = uid()
        anim_name = name or Path(mesh.path).stem
        def documents(b):
            W(b, "Count", [1])
            def document(b2):
                def doc_props(b3):
                    W(b3, "P", ["SourceObject", "object", "", ""])
                    W(b3, "P", ["ActiveAnimStackName", "KString", "", "",
                                anim_name])
                W(b2, "Properties70", children=[doc_props])
                W(b2, "RootNode", [0])
            W(b, "Document",
              [doc_root_id, "Scene\x00\x01SceneInfo", "Scene"],
              children=[document])
        W(buf, "Documents", children=[documents])

        _bone_count_for_def = len(skeleton.bones)
        def definitions(b):
            type_counts = [
                ("Model", _bone_count_for_def + len(mesh.submeshes)),
                ("NodeAttribute", _bone_count_for_def),
                ("Geometry", len(mesh.submeshes)),
                ("Material", len(mesh.submeshes)),
                ("Deformer", len(mesh.submeshes)
                            + _bone_count_for_def * len(mesh.submeshes)),
                ("Pose", 1),
                ("AnimationStack", 1),
                ("AnimationLayer", 1),
                ("AnimationCurveNode", _bone_count_for_def),
                ("AnimationCurve", _bone_count_for_def * 3),
            ]
            W(b, "Version", [100])
            W(b, "Count", [len(type_counts)])
            for ot, count in type_counts:
                def object_type(b2, _ot=ot, _c=count):
                    W(b2, "Count", [_c])
                W(b, "ObjectType", [ot], children=[object_type])
        W(buf, "Definitions", children=[definitions])

    # ── ANIMATION PREP (only when `animation` is provided) ──────────
    # Computes per-bone Euler keyframe series + KTime ticks. The actual
    # AnimationStack/Layer/CurveNode/Curve nodes get emitted in
    # objects() and wired in connections() — but we need IDs and the
    # per-bone series here so both phases can use them.
    has_animation = animation is not None and skeleton and skeleton.bones
    anim_stack_id = uid() if has_animation else None
    anim_layer_id = uid() if has_animation else None
    curve_node_ids: dict[int, _FbxId] = {}
    curve_ids: dict[int, list[_FbxId]] = {}
    per_bone_eulers: dict[int, list[tuple[float, float, float]]] = {}
    frame_ticks: list[int] = []
    final_tick = 0

    if has_animation:
        from core.animation_fbx_exporter import (
            _quat_xyzw_to_euler_xyz_degrees,
            _ensure_euler_continuity,
            _canonicalize_quaternion_sign,
            _frame_ticks,
            KTIME_TICKS_PER_SECOND,
        )

        def _quat_mul(q1, q2):
            x1, y1, z1, w1 = q1
            x2, y2, z2, w2 = q2
            return (
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
                w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            )

        total_frames = max(animation.frame_count, len(animation.keyframes))
        duration = animation.duration
        # Allocate IDs per skeleton bone (only those with rotation data).
        for bone in skeleton.bones:
            curve_node_ids[bone.index] = uid()
            curve_ids[bone.index] = [uid(), uid(), uid()]

        # Compute per-bone Euler series.
        #   1. (delta-mode only) Compose PAB bind rotation × PAA per-
        #      frame rotation in Y-up. Old PAA formats store
        #      per-frame rotation as a DELTA from bind so we need to
        #      add the bind back in to recover the local rotation.
        #   2. (absolute-mode) PAA per-frame quaternion IS the bone's
        #      full local rotation already. No composition.
        #   3. Convert Y-up → Z-up via _yup_to_zup_quat.
        #   4. Decompose to intrinsic XYZ Euler degrees.
        # The exporter picks delta vs absolute based on the
        # animation.embedded_tracks_absolute flag set by the parser
        # (True for the link-with-embedded-tracks layout used by
        # cd_damian_* walks; False for old PAA formats like sample_talk).
        is_absolute = bool(getattr(animation,
                                    "embedded_tracks_absolute", False))
        D(f"Animation rotation mode: "
          f"{'ABSOLUTE (no bind composition)' if is_absolute else 'DELTA (bind × paa)'}")
        for bone in skeleton.bones:
            bind_rot = bone.rotation or (0.0, 0.0, 0.0, 1.0)
            series: list[tuple[float, float, float]] = []
            previous_euler: tuple[float, float, float] | None = None
            previous_quat: tuple[float, float, float, float] | None = None
            for kf in animation.keyframes:
                if bone.index < len(kf.bone_rotations):
                    paa_quat = kf.bone_rotations[bone.index]
                else:
                    paa_quat = (0.0, 0.0, 0.0, 1.0)
                if is_absolute:
                    # PAA quat IS the local rotation already. Use directly.
                    quat_yup = paa_quat
                else:
                    # PAA quat is a delta from bind. Compose with bind.
                    quat_yup = _quat_mul(bind_rot, paa_quat)
                quat = _yup_to_zup_quat(quat_yup)
                quat = _canonicalize_quaternion_sign(quat, previous_quat)
                previous_quat = quat
                euler = _quat_xyzw_to_euler_xyz_degrees(*quat)
                euler = _ensure_euler_continuity(euler, previous_euler)
                series.append(euler)
                previous_euler = euler
            per_bone_eulers[bone.index] = series

        # Ticks for each keyframe.
        for i in range(total_frames):
            frame_ticks.append(_frame_ticks(i, total_frames, duration, fps))
        final_tick = frame_ticks[-1] if frame_ticks else 0

        D("")
        D(f"=== Animation export ===")
        D(f"Animation frames: {total_frames}, duration: {duration:.3f}s, fps: {fps}")
        D(f"Animated bones: {len(per_bone_eulers)}")
        D(f"Final tick: {final_tick}")

    # ── Skin-export-safety check ──
    # If the parsed skeleton is incomplete (we know the PAB parser
    # bails after ~56 bones on real character skeletons of 178-192
    # bones, padding the rest with identity stubs), exporting Skin/
    # Cluster deformers produces the "spiky explosion" — vertices
    # weighted to stub bones get pulled to world origin since their
    # bind matrix is identity instead of the real bone position.
    #
    # The fix: when we detect ANY stub bones in the skeleton, skip
    # the Skin deformer entirely. The FBX still contains the full
    # bone armature for visual reference, but the mesh is exported
    # as static geometry parented to the root. Vertices stay in
    # their bind-pose positions (correct), the user edits in
    # Blender, and the .cfmeta.json sidecar carries the real per-
    # vertex bone weights through the round-trip — so the rebuilt
    # PAC has correct skinning even though the FBX didn't display
    # it. Same path the OBJ flow already uses.
    has_stub_bones = bool(
        skeleton and any(
            isinstance(getattr(b, 'name', None), str) and b.name.startswith('_stub_bone_')
            for b in skeleton.bones
        )
    )
    skip_skin = has_stub_bones
    n_stubs = (sum(1 for b in skeleton.bones if b.name.startswith('_stub_bone_'))
               if skeleton else 0)
    D("")
    D(f"=== Skin export decision ===")
    D(f"has_stub_bones = {has_stub_bones} (stub count: {n_stubs})")
    D(f"skip_skin     = {skip_skin}  "
      f"({'NO armature/skin written' if skip_skin else 'WILL write armature + skin'})")
    if skip_skin:
        logger.warning(
            "FBX export %s: skeleton has stub bones — skipping Skin "
            "deformer to avoid spike artifacts. Skin weights will "
            "round-trip via the .cfmeta.json sidecar instead.",
            base
        )

    # Precompute skinning data per submesh.
    # For each submesh, we collect the set of (bone_index, [(vertex_idx, weight), ...])
    # entries. Only bones that actually influence at least one vertex in the
    # submesh get a Cluster — FBX importers tolerate unused bones but empty
    # clusters confuse some importers.
    skin_ids: list[_FbxId | None] = []          # one per submesh; None if no bones used
    cluster_ids: list[dict[int, _FbxId]] = []    # per submesh: {bone_index: cluster_id}
    cluster_data: list[dict[int, list[tuple[int, float]]]] = []  # per submesh: {bone_index: [(vi, w)]}

    for sm_idx, sm in enumerate(mesh.submeshes):
        per_bone: dict[int, list[tuple[int, float]]] = {}
        if skeleton and skeleton.bones and not skip_skin:
            # ── WEIGHT NORMALIZATION (root cause of partial-weight collapse) ──
            #
            # The PAC vertex format stores up to 4 (slot, weight) pairs.
            # The engine quantizes weights to u8 and DOESN'T require them
            # to sum to 255 — many vertices store only their dominant
            # influence(s), e.g. raw weights (3, 0, 0, 0) for a vertex
            # that's nearly-rigid to one bone. The GPU shader normalizes
            # at runtime: w_normalized = w_raw / Σ w_raw.
            #
            # FBX clusters DON'T renormalize. If we hand Blender weights
            # (0.012,) for a vertex, Blender's armature modifier computes
            # rest_pos = 0.012 × bone_bind × inv(bone_bind) × v_local
            #          = 0.012 × v_local
            # — the vertex collapses to 1.2% of its world-bind distance
            # from origin. With v_local at ~1.3m, that's a 1.3m drift.
            # Across thousands of vertices, you get the spike-shard pattern
            # (each vertex pulled along its own ray toward world origin).
            #
            # Fix: normalize per-vertex before binding. Σ w → 1.0.
            #
            # Iterate the post-filter view's bone arrays so cluster vi
            # values are NEW indices (matching the FBX geometry's
            # filtered vertex order). The view's bone_indices_post /
            # bone_weights_post lists are the same length as the
            # filtered vertex array, indexed by NEW vi.
            view = submesh_views[sm_idx]
            view_bi = view["bone_indices_post"]
            view_bw = view["bone_weights_post"]
            # ── DEFENSIVE WEIGHT REMAPPING ──
            # Pearl Abyss's "B_TL_*" control bones and "B_MoveControl_*"
            # are SIBLINGS of the Bip01 root in the PAB hierarchy
            # (parent = -1, not parent = Bip01). They don't animate
            # during walk — they're locomotion target points the IK
            # rig uses to plan footstep paths.
            #
            # When character animation translates Bip01 forward, these
            # control bones STAY AT THEIR WORLD BIND POSITION because
            # their parent is the root, not Bip01. Any vertex weighted
            # to them gets LEFT BEHIND while the rest of the character
            # walks forward — that's the upper-body-shatter pattern
            # the user has been seeing.
            #
            # Fix: redirect any weight that lands on a non-animated
            # control bone to Bip01 instead. Bip01 IS the animated
            # root, so the verts will at least translate with the
            # character. This won't fix the per-submesh skinning slot
            # palette issue (still a mystery), but it eliminates the
            # "trail of stationary fragments" symptom.
            CONTROL_BONE_PREFIXES = ("B_TL_", "B_MoveControl_",)
            BIP01_INDEX = 0  # always bone 0 in PA character skeletons
            for vi, (bones, weights) in enumerate(zip(view_bi, view_bw)):
                wsum = sum(float(w) for w in weights if w > 0.0)
                if wsum <= 1e-6:
                    continue
                inv_sum = 1.0 / wsum
                for b_idx, w in zip(bones, weights):
                    w = float(w)
                    if w <= 0.0:
                        continue
                    if not (0 <= b_idx < len(skeleton.bones)):
                        continue
                    bone_name = skeleton.bones[int(b_idx)].name
                    is_control = any(
                        bone_name.startswith(p)
                        for p in CONTROL_BONE_PREFIXES
                    )
                    target_b = BIP01_INDEX if is_control else int(b_idx)
                    per_bone.setdefault(target_b, []).append(
                        (vi, w * inv_sum))
        if per_bone:
            skin_ids.append(uid())
            cluster_ids.append({b_idx: uid() for b_idx in per_bone})
        else:
            skin_ids.append(None)
            cluster_ids.append({})
        cluster_data.append(per_bone)
        # Debug: per-submesh skin summary
        if not skip_skin:
            n_clusters = len(per_bone)
            total_pairs = sum(len(v) for v in per_bone.values())
            D(f"  submesh[{len(skin_ids)-1}] {sm.name!r}: "
              f"{n_clusters} clusters, {total_pairs} (vert, weight) pairs")
            # Sample 3 bones
            for j, (bi, pairs) in enumerate(list(per_bone.items())[:3]):
                bone_name = (skeleton.bones[bi].name
                             if 0 <= bi < len(skeleton.bones) else f'bone#{bi}')
                D(f"      bone[{bi:3d}] {bone_name!r:<25s}: "
                  f"{len(pairs)} verts (e.g. vi={pairs[0][0]} w={pairs[0][1]:.3f})")

    # Objects
    def objects(b):
        # Mesh geometry + model + material (same as before)
        for idx, sm in enumerate(mesh.submeshes):
            mid = mesh_ids[idx]
            mod_id = model_ids[idx]
            ma_id = mat_ids[idx]

            # Y-up → Z-up axis swap on vertex positions and normals so
            # they end up in the same coordinate system as the (already
            # converted) bone matrices and cluster TransformLinks.
            # See _yup_to_zup_vec3 docstring at the top of this file.
            #
            # Use the post-filter view (spike vertices removed). When
            # filter_unskinned_outliers=False the view is identical to
            # sm so this is a transparent change.
            view = submesh_views[idx]
            view_verts = view["verts"]
            view_faces = view["faces"]
            view_normals = view["normals"]

            verts_flat = []
            for x, y, z in view_verts:
                vx, vy, vz = _yup_to_zup_vec3((x * scale, y * scale, z * scale))
                verts_flat.extend([vx, vy, vz])

            indices_flat = []
            for a, b_idx, c in view_faces:
                indices_flat.extend([a, b_idx, c ^ -1])

            normals_flat = []
            for nx, ny, nz in view_normals:
                nvx, nvy, nvz = _yup_to_zup_vec3((nx, ny, nz))
                normals_flat.extend([nvx, nvy, nvz])

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
                W(b2, "MultiLayer", [0])
                W(b2, "MultiTake", [0])
                W(b2, "Shading", [True])
                W(b2, "Culling", ["CullingOff"])
                # Explicit identity Lcl TRS — mesh sits at world origin.
                # Matters because BindPose has the mesh at identity, and
                # TransformLink gives bone world positions; if the mesh
                # had a non-identity transform the cluster math would
                # shift every skinned vertex by that transform.
                def mesh_props(b3):
                    W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A",
                                0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Rotation", "Lcl Rotation", "", "A",
                                0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Scaling", "Lcl Scaling", "", "A",
                                1.0, 1.0, 1.0])
                W(b2, "Properties70", children=[mesh_props])
            W(b, "Model", [mod_id, f"{sm.name}\x00\x01Model", "Mesh"],
              children=[model_node])

            def mat_node(b2):
                W(b2, "Version", [102])
                W(b2, "ShadingModel", ["phong"])
            W(b, "Material", [ma_id, f"{sm.material or sm.name}\x00\x01Material", ""],
              children=[mat_node])

        # ── Precompute LOCAL bind matrix per bone ──
        #
        # FBX Lcl Translation/Rotation/Scaling on a bone Model is **parent-
        # relative** — Blender's importer compounds it up the hierarchy at
        # frame 0. If we feed the WORLD bind TRS into Lcl directly while
        # also wiring a real parent chain, the parent's transform stacks
        # on top of the child's already-world-space Lcl, putting bones at
        # double-transform positions = spikes.
        #
        # The fix: compute LOCAL bind = inv(parent_world) × bone_world per
        # bone, decompose THAT for Lcl TRS. The parent compounding then
        # restores M_pose_world = bone_world at frame 0, which equals
        # TransformLink → skin deformer sees identity → no deformation.
        #
        # TransformLink and BindPose Matrix stay as the WORLD bind matrix
        # because that's what the FBX cluster math expects (skinning
        # formula: V_world = M_pose_world × inv(TransformLink) × Transform).
        local_bind_by_idx: dict[int, list[float]] = {}
        if skeleton and skeleton.bones:
            # Convert each bone's world bind matrix from Y-up (Pearl Abyss
            # native) to Z-up (Blender scene). Every downstream consumer
            # — local_bind chain, cluster TransformLink, BindPose Matrix
            # — uses these converted matrices, so the entire skin pipeline
            # stays in Z-up consistently.
            world_by_idx = {
                bn.index: (_yup_to_zup_mat4(
                                [float(v) for v in bn.bind_matrix])
                           if getattr(bn, "bind_matrix", None)
                              and len(bn.bind_matrix) == 16
                           else [
                               1.0, 0.0, 0.0, 0.0,
                               0.0, 1.0, 0.0, 0.0,
                               0.0, 0.0, 1.0, 0.0,
                               0.0, 0.0, 0.0, 1.0,
                           ])
                for bn in skeleton.bones
            }
            # ── PROPER PARENT-CHILD HIERARCHY ──
            #
            # Each bone's Lcl TRS is its parent-relative LOCAL bind
            # matrix.  Blender compounds parent × child up the
            # hierarchy to reconstruct world bind = TransformLink.
            # Cluster math at rest pose: matrix_local × inv(TL) =
            # identity → vertex stays at rest.
            #
            # This is the natural FBX rigging convention. It depends
            # critically on:
            #   (1) Decomposition using INTRINSIC XYZ Euler (Blender's
            #       interpretation), not extrinsic XYZ.
            #   (2) Sign-aware gimbal-lock formula for sb=±1 (the
            #       Bip01 Pelvis fix; otherwise gimbal cases get
            #       2.0-element errors that compound to multi-meter
            #       drift).
            # Both are now in place — verified chain-compound error
            # is fp32 floor (1.52e-5).
            for bn in skeleton.bones:
                w = world_by_idx[bn.index]
                p_idx = bn.parent_index
                if p_idx is not None and p_idx >= 0 and p_idx in world_by_idx:
                    pw_inv = _mat4_inverse(world_by_idx[p_idx])
                    local_bind_by_idx[bn.index] = _mat4_mul(pw_inv, w)
                else:
                    # Root bone: local == world.
                    local_bind_by_idx[bn.index] = list(w)

            # Debug — print Lcl TRS that will be written for first 6 bones
            D("")
            D("=== Lcl TRS for first 6 bones (parent-local, written to FBX) ===")
            for bn in skeleton.bones[:6]:
                local = local_bind_by_idx.get(bn.index, [])
                if len(local) == 16:
                    tx, ty, tz, rx, ry, rz, sx, sy, sz = \
                        _lcl_from_bind_matrix(local, scale)
                    D(f"  [{bn.index:3d}] {bn.name!r:<22s} "
                      f"T=({tx:>7.3f},{ty:>7.3f},{tz:>7.3f}) "
                      f"R=({rx:>7.2f},{ry:>7.2f},{rz:>7.2f}) "
                      f"S=({sx:.3f},{sy:.3f},{sz:.3f})")

            # ── ACTIVE REST-POSE VERIFICATION ──
            # Decomposition self-test: for every bone, decompose
            # local_bind into Lcl TRS, recompose to a 4x4 matrix, and
            # compare to the original local_bind. Any drift here is a
            # decomposition bug — Blender sees a different rest pose
            # than we intended → mesh explodes when skin modifier
            # tries to reach a TransformLink the bones can't actually
            # reach via Lcl TRS compounding.
            #
            # Then chain self-test: starting from each root, compound
            # local matrices down the hierarchy and check that the
            # resulting world matrix equals the original bone world
            # bind matrix. Drift here means parent-child math is wrong.
            def _mat_max_diff(a, b):
                return max(abs(float(a[i]) - float(b[i])) for i in range(16))

            decomp_worst = 0.0
            decomp_worst_idx = -1
            for bn in skeleton.bones:
                local = local_bind_by_idx.get(bn.index, [])
                if len(local) != 16:
                    continue
                # Decompose with scale=1 (we're verifying the geometry
                # transform, not the user-facing world units).
                tx, ty, tz, rx, ry, rz, sx, sy, sz = \
                    _lcl_from_bind_matrix(local, 1.0)
                recomposed = _mat4_from_lcl_trs(
                    tx, ty, tz, rx, ry, rz, sx, sy, sz)
                err = _mat_max_diff(local, recomposed)
                if err > decomp_worst:
                    decomp_worst = err
                    decomp_worst_idx = bn.index

            chain_worst = 0.0
            chain_worst_idx = -1
            simulated_world: dict[int, list[float]] = {}
            for bn in skeleton.bones:
                local = local_bind_by_idx.get(bn.index, [])
                if len(local) != 16:
                    continue
                p_idx = bn.parent_index
                if (p_idx is not None and p_idx >= 0
                        and p_idx in simulated_world):
                    sim_world = _mat4_mul(simulated_world[p_idx], local)
                else:
                    sim_world = list(local)
                simulated_world[bn.index] = sim_world
                expected = world_by_idx.get(bn.index)
                if expected and len(expected) == 16:
                    err = _mat_max_diff(sim_world, expected)
                    if err > chain_worst:
                        chain_worst = err
                        chain_worst_idx = bn.index

            decomp_worst_name = (skeleton.bones[decomp_worst_idx].name
                                 if decomp_worst_idx >= 0 else 'n/a')
            chain_worst_name = (skeleton.bones[chain_worst_idx].name
                                if chain_worst_idx >= 0 else 'n/a')
            D("")
            D("=== ACTIVE REST-POSE VERIFICATION ===")
            D(f"Decompose→recompose worst error: {decomp_worst:.6g}  "
              f"(bone {decomp_worst_idx} = {decomp_worst_name!r})")
            D(f"Parent-chain compounding error  : {chain_worst:.6g}  "
              f"(bone {chain_worst_idx} = {chain_worst_name!r})")
            if decomp_worst > 1e-3:
                D("  ⚠ WARN: decomposition drifts > 1e-3 — Blender will "
                  "see wrong rest pose for the worst bone")
            if chain_worst > 1e-3:
                D("  ⚠ WARN: chain compounding drifts > 1e-3 — bone "
                  "hierarchy reconstruction is broken")
            if decomp_worst <= 1e-4 and chain_worst <= 1e-4:
                D("  ✓ Math is sound — Blender's rest pose should "
                  "exactly match TransformLink for every bone.")

            # ── PER-SUBMESH RAW SKIN AUDIT ──
            # For each submesh, log:
            #  - bone palette length and a sample of entries
            #  - number of vertices with non-empty bone tuple
            #  - first 5 vertices' (bone tuple, weight tuple) so we can
            #    eyeball whether slots are sensible (small numbers in
            #    palette range) vs. random (full 0..255 spread).
            # Ratio < 5% skinned for a character submesh almost always
            # means the vertex layout offsets are wrong.
            D("")
            D("=== PER-SUBMESH RAW SKIN AUDIT ===")
            for i, sm in enumerate(mesh.submeshes):
                bp = list(getattr(sm, "bone_palette", ())) \
                     if hasattr(sm, "bone_palette") else []
                n_skinned = sum(1 for bi in (sm.bone_indices or []) if bi)
                n_total = len(sm.vertices)
                pct = (100.0 * n_skinned / n_total) if n_total else 0.0
                D(f"  submesh[{i}] {sm.name!r}: {n_skinned}/{n_total} "
                  f"({pct:.1f}%) skinned")
                if bp:
                    D(f"      palette: {len(bp)} entries, "
                      f"first 10 = {bp[:10]}, max = {max(bp)}")
                # Show first 5 SKINNED verts (more informative than v[0..4]
                # which are usually unskinned static parts).
                shown = 0
                weight_sums = []
                for vi in range(n_total):
                    bi_t = sm.bone_indices[vi] if vi < len(sm.bone_indices) else ()
                    bw_t = sm.bone_weights[vi] if vi < len(sm.bone_weights) else ()
                    if not bi_t:
                        continue
                    if shown < 5:
                        D(f"      v[{vi}] bones={tuple(bi_t)} "
                          f"weights={tuple(round(w, 3) for w in bw_t)} "
                          f"sum={sum(bw_t):.3f}")
                        shown += 1
                    weight_sums.append(sum(bw_t))
                if weight_sums:
                    avg_sum = sum(weight_sums) / len(weight_sums)
                    min_sum = min(weight_sums)
                    max_sum = max(weight_sums)
                    D(f"      weight-sum stats: min={min_sum:.3f} "
                      f"avg={avg_sum:.3f} max={max_sum:.3f}  "
                      f"({'OK' if 0.95 <= min_sum and max_sum <= 1.05 else '⚠ NOT NORMALIZED'})")

        # Armature Null parent (animation only).
        # Acts as the explicit "armature object" in Blender's outliner
        # so the importer can attach Action data. Without it (mesh-only
        # exports) Blender creates an implicit armature from the bone
        # tree — works fine for static meshes but breaks animation
        # binding.
        if armature_null_id is not None:
            def armature_null_node(b2):
                W(b2, "Version", [232])
                W(b2, "MultiLayer", [0])
                W(b2, "MultiTake", [0])
                W(b2, "Shading", [True])
                W(b2, "Culling", ["CullingOff"])
                def arm_props(b3):
                    W(b3, "P", ["Lcl Translation", "Lcl Translation",
                                "", "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Rotation", "Lcl Rotation",
                                "", "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Scaling", "Lcl Scaling",
                                "", "A", 1.0, 1.0, 1.0])
                W(b2, "Properties70", children=[arm_props])
            W(b, "Model",
              [armature_null_id, "Armature\x00\x01Model", "Null"],
              children=[armature_null_node])

        # Bone nodes
        # When skip_skin is True we don't write any bones at all. The
        # mesh exports as static geometry; Blender shows it cleanly
        # without an armature; the .cfmeta.json sidecar carries the
        # weights. Adding the bones here would just clutter the scene
        # with octahedra at wrong positions (the user's "spike-shape
        # accessories" turned out to be Blender's bone-display for
        # the 136 stub bones piled at world origin — looked like
        # mesh artifacts when the body mesh was visible).
        if skeleton and skeleton.bones and not skip_skin:
            for bone in skeleton.bones:
                # NodeAttribute (LimbNode) — match Blender's exporter:
                # TypeFlags + Properties70 with Size. The Size property is
                # what tells Blender how long to draw each bone visually,
                # but its presence ALSO seems to be required for some
                # importers to position the bone correctly. Without a
                # Properties70 here, Blender falls back to default bone
                # placement (head=origin) and clusters then drag every
                # vertex toward origin via the inv(TransformLink) factor
                # — exactly the symptom we're seeing.
                def bone_attr(b2, bn=bone):
                    def attr_props(b3):
                        W(b3, "P", ["Size", "double", "Number", "", 0.05])
                    W(b2, "Properties70", children=[attr_props])
                    W(b2, "TypeFlags", ["Skeleton"])
                W(b, "NodeAttribute", [bone_attr_ids[bone.index],
                    f"{bone.name}\x00\x01NodeAttribute", "LimbNode"],
                    children=[bone_attr])

                # Model for bone — Lcl TRS is the LOCAL (parent-relative)
                # bind matrix decomposed via the column-vector XYZ-Euler
                # path in _lcl_from_bind_matrix.
                #
                # CRITICAL: InheritType=1 (RSrs) prevents parent scale
                # from propagating to children. The FBX default is
                # 0 (RrSs), under which any tiny scale error in a
                # parent bone gets multiplied through every descendant —
                # over 6+ levels of hierarchy that produces the
                # spike-shard explosion (vertices weighted to deep
                # bones get cube-of-error scaling).
                #
                # RotationOrder=0 (eEulerXYZ) and RotationActive=1
                # explicitly tell Blender how to read Lcl Rotation —
                # absent these the importer's transform-formula
                # defaults can compound PreRotation/PostRotation in
                # ways that desync M_pose_frame0 from TransformLink.
                def bone_model(b2, bn=bone):
                    W(b2, "Version", [232])
                    # Match Blender's own exporter — these four are not
                    # documented as required by the FBX SDK but appear
                    # in every Blender-exported FBX and seem to be a
                    # signal Blender's importer uses to recognize a
                    # well-formed Model node. Without them, the importer
                    # falls back to defaults that ignore Lcl T/R/S.
                    W(b2, "MultiLayer", [0])
                    W(b2, "MultiTake", [0])
                    W(b2, "Shading", [True])
                    W(b2, "Culling", ["CullingOff"])
                    def props(b3, _bn=bn):
                        local = local_bind_by_idx.get(_bn.index)
                        if local and len(local) == 16:
                            tx, ty, tz, rx, ry, rz, sx, sy, sz = \
                                _lcl_from_bind_matrix(local, scale)
                        else:
                            tx = float(_bn.position[0] * scale)
                            ty = float(_bn.position[1] * scale)
                            tz = float(_bn.position[2] * scale)
                            rx = ry = rz = 0.0
                            sx = sy = sz = 1.0
                        # ── BLENDER'S OWN-EXPORTER CONVENTION ──
                        # Direct Lcl T/R/S, no PreRotation. This matches
                        # io_scene_fbx/export_fbx_bin.py so the importer
                        # round-trips it without surprises.
                        #
                        # World transform formula in FBX:
                        #   World = T × Roff × Rp × Rpre × R × Rpost⁻¹ × Rp⁻¹
                        #         × Soff × Sp × S × Sp⁻¹
                        # With our values: T = bind_t, Rpre = identity,
                        # R = bind_r, Rpost = identity, S = bind_s, all
                        # pivots/offsets = identity → World = T × R × S
                        # = local bind matrix. Compounded up the parent
                        # chain → world bind matrix = TransformLink. ✓
                        #
                        # The previous Maya-rig convention (PreRotation =
                        # bind_r, Lcl R = 0) is mathematically equivalent
                        # but Blender's importer has documented quirks
                        # applying PreRotation to LimbNodes — bones get
                        # auto-oriented by the importer based on their
                        # children, which can fight PreRotation values
                        # and produce a different rest pose than
                        # TransformLink.  Direct Lcl R sidesteps this:
                        # the importer reads our R verbatim into the
                        # pose-bone matrix at frame 0.
                        W(b3, "P", ["InheritType",   "enum", "",       "", 1])
                        W(b3, "P", ["RotationOrder", "enum", "",       "", 0])
                        W(b3, "P", ["RotationActive", "bool", "",      "", 1])
                        W(b3, "P", ["Size", "double", "Number", "", 1.0])
                        W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A",
                                    tx, ty, tz])
                        # Direct Lcl R (NOT PreRotation). PreRotation = 0.
                        W(b3, "P", ["Lcl Rotation",    "Lcl Rotation",    "", "A",
                                    rx, ry, rz])
                        W(b3, "P", ["Lcl Scaling",     "Lcl Scaling",     "", "A",
                                    sx, sy, sz])
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
              Transform     = mesh world matrix at bind time.  The mesh sits
                              at the world origin, so this is always identity.
              TransformLink = bone world matrix at bind time, AXIS-CONVERTED
                              to Z-up to match every other piece of geometry
                              in this FBX (which we also pre-convert to
                              Z-up).  If we left this in Y-up while the bone
                              positions were converted to Z-up by Blender,
                              the cluster math would mix coord systems and
                              every vertex would drift along (zup_pos − yup_pos)
                              — exactly the explosion the user was seeing.
            """
            transform = list(identity_matrix)
            if getattr(bone, "bind_matrix", None) and len(bone.bind_matrix) == 16:
                link = _yup_to_zup_mat4(
                    [float(v) for v in bone.bind_matrix])
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

        # BindPose — tells Blender each node's world matrix at bind time.
        # Must include EVERY skinned node, including the mesh Model nodes
        # (FBX SDK invariant). If we omit the mesh entries, some FBX
        # importers — Blender 4.x specifically — refuse to apply the
        # bind pose at all and fall back to Lcl-derived rest pose, which
        # for compound-rotation bones produces the spike-shard explosion.
        if skeleton and skeleton.bones and not skip_skin:
            identity_4x4 = [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ]
            bones_with_link = [(bone, _bone_bind_matrices(bone)[1])
                               for bone in skeleton.bones]
            # Mesh Model nodes get identity bind (mesh is at world origin).
            mesh_pose_entries = [(model_ids[idx], list(identity_4x4))
                                 for idx in range(len(mesh.submeshes))]
            total_pose_nodes = len(bones_with_link) + len(mesh_pose_entries)

            def pose_body(b2, bwl=bones_with_link, meshes=mesh_pose_entries):
                W(b2, "Type", ["BindPose"])
                W(b2, "Version", [100])
                W(b2, "NbPoseNodes", [total_pose_nodes])
                # Mesh nodes first (FBX SDK convention).
                for mid, mat in meshes:
                    def pn_mesh(b3, bid=mid, m=mat):
                        W(b3, "Node", [bid])
                        W(b3, "Matrix", [m])
                    W(b2, "PoseNode", children=[pn_mesh])
                # Then every bone Model.
                for _bone, _link in bwl:
                    def pn_bone(b3, bid=bone_model_ids[_bone.index], mat=_link):
                        W(b3, "Node", [bid])
                        W(b3, "Matrix", [mat])
                    W(b2, "PoseNode", children=[pn_bone])
            W(b, "Pose", [pose_id, "BindPose\x00\x01Pose", "BindPose"],
              children=[pose_body])

        # ── ANIMATION OBJECTS (Stack / Layer / CurveNode / Curve) ──
        if has_animation:
            base_anim_name = name or Path(mesh.path).stem

            # AnimationStack
            def stack_node(b2):
                def props(b3):
                    W(b3, "P", ["LocalStart", "KTime", "Time", "",
                                int(frame_ticks[0]) if frame_ticks else 0])
                    W(b3, "P", ["LocalStop", "KTime", "Time", "",
                                int(final_tick)])
                W(b2, "Properties70", children=[props])
            W(b, "AnimationStack",
              [anim_stack_id, f"{base_anim_name}\x00\x01AnimStack", ""],
              children=[stack_node])

            # AnimationLayer
            def layer_node(b2):
                def props(b3):
                    W(b3, "P", ["Weight", "Number", "", "A", 100.0])
                W(b2, "Properties70", children=[props])
            W(b, "AnimationLayer",
              [anim_layer_id, "BaseLayer\x00\x01AnimLayer", ""],
              children=[layer_node])

            # Per-bone CurveNode + 3 Curves (X/Y/Z Euler).
            for bn in skeleton.bones:
                eulers = per_bone_eulers.get(bn.index, [])
                if not eulers:
                    continue

                def cnode_node(b2, _el=eulers):
                    def props(b3, _el2=_el):
                        rx, ry, rz = _el2[0]
                        W(b3, "P", ["d|X", "Number", "", "A", float(rx)])
                        W(b3, "P", ["d|Y", "Number", "", "A", float(ry)])
                        W(b3, "P", ["d|Z", "Number", "", "A", float(rz)])
                    W(b2, "Properties70", children=[props])
                W(b, "AnimationCurveNode",
                  [curve_node_ids[bn.index],
                   f"{bn.name}_R\x00\x01AnimCurveNode", ""],
                  children=[cnode_node])

                for axis_idx, axis_label in enumerate(("X", "Y", "Z")):
                    values = [e[axis_idx] for e in eulers]
                    times = [int(t) for t in frame_ticks[:len(values)]]
                    # Pad/trim values to match times
                    if len(values) < len(times):
                        times = times[:len(values)]

                    def curve_node(b2, _times=times, _values=values):
                        W(b2, "Default", [float(_values[0]) if _values else 0.0])
                        W(b2, "KeyVer", [4008])
                        W(b2, "KeyTime", [list(_times)])
                        W(b2, "KeyValueFloat",
                          [[float(v) for v in _values]])
                        W(b2, "KeyAttrFlags", [[0x2008]])  # cubic auto
                        W(b2, "KeyAttrDataFloat", [[0.0, 0.0, 0.0, 0.0]])
                        W(b2, "KeyAttrRefCount", [[len(_times)]])
                    cid = curve_ids[bn.index][axis_idx]
                    W(b, "AnimationCurve",
                      [cid,
                       f"{bn.name}_R_{axis_label}\x00\x01AnimCurve", ""],
                      children=[curve_node])

    W(buf, "Objects", children=[objects])

    # Connections
    def connections(b):
        for idx in range(len(mesh.submeshes)):
            W(b, "C", ["OO", model_ids[idx], _FbxId(0)])
            W(b, "C", ["OO", mesh_ids[idx], model_ids[idx]])
            W(b, "C", ["OO", mat_ids[idx], model_ids[idx]])

        # BindPose → root
        if skeleton and skeleton.bones and not skip_skin:
            W(b, "C", ["OO", pose_id, _FbxId(0)])

        # Bone connections — REAL parent-child hierarchy.
        # Blender's importer compounds parent Lcl TRS onto child Lcl TRS to
        # build the world rest pose at frame 0. We supply parent-relative
        # LOCAL Lcl above, so the compounded result equals the original
        # world bind matrix == TransformLink == BindPose Matrix → skin
        # deformer sees identity → no deformation.
        if skeleton and skeleton.bones and not skip_skin:
            # Armature Null → scene root (animation only). Provides the
            # single armature object Blender attaches Action data to.
            if armature_null_id is not None:
                W(b, "C", ["OO", armature_null_id, _FbxId(0)])
            for bone in skeleton.bones:
                # NodeAttribute → Bone Model
                W(b, "C", ["OO", bone_attr_ids[bone.index], bone_model_ids[bone.index]])
                # Real parent-child hierarchy.  Lcl TRS is parent-
                # relative, Blender compounds the chain to give world
                # bind = TransformLink. Verified math is exact (fp32
                # floor) with intrinsic XYZ Euler + sign-aware gimbal
                # formula. The armature now displays as a connected
                # skeleton, not a 1m spike forest.
                if (bone.parent_index is not None
                        and bone.parent_index >= 0
                        and bone.parent_index in bone_model_ids):
                    W(b, "C", ["OO", bone_model_ids[bone.index],
                               bone_model_ids[bone.parent_index]])
                else:
                    # Root bone: parent depends on whether we have an
                    # armature Null. With animation, Blender requires
                    # a single Null parent for all root bones to wire
                    # the Action; without animation, scene root is fine.
                    parent = (armature_null_id if armature_null_id
                              is not None else _FbxId(0))
                    W(b, "C", ["OO", bone_model_ids[bone.index], parent])

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

        # ── ANIMATION CONNECTIONS ──
        # AnimationLayer -> AnimationStack
        # AnimationStack -> scene root (so importer treats it as active)
        # CurveNode -> AnimationLayer
        # CurveNode -> Bone Model.Lcl Rotation property (OP)
        # AnimationCurve -> CurveNode.d|X/Y/Z properties (OP)
        if has_animation:
            W(b, "C", ["OO", anim_layer_id, anim_stack_id])
            W(b, "C", ["OO", anim_stack_id, _FbxId(0)])
            for bn in skeleton.bones:
                if bn.index not in curve_node_ids:
                    continue
                if not per_bone_eulers.get(bn.index):
                    continue
                W(b, "C", ["OO", curve_node_ids[bn.index], anim_layer_id])
                W(b, "C", ["OP", curve_node_ids[bn.index],
                           bone_model_ids[bn.index], "Lcl Rotation"])
                for axis_idx, axis_label in enumerate(("X", "Y", "Z")):
                    cid = curve_ids[bn.index][axis_idx]
                    W(b, "C", ["OP", cid, curve_node_ids[bn.index],
                               f"d|{axis_label}"])

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

    D("")
    D("=" * 70)
    D("FBX EXPORT SUMMARY")
    D("=" * 70)
    D(f"Output FBX size      : {len(buf.getvalue()):,} bytes")
    D(f"Submeshes            : {len(mesh.submeshes)}")
    D(f"Total vertices       : {mesh.total_vertices:,}")
    D(f"Total faces          : {mesh.total_faces:,}")
    D(f"Bones written        : {len(skeleton.bones) if (skeleton and not skip_skin) else 0}")
    D(f"Skin deformers       : {sum(1 for s in skin_ids if s is not None)}")
    D(f"Total cluster entries: {sum(len(c) for c in cluster_ids)}")
    if skeleton and skeleton.bones and not skip_skin:
        D(f"BindPose nodes       : {len(mesh.submeshes) + len(skeleton.bones)}"
          f" ({len(mesh.submeshes)} mesh + {len(skeleton.bones)} bones)")
    D("=" * 70)
    D("Convention used      : Blender direct-Lcl-R (matches Blender's own exporter)")
    D("                       Lcl T = bind translation (parent-local)")
    D("                       Lcl R = bind rotation Euler XYZ (parent-local)")
    D("                       Lcl S = bind scale")
    D("                       PreRotation = (0,0,0)")
    D("                       InheritType=1 (RSrs, scale doesn't propagate)")
    D("=" * 70)

    # Write the binary FBX
    with open(fbx_path, "wb") as f:
        f.write(buf.getvalue())

    # Write the verbose debug companion file
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write("\n".join(debug_lines))
        logger.info("Debug log written: %s", debug_path)
    except OSError as e:
        logger.warning("Could not write debug log %s: %s", debug_path, e)

    # Write the v2 sidecar with source_vertex_map + filtered_vertices.
    # The importer needs this to round-trip the spike-filter losslessly:
    # without source_vertex_map it can't tell which PAC vertex slot a
    # given FBX vertex came from, and without filtered_vertices it has
    # no way to reinsert the dropped engine helper geometry.
    sidecar_path = _write_cfmeta_sidecar_v2(mesh, fbx_path, submesh_views)
    if sidecar_path:
        logger.info("Sidecar written: %s", sidecar_path)

    bone_count = len(skeleton.bones) if skeleton else 0
    total_filtered = sum(v["spikes_count"] for v in submesh_views)
    logger.info(
        "Exported FBX+Skeleton: %s (%d verts kept of %d, %d faces, %d bones, "
        "%d filtered spike verts in sidecar)",
        fbx_path,
        sum(len(v["verts"]) for v in submesh_views),
        mesh.total_vertices,
        sum(len(v["faces"]) for v in submesh_views),
        bone_count,
        total_filtered,
    )
    return fbx_path
