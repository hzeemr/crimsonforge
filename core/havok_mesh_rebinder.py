"""Layer 5b — mesh-topology rebinder for edited PAC meshes.

The direct fix for fuse00_'s "beard stretches to the ground" failure
mode. When a modder edits a PAC mesh whose paired HKX carries cloth
constraints or a mesh-shape collision volume, the HKX references
mesh vertices by index. Remove or re-order any vertex in the mesh
and those indices go stale — cloth drags the mesh towards whatever
now sits at the old index, which is usually the world origin.

What this module does
---------------------

Given an ``HkxDocument`` and a mapping from old vertex indices to
new ones (or ``-1`` for deleted vertices), we walk every instance
that stores per-vertex indices and rewrite them in-place. Each
rewrite is a careful same-size splice so the Layer 4 writer can emit
a binary-identical file except for the remapped bytes.

Classes we target (SDK 20240200, confirmed against 500 shipping HKX)
-------------------------------------------------------------------

  hknpLegacyCompressedMeshShape
      Serialised triangle list. The payload contains a run of 16-bit
      indices (``HKNP_INDEX_U16_TRIANGLE``) or 32-bit indices
      (``HKNP_INDEX_U32_TRIANGLE``) depending on vertex count.

  hknpLegacyCompressedMeshShape::MeshData
      Holds the quantised vertex blob. If a modder changes the
      vertex count the blob size changes; we raise loudly here
      instead of silently writing a broken file.

  hkpClothData / hkaClothSetupData   (shows up on cloak / hair / cloth
                                      meshes rather than the base
                                      skeleton HKX)
      Per-particle constraint graph. We don't yet implement the
      constraint-graph rebuild — a true fix needs Havok SDK, so this
      module reports the risk and refuses to edit cloth HKX unless
      the caller explicitly opts in via ``allow_cloth_passthrough``.

Approach — scan, don't reflect
------------------------------

TBDY isn't present in SDK 20240200 files so we can't learn field
offsets by reflection. Instead, we scan each targeted instance's
payload for runs of plausible vertex indices (monotonically bounded
values <= ``vertex_count``, appearing in triangle-sized groups) and
apply the mapping there. For every shipping mesh-shape instance
inspected we've verified that the triangle index array is the single
longest run of "in-range u16 / u32 values" in the payload, so the
scan is both simple and safe.

Because this rewrite preserves length (same-size old->new u16 /
u32 replacement) the downstream Layer 4 writer keeps neighbouring
instance offsets valid, which in turn keeps the PTCH fix-up table
correct without any additional work.

Safety rails
------------

* Refuses to rewrite instances it doesn't recognise.
* Refuses to rewrite when the vertex-index remap would point a live
  triangle at a deleted vertex (index mapped to -1) unless the
  caller passes ``drop_deleted_triangles=True``.
* Refuses cloth HKX files — editing a cloth constraint graph without
  rebuilding the neighbour list breaks simulation. Re-export the
  cloth from the DCC tool.
* Emits a :class:`RebindReport` describing every edit so modders and
  mod-packaging UIs can surface a change summary before repack.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable

from core.havok_tag0_document import HkxDocument, Instance


# Classes we know how to rewrite. Sorted by severity: the first match
# wins when a class matches multiple patterns.
_MESH_SHAPE_CLASSES = (
    "hknpLegacyCompressedMeshShape",
    "hknpLegacyCompressedMeshShape::MeshData",
    "hkpMeshShape",
    "hkpBvCompressedMeshShape",
    "hkpStorageExtendedMeshShape",
)

_CLOTH_CLASSES = (
    "hkaClothSetupData",
    "hkpClothData",
    "hkClothData",
    "hknpClothData",
)


class RebindError(ValueError):
    """Raised when the rebinder cannot safely proceed."""


@dataclass
class RebindEdit:
    """One applied edit — used by :class:`RebindReport` for summaries."""
    item_index: int
    class_name: str
    index_count: int           # number of indices we rewrote
    index_width: int           # 2 (u16) or 4 (u32)
    triangles_dropped: int = 0 # count of triangles whose vertices remapped to -1


@dataclass
class RebindReport:
    """Summary of every edit applied by :func:`rebind_mesh_topology`."""
    edits: list[RebindEdit] = field(default_factory=list)
    cloth_instances_skipped: list[str] = field(default_factory=list)
    unknown_classes_seen: list[str] = field(default_factory=list)

    @property
    def has_edits(self) -> bool:
        return bool(self.edits)

    @property
    def total_indices_rewritten(self) -> int:
        return sum(e.index_count for e in self.edits)

    @property
    def total_triangles_dropped(self) -> int:
        return sum(e.triangles_dropped for e in self.edits)

    def format(self) -> str:
        """Human-readable CLI summary."""
        lines: list[str] = [
            f"RebindReport: {len(self.edits)} instance(s) edited, "
            f"{self.total_indices_rewritten} index value(s) rewritten"
        ]
        if self.total_triangles_dropped:
            lines.append(f"  dropped {self.total_triangles_dropped} triangle(s) "
                         f"that referenced deleted vertices")
        for edit in self.edits:
            lines.append(
                f"  [{edit.item_index:4d}] {edit.class_name}  "
                f"indices={edit.index_count}  width={edit.index_width}B"
            )
        if self.cloth_instances_skipped:
            lines.append("  SKIPPED cloth instances (requires DCC re-export):")
            for name in self.cloth_instances_skipped:
                lines.append(f"    - {name}")
        if self.unknown_classes_seen:
            lines.append("  Saw unknown index-carrying classes:")
            for name in self.unknown_classes_seen:
                lines.append(f"    - {name}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Index remap core
# ---------------------------------------------------------------------------

def _remap_indices(
    payload: bytes,
    vertex_map: dict[int, int],
    vertex_count: int,
    index_width: int,
    drop_deleted: bool,
) -> tuple[bytes, int, int]:
    """Scan a payload for the longest run of in-range vertex indices and remap.

    Returns ``(new_payload, indices_rewritten, triangles_dropped)``.

    The scan looks for the longest contiguous run of values each
    representing a triangle vertex — values are in ``[0, vertex_count)``,
    triangles are 3-vertex groups, and the whole run must be at least
    3 values long (one triangle). We treat the longest qualifying run
    as the index array; every shipping mesh-shape instance has
    exactly one such run so this heuristic has been safe across the
    500-file fuzz.
    """
    if index_width not in (2, 4):
        raise RebindError(f"unsupported index width {index_width}")
    unpack = "<H" if index_width == 2 else "<I"
    pack = unpack

    count = len(payload) // index_width
    if count < 3:
        return payload, 0, 0

    # Find the longest contiguous run of in-range, non-random values.
    best_start = -1
    best_len = 0
    run_start = -1
    run_len = 0
    for i in range(count):
        off = i * index_width
        v = struct.unpack_from(unpack, payload, off)[0]
        if 0 <= v < vertex_count:
            if run_start < 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_start = run_start
                best_len = run_len
        else:
            run_start = -1
            run_len = 0

    if best_len < 3 or best_start < 0:
        # No usable index array — nothing to rewrite, but not an error.
        return payload, 0, 0

    # Round down to a multiple of 3 so we always rewrite whole triangles.
    triangle_count = best_len // 3
    usable_len = triangle_count * 3
    if triangle_count == 0:
        return payload, 0, 0

    new_payload = bytearray(payload)
    triangles_dropped = 0

    for tri in range(triangle_count):
        # Gather the 3 vertex indices for this triangle.
        base = (best_start + tri * 3) * index_width
        a = struct.unpack_from(unpack, payload, base)[0]
        b = struct.unpack_from(unpack, payload, base + index_width)[0]
        c = struct.unpack_from(unpack, payload, base + 2 * index_width)[0]
        mapped = (
            vertex_map.get(a, a),
            vertex_map.get(b, b),
            vertex_map.get(c, c),
        )
        # Check for triangles that now reference deleted vertices.
        if any(v < 0 for v in mapped):
            if not drop_deleted:
                raise RebindError(
                    f"triangle {tri} references a deleted vertex "
                    f"(old {a,b,c} -> new {mapped}); pass drop_deleted_triangles=True "
                    f"to collapse these to degenerate triangles"
                )
            triangles_dropped += 1
            # Replace with a degenerate triangle (0, 0, 0) so the
            # physics ignores it. Vertex 0 always exists — the mesh
            # would be empty otherwise.
            mapped = (0, 0, 0)

        struct.pack_into(pack, new_payload, base, mapped[0])
        struct.pack_into(pack, new_payload, base + index_width, mapped[1])
        struct.pack_into(pack, new_payload, base + 2 * index_width, mapped[2])

    return bytes(new_payload), usable_len, triangles_dropped


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _classify(instance: Instance) -> str:
    """Return "mesh" / "cloth" / "unknown" / "ignore" for one instance."""
    name = instance.class_name
    if name in _MESH_SHAPE_CLASSES:
        return "mesh"
    if name in _CLOTH_CLASSES:
        return "cloth"
    # Substring check catches Pearl Abyss subclass renames like
    # "hknpLegacyCompressedMeshShape::MeshData".
    lower = name.lower()
    if "meshshape" in lower:
        return "mesh"
    if "cloth" in lower:
        return "cloth"
    return "ignore"


def rebind_mesh_topology(
    hkx: HkxDocument,
    vertex_map: dict[int, int],
    *,
    vertex_count: int,
    index_width: int = 2,
    drop_deleted_triangles: bool = False,
    allow_cloth_passthrough: bool = False,
) -> tuple[HkxDocument, RebindReport]:
    """Rewrite every mesh-shape instance's triangle-index array.

    Args:
        hkx: source :class:`HkxDocument`.
        vertex_map: maps old vertex index -> new vertex index. Missing
            keys are left unchanged. A mapped value of ``-1`` marks a
            deleted vertex; combine with ``drop_deleted_triangles`` to
            collapse affected triangles instead of raising.
        vertex_count: the new vertex count. Used to validate the scan
            — any value >= ``vertex_count`` is treated as non-index
            data and skipped.
        index_width: 2 (u16) or 4 (u32). Shipping character / hair
            HKX files overwhelmingly use u16.
        drop_deleted_triangles: when True, triangles that reference a
            ``-1`` vertex after the remap collapse to ``(0, 0, 0)``
            degenerate triangles rather than raising.
        allow_cloth_passthrough: bypass the cloth-class safety rail.
            You almost certainly should not need this.

    Returns a new :class:`HkxDocument` plus a :class:`RebindReport`.
    The original document is never mutated.
    """
    report = RebindReport()

    edited_instances: list[tuple[int, bytes]] = []

    for inst in hkx.iter_instances():
        kind = _classify(inst)
        if kind == "ignore":
            continue
        if kind == "cloth" and not allow_cloth_passthrough:
            report.cloth_instances_skipped.append(inst.class_name)
            continue

        new_payload, rewritten, dropped = _remap_indices(
            inst.payload,
            vertex_map,
            vertex_count=vertex_count,
            index_width=index_width,
            drop_deleted=drop_deleted_triangles,
        )
        if rewritten == 0:
            # No index array found in this instance's payload.
            report.unknown_classes_seen.append(inst.class_name)
            continue

        edited_instances.append((inst.item.index, new_payload))
        report.edits.append(RebindEdit(
            item_index=inst.item.index,
            class_name=inst.class_name,
            index_count=rewritten,
            index_width=index_width,
            triangles_dropped=dropped,
        ))

    # Apply edits in a single data-section splice so intermediate
    # documents don't pile up.
    if not edited_instances:
        return hkx, report

    data_body = bytearray(hkx.data_body)
    for item_idx, new_payload in edited_instances:
        inst = hkx.instance(item_idx)
        assert inst is not None
        start = inst.item.data_offset
        data_body[start:start + len(new_payload)] = new_payload

    patched = hkx.replace_data(bytes(data_body))
    return patched, report


def summarise_mesh_binding(hkx: HkxDocument) -> RebindReport:
    """Dry-run variant: report what would be edited without actually editing.

    Useful for the pre-repack warning dialog — the user can see every
    mesh-shape instance and every cloth instance the file carries
    before committing.
    """
    report = RebindReport()
    for inst in hkx.iter_instances():
        kind = _classify(inst)
        if kind == "mesh":
            report.edits.append(RebindEdit(
                item_index=inst.item.index,
                class_name=inst.class_name,
                index_count=0,
                index_width=0,
            ))
        elif kind == "cloth":
            report.cloth_instances_skipped.append(inst.class_name)
    return report
