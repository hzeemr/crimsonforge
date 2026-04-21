"""Enterprise .prefab editor dialog.

Surfaces every editable string in a Pearl Abyss .prefab file —
file references (mesh / skeleton / xml paths), tag values
(_shrinkTag, socket names), property labels, and type tokens —
and lets the user rewrite any of them with a safe same-length
default plus an explicit "allow length change" override.

Layout
------
  Info bar:     file name + hash fingerprint + byte size
  Filter bar:   category dropdown + text search + safety toggle
  Main table:   Offset / Category / Property / Value / Length
  Details:      byte context + hex dump of the selected string
  Bottom bar:   revert / save to disk / patch to game

Community workflows supported
-----------------------------
  * qq_Hikka's body-part-hiding: find ``_shrinkTag`` value
    (e.g. ``Upperbody``), rename to a custom preset added in
    ``partshrinkdesc.xml`` (e.g. ``Clearbody``). Same-length
    edit is enforced by default.

  * Model swap: change ``.pac`` / ``.xml`` path reference to
    point at a different mesh. Length change allowed when the
    safety toggle is off.
"""

from __future__ import annotations

import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableView, QHeaderView, QAbstractItemView, QApplication,
    QMessageBox, QSplitter, QWidget, QComboBox, QLineEdit,
    QTextEdit, QCheckBox, QInputDialog,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer
from PySide6.QtGui import QColor, QFont

from core.prefab_parser import (
    ParsedPrefab, PrefabEdit, PrefabString,
    apply_edits, parse_prefab,
)
from core.pamt_parser import PamtFileEntry
from core.vfs_manager import VfsManager
from utils.logger import get_logger

logger = get_logger("ui.dialogs.prefab_editor")


# ---------------------------------------------------------------------------
# Category colours (match the Catppuccin theme used elsewhere)
# ---------------------------------------------------------------------------

_CATEGORY_COLORS = {
    "file_ref":      QColor("#a6e3a1"),   # green  — the big "swap this"
    "tag_value":     QColor("#f9e2af"),   # yellow — enum/tag values
    "property_name": QColor("#89b4fa"),   # blue   — structural (read-only)
    "type_name":     QColor("#cba6f7"),   # mauve  — type labels (read-only)
    "other":         QColor("#a6adc8"),   # muted  — leftover
}

_CATEGORY_LABELS = {
    "file_ref":      "File Reference",
    "tag_value":     "Tag / Enum Value",
    "property_name": "Property Name",
    "type_name":     "Type Name",
    "other":         "Other",
}

# property_name and type_name are structural — editing them corrupts the file
_READ_ONLY_CATEGORIES = frozenset({"property_name", "type_name"})


class _StringsModel(QAbstractTableModel):
    """Table model: Offset | Category | Property | Value | Length."""

    COLS = ("Offset", "Category", "Property", "Value", "Length")

    def __init__(self, prefab: ParsedPrefab, parent=None):
        super().__init__(parent)
        self._prefab = prefab
        self._all = list(prefab.strings)
        self._visible = list(self._all)
        # Pending edits keyed by prefix_offset
        self._edits: dict[int, str] = {}

    # ---- filtering -----------------------------------------------------

    def set_filter(self, category: str | None, text: str) -> None:
        text = (text or "").strip().lower()
        self.beginResetModel()
        self._visible = [
            s for s in self._all
            if (category is None or s.category == category)
            and (not text or text in s.value.lower()
                 or (s.property_name or "").lower().find(text) >= 0)
        ]
        self.endResetModel()

    def string_at_row(self, row: int) -> PrefabString | None:
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def pending_edits(self) -> dict[int, str]:
        return dict(self._edits)

    def clear_edits(self) -> None:
        self.beginResetModel()
        self._edits.clear()
        self.endResetModel()

    # ---- QAbstractTableModel required methods --------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row >= len(self._visible):
            return None
        s = self._visible[row]
        current_value = self._edits.get(s.prefix_offset, s.value)
        edited = s.prefix_offset in self._edits

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col == 0:
                return f"0x{s.prefix_offset:06x}"
            elif col == 1:
                return _CATEGORY_LABELS.get(s.category, s.category)
            elif col == 2:
                return s.property_name or ""
            elif col == 3:
                return current_value
            elif col == 4:
                orig_len = s.length
                new_len = len(current_value.encode("utf-8"))
                if new_len != orig_len:
                    return f"{orig_len} → {new_len}"
                return str(orig_len)

        if role == Qt.ForegroundRole:
            if col == 1:
                return _CATEGORY_COLORS.get(s.category)
            if col == 3 and edited:
                return QColor("#f9e2af")  # yellow — edited

        if role == Qt.FontRole and col == 3 and edited:
            f = QFont()
            f.setBold(True)
            return f

        if role == Qt.ToolTipRole:
            tip = (
                f"Offset:   0x{s.prefix_offset:06x}\n"
                f"Category: {_CATEGORY_LABELS.get(s.category, s.category)}\n"
                f"Length:   {s.length} bytes\n"
            )
            if s.property_name:
                tip += f"Property: {s.property_name}\n"
            if edited:
                tip += f"Original: {s.value!r}\n"
            tip += f"Value:    {current_value!r}"
            return tip

        return None

    def flags(self, index):
        base = super().flags(index)
        if not index.isValid():
            return base
        row = index.row()
        col = index.column()
        if row >= len(self._visible):
            return base
        s = self._visible[row]
        if col == 3 and s.category not in _READ_ONLY_CATEGORIES:
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        row = index.row()
        col = index.column()
        if col != 3 or row >= len(self._visible):
            return False
        s = self._visible[row]
        if s.category in _READ_ONLY_CATEGORIES:
            return False
        new_value = str(value)
        if new_value == s.value:
            self._edits.pop(s.prefix_offset, None)
        else:
            self._edits[s.prefix_offset] = new_value
        self.dataChanged.emit(index, self.index(row, 4))
        return True


class PrefabEditorDialog(QDialog):

    def __init__(
        self,
        prefab: ParsedPrefab,
        entry: PamtFileEntry | None = None,
        vfs: VfsManager | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._prefab = prefab
        self._entry = entry
        self._vfs = vfs

        name = os.path.basename(prefab.path) or "unknown.prefab"
        self.setWindowTitle(f"Prefab Editor — {name}")
        self.setMinimumSize(1000, 600)
        self.resize(1400, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Info bar ───────────────────────────────────────────────────
        info = QHBoxLayout()
        info.addWidget(QLabel(
            f"<b style='font-size:14px;'>{name}</b>"
        ))
        n_file_refs = len(prefab.file_references())
        n_tag_values = len(prefab.tag_values())
        info.addWidget(QLabel(
            f"<span style='color:#a6adc8;'>"
            f"{len(prefab.strings)} strings &nbsp;|&nbsp; "
            f"{n_file_refs} file refs &nbsp;|&nbsp; "
            f"{n_tag_values} tag values &nbsp;|&nbsp; "
            f"hash 0x{prefab.hash1:08x}/0x{prefab.hash2:08x} &nbsp;|&nbsp; "
            f"{len(prefab.raw):,} bytes"
            f"</span>"
        ))
        info.addStretch()
        layout.addLayout(info)

        # ── Filter bar ─────────────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Category:"))
        self._cat_combo = QComboBox()
        self._cat_combo.addItem("All", None)
        for cat, label in _CATEGORY_LABELS.items():
            self._cat_combo.addItem(label, cat)
        self._cat_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._cat_combo)

        filter_row.addWidget(QLabel("Search:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Filter by value or property name…")
        self._search_input.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filter)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        filter_row.addWidget(self._search_input, 1)

        self._same_length_cb = QCheckBox("Same-length edits only (safer)")
        self._same_length_cb.setChecked(True)
        self._same_length_cb.setToolTip(
            "When checked, any edit that would change the string's byte "
            "length is rejected. This is the community-documented safe "
            "mode (qq_Hikka guide). Uncheck to allow length changes — "
            "the length prefix will be updated and downstream bytes "
            "shifted."
        )
        filter_row.addWidget(self._same_length_cb)
        layout.addLayout(filter_row)

        # ── Table + details split ──────────────────────────────────────
        splitter = QSplitter(Qt.Vertical)

        self._model = _StringsModel(prefab)
        self._table_view = QTableView()
        self._table_view.setModel(self._model)
        self._table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table_view.setAlternatingRowColors(True)
        self._table_view.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.EditKeyPressed |
            QAbstractItemView.SelectedClicked
        )
        header = self._table_view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table_view.verticalHeader().setDefaultSectionSize(24)
        # Fixed section size + per-pixel scroll = smooth scroll on large
        # prefabs without per-row height measurement. (QTableView has no
        # setUniformRowHeights — that's a QTreeView method.)
        self._table_view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._table_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._table_view.selectionModel().currentRowChanged.connect(
            self._on_row_selected
        )
        splitter.addWidget(self._table_view)

        # ── Details pane ───────────────────────────────────────────────
        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.addWidget(QLabel("<b>Details</b>"))
        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setFont(QFont("Consolas", 9))
        details_layout.addWidget(self._details)
        splitter.addWidget(details_widget)
        splitter.setSizes([550, 150])
        layout.addWidget(splitter, 1)

        # ── Status + button bar ────────────────────────────────────────
        button_row = QHBoxLayout()
        self._status = QLabel("Ready.")
        self._status.setStyleSheet("color:#a6adc8;")
        button_row.addWidget(self._status, 1)

        revert_btn = QPushButton("Revert")
        revert_btn.setToolTip("Discard all pending edits.")
        revert_btn.clicked.connect(self._revert_all)
        button_row.addWidget(revert_btn)

        save_btn = QPushButton("Save As…")
        save_btn.setToolTip("Write the edited prefab to a new .prefab file on disk.")
        save_btn.clicked.connect(self._save_as)
        button_row.addWidget(save_btn)

        self._patch_btn = QPushButton("Patch to Game")
        self._patch_btn.setToolTip(
            "Write the edited prefab back into the live game archives "
            "(PAZ/PAMT/PAPGT). A backup of the original is made first."
        )
        self._patch_btn.setStyleSheet(
            "QPushButton { background:#a6e3a1; color:#1e1e2e; "
            "padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background:#94d38f; }"
            "QPushButton:disabled { background:#45475a; color:#6c7086; }"
        )
        self._patch_btn.clicked.connect(self._patch_to_game)
        self._patch_btn.setEnabled(entry is not None and vfs is not None)
        button_row.addWidget(self._patch_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    # ---------------------------------------------------------------- filter

    def _apply_filter(self) -> None:
        cat = self._cat_combo.currentData()
        text = self._search_input.text()
        self._model.set_filter(cat, text)

    # ---------------------------------------------------------------- details pane

    def _on_row_selected(self, current: QModelIndex, _prev: QModelIndex) -> None:
        s = self._model.string_at_row(current.row())
        if s is None:
            self._details.clear()
            return
        edited = self._model.pending_edits().get(s.prefix_offset)
        current_value = edited if edited is not None else s.value
        bytes_before = self._prefab.raw[max(0, s.prefix_offset - 8): s.prefix_offset]
        bytes_after = self._prefab.raw[s.prefix_offset + 4 + s.length:
                                       s.prefix_offset + 4 + s.length + 8]
        lines = [
            f"<b>Offset:</b>   0x{s.prefix_offset:06x} (length prefix) → "
            f"0x{s.value_offset:06x} (value)",
            f"<b>Category:</b> <span style='color:{_CATEGORY_COLORS[s.category].name()};'>"
            f"{_CATEGORY_LABELS.get(s.category, s.category)}</span>",
        ]
        if s.property_name:
            lines.append(f"<b>Property hint:</b> {s.property_name}")
        lines.append(f"<b>Original:</b> {s.value!r} ({s.length} bytes)")
        if edited is not None:
            new_len = len(edited.encode("utf-8"))
            delta = new_len - s.length
            lines.append(
                f"<b>Edited:</b>   <span style='color:#f9e2af;'>{edited!r}</span> "
                f"({new_len} bytes{f', Δ {delta:+d}' if delta else ''})"
            )
        lines.append("")
        lines.append(f"<b>Bytes before:</b> <code>{bytes_before.hex(' ')}</code>")
        lines.append(f"<b>Bytes after:</b>  <code>{bytes_after.hex(' ')}</code>")
        self._details.setHtml("<br>".join(lines))

    # ---------------------------------------------------------------- revert

    def _revert_all(self) -> None:
        if not self._model.pending_edits():
            return
        self._model.clear_edits()
        self._status.setText("Reverted all edits.")

    # ---------------------------------------------------------------- build new bytes

    def _build_edited_bytes(self) -> bytes:
        edits = [
            PrefabEdit(prefix_offset=off, new_value=val)
            for off, val in self._model.pending_edits().items()
        ]
        if not edits:
            return self._prefab.raw
        return apply_edits(
            self._prefab,
            edits,
            allow_length_change=not self._same_length_cb.isChecked(),
        )

    # ---------------------------------------------------------------- save

    def _save_as(self) -> None:
        try:
            new_data = self._build_edited_bytes()
        except ValueError as e:
            QMessageBox.critical(self, "Edit rejected", str(e))
            return
        from PySide6.QtWidgets import QFileDialog
        default = os.path.basename(self._prefab.path) or "edited.prefab"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Prefab", default, "Prefab (*.prefab);;All files (*)",
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(new_data)
        self._status.setText(f"Saved {len(new_data):,} bytes to {path}")

    # ---------------------------------------------------------------- patch

    def _patch_to_game(self) -> None:
        if self._entry is None or self._vfs is None:
            QMessageBox.warning(
                self, "Not available",
                "This dialog was opened without a VFS context — cannot "
                "patch back to the game. Use 'Save As…' instead.",
            )
            return
        try:
            new_data = self._build_edited_bytes()
        except ValueError as e:
            QMessageBox.critical(self, "Edit rejected", str(e))
            return
        if new_data == self._prefab.raw:
            QMessageBox.information(self, "No changes", "Nothing to patch — no edits made.")
            return
        reply = QMessageBox.question(
            self, "Patch to Game",
            f"Modify {self._entry.path} in live game archives?\n"
            f"Original: {len(self._prefab.raw):,} bytes → "
            f"New: {len(new_data):,} bytes\n"
            f"A backup will be created automatically.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from core.repack_engine import RepackEngine, ModifiedFile
            self._status.setText("Patching…")
            QApplication.processEvents()
            game = os.path.dirname(os.path.dirname(self._entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            grp = os.path.basename(os.path.dirname(self._entry.paz_file))
            pamt = self._vfs.load_pamt(grp)
            mf = ModifiedFile(
                data=new_data, entry=self._entry,
                pamt_data=pamt, package_group=grp,
            )
            result = RepackEngine(game).repack([mf], papgt_path=papgt)
            if result.success:
                QMessageBox.information(
                    self, "Patched",
                    f"Patched {self._entry.path}\n"
                    f"Original: {len(self._prefab.raw):,} → "
                    f"New: {len(new_data):,} bytes",
                )
                self._status.setText("Patch OK.")
                # Refresh in-memory model so follow-up edits operate on
                # the freshly patched bytes.
                self._prefab = parse_prefab(new_data, self._prefab.path)
                self._model = _StringsModel(self._prefab)
                self._table_view.setModel(self._model)
                self._apply_filter()
            else:
                QMessageBox.critical(
                    self, "Failed",
                    "\n".join(result.errors) if result.errors else "Unknown error",
                )
        except Exception as e:
            logger.exception("Patch failed")
            QMessageBox.critical(self, "Error", str(e))
