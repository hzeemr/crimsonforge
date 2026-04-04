"""Translation workspace tab for AI and manual translation work.

Auto-detects all paloc localization files from the loaded game.
Source language dropdown shows only the game's actual languages.
Destination language dropdown shows all world languages.
Includes "Patch to Game" button that exports, repacks, and updates
the full checksum chain so the game is ready to play immediately.
"""

import os
import json
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QGroupBox, QApplication, QProgressBar,
)
from PySide6.QtCore import QSignalBlocker

from core.vfs_manager import VfsManager
from core.paloc_parser import parse_paloc, build_paloc, PalocEntry
from core.game_patch_service import patch_translation_to_game
from core.crypto_engine import encrypt
from core.compression_engine import compress
from core.checksum_engine import pa_checksum
from core.pamt_parser import (
    parse_pamt, PamtData, PamtFileEntry,
    update_pamt_paz_entry, update_pamt_file_entry, update_pamt_self_crc,
)
from core.papgt_manager import (
    parse_papgt, get_pamt_crc_offset,
    update_papgt_pamt_crc, update_papgt_self_crc,
)
from core.backup_manager import BackupManager
from translation.language_config import LanguageConfig
from translation.translation_state import TranslationEntry, StringStatus
from translation.translation_project import TranslationProject
from translation.translation_batch import TranslationBatchProcessor
from translation.translation_export import TranslationExporter
from translation.localization_usage_index import (
    CATEGORY_ORDER,
    CATEGORY_UNCATEGORIZED,
    LocalizationUsageIndex,
)
from translation.autosave_manager import AutosaveManager
from translation.baseline_manager import BaselineManager
from translation.glossary_manager import GlossaryManager
from ai.provider_registry import ProviderRegistry, PROVIDER_CLASSES
from ai.model_loader import ModelLoader
from ai.prompt_manager import PromptManager
from ai.translation_engine import TranslationEngine
from ai.pricing_registry import format_cost, format_tokens
from ui.widgets.translation_table import TranslationTableWidget
from ui.widgets.progress_widget import ProgressWidget
from ui.dialogs.file_picker import pick_save_file, pick_file
from ui.dialogs.confirmation import show_error, show_info, confirm_action
from ui.dialogs.patch_progress import PatchProgressDialog
from utils.thread_worker import FunctionWorker
from utils.platform_utils import (
    pad_to_16, get_file_timestamps, set_file_timestamps, atomic_write,
)
from utils.logger import get_logger

logger = get_logger("ui.tab_translate")


class TranslateTab(QWidget):
    """Translation workspace with auto-detected game languages."""

    def __init__(self, config, registry: ProviderRegistry, parent=None):
        super().__init__(parent)
        self._config = config
        self._registry = registry
        self._lang_config = LanguageConfig()
        self._model_loader = ModelLoader(registry)
        self._prompt_manager = PromptManager()
        self._prompt_manager.update_from_config(config.get_section("translation"))
        self._project = TranslationProject()
        self._batch_processor = None
        self._worker: FunctionWorker = None
        self._vfs: VfsManager = None
        self._usage_index = None
        self._discovered_palocs: list[dict] = []
        self._packages_path = ""
        self._baseline_mgr = BaselineManager()
        self._glossary_mgr = GlossaryManager()

        self._autosave = AutosaveManager(
            interval_seconds=config.get("translation.autosave_interval_seconds", 30),
            parent=self,
        )
        self._autosave.autosaved.connect(
            lambda time_str: self._update_autosave_indicator(last_save_time=time_str)
        )
        self._autosave.autosave_error.connect(lambda e: logger.error(e))

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        lang_row = QHBoxLayout()

        src_group = QGroupBox("Source Language")
        src_layout = QVBoxLayout(src_group)
        self._source_combo = QComboBox()
        self._source_combo.setToolTip(
            "Select the game language to load as the source text for translation.\n"
            "Languages are auto-detected from game archives when the game is loaded."
        )
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        src_layout.addWidget(self._source_combo)
        self._paloc_info = QLabel("No game loaded")
        self._paloc_info.setToolTip("Shows the currently loaded localization file, group, and detection info.")
        self._paloc_info.setStyleSheet("font-size: 11px; color: #a6adc8;")
        src_layout.addWidget(self._paloc_info)
        lang_row.addWidget(src_group)

        dst_group = QGroupBox("Destination Language")
        dst_layout = QVBoxLayout(dst_group)
        self._target_combo = QComboBox()
        self._target_combo.setToolTip(
            "Select the language you are translating into.\n"
            "This determines the AI prompt language and the output paloc file."
        )
        all_langs = self._lang_config.get_all_languages()
        for lang in all_langs:
            self._target_combo.addItem(lang.name, lang.code)
        self._target_combo.currentIndexChanged.connect(self._on_target_lang_changed)
        dst_layout.addWidget(self._target_combo)
        lang_row.addWidget(dst_group)

        ai_group = QGroupBox("AI Provider")
        ai_layout = QVBoxLayout(ai_group)
        self._provider_combo = QComboBox()
        self._provider_combo.setToolTip(
            "Select the AI provider and model for automatic translation.\n"
            "Configure API keys and default models in Settings tab → AI Providers."
        )
        ai_layout.addWidget(self._provider_combo)
        self._ai_info_label = QLabel("Model from Settings")
        self._ai_info_label.setToolTip("Shows the default model for the selected provider. Change in Settings → AI Providers.")
        self._ai_info_label.setStyleSheet("font-size: 11px; color: #a6adc8;")
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        ai_layout.addWidget(self._ai_info_label)
        lang_row.addWidget(ai_group)

        layout.addLayout(lang_row)

        ctrl_row = QHBoxLayout()
        self._load_btn = QPushButton("Load Selected Language")
        self._load_btn.setObjectName("primary")
        self._load_btn.setToolTip(
            "Load the selected source language from game archives.\n"
            "Merges with any saved translations from previous sessions automatically."
        )
        self._load_btn.clicked.connect(self._load_selected_paloc)
        ctrl_row.addWidget(self._load_btn)
        self._glossary_btn = QPushButton("Glossary")
        self._glossary_btn.setToolTip(
            "Open glossary editor for proper nouns (names, places, factions).\n"
            "Ensures consistent translation across all game text."
        )
        self._glossary_btn.clicked.connect(self._open_glossary)
        ctrl_row.addWidget(self._glossary_btn)
        self._glossary_status = QLabel("")
        self._glossary_status.setToolTip("Shows the number of glossary terms loaded for consistent translation.")
        self._glossary_status.setStyleSheet("font-size: 11px; color: #a6adc8;")
        ctrl_row.addWidget(self._glossary_status)
        self._auto_translate_btn = QPushButton("Auto Translate All")
        self._auto_translate_btn.setObjectName("primary")
        self._auto_translate_btn.setToolTip(
            "Start automatic AI translation of all pending entries.\n"
            "Locked and already-translated entries are skipped.\n"
            "Uses the selected provider and model above."
        )
        self._auto_translate_btn.clicked.connect(self._auto_translate_all)
        ctrl_row.addWidget(self._auto_translate_btn)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setToolTip("Pause the running batch translation. Resume anytime without losing progress.")
        self._pause_btn.clicked.connect(self._pause_translate)
        self._pause_btn.setEnabled(False)
        ctrl_row.addWidget(self._pause_btn)
        self._resume_btn = QPushButton("Resume")
        self._resume_btn.setToolTip("Resume the paused batch translation from where it stopped.")
        self._resume_btn.clicked.connect(self._resume_translate)
        self._resume_btn.setEnabled(False)
        ctrl_row.addWidget(self._resume_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setToolTip("Stop and cancel the batch translation. Already completed translations are kept.")
        self._stop_btn.clicked.connect(self._stop_translate)
        self._stop_btn.setEnabled(False)
        ctrl_row.addWidget(self._stop_btn)
        ctrl_row.addStretch()
        self._batch_progress_label = QLabel("")
        self._batch_progress_label.setToolTip("Real-time progress of the running batch translation.")
        ctrl_row.addWidget(self._batch_progress_label)
        layout.addLayout(ctrl_row)

        self._table = TranslationTableWidget(config=self._config)
        self._table.translation_edited.connect(self._on_translation_edited)
        self._table.ai_requested.connect(self._on_ai_requested)
        self._table.ai_batch_requested.connect(self._on_ai_batch_selected)
        self._table.status_changed.connect(self._on_status_changed)
        self._table.entry_double_clicked.connect(self._on_entry_double_clicked)
        layout.addWidget(self._table, 1)

        from PySide6.QtWidgets import QFrame
        status_bar = QFrame()
        status_bar.setObjectName("translateStatusBar")
        status_bar.setStyleSheet(
            "QFrame#translateStatusBar {"
            "  background-color: #181825;"
            "  border: 1px solid #313244;"
            "  border-radius: 8px;"
            "  padding: 2px 4px;"
            "}"
        )
        status_bar.setFixedHeight(36)
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(8, 2, 8, 2)
        sb_layout.setSpacing(6)

        # Game version badge
        self._game_version_label = QLabel("")
        self._game_version_label.setToolTip("Game build fingerprint: CRC hash and last modified date of game data.")
        self._game_version_label.setStyleSheet(
            "font-size: 11px; color: #6c7086; padding: 0 6px;"
            "border-right: 1px solid #313244;"
        )
        sb_layout.addWidget(self._game_version_label)

        self._sync_summary_label = QLabel("Text Sync: waiting")
        self._sync_summary_label.setToolTip(
            "Shows the latest tracked game-text sync for this project,\n"
            "including which build added, changed, or removed strings."
        )
        self._sync_summary_label.setStyleSheet(
            "font-size: 11px; color: #89b4fa; padding: 0 6px;"
            "border-right: 1px solid #313244;"
        )
        sb_layout.addWidget(self._sync_summary_label)

        # Progress bar (compact)
        self._stats_progress = QProgressBar()
        self._stats_progress.setToolTip(
            "Overall translation progress.\n"
            "Counts translated + reviewed + approved entries out of total."
        )
        self._stats_progress.setFixedHeight(14)
        self._stats_progress.setTextVisible(True)
        self._stats_progress.setFormat("%v / %m  (%p%)")
        self._stats_progress.setMinimumWidth(180)
        sb_layout.addWidget(self._stats_progress, 1)

        # Vertical divider
        _div1 = QFrame(); _div1.setFrameShape(QFrame.VLine)
        _div1.setStyleSheet("color: #313244;"); _div1.setFixedWidth(1)
        sb_layout.addWidget(_div1)

        # Status badges — Pending
        self._badge_pending = QLabel("⊙  Pending: 0")
        self._badge_pending.setToolTip("Entries waiting for translation.\nThese will be sent to AI when you click Auto Translate.")
        self._badge_pending.setStyleSheet(
            "font-size: 11px; font-weight: 600;"
            "color: #9399b2; background: #1e1e2e;"
            "border-radius: 4px; padding: 1px 8px;"
        )
        sb_layout.addWidget(self._badge_pending)

        # Translated
        self._badge_translated = QLabel("◆  Translated: 0")
        self._badge_translated.setToolTip("Entries translated by AI or manually.\nReady for human review.")
        self._badge_translated.setStyleSheet(
            "font-size: 11px; font-weight: 600;"
            "color: #89b4fa; background: #1e1e2e;"
            "border-radius: 4px; padding: 1px 8px;"
        )
        sb_layout.addWidget(self._badge_translated)

        # Reviewed
        self._badge_reviewed = QLabel("★  Reviewed: 0")
        self._badge_reviewed.setToolTip("Entries reviewed by a human.\nReady for final approval.")
        self._badge_reviewed.setStyleSheet(
            "font-size: 11px; font-weight: 600;"
            "color: #f9e2af; background: #1e1e2e;"
            "border-radius: 4px; padding: 1px 8px;"
        )
        sb_layout.addWidget(self._badge_reviewed)

        # Approved
        self._badge_approved = QLabel("✔  Approved: 0")
        self._badge_approved.setToolTip("Entries approved and finalized.\nReady to patch into the game.")
        self._badge_approved.setStyleSheet(
            "font-size: 11px; font-weight: 600;"
            "color: #a6e3a1; background: #1e1e2e;"
            "border-radius: 4px; padding: 1px 8px;"
        )
        sb_layout.addWidget(self._badge_approved)

        # Vertical divider
        _div2 = QFrame(); _div2.setFrameShape(QFrame.VLine)
        _div2.setStyleSheet("color: #313244;"); _div2.setFixedWidth(1)
        sb_layout.addWidget(_div2)

        # Tokens / cost
        self._stats_label = QLabel("Tokens: 0  |  Cost: $0.00")
        self._stats_label.setToolTip(
            "Cumulative AI usage for this session.\n"
            "Tokens: total consumed (input + output).\n"
            "Cost: estimated USD based on provider pricing."
        )
        self._stats_label.setStyleSheet("font-size: 11px; color: #6c7086; padding: 0 4px;")
        sb_layout.addWidget(self._stats_label)

        # Vertical divider
        _div3 = QFrame(); _div3.setFrameShape(QFrame.VLine)
        _div3.setStyleSheet("color: #313244;"); _div3.setFixedWidth(1)
        sb_layout.addWidget(_div3)

        # Autosave indicator
        self._autosave_label = QLabel("● Autosave: On")
        self._autosave_label.setToolTip(
            "Autosave runs every 30 seconds.\n"
            "Green dot = last save successful. Your work is safe."
        )
        self._autosave_label.setStyleSheet(
            "font-size: 11px; color: #a6e3a1; padding: 0 4px;"
        )
        sb_layout.addWidget(self._autosave_label)

        layout.addWidget(status_bar)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)

        clear_sel_btn = QPushButton("Clear Selected")
        clear_sel_btn.setObjectName("danger")
        clear_sel_btn.setToolTip("Clear translation text for the selected rows.\nResets them to Pending status. Locked entries are protected.")
        clear_sel_btn.clicked.connect(self._clear_selected)
        bottom_row.addWidget(clear_sel_btn)
        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.setObjectName("danger")
        clear_all_btn.setToolTip("Clear ALL translations and reset to Pending.\nLocked entries are protected. This cannot be undone.")
        clear_all_btn.clicked.connect(self._clear_all)
        bottom_row.addWidget(clear_all_btn)
        revert_sel_btn = QPushButton("Revert Selected")
        revert_sel_btn.setObjectName("warning")
        revert_sel_btn.setToolTip("Revert selected entries to the original baseline text.\nUseful when the game updates and source text changes.")
        revert_sel_btn.clicked.connect(self._revert_selected)
        bottom_row.addWidget(revert_sel_btn)

        self._add_separator(bottom_row)

        export_json_btn = QPushButton("Export JSON")
        export_json_btn.setToolTip("Export translations to a JSON file for external editing or backup.\nIncludes keys, original text, translations, and status.")
        export_json_btn.clicked.connect(self._export_json)
        bottom_row.addWidget(export_json_btn)
        import_json_btn = QPushButton("Import JSON")
        import_json_btn.setToolTip("Import translations from a JSON file.\nMerges by key — existing translations are updated, new ones are added.")
        import_json_btn.clicked.connect(self._import_json)
        bottom_row.addWidget(import_json_btn)

        self._add_separator(bottom_row)

        load_project_btn = QPushButton("Load Project...")
        load_project_btn.setToolTip("Load a previously saved translation project (.json).\nRestores all entries, translations, statuses, and metadata.")
        load_project_btn.clicked.connect(self._load_project)
        bottom_row.addWidget(load_project_btn)
        save_project_btn = QPushButton("Save Project")
        save_project_btn.setToolTip("Save the current translation project to a file.\nIncludes all entries, translations, statuses, and AI metadata.")
        save_project_btn.clicked.connect(self._save_project)
        bottom_row.addWidget(save_project_btn)
        export_btn = QPushButton("Export Paloc")
        export_btn.setToolTip("Export a standalone translated .paloc file.\nUse 'Patch to Game' instead to apply directly to game archives.")
        export_btn.clicked.connect(self._export_paloc)
        bottom_row.addWidget(export_btn)

        self._patch_btn = QPushButton("  Patch to Game  ")
        self._patch_btn.setObjectName("success")
        self._patch_btn.setToolTip(
            "Apply translations directly to the game:\n"
            "1. Builds the translated .paloc file\n"
            "2. Repacks it into the game's PAZ archives\n"
            "3. Updates PAMT index and PAPGT checksum chain\n"
            "4. Game is ready to play with your translations!"
        )
        self._patch_btn.clicked.connect(self._patch_to_game)
        bottom_row.addWidget(self._patch_btn)

        self._ship_btn = QPushButton("  Ship to App  ")
        self._ship_btn.setObjectName("primary")
        self._ship_btn.setToolTip(
            "Generate a standalone installer for end users.\n"
            "Packages your translation + font into a single .exe\n"
            "that end users run to install/uninstall the mod.\n"
            "Supports auto-updates, custom icon, and vanilla revert."
        )
        self._ship_btn.clicked.connect(self._ship_to_app)
        bottom_row.addWidget(self._ship_btn)

        layout.addLayout(bottom_row)

        self._progress = ProgressWidget()
        layout.addWidget(self._progress)
        self.refresh_from_settings(preserve_selection=False)

    def refresh_from_settings(self, preserve_selection: bool = True) -> None:
        """Refresh provider choices and translation behavior after Settings changes."""
        self._prompt_manager.update_from_config(self._config.get_section("translation"))
        self._autosave.set_enabled(self._config.get("translation.autosave_enabled", True))
        self._autosave.set_interval(self._config.get("translation.autosave_interval_seconds", 30))
        self._refresh_provider_combo(preserve_selection=preserve_selection)
        self._update_autosave_indicator()

    def _refresh_provider_combo(self, preserve_selection: bool = True) -> None:
        previous_provider_id = self._provider_combo.currentData() if preserve_selection else ""
        blocker = QSignalBlocker(self._provider_combo)
        self._provider_combo.clear()

        first_enabled_index = -1
        restore_index = -1
        for provider_id in PROVIDER_CLASSES:
            try:
                provider = self._registry.get_provider(provider_id)
            except ValueError:
                continue

            enabled = self._registry.is_enabled(provider_id)
            label = self._build_provider_label(provider_id, provider.name, enabled)
            self._provider_combo.addItem(label, provider_id)
            row = self._provider_combo.count() - 1
            if enabled and first_enabled_index < 0:
                first_enabled_index = row
            if provider_id == previous_provider_id:
                restore_index = row

        if restore_index >= 0:
            self._provider_combo.setCurrentIndex(restore_index)
        elif first_enabled_index >= 0:
            self._provider_combo.setCurrentIndex(first_enabled_index)
        elif self._provider_combo.count() > 0:
            self._provider_combo.setCurrentIndex(0)

        del blocker
        self._on_provider_changed()

    def _build_provider_label(self, provider_id: str, provider_name: str, enabled: bool) -> str:
        default_model = self._config.get(f"ai_providers.{provider_id}.default_model", "")
        details = []
        if default_model:
            details.append(default_model)
        if not enabled:
            details.append("disabled in Settings")
        if not details:
            return provider_name
        return f"{provider_name} ({' | '.join(details)})"

    def _update_autosave_indicator(self, last_save_time: str = "") -> None:
        autosave_enabled = self._config.get("translation.autosave_enabled", True)
        autosave_interval = self._config.get("translation.autosave_interval_seconds", 30)
        if not autosave_enabled:
            self._autosave_label.setText("Autosave: Off")
            self._autosave_label.setStyleSheet("font-size: 11px; color: #f9e2af; padding: 0 4px;")
            self._autosave_label.setToolTip("Autosave is disabled in Settings > Translation.")
            self._update_sync_summary_label()
            return

        effective_time = last_save_time or self._autosave.last_save_time
        if effective_time:
            self._autosave_label.setText(f"Autosave: {effective_time}")
            self._autosave_label.setToolTip(
                f"Autosave is enabled every {autosave_interval} seconds.\n"
                f"Last successful save: {effective_time}."
            )
        else:
            self._autosave_label.setText(f"Autosave: On ({autosave_interval}s)")
            self._autosave_label.setToolTip(
                f"Autosave is enabled every {autosave_interval} seconds.\n"
                "Progress is saved automatically after you save the project."
            )
        self._autosave_label.setStyleSheet("font-size: 11px; color: #a6e3a1; padding: 0 4px;")

    @staticmethod
    def _add_separator(layout: QHBoxLayout) -> None:
        from PySide6.QtWidgets import QFrame
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #45475a;")
        sep.setFixedWidth(2)
        layout.addWidget(sep)

    def initialize_from_game(self, vfs: VfsManager, discovered_palocs: list[dict]) -> None:
        """Called by main_window after game is loaded."""
        self._vfs = vfs
        self._usage_index = LocalizationUsageIndex(vfs)
        self._packages_path = vfs._packages_path
        self._discovered_palocs = [
            paloc_info
            for paloc_info in discovered_palocs
            if paloc_info.get("lang_code") != "ar"
        ]

        self._source_combo.clear()
        for paloc_info in self._discovered_palocs:
            lang_code = paloc_info["lang_code"]
            lang = self._lang_config.get_language(lang_code)
            display_name = lang.name if lang else lang_code
            self._source_combo.addItem(
                f"{display_name} ({paloc_info['filename']})",
                paloc_info,
            )

        count = len(self._discovered_palocs)
        build_info = self._get_game_build_info()
        if build_info["short_label"]:
            self._paloc_info.setText(f"{count} game languages detected | {build_info['short_label']}")
        else:
            self._paloc_info.setText(f"{count} game languages detected")
        self._update_game_header()
        self._update_sync_summary_label()
        self._progress.set_status(f"Game loaded: {count} localization files found")

    def _get_game_build_info(self) -> dict:
        """Get the current game build metadata from 0.paver + 0.papgt."""
        info = {
            "version_text": "",
            "build_id": "",
            "build_display": "",
            "fingerprint": "",
            "short_label": "",
            "modified": "",
            "crc": 0,
            "size": 0,
        }
        try:
            import struct
            from datetime import datetime

            game_path = self._config.get("general.last_game_path", "")
            if not game_path:
                return info

            paver_path = os.path.join(game_path, "meta", "0.paver")
            if os.path.isfile(paver_path):
                with open(paver_path, "rb") as f:
                    paver_data = f.read()
                if len(paver_data) >= 6:
                    major, minor, patch = struct.unpack_from("<HHH", paver_data, 0)
                    info["version_text"] = f"v{major}.{minor:02d}.{patch:02d}"

            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            if os.path.isfile(papgt_path):
                stat = os.stat(papgt_path)
                info["modified"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                with open(papgt_path, "rb") as f:
                    papgt_data = f.read()
                info["size"] = len(papgt_data)
                if len(papgt_data) > 12:
                    info["crc"] = pa_checksum(papgt_data[12:])

            version_text = info["version_text"]
            crc = info["crc"]
            if version_text and crc:
                info["build_id"] = f"{version_text}|CRC:0x{crc:08X}"
                info["build_display"] = f"{version_text} | CRC 0x{crc:08X}"
            elif version_text:
                info["build_id"] = version_text
                info["build_display"] = version_text
            elif crc:
                info["build_id"] = f"CRC:0x{crc:08X}"
                info["build_display"] = f"CRC 0x{crc:08X}"

            if info["build_id"]:
                info["fingerprint"] = f"{info['build_id']}|SIZE:{info['size']}"
            elif info["size"]:
                info["fingerprint"] = f"SIZE:{info['size']}"

            info["short_label"] = info["build_display"]
            if info["short_label"] and info["modified"]:
                info["short_label"] = f"{info['short_label']} | {info['modified']}"
        except Exception:
            return info
        return info

    def _get_game_version_short(self) -> str:
        """Get a short game version string for status labels."""
        return self._get_game_build_info().get("short_label", "")

    def _update_game_header(self) -> None:
        build_info = self._get_game_build_info()
        build_display = build_info.get("build_display", "")
        short_label = build_info.get("short_label", "")
        self._game_version_label.setText(build_display or "Game build unknown")
        self._game_version_label.setToolTip(
            short_label
            or "Game build metadata could not be detected from meta/0.paver and meta/0.papgt."
        )

    def _update_sync_summary_label(self) -> None:
        summary = self._project.last_sync_summary if self._project.entries else {}
        build_display = self._project.game_build_display or self._get_game_build_info().get("build_display", "")

        if summary:
            parts = []
            if summary.get("new", 0):
                parts.append(f"+{int(summary['new']):,}")
            if summary.get("changed", 0):
                parts.append(f"~{int(summary['changed']):,}")
            if summary.get("removed", 0):
                parts.append(f"-{int(summary['removed']):,}")
            delta_text = " ".join(parts) if parts else "up to date"
            display = summary.get("display", build_display or summary.get("version", ""))
            self._sync_summary_label.setText(f"Text Sync: {display} | {delta_text}".strip())
            self._sync_summary_label.setToolTip(
                "Latest text sync summary.\n"
                f"Build: {display or 'Unknown'}\n"
                f"Added: {int(summary.get('new', 0)):,}\n"
                f"Changed: {int(summary.get('changed', 0)):,}\n"
                f"Removed: {int(summary.get('removed', 0)):,}"
            )
            return

        if build_display:
            self._sync_summary_label.setText(f"Text Sync: {build_display}")
            self._sync_summary_label.setToolTip(
                "No text-delta history has been recorded for this project yet.\n"
                f"Current build: {build_display}"
            )
        else:
            self._sync_summary_label.setText("Text Sync: waiting")
            self._sync_summary_label.setToolTip(
                "Load the game and a language file to start version-aware text tracking."
            )

    def _serialize_ui_state(self) -> dict:
        build_info = self._get_game_build_info()
        return {
            "source_lang": self._project.source_lang,
            "target_lang": self._project.target_lang,
            "source_file": self._project.source_file,
            "source_combo_text": self._source_combo.currentText(),
            "target_combo_index": self._target_combo.currentIndex(),
            "provider_id": self._provider_combo.currentData() or "",
            "game_build_id": build_info.get("build_id", ""),
            "game_build_display": build_info.get("build_display", ""),
            "game_fingerprint": build_info.get("fingerprint", ""),
        }

    def _persist_ui_state(self) -> None:
        try:
            recovery_dir = os.path.join(os.path.expanduser("~"), ".crimsonforge")
            os.makedirs(recovery_dir, exist_ok=True)
            ui_path = os.path.join(recovery_dir, "autosave_ui_state.json")
            with open(ui_path, "w", encoding="utf-8") as f:
                json.dump(self._serialize_ui_state(), f, indent=2)
        except Exception as ex:
            logger.warning("Failed to persist UI state: %s", ex)

    def _on_source_changed(self):
        paloc_info = self._source_combo.currentData()
        if paloc_info:
            self._paloc_info.setText(
                f"Group: {paloc_info['group']} | File: {paloc_info['filename']}"
            )

    def _load_selected_paloc(self):
        """Load paloc from game, merging with any saved translations.

        If the user has already translated and patched strings,
        those translations are preserved by checking the autosave
        project file for matching entries.
        """
        paloc_info = self._source_combo.currentData()
        if not paloc_info:
            show_error(self, "Error", "Select a source language first.")
            return
        if not self._vfs:
            show_error(self, "Error", "Game not loaded.")
            return

        self._progress.set_progress(0, f"Loading {paloc_info['filename']}...")

        try:
            group = paloc_info["group"]
            pamt_data = self._vfs.load_pamt(group)
            fresh_entry = None
            for fe in pamt_data.file_entries:
                if fe.path.lower().endswith(paloc_info["filename"].lower()):
                    fresh_entry = fe
                    break
            if not fresh_entry:
                show_error(self, "Error",
                           f"File {paloc_info['filename']} not found in PAMT for group {group}.")
                return

            paloc_info["entry"] = fresh_entry
            raw_data = self._vfs.read_entry_data(fresh_entry)
            from core.paloc_parser import parse_paloc
            all_entries = parse_paloc(raw_data)

            string_entries = [(e.key, e.value) for e in all_entries
                              if not e.key.startswith("@") and not e.key.startswith("#")]

            source_code = paloc_info["lang_code"]
            target_code = self._target_combo.currentData() or ""
            build_info = self._get_game_build_info()

            # Preserve the original baseline forever and only add newly discovered keys.
            filename = paloc_info["filename"]
            baseline_merge = self._baseline_mgr.merge_into_baseline(
                filename,
                string_entries,
                source_code,
                build_id=build_info.get("build_id", ""),
                build_display=build_info.get("build_display", ""),
                game_fingerprint=build_info.get("fingerprint", ""),
            )
            if baseline_merge.get("added", 0):
                logger.info(
                    "Baseline extended for %s with %d newly discovered keys",
                    filename,
                    int(baseline_merge["added"]),
                )

            saved_project = None
            recovery_dir = os.path.join(os.path.expanduser("~"), ".crimsonforge")
            recovery_path = os.path.join(recovery_dir, "autosave_project.json")
            if os.path.isfile(recovery_path):
                try:
                    candidate = TranslationProject()
                    candidate.load(recovery_path)
                    if candidate.source_file == filename:
                        saved_project = candidate
                        logger.info(
                            "Found saved translation project for %s (%d entries)",
                            filename,
                            candidate.entry_count,
                        )
                except Exception as ex:
                    logger.warning("Could not load saved project: %s", ex)

            restored = 0
            merge = {"new": 0, "changed": 0, "removed": 0}
            if saved_project is not None:
                self._project = saved_project
                if target_code and self._project.target_lang != target_code:
                    self._project.target_lang = target_code
                merge = self._merge_with_fresh_game_data(build_info=build_info)
                restored = sum(1 for entry in self._project.entries if entry.translated_text.strip())
            else:
                self._project = TranslationProject()
                self._project.create_from_paloc(
                    string_entries,
                    source_code,
                    target_code,
                    filename,
                    game_build_id=build_info.get("build_id", ""),
                    game_build_display=build_info.get("build_display", ""),
                    game_fingerprint=build_info.get("fingerprint", ""),
                )
                self._project.record_sync_summary({
                    "version": build_info.get("build_id", ""),
                    "display": build_info.get("build_display", ""),
                    "new": 0,
                    "changed": 0,
                    "removed": 0,
                })

            self._apply_usage_tags(rebuild=True)
            self._reload_table()
            self._autosave.set_project(self._project)
            self._autosave.start()
            self._update_stats()
            self._update_game_header()
            self._update_sync_summary_label()

            status_msg = f"Loaded {len(string_entries)} strings from {filename}"
            if restored > 0:
                status_msg += f" ({restored} translations restored)"
            total_changes = merge["new"] + merge["changed"] + merge["removed"]
            if total_changes > 0:
                parts = []
                if merge["new"]:
                    parts.append(f"+{merge['new']:,} new")
                if merge["changed"]:
                    parts.append(f"~{merge['changed']:,} changed")
                if merge["removed"]:
                    parts.append(f"-{merge['removed']:,} removed")
                status_msg += f" | Synced: {', '.join(parts)}"
            self._progress.set_progress(100, status_msg)
        except Exception as e:
            show_error(self, "Load Error", f"Failed to load paloc from game archives: {e}")
            self._progress.set_progress(0, "Load failed")

    def _on_target_lang_changed(self):
        code = self._target_combo.currentData()
        if code:
            if self._project.entries:
                self._project.target_lang = code

    def _on_provider_changed(self):
        pid = self._provider_combo.currentData()
        if not pid:
            self._ai_info_label.setText("No AI providers configured")
            return
        try:
            provider = self._registry.get_provider(pid)
        except ValueError:
            self._ai_info_label.setText("Provider unavailable")
            return

        enabled = self._registry.is_enabled(pid)
        default_model = self._config.get(f"ai_providers.{pid}.default_model", "")
        if not enabled:
            self._ai_info_label.setText(f"{provider.name} is disabled in Settings")
        elif default_model:
            self._ai_info_label.setText(f"Model: {default_model}")
        else:
            self._ai_info_label.setText("Enabled, but no default model set in Settings")

    def _open_glossary(self):
        """Open the glossary editor dialog."""
        source_code = self._project.source_lang if self._project.entries else "en"
        target_code = self._target_combo.currentData() or ""
        if not target_code:
            show_error(self, "Error", "Select a destination language first.")
            return

        self._glossary_mgr.load(source_code, target_code)

        if self._glossary_mgr.entry_count == 0 and self._project.entries:
            self._progress.set_status("Extracting glossary candidates...")
            QApplication.processEvents()
            string_entries = [(e.key, e.original_text) for e in self._project.entries]
            added = self._glossary_mgr.extract_from_paloc(string_entries)
            if added:
                self._glossary_mgr.save()
                self._progress.set_status(f"Extracted {added} glossary candidates")

        def ai_translate_term(term: str) -> str:
            """Translate a single glossary term using the current AI provider."""
            provider, model, _ = self._get_ai_provider_and_model()
            if not provider:
                return ""
            from ai.translation_engine import TranslationEngine
            engine = TranslationEngine(provider=provider, prompt_manager=self._prompt_manager)
            source_lang = self._project.source_lang if self._project.entries else "en"
            target_lang = self._target_combo.currentData() or ""
            result = engine.translate_single(
                text=term,
                source_lang=source_lang,
                target_lang=target_lang,
                model=model,
            )
            return result.translated_text if result.success else ""

        from ui.dialogs.glossary_editor import GlossaryEditorDialog
        dialog = GlossaryEditorDialog(
            self._glossary_mgr,
            ai_translate_fn=ai_translate_term,
            parent=self,
        )
        dialog.exec()

        self._glossary_mgr.save()
        tc = self._glossary_mgr.translated_count
        total = self._glossary_mgr.entry_count
        self._glossary_status.setText(f"Glossary: {tc}/{total}")

    def _get_glossary_prompt(self) -> str:
        """Get the glossary prompt text for AI injection."""
        target_code = self._target_combo.currentData() or ""
        source_code = self._project.source_lang if self._project.entries else "en"
        if target_code and source_code:
            self._glossary_mgr.load(source_code, target_code)
            return self._glossary_mgr.build_prompt_glossary()
        return ""

    def _load_project(self):
        path = pick_file(self, "Load Translation Project", "", "JSON Files (*.json);;All Files (*.*)")
        if not path:
            return
        try:
            self._project = TranslationProject()
            self._project.load(path)
            merge = self._merge_with_fresh_game_data()
            self._apply_usage_tags(rebuild=True)
            self._reload_table()
            self._autosave.set_project(self._project)
            self._autosave.start()
            self._update_stats()
            self._update_game_header()
            self._update_sync_summary_label()

            parts = []
            if merge["new"]:
                parts.append(f"+{merge['new']:,} new")
            if merge["changed"]:
                parts.append(f"~{merge['changed']:,} changed")
            if merge["removed"]:
                parts.append(f"-{merge['removed']:,} removed")
            suffix = f" | Synced: {', '.join(parts)}" if parts else ""
            self._progress.set_status(
                f"Loaded project: {self._project.entry_count} strings{suffix}"
            )
        except Exception as e:
            show_error(self, "Load Error", f"Failed to load project: {e}")

    def _save_project(self):
        if not self._project.project_file:
            path = pick_save_file(self, "Save Translation Project", "", "JSON Files (*.json)")
            if not path:
                return
        else:
            path = self._project.project_file
        try:
            self._project.save(path)
            self._autosave.set_project(self._project)
            self._autosave.start()
            self._progress.set_status(f"Project saved: {os.path.basename(path)}")
        except Exception as e:
            show_error(self, "Save Error", str(e))

    def _auto_translate_all(self):
        provider, model, error = self._get_ai_provider_and_model()
        if not provider:
            show_error(self, "Error", error or "Select an AI provider in Settings first.")
            return
        if not self._project.entries:
            show_error(self, "Error", "Load a language file first.")
            return

        engine = TranslationEngine(
            provider=provider,
            prompt_manager=self._prompt_manager,
            batch_size=self._config.get("translation.batch_size", 10),
            batch_delay_ms=self._config.get("translation.batch_delay_ms", 500),
        )
        engine.set_glossary(self._get_glossary_prompt())
        self._batch_processor = TranslationBatchProcessor(engine, self._project)

        self._auto_translate_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)

        def do_batch(worker):
            def progress_cb(completed, total, result):
                pct = int((completed / total) * 100) if total else 0
                worker.report_progress(pct, f"Translating {completed}/{total}...")
            return self._batch_processor.translate_all_pending(
                model=model,
                progress_callback=progress_cb,
            )

        self._worker = FunctionWorker(do_batch)
        self._worker.progress.connect(lambda p, m: self._progress.set_progress(p, m))
        self._worker.finished_result.connect(self._on_batch_done)
        self._worker.error_occurred.connect(lambda e: show_error(self, "Translation Error", e))
        self._worker.start()

    def _pause_translate(self):
        if self._batch_processor:
            self._batch_processor.pause()
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(True)

    def _resume_translate(self):
        if self._batch_processor:
            self._batch_processor.resume()
            self._pause_btn.setEnabled(True)
            self._resume_btn.setEnabled(False)

    def _stop_translate(self):
        if self._batch_processor:
            self._batch_processor.stop()

    def _on_batch_done(self, results):
        self._auto_translate_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)

        succeeded = sum(1 for r in results if r.success)
        failed = [r for r in results if not r.success]
        for r in failed:
            logger.error("Batch translate failed: %s", r.error)

        self._reload_table()

        if self._batch_processor:
            stats = self._batch_processor.stats
            self._stats_label.setText(
                f"Tokens: {format_tokens(stats.total_tokens)}  "
                f"({stats.total_input_tokens:,} in / {stats.total_output_tokens:,} out)  |  "
                f"Cost: ~{format_cost(stats.total_cost)}  |  "
                f"Avg: {stats.avg_latency_ms:.0f} ms/str"
            )
        self._progress.set_progress(100, f"Done: {succeeded}/{len(results)} translated, {len(failed)} failed")
        self._update_stats()
        self._autosave.notify_change()

    def _on_translation_edited(self, index: int, text: str):
        entry = self._project.get_entry(index)
        if entry:
            entry.edit_translation(text)
            if text and entry.status == StringStatus.PENDING:
                entry.status = StringStatus.TRANSLATED
            self._project.mark_modified()
            self._autosave.notify_change()
            self._update_stats()

    def _get_ai_provider_and_model(self):
        """Get the current AI provider and model from settings.

        Returns (provider, model_id, error_message).
        """
        pid = self._provider_combo.currentData()
        if not pid:
            return None, "", "No AI provider selected."
        try:
            provider = self._registry.get_provider(pid)
        except ValueError as ex:
            return None, "", str(ex)
        if not self._registry.is_enabled(pid):
            return None, "", f"{provider.name} is disabled in Settings > AI Providers. Enable it and save to use it here."
        model = self._config.get(f"ai_providers.{pid}.default_model", "")
        return provider, model, ""

    def _on_ai_requested(self, index: int):
        """Handle AI translate request for one entry, with duplicate detection."""
        entry = self._project.get_entry(index)
        if not entry:
            return
        provider, model, error = self._get_ai_provider_and_model()
        if not provider:
            show_error(self, "Error", error or "Select an AI provider in Settings first.")
            return

        engine = TranslationEngine(provider=provider, prompt_manager=self._prompt_manager)
        engine.set_glossary(self._get_glossary_prompt())
        batch = TranslationBatchProcessor(engine, self._project)
        result = batch.translate_single_entry(entry, model=model)
        if not result.success:
            show_error(self, "AI Error", f"Translation failed: {result.error}")
            return

        self._table.update_entry_row(index)
        self._autosave.notify_change()

        duplicates = self._count_duplicates_for(entry)
        if duplicates > 0 and entry.translated_text:
            if confirm_action(
                self, "Duplicates Found",
                f"'{entry.original_text[:50]}...' has {duplicates} more identical entries.\n"
                f"Apply the same translation to all duplicates?"
            ):
                applied = self._apply_to_duplicates(entry)
                self._table.refresh()
                self._progress.set_status(f"Applied to {applied} duplicate entries")

    def _on_ai_batch_selected(self, indices: list[int]):
        """Handle AI translate for multiple selected entries."""
        if not indices:
            return
        provider, model, error = self._get_ai_provider_and_model()
        if not provider:
            show_error(self, "Error", error or "Select an AI provider in Settings first.")
            return

        entries = [self._project.get_entry(i) for i in indices]
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        engine = TranslationEngine(
            provider=provider,
            prompt_manager=self._prompt_manager,
            batch_size=self._config.get("translation.batch_size", 10),
            batch_delay_ms=self._config.get("translation.batch_delay_ms", 500),
        )
        engine.set_glossary(self._get_glossary_prompt())
        self._batch_processor = TranslationBatchProcessor(engine, self._project)

        self._auto_translate_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)

        def do_batch(worker, _entries=entries):
            def progress_cb(completed, total, result):
                pct = int((completed / total) * 100) if total else 0
                worker.report_progress(pct, f"Translating {completed}/{total} selected...")
            return self._batch_processor.translate_entries(
                _entries, model=model, progress_callback=progress_cb,
            )

        self._worker = FunctionWorker(do_batch)
        self._worker.progress.connect(lambda p, m: self._progress.set_progress(p, m))
        self._worker.finished_result.connect(self._on_batch_done)
        self._worker.error_occurred.connect(lambda e: show_error(self, "Translation Error", e))
        self._worker.start()

    def _count_duplicates_for(self, entry: TranslationEntry) -> int:
        """Count how many other entries have the same original text."""
        if not entry.original_text.strip():
            return 0
        count = 0
        text = entry.original_text.strip()
        for e in self._project.entries:
            if e.index != entry.index and e.original_text.strip() == text and not e.translated_text:
                count += 1
        return count

    def _on_status_changed(self, index: int, status_text: str):
        # index == -1 means batch operation (from table widget), just update stats
        if index == -1:
            self._project.mark_modified()
            self._autosave.notify_change()
            self._update_stats()
            self._progress.set_status(status_text)
            return
        entry = self._project.get_entry(index)
        if not entry:
            return
        if status_text == "Pending":
            entry.revert_to_pending()
        elif status_text == "Reviewed":
            entry.set_reviewed()
        elif status_text == "Approved":
            entry.set_approved()
        self._project.mark_modified()
        self._autosave.notify_change()
        self._update_stats()

    def _export_paloc(self):
        path = pick_save_file(self, "Export Paloc", "", "Paloc Files (*.paloc)")
        if not path:
            return
        try:
            exporter = TranslationExporter()
            exporter.export_to_paloc(self._project, path)
            show_info(self, "Export Complete", f"Exported to {os.path.basename(path)}")
        except Exception as e:
            show_error(self, "Export Error", str(e))

    def _ship_to_app(self):
        """Open the Ship to App dialog to generate a ZIP package."""
        if not self._project.entries:
            show_error(self, "Error", "No translation loaded. Load a language first.")
            return
        if not any(e.translated_text for e in self._project.entries):
            show_error(self, "Error", "No translated entries yet.")
            return

        from ui.dialogs.ship_dialog import ShipToAppDialog
        dlg = ShipToAppDialog(self._project, self._vfs,
                              self._discovered_palocs, self._config, self)
        dlg.exec()

    def _patch_to_game(self):
        """Full pipeline: export, compress, encrypt, write PAZ, checksum chain, done."""
        paloc_info = self._source_combo.currentData()
        if not paloc_info:
            show_error(self, "Error", "No source language selected.")
            return
        if not self._project.entries:
            show_error(self, "Error", "No translation data loaded.")
            return
        if not self._packages_path:
            show_error(self, "Error", "Game path not set. Load the game first.")
            return

        translated_count = sum(
            1 for e in self._project.entries if e.translated_text
        )
        total_count = len(self._project.entries)
        if translated_count == 0:
            show_error(self, "Error", "No strings have been translated yet.")
            return

        # ── Find duplicates ONCE using the pre-built map (O(n), not O(n²)) ──
        pending_dup_apply: list[tuple[TranslationEntry, list[TranslationEntry]]] = []
        pending_dup_count = 0
        duplicates = self._find_duplicate_values()
        if duplicates:
            for text, dup_entries in duplicates.items():
                sources = [e for e in dup_entries if e.translated_text]
                targets = [e for e in dup_entries if not e.translated_text]
                if sources and targets:
                    pending_dup_apply.append((sources[0], targets))
                    pending_dup_count += len(targets)

        # ── Single confirmation dialog that covers both duplicate-apply + patch ──
        dup_line = ""
        if pending_dup_count:
            dup_line = (
                f"\n\u26a0\ufe0f  {pending_dup_count} untranslated duplicates will be "
                f"auto-filled from already-translated entries.\n"
            )

        if not confirm_action(
            self, "Patch to Game",
            f"Source:  {paloc_info['filename']}\n"
            f"Package: {paloc_info['group']}\n"
            f"Strings: {translated_count:,} / {total_count:,} translated\n"
            f"{dup_line}\n"
            f"A backup will be created before any game files are modified.\n\n"
            f"Proceed?"
        ):
            return

        # ── Apply duplicates now (O(n) — direct list walk, no re-scan) ──
        if pending_dup_apply:
            applied = 0
            for source, targets in pending_dup_apply:
                for t in targets:
                    t.set_translated(
                        source.translated_text,
                        source.ai_provider,
                        source.ai_model,
                    )
                    applied += 1
            translated_count += applied
            self._project.mark_modified()
            self._autosave.notify_change()
            # Refresh model data without blocking — virtual model makes this instant
            self._table._model.layoutChanged.emit()
            self._progress.set_status(f"Auto-filled {applied} duplicate entries")

        dialog = PatchProgressDialog(self)
        self._patch_btn.setEnabled(False)

        TOTAL_STEPS = 9
        replacements_by_key = {}
        for te in self._project.entries:
            if te.translated_text:
                replacements_by_key[te.key] = te.translated_text

        def do_patch(worker):
            def step(n, t, msg):
                worker.report_progress(int((n / t) * 85), msg)

            translation_result = patch_translation_to_game(
                packages_path=str(self._packages_path),
                group=paloc_info["group"],
                filename=paloc_info["filename"],
                replacements_by_key=replacements_by_key,
                create_backup=True,
                progress_callback=step,
            )
            return translation_result

        self._worker = FunctionWorker(do_patch)

        def on_progress(pct, msg):
            dialog.set_step(
                max(1, int(pct / 100 * TOTAL_STEPS)),
                TOTAL_STEPS,
                msg,
            )
            dialog.log(msg)

        def on_done(translation_result):
            self._patch_btn.setEnabled(True)
            if not translation_result.success:
                dialog.log(f"ERROR: {translation_result.message}")
                dialog.set_finished_error(translation_result.message)
                return

            dialog.log(f"PAZ CRC:   0x{translation_result.paz_crc:08X}")
            dialog.log(f"PAMT CRC:  0x{translation_result.pamt_crc:08X}")
            dialog.log(f"PAPGT CRC: 0x{translation_result.papgt_crc:08X}")
            dialog.log("All checksums verified!")

            dialog.set_finished_success(
                translation_result.paz_crc,
                translation_result.pamt_crc,
                translation_result.papgt_crc,
                translation_result.backup_dir,
            )

        def on_error(error_msg):
            self._patch_btn.setEnabled(True)
            dialog.log(f"ERROR: {error_msg}")
            dialog.set_finished_error(error_msg)

        self._worker.progress.connect(on_progress)
        self._worker.finished_result.connect(on_done)
        self._worker.error_occurred.connect(on_error)
        self._worker.start()
        dialog.exec()

    def _on_entry_double_clicked(self, entry_index: int):
        """Open the entry editor dialog for the double-clicked entry."""
        entry = self._project.get_entry(entry_index)
        if not entry:
            return

        # Get baseline original for this entry
        baseline_text = None
        if self._project.source_file:
            baseline_text = self._baseline_mgr.get_original_text(
                self._project.source_file, entry.key
            )

        # Get current provider info from settings
        provider, model, _ = self._get_ai_provider_and_model()
        provider_name = provider.name if provider else ""
        model_name = model or ""

        from ui.dialogs.entry_editor import EntryEditorDialog
        dialog = EntryEditorDialog(
            entry,
            baseline_text=baseline_text,
            provider_name=provider_name,
            model_name=model_name,
            parent=self,
        )

        def on_ai_from_dialog(idx):
            self._on_ai_requested(idx)
            refreshed = self._project.get_entry(idx)
            if refreshed and refreshed.translated_text:
                dialog.update_translation(refreshed.translated_text)

        dialog.ai_requested.connect(on_ai_from_dialog)

        def on_entry_saved(idx, new_text, new_status):
            e = self._project.get_entry(idx)
            if not e:
                return
            # Apply the translation text
            if new_text != e.translated_text:
                e.edit_translation(new_text)
            # Auto-transition: if text was entered and status is Pending, promote to Translated
            if new_text and e.status == StringStatus.PENDING:
                e.status = StringStatus.TRANSLATED
            # If text was cleared, revert to Pending regardless of status combo
            if not new_text and e.status != StringStatus.PENDING:
                e.revert_to_pending()
            else:
                # Apply the user-selected status from the combo
                if new_status == "Pending":
                    e.revert_to_pending()
                elif new_status == "Translated":
                    if e.translated_text:
                        e.status = StringStatus.TRANSLATED
                elif new_status == "Reviewed":
                    e.set_reviewed()
                elif new_status == "Approved":
                    e.set_approved()
            self._project.mark_modified()
            self._autosave.notify_change()
            self._table.update_entry_row(idx)
            self._update_stats()

        dialog.entry_saved.connect(on_entry_saved)
        dialog.exec()

    def _find_duplicate_values(self) -> dict[str, list[TranslationEntry]]:
        """Find entries with identical original text (duplicates)."""
        text_map: dict[str, list[TranslationEntry]] = {}
        for entry in self._project.entries:
            if not entry.original_text.strip():
                continue
            key = entry.original_text.strip()
            if key not in text_map:
                text_map[key] = []
            text_map[key].append(entry)
        return {k: v for k, v in text_map.items() if len(v) > 1}

    def _apply_to_duplicates(self, source_entry: TranslationEntry) -> int:
        """Apply a translation to all entries with the same original text.

        Returns the number of additional entries updated.
        """
        if not source_entry.translated_text:
            return 0
        count = 0
        for entry in self._project.entries:
            if entry.index == source_entry.index:
                continue
            if entry.original_text.strip() == source_entry.original_text.strip():
                if not entry.translated_text:
                    entry.set_translated(
                        source_entry.translated_text,
                        source_entry.ai_provider,
                        source_entry.ai_model,
                    )
                    count += 1
        return count

    def _clear_selected(self):
        """Clear translations for selected entries in the table."""
        indexes = self._table._view.selectionModel().selectedRows()
        if not indexes:
            show_error(self, "Error", "Select entries to clear first.")
            return
        count = 0
        for idx in indexes:
            entry = self._table._model.entry_at(idx.row())
            if entry and entry.translated_text:
                entry.edit_translation("")
                entry.revert_to_pending()
                count += 1
        if count:
            self._table.refresh()
            self._project.mark_modified()
            self._autosave.notify_change()
            self._progress.set_status(f"Cleared {count} translations")

    def _clear_all(self):
        """Clear all translations after confirmation."""
        if not self._project.entries:
            return
        translated = sum(1 for e in self._project.entries if e.translated_text)
        if translated == 0:
            show_info(self, "Nothing to Clear", "No translations to clear.")
            return
        if not confirm_action(
            self, "Clear All Translations",
            f"Clear all {translated} translations?\nThis cannot be undone."
        ):
            return
        for entry in self._project.entries:
            if entry.translated_text:
                entry.edit_translation("")
                entry.revert_to_pending()
        self._table.refresh()
        self._project.mark_modified()
        self._autosave.notify_change()
        self._update_stats()
        self._progress.set_status(f"Cleared {translated} translations")

    def _revert_selected(self):
        """Revert selected entries to original baseline text."""
        indexes = self._table._view.selectionModel().selectedRows()
        if not indexes:
            show_error(self, "Error", "Select entries to revert first.")
            return
        filename = self._project.source_file
        baseline = self._baseline_mgr.load_baseline(filename)
        if not baseline:
            show_error(self, "Error", "No baseline found. Load the language first.")
            return
        count = 0
        for idx in indexes:
            entry = self._table._model.entry_at(idx.row())
            if entry:
                orig = baseline.get(entry.key)
                if orig is not None:
                    entry.edit_translation("")
                    entry.original_text = orig
                    entry.revert_to_pending()
                    count += 1
        if count:
            self._table.refresh()
            self._project.mark_modified()
            self._autosave.notify_change()
            self._update_stats()
            self._progress.set_status(f"Reverted {count} entries to original baseline")

    def _update_stats(self):
        """Update the status bar badges, progress bar, and labels."""
        if not self._project.entries:
            self._stats_progress.setMaximum(1)
            self._stats_progress.setValue(0)
            self._badge_pending.setText("⊙  Pending: 0")
            self._badge_translated.setText("◆  Translated: 0")
            self._badge_reviewed.setText("★  Reviewed: 0")
            self._badge_approved.setText("✔  Approved: 0")
            self._update_sync_summary_label()
            return

        stats = self._project.get_stats()
        total = stats.get("total", 0)
        pending = stats.get("pending", 0)
        translated = stats.get("translated", 0)
        reviewed = stats.get("reviewed", 0)
        approved = stats.get("approved", 0)
        done = translated + reviewed + approved

        self._stats_progress.setMaximum(total)
        self._stats_progress.setValue(done)

        def pct(n):
            return f"{n / total * 100:.0f}%" if total else "0%"

        self._badge_pending.setText(f"⊙  Pending: {pending:,}  ({pct(pending)})")
        self._badge_translated.setText(f"◆  Translated: {translated:,}  ({pct(translated)})")
        self._badge_reviewed.setText(f"★  Reviewed: {reviewed:,}  ({pct(reviewed)})")
        self._badge_approved.setText(f"✔  Approved: {approved:,}  ({pct(approved)})")

        # Highlight approved badge green when everything is approved
        if approved == total and total > 0:
            self._badge_approved.setStyleSheet(
                "font-size: 11px; font-weight: 600;"
                "color: #1e1e2e; background: #a6e3a1;"
                "border-radius: 4px; padding: 1px 8px;"
            )
        else:
            self._badge_approved.setStyleSheet(
                "font-size: 11px; font-weight: 600;"
                "color: #a6e3a1; background: #1e1e2e;"
                "border-radius: 4px; padding: 1px 8px;"
            )

        self._update_sync_summary_label()

    def _export_json(self):
        """Export translations to JSON for external editing."""
        if not self._project.entries:
            show_error(self, "Error", "No translation data loaded.")
            return

        entries_to_export, export_scope = self._table.get_export_entries()
        if not entries_to_export:
            show_error(self, "Error", "No entries match the current filter or selection.")
            return

        path = pick_save_file(self, "Export Translations to JSON", "", "JSON Files (*.json)")
        if not path:
            return

        filter_state = self._table.get_filter_state()
        data = {
            "version": "1.1.0",
            "source_lang": self._project.source_lang,
            "target_lang": self._project.target_lang,
            "source_file": self._project.source_file,
            "entry_count": len(entries_to_export),
            "export_scope": export_scope,
            "filters": filter_state,
            "entries": [],
        }
        for entry in entries_to_export:
            data["entries"].append({
                "index": entry.index,
                "key": entry.key,
                "original": entry.original_text,
                "translation": entry.translated_text,
                "status": entry.status.value,
                "usage_tags": list(entry.usage_tags or []),
            })
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        show_info(self, "Export Complete",
                  f"Exported {len(data['entries'])} {export_scope} entries to {os.path.basename(path)}")

    def _import_json(self):
        """Import translations from JSON, merging by key."""
        if not self._project.entries:
            show_error(self, "Error", "Load a language first before importing.")
            return
        path = pick_file(self, "Import Translations from JSON", "", "JSON Files (*.json);;All Files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            show_error(self, "Import Error", f"Failed to read JSON: {e}")
            return

        import_entries = data.get("entries", [])
        if not import_entries:
            show_error(self, "Import Error", "No entries found in JSON file.")
            return

        import_map = {}
        for ie in import_entries:
            key = ie.get("key", "")
            translation = ie.get("translation", "")
            if key and translation:
                import_map[key] = {
                    "text": translation,
                    "status": ie.get("status", "translated"),
                }

        merged = 0
        skipped = 0
        for entry in self._project.entries:
            imp = import_map.get(entry.key)
            if imp:
                entry.edit_translation(imp["text"])
                try:
                    entry.status = StringStatus(imp["status"])
                except ValueError:
                    entry.status = StringStatus.TRANSLATED
                merged += 1
            else:
                skipped += 1

        self._table.refresh()
        self._project.mark_modified()
        self._autosave.notify_change()
        self._update_stats()
        show_info(self, "Import Complete",
                  f"Merged {merged} translations, {skipped} entries had no match.")

    def _apply_usage_tags(self, rebuild: bool = False) -> None:
        if not self._project.entries:
            self._table.set_usage_filter_options([("All", "All")])
            return

        usage_map = {}
        if rebuild and self._usage_index is not None:
            try:
                usage_map = self._usage_index.build({entry.key for entry in self._project.entries})
            except Exception as ex:
                logger.warning("Failed to build usage index: %s", ex)
                usage_map = {}

        for entry in self._project.entries:
            tags = usage_map.get(entry.key)
            if tags is None:
                tags = list(entry.usage_tags) if entry.usage_tags else [CATEGORY_UNCATEGORIZED]
            entry.usage_tags = tags

        self._table.set_usage_filter_options(self._build_usage_filter_options())

    def _build_usage_filter_options(self) -> list[tuple[str, str]]:
        counts = {category: 0 for category in CATEGORY_ORDER}
        for entry in self._project.entries:
            tags = entry.usage_tags or [CATEGORY_UNCATEGORIZED]
            for tag in tags:
                counts[tag] = counts.get(tag, 0) + 1

        options = [("All", "All")]
        for category in CATEGORY_ORDER:
            options.append((f"{category} ({counts.get(category, 0):,})", category))
        return options

    def _build_version_filter_options(self) -> list[tuple[str, str]]:
        options = [("All Versions", "__all__")]
        if not self._project.entries:
            return options

        version_counts: dict[str, dict[str, int]] = {}
        for entry in self._project.entries:
            for event in getattr(entry, "game_event_history", []) or []:
                version = event.get("version", "")
                kind = event.get("kind", "")
                if not version:
                    continue
                bucket = version_counts.setdefault(
                    version,
                    {"baseline": 0, "added": 0, "changed": 0, "removed": 0},
                )
                if kind in bucket:
                    bucket[kind] += 1

        if not version_counts:
            return options

        display_map = {
            item.get("version", ""): item.get("display", item.get("version", ""))
            for item in self._project.update_history
            if item.get("version")
        }
        if self._project.game_build_id:
            display_map.setdefault(
                self._project.game_build_id,
                self._project.game_build_display or self._project.game_build_id,
            )

        ordered_versions: list[str] = []
        for item in self._project.update_history:
            version = item.get("version", "")
            if version and version in version_counts and version not in ordered_versions:
                ordered_versions.append(version)
        for version in version_counts:
            if version not in ordered_versions:
                ordered_versions.append(version)

        for version in reversed(ordered_versions):
            counts = version_counts.get(version, {})
            summary_parts = []
            if counts.get("added", 0):
                summary_parts.append(f"+{counts['added']:,}")
            if counts.get("changed", 0):
                summary_parts.append(f"~{counts['changed']:,}")
            if counts.get("removed", 0):
                summary_parts.append(f"-{counts['removed']:,}")
            if counts.get("baseline", 0) and not summary_parts:
                summary_parts.append(f"{counts['baseline']:,} tracked")
            label = display_map.get(version, version)
            if summary_parts:
                label = f"{label} ({', '.join(summary_parts)})"
            options.append((label, version))
        return options

    def _reload_table(self) -> None:
        self._table.load_entries(
            self._project.entries,
            self._build_usage_filter_options(),
            self._build_version_filter_options(),
        )

    def save_state(self) -> None:
        """Save current project + UI selections to recovery files (called on app close)."""
        if not self._project.entries:
            return
        try:
            recovery_dir = os.path.join(os.path.expanduser("~"), ".crimsonforge")
            os.makedirs(recovery_dir, exist_ok=True)

            recovery_path = os.path.join(recovery_dir, "autosave_project.json")
            self._project.save(recovery_path)
            self._persist_ui_state()

            logger.info("State saved to %s", recovery_path)
        except Exception as e:
            logger.error("Failed to save recovery state: %s", e)

    def restore_state(self) -> bool:
        """Try to restore project + UI selections from recovery files. Returns True if restored."""
        recovery_dir = os.path.join(os.path.expanduser("~"), ".crimsonforge")
        recovery_path = os.path.join(recovery_dir, "autosave_project.json")
        ui_path = os.path.join(recovery_dir, "autosave_ui_state.json")

        if not os.path.isfile(recovery_path):
            return False
        try:
            self._project = TranslationProject()
            self._project.load(recovery_path)

            ui_state = {}
            if os.path.isfile(ui_path):
                import json
                with open(ui_path, "r", encoding="utf-8") as f:
                    ui_state = json.load(f)

            source_text = ui_state.get("source_combo_text", "")
            if source_text:
                for i in range(self._source_combo.count()):
                    if self._source_combo.itemText(i) == source_text:
                        self._source_combo.setCurrentIndex(i)
                        break
            elif self._project.source_file:
                for i in range(self._source_combo.count()):
                    paloc_info = self._source_combo.itemData(i)
                    if paloc_info and paloc_info.get("filename") == self._project.source_file:
                        self._source_combo.setCurrentIndex(i)
                        break

            target_idx = ui_state.get("target_combo_index", -1)
            target_lang = ui_state.get("target_lang", "")
            if target_lang:
                for i in range(self._target_combo.count()):
                    if self._target_combo.itemData(i) == target_lang:
                        self._target_combo.setCurrentIndex(i)
                        break
            elif target_idx >= 0 and target_idx < self._target_combo.count():
                self._target_combo.setCurrentIndex(target_idx)

            provider_id = ui_state.get("provider_id", "")
            if provider_id:
                for i in range(self._provider_combo.count()):
                    if self._provider_combo.itemData(i) == provider_id:
                        self._provider_combo.setCurrentIndex(i)
                        break

            # Always merge with fresh game data to pick up new entries,
            # parser improvements, or game patches.
            saved_fp = self._project.game_fingerprint or ui_state.get("game_fingerprint", "")
            current_fp = self._get_game_fingerprint()
            is_game_update = bool(saved_fp and current_fp and saved_fp != current_fp)

            merge = self._merge_with_fresh_game_data()
            total_changes = merge["new"] + merge["changed"] + merge["removed"]

            if is_game_update:
                logger.info("Game update detected: %s -> %s", saved_fp, current_fp)

            # Show detailed popup when there are real changes
            if total_changes > 0:
                from ui.dialogs.confirmation import show_info
                title = "Game Update Detected" if is_game_update else "Project Synced with Game"
                lines = []
                if is_game_update:
                    lines.append("The game was updated since your last session.\n")
                if merge.get("previous_display"):
                    lines.append(f"Previous build: {merge['previous_display']}")
                if merge.get("display"):
                    lines.append(f"Current build: {merge['display']}\n")
                lines.append(f"Game file: {merge['total_fresh']:,} entries")
                lines.append(f"Your project: {merge['total_saved']:,} entries\n")
                if merge["new"]:
                    lines.append(f"  + {merge['new']:,} new entries added")
                if merge["changed"]:
                    lines.append(f"  ~ {merge['changed']:,} entries text changed (marked for re-review)")
                if merge["removed"]:
                    lines.append(f"  - {merge['removed']:,} entries no longer in game")
                if merge["changed_samples"]:
                    lines.append("\nChanged text samples:")
                    for key, old, new in merge["changed_samples"]:
                        lines.append(f"  [{key}]")
                        lines.append(f"    was: {old}")
                        lines.append(f"    now: {new}")
                if merge.get("new_samples"):
                    lines.append("\nNew key samples:")
                    for key in merge["new_samples"]:
                        lines.append(f"  + {key}")
                if merge.get("removed_samples"):
                    lines.append("\nRemoved key samples:")
                    for key in merge["removed_samples"]:
                        lines.append(f"  - {key}")
                lines.append("\nYour translations are preserved.")
                show_info(self, title, "\n".join(lines))

            self._apply_usage_tags(rebuild=True)
            self._reload_table()
            self._update_stats()
            self._update_game_header()
            self._update_sync_summary_label()
            self._autosave.set_project(self._project)
            self._autosave.start()

            stats = self._project.get_stats()
            translated = stats.get("translated", 0) + stats.get("reviewed", 0) + stats.get("approved", 0)
            merge_msg = ""
            if total_changes > 0:
                parts = []
                if merge["new"]:
                    parts.append(f"+{merge['new']:,} new")
                if merge["changed"]:
                    parts.append(f"~{merge['changed']:,} changed")
                if merge["removed"]:
                    parts.append(f"-{merge['removed']:,} removed")
                merge_msg = f" | Synced: {', '.join(parts)}"
            self._progress.set_status(
                f"Restored {self._project.entry_count:,} entries "
                f"({translated:,} translated){merge_msg}"
            )
            logger.info("Restored state from %s", recovery_path)
            return True
        except Exception as e:
            logger.warning("Failed to restore recovery state: %s", e)
            return False

    def _get_game_fingerprint(self) -> str:
        """Get a fingerprint of the current game version using PAPGT CRC."""
        return self._get_game_build_info().get("fingerprint", "")

    def _looks_like_legacy_single_build_history(self, current_build_id: str) -> bool:
        """Detect old projects that were bootstrapped into a single baseline build."""
        if not self._project.entries:
            return False

        history_versions = set()
        for entry in self._project.entries:
            history = list(getattr(entry, "game_event_history", []) or [])
            if len(history) > 1:
                return False
            if history:
                event = history[0]
                if event.get("kind") != "baseline":
                    return False
                version = event.get("version", "")
                if version:
                    history_versions.add(version)

        if len(history_versions) > 1:
            return False
        only_version = next(iter(history_versions), "")
        if current_build_id and only_version and only_version != current_build_id:
            return False

        for item in self._project.update_history:
            if any(int(item.get(key, 0)) > 0 for key in ("new", "changed", "removed")):
                return False
        return True

    def _bootstrap_legacy_version_history(self, current_build_id: str, current_display: str) -> dict:
        """Infer a practical version history for pre-feature projects.

        All existing entries become the original legacy baseline.
        Pending entries are treated as the current update bucket so they are easy to review.
        """
        if not self._project.entries:
            return {"new": 0, "legacy_version": "", "legacy_display": ""}

        legacy_display = (
            f"Before {current_display}"
            if current_display
            else "Legacy Project Baseline"
        )
        legacy_version = (
            f"legacy-before:{current_build_id}"
            if current_build_id
            else "legacy-baseline"
        )

        pending_count = 0
        for entry in self._project.entries:
            entry.game_event_history = []
            entry.game_introduced_version = ""
            entry.game_last_seen_version = ""
            entry.game_last_changed_version = ""
            entry.game_removed_in_version = ""
            entry.game_sync_state = ""
            entry.record_game_event(legacy_version, "baseline")
            if current_build_id and entry.status == StringStatus.PENDING and not entry.locked:
                entry.record_game_event(current_build_id, "added", "legacy pending migration")
                pending_count += 1

        self._project.record_sync_summary({
            "version": legacy_version,
            "display": legacy_display,
            "new": 0,
            "changed": 0,
            "removed": 0,
        })
        logger.info(
            "Bootstrapped legacy translation history: %d baseline entries, %d current-update pending entries",
            len(self._project.entries),
            pending_count,
        )
        return {
            "new": pending_count,
            "legacy_version": legacy_version,
            "legacy_display": legacy_display,
        }

    def _merge_with_fresh_game_data(self, build_info: dict | None = None) -> dict:
        """Merge saved translations with fresh game paloc data.

        Reads the current game paloc, compares with saved project:
        - Entries with same key: keep translation, update original_text if changed
        - New entries in game: add as Pending
        - Entries removed from game: counted but kept with translations

        Returns a dict with: new, changed, removed, total_fresh, total_saved,
        changed_samples (list of (key, old_text, new_text) up to 5).
        """
        build_info = build_info or self._get_game_build_info()
        current_build_id = build_info.get("build_id", "")
        current_display = build_info.get("build_display", current_build_id)
        current_fingerprint = build_info.get("fingerprint", "")
        previous_display = self._project.game_build_display
        previous_version = self._project.game_build_id

        empty = {
            "version": current_build_id,
            "display": current_display,
            "previous_version": previous_version,
            "previous_display": previous_display,
            "new": 0,
            "changed": 0,
            "removed": 0,
            "total_fresh": 0,
            "total_saved": 0,
            "changed_samples": [],
            "new_samples": [],
            "removed_samples": [],
        }

        if not self._vfs or not self._discovered_palocs or not self._project.entries:
            return empty

        # Find the source paloc matching the project's source language
        source_file = self._project.source_file
        paloc_info = None
        for p in self._discovered_palocs:
            if p["filename"] == source_file:
                paloc_info = p
                break
        if not paloc_info:
            return empty

        try:
            from core.paloc_parser import parse_paloc
            group = paloc_info["group"]
            pamt_data = self._vfs.load_pamt(group)
            fresh_entry = None
            for fe in pamt_data.file_entries:
                if fe.path.lower().endswith(paloc_info["filename"].lower()):
                    fresh_entry = fe
                    break
            if not fresh_entry:
                return empty
            raw = self._vfs.read_entry_data(fresh_entry)
            fresh_entries = parse_paloc(raw)
            fresh_map = {}
            for e in fresh_entries:
                if not e.key.startswith("@") and not e.key.startswith("#"):
                    fresh_map[e.key] = e.value
        except Exception as ex:
            logger.warning("Failed to read fresh paloc for merge: %s", ex)
            return empty

        self._baseline_mgr.merge_into_baseline(
            source_file,
            list(fresh_map.items()),
            paloc_info.get("lang_code", self._project.source_lang),
            build_id=current_build_id,
            build_display=current_display,
            game_fingerprint=current_fingerprint,
        )

        # Build map of existing translations
        existing_map = {e.key: e for e in self._project.entries}
        legacy_bootstrap = {"new": 0, "legacy_version": "", "legacy_display": ""}
        if self._looks_like_legacy_single_build_history(current_build_id):
            legacy_bootstrap = self._bootstrap_legacy_version_history(
                current_build_id,
                current_display,
            )
            if legacy_bootstrap.get("legacy_version"):
                previous_version = legacy_bootstrap["legacy_version"]
            if legacy_bootstrap.get("legacy_display"):
                previous_display = legacy_bootstrap["legacy_display"]

        result = {
            "version": current_build_id,
            "display": current_display,
            "previous_version": previous_version,
            "previous_display": previous_display,
            "new": int(legacy_bootstrap.get("new", 0)),
            "changed": 0,
            "removed": 0,
            "total_fresh": len(fresh_map),
            "total_saved": len(existing_map),
            "changed_samples": [],
            "new_samples": [],
            "removed_samples": [],
        }

        bootstrap_version = self._project.game_build_id or current_build_id
        bootstrap_display = self._project.game_build_display or current_display
        for entry in self._project.entries:
            entry.clear_game_sync_state()
            if not entry.game_event_history and bootstrap_version:
                entry.game_last_seen_version = entry.game_last_seen_version or bootstrap_version
                entry.record_game_event(bootstrap_version, "baseline")

        # Update existing entries with fresh original text
        for entry in self._project.entries:
            if entry.key in fresh_map:
                fresh_text = fresh_map[entry.key]
                if current_build_id:
                    entry.game_last_seen_version = current_build_id
                if entry.game_removed_in_version and current_build_id:
                    entry.record_game_event(current_build_id, "added", "reintroduced")
                if fresh_text != entry.original_text:
                    if len(result["changed_samples"]) < 5:
                        old_preview = entry.original_text[:60]
                        new_preview = fresh_text[:60]
                        result["changed_samples"].append((entry.key, old_preview, new_preview))
                    entry.original_text = fresh_text
                    if entry.translated_text and entry.status != StringStatus.PENDING:
                        entry.status = StringStatus.TRANSLATED  # needs re-review
                    if current_build_id:
                        entry.record_game_event(current_build_id, "changed")
                    result["changed"] += 1

        # Count removed entries (in saved project but not in fresh game)
        for key, entry in existing_map.items():
            if key not in fresh_map:
                if len(result["removed_samples"]) < 5:
                    result["removed_samples"].append(key)
                if current_build_id:
                    entry.record_game_event(current_build_id, "removed")
                result["removed"] += 1

        # Add new entries from game
        max_index = max((e.index for e in self._project.entries), default=-1)
        new_entries = []
        for key, value in fresh_map.items():
            if key not in existing_map:
                max_index += 1
                from translation.translation_state import TranslationEntry
                entry = TranslationEntry(
                    index=max_index,
                    key=key,
                    original_text=value,
                )
                # Auto-lock untranslatable entries
                stripped = value.strip()
                if (not stripped
                        or stripped.startswith(("PHM_", "PHW_", "PHF_", "TODO", "TBD"))):
                    entry.translated_text = value
                    entry.status = StringStatus.APPROVED
                    entry.locked = True
                    entry.notes = "auto-locked: untranslatable"
                if current_build_id:
                    entry.game_last_seen_version = current_build_id
                    entry.record_game_event(current_build_id, "added")
                if len(result["new_samples"]) < 5:
                    result["new_samples"].append(key)
                new_entries.append(entry)
                result["new"] += 1

        if new_entries:
            self._project._entries.extend(new_entries)
            self._project._rebuild_index_map()

        total_changes = result["new"] + result["changed"] + result["removed"]
        if current_build_id:
            self._project.set_game_build(
                current_build_id,
                current_display,
                current_fingerprint,
            )
            self._project.record_sync_summary(result)
        if total_changes > 0:
            self._project.mark_modified()
        logger.info(
            "Fresh merge [%s]: %d new, %d changed, %d removed (fresh=%d, saved=%d)",
            current_display or "unknown build",
            result["new"], result["changed"], result["removed"],
            result["total_fresh"], result["total_saved"],
        )

        return result

    def _on_autosaved(self, time_str: str):
        self._autosave_label.setText(f"● Autosave: {time_str}")
        self._autosave_label.setStyleSheet("font-size: 11px; color: #a6e3a1; padding: 0 4px;")
