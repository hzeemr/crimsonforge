"""Re-import every edited OBJ from a bulk-export folder back into
the game archives.

Closes the round-trip loop opened by
:mod:`core.character_bulk_export`. Reads the ``manifest.json``
that the bulk export wrote, finds each edited ``.obj`` in
``meshes_obj/``, rebuilds the corresponding ``.pac`` using the
existing mesh-importer pipeline, and patches the rebuilt mesh
back into the live archive — all routed through the v1.22.9
:class:`MeshBaselineManager` so re-imports are byte-stable across
multiple iterations.

Why this is safe
----------------
The bulk exporter saved the **original PAC bytes** verbatim into
``meshes_pac_original/``. The re-importer uses those bytes as the
donor source for the rebuild instead of reading from the live
(potentially already-modified) archive. That gives modders the
same idempotence guarantee they get from single-file editing
through the right-click menu.

Two-mode operation
------------------
1. **Build only** — produces fresh ``.pac`` bytes in a per-mesh
   output folder, NO game archive touched. Default for review.
2. **Patch to game** — also calls :class:`RepackEngine` to commit
   each rebuilt PAC back into the live PAZ/PAMT/PAPGT chain.
   Uses the existing backup machinery so the user can roll back.

Both modes return a :class:`BulkReimportSummary` with per-file
results so the UI can render a proper success / failure table.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.character_bulk_reimport")


@dataclass
class ReimportResult:
    """One per-mesh outcome."""
    obj_path: str
    vfs_path: str
    new_pac_bytes: bytes = b""
    success: bool = False
    error: str = ""
    patched: bool = False


@dataclass
class BulkReimportSummary:
    """Aggregate result of a bulk re-import."""
    work_dir: Path
    results: list[ReimportResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success)

    @property
    def patched(self) -> int:
        return sum(1 for r in self.results if r.patched)

    def report(self) -> str:
        lines = [
            f"Re-import: {self.succeeded}/{len(self.results)} mesh(es) "
            f"rebuilt, {self.patched} patched into game.",
        ]
        if self.failed:
            lines.append(f"Failures ({self.failed}):")
            for r in self.results:
                if not r.success:
                    lines.append(f"  {os.path.basename(r.obj_path)} — {r.error}")
        return "\n".join(lines)


def bulk_reimport_character(
    work_dir: Path | str,
    vfs,
    patch_to_game: bool = False,
    progress_cb=None,
) -> BulkReimportSummary:
    """Re-import every OBJ in ``work_dir/meshes_obj/`` back into
    the game.

    Parameters
    ----------
    work_dir
        Folder produced by :func:`bulk_export_character`. Must
        contain ``manifest.json``, ``meshes_obj/``, and
        ``meshes_pac_original/``.
    vfs
        Loaded :class:`core.vfs_manager.VfsManager`.
    patch_to_game
        If True, commit each rebuilt PAC back into the live
        archive. If False (default), only build the new PAC bytes
        and stash them in ``rebuilt_pac/`` for review — game files
        stay untouched.
    progress_cb
        Optional ``progress_cb(current, total, label)``.

    Returns
    -------
    BulkReimportSummary
    """
    work_dir = Path(work_dir)
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"manifest.json not found in {work_dir} — was this folder "
            f"produced by bulk_export_character?"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = BulkReimportSummary(work_dir=work_dir)

    mesh_dir = work_dir / "meshes_obj"
    pac_orig_dir = work_dir / "meshes_pac_original"
    rebuilt_dir = work_dir / "rebuilt_pac"
    rebuilt_dir.mkdir(exist_ok=True)

    from core.mesh_importer import import_obj, build_mesh
    from core.mesh_baseline_manager import MeshBaselineManager
    baseline_mgr = MeshBaselineManager()

    entries = manifest.get("exported_meshes", [])
    total = len(entries)

    for idx, m in enumerate(entries, start=1):
        obj_rel = m.get("obj", "")
        obj_path = work_dir / obj_rel
        vfs_path = m.get("vfs_path", "")
        result = ReimportResult(
            obj_path=str(obj_path),
            vfs_path=vfs_path,
        )
        if progress_cb:
            try:
                progress_cb(idx, total, f"Rebuilding {os.path.basename(vfs_path)}")
            except Exception:
                pass

        if not obj_path.is_file():
            result.error = f"OBJ not found: {obj_path}"
            summary.results.append(result)
            continue

        try:
            imported = import_obj(str(obj_path))
        except Exception as exc:
            result.error = f"OBJ parse failed: {exc}"
            summary.results.append(result)
            continue

        # Donor source — prefer the original PAC bytes the
        # exporter stashed, fall back to the baseline-manager
        # snapshot, fall back to the live archive if both miss.
        # Falling back to the live archive is the LEAST-safe path
        # but matches what the right-click flow would do, so the
        # behaviour is consistent.
        donor_path = pac_orig_dir / os.path.basename(m.get("pac_original", ""))
        if donor_path.is_file():
            donor_bytes = donor_path.read_bytes()
        else:
            donor_bytes = baseline_mgr.get_or_snapshot(
                vfs_path,
                live_read=lambda vp=vfs_path: _live_read(vfs, vp),
            )

        # Tell build_mesh which file format to target — same dispatch
        # the right-click flow uses.
        ext = os.path.splitext(vfs_path.lower())[1]
        imported.path = vfs_path
        imported.format = (
            "pac" if ext == ".pac"
            else "pamlod" if ext == ".pamlod"
            else "pam"
        )
        try:
            new_bytes = build_mesh(imported, donor_bytes)
        except Exception as exc:
            result.error = f"build_mesh failed: {exc}"
            summary.results.append(result)
            continue

        result.new_pac_bytes = new_bytes
        result.success = True
        # Always stash the rebuilt bytes for review.
        rebuilt_path = rebuilt_dir / os.path.basename(vfs_path)
        rebuilt_path.write_bytes(new_bytes)

        if patch_to_game:
            try:
                _patch_one(vfs, vfs_path, new_bytes)
                result.patched = True
            except Exception as exc:
                result.error = f"patch_to_game failed: {exc}"
                # Keep success=True since the rebuild itself worked.
        summary.results.append(result)

    logger.info("bulk re-import done: %s", summary.report())
    return summary


# ── Helpers ────────────────────────────────────────────────────

def _live_read(vfs, vfs_path: str) -> bytes:
    """Read a single VFS path's live bytes."""
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception:
            continue
        for e in pamt.file_entries:
            if e.path.lower() == vfs_path.lower():
                return vfs.read_entry_data(e)
    raise FileNotFoundError(vfs_path)


def _patch_one(vfs, vfs_path: str, new_bytes: bytes) -> None:
    """Commit one rebuilt PAC back into the live archive.

    Mirrors the patch path used by ``ExplorerTab._import_and_patch_mesh``
    so the same backup + verify flow happens here. Raises on any
    failure so the caller records the error.
    """
    from core.repack_engine import RepackEngine, ModifiedFile

    entry = None
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception:
            continue
        for e in pamt.file_entries:
            if e.path.lower() == vfs_path.lower():
                entry = e
                pamt_data = pamt
                paz_dir = group_dir
                break
        if entry is not None:
            break
    if entry is None:
        raise FileNotFoundError(vfs_path)

    game_path = os.path.dirname(os.path.dirname(entry.paz_file))
    papgt_path = os.path.join(game_path, "meta", "0.papgt")
    mod_file = ModifiedFile(
        data=new_bytes,
        entry=entry,
        pamt_data=pamt_data,
        package_group=paz_dir,
    )
    engine = RepackEngine(game_path)
    result = engine.repack(
        [mod_file], papgt_path=papgt_path,
        create_backup=True, verify_after=True,
    )
    if not result.success:
        raise RuntimeError(
            "; ".join(result.errors) if result.errors else "unknown failure"
        )
    # Invalidate the PAMT cache so subsequent reads see the
    # updated offsets (mirrors the right-click flow).
    try:
        vfs.invalidate_pamt_cache(paz_dir)
    except Exception:
        pass
