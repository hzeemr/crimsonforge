"""Original baseline manager for translation projects.

Saves the original untouched game text per language to a baseline file.
This baseline is NEVER modified — it serves as the reference for:
- Detecting new/changed/removed strings when the game updates
- Reverting individual translations to original
- Comparing current game text vs original

Baseline files are stored at ~/.crimsonforge/baselines/<filename>.json
"""

import json
import os
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("translation.baseline")

BASELINE_DIR = os.path.join(os.path.expanduser("~"), ".crimsonforge", "baselines")


class BaselineManager:
    """Manages original game text baselines per paloc file."""

    def __init__(self):
        Path(BASELINE_DIR).mkdir(parents=True, exist_ok=True)

    def _baseline_path(self, paloc_filename: str) -> str:
        safe_name = paloc_filename.replace("/", "_").replace("\\", "_")
        return os.path.join(BASELINE_DIR, f"{safe_name}.baseline.json")

    def has_baseline(self, paloc_filename: str) -> bool:
        return os.path.isfile(self._baseline_path(paloc_filename))

    def save_baseline(
        self,
        paloc_filename: str,
        entries: list[tuple[str, str]],
        lang_code: str,
        build_id: str = "",
        build_display: str = "",
        game_fingerprint: str = "",
    ) -> str:
        """Save original game text as baseline. Only saves if no baseline exists yet.

        Args:
            paloc_filename: e.g. "localizationstring_eng.paloc"
            entries: list of (key, value) from paloc parser
            lang_code: language code

        Returns:
            Path to baseline file.
        """
        path = self._baseline_path(paloc_filename)
        if os.path.isfile(path):
            logger.info("Baseline already exists: %s", path)
            return path

        data = {
            "version": "1.0.0",
            "paloc_filename": paloc_filename,
            "lang_code": lang_code,
            "game_build_id": build_id,
            "game_build_display": build_display or build_id,
            "game_fingerprint": game_fingerprint,
            "entry_count": len(entries),
            "entries": {key: value for key, value in entries},
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        logger.info("Baseline saved: %s (%d entries)", path, len(entries))
        return path

    def force_save_baseline(
        self,
        paloc_filename: str,
        entries: list[tuple[str, str]],
        lang_code: str,
        build_id: str = "",
        build_display: str = "",
        game_fingerprint: str = "",
    ) -> str:
        """Force overwrite baseline (for when game updates)."""
        path = self._baseline_path(paloc_filename)
        data = {
            "version": "1.0.0",
            "paloc_filename": paloc_filename,
            "lang_code": lang_code,
            "game_build_id": build_id,
            "game_build_display": build_display or build_id,
            "game_fingerprint": game_fingerprint,
            "entry_count": len(entries),
            "entries": {key: value for key, value in entries},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info("Baseline force-saved: %s (%d entries)", path, len(entries))
        return path

    def merge_into_baseline(
        self,
        paloc_filename: str,
        entries: list[tuple[str, str]],
        lang_code: str,
        build_id: str = "",
        build_display: str = "",
        game_fingerprint: str = "",
    ) -> dict[str, int | str]:
        """Preserve the first-seen baseline text and only add newly discovered keys."""
        path = self._baseline_path(paloc_filename)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "version": "1.0.0",
                "paloc_filename": paloc_filename,
                "lang_code": lang_code,
                "game_build_id": build_id,
                "game_build_display": build_display or build_id,
                "game_fingerprint": game_fingerprint,
                "entry_count": 0,
                "entries": {},
            }

        baseline_entries = dict(data.get("entries", {}))
        added = 0
        for key, value in entries:
            if key not in baseline_entries:
                baseline_entries[key] = value
                added += 1

        data["version"] = "1.0.0"
        data["paloc_filename"] = paloc_filename
        data["lang_code"] = lang_code
        if not data.get("game_build_id"):
            data["game_build_id"] = build_id
        if not data.get("game_build_display"):
            data["game_build_display"] = build_display or build_id
        if not data.get("game_fingerprint"):
            data["game_fingerprint"] = game_fingerprint
        data["last_merge_build_id"] = build_id
        data["last_merge_build_display"] = build_display or build_id
        data["last_merge_game_fingerprint"] = game_fingerprint
        data["entry_count"] = len(baseline_entries)
        data["entries"] = baseline_entries

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        logger.info(
            "Baseline merged: %s (%d total, %d new keys added)",
            path,
            len(baseline_entries),
            added,
        )
        return {
            "total": len(baseline_entries),
            "added": added,
            "path": path,
        }

    def load_baseline(self, paloc_filename: str) -> Optional[dict[str, str]]:
        """Load baseline entries as {key: original_text} dict.

        Returns None if no baseline exists.
        """
        path = self._baseline_path(paloc_filename)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {})

    def get_baseline_metadata(self, paloc_filename: str) -> dict[str, str]:
        """Load baseline metadata without the full entry payload."""
        path = self._baseline_path(paloc_filename)
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "game_build_id": data.get("game_build_id", ""),
            "game_build_display": data.get("game_build_display", data.get("game_build_id", "")),
            "game_fingerprint": data.get("game_fingerprint", ""),
            "lang_code": data.get("lang_code", ""),
            "paloc_filename": data.get("paloc_filename", paloc_filename),
        }

    def diff_with_current(self, paloc_filename: str,
                          current_entries: list[tuple[str, str]]) -> dict:
        """Compare current game text with saved baseline.

        Returns:
            {
                "new": [(key, value), ...],       # keys in current but not baseline
                "removed": [(key, value), ...],   # keys in baseline but not current
                "changed": [(key, old, new), ...], # keys with different values
                "unchanged": int,                  # count of unchanged entries
            }
        """
        baseline = self.load_baseline(paloc_filename)
        if baseline is None:
            return {"new": current_entries, "removed": [], "changed": [], "unchanged": 0}

        current_dict = {k: v for k, v in current_entries}
        result = {"new": [], "removed": [], "changed": [], "unchanged": 0}

        for key, value in current_entries:
            if key not in baseline:
                result["new"].append((key, value))
            elif baseline[key] != value:
                result["changed"].append((key, baseline[key], value))
            else:
                result["unchanged"] += 1

        for key, value in baseline.items():
            if key not in current_dict:
                result["removed"].append((key, value))

        return result

    def get_original_text(self, paloc_filename: str, key: str) -> Optional[str]:
        """Get the original baseline text for a single key."""
        baseline = self.load_baseline(paloc_filename)
        if baseline is None:
            return None
        return baseline.get(key)
