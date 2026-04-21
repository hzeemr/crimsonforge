"""Texture discovery + per-face colour sampling for PAC previews.

The Explorer mesh preview currently renders every submesh with the same
blue-shift Lambert palette. Two users in the community chat asked for
"coloured preview with the actual texture" — this module is the data
layer that answers that request.

Scope
-----

  * Pair every PAC submesh with its shipping ``.dds`` texture. Crimson
    Desert meshes follow a handful of naming conventions (same basename,
    common suffixes like ``_d`` / ``_diffuse`` / ``_albedo`` / ``_col``),
    so we probe each of them in the character archive.
  * Decode the matched DDS to RGBA once, then sample the texture at
    every face's UV centroid. Per-face colours are enough to give the
    preview genuine visual fidelity without writing a full UV-
    interpolating software rasteriser.
  * Keep the output compact — a list of ``(r, g, b, a)`` tuples in the
    same order as ``SubMesh.faces``. The viewer simply swaps its
    procedural palette for these colours.

Explicit non-goals
------------------

  * No nearest-texel aliasing reduction: a tiny UV movement can flip
    the sampled texel. The viewer already draws faces at sub-pixel size
    on small meshes; bilinear sampling would help but adds a 2× cost
    for a feature that's mostly cosmetic.
  * No material-graph resolution. Crimson Desert ships a per-mesh
    ``.xml`` that maps submesh material names to textures, but decoding
    that XML is a separate effort (it varies by content type). This
    module matches by filename heuristics, which is the convention
    every Nexus mod follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from utils.logger import get_logger

logger = get_logger("core.mesh_texture_service")


# ---------------------------------------------------------------------------
# Texture pairing
# ---------------------------------------------------------------------------

# Diffuse-channel suffixes the engine uses, in order of preference. The
# empty string comes first because Crimson Desert's shipping convention
# is that ``<material>.dds`` IS the diffuse — everything else is a
# sidecar channel (``_n`` normal, ``_sp`` specular, ``_m`` metallic,
# ``_disp`` displacement, etc.). Explicit ``_d`` / ``_diffuse`` /
# ``_albedo`` variants cover third-party mods and the occasional naming
# outlier.
TEXTURE_SUFFIX_PROBES: tuple[str, ...] = (
    "",             # cd_foo.dds                  ← the shipping default
    "_d",           # cd_foo_d.dds
    "_diffuse",     # cd_foo_diffuse.dds
    "_albedo",      # cd_foo_albedo.dds
    "_col",         # cd_foo_col.dds
    "_color",       # cd_foo_color.dds
    "_base",        # cd_foo_base.dds
    "_basecolor",   # cd_foo_basecolor.dds
)

_MESH_SUFFIXES = (".pac", ".pam", ".pamlod", ".pab", ".pabc")


def _mesh_basename(mesh_path: str) -> str:
    normalised = mesh_path.replace("\\", "/")
    for mesh_suffix in _MESH_SUFFIXES:
        if normalised.lower().endswith(mesh_suffix):
            return normalised[: -len(mesh_suffix)]
    return normalised


def _mesh_directory(mesh_path: str) -> str:
    """Return the directory portion of a VFS mesh path (no trailing slash)."""
    normalised = mesh_path.replace("\\", "/")
    if "/" not in normalised:
        return ""
    return normalised.rsplit("/", 1)[0]


def candidate_texture_paths(mesh_path: str) -> list[str]:
    """Mesh-basename fallback probe list.

    Kept for the rare case where a mod ships a texture using the mesh
    basename (e.g. ``cd_foo.pac`` + ``cd_foo.dds``) rather than the
    material name. ``candidate_texture_paths_for_material`` below covers
    the shipping convention and is what ``compute_mesh_texture_report``
    tries first.
    """
    stem = _mesh_basename(mesh_path)
    return [f"{stem}{suffix}.dds" for suffix in TEXTURE_SUFFIX_PROBES]


def candidate_texture_paths_for_material(mesh_path: str, material: str) -> list[str]:
    """Return every DDS path we should try for a given material.

    Crimson Desert ships textures named after the material, lowercased,
    living in the same directory as the mesh::

        submesh.material = 'CD_PHM_00_Cloak_0032_00_01_01'
        mesh_path        = 'character/cd_phm_00_cloak_00_0208.pac'
        diffuse          = 'character/cd_phm_00_cloak_0032_00_01_01.dds'

    So we join ``mesh_dir + lower(material) + suffix + .dds`` for every
    suffix in ``TEXTURE_SUFFIX_PROBES``. The empty-suffix hit is the
    shipping default and gets checked first.
    """
    material_lower = material.strip().lower()
    if not material_lower:
        return []
    directory = _mesh_directory(mesh_path)
    prefix = f"{directory}/" if directory else ""
    return [f"{prefix}{material_lower}{suffix}.dds" for suffix in TEXTURE_SUFFIX_PROBES]


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DecodedTexture:
    """A decoded DDS ready for nearest-texel sampling."""
    width: int
    height: int
    rgba: bytes  # row-major, 4 bytes per texel

    @property
    def texel_count(self) -> int:
        return self.width * self.height

    def sample_uv(self, u: float, v: float) -> tuple[int, int, int, int]:
        """Nearest-texel sample at (u, v) in [0, 1] texture space.

        ``v`` is treated in the "OBJ convention" (0 at bottom, 1 at top)
        that the CrimsonForge exporter writes. DDS rows go top-to-bottom
        though, so we flip v before indexing. Values outside [0, 1] wrap
        — real shipping meshes occasionally carry UVs slightly outside
        the range, and wrapping is what the engine does at runtime.
        """
        if self.width <= 0 or self.height <= 0 or not self.rgba:
            return (0, 0, 0, 255)

        # Wrap into [0, 1).
        u = u - int(u) if u >= 0 else 1.0 - ((-u) - int(-u))
        v = v - int(v) if v >= 0 else 1.0 - ((-v) - int(-v))
        if u < 0:
            u += 1.0
        if v < 0:
            v += 1.0

        # DDS rows run top-to-bottom; our UV convention has v=0 at bottom.
        v_flipped = 1.0 - v

        x = max(0, min(self.width - 1, int(u * self.width)))
        y = max(0, min(self.height - 1, int(v_flipped * self.height)))
        idx = (y * self.width + x) * 4
        if idx + 4 > len(self.rgba):
            return (0, 0, 0, 255)
        r = self.rgba[idx]
        g = self.rgba[idx + 1]
        b = self.rgba[idx + 2]
        a = self.rgba[idx + 3]
        return (r, g, b, a)


def decode_dds_payload(data: bytes) -> DecodedTexture | None:
    """Decode raw DDS bytes. Returns ``None`` on any failure.

    Deferring the ``core.dds_reader`` import keeps this module importable
    from tests even when Pillow / heavy decoders aren't available.
    """
    try:
        from core.dds_reader import decode_dds_to_rgba
    except ImportError:
        return None

    try:
        width, height, rgba = decode_dds_to_rgba(data)
    except Exception as exc:
        logger.warning("DDS decode failed: %s", exc)
        return None

    return DecodedTexture(width=width, height=height, rgba=rgba)


# ---------------------------------------------------------------------------
# Mesh ⇆ texture glue
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SubmeshTexture:
    """Texture data attached to a single submesh."""
    submesh_index: int
    texture_path: str
    texture: DecodedTexture
    # Per-face RGBA. Index matches ``SubMesh.faces[i]``. Kept for the
    # software-viewer fallback and for tests that pre-date the GPU
    # texturing path; the OpenGL viewer goes straight to the raw
    # ``texture`` and the mesh's UVs.
    face_colors: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass(slots=True)
class MeshTextureReport:
    """Every submesh's discovered texture, or None if none matched."""
    mesh_path: str
    submeshes: list[SubmeshTexture | None] = field(default_factory=list)

    @property
    def any_textured(self) -> bool:
        return any(s is not None for s in self.submeshes)


@dataclass(slots=True)
class GpuTexturePayload:
    """Everything the OpenGL viewer needs to render a textured mesh.

    Flattened across every submesh so the viewer can upload a single
    non-indexed vertex stream plus one decoded texture per distinct
    diffuse file. Submeshes sharing a material share a texture id here,
    which keeps the number of GPU uploads to the minimum the archive
    actually required.

    * ``positions`` / ``normals`` / ``uvs`` are flat lists of length
      3 * total_triangles. Each triangle contributes three entries.
    * ``texture_ids`` maps *triangle index* to an entry in
      ``textures``. Grey fallback triangles use -1.
    * ``textures`` is the ordered list of unique ``DecodedTexture``
      objects referenced by at least one triangle.
    """
    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    texture_ids: list[int] = field(default_factory=list)
    textures: list[DecodedTexture] = field(default_factory=list)

    @property
    def triangle_count(self) -> int:
        return len(self.texture_ids)

    @property
    def is_empty(self) -> bool:
        return self.triangle_count == 0


def build_gpu_texture_payload(parsed_mesh, report: MeshTextureReport) -> GpuTexturePayload:
    """Flatten a parsed mesh + its texture report into a GPU-ready payload.

    The resulting lists are non-indexed: every triangle owns its three
    vertices so different submeshes can carry different textures without
    the shared-vertex blending that plagues per-face flat colouring.
    """
    payload = GpuTexturePayload()
    unique_textures: dict[int, int] = {}  # id(DecodedTexture) -> index in payload.textures

    for sm_idx, sm in enumerate(parsed_mesh.submeshes):
        entry = report.submeshes[sm_idx] if sm_idx < len(report.submeshes) else None

        if entry is None:
            tex_id = -1
        else:
            key = id(entry.texture)
            tex_id = unique_textures.get(key)
            if tex_id is None:
                tex_id = len(payload.textures)
                unique_textures[key] = tex_id
                payload.textures.append(entry.texture)

        vertices = sm.vertices
        uvs = sm.uvs
        # Per-vertex smooth normals were computed during parsing; the
        # parser always populates ``sm.normals`` (if missing, it falls
        # back to computing them). Trust its output here.
        normals = sm.normals or [(0.0, 1.0, 0.0)] * len(vertices)

        for face in sm.faces:
            a, b, c = face
            if a >= len(vertices) or b >= len(vertices) or c >= len(vertices):
                continue
            for idx in (a, b, c):
                payload.positions.append(vertices[idx])
                payload.normals.append(normals[idx] if idx < len(normals) else (0.0, 1.0, 0.0))
                if idx < len(uvs):
                    payload.uvs.append(uvs[idx])
                else:
                    payload.uvs.append((0.0, 0.0))
            payload.texture_ids.append(tex_id)

    return payload


def _face_centroid_uv(
    face: tuple[int, int, int],
    uvs: list[tuple[float, float]],
) -> tuple[float, float] | None:
    try:
        a, b, c = face
        ua, va = uvs[a]
        ub, vb = uvs[b]
        uc, vc = uvs[c]
    except (IndexError, ValueError):
        return None
    return ((ua + ub + uc) / 3.0, (va + vb + vc) / 3.0)


def sample_face_colors(
    texture: DecodedTexture,
    uvs: list[tuple[float, float]],
    faces: list[tuple[int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Return one RGBA sample per face, using the face's UV centroid.

    Short-circuits on empty input. For faces with missing or malformed
    UVs the returned colour is the texture's top-left texel, which
    matches how the engine handles degenerate UV data.
    """
    if not faces:
        return []
    fallback = texture.sample_uv(0.0, 0.0)
    colors: list[tuple[int, int, int, int]] = []
    for face in faces:
        uv = _face_centroid_uv(face, uvs)
        if uv is None:
            colors.append(fallback)
            continue
        colors.append(texture.sample_uv(uv[0], uv[1]))
    return colors


def _find_first_matching(pamt_index: dict, candidate_paths: list[str]):
    """Return ``(entry, matched_path)`` for the first candidate in ``pamt_index``."""
    for candidate in candidate_paths:
        hit = pamt_index.get(candidate.lower())
        if hit is not None:
            return hit, candidate
    return None, None


def _find_texture_entry(pamt_entries: list, mesh_path: str):
    """Back-compat helper used by older callers and the mesh-basename fallback.

    Returns the first ``(entry, matched_path)`` whose path matches any
    mesh-basename probe. Per-submesh material resolution is preferred
    and lives in ``compute_mesh_texture_report``.
    """
    pamt_index = {
        entry.path.replace("\\", "/").lower(): entry for entry in pamt_entries
    }
    return _find_first_matching(pamt_index, candidate_texture_paths(mesh_path))


def compute_mesh_texture_report(
    vfs,
    mesh_path: str,
    parsed_mesh,
    *,
    package_groups: Iterable[str] = ("0009",),
) -> MeshTextureReport:
    """Resolve and sample textures for every submesh of ``parsed_mesh``.

    Lookup strategy (per submesh, in order):

      1. ``{mesh_dir}/{material_lowered}.dds`` and common diffuse
         suffixes — this is Crimson Desert's shipping convention
         (verified against character/cd_phm_00_cloak_00_0208.pac which
         uses material ``CD_PHM_00_Cloak_0032_00_01_01`` and a diffuse
         at ``character/cd_phm_00_cloak_0032_00_01_01.dds``).
      2. ``{mesh_stem}{suffix}.dds`` — legacy mesh-basename fallback
         for mods that don't follow the material convention.

    A decoded-texture cache keeps us from re-reading a shared diffuse
    for every submesh that references the same material (common case —
    character head + eye-cover submesh share one material).
    """
    report = MeshTextureReport(mesh_path=mesh_path)

    # Build one combined PAMT index across all requested groups up front.
    # Previously we walked each group separately per lookup, which on
    # 0009 (396k entries) means 396k dict lookups per call.
    pamt_index: dict = {}
    for group in package_groups:
        try:
            pamt = vfs.load_pamt(group)
        except Exception as exc:
            logger.warning("Could not load PAMT for group %s: %s", group, exc)
            continue
        for entry in pamt.file_entries:
            # First writer wins — if two groups carry the same path the
            # earlier group (usually 0009) is the authoritative one.
            pamt_index.setdefault(
                entry.path.replace("\\", "/").lower(), entry
            )

    decoded_cache: dict[str, DecodedTexture | None] = {}

    def resolve_texture(material: str):
        # Per-material lookup first, mesh-basename fallback second.
        candidates = candidate_texture_paths_for_material(mesh_path, material)
        entry, matched_path = _find_first_matching(pamt_index, candidates)
        if entry is None:
            entry, matched_path = _find_first_matching(
                pamt_index, candidate_texture_paths(mesh_path),
            )
        return entry, matched_path

    for sm_idx, sm in enumerate(parsed_mesh.submeshes):
        material = (sm.material or "").strip()
        entry, texture_path = resolve_texture(material)
        if entry is None:
            report.submeshes.append(None)
            continue

        if texture_path in decoded_cache:
            decoded = decoded_cache[texture_path]
        else:
            try:
                dds_bytes = vfs.read_entry_data(entry)
            except Exception as exc:
                logger.warning("Could not read texture %s: %s", texture_path, exc)
                decoded = None
            else:
                decoded = decode_dds_payload(dds_bytes)
            decoded_cache[texture_path] = decoded

        if decoded is None:
            report.submeshes.append(None)
            continue

        face_colors = sample_face_colors(decoded, sm.uvs, sm.faces)
        report.submeshes.append(
            SubmeshTexture(
                submesh_index=sm_idx,
                texture_path=texture_path,
                texture=decoded,
                face_colors=face_colors,
            )
        )

    return report
