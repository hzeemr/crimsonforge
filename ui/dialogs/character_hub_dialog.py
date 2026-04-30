"""Unified per-character workspace.

Type a character name (e.g. "ogre", "hexe", "marie"), see every
related file in the game, and act on them with one click — open
in the right viewer, export them all to a Blender-ready folder, or
re-import an edited folder back into the game.

Why this exists
---------------
A single named character (the Ogre, for example) involves 539
files spread across 12 categories — 14 meshes, 404 animations, 7
physics shapes, 19 database tables, 14 localized strings. The
right-click flow surfaces files one at a time, which is fine for
small edits but a slog for full-character work. The Hub
collapses the whole graph into one view with bulk operations.

Layout
------
  Search bar              — text box + Resolve button + status label
  Header banner           — canonical key + counts + size
  Category tree           — collapsible per-category file list with
                            Open / Export buttons per row
  Bulk action row         — "Export All to Folder…" + "Re-import
                            Edited Folder…" + "Open in Viewer"

Non-modal so users can keep it open while editing meshes in
Blender + the main Explorer view alongside.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressDialog,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.character_asset_resolver import (
    AssetEntry, CATEGORIES, CharacterAssetBundle, resolve_character_assets,
)
from core.character_bulk_export import (
    BulkExportSummary, bulk_export_character,
)
from core.character_bulk_reimport import (
    BulkReimportSummary, bulk_reimport_character,
)
from utils.logger import get_logger

logger = get_logger("ui.dialogs.character_hub")


# Icons / colours per category (Catppuccin-mocha — same palette
# used elsewhere in the app).
_CAT_COLOURS = {
    "Mesh":                 "#a6e3a1",
    "Skeleton":             "#94e2d5",
    "Morph":                "#cba6f7",
    "Appearance / Prefab":  "#f9e2af",
    "Animation":            "#89b4fa",
    "Physics":              "#fab387",
    "Effects":              "#f38ba8",
    "Sequencer / Cutscene": "#cba6f7",
    "Texture":              "#74c7ec",
    "UI":                   "#a6adc8",
    "Database (game data)": "#f5c2e7",
    "Localization":         "#94e2d5",
    "Audio":                "#fab387",
    "Other":                "#7f849c",
}


class CharacterHubDialog(QDialog):
    """Unified per-character workspace dialog.

    Parameters
    ----------
    vfs
        Loaded VfsManager.
    parent
        Qt parent.
    initial_search
        Optional starting search term — populates the search box
        and resolves immediately. Useful for "right-click → Open
        in Character Hub" flows.
    inspect_callback
        Optional callable ``inspect_callback(vfs_path)`` invoked
        when the user clicks "Open" on a row. Lets the host
        Explorer route to the right per-file viewer (PABC viewer,
        PAC XML editor, etc.) without re-implementing dispatch
        here. If None, we just show the file path.
    """

    def __init__(
        self,
        vfs,
        parent=None,
        initial_search: str = "",
        inspect_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._vfs = vfs
        self._inspect_cb = inspect_callback
        self._bundle: Optional[CharacterAssetBundle] = None

        self.setWindowTitle("Character Hub — find every file for one character")
        self.setModal(False)
        self.resize(1200, 800)
        self._setup_ui()

        if initial_search:
            self._search_box.setText(initial_search)
            self._resolve()

    # ── UI construction ─────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # ── Search bar ──────────────────────────────
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Character name or key:"))
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(
            "e.g. ogre   |   hexe_marie   |   CD_M0001_00_Ogre"
        )
        self._search_box.returnPressed.connect(self._resolve)
        search_row.addWidget(self._search_box, 1)
        resolve_btn = QPushButton("Resolve")
        resolve_btn.setObjectName("primary")
        resolve_btn.setToolTip(
            "Walk every game archive + database table and find "
            "every file related to this name. Takes ~3-5 s on a "
            "stock install."
        )
        resolve_btn.clicked.connect(self._resolve)
        search_row.addWidget(resolve_btn)
        root.addLayout(search_row)

        # ── Header banner ───────────────────────────
        self._header = QLabel("Type a character name and click Resolve.")
        self._header.setWordWrap(True)
        self._header.setTextFormat(Qt.RichText)
        self._header.setStyleSheet(
            "color: #cdd6f4; padding: 8px 10px;"
            "background: #1e1e2e; border: 1px solid #45475a;"
            "border-radius: 6px; font-size: 12px;"
        )
        root.addWidget(self._header)

        # ── Category tree ───────────────────────────
        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Category / File", "Size", "Group", "Reason"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        h = self._tree.header()
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self._tree.setColumnWidth(0, 540)
        root.addWidget(self._tree, 1)

        # ── Bulk action row ─────────────────────────
        action_row = QHBoxLayout()
        action_row.addStretch()

        self._open_btn = QPushButton("Open Selected in Viewer")
        self._open_btn.setToolTip(
            "Open the selected file in its dedicated viewer (PAC "
            "viewer for meshes, PABC viewer for morph data, PAC "
            "XML editor for .pac_xml, etc.). Same routing as the "
            "Explorer right-click."
        )
        self._open_btn.clicked.connect(self._open_selected)
        self._open_btn.setEnabled(False)
        action_row.addWidget(self._open_btn)

        self._export_btn = QPushButton("Export All to Folder…")
        self._export_btn.setObjectName("primary")
        self._export_btn.setToolTip(
            "Bulk-export every mesh as OBJ + every texture as DDS "
            "into a chosen folder, plus a Blender setup script and "
            "a manifest for round-trip re-import. Game files are "
            "NOT modified."
        )
        self._export_btn.clicked.connect(self._export_all)
        self._export_btn.setEnabled(False)
        action_row.addWidget(self._export_btn)

        self._reimport_btn = QPushButton("Re-import Edited Folder…")
        self._reimport_btn.setObjectName("warning")
        self._reimport_btn.setToolTip(
            "Pick a previously-exported folder; rebuild every "
            ".pac from the edited OBJs. Default: build only (no "
            "game changes). You'll be asked before patching to "
            "the live game."
        )
        self._reimport_btn.clicked.connect(self._reimport_folder)
        self._reimport_btn.setEnabled(True)
        action_row.addWidget(self._reimport_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        action_row.addWidget(close_btn)
        root.addLayout(action_row)

    # ── Resolve flow ────────────────────────────────

    def _resolve(self) -> None:
        needle = self._search_box.text().strip()
        if len(needle) < 2:
            QMessageBox.information(
                self, "Search too short",
                "Type at least 2 characters of the character's name "
                "or key (e.g. 'ogre', 'hexe', 'marie').",
            )
            return
        self._header.setText(f"<i>Resolving '{needle}'…</i>")
        self._tree.clear()
        # Run synchronously — resolve takes ~3-5 s, fine for a
        # user-initiated action. If it ever needs async, wrap in a
        # FunctionWorker.
        try:
            bundle = resolve_character_assets(self._vfs, needle)
        except Exception as exc:
            logger.exception("resolve failed: %s", exc)
            QMessageBox.warning(self, "Resolve Error", str(exc))
            return
        self._bundle = bundle
        self._populate_tree(bundle)
        self._update_header(bundle)
        self._export_btn.setEnabled(bundle.total_files > 0)

    def _populate_tree(self, bundle: CharacterAssetBundle) -> None:
        self._tree.clear()
        bold = QFont()
        bold.setBold(True)
        for cat, entries in bundle.by_category.items():
            cat_item = QTreeWidgetItem(
                [f"{cat}  ({len(entries)})", "", "", ""]
            )
            colour = _CAT_COLOURS.get(cat, "#cdd6f4")
            cat_item.setForeground(0, _qcolor(colour))
            cat_item.setFont(0, bold)
            cat_item.setData(0, Qt.UserRole, ("category", cat))
            self._tree.addTopLevelItem(cat_item)
            for entry in entries:
                child = QTreeWidgetItem([
                    entry.path,
                    f"{entry.size:,}" if entry.size else "—",
                    entry.package_group,
                    entry.reason,
                ])
                child.setData(0, Qt.UserRole, ("file", entry.path))
                cat_item.addChild(child)
            # Auto-expand the most-relevant categories so users see
            # the meshes / morph files immediately. Leave Animation
            # collapsed — 400+ entries would dwarf the rest.
            if cat in ("Mesh", "Skeleton", "Morph", "Appearance / Prefab"):
                cat_item.setExpanded(True)

    def _update_header(self, bundle: CharacterAssetBundle) -> None:
        if bundle.total_files == 0:
            self._header.setText(
                f"<b>No files found for '{bundle.needle}'.</b> "
                "Try a different name or a partial match."
            )
            return
        size_mb = bundle.total_size_bytes / 1024 / 1024
        cat_summary = " · ".join(
            f"<span style='color:{_CAT_COLOURS.get(c, '#cdd6f4')};'>"
            f"{c}: {len(es)}</span>"
            for c, es in bundle.by_category.items()
        )
        self._header.setText(
            f"<b>Canonical key:</b> {bundle.canonical_key or '(unknown)'}"
            f" · <b>{bundle.total_files} files</b> · "
            f"<b>{size_mb:.1f} MB</b><br>{cat_summary}"
        )

    # ── Selection actions ───────────────────────────

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        kind, payload = (item.data(0, Qt.UserRole) or (None, None))
        if kind == "file":
            self._dispatch_open(payload)

    def _open_selected(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            QMessageBox.information(
                self, "Open Selected",
                "Select a file row in the tree first.",
            )
            return
        kind, payload = items[0].data(0, Qt.UserRole) or (None, None)
        if kind != "file":
            return
        self._dispatch_open(payload)

    def _dispatch_open(self, vfs_path: str) -> None:
        if self._inspect_cb:
            try:
                self._inspect_cb(vfs_path)
                return
            except Exception as exc:
                logger.warning("inspect callback failed: %s", exc)
        # Fallback: just show the path.
        QMessageBox.information(
            self, "File path", vfs_path,
        )

    def _file_clicked(self) -> None:
        items = self._tree.selectedItems()
        self._open_btn.setEnabled(
            bool(items)
            and (items[0].data(0, Qt.UserRole) or (None, None))[0] == "file"
        )

    # ── Bulk export ─────────────────────────────────

    def _export_all(self) -> None:
        if self._bundle is None or self._bundle.total_files == 0:
            return
        from ui.dialogs.file_picker import pick_directory
        out_dir = pick_directory(
            self,
            f"Pick output folder for '{self._bundle.canonical_key}' export",
        )
        if not out_dir:
            return

        # Estimate work = meshes + textures (others are free).
        meshes = sum(1 for e in self._bundle.entries
                     if e.path.lower().endswith((".pac", ".pam", ".pamlod")))
        textures = sum(1 for e in self._bundle.entries
                       if e.path.lower().endswith(".dds"))
        total = meshes + textures
        if total == 0:
            QMessageBox.information(
                self, "Nothing to export",
                "This bundle has no exportable meshes or textures.",
            )
            return

        progress = QProgressDialog(
            "Exporting…", "Cancel", 0, total, self,
        )
        progress.setWindowTitle("Bulk Export")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def _cb(cur, tot, label):
            progress.setMaximum(tot)
            progress.setValue(cur)
            progress.setLabelText(label)
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()

        try:
            summary = bulk_export_character(
                self._bundle, self._vfs, out_dir, progress_cb=_cb,
            )
        except Exception as exc:
            logger.exception("bulk export failed: %s", exc)
            progress.close()
            QMessageBox.warning(self, "Export Error", str(exc))
            return
        progress.close()

        msg = QMessageBox(self)
        msg.setWindowTitle("Bulk Export Complete")
        msg.setText(summary.report())
        msg.setIcon(QMessageBox.Information)
        msg.exec()

    # ── Bulk re-import ──────────────────────────────

    def _reimport_folder(self) -> None:
        from ui.dialogs.file_picker import pick_directory
        in_dir = pick_directory(
            self, "Pick a previously-exported folder to re-import",
        )
        if not in_dir:
            return
        manifest = Path(in_dir) / "manifest.json"
        if not manifest.is_file():
            QMessageBox.warning(
                self, "Not a bundle folder",
                f"No manifest.json found in {in_dir}. Pick a folder "
                "that was produced by Export All to Folder…",
            )
            return

        # Ask whether to commit to game.
        ans = QMessageBox.question(
            self, "Re-import Mode",
            "Build only (NO game changes), or Build + Patch to Game?\n\n"
            "  • Yes  = Build + Patch (changes the live game; auto-backup)\n"
            "  • No   = Build only (writes rebuilt PACs to the folder for "
            "review)\n"
            "  • Cancel = Abort",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No,
        )
        if ans == QMessageBox.Cancel:
            return
        patch = (ans == QMessageBox.Yes)

        # Total = number of mesh entries in the manifest.
        try:
            import json
            data = json.loads(manifest.read_text(encoding="utf-8"))
            total = len(data.get("exported_meshes", []))
        except Exception:
            total = 1

        progress = QProgressDialog(
            "Rebuilding…", "Cancel", 0, total, self,
        )
        progress.setWindowTitle("Bulk Re-import")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def _cb(cur, tot, label):
            progress.setMaximum(tot)
            progress.setValue(cur)
            progress.setLabelText(label)
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()

        try:
            summary = bulk_reimport_character(
                in_dir, self._vfs, patch_to_game=patch, progress_cb=_cb,
            )
        except Exception as exc:
            logger.exception("bulk re-import failed: %s", exc)
            progress.close()
            QMessageBox.warning(self, "Re-import Error", str(exc))
            return
        progress.close()

        QMessageBox.information(
            self, "Bulk Re-import Complete", summary.report(),
        )


def _qcolor(hex_str: str):
    from PySide6.QtGui import QColor
    return QColor(hex_str)
