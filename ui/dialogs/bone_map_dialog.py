"""Bone-Map Editor dialog.

Lets the user review + override the PAA-track -> PAB-bone mapping
for a given rig. When the user clicks Save, the mapping is persisted
to ``%APPDATA%/CrimsonForge/bone_maps/<rig>.bonemap.json`` and
auto-loaded by the export pipeline for every subsequent PAA that
targets that rig.

Layout
------
  Top bar: rig key display + track/bone counts
  Table:   Track # | Confidence | Bind Quat | → PAB Bone dropdown
  Bottom:  Auto-correlate | Save | Revert | Close

The table has one row per PAA track. The rightmost dropdown lets
the user pick any PAB bone (or "-- drop --") per row. Auto-correlate
repopulates every dropdown with the bind-pose best-match guess.
"""

from __future__ import annotations

import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableView, QHeaderView, QAbstractItemView, QApplication,
    QMessageBox, QComboBox, QStyledItemDelegate,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QColor, QFont

from core.paa_bone_mapping import (
    BoneMap, auto_correlate, save_bone_map, load_bone_map,
)
from utils.logger import get_logger

logger = get_logger("ui.dialogs.bone_map")


_DROP_LABEL = "-- drop (don't export) --"


class _BoneDelegate(QStyledItemDelegate):
    """Per-row dropdown delegate listing every PAB bone + drop option."""

    def __init__(self, bone_labels: list[str], parent=None):
        super().__init__(parent)
        self._labels = [_DROP_LABEL] + bone_labels

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.addItems(self._labels)
        return cb

    def setEditorData(self, editor, index):
        current = index.data(Qt.EditRole)
        editor.setCurrentIndex(current + 1 if current is not None and current >= 0 else 0)

    def setModelData(self, editor, model, index):
        i = editor.currentIndex()
        pab_idx = -1 if i == 0 else i - 1
        model.setData(index, pab_idx, Qt.EditRole)


class _MapTableModel(QAbstractTableModel):
    """Rows = PAA tracks. Cols = Track # | Confidence | Bind | → PAB bone."""

    COLS = ("Track", "Confidence", "Bind xyzw", "→ PAB bone")

    def __init__(self, paa_tracks, pab_bones, bone_map: BoneMap, parent=None):
        super().__init__(parent)
        self._tracks = paa_tracks
        self._bones = pab_bones
        self._map = bone_map
        self._dirty = False

    def mark_clean(self) -> None:
        self._dirty = False

    def is_dirty(self) -> bool:
        return self._dirty

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._tracks)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        if role == Qt.DisplayRole and orientation == Qt.Vertical:
            return str(section)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._tracks):
            return None
        r = index.row()
        c = index.column()
        track = self._tracks[r]
        pab_idx = self._map.for_track(r)
        conf = self._map.confidence.get(r, 0.0)

        if role in (Qt.DisplayRole, Qt.EditRole):
            if c == 0:
                return r
            if c == 1:
                return f"{conf:.0%}"
            if c == 2:
                q = track.bind_quat
                return f"({q[0]:+.3f}, {q[1]:+.3f}, {q[2]:+.3f}, {q[3]:+.3f})"
            if c == 3:
                if role == Qt.EditRole:
                    return pab_idx
                if pab_idx < 0 or pab_idx >= len(self._bones):
                    return _DROP_LABEL
                return f"[{pab_idx:2d}] {self._bones[pab_idx].name}"

        if role == Qt.ForegroundRole and c == 1:
            # Colour confidence: green high, yellow mid, red low
            if conf >= 0.8:
                return QColor("#a6e3a1")
            if conf >= 0.5:
                return QColor("#f9e2af")
            return QColor("#f38ba8")

        if role == Qt.ForegroundRole and c == 3 and pab_idx < 0:
            return QColor("#585b70")

        if role == Qt.ToolTipRole and c == 3:
            return (
                f"PAA track {r} (bind {track.bind_quat})\n"
                f"Currently mapped to: "
                f"{self._bones[pab_idx].name if 0 <= pab_idx < len(self._bones) else '(dropped)'}\n"
                f"Confidence: {conf:.0%}"
            )
        return None

    def flags(self, index):
        base = super().flags(index)
        if not index.isValid():
            return base
        if index.column() == 3:
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or index.column() != 3:
            return False
        r = index.row()
        self._map.set(r, int(value), confidence=1.0)  # user edit = full confidence
        self._map.source = "user"
        self._dirty = True
        self.dataChanged.emit(
            self.index(r, 1), self.index(r, 3),
        )
        return True


class BoneMapDialog(QDialog):
    """Dialog to review + edit a bone map."""

    def __init__(self, paa_tracks, pab_bones, rig_key: str, parent=None):
        super().__init__(parent)
        self._tracks = paa_tracks
        self._bones = pab_bones
        self._rig_key = rig_key

        # Load existing override or compute auto-correlate
        existing = load_bone_map(rig_key)
        self._bone_map = existing or auto_correlate(
            paa_tracks, pab_bones, rig_key=rig_key,
        )

        self.setWindowTitle(f"Bone Mapping — rig={rig_key}")
        self.setMinimumSize(800, 500)
        self.resize(1000, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Info bar
        info = QHBoxLayout()
        info.addWidget(QLabel(
            f"<b style='font-size:14px;'>Rig:</b> {rig_key} "
            f"<span style='color:#a6adc8;'>"
            f"({len(paa_tracks)} PAA tracks × {len(pab_bones)} PAB bones) "
            f"source: {self._bone_map.source}"
            f"</span>"
        ))
        info.addStretch()
        layout.addLayout(info)

        # Table
        self._model = _MapTableModel(paa_tracks, pab_bones, self._bone_map)
        self._table = QTableView()
        self._table.setModel(self._model)
        delegate = _BoneDelegate(
            [f"[{i:2d}] {b.name}" for i, b in enumerate(pab_bones)], self,
        )
        self._table.setItemDelegateForColumn(3, delegate)
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self._table.setAlternatingRowColors(True)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        layout.addWidget(self._table, 1)

        # Bottom bar
        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#a6adc8;")
        bottom.addWidget(self._status, 1)

        auto_btn = QPushButton("Re-run Auto-correlate")
        auto_btn.setToolTip(
            "Reset every mapping to the bind-pose best-guess. "
            "Overwrites any manual edits."
        )
        auto_btn.clicked.connect(self._recorrelate)
        bottom.addWidget(auto_btn)

        revert_btn = QPushButton("Revert")
        revert_btn.setToolTip("Reload the saved override file (discards pending edits).")
        revert_btn.clicked.connect(self._revert)
        bottom.addWidget(revert_btn)

        save_btn = QPushButton("Save Override")
        save_btn.setStyleSheet(
            "QPushButton { background:#a6e3a1; color:#1e1e2e; "
            "padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background:#94d38f; }"
        )
        save_btn.setToolTip(
            "Persist this mapping to %APPDATA%/CrimsonForge/bone_maps/"
            f"{rig_key}.bonemap.json. Future PAAs on this rig will use it."
        )
        save_btn.clicked.connect(self._save)
        bottom.addWidget(save_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)

        layout.addLayout(bottom)

    def bone_map(self) -> BoneMap:
        """Return the (possibly edited) BoneMap to pass to the FBX exporter."""
        return self._bone_map

    # ---- actions ----------------------------------------------------------

    def _recorrelate(self) -> None:
        fresh = auto_correlate(self._tracks, self._bones, rig_key=self._rig_key)
        self._bone_map.mapping = fresh.mapping
        self._bone_map.confidence = fresh.confidence
        self._bone_map.source = "auto"
        self._model.layoutChanged.emit()
        self._model.mark_clean()
        self._status.setText("Re-correlated from bind poses.")

    def _revert(self) -> None:
        saved = load_bone_map(self._rig_key)
        if saved is None:
            QMessageBox.information(
                self, "No saved override",
                "No override file exists for this rig yet. "
                "Use 'Re-run Auto-correlate' to reset to auto-guess.",
            )
            return
        self._bone_map.mapping = saved.mapping
        self._bone_map.confidence = saved.confidence
        self._bone_map.source = saved.source
        self._model.layoutChanged.emit()
        self._model.mark_clean()
        self._status.setText("Reverted to saved override.")

    def _save(self) -> None:
        self._bone_map.source = "user"
        try:
            path = save_bone_map(self._bone_map)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self._model.mark_clean()
        self._status.setText(f"Saved: {path}")
