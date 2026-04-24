"""Sidecar-file discovery for PAC meshes.

A Crimson Desert character/armor mesh file (``cd_xxx.pac``) is rarely
alone on disk. The engine pulls in a handful of paired files that live
in the same directory and share the mesh's basename:

  ``<mesh>.xml``             — prefab data / bone bindings / attachment
                                points. XML, ChaCha20-encrypted in the
                                PAZ archive.
  ``<mesh>.hkx``             — Havok physics binary. Carries rigid-body
                                collision hulls, cloth simulation shapes,
                                and ragdoll constraints when present.
  ``<mesh>.wrinkle.xml``     — facial micro-physics / wrinkle map data
                                for high-detail heads.
  ``<mesh>.prefabdata.xml``  — supplementary prefab metadata when the
                                engine wants to keep it out of the main
                                .xml.

When a modder repacks a mesh after editing it in Blender, the current
toolchain only swaps the ``.pac``. The sidecar files stay pointing at
the pre-edit topology:

  * vertex positions in ``.hkx`` collision hulls no longer match
    the new mesh — the capsule sits in the old location,
  * attachment points in ``.xml`` still reference vertex indices that
    may have shifted,
  * wrinkle masks reference UV coordinates that may have been remapped.

Depending on how drastic the edit is, the symptoms range from
invisible (physics drags the mesh back to origin) to janky (hair
clipping, weapons floating) to no-op (if the edit was position-only
and small).

This module doesn't try to regenerate sidecars — producing valid HKX
content requires a full Havok SDK license — but it does:

  * enumerate every sidecar paired with a given mesh entry,
  * classify each sidecar by its role,
  * let UIs surface a clear "modifying this mesh will desync N sidecar
    files" warning before the user commits to a repack,
  * expose a helper to bundle the sidecars into a mod package so users
    redistributing a mesh edit ship the matching unaltered sidecars
    instead of letting the game fall back to whatever was installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from utils.logger import get_logger

logger = get_logger("core.mesh_sidecar_service")


# ---------------------------------------------------------------------------
# Sidecar kind taxonomy
# ---------------------------------------------------------------------------

# Ordered from "most disruptive when stale" to "least". The UI uses this
# order when surfacing a warning so the worst desync is shown first.
#
# April-2026 game patch renamed compound .foo.xml suffixes to .foo_xml:
#   .app.xml         → .app_xml
#   .pac.xml         → .pac_xml
#   .prefabdata.xml  → .prefabdata_xml
# We include BOTH so the same code path works on pre-patch and
# post-patch installs. Order within each kind doesn't matter — the
# discovery loop checks each candidate against the VFS and uses the
# one that exists.
SIDECAR_KINDS: tuple[tuple[str, str, str], ...] = (
    # (suffix,                 kind,          human description)
    (".hkx",                   "physics",     "Havok physics (collision, cloth, ragdoll)"),
    (".wrinkle.xml",           "wrinkle",     "Facial wrinkle / micro-physics data"),
    (".prefabdata_xml",        "prefab_data", "Supplementary prefab metadata (post-patch)"),
    (".prefabdata.xml",        "prefab_data", "Supplementary prefab metadata"),
    (".pac_xml",               "prefab",      "Prefab data (post-patch .pac_xml)"),
    (".pac.xml",               "prefab",      "Prefab data (legacy .pac.xml)"),
    (".app_xml",               "appearance",  "Appearance XML (post-patch .app_xml)"),
    (".app.xml",               "appearance",  "Appearance XML (legacy .app.xml)"),
    (".xml",                   "prefab",      "Prefab data / attachment points / bone binds"),
)


@dataclass(slots=True)
class SidecarEntry:
    """One discovered sidecar file."""
    path: str                 # VFS-normalised path (slash, lower-case matches how VFS stores it)
    kind: str                 # one of SIDECAR_KINDS' kind fields
    description: str          # human label
    suffix: str               # exactly which suffix matched (kept so callers can rebuild paths)
    pamt_entry: object = None  # PamtFileEntry when discovered via VFS; None for synthetic
    # Populated on-demand by analyze_physics_risk(). The sidecar
    # discovery step itself stays cheap — parsing every HKX for every
    # mesh in the archive would be needless I/O. Users call
    # analyze_physics_risk explicitly when they're about to commit a
    # mesh edit.
    risk: object = None        # HavokEditRisk instance for the physics sidecar, else None


@dataclass(slots=True)
class SidecarReport:
    """Aggregate sidecar discovery for one mesh."""
    mesh_path: str
    sidecars: list[SidecarEntry] = field(default_factory=list)

    @property
    def has_physics(self) -> bool:
        return any(s.kind == "physics" for s in self.sidecars)

    @property
    def has_wrinkle(self) -> bool:
        return any(s.kind == "wrinkle" for s in self.sidecars)

    def kinds(self) -> list[str]:
        """Deduplicated list of sidecar kinds present, in severity order."""
        seen: list[str] = []
        for kind, *_ in SIDECAR_KINDS:
            if any(s.kind == kind for s in self.sidecars):
                seen.append(kind)
        # Fall back: include any unknown kinds at the end in stable order.
        for s in self.sidecars:
            if s.kind not in seen:
                seen.append(s.kind)
        return seen

    def format_warning(self) -> str:
        """Human-readable warning for a pre-repack confirmation dialog."""
        if not self.sidecars:
            return ""
        lines = [
            f"Editing {self.mesh_path} will desynchronise "
            f"{len(self.sidecars)} paired file(s):",
        ]
        for s in self.sidecars:
            lines.append(f"  • {s.path}  ({s.description})")
        lines.append("")
        lines.append(
            "Physics, attachment-point, and wrinkle data reference the "
            "original mesh topology. Re-import the companion files alongside "
            "your .pac if you changed vertex positions, bone bindings, or UVs."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

def _strip_suffix(path: str, suffix: str) -> str:
    if suffix and path.lower().endswith(suffix.lower()):
        return path[: -len(suffix)]
    return path


def mesh_basename(mesh_path: str) -> str:
    """Return the shared stem every sidecar key is built from.

    For ``character/foo.pac`` this is ``character/foo``. Because a few
    sidecars (``.wrinkle.xml``, ``.prefabdata.xml``) use compound suffixes,
    we strip ``.pac``, ``.pam``, ``.pamlod`` explicitly rather than using
    ``os.path.splitext`` which would only drop the final extension.
    """
    normalised = mesh_path.replace("\\", "/")
    for mesh_suffix in (".pac", ".pam", ".pamlod", ".pab", ".pabc"):
        if normalised.lower().endswith(mesh_suffix):
            return normalised[: -len(mesh_suffix)]
    return normalised


def candidate_sidecar_paths(mesh_path: str) -> list[tuple[str, str, str]]:
    """Return ``(path, kind, description)`` triples the VFS should be checked for."""
    stem = mesh_basename(mesh_path)
    out: list[tuple[str, str, str]] = []
    for suffix, kind, description in SIDECAR_KINDS:
        out.append((f"{stem}{suffix}", kind, description))
    return out


# ---------------------------------------------------------------------------
# VFS-backed discovery
# ---------------------------------------------------------------------------

def discover_sidecars(
    vfs,  # VfsManager; untyped to keep the module dependency-light
    mesh_path: str,
    *,
    package_groups: Iterable[str] = ("0009",),
) -> SidecarReport:
    """Enumerate sidecar files paired with ``mesh_path``.

    ``package_groups`` defaults to ``("0009",)`` because that's the
    character/armor archive — the only group where the four sidecar
    suffixes have ever been observed in shipping builds. Callers can
    broaden the scope to cover other groups for future content.
    """
    report = SidecarReport(mesh_path=mesh_path)

    # Build the candidate paths once, then check each group for each.
    candidates = candidate_sidecar_paths(mesh_path)
    seen: set[str] = set()

    for group in package_groups:
        try:
            pamt = vfs.load_pamt(group)
        except Exception as exc:
            logger.warning("Could not load PAMT for group %s: %s", group, exc)
            continue

        # Index entries by lowercased VFS path for O(1) lookups.
        by_path = {
            entry.path.replace("\\", "/").lower(): entry
            for entry in pamt.file_entries
        }

        for path, kind, description in candidates:
            key = path.lower()
            if key in seen:
                continue
            entry = by_path.get(key)
            if entry is None:
                continue
            seen.add(key)
            report.sidecars.append(
                SidecarEntry(
                    path=path,
                    kind=kind,
                    description=description,
                    suffix=_suffix_for(path),
                    pamt_entry=entry,
                )
            )

    return report


def _suffix_for(path: str) -> str:
    """Return the declared suffix from SIDECAR_KINDS that matches ``path``.

    We can't use ``os.path.splitext`` because our compound suffixes
    (``.wrinkle.xml``, ``.prefabdata.xml``) share the final ``.xml`` with
    plain prefabs. Iterating SIDECAR_KINDS in declaration order picks the
    most specific match first.
    """
    lower = path.lower()
    for suffix, _, _ in SIDECAR_KINDS:
        if lower.endswith(suffix):
            return suffix
    return ""


# ---------------------------------------------------------------------------
# Bundling helpers for mod packaging
# ---------------------------------------------------------------------------

def collect_sidecar_bytes(
    vfs,
    report: SidecarReport,
) -> dict[str, bytes]:
    """Read every sidecar's decompressed payload from the VFS.

    The output is keyed by the sidecar's VFS path so mod packagers can
    drop the bytes into a bundle without further bookkeeping.
    """
    out: dict[str, bytes] = {}
    for s in report.sidecars:
        if s.pamt_entry is None:
            continue
        try:
            out[s.path] = vfs.read_entry_data(s.pamt_entry)
        except Exception as exc:
            logger.warning("Could not read sidecar %s: %s", s.path, exc)
    return out


def analyze_physics_risk(vfs, report: SidecarReport) -> SidecarReport:
    """Populate ``SidecarEntry.risk`` for every physics sidecar.

    Reads and parses each ``.hkx`` sidecar through the Havok TAG0
    parser and attaches a :class:`HavokEditRisk` (severity + reasons +
    driving systems). UIs and mod packagers should surface the
    resulting warning before accepting a mesh edit — this is the
    primary fix for the "beard stretches to ground" failure mode
    reported by fuse00_ on the community discord.

    Returns the same report (mutated in place) so calls can chain.
    """
    try:
        from core.havok_parser import assess_mesh_edit_risk
    except ImportError:
        return report

    for entry in report.sidecars:
        if entry.kind != "physics" or entry.pamt_entry is None:
            continue
        try:
            hkx_bytes = vfs.read_entry_data(entry.pamt_entry)
        except Exception as exc:
            logger.warning("Could not read HKX sidecar %s: %s", entry.path, exc)
            continue
        try:
            entry.risk = assess_mesh_edit_risk(hkx_bytes)
        except Exception as exc:
            logger.warning("HKX risk assessment failed for %s: %s", entry.path, exc)

    return report
