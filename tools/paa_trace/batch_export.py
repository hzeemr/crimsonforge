"""Batch PAA -> FBX exporter.

Walks every .paa in a package group (or a filter matching a glob),
finds the matching .pab skeleton (either via the PAA's embedded
``%character/.../phm_01.pab`` link target or a CLI-specified global
fallback), then runs the :class:`AnimationExportPipeline`.

Writes one ``.fbx`` and one ``.pipeline.json`` per source file to
the output directory. The JSON captures every decision the
pipeline made so failures are easy to triage without re-running
the whole batch.

Usage:

    python -m tools.paa_trace.batch_export \
        "C:/Program Files (x86)/Steam/steamapps/common/Crimson Desert" \
        --group 0009 --limit 50 \
        --out exports/paa_batch \
        --fallback-pab character/phm_01.pab

The fallback PAB is used when a PAA either:
  * doesn't carry a link target (real animations, not references),
  * carries a target we can't resolve in the VFS.

Anyone who wants to round-trip a specific character/creature can
run twice with a different ``--fallback-pab``.

CSV summary at the end:

    path, fbx_path, tracks, frames, duration, variant, status

Failed entries carry ``status=error: <reason>`` so they can be
grepped out of the CSV with one shell command.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import traceback
from pathlib import Path

# Allow running as a plain script from the repo root ("python tools/...").
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.animation_export_pipeline import AnimationExportPipeline  # noqa: E402
from core.animation_parser import parse_paa  # noqa: E402
from core.pamt_parser import PamtData, PamtFileEntry  # noqa: E402
from core.vfs_manager import VfsManager  # noqa: E402


# ----------------------------- CLI --------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_root")
    ap.add_argument("--group", default="0009", help="package group (default 0009)")
    ap.add_argument("--filter", default="", help="substring filter on the PAA path (case-insensitive)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (0 = all)")
    ap.add_argument("--out", default="exports/paa_batch", help="output directory")
    ap.add_argument("--fallback-pab", default="character/phm_01.pab",
                    help="PAB to use when the PAA has no link target")
    ap.add_argument("--skip-existing", action="store_true",
                    help="don't overwrite FBX files that already exist")
    return ap.parse_args()


# --------------------------- helpers ------------------------------

def _path_index(pamt: PamtData) -> dict[str, PamtFileEntry]:
    """Lower-case path -> entry lookup for fast link resolution."""
    return {e.path.lower(): e for e in pamt.file_entries}


def _resolve_link_pab(vfs: VfsManager, link: str) -> bytes | None:
    """Resolve a ``%character/.../something.pab`` link target by
    searching every loaded PAMT. Returns the raw decrypted +
    decompressed bytes, or ``None`` if the link doesn't resolve.
    """
    if not link:
        return None
    target = link.lstrip("%").replace("\\", "/").lower()
    # Search every available group — some skeletons live in 0009,
    # others may live in dedicated asset groups.
    for g in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(g)
        except Exception:
            continue
        idx = _path_index(pamt)
        if target in idx:
            try:
                return vfs.read_entry_data(idx[target])
            except Exception:
                return None
        # Also try just the basename — some link targets include
        # subfolders we don't always keep intact.
        base = os.path.basename(target)
        for p, ent in idx.items():
            if os.path.basename(p) == base:
                try:
                    return vfs.read_entry_data(ent)
                except Exception:
                    return None
    return None


# ----------------------------- main -------------------------------

def main() -> int:
    args = _parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    vfs = VfsManager(args.game_root)
    vfs.load_papgt()
    pamt = vfs.load_pamt(args.group)
    path_idx = _path_index(pamt)

    # Resolve the global fallback PAB once — every PAA that lacks a
    # link target shares it.
    fallback_pab: bytes | None = None
    fallback_key = args.fallback_pab.lower()
    if fallback_key in path_idx:
        fallback_pab = vfs.read_entry_data(path_idx[fallback_key])
    elif args.fallback_pab:
        # User provided a local path — read from disk.
        if os.path.isfile(args.fallback_pab):
            with open(args.fallback_pab, "rb") as f:
                fallback_pab = f.read()

    if fallback_pab:
        print(f"# fallback PAB loaded ({len(fallback_pab)} bytes)")
    else:
        print("# WARNING: no fallback PAB — PAAs without a link target will fail")

    paa_entries = sorted(
        [e for e in pamt.file_entries if e.path.lower().endswith(".paa")],
        key=lambda e: e.path,
    )
    if args.filter:
        needle = args.filter.lower()
        paa_entries = [e for e in paa_entries if needle in e.path.lower()]
    if args.limit and args.limit < len(paa_entries):
        step = max(1, len(paa_entries) // args.limit)
        paa_entries = paa_entries[::step][:args.limit]

    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "_index.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as csvf:
        writer = csv.writer(csvf)
        writer.writerow(["path", "fbx_path", "tracks", "frames",
                         "duration", "variant", "status"])

        n_ok = n_skipped = n_error = 0
        for i, e in enumerate(paa_entries, 1):
            stem = os.path.splitext(os.path.basename(e.path))[0]
            fbx_out = os.path.join(args.out, f"{stem}.fbx")
            if args.skip_existing and os.path.isfile(fbx_out):
                n_skipped += 1
                writer.writerow([e.path, fbx_out, "", "", "", "", "skipped"])
                continue
            try:
                data = vfs.read_entry_data(e)
            except Exception as ex:
                writer.writerow([e.path, "", "", "", "", "", f"error: read {ex}"])
                n_error += 1
                continue

            # Pick the right PAB: link-target first, fallback second.
            pab_data: bytes | None = None
            try:
                anim_preview = parse_paa(data, e.path)
                if anim_preview.is_link and anim_preview.link_target:
                    pab_data = _resolve_link_pab(vfs, anim_preview.link_target)
            except Exception:
                pass
            if pab_data is None:
                pab_data = fallback_pab
            if pab_data is None:
                writer.writerow([e.path, "", "", "", "", "", "error: no skeleton"])
                n_error += 1
                continue

            # Run the export pipeline. We intentionally use
            # bone_mapping="sequential" — the smart heuristic is
            # unreliable on first-keyframe data (see pipeline docstring).
            try:
                pipe = AnimationExportPipeline(
                    paa_data=data, pab_data=pab_data, paa_path=e.path,
                )
                res = pipe.export(output_dir=args.out, name=stem,
                                  bone_mapping="sequential")
                writer.writerow([
                    e.path, res.fbx_path,
                    res.animation.bone_count, res.animation.frame_count,
                    f"{res.animation.duration:.3f}",
                    res.animation.format_variant, "ok",
                ])
                n_ok += 1
            except Exception as ex:
                tb = traceback.format_exc(limit=1).strip().splitlines()[-1]
                writer.writerow([e.path, "", "", "", "", "",
                                 f"error: {type(ex).__name__} {tb}"])
                n_error += 1

            if i % 25 == 0 or i == len(paa_entries):
                print(f"  [{i}/{len(paa_entries)}] ok={n_ok} err={n_error} skipped={n_skipped}", flush=True)

    print()
    print(f"Wrote: {csv_path}")
    print(f"Summary: ok={n_ok}  errors={n_error}  skipped={n_skipped}  total={len(paa_entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
