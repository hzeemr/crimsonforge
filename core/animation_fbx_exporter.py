"""FBX animation export for parsed PAA files.

Takes a ``ParsedAnimation`` (from ``core.animation_parser``) plus the
matching :class:`Skeleton` and writes an FBX file carrying:

  * FBXHeaderExtension / GlobalSettings
  * one LimbNode per bone (so the armature imports cleanly even if the
    caller didn't ship a paired mesh)
  * an AnimationStack containing one AnimationLayer
  * one AnimationCurveNode per bone with three AnimationCurves (X / Y
    / Z rotation channels)

Downstream use: Blender / Maya / 3ds Max import the result, apply it
to the existing rigged character, re-export to FBX — and we ship an
inverse "FBX keyframes -> PAA" converter in a later commit to round-
trip. For now, read-only PAA extraction into FBX is enough to unblock
Yoo's "tried to vibecode an export/import to fbx on paa too, but gave
me garbled data" complaint from the community discord.

Coordinate / rotation convention
--------------------------------

FBX stores bone rotation as Euler degrees in a configurable rotation
order. We emit eEulerXYZ (the default and the only one Blender's FBX
importer reliably handles). PAA rotations come in as quaternions
``(x, y, z, w)``; we convert to XYZ Euler via the standard
atan2/asin formulas before packing the keyframe values.

Unit scale
----------

Pearl Abyss stores bone positions in **meters** (e.g. Bip01 = 0.97m
human pelvis height). FBX's ``UnitScaleFactor`` defaults to 1.0,
meaning "1 FBX unit = 1 centimeter". Blender's FBX importer honours
that and scales everything by 0.01 on import.

If we naively wrote bone positions in meters, the whole skeleton
would collapse into a ~1 cm "spiky star" cluster at origin — which
is exactly what Yoo reported. The fix is to multiply all positions
by ``POSITION_SCALE`` (100) before writing, so the FBX carries
centimetre values and round-trips back to metres in Blender.

Time units
----------

FBX uses KTime — int64 ticks at a rate of 46_186_158_000 per second.
Our exporter emits one keyframe per PAA frame at the animation's
declared duration. Callers that want a different playback rate can
pass ``fps=`` and we recompute the tick values.
"""

from __future__ import annotations

import io
import math
import os
import struct
from pathlib import Path

from core.animation_parser import ParsedAnimation
from core.mesh_exporter import _FbxId, _fbx_node  # reuse the 7.4 binary writer
from core.skeleton_parser import Skeleton
from utils.logger import get_logger

logger = get_logger("core.animation_fbx_exporter")


KTIME_TICKS_PER_SECOND = 46_186_158_000

# FBX default UnitScaleFactor=1.0 means "1 unit = 1 centimetre". Pearl
# Abyss stores bone positions in metres, so we scale up by 100 so that
# Blender / Maya / 3ds Max (which all scale cm -> scene units on import)
# recover the correct human-scale skeleton rather than a ~1 cm cluster
# collapsed at the origin.
POSITION_SCALE = 100.0

# Radius of the placeholder cube emitted at each bone (in centimetres,
# matching the post-POSITION_SCALE coordinate system).
#
# v1.21.0 used 2.5 cm (small joint markers with visible gaps between
# adjacent cubes + prisms). v1.22.0 enlarges to 3.5 cm so the joint
# cubes OVERLAP the limb prism ends — combined with the split-weight
# limb (v1.21.1) the eye reads a continuous surface instead of a
# string of disconnected cubes. This is the practical resolution of
# Known Issue #4 from the v1.18.0 FBX-animation audit.
PLACEHOLDER_VERTEX_RADIUS = 3.5

# Radius of the limb cylinder emitted between a child bone and its
# parent. Matched to the joint cube so limbs "seat" inside the
# cube volume, eliminating the visible gap at joints.
PLACEHOLDER_LIMB_RADIUS = 3.0


def _make_unit_cube_verts(radius: float) -> list[tuple[float, float, float]]:
    """Return 8 vertices of a cube centred at origin with half-side ``radius``."""
    r = radius
    return [
        (-r, -r, -r), ( r, -r, -r), ( r,  r, -r), (-r,  r, -r),
        (-r, -r,  r), ( r, -r,  r), ( r,  r,  r), (-r,  r,  r),
    ]


# 12-triangle cube with per-face winding pointing OUTWARD. Each tuple is
# (a, b, c) into the 8-vertex cube returned by _make_unit_cube_verts.
_CUBE_TRIS = (
    # -Z face
    (0, 2, 1), (0, 3, 2),
    # +Z face
    (4, 5, 6), (4, 6, 7),
    # -Y face
    (0, 1, 5), (0, 5, 4),
    # +Y face
    (3, 6, 2), (3, 7, 6),
    # -X face
    (0, 4, 7), (0, 7, 3),
    # +X face
    (1, 2, 6), (1, 6, 5),
)


def _bone_world_position(bone, position_scale: float) -> tuple[float, float, float] | None:
    """Return the world-space bind translation of a bone.

    PAB stores ``bind_matrix`` as a row-major 4x4 with the translation
    in row 3 (indices 12, 13, 14). When the bind matrix is missing or
    degenerate, returns ``None`` so callers can skip the bone.
    """
    bm = getattr(bone, "bind_matrix", None)
    if not bm or len(bm) != 16:
        return None
    tx, ty, tz = bm[12], bm[13], bm[14]
    # The matrix's bottom-right element should be 1.0 in a well-formed
    # rigid transform — anything else means we're misreading the layout
    # and the position is garbage.
    if abs(bm[15] - 1.0) > 0.01:
        return None
    return (tx * position_scale, ty * position_scale, tz * position_scale)


def _make_limb_quad_verts(
    head_pos: tuple[float, float, float],
    tail_pos: tuple[float, float, float],
    radius: float,
) -> list[tuple[float, float, float]]:
    """Build 8 vertices for a square-prism limb from head to tail.

    Returns 4 vertices around the head ring + 4 around the tail ring,
    forming a thin elongated box. The prism axis follows the
    head -> tail direction; perpendicular axes are picked from a
    world-up reference (Z) when possible, falling back to X otherwise
    to handle bones that already point along Z.
    """
    import math
    hx, hy, hz = head_pos
    tx, ty, tz = tail_pos
    dx, dy, dz = tx - hx, ty - hy, tz - hz
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < 1e-6:
        return []
    # Forward axis (along the bone)
    fx, fy, fz = dx / length, dy / length, dz / length
    # Pick a reference up axis that isn't parallel to the forward axis
    if abs(fz) < 0.9:
        ux, uy, uz = 0.0, 0.0, 1.0
    else:
        ux, uy, uz = 1.0, 0.0, 0.0
    # Right axis = forward x up, then re-orthogonalise up
    rx = fy * uz - fz * uy
    ry = fz * ux - fx * uz
    rz = fx * uy - fy * ux
    rl = math.sqrt(rx * rx + ry * ry + rz * rz)
    if rl < 1e-6:
        return []
    rx, ry, rz = rx / rl, ry / rl, rz / rl
    ux = ry * fz - rz * fy
    uy = rz * fx - rx * fz
    uz = rx * fy - ry * fx
    # 4 vertices at head, 4 at tail — forming a square prism
    r = radius
    verts = []
    for ring_pos in (head_pos, tail_pos):
        cx, cy, cz = ring_pos
        for sx, sy in ((+1, +1), (-1, +1), (-1, -1), (+1, -1)):
            verts.append((
                cx + sx * r * rx + sy * r * ux,
                cy + sx * r * ry + sy * r * uy,
                cz + sx * r * rz + sy * r * uz,
            ))
    return verts


# Triangulation of the 8-vertex limb prism. Vertices 0-3 are the head
# ring (CCW looking from tail toward head), 4-7 are the tail ring in
# the same order. Faces are oriented OUTWARD.
_LIMB_TRIS = (
    # Head cap (vertices 0-3, looking from outside down the limb)
    (0, 2, 1), (0, 3, 2),
    # Tail cap (vertices 4-7)
    (4, 5, 6), (4, 6, 7),
    # 4 side quads, each as 2 triangles
    (0, 1, 5), (0, 5, 4),
    (1, 2, 6), (1, 6, 5),
    (2, 3, 7), (2, 7, 6),
    (3, 0, 4), (3, 4, 7),
)


def _generate_placeholder_skinned_mesh(skeleton) -> tuple[
    list[float],                          # flat vertex floats (xyz xyz ...)
    list[int],                            # polygon vertex indices (with last-of-face XOR'd)
    dict[int, list[tuple[int, float]]],   # per-bone (vertex_index, weight) pairs
]:
    """Build a skinned placeholder mesh.

    For each bone:
      * a small cube at the bone's WORLD bind position (joint marker)
      * a thin prism between this bone and its parent (limb segment)

    Both are 100% weighted to the bone they belong to (limb prism
    vertices to the CHILD bone — this matches how Blender displays
    bones, where the bone "owns" the volume between its head and the
    parent's head). When animations play, the joint moves and the
    limb follows naturally.

    Without this mesh, an FBX preview shows 56 abstract bone widgets
    that all look identical at any single frame ("all paa same shape"
    bug). With it, you see a recognisable humanoid silhouette that
    visibly differs per animation.
    """
    vertex_floats: list[float] = []
    polygon_indices: list[int] = []
    bone_weights: dict[int, list[tuple[int, float]]] = {}

    # Pre-compute world positions for every bone so we can build limbs.
    world_positions: dict[int, tuple[float, float, float]] = {}
    for bone in skeleton.bones:
        wp = _bone_world_position(bone, POSITION_SCALE)
        if wp is not None:
            world_positions[bone.index] = wp

    cube_verts = _make_unit_cube_verts(PLACEHOLDER_VERTEX_RADIUS)

    def _emit(verts, tris, owner_bone_idx):
        v_offset = len(vertex_floats) // 3
        for vx, vy, vz in verts:
            vertex_floats.extend([vx, vy, vz])
        bone_weights.setdefault(owner_bone_idx, []).extend(
            (v_offset + i, 1.0) for i in range(len(verts))
        )
        for a, b, c in tris:
            polygon_indices.extend([a + v_offset, b + v_offset, (c + v_offset) ^ -1])

    def _emit_limb_split(verts, tris, parent_idx, child_idx):
        """Emit a limb prism where the 4 vertices at the parent end are
        weighted to the PARENT bone and the 4 at the child end are
        weighted to the CHILD. Produces a continuous deformation
        across the joint — parent and child rotations blend smoothly
        along the prism's length.
        """
        v_offset = len(vertex_floats) // 3
        for vx, vy, vz in verts:
            vertex_floats.extend([vx, vy, vz])
        # Per _make_limb_quad_verts contract: verts 0..3 are the HEAD
        # ring (at parent_wp), 4..7 are the TAIL ring (at child_wp).
        bone_weights.setdefault(parent_idx, []).extend(
            (v_offset + i, 1.0) for i in range(4)
        )
        bone_weights.setdefault(child_idx, []).extend(
            (v_offset + i, 1.0) for i in range(4, 8)
        )
        for a, b, c in tris:
            polygon_indices.extend([a + v_offset, b + v_offset, (c + v_offset) ^ -1])

    for bone in skeleton.bones:
        wp = world_positions.get(bone.index)
        if wp is None:
            continue
        wx, wy, wz = wp
        # Joint cube at this bone's bind position, weighted to this bone
        cube_translated = [(wx + vx, wy + vy, wz + vz) for vx, vy, vz in cube_verts]
        _emit(cube_translated, _CUBE_TRIS, bone.index)

        # Limb prism between this bone and its parent (if it has one
        # and the parent's position is non-degenerate). SPLIT weighting:
        # 4 parent-end vertices go to the parent bone, 4 child-end
        # vertices go to this bone. When the parent rotates, the
        # parent end of the prism rotates with it; when the child
        # rotates, the other end tracks. The geometry between
        # stretches/bends smoothly, giving a continuous surface across
        # the joint instead of disconnected prisms sliding past each
        # other (the visual gap was Known Issue #4 in v1.18.0).
        if bone.parent_index >= 0 and bone.parent_index in world_positions:
            parent_wp = world_positions[bone.parent_index]
            dx = wp[0] - parent_wp[0]
            dy = wp[1] - parent_wp[1]
            dz = wp[2] - parent_wp[2]
            if dx * dx + dy * dy + dz * dz > 1.0:  # > 1 cm gap
                limb_verts = _make_limb_quad_verts(
                    parent_wp, wp, PLACEHOLDER_LIMB_RADIUS,
                )
                if limb_verts:
                    _emit_limb_split(
                        limb_verts, _LIMB_TRIS,
                        parent_idx=bone.parent_index,
                        child_idx=bone.index,
                    )

    return vertex_floats, polygon_indices, bone_weights


def _matrix_pab_row_to_fbx_column(row_major: tuple) -> list[float]:
    """Transpose a row-major 4x4 (PAB layout) into FBX's column-major.

    FBX cluster ``TransformLink`` and ``Transform`` matrices are
    column-major: the first 4 floats are column 0, the next 4 column 1,
    etc., with translation in indices 12-15. PAB stores the same data
    transposed (rows of basis vectors with translation in the bottom
    row).
    """
    if len(row_major) != 16:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
    out = [0.0] * 16
    for r in range(4):
        for c in range(4):
            # transpose: out[c, r] = row_major[r, c]
            out[c * 4 + r] = float(row_major[r * 4 + c])
    return out


def _invert_4x4(m: list[float]) -> list[float]:
    """Invert a 4x4 column-major matrix. Falls back to identity on
    singular input — callers should accept that as "no bind override".
    """
    # Column-major: m[col*4 + row]
    def el(c, r):
        return m[c * 4 + r]

    # Use cofactor expansion via NumPy-free implementation. For our use
    # case the matrices are rigid transforms so the inverse always exists.
    # We adapt the 4x4 inverse from the GLU library reference implementation.
    inv = [0.0] * 16
    inv[0] = (el(1, 1) * el(2, 2) * el(3, 3) - el(1, 1) * el(2, 3) * el(3, 2)
              - el(2, 1) * el(1, 2) * el(3, 3) + el(2, 1) * el(1, 3) * el(3, 2)
              + el(3, 1) * el(1, 2) * el(2, 3) - el(3, 1) * el(1, 3) * el(2, 2))
    inv[4] = (-el(1, 0) * el(2, 2) * el(3, 3) + el(1, 0) * el(2, 3) * el(3, 2)
              + el(2, 0) * el(1, 2) * el(3, 3) - el(2, 0) * el(1, 3) * el(3, 2)
              - el(3, 0) * el(1, 2) * el(2, 3) + el(3, 0) * el(1, 3) * el(2, 2))
    inv[8] = (el(1, 0) * el(2, 1) * el(3, 3) - el(1, 0) * el(2, 3) * el(3, 1)
              - el(2, 0) * el(1, 1) * el(3, 3) + el(2, 0) * el(1, 3) * el(3, 1)
              + el(3, 0) * el(1, 1) * el(2, 3) - el(3, 0) * el(1, 3) * el(2, 1))
    inv[12] = (-el(1, 0) * el(2, 1) * el(3, 2) + el(1, 0) * el(2, 2) * el(3, 1)
               + el(2, 0) * el(1, 1) * el(3, 2) - el(2, 0) * el(1, 2) * el(3, 1)
               - el(3, 0) * el(1, 1) * el(2, 2) + el(3, 0) * el(1, 2) * el(2, 1))
    inv[1] = (-el(0, 1) * el(2, 2) * el(3, 3) + el(0, 1) * el(2, 3) * el(3, 2)
              + el(2, 1) * el(0, 2) * el(3, 3) - el(2, 1) * el(0, 3) * el(3, 2)
              - el(3, 1) * el(0, 2) * el(2, 3) + el(3, 1) * el(0, 3) * el(2, 2))
    inv[5] = (el(0, 0) * el(2, 2) * el(3, 3) - el(0, 0) * el(2, 3) * el(3, 2)
              - el(2, 0) * el(0, 2) * el(3, 3) + el(2, 0) * el(0, 3) * el(3, 2)
              + el(3, 0) * el(0, 2) * el(2, 3) - el(3, 0) * el(0, 3) * el(2, 2))
    inv[9] = (-el(0, 0) * el(2, 1) * el(3, 3) + el(0, 0) * el(2, 3) * el(3, 1)
              + el(2, 0) * el(0, 1) * el(3, 3) - el(2, 0) * el(0, 3) * el(3, 1)
              - el(3, 0) * el(0, 1) * el(2, 3) + el(3, 0) * el(0, 3) * el(2, 1))
    inv[13] = (el(0, 0) * el(2, 1) * el(3, 2) - el(0, 0) * el(2, 2) * el(3, 1)
               - el(2, 0) * el(0, 1) * el(3, 2) + el(2, 0) * el(0, 2) * el(3, 1)
               + el(3, 0) * el(0, 1) * el(2, 2) - el(3, 0) * el(0, 2) * el(2, 1))
    inv[2] = (el(0, 1) * el(1, 2) * el(3, 3) - el(0, 1) * el(1, 3) * el(3, 2)
              - el(1, 1) * el(0, 2) * el(3, 3) + el(1, 1) * el(0, 3) * el(3, 2)
              + el(3, 1) * el(0, 2) * el(1, 3) - el(3, 1) * el(0, 3) * el(1, 2))
    inv[6] = (-el(0, 0) * el(1, 2) * el(3, 3) + el(0, 0) * el(1, 3) * el(3, 2)
              + el(1, 0) * el(0, 2) * el(3, 3) - el(1, 0) * el(0, 3) * el(3, 2)
              - el(3, 0) * el(0, 2) * el(1, 3) + el(3, 0) * el(0, 3) * el(1, 2))
    inv[10] = (el(0, 0) * el(1, 1) * el(3, 3) - el(0, 0) * el(1, 3) * el(3, 1)
               - el(1, 0) * el(0, 1) * el(3, 3) + el(1, 0) * el(0, 3) * el(3, 1)
               + el(3, 0) * el(0, 1) * el(1, 3) - el(3, 0) * el(0, 3) * el(1, 1))
    inv[14] = (-el(0, 0) * el(1, 1) * el(3, 2) + el(0, 0) * el(1, 2) * el(3, 1)
               + el(1, 0) * el(0, 1) * el(3, 2) - el(1, 0) * el(0, 2) * el(3, 1)
               - el(3, 0) * el(0, 1) * el(1, 2) + el(3, 0) * el(0, 2) * el(1, 1))
    inv[3] = (-el(0, 1) * el(1, 2) * el(2, 3) + el(0, 1) * el(1, 3) * el(2, 2)
              + el(1, 1) * el(0, 2) * el(2, 3) - el(1, 1) * el(0, 3) * el(2, 2)
              - el(2, 1) * el(0, 2) * el(1, 3) + el(2, 1) * el(0, 3) * el(1, 2))
    inv[7] = (el(0, 0) * el(1, 2) * el(2, 3) - el(0, 0) * el(1, 3) * el(2, 2)
              - el(1, 0) * el(0, 2) * el(2, 3) + el(1, 0) * el(0, 3) * el(2, 2)
              + el(2, 0) * el(0, 2) * el(1, 3) - el(2, 0) * el(0, 3) * el(1, 2))
    inv[11] = (-el(0, 0) * el(1, 1) * el(2, 3) + el(0, 0) * el(1, 3) * el(2, 1)
               + el(1, 0) * el(0, 1) * el(2, 3) - el(1, 0) * el(0, 3) * el(2, 1)
               - el(2, 0) * el(0, 1) * el(1, 3) + el(2, 0) * el(0, 3) * el(1, 1))
    inv[15] = (el(0, 0) * el(1, 1) * el(2, 2) - el(0, 0) * el(1, 2) * el(2, 1)
               - el(1, 0) * el(0, 1) * el(2, 2) + el(1, 0) * el(0, 2) * el(2, 1)
               + el(2, 0) * el(0, 1) * el(1, 2) - el(2, 0) * el(0, 2) * el(1, 1))

    det = (el(0, 0) * inv[0] + el(0, 1) * inv[4]
           + el(0, 2) * inv[8] + el(0, 3) * inv[12])
    if abs(det) < 1e-12:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
    inv_det = 1.0 / det
    return [v * inv_det for v in inv]


def _quat_xyzw_to_euler_xyz_degrees(
    x: float, y: float, z: float, w: float,
) -> tuple[float, float, float]:
    """Convert a unit quaternion (xyzw) to XYZ-intrinsic Euler degrees.

    Handles the ±90° pitch gimbal-lock case explicitly. In the
    non-singular case, the standard roll-pitch-yaw decomposition
    applies. At the poles (|sinp| ~ 1), cosr_cosp drifts to a tiny
    negative value due to floating-point rounding and ``atan2(0, eps-)``
    returns π instead of 0 — we catch that and assign all Z rotation
    to the yaw channel with roll=0.
    """
    sinp = 2.0 * (w * y - z * x)
    sinp_clamped = max(-1.0, min(1.0, sinp))

    # Gimbal-lock tolerance: when the sine is this close to ±1 we
    # drop roll entirely and express the composed Z rotation on yaw.
    GIMBAL_EPSILON = 1e-4
    if abs(sinp_clamped) > 1.0 - GIMBAL_EPSILON:
        pitch = math.copysign(math.pi / 2.0, sinp_clamped)
        roll = 0.0
        # In this degenerate orientation the X-then-Z rotation compose
        # into a single angle; recover it via atan2 of the remaining
        # off-diagonal quaternion products. The atan2 denominator can
        # drift to a tiny negative value due to floating-point rounding
        # — if both arguments are effectively zero the geometry is
        # "pure pitch" and yaw should be 0 rather than ±π.
        num = -2.0 * (x * y - w * z)
        den = 1.0 - 2.0 * (y * y + z * z)
        if abs(num) < 1e-6 and abs(den) < 1e-6:
            yaw = 0.0
        else:
            yaw = math.atan2(num, den)
        return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)

    # Roll (X rotation).
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    pitch = math.asin(sinp_clamped)

    # Yaw (Z rotation).
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def _frame_ticks(frame_index: int, total_frames: int, duration: float, fps: float) -> int:
    """Return the KTime tick value for a given keyframe.

    Prefers the animation's declared duration when plausible so the
    playback speed matches what the game uses; falls back to ``fps``
    when the PAA didn't carry a duration.
    """
    if duration > 0 and total_frames > 1:
        seconds = (frame_index / (total_frames - 1)) * duration
    else:
        seconds = frame_index / max(fps, 1.0)
    return int(seconds * KTIME_TICKS_PER_SECOND)


def _ensure_euler_continuity(
    current: tuple[float, float, float],
    previous: tuple[float, float, float] | None,
) -> tuple[float, float, float]:
    """Unwrap Euler angles so adjacent keyframes don't jump 360°.

    Quaternion -> Euler can produce wildly different degree values for
    essentially identical rotations — the atan2 branch flips at ±180°.
    If the previous keyframe exists, we add multiples of 360 until the
    angular delta is in [-180, 180].
    """
    if previous is None:
        return current
    fixed = list(current)
    for i in range(3):
        while fixed[i] - previous[i] > 180.0:
            fixed[i] -= 360.0
        while fixed[i] - previous[i] < -180.0:
            fixed[i] += 360.0
    return fixed[0], fixed[1], fixed[2]


def _canonicalize_quaternion_sign(
    q: tuple[float, float, float, float],
    previous: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    """Flip ``q`` to its antipode if that brings it closer to ``previous``.

    The unit quaternions ``q`` and ``-q`` encode the exact same rotation
    (the double cover of SO(3) by S³). PAA ships raw int16-quantised
    components with no sign-canonicalisation guarantee — the exporter
    we're feeding the data into (Pearl Abyss's runtime) doesn't care
    because it just slerps on the unit 3-sphere. But FBX stores Euler
    degrees, and the quat→Euler decomposition is *very* different for
    ``q`` vs ``-q`` near gimbal lock:

      * ``(sin45, 0, 0, cos45)``   → roll = +90°
      * ``(-sin45, 0, 0, -cos45)`` → roll = +90° as well in the naïve
         case — but in a mixed-axis rotation near pitch = ±90° the two
         representations alight on different Euler branches.

    If the source stream alternates between ``q`` and ``-q`` frame to
    frame — and it does: dot products of -0.132, -0.501, -0.157 show
    up across the first 30 frames of a typical roofclimb animation —
    the Euler output flips with it, and
    :func:`_ensure_euler_continuity` responds by adding increments of
    360° to "catch up". Those increments accumulate: we measured
    ``Bip01_R_X`` swinging from -738° to +7560° (delta 8298°) over a
    2741-keyframe climb that in reality never turns more than ±180°.

    The fix is to guarantee quaternion-stream continuity BEFORE Euler
    decomposition, by negating ``q`` when ``dot(q_prev, q) < 0``. The
    result is geometrically identical but produces a continuous Euler
    stream that the 360° unwrap can handle correctly.
    """
    if previous is None:
        return q
    qx, qy, qz, qw = q
    px, py, pz, pw = previous
    if qx * px + qy * py + qz * pz + qw * pw < 0.0:
        return -qx, -qy, -qz, -qw
    return q


def export_animation_fbx(
    animation: ParsedAnimation,
    skeleton: Skeleton,
    output_dir: str,
    name: str = "",
    fps: float = 30.0,
    metabin_data: bytes | None = None,
    bone_map=None,
) -> str:
    """Write an FBX file containing the skeleton + animation curves.

    Args:
        animation: parsed PAA with per-frame per-bone quaternions.
        skeleton: parsed PAB whose ``bones`` names the channels.
            If ``len(skeleton.bones) < animation.bone_count`` we emit
            placeholder bones (``Bone_N``) for the overflow.
        output_dir: directory to write the FBX into.
        name: output filename stem (defaults to the PAA's file stem).
        fps: fallback frame rate when the PAA has no declared duration.
        metabin_data: optional raw bytes of the ``.paa_metabin`` sidecar.
            When supplied, the heuristic metabin parser is used to
            refine the animation duration.
        bone_map: optional :class:`core.paa_bone_mapping.BoneMap`. When
            provided, PAA track ``i`` drives PAB bone ``bone_map[i]``
            instead of the default 1:1-by-index mapping. The
            canonical-rig problem documented in v1.18.0's Known Issues
            means the default 1:1 is wrong for 30+ bones on phm_01;
            the UI's "Edit bone map" dialog builds and persists these
            overrides per-rig.

    Returns the absolute output path.
    """
    os.makedirs(output_dir, exist_ok=True)
    base = name or Path(animation.path).stem or "animation"
    fbx_path = os.path.join(output_dir, f"{base}.fbx")

    # If we got a bone map, remap the animation's keyframes so track N
    # drives PAB bone bone_map[N] on export. We do this by
    # REINDEXING each frame's bone_rotations: build a dense list
    # sized to skeleton.bones where slot j gets track i's rotation
    # iff bone_map.for_track(i) == j.
    if bone_map is not None and animation.keyframes:
        n_pab = len(skeleton.bones) if skeleton else animation.bone_count
        n_track = animation.bone_count
        # Invert: pab_idx -> track_idx (take first if multiple map)
        pab_to_track: dict[int, int] = {}
        for track_idx in range(n_track):
            pab_idx = bone_map.for_track(track_idx)
            if 0 <= pab_idx < n_pab and pab_idx not in pab_to_track:
                pab_to_track[pab_idx] = track_idx

        from core.animation_parser import AnimationKeyframe
        remapped = []
        for kf in animation.keyframes:
            new_rots: list[tuple[float, float, float, float]] = []
            for pab_idx in range(n_pab):
                t_idx = pab_to_track.get(pab_idx, -1)
                if 0 <= t_idx < len(kf.bone_rotations):
                    new_rots.append(kf.bone_rotations[t_idx])
                else:
                    # No track for this PAB bone -> hold bind pose
                    bone = skeleton.bones[pab_idx] if pab_idx < len(skeleton.bones) else None
                    bind = bone.rotation if bone else (0.0, 0.0, 0.0, 1.0)
                    new_rots.append(bind)
            remapped.append(AnimationKeyframe(
                frame_index=kf.frame_index,
                bone_rotations=new_rots,
            ))
        # Rebuild animation with remapped keyframes (don't mutate caller)
        from copy import copy
        animation = copy(animation)
        animation.keyframes = remapped
        animation.bone_count = n_pab

    # Consult the metabin sidecar (if provided) for a better duration
    # estimate. The PAA itself doesn't reliably carry duration; the
    # metabin does, to within a few percent, under our heuristic.
    if metabin_data:
        from core.paa_metabin_parser import parse_metabin
        meta = parse_metabin(metabin_data)
        if meta.valid and meta.duration > 0:
            # Overwrite only when the existing duration is clearly
            # stale (unset or mis-estimated from frame_count / fps).
            if animation.duration <= 0 or abs(animation.duration - meta.duration) > 0.5:
                logger.info(
                    "metabin refines duration: PAA said %.3fs, metabin says %.3fs",
                    animation.duration, meta.duration,
                )
                animation.duration = meta.duration

    buf = io.BytesIO()
    W = _fbx_node

    # Magic header + version.
    buf.write(b"Kaydara FBX Binary  \x00")
    buf.write(b"\x1a\x00")
    buf.write(struct.pack("<I", 7400))

    id_ctr = [3_200_000_000]
    def uid() -> _FbxId:
        id_ctr[0] += 1
        return _FbxId(id_ctr[0])

    def header_ext(b):
        W(b, "FBXHeaderVersion", [1003])
        W(b, "FBXVersion", [7400])
        W(b, "Creator", ["CrimsonForge PAA Animation Exporter"])
    W(buf, "FBXHeaderExtension", children=[header_ext])

    # Document root — Blender's FBX importer looks for this and won't
    # play animation without it.
    doc_root_id = uid()

    # Resolve bone names first so we can compute frame ticks before
    # we emit GlobalSettings (which needs TimeSpan).
    #
    # Bone-count strategy: clamp to the skeleton's bone count when one
    # is available, NOT max(skeleton, animation). PAA files routinely
    # reference 1-20 more "channels" than the matching PAB carries
    # (e.g. roofclimb has 76 PAA channels vs 56 PAB bones — likely
    # gear / weapon attachment bones from a different rig). Without a
    # skeleton position for those extras we'd emit them as Bone_56,
    # Bone_57, ... at the world origin, all parented to <root>, each
    # with a default 1-unit tail. The result is a "spike star" of
    # 20 zero-positioned bones radiating from origin that visually
    # dominates the entire armature in Blender — exactly the bug Yoo
    # kept reporting after every other fix.
    #
    # Dropping the extra channels loses some animation data, but the
    # skeleton we DO have is anatomically complete for the visible
    # human rig, and that's what matters for FBX preview / re-targeting.
    if skeleton and len(skeleton.bones) > 0:
        bone_count = len(skeleton.bones)
        if animation.bone_count > bone_count:
            logger.info(
                "PAA references %d bones but skeleton has %d — "
                "dropping %d extra channels (likely gear / weapon bones "
                "from a different rig)",
                animation.bone_count, bone_count,
                animation.bone_count - bone_count,
            )
    else:
        bone_count = animation.bone_count
    total_frames_early = max(animation.frame_count, len(animation.keyframes))
    duration_early = animation.duration
    final_tick = int(max(duration_early, total_frames_early / max(fps, 1.0))
                     * KTIME_TICKS_PER_SECOND)
    if final_tick <= 0:
        final_tick = KTIME_TICKS_PER_SECOND   # 1-second fallback

    # Pre-allocate animation-stack id so GlobalSettings can reference it.
    anim_stack_id = uid()
    anim_layer_id = uid()

    # Generate the placeholder mesh up-front so the Definitions section
    # (which is emitted before Objects) can declare the right counts
    # for Geometry / Material / Deformer.
    mesh_vertices_flat, mesh_polygon_indices, mesh_bone_weights = (
        _generate_placeholder_skinned_mesh(skeleton)
        if skeleton and skeleton.bones else ([], [], {})
    )
    has_placeholder_mesh = bool(mesh_vertices_flat)
    if has_placeholder_mesh:
        mesh_geom_id = uid()
        mesh_model_id = uid()
        mesh_material_id = uid()
        skin_id = uid()
        cluster_ids: dict[int, _FbxId] = {
            b_idx: uid() for b_idx in mesh_bone_weights
        }
    else:
        mesh_geom_id = mesh_model_id = mesh_material_id = skin_id = None
        cluster_ids = {}

    def global_settings(b):
        def props70(b2):
            W(b2, "P", ["UpAxis", "int", "Integer", "", 1])
            W(b2, "P", ["UpAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["FrontAxis", "int", "Integer", "", 2])
            W(b2, "P", ["FrontAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["CoordAxis", "int", "Integer", "", 0])
            W(b2, "P", ["CoordAxisSign", "int", "Integer", "", 1])
            W(b2, "P", ["UnitScaleFactor", "double", "Number", "", 1.0])
            # TimeMode 11 = 30 fps (FBX enum). TimeSpan + CurrentTime tell
            # the importer where to set the timeline ruler.
            W(b2, "P", ["TimeMode", "enum", "", "", 11])
            W(b2, "P", ["TimeSpanStart", "KTime", "Time", "", 0])
            W(b2, "P", ["TimeSpanStop", "KTime", "Time", "", int(final_tick)])
            W(b2, "P", ["CustomFrameRate", "double", "Number", "", float(fps)])
        W(b, "Properties70", children=[props70])
    W(buf, "GlobalSettings", children=[global_settings])

    # Documents section — declares the active FBX scene.
    def documents(b):
        W(b, "Count", [1])
        def document(b2):
            def doc_props(b3):
                W(b3, "P", ["SourceObject", "object", "", ""])
                W(b3, "P", ["ActiveAnimStackName", "KString", "", "",
                             name or "Take 001"])
            W(b2, "Properties70", children=[doc_props])
            W(b2, "RootNode", [0])
        W(b, "Document",
          [doc_root_id, "Scene\x00\x01SceneInfo", "Scene"],
          children=[document])
    W(buf, "Documents", children=[documents])

    # Definitions section — Blender relies on this to know how many
    # objects of each type to expect. Without it, animation data is
    # often silently discarded.
    def definitions(b):
        # Object type counts we emit: Model (bones + optional mesh),
        # NodeAttribute, AnimationStack/Layer/CurveNode/Curve, and
        # — when a placeholder mesh is included — Geometry, Material
        # and Deformer (one Skin + one Cluster per skinned bone).
        type_counts = [
            ("Model", bone_count + 1 + (1 if has_placeholder_mesh else 0)),
            ("NodeAttribute", bone_count),
            ("AnimationStack", 1),
            ("AnimationLayer", 1),
            ("AnimationCurveNode", bone_count),
            ("AnimationCurve", bone_count * 3),
        ]
        if has_placeholder_mesh:
            type_counts.extend([
                ("Geometry", 1),
                ("Material", 1),
                ("Deformer", 1 + len(cluster_ids)),  # Skin + Clusters
            ])
        W(b, "Version", [100])
        W(b, "Count", [len(type_counts)])
        for ot, count in type_counts:
            def object_type(b2, _ot=ot, _c=count):
                W(b2, "Count", [_c])
            W(b, "ObjectType", [ot], children=[object_type])
    W(buf, "Definitions", children=[definitions])

    # Bone names (used in the Objects and Connections sections).
    bone_names: list[str] = []
    for i in range(bone_count):
        if skeleton and i < len(skeleton.bones):
            bone_names.append(skeleton.bones[i].name or f"Bone_{i}")
        else:
            bone_names.append(f"Bone_{i}")

    # Armature root Null — this is the parent scene-graph node for
    # every LimbNode. Its ID must be allocated at the outer scope so
    # the Connections section can reference it.
    armature_id = uid()

    # Allocate unique IDs for bones (after the pre-allocated IDs above).
    bone_model_ids: dict[int, _FbxId] = {}
    bone_attr_ids: dict[int, _FbxId] = {}
    for i in range(bone_count):
        bone_model_ids[i] = uid()
        bone_attr_ids[i] = uid()

    # One CurveNode + 3 AnimationCurves per bone (rotation channels).
    # We only emit curves that actually have data to keep the file
    # lean — meshes with fewer animated bones than expected still
    # produce a valid FBX.
    curve_node_ids: dict[int, _FbxId] = {}
    curve_ids: dict[int, tuple[_FbxId, _FbxId, _FbxId]] = {}
    for i in range(bone_count):
        curve_node_ids[i] = uid()
        curve_ids[i] = (uid(), uid(), uid())

    # (Placeholder mesh generation moved up-front so Definitions can
    # see the counts. See has_placeholder_mesh / cluster_ids above.)

    # Precompute the Euler keyframes per bone.
    # Layout of ``animation.keyframes``: one AnimationKeyframe per frame
    # where ``bone_rotations[i]`` is the quaternion for bone i at that
    # frame. We walk frames x bones to produce per-bone time series.
    total_frames = max(animation.frame_count, len(animation.keyframes))
    duration = animation.duration
    frame_ticks = [
        _frame_ticks(fi, total_frames, duration, fps) for fi in range(total_frames)
    ]
    def _quat_mul(q1, q2):
        """Quaternion Hamilton product: q1 * q2 as (xyzw, xyzw) -> xyzw."""
        x1, y1, z1, w1 = q1
        x2, y2, z2, w2 = q2
        return (
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        )

    per_bone_eulers: dict[int, list[tuple[float, float, float]]] = {}
    for bone_idx in range(bone_count):
        # PAA rotations are stored RELATIVE to the bone's bind-pose
        # orientation (in PA's local frame). To produce a correct FBX
        # Lcl Rotation we must compose the PAB bind rotation with the
        # PAA per-frame rotation:
        #
        #     fbx_local_rot(t) = PAB_bind_rot ⊗ PAA_rot(t)
        #
        # Without this composition, animations start at an effectively
        # identity-rotated bone instead of its T-pose orientation, and
        # every file looks like the same scrambled blob because the
        # T-pose offset gets dropped.
        if skeleton and bone_idx < len(skeleton.bones):
            bind_rot = skeleton.bones[bone_idx].rotation or (0.0, 0.0, 0.0, 1.0)
        else:
            bind_rot = (0.0, 0.0, 0.0, 1.0)

        series: list[tuple[float, float, float]] = []
        previous_euler: tuple[float, float, float] | None = None
        previous_quat: tuple[float, float, float, float] | None = None
        for kf in animation.keyframes:
            if bone_idx < len(kf.bone_rotations):
                paa_quat = kf.bone_rotations[bone_idx]
            else:
                paa_quat = (0.0, 0.0, 0.0, 1.0)
            # Compose with PAB bind to recover world-frame local rotation
            quat = _quat_mul(bind_rot, paa_quat)
            # Enforce S³ continuity BEFORE Euler decomposition
            quat = _canonicalize_quaternion_sign(quat, previous_quat)
            previous_quat = quat
            euler = _quat_xyzw_to_euler_xyz_degrees(*quat)
            euler = _ensure_euler_continuity(euler, previous_euler)
            series.append(euler)
            previous_euler = euler
        per_bone_eulers[bone_idx] = series

    def objects(b):
        # Armature root model so every LimbNode has a parent FBX importers expect.
        # ``armature_id`` is allocated at the outer scope so Connections
        # can reference it.
        def armature_node(b2):
            W(b2, "Version", [232])
            def arm_props(b3):
                W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A",
                            0.0, 0.0, 0.0])
                W(b3, "P", ["Lcl Rotation", "Lcl Rotation", "", "A",
                            0.0, 0.0, 0.0])
                W(b3, "P", ["Lcl Scaling", "Lcl Scaling", "", "A",
                            1.0, 1.0, 1.0])
            W(b2, "Properties70", children=[arm_props])
        W(b, "Model", [armature_id, "Armature\x00\x01Model", "Null"],
          children=[armature_node])

        # Placeholder mesh — Geometry + Mesh Model + Material. Vertices
        # were generated at each bone's WORLD bind position so the mesh
        # forms a humanoid silhouette before any animation is applied.
        if has_placeholder_mesh:
            def geom_node(b2):
                W(b2, "Vertices", [list(mesh_vertices_flat)])
                W(b2, "PolygonVertexIndex", [list(mesh_polygon_indices)])
                # Workbench / Eevee both render fine without explicit
                # normals — Blender computes per-face normals on import.
            W(b, "Geometry",
              [mesh_geom_id, "PlaceholderBody\x00\x01Geometry", "Mesh"],
              children=[geom_node])

            def mesh_model_node(b2):
                W(b2, "Version", [232])
                def props(b3):
                    W(b3, "P", ["Lcl Translation", "Lcl Translation", "",
                                "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Rotation", "Lcl Rotation", "",
                                "A", 0.0, 0.0, 0.0])
                    W(b3, "P", ["Lcl Scaling", "Lcl Scaling", "",
                                "A", 1.0, 1.0, 1.0])
                W(b2, "Properties70", children=[props])
            W(b, "Model",
              [mesh_model_id, "PlaceholderBody\x00\x01Model", "Mesh"],
              children=[mesh_model_node])

            def material_node(b2):
                W(b2, "Version", [102])
                W(b2, "ShadingModel", ["lambert"])
                def props(b3):
                    # Soft skin tone so the mesh is visible in Workbench.
                    W(b3, "P", ["DiffuseColor", "Color", "", "A",
                                0.85, 0.70, 0.55])
                W(b2, "Properties70", children=[props])
            W(b, "Material",
              [mesh_material_id, "BodyMaterial\x00\x01Material", ""],
              children=[material_node])

        # Bone LimbNodes.
        for i in range(bone_count):
            bname = bone_names[i]

            def attr_node(b2, _i=i, _nm=bname):
                W(b2, "TypeFlags", ["Skeleton"])
                # NodeAttribute.Properties70 is where bone display data
                # lives in FBX — Blender's importer reads LimbLength +
                # Size from here (not from the Model's properties).
                def attr_props(b3):
                    W(b3, "P", ["Size", "double", "Number", "", 1.0])
                W(b2, "Properties70", children=[attr_props])
            W(b, "NodeAttribute",
              [bone_attr_ids[i], f"{bname}\x00\x01NodeAttribute", "LimbNode"],
              children=[attr_node])

            def bone_model(b2, _i=i, _nm=bname):
                W(b2, "Version", [232])
                if skeleton and _i < len(skeleton.bones):
                    pos = skeleton.bones[_i].position or (0.0, 0.0, 0.0)
                    rot = skeleton.bones[_i].rotation or (0.0, 0.0, 0.0, 1.0)
                else:
                    pos = (0.0, 0.0, 0.0)
                    rot = (0.0, 0.0, 0.0, 1.0)
                # Guard against Blender's FBX importer crashing on
                # degenerate zero-position bones. Blender's
                # ``similar_values_iter`` computes
                # ``abs(v1 - v2) / max(abs(v1), abs(v2))`` and raises
                # ZeroDivisionError when a parent's tail and child's
                # head both land at (0, 0, 0). Giving every zero-
                # position bone a UNIQUE tiny offset (scaled by the
                # bone index) prevents bones from collapsing into the
                # same point — the offset is below visual perception
                # but saves Blender's importer. Epsilon is in metres
                # (pre-scale); multiplying by POSITION_SCALE brings it
                # to the same cm unit system the rest of the positions
                # use, keeping the offsets sub-millimetre after import.
                if abs(pos[0]) < 1e-9 and abs(pos[1]) < 1e-9 and abs(pos[2]) < 1e-9:
                    eps = 1e-4 * (_i + 1)
                    pos = (eps, eps * 0.5, -eps * 0.5)
                # Convert PAA metres -> FBX centimetres (see POSITION_SCALE).
                pos = (pos[0] * POSITION_SCALE,
                       pos[1] * POSITION_SCALE,
                       pos[2] * POSITION_SCALE)
                # Convert bind-pose quaternion to Euler XYZ degrees so
                # the armature displays its proper rest pose in Blender
                # (before the animation curves apply).
                rot_euler_deg = _quat_xyzw_to_euler_xyz_degrees(*rot)
                def props(b3, _p=pos, _r=rot_euler_deg):
                    W(b3, "P", ["Lcl Translation", "Lcl Translation", "", "A",
                                float(_p[0]), float(_p[1]), float(_p[2])])
                    W(b3, "P", ["Lcl Rotation", "Lcl Rotation", "", "A",
                                float(_r[0]), float(_r[1]), float(_r[2])])
                    W(b3, "P", ["Lcl Scaling", "Lcl Scaling", "", "A",
                                1.0, 1.0, 1.0])
                    # RotationActive signals to DCC tools that this bone
                    # has a non-identity rotation worth respecting.
                    W(b3, "P", ["RotationActive", "bool", "", "", 1])
                W(b2, "Properties70", children=[props])
            W(b, "Model",
              [bone_model_ids[i], f"{bname}\x00\x01Model", "LimbNode"],
              children=[bone_model])

        # Skin + Cluster deformers — what actually ties the mesh
        # vertices to the bones so the placeholder body deforms when
        # the animation curves drive the bone rotations.
        if has_placeholder_mesh:
            def skin_node(b2):
                W(b2, "Version", [101])
                W(b2, "Link_DeformAcuracy", [50.0])
                W(b2, "SkinningType", ["Linear"])
            W(b, "Deformer",
              [skin_id, "PlaceholderSkin\x00\x01Deformer", "Skin"],
              children=[skin_node])

            for b_idx, weight_list in mesh_bone_weights.items():
                if not weight_list:
                    continue
                cl_id = cluster_ids[b_idx]
                bone = skeleton.bones[b_idx]
                indexes = [vi for vi, _ in weight_list]
                weights = [w for _, w in weight_list]
                # TransformLink = bone's world bind matrix (column-major
                # for FBX). Transform = inverse of TransformLink so the
                # mesh in rest pose round-trips through the deformer
                # cleanly when no animation is applied.
                bind_world = _matrix_pab_row_to_fbx_column(
                    bone.bind_matrix or ()
                )
                # Apply POSITION_SCALE to the translation column of the
                # bind matrix so it lives in the same cm coordinate
                # system as the vertices.
                bind_world[12] *= POSITION_SCALE
                bind_world[13] *= POSITION_SCALE
                bind_world[14] *= POSITION_SCALE
                bind_inv = _invert_4x4(bind_world)

                def cluster_node(b2, _idx=indexes, _w=weights,
                                 _t=bind_inv, _l=bind_world,
                                 _bn=bone):
                    W(b2, "Version", [100])
                    W(b2, "UserData", ["", ""])
                    W(b2, "Indexes", [list(_idx)])
                    W(b2, "Weights", [list(_w)])
                    W(b2, "Transform", [list(_t)])
                    W(b2, "TransformLink", [list(_l)])
                W(b, "Deformer",
                  [cl_id,
                   f"{bone.name}_Cluster\x00\x01SubDeformer", "Cluster"],
                  children=[cluster_node])

        # AnimationStack.
        def stack_node(b2):
            def props(b3):
                W(b3, "P", ["LocalStart", "KTime", "Time", "", int(frame_ticks[0]) if frame_ticks else 0])
                W(b3, "P", ["LocalStop", "KTime", "Time", "",
                            int(frame_ticks[-1]) if frame_ticks else 0])
            W(b2, "Properties70", children=[props])
        W(b, "AnimationStack",
          [anim_stack_id, f"{base}\x00\x01AnimStack", ""],
          children=[stack_node])

        # AnimationLayer.
        def layer_node(b2):
            def props(b3):
                W(b3, "P", ["Weight", "Number", "", "A", 100.0])
            W(b2, "Properties70", children=[props])
        W(b, "AnimationLayer",
          [anim_layer_id, "BaseLayer\x00\x01AnimLayer", ""],
          children=[layer_node])

        # Per-bone CurveNode + Curves.
        for bone_idx in range(bone_count):
            eulers = per_bone_eulers[bone_idx]
            if not eulers:
                continue

            def cnode_node(b2, _el=eulers):
                def props(b3, _el2=_el):
                    rx = _el2[0][0]; ry = _el2[0][1]; rz = _el2[0][2]
                    W(b3, "P", ["d|X", "Number", "", "A", float(rx)])
                    W(b3, "P", ["d|Y", "Number", "", "A", float(ry)])
                    W(b3, "P", ["d|Z", "Number", "", "A", float(rz)])
                W(b2, "Properties70", children=[props])
            W(b, "AnimationCurveNode",
              [curve_node_ids[bone_idx],
               f"{bone_names[bone_idx]}_R\x00\x01AnimCurveNode", ""],
              children=[cnode_node])

            # Three AnimationCurves (X/Y/Z).
            for axis_idx, axis_label in enumerate(("X", "Y", "Z")):
                values = [e[axis_idx] for e in eulers]
                times = [int(t) for t in frame_ticks]
                # KeyAttrFlags 8200 (0x2008) = "cubic auto interpolation".
                # KeyAttrDataFloat: 4 floats per attribute — zeros are
                # fine for auto tangents.
                def curve_node(b2, _times=times, _values=values):
                    W(b2, "Default", [float(_values[0]) if _values else 0.0])
                    W(b2, "KeyVer", [4008])
                    W(b2, "KeyTime", [list(_times)])
                    W(b2, "KeyValueFloat",
                      [[float(v) for v in _values]])
                    W(b2, "KeyAttrFlags", [[0x2008]])
                    W(b2, "KeyAttrDataFloat", [[0.0, 0.0, 0.0, 0.0]])
                    W(b2, "KeyAttrRefCount", [[len(_times)]])
                cid = curve_ids[bone_idx][axis_idx]
                W(b, "AnimationCurve",
                  [cid,
                   f"{bone_names[bone_idx]}_R_{axis_label}\x00\x01AnimCurve", ""],
                  children=[curve_node])

    W(buf, "Objects", children=[objects])

    # Connections.
    def connections(b):
        # Scene root -> armature Null. Without this connection the
        # entire armature is orphaned and Blender treats it as unused
        # geometry — the animation curves connected to the LimbNodes
        # never apply to anything visible.
        W(b, "C", ["OO", armature_id, _FbxId(0)])

        # Bone hierarchy: child -> parent (or ARMATURE if no parent —
        # NOT the scene root, which would fork the armature into a
        # bunch of disconnected trees).
        for i in range(bone_count):
            W(b, "C", ["OO", bone_attr_ids[i], bone_model_ids[i]])
            parent_idx = skeleton.bones[i].parent_index if (skeleton and i < len(skeleton.bones)) else -1
            if parent_idx >= 0 and parent_idx in bone_model_ids:
                W(b, "C", ["OO", bone_model_ids[i], bone_model_ids[parent_idx]])
            else:
                W(b, "C", ["OO", bone_model_ids[i], armature_id])

        # Placeholder mesh wiring:
        #   Mesh Model    -> scene root  (so the body shows up)
        #   Geometry      -> Mesh Model  (mesh data attaches to the model)
        #   Material      -> Mesh Model  (so it isn't rendered black)
        #   Skin Deformer -> Geometry    (skin attaches to the mesh)
        #   Cluster_N     -> Skin        (cluster joins the skin)
        #   Bone_N Model  -> Cluster_N   (bone drives the cluster)
        # This is the topology Blender needs to rebuild the Armature
        # modifier on import so the body deforms with the bones.
        if has_placeholder_mesh:
            W(b, "C", ["OO", mesh_model_id, _FbxId(0)])
            W(b, "C", ["OO", mesh_geom_id, mesh_model_id])
            W(b, "C", ["OO", mesh_material_id, mesh_model_id])
            W(b, "C", ["OO", skin_id, mesh_geom_id])
            for b_idx, cl_id in cluster_ids.items():
                W(b, "C", ["OO", cl_id, skin_id])
                W(b, "C", ["OO", bone_model_ids[b_idx], cl_id])

        # AnimLayer -> AnimStack -> scene root (so the importer knows
        # the stack is the active one for this document).
        W(b, "C", ["OO", anim_layer_id, anim_stack_id])
        W(b, "C", ["OO", anim_stack_id, _FbxId(0)])

        # Per-bone: CurveNode -> AnimLayer, and each axis curve ->
        # CurveNode via OP with the d|X/d|Y/d|Z property name.
        for bone_idx in range(bone_count):
            W(b, "C", ["OO", curve_node_ids[bone_idx], anim_layer_id])
            # CurveNode -> Model.Lcl Rotation
            W(b, "C", ["OP", curve_node_ids[bone_idx],
                       bone_model_ids[bone_idx], "Lcl Rotation"])
            for axis_idx, axis_label in enumerate(("X", "Y", "Z")):
                cid = curve_ids[bone_idx][axis_idx]
                W(b, "C", ["OP", cid, curve_node_ids[bone_idx],
                           f"d|{axis_label}"])

    W(buf, "Connections", children=[connections])

    # Legacy Takes section — some FBX importers (older Blender,
    # Maya 2015-) fall back to this when they can't parse the modern
    # AnimationStack. Even when not strictly needed, emitting it is
    # a strong hint that the file carries one take per stack.
    take_name = name or "Take_001"
    def takes(b):
        W(b, "Current", [take_name])
        def take(b2):
            W(b2, "FileName", [f"{take_name}.tak"])
            W(b2, "LocalTime", [0, int(final_tick)])
            W(b2, "ReferenceTime", [0, int(final_tick)])
        W(b, "Take", [take_name], children=[take])
    W(buf, "Takes", children=[takes])

    # Footer.
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

    logger.info(
        "Exported animation FBX: %s (%d bones, %d frames, duration=%.2fs)",
        fbx_path, bone_count, total_frames, duration or total_frames / fps,
    )
    return fbx_path
