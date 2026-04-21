"""Character unlock orchestration across all game data files that gate play.

Crimson Desert locks non-Kliff characters out of most of the game through two
string-encoded condition tables:

  gamedata/conditioninfo.pabgb     — NPC dialogue / quest triggers / UI gates.
                                     Contains ``CheckCharacterKey(X)`` as a
                                     compound condition with other checks.
  gamedata/gimmickgroupinfo.pabgb  — World-object interactions (talk / interact
                                     / trigger). CLAUDE.md documents 185
                                     Kliff-gated gimmick conditions here.

Both files use the same byte-level text format, so ``core.condition_patcher``
handles either: it strips ``CheckCharacterKey(X)`` while preserving the
surrounding binary layout (string length prefix, null terminator, adjacent
records). This module is the thin orchestration layer that:

  * Reads both files through VFS, runs the patcher on each,
  * Reports a summary of found vs. patched conditions per file,
  * Optionally pushes the patched bytes back through the full repack pipeline
    (PAZ → PAMT CRC → PAPGT CRC) by delegating to
    ``game_patch_service.repack_relative_files``.

By design this module does **not** perform any live-game RE work (it doesn't
touch memory, vtables, or DLL loading). Those runtime bypasses live in the
tools/character_unlock ASI and are applied separately. This module only
handles the data-file side, so it is safe to call from tests and from the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from core.condition_patcher import (
    ConditionMatch,
    build_patched_expression,
    find_character_conditions,
    patch_conditions,
)
from core.game_patch_service import repack_relative_files
from core.repack_engine import RepackResult
from core.vfs_manager import VfsManager
from utils.logger import get_logger

logger = get_logger("core.character_unlock_service")


# Files that are known to carry CheckCharacterKey expressions. Keep this list
# explicit rather than scanning every table — we don't want to accidentally
# rewrite unrelated game data even if it happens to contain the substring.
UNLOCK_TARGETS: tuple[tuple[str, str], ...] = (
    ("0008", "gamedata/conditioninfo.pabgb"),
    ("0008", "gamedata/gimmickgroupinfo.pabgb"),
)

DEFAULT_CHARACTER_KEYS: tuple[str, ...] = ("Kliff", "Damiane", "Oongka", "Yahn")


@dataclass(slots=True)
class FilePatchSummary:
    """Result of running the patcher against a single data file."""

    relative_path: str               # e.g. "gamedata/conditioninfo.pabgb"
    package_group: str               # e.g. "0008"
    matches: list[ConditionMatch] = field(default_factory=list)
    patched_bytes: bytes | None = None  # None if preview-only
    per_character_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_matches(self) -> int:
        return len(self.matches)


@dataclass(slots=True)
class CharacterUnlockReport:
    """Aggregate result across all target files."""

    files: list[FilePatchSummary] = field(default_factory=list)
    repack: Optional[RepackResult] = None

    @property
    def total_matches(self) -> int:
        return sum(f.total_matches for f in self.files)


def _patch_single_file(
    data: bytes,
    character_keys: tuple[str, ...],
    progress_fn: Callable[[str], None] | None,
) -> tuple[bytes, list[ConditionMatch], dict[str, int]]:
    """Apply the patcher sequentially for every character key.

    Each character key is a separate pass over the (already patched) bytes,
    so conditions that mention more than one key collapse correctly. Because
    every patch preserves byte length, the string offsets from earlier passes
    stay valid for later passes — we're never reshuffling the file, only
    overwriting characters in place.
    """
    current = data
    all_matches: list[ConditionMatch] = []
    per_key_counts: dict[str, int] = {}

    for key in character_keys:
        matches_for_key = find_character_conditions(current, key)
        per_key_counts[key] = len(matches_for_key)
        if not matches_for_key:
            continue
        current, applied = patch_conditions(current, character_key=key, progress_fn=progress_fn)
        all_matches.extend(applied)

    return current, all_matches, per_key_counts


def preview_character_unlock(
    vfs: VfsManager,
    character_keys: tuple[str, ...] = DEFAULT_CHARACTER_KEYS,
) -> CharacterUnlockReport:
    """Dry-run the unlock across every target file without writing back.

    Returns a report describing how many conditions would be patched per file
    and per character. The UI uses this for a before/after confirmation
    dialog — never surprise the user with a silent bulk rewrite.
    """
    report = CharacterUnlockReport()

    for group, relative_path in UNLOCK_TARGETS:
        try:
            pamt = vfs.load_pamt(group)
        except Exception as exc:
            logger.warning("Could not load PAMT for group %s: %s", group, exc)
            continue

        entry = None
        lowered = relative_path.lower()
        for candidate in pamt.file_entries:
            if candidate.path.replace("\\", "/").lower() == lowered:
                entry = candidate
                break

        if entry is None:
            logger.info("%s not present in group %s; skipping", relative_path, group)
            continue

        data = vfs.read_entry_data(entry)
        patched, matches, per_key = _patch_single_file(data, character_keys, progress_fn=None)

        report.files.append(
            FilePatchSummary(
                relative_path=relative_path,
                package_group=group,
                matches=matches,
                # Only keep the patched bytes if they actually differ, so that
                # the commit path can skip no-op repacks cleanly.
                patched_bytes=patched if patched != data else None,
                per_character_counts=per_key,
            )
        )

    return report


def apply_character_unlock(
    packages_path: str,
    vfs: VfsManager,
    character_keys: tuple[str, ...] = DEFAULT_CHARACTER_KEYS,
    create_backup: bool = True,
    backup_dir: str = "",
    preserve_timestamps: bool = True,
    verify_after: bool = True,
    progress_callback: Callable[[int, str], None] | None = None,
) -> CharacterUnlockReport:
    """Patch every target file and repack it into the live archives.

    This is the "Apply" button for the Quick Mods entry. All files that
    actually change are bundled into a single RepackEngine run so the
    PAMT / PAPGT checksum chain is only walked once.
    """
    def sub_progress(msg: str) -> None:
        if progress_callback:
            progress_callback(-1, msg)

    preview = preview_character_unlock(vfs, character_keys=character_keys)
    replacements: dict[str, bytes] = {}
    for summary in preview.files:
        if summary.patched_bytes is not None:
            replacements[summary.relative_path] = summary.patched_bytes
            sub_progress(
                f"{summary.relative_path}: {summary.total_matches} condition(s) "
                f"ready to rewrite"
            )

    if not replacements:
        logger.info("Character unlock: nothing to patch — all target files already clean")
        return preview

    preview.repack = repack_relative_files(
        packages_path=packages_path,
        replacements=replacements,
        create_backup=create_backup,
        backup_dir=backup_dir,
        preserve_timestamps=preserve_timestamps,
        verify_after=verify_after,
        progress_callback=progress_callback,
    )
    return preview


def format_preview_report(report: CharacterUnlockReport) -> str:
    """Human-readable summary for the confirmation dialog / logs."""
    if not report.files:
        return "No target files were reachable through VFS."

    lines = ["Character unlock preview:", ""]
    for summary in report.files:
        lines.append(f"[{summary.relative_path}]")
        if summary.total_matches == 0:
            lines.append("    (no CheckCharacterKey entries — already clean)")
            lines.append("")
            continue
        for key, count in summary.per_character_counts.items():
            if count:
                lines.append(f"    {key:<10s} {count:4d} condition(s)")
        lines.append(f"    TOTAL       {summary.total_matches:4d}")
        lines.append("")

    lines.append(f"Grand total: {report.total_matches} condition(s) will be rewritten.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Byte-level helpers exposed for tests / tooling — intentionally simple.
# ---------------------------------------------------------------------------

def synthesize_condition_blob(expressions: list[str]) -> bytes:
    """Produce a byte blob matching the on-disk pattern [strlen:u32][chars]\0.

    Used exclusively by tests so we can round-trip condition patching without
    needing a real game install. The layout matches what parse_pabgb produces
    for string fields (see core/pabgb_parser.py)."""
    import struct

    buf = bytearray()
    for expr in expressions:
        encoded = expr.encode("ascii")
        buf.extend(struct.pack("<I", len(encoded)))
        buf.extend(encoded)
        buf.append(0)  # null terminator
    return bytes(buf)


def read_condition_expressions(blob: bytes) -> list[str]:
    """Re-parse a blob produced by ``synthesize_condition_blob``."""
    import struct

    out: list[str] = []
    pos = 0
    while pos + 4 <= len(blob):
        length = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        if length == 0 or pos + length > len(blob):
            break
        out.append(blob[pos:pos + length].decode("ascii", errors="replace"))
        pos += length
        # Skip optional trailing null.
        if pos < len(blob) and blob[pos] == 0:
            pos += 1
    return out


def patch_expressions(
    expressions: list[str],
    character_keys: tuple[str, ...] = DEFAULT_CHARACTER_KEYS,
) -> list[str]:
    """Apply ``condition_patcher`` to an in-memory list of expressions.

    The blob form is the real disk layout. This helper serializes, runs the
    patcher, and deserializes, so tests don't have to touch byte offsets.
    """
    blob = synthesize_condition_blob(expressions)
    patched, _matches, _counts = _patch_single_file(blob, character_keys, progress_fn=None)
    return read_condition_expressions(patched)
