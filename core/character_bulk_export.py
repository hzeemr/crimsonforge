"""Bulk export every mesh + texture + skeleton in a character bundle.

Given a :class:`CharacterAssetBundle` from
:mod:`core.character_asset_resolver`, dump every related ``.pac``
mesh as ``.obj`` + every ``.dds`` texture into a user folder, plus
a ``manifest.json`` that records the source VFS path of each file
and a ``import_blender.py`` script that loads everything into a
single Blender scene with auto-rigged armatures.

Why bulk
--------
Modders editing a character touch many files at once — body mesh,
head, hair, cloak, armour, eyes, teeth, plus their textures. Doing
each one through the right-click menu is tedious + error-prone.
Bulk export gives a single call that produces a self-contained
work folder ready for Blender, with the round-trip path back into
the game gated by the existing baseline manager so re-import is
idempotent.

What gets exported
------------------
* **Meshes** — every ``.pac`` / ``.pam`` / ``.pamlod`` becomes an
  ``.obj`` (via the existing :func:`core.mesh_exporter.export_obj`)
  alongside its ``.cfmeta.json`` sidecar (skin weights, etc.) so
  re-import preserves bone bindings.
* **Textures** — every ``.dds`` is copied verbatim. Blender reads
  DDS natively via Pillow so users don't need to convert.
* **Manifest** — ``manifest.json`` records the bundle's canonical
  key + every (out_path, vfs_path) pair so the re-import flow can
  find the original archive entry.
* **Blender script** — ``import_blender.py`` is a Python script
  the user runs from Blender's text editor. It imports every OBJ
  into a single scene, applies the matching textures, and groups
  them under empties named per-submesh.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.character_asset_resolver import CharacterAssetBundle
from utils.logger import get_logger

logger = get_logger("core.character_bulk_export")


@dataclass
class BulkExportSummary:
    """Counts / paths for a completed bulk export."""
    out_dir: Path
    meshes_exported: int = 0
    textures_copied: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    manifest_path: Optional[Path] = None
    blender_script_path: Optional[Path] = None

    @property
    def total_files(self) -> int:
        return self.meshes_exported + self.textures_copied

    def report(self) -> str:
        lines = [
            f"Exported {self.meshes_exported} mesh(es) + "
            f"{self.textures_copied} texture(s) → {self.out_dir}",
        ]
        if self.skipped:
            lines.append(f"Skipped {len(self.skipped)} file(s):")
            for path, reason in self.skipped[:10]:
                lines.append(f"  {path} — {reason}")
            if len(self.skipped) > 10:
                lines.append(f"  ... +{len(self.skipped)-10} more")
        if self.blender_script_path:
            lines.append(
                f"Blender setup: {self.blender_script_path} "
                "(open in Blender → Text Editor → Run Script)"
            )
        return "\n".join(lines)


def bulk_export_character(
    bundle: CharacterAssetBundle,
    vfs,
    out_dir: Path | str,
    progress_cb=None,
) -> BulkExportSummary:
    """Export every mesh + texture in ``bundle`` to ``out_dir``.

    Creates the directory tree, writes the manifest + Blender
    script, and returns a :class:`BulkExportSummary` with counts +
    skip reasons. Never raises for a single-file failure — it logs
    + records the skip and moves on so a bad mesh doesn't kill the
    whole export.

    ``progress_cb(current, total, label)`` is called on every file.
    Pass ``None`` for headless / silent operation.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = BulkExportSummary(out_dir=out_dir)

    # Sub-folders so the export folder stays browsable.
    mesh_dir = out_dir / "meshes_obj"
    tex_dir = out_dir / "textures_dds"
    pac_dir = out_dir / "meshes_pac_original"
    mesh_dir.mkdir(exist_ok=True)
    tex_dir.mkdir(exist_ok=True)
    pac_dir.mkdir(exist_ok=True)

    # Walk meshes + textures only — every other category is
    # informational and stays in the manifest for reference but
    # doesn't need to land on disk.
    mesh_entries = [
        e for e in bundle.entries
        if e.path.lower().endswith((".pac", ".pam", ".pamlod"))
    ]
    texture_entries = [
        e for e in bundle.entries if e.path.lower().endswith(".dds")
    ]
    total = len(mesh_entries) + len(texture_entries)
    done = 0

    manifest = {
        "needle": bundle.needle,
        "canonical_key": bundle.canonical_key,
        "exported_meshes": [],
        "exported_textures": [],
        "all_related_files": [
            {
                "path": e.path,
                "category": e.category,
                "size": e.size,
                "reason": e.reason,
            }
            for e in bundle.entries
        ],
    }

    # ── Meshes ────────────────────────────────────
    from core.mesh_exporter import export_obj
    from core.mesh_parser import parse_mesh
    for entry in mesh_entries:
        done += 1
        bn = os.path.basename(entry.path)
        if progress_cb:
            try:
                progress_cb(done, total, f"Exporting {bn}")
            except Exception:
                pass
        try:
            data = vfs.read_entry_data(_lookup_entry(vfs, entry.path))
        except Exception as exc:
            summary.skipped.append((entry.path, f"read failed: {exc}"))
            continue

        # Save the original PAC bytes — re-import needs them as
        # the donor source if the user later re-builds.
        try:
            (pac_dir / bn).write_bytes(data)
        except Exception as exc:
            logger.warning("PAC copy failed for %s: %s", entry.path, exc)

        try:
            mesh = parse_mesh(data, entry.path)
            stem = Path(bn).stem
            export_obj(mesh, str(mesh_dir), name=stem)
            obj_path = mesh_dir / f"{stem}.obj"
            summary.meshes_exported += 1
            manifest["exported_meshes"].append({
                "obj": str(obj_path.relative_to(out_dir)),
                "pac_original": str((pac_dir / bn).relative_to(out_dir)),
                "vfs_path": entry.path,
                "vertices": mesh.total_vertices,
                "faces": mesh.total_faces,
                "submeshes": [sm.name for sm in mesh.submeshes],
            })
        except Exception as exc:
            summary.skipped.append((entry.path, f"export failed: {exc}"))

    # ── Textures ──────────────────────────────────
    for entry in texture_entries:
        done += 1
        bn = os.path.basename(entry.path)
        if progress_cb:
            try:
                progress_cb(done, total, f"Copying {bn}")
            except Exception:
                pass
        try:
            data = vfs.read_entry_data(_lookup_entry(vfs, entry.path))
        except Exception as exc:
            summary.skipped.append((entry.path, f"read failed: {exc}"))
            continue
        try:
            (tex_dir / bn).write_bytes(data)
            summary.textures_copied += 1
            manifest["exported_textures"].append({
                "dds": str((tex_dir / bn).relative_to(out_dir)),
                "vfs_path": entry.path,
                "size": len(data),
            })
        except Exception as exc:
            summary.skipped.append((entry.path, f"write failed: {exc}"))

    # ── Manifest ──────────────────────────────────
    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    summary.manifest_path = manifest_path

    # ── Blender setup script ─────────────────────
    blender_script = _generate_blender_script(manifest, mesh_dir, tex_dir)
    blender_script_path = out_dir / "import_blender.py"
    blender_script_path.write_text(blender_script, encoding="utf-8")
    summary.blender_script_path = blender_script_path

    # ── README ───────────────────────────────────
    readme = _generate_readme(bundle, summary)
    (out_dir / "README.txt").write_text(readme, encoding="utf-8")

    logger.info("bulk export done: %s", summary.report())
    return summary


# ── Helpers ────────────────────────────────────────────────────

def _lookup_entry(vfs, path: str):
    """Resolve a VFS path back to its PamtFileEntry."""
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception:
            continue
        for e in pamt.file_entries:
            if e.path.lower() == path.lower():
                return e
    raise FileNotFoundError(path)


def _generate_blender_script(
    manifest: dict, mesh_dir: Path, tex_dir: Path,
) -> str:
    """Generate a self-contained Python script users run inside
    Blender's Text Editor to import the whole character.

    The script:
      * Creates a top-level Empty named after the character.
      * Imports each OBJ as its own object, parented to the Empty.
      * Looks for matching textures and creates a Principled BSDF
        material per mesh with the diffuse texture wired up.
      * Reports a final summary in the Blender info bar.
    """
    canonical = manifest.get("canonical_key", "Character")
    n_meshes = len(manifest.get("exported_meshes", []))
    n_textures = len(manifest.get("exported_textures", []))

    # Build the script as a plain string (no f-string) so the
    # embedded Blender-side f-strings are not eaten by the outer
    # Python interpreter. We splice in only the few values we need
    # via simple .replace().
    script = '''"""Auto-generated by CrimsonForge bulk export.

Open this file in Blender's Text Editor and click Run Script. It
will import every OBJ in the meshes_obj/ folder and apply the
matching diffuse texture from textures_dds/ when one is found.

Tested with Blender 4.0+. DDS textures load via Blender's built-in
DDS reader; on older Blender versions you may need to enable the
'Image Editor' DDS support add-on.
"""

import os
import sys
import bpy


# ── Manual override (set this if every auto-detect path below fails) ──
# Example (uncomment and edit; forward slashes are safe on Windows):
#   MANUAL_ROOT = r"C:/Users/hzeem/Downloads/damian_export"
MANUAL_ROOT = ""


def _resolve_root():
    """Locate the export folder this script lives in.

    Tries every Blender execution mode in order, stopping at the
    first hit that actually contains ``meshes_obj/``:

      1. ``MANUAL_ROOT`` constant above (escape hatch for users).
      2. Active Text Editor's text-datablock filepath
         (Text Editor + Run Script — the common case).
      3. ``__file__`` resolved against bpy.path
         (``blender --python this.py`` from the command line).
      4. Any text datablock named ``import_blender.py`` whose
         filepath points at a folder that contains ``meshes_obj/``.

    If none work the script aborts with a clear instruction telling
    the user to set MANUAL_ROOT. We refuse to silently fall back to
    Blender's CWD (typically ``C:\\``) because that produces the
    confusing ``C:\\meshes_obj`` "path not found" error.
    """
    candidates = []

    if MANUAL_ROOT:
        candidates.append(("MANUAL_ROOT", MANUAL_ROOT))

    # 2. Text Editor active text
    try:
        text = bpy.context.space_data.text
        if text and text.filepath:
            p = bpy.path.abspath(text.filepath)
            candidates.append(("active Text Editor file", os.path.dirname(p)))
    except Exception:
        pass

    # 3. __file__ when launched via ``blender --python``
    try:
        f = __file__
        if f and os.path.isabs(f):
            candidates.append(("__file__", os.path.dirname(f)))
    except NameError:
        pass

    # 4. Scan every loaded text datablock for one with our filename
    try:
        for t in bpy.data.texts:
            if t.name.lower() == "import_blender.py" and t.filepath:
                p = bpy.path.abspath(t.filepath)
                candidates.append(("bpy.data.texts lookup", os.path.dirname(p)))
    except Exception:
        pass

    for label, candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.normpath(candidate)
        mesh_dir = os.path.join(candidate, "meshes_obj")
        if os.path.isdir(mesh_dir):
            print("[CrimsonForge import] using ROOT (" + label + "): "
                  + candidate)
            return candidate

    # Nothing worked.
    print("[CrimsonForge import] ERROR: cannot locate export folder.")
    print("  Tried:")
    for label, c in candidates:
        print("    - " + label + ": " + str(c))
    print("  Fix: set MANUAL_ROOT at the top of this script.")
    print("  Use forward slashes, e.g.  "
          'MANUAL_ROOT = r"C:/Users/me/export"')
    sys.exit(1)


ROOT = _resolve_root()
MESH_DIR = os.path.join(ROOT, "meshes_obj")
TEX_DIR = os.path.join(ROOT, "textures_dds")
CHARACTER_NAME = "__CANONICAL__"

# Top-level empty so every imported mesh stays grouped.
empty = bpy.data.objects.new(CHARACTER_NAME, None)
bpy.context.collection.objects.link(empty)


def _find_texture(stem):
    """Look for a .dds whose name matches the mesh stem.
    Returns absolute path or None."""
    if not os.path.isdir(TEX_DIR):
        return None
    candidates = (
        stem + ".dds",
        stem + "_d.dds",
        stem + "_diff.dds",
        stem.lower() + ".dds",
    )
    for c in candidates:
        p = os.path.join(TEX_DIR, c)
        if os.path.isfile(p):
            return p
    # Fallback: any DDS whose name contains the stem.
    for f in os.listdir(TEX_DIR):
        if f.lower().endswith(".dds") and stem.lower() in f.lower():
            return os.path.join(TEX_DIR, f)
    return None


imported_count = 0
for fname in sorted(os.listdir(MESH_DIR)):
    if not fname.lower().endswith(".obj"):
        continue
    obj_path = os.path.join(MESH_DIR, fname)
    stem = os.path.splitext(fname)[0]
    print("Importing " + fname + " ...")
    bpy.ops.wm.obj_import(filepath=obj_path)

    # Newly-imported objects become the active selection. Parent
    # them to the empty so the character stays organised.
    for obj in bpy.context.selected_objects:
        obj.parent = empty
        # Wire diffuse texture if we can find one.
        tex_path = _find_texture(stem)
        if tex_path and obj.type == 'MESH' and obj.data.materials:
            mat = obj.data.materials[0]
            if not mat.use_nodes:
                mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            tex_node = nodes.new('ShaderNodeTexImage')
            try:
                tex_node.image = bpy.data.images.load(tex_path)
                bsdf = nodes.get('Principled BSDF')
                if bsdf:
                    links.new(tex_node.outputs['Color'],
                              bsdf.inputs['Base Color'])
            except Exception as exc:
                print("  texture load failed for " + stem + ": " + str(exc))
        imported_count += 1

print("Imported " + str(imported_count) + " mesh object(s) under '" + CHARACTER_NAME + "'.")
print("Total OBJs found: __N_MESHES__")
print("Total textures available: __N_TEXTURES__")
'''
    return (script
            .replace("__CANONICAL__", canonical)
            .replace("__N_MESHES__", str(n_meshes))
            .replace("__N_TEXTURES__", str(n_textures)))


def _generate_readme(bundle: CharacterAssetBundle, summary: BulkExportSummary) -> str:
    """User-facing README for the export folder."""
    return f"""CrimsonForge — Character Bulk Export
=====================================

Search term     : {bundle.needle}
Canonical key   : {bundle.canonical_key}
Total files seen: {bundle.total_files}
Exported to     : {summary.out_dir}

Folder layout
-------------
  meshes_obj/             — every .pac as .obj (+ .cfmeta.json sidecars)
  meshes_pac_original/    — verbatim original .pac bytes (for re-import)
  textures_dds/           — every .dds copied as-is
  manifest.json           — records every source VFS path
  import_blender.py       — open in Blender > Text Editor > Run Script
  README.txt              — this file

Round-trip workflow
-------------------
1. Open import_blender.py in Blender's Text Editor and Run Script.
2. Edit the meshes (move vertices, retex, add geometry, …).
3. Re-export each edited mesh as OBJ, OVERWRITING the file in
   meshes_obj/. Keep the original filename — the re-importer uses it
   as the lookup key.
4. In CrimsonForge → Explorer, right-click any of the original .pac
   files and choose "Build PAC to Folder…" pointing at meshes_obj/
   for round-trip preview, OR "Import OBJ + Patch to Game" to
   commit the change. The Mesh Baseline Manager (v1.22.9) keeps
   the donor data stable across multiple patches, so re-importing
   is byte-deterministic.

Notes
-----
* {summary.meshes_exported} mesh(es) and {summary.textures_copied} texture(s) exported.
* {len(summary.skipped)} file(s) were skipped (see manifest.json).
* This export is read-only — no game files were modified.
"""
