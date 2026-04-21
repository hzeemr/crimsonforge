"""Enterprise-level Game Data Table (.pabgb) editor dialog.

Layout: Left row-list + right field-editor with smart analysis.
  - Left: searchable/filterable row list with name + hash columns
  - Right top: field filter bar with smart categories
  - Right middle: editable field table with auto-labels and color coding
  - Right bottom: details pane with hex dump, analysis, and row comparison
  - Bottom bar: duplicate/delete row, status, patch-to-game
"""

from __future__ import annotations

import math
import os
import struct
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableView, QHeaderView, QAbstractItemView, QApplication,
    QMessageBox, QSplitter, QWidget, QComboBox, QLineEdit,
    QTextEdit,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer
from PySide6.QtGui import QColor, QFont

from core.pabgb_parser import (
    PabgbTable, PabgbRow, PabgbField, serialize_pabgb, serialize_header,
)
from core.pamt_parser import PamtFileEntry
from core.vfs_manager import VfsManager
from utils.logger import get_logger

logger = get_logger("ui.dialogs.pabgb_editor")

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
_CLR_STR = QColor("#a6e3a1")
_CLR_F32 = QColor("#f9e2af")
_CLR_U32 = QColor("#cdd6f4")
_CLR_I32 = QColor("#89b4fa")
_CLR_HASH = QColor("#cba6f7")
_CLR_BLOB = QColor("#6c7086")
_CLR_NAME = QColor("#f38ba8")
_CLR_DIM = QColor("#585b70")
_CLR_SCALE = QColor("#94e2d5")   # Likely scale/multiplier
_CLR_ZERO = QColor("#45475a")    # Zero values (dimmed)

_KIND_COLORS = {
    "str": _CLR_STR, "f32": _CLR_F32, "u32": _CLR_U32,
    "i32": _CLR_I32, "hash": _CLR_HASH, "blob": _CLR_BLOB,
}

# ---------------------------------------------------------------------------
# Smart field labelling
# ---------------------------------------------------------------------------
def _guess_field_label(f: PabgbField, field_idx: int, row_name: str) -> str:
    """Heuristic label for a field based on its position, type, and value."""
    if f.kind == "str":
        if field_idx == 0:
            return "Name"
        return "String"

    if f.kind == "f32" and isinstance(f.value, (int, float)):
        v = float(f.value)
        if v == 0.0:
            return ""
        if 0.9 <= abs(v) <= 1.1:
            return "Scale?"
        if 0.1 <= abs(v) <= 0.5:
            return "Rate/Speed?"
        if 1.5 <= abs(v) <= 100:
            return "Range/Size?"
        return "Float"

    if f.kind == "u32" and isinstance(f.value, int):
        v = f.value
        if v == 0:
            return ""
        if v == 0xFFFFFFFF:
            return "None/-1"
        if v == 1:
            return "True/Enabled?"
        if 1 < v <= 100:
            return "Count/Level?"
        if 100 < v <= 10000:
            return "Value/Stat?"
        if v > 0x00100000:
            return "Hash/Ref"
        return ""

    return ""


def _field_category(f: PabgbField) -> str:
    """Categorize a field for filtering."""
    if f.kind == "str":
        return "strings"
    if f.kind == "f32":
        return "floats"
    if f.kind == "u32" and isinstance(f.value, int):
        if f.value == 0:
            return "zeros"
        if f.value > 0x00100000:
            return "hashes"
        return "numbers"
    if f.kind == "blob":
        return "other"
    return "numbers"


# ===================================================================
# Row List Model (left panel)
# ===================================================================
class _RowListModel(QAbstractTableModel):
    _HEADERS = ["Row Name", "Hash"]

    def __init__(self, table: PabgbTable, parent=None):
        super().__init__(parent)
        self._table = table
        self._filtered: list[int] = list(range(len(table.rows)))
        self._search = ""
        self._filter_mode = "all"

    def set_filter(self, search: str = "", mode: str = "all"):
        self.beginResetModel()
        self._search = search.strip().lower()
        self._filter_mode = mode
        self._refilter()
        self.endResetModel()

    def _refilter(self):
        result = []
        for i, row in enumerate(self._table.rows):
            if self._filter_mode == "named" and not row.name:
                continue
            if self._filter_mode == "hash_only" and row.name:
                continue
            if self._search:
                haystack = (row.display_name + f" 0x{row.row_hash:08X}").lower()
                if self._search not in haystack:
                    continue
            result.append(i)
        self._filtered = result

    def row_at(self, view_row: int) -> PabgbRow | None:
        if 0 <= view_row < len(self._filtered):
            return self._table.rows[self._filtered[view_row]]
        return None

    def real_index(self, view_row: int) -> int:
        if 0 <= view_row < len(self._filtered):
            return self._filtered[view_row]
        return -1

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return 2

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.row_at(index.row())
        if not row:
            return None
        col = index.column()
        if role == Qt.DisplayRole:
            return row.display_name if col == 0 else f"0x{row.row_hash:08X}"
        if role == Qt.ForegroundRole:
            return (_CLR_NAME if row.name else _CLR_DIM) if col == 0 else _CLR_DIM
        if role == Qt.ToolTipRole:
            return (f"Row {row.index} | Hash: 0x{row.row_hash:08X}\n"
                    f"Offset: {row.data_offset} | Size: {row.data_size} bytes | Fields: {len(row.fields)}")
        return None

    @property
    def filtered_count(self):
        return len(self._filtered)

    @property
    def total_count(self):
        return len(self._table.rows)


# ===================================================================
# Field Table Model (right panel — shows fields for ONE selected row)
# ===================================================================
class _FieldModel(QAbstractTableModel):
    _HEADERS = ["#", "Type", "Label", "Value", "Hex"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._row: PabgbRow | None = None
        self._hash_names: dict[int, str] = {}
        self._filtered: list[int] = []
        self._field_filter = "all"

    def set_row(self, row: PabgbRow | None):
        self.beginResetModel()
        self._row = row
        self._refilter()
        self.endResetModel()

    def set_hash_names(self, names: dict[int, str]):
        self._hash_names = names

    def set_field_filter(self, category: str):
        self.beginResetModel()
        self._field_filter = category.lower()
        self._refilter()
        self.endResetModel()

    def _refilter(self):
        if not self._row:
            self._filtered = []
            return
        if self._field_filter == "all":
            self._filtered = list(range(len(self._row.fields)))
        elif self._field_filter == "non-zero":
            self._filtered = [i for i, f in enumerate(self._row.fields)
                              if not (f.kind == "u32" and f.value == 0)]
        elif self._field_filter == "scale/size":
            self._filtered = [i for i, f in enumerate(self._row.fields)
                              if f.kind == "f32" and isinstance(f.value, (int, float))
                              and 0.01 < abs(float(f.value)) < 1000]
        elif self._field_filter == "strings":
            self._filtered = [i for i, f in enumerate(self._row.fields) if f.kind == "str"]
        elif self._field_filter == "counts/stats":
            self._filtered = [i for i, f in enumerate(self._row.fields)
                              if f.kind == "u32" and isinstance(f.value, int)
                              and 0 < f.value <= 10000]
        elif self._field_filter == "hash refs":
            self._filtered = [i for i, f in enumerate(self._row.fields)
                              if f.kind == "u32" and isinstance(f.value, int)
                              and f.value > 0x00100000]
        else:
            self._filtered = list(range(len(self._row.fields)))

    def field_at(self, view_row: int) -> tuple[int, PabgbField] | None:
        if self._row and 0 <= view_row < len(self._filtered):
            fi = self._filtered[view_row]
            return fi, self._row.fields[fi]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return 5

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not self._row:
            return None
        result = self.field_at(index.row())
        if not result:
            return None
        fi, f = result
        col = index.column()

        if role == Qt.EditRole and col == 3:
            # Populate the edit box with the raw value (not the display format)
            if f.kind == "str":
                return str(f.value)
            if f.kind == "f32":
                return f"{float(f.value):.6f}"
            if f.kind in ("u32", "hash"):
                v = f.value
                if isinstance(v, int) and v > 0xFFFF:
                    return f"0x{v:08X}"
                return str(v)
            if f.kind == "i32":
                return str(f.value)
            return str(f.value)

        if role == Qt.DisplayRole:
            if col == 0:
                return str(fi)
            if col == 1:
                return f.kind
            if col == 2:
                label = _guess_field_label(f, fi, self._row.name or "")
                if f.kind == "u32" and isinstance(f.value, int) and f.value in self._hash_names:
                    label = self._hash_names[f.value]
                return label
            if col == 3:
                return f.display_value()
            if col == 4:
                return f.raw.hex()[:24] + ("..." if len(f.raw) > 12 else "")

        if role == Qt.ForegroundRole:
            if col == 2:
                # Label column — use special colors for guessed labels
                if f.kind == "u32" and isinstance(f.value, int) and f.value in self._hash_names:
                    return _CLR_SCALE
                return _CLR_DIM
            if col == 3:
                # Value column — dim zeros, highlight scales
                if f.kind == "u32" and f.value == 0:
                    return _CLR_ZERO
                if f.kind == "f32" and isinstance(f.value, (int, float)):
                    v = abs(float(f.value))
                    if 0.8 <= v <= 1.2:
                        return _CLR_SCALE
                return _KIND_COLORS.get(f.kind, _CLR_U32)
            if col in (1, 4):
                return _CLR_DIM

        if role == Qt.FontRole:
            if col == 3 and f.kind == "f32" and isinstance(f.value, (int, float)):
                v = abs(float(f.value))
                if 0.8 <= v <= 1.2:
                    font = QFont()
                    font.setBold(True)
                    return font

        if role == Qt.ToolTipRole:
            tip = f"Field {fi} | Type: {f.kind} | Offset: {f.offset} | Size: {f.size}\nRaw: {f.raw.hex()}"
            if f.kind == "u32" and isinstance(f.value, int) and f.value in self._hash_names:
                tip += f"\nResolved: {self._hash_names[f.value]}"
            return tip

        return None

    def flags(self, index):
        base = super().flags(index)
        if not index.isValid() or not self._row or index.column() != 3:
            return base
        result = self.field_at(index.row())
        if result:
            _, f = result
            if f.kind in ("u32", "f32", "i32", "str", "hash"):
                return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid() or not self._row or index.column() != 3:
            return False
        result = self.field_at(index.row())
        if not result:
            return False
        fi, f = result
        text = str(value).strip()
        try:
            if f.kind in ("u32", "hash"):
                new_val = int(text, 16) if text.startswith("0x") else int(text)
                new_raw = struct.pack("<I", new_val & 0xFFFFFFFF)
            elif f.kind == "i32":
                new_val = int(text, 16) if text.startswith("0x") else int(text)
                new_raw = struct.pack("<i", new_val)
            elif f.kind == "f32":
                new_val = float(text)
                new_raw = struct.pack("<f", new_val)
            elif f.kind == "str":
                new_val = text
                new_raw = struct.pack("<I", len(text) + 1) + text.encode("utf-8") + b"\x00"
            else:
                return False
            self._row.fields[fi] = PabgbField(f.offset, len(new_raw), new_raw, f.kind, new_val)
            self.dataChanged.emit(index, index, [Qt.DisplayRole])
            return True
        except (ValueError, TypeError, struct.error):
            return False


# ===================================================================
# Main Dialog
# ===================================================================
class PabgbEditorDialog(QDialog):
    def __init__(
        self,
        table: PabgbTable,
        entry: PamtFileEntry,
        vfs: VfsManager,
        patch_mode: bool = False,
        initial_search: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._table = table
        self._entry = entry
        self._vfs = vfs
        self._patch_mode = patch_mode
        self._hash_names: dict[int, str] = {}
        self._compare_row: PabgbRow | None = None

        self.setWindowTitle(f"Game Data Editor — {table.file_name}")
        self.setMinimumSize(1100, 650)
        self.resize(1500, 850)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Info bar ──
        info = QHBoxLayout()
        info.addWidget(QLabel(
            f"<b style='font-size:14px;'>{table.file_name}</b>"
        ))
        info.addWidget(QLabel(
            f"<span style='color:#a6adc8;'>{len(table.rows)} rows  |  "
            f"{'simple' if table.is_simple else 'hashed'}  |  "
            f"{len(table.raw_data):,} bytes</span>"
        ))
        info.addStretch()

        # Simple / Expert toggle
        self._mode_btn = QPushButton("Expert")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setChecked(False)  # Simple by default
        self._mode_btn.setToolTip("Toggle between Simple (friendly view) and Expert (raw table editor)")
        self._mode_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #cdd6f4; padding: 4px 14px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:checked { background: #89b4fa; color: #1e1e2e; }")
        self._mode_btn.toggled.connect(self._toggle_mode)
        info.addWidget(self._mode_btn)

        resolve_btn = QPushButton("Resolve Hashes")
        resolve_btn.setToolTip("Load character/formation/item names to annotate hash values.")
        resolve_btn.clicked.connect(self._resolve_hashes)
        info.addWidget(resolve_btn)
        layout.addLayout(info)

        # ── Simple Mode View ── (shown by default)
        self._simple_widget = self._build_simple_view()
        layout.addWidget(self._simple_widget)

        # ── Expert container (hidden in simple mode) ──
        self._expert_widget = QWidget()
        expert_layout = QVBoxLayout(self._expert_widget)
        expert_layout.setContentsMargins(0, 0, 0, 0)
        expert_layout.setSpacing(4)
        layout.addWidget(self._expert_widget, 1)

        # ── Row search bar ──
        row_filter = QHBoxLayout()
        row_filter.addWidget(QLabel("Search rows:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Type name or hash to filter rows...")
        self._search_input.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_row_filter)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        row_filter.addWidget(self._search_input, 1)

        row_filter.addWidget(QLabel("Show:"))
        self._show_combo = QComboBox()
        self._show_combo.addItems(["All Rows", "Named Only", "Hash Only"])
        self._show_combo.currentTextChanged.connect(lambda _: self._apply_row_filter())
        row_filter.addWidget(self._show_combo)

        self._count_label = QLabel(f"{len(table.rows)} / {len(table.rows)}")
        self._count_label.setStyleSheet("color: #89b4fa; font-weight: 600; padding: 0 8px;")
        row_filter.addWidget(self._count_label)
        expert_layout.addLayout(row_filter)

        # ── Main splitter ──
        main_splitter = QSplitter(Qt.Horizontal)

        # LEFT: Row list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)
        ll.addWidget(QLabel("<b>Records</b>"))

        self._row_model = _RowListModel(table, self)
        self._row_view = QTableView()
        self._row_view.setModel(self._row_model)
        self._row_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._row_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._row_view.setAlternatingRowColors(True)
        self._row_view.setShowGrid(False)
        self._row_view.verticalHeader().setVisible(False)
        self._row_view.verticalHeader().setDefaultSectionSize(22)
        self._row_view.verticalHeader().setMinimumSectionSize(20)
        self._row_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._row_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._row_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._row_view.setColumnWidth(1, 100)
        self._row_view.selectionModel().currentRowChanged.connect(self._on_row_selected)
        ll.addWidget(self._row_view, 1)

        compare_btn = QPushButton("Set as Compare Base")
        compare_btn.setToolTip("Mark the selected row as baseline — differences will be highlighted when you click other rows.")
        compare_btn.clicked.connect(self._set_compare_row)
        ll.addWidget(compare_btn)

        main_splitter.addWidget(left)

        # RIGHT: Field editor + details
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)

        # Field header + filter
        field_header = QHBoxLayout()
        self._field_title = QLabel("<b>Fields</b> — select a row")
        field_header.addWidget(self._field_title, 1)

        field_header.addWidget(QLabel("Filter:"))
        self._field_filter = QComboBox()
        self._field_filter.addItems([
            "All Fields", "Non-Zero", "Scale/Size", "Strings",
            "Counts/Stats", "Hash Refs",
        ])
        self._field_filter.setToolTip(
            "Filter fields by category:\n"
            "  All Fields — show everything\n"
            "  Non-Zero — hide zero-value fields\n"
            "  Scale/Size — floats between 0.01-1000 (likely scale/size/speed)\n"
            "  Strings — text fields only\n"
            "  Counts/Stats — integers 1-10000 (likely stats/levels/counts)\n"
            "  Hash Refs — large integers (likely references to other records)"
        )
        self._field_filter.currentTextChanged.connect(self._apply_field_filter)
        field_header.addWidget(self._field_filter)

        self._field_count_label = QLabel("")
        self._field_count_label.setStyleSheet("color: #89b4fa; padding: 0 4px;")
        field_header.addWidget(self._field_count_label)
        rl.addLayout(field_header)

        right_splitter = QSplitter(Qt.Vertical)

        # Field table
        self._field_model = _FieldModel(self)
        self._field_view = QTableView()
        self._field_view.setModel(self._field_model)
        self._field_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._field_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._field_view.setAlternatingRowColors(True)
        self._field_view.verticalHeader().setVisible(False)
        self._field_view.verticalHeader().setDefaultSectionSize(28)
        self._field_view.verticalHeader().setMinimumSectionSize(26)
        self._field_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._field_view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._field_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self._field_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._field_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self._field_view.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._field_view.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        self._field_view.setColumnWidth(0, 45)
        self._field_view.setColumnWidth(1, 45)
        self._field_view.setColumnWidth(2, 120)
        self._field_view.setColumnWidth(4, 140)
        right_splitter.addWidget(self._field_view)

        # Details / compare pane
        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setPlaceholderText("Select a row to see details. Use 'Set as Compare Base' to diff rows.")
        self._details.setMaximumHeight(200)
        right_splitter.addWidget(self._details)

        right_splitter.setStretchFactor(0, 4)
        right_splitter.setStretchFactor(1, 1)
        rl.addWidget(right_splitter, 1)

        main_splitter.addWidget(right)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([280, 1120])
        expert_layout.addWidget(main_splitter, 1)

        # ── Button bar ──
        btns = QHBoxLayout()
        dup_btn = QPushButton("Duplicate Row")
        dup_btn.clicked.connect(self._duplicate_row)
        btns.addWidget(dup_btn)
        del_btn = QPushButton("Delete Row")
        del_btn.clicked.connect(self._delete_row)
        btns.addWidget(del_btn)
        btns.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet("color: #89b4fa;")
        btns.addWidget(self._status)

        if patch_mode:
            patch_btn = QPushButton("Save + Patch to Game")
            patch_btn.setObjectName("primary")
            patch_btn.clicked.connect(self._patch_to_game)
            btns.addWidget(patch_btn)
        else:
            save_btn = QPushButton("Export Binary")
            save_btn.clicked.connect(self._export_binary)
            btns.addWidget(save_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        # ── Init ──
        # Start in Simple mode (Expert hidden)
        self._expert_widget.setVisible(False)
        self._simple_widget.setVisible(True)

        if initial_search:
            self._search_input.setText(initial_search)
            self._apply_row_filter()
        if table.rows:
            self._row_view.selectRow(0)

    # ------------------------------------------------------------------
    # Row selection
    # ------------------------------------------------------------------
    def _on_row_selected(self, current: QModelIndex, _prev: QModelIndex):
        row = self._row_model.row_at(current.row()) if current.isValid() else None
        self._field_model.set_row(row)
        if row:
            fc = self._field_model.rowCount()
            tc = len(row.fields)
            self._field_title.setText(
                f"<b>Fields</b> — {row.display_name}"
            )
            self._field_count_label.setText(f"{fc} / {tc} fields")
            self._update_details(row)
        else:
            self._field_title.setText("<b>Fields</b> — select a row")
            self._field_count_label.setText("")
            self._details.clear()

    def _update_details(self, row: PabgbRow):
        lines = []
        lines.append(f"=== {row.display_name} ===")
        lines.append(f"Hash: 0x{row.row_hash:08X}  |  Index: {row.index}  |  "
                      f"Offset: {row.data_offset}  |  Size: {row.data_size} bytes  |  "
                      f"Fields: {len(row.fields)}")

        # Type summary
        types = {}
        for f in row.fields:
            types[f.kind] = types.get(f.kind, 0) + 1
        lines.append(f"Types: {', '.join(f'{k}={v}' for k, v in sorted(types.items()))}")

        # Strings
        strings = [(i, f) for i, f in enumerate(row.fields) if f.kind == "str"]
        if strings:
            lines.append(f"\nStrings ({len(strings)}):")
            for i, f in strings:
                lines.append(f"  [{i}] {f.value}")

        # Scale/multiplier candidates (floats near 1.0)
        scales = [(i, f) for i, f in enumerate(row.fields)
                  if f.kind == "f32" and isinstance(f.value, (int, float))
                  and 0.5 <= abs(float(f.value)) <= 2.0 and float(f.value) != 0]
        if scales:
            lines.append(f"\nLikely Scale/Multiplier fields ({len(scales)}):")
            for i, f in scales:
                lines.append(f"  [{i}] {float(f.value):.4f}")

        # Compare with baseline
        if self._compare_row and self._compare_row.index != row.index:
            lines.append(f"\n=== DIFF vs {self._compare_row.display_name} ===")
            diffs = self._compute_diff(self._compare_row, row)
            if diffs:
                lines.append(f"{len(diffs)} fields differ:")
                for fi, old_val, new_val in diffs[:50]:
                    lines.append(f"  [{fi}] {old_val} → {new_val}")
                if len(diffs) > 50:
                    lines.append(f"  ... and {len(diffs) - 50} more")
            else:
                lines.append("  No differences found (identical data)")

        self._details.setPlainText("\n".join(lines))

    @staticmethod
    def _compute_diff(base: PabgbRow, other: PabgbRow) -> list[tuple[int, str, str]]:
        diffs = []
        max_fields = max(len(base.fields), len(other.fields))
        for i in range(max_fields):
            if i >= len(base.fields):
                diffs.append((i, "<missing>", other.fields[i].display_value()))
            elif i >= len(other.fields):
                diffs.append((i, base.fields[i].display_value(), "<missing>"))
            elif base.fields[i].raw != other.fields[i].raw:
                diffs.append((i, base.fields[i].display_value(), other.fields[i].display_value()))
        return diffs

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def _apply_row_filter(self):
        mode_map = {"All Rows": "all", "Named Only": "named", "Hash Only": "hash_only"}
        mode = mode_map.get(self._show_combo.currentText(), "all")
        self._row_model.set_filter(self._search_input.text(), mode)
        self._count_label.setText(
            f"{self._row_model.filtered_count} / {self._row_model.total_count}"
        )

    def _apply_field_filter(self, text: str):
        filter_map = {
            "All Fields": "all", "Non-Zero": "non-zero", "Scale/Size": "scale/size",
            "Strings": "strings", "Counts/Stats": "counts/stats", "Hash Refs": "hash refs",
        }
        self._field_model.set_field_filter(filter_map.get(text, "all"))
        self._field_count_label.setText(
            f"{self._field_model.rowCount()} / "
            f"{len(self._field_model._row.fields) if self._field_model._row else 0} fields"
        )

    # ------------------------------------------------------------------
    # Compare
    # ------------------------------------------------------------------
    def _set_compare_row(self):
        idx = self._row_view.currentIndex()
        if idx.isValid():
            self._compare_row = self._row_model.row_at(idx.row())
            if self._compare_row:
                self._status.setText(f"Compare base: {self._compare_row.display_name}")

    # ------------------------------------------------------------------
    # Hash resolution
    # ------------------------------------------------------------------
    def _resolve_hashes(self):
        self._status.setText("Resolving hashes...")
        QApplication.processEvents()
        try:
            names: dict[int, str] = {}
            for row in self._table.rows:
                if row.name:
                    names[row.row_hash] = row.name

            pamt = self._vfs.load_pamt("0008")
            for entry in pamt.file_entries:
                lower = entry.path.lower()
                if lower in ("gamedata/characterinfo.pabgb", "gamedata/formationinfo.pabgb",
                             "gamedata/allygroupinfo.pabgb"):
                    header_path = entry.path[:-1] + "h"
                    he = None
                    for h in pamt.file_entries:
                        if h.path.lower() == header_path.lower():
                            he = h
                            break
                    if he:
                        from core.pabgb_parser import parse_pabgb
                        d = self._vfs.read_entry_data(entry)
                        hd = self._vfs.read_entry_data(he)
                        t = parse_pabgb(d, hd, os.path.basename(entry.path))
                        for r in t.rows:
                            if r.name:
                                names[r.row_hash] = r.name

            self._hash_names = names
            self._field_model.set_hash_names(names)
            current = self._row_view.currentIndex()
            if current.isValid():
                self._field_model.set_row(self._row_model.row_at(current.row()))
            self._status.setText(f"Resolved {len(names):,} names")
        except Exception as e:
            self._status.setText(f"Error: {e}")

    # ------------------------------------------------------------------
    # Row operations
    # ------------------------------------------------------------------
    def _duplicate_row(self):
        idx = self._row_view.currentIndex()
        if not idx.isValid():
            return
        row = self._row_model.row_at(idx.row())
        if not row:
            return
        new_fields = [PabgbField(f.offset, f.size, f.raw, f.kind, f.value) for f in row.fields]
        new_row = PabgbRow(
            index=len(self._table.rows), row_hash=row.row_hash,
            data_offset=0, data_size=row.data_size,
            name=(row.name + "_copy") if row.name else "",
            fields=new_fields, raw=row.raw,
        )
        self._row_model.beginInsertRows(QModelIndex(), len(self._table.rows), len(self._table.rows))
        self._table.rows.append(new_row)
        self._row_model.endInsertRows()
        self._apply_row_filter()
        self._status.setText(f"Duplicated → {len(self._table.rows)} rows")

    def _delete_row(self):
        idx = self._row_view.currentIndex()
        if not idx.isValid():
            return
        ri = self._row_model.real_index(idx.row())
        if ri < 0:
            return
        self._row_model.beginRemoveRows(QModelIndex(), idx.row(), idx.row())
        self._table.rows.pop(ri)
        self._row_model._refilter()
        self._row_model.endRemoveRows()
        self._apply_row_filter()
        self._status.setText(f"Deleted → {len(self._table.rows)} rows")

    # ------------------------------------------------------------------
    # Export / Patch
    # ------------------------------------------------------------------
    def _export_binary(self):
        from ui.dialogs.file_picker import pick_save_file
        path = pick_save_file(
            self, "Save Modified Game Data",
            default_name=self._table.file_name,
            filters="Game Data (*.pabgb);;All (*.*)",
        )
        if not path:
            return
        new_data = serialize_pabgb(self._table)
        with open(path, "wb") as f:
            f.write(new_data)
        header_path = path[:-1] + "h"
        new_header = serialize_header(self._table, self._table.is_simple)
        with open(header_path, "wb") as f:
            f.write(new_header)
        self._status.setText(f"Saved {len(new_data):,} bytes")

    def _patch_to_game(self):
        reply = QMessageBox.question(
            self, "Patch to Game",
            f"Modify {self._entry.path} in live game archives?\nBackup will be created.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from core.repack_engine import RepackEngine, ModifiedFile
            new_data = serialize_pabgb(self._table)
            self._status.setText("Patching...")
            QApplication.processEvents()
            game = os.path.dirname(os.path.dirname(self._entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            grp = os.path.basename(os.path.dirname(self._entry.paz_file))
            pamt = self._vfs.load_pamt(grp)
            mf = ModifiedFile(data=new_data, entry=self._entry, pamt_data=pamt, package_group=grp)
            result = RepackEngine(game).repack([mf], papgt_path=papgt)
            if result.success:
                QMessageBox.information(self, "Patched",
                    f"Patched {self._entry.path}\n"
                    f"Original: {len(self._table.raw_data):,} → New: {len(new_data):,} bytes")
                self._status.setText("Patch OK")
            else:
                QMessageBox.critical(self, "Failed", "\n".join(result.errors) if result.errors else "Unknown error")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # ------------------------------------------------------------------
    # Simple / Expert mode toggle
    # ------------------------------------------------------------------

    # Per-file simple view definitions: {filename: [(row_label, field_descriptions...), ...]}
    _SIMPLE_DEFS = {
        "mercenaryinfo.pabgb": {
            "title": "Mercenary & Camp Settings",
            "desc": "Controls Greymane Camp limits: comrade roster, dispatch slots, storage, provisions. Values are min/max pairs per system.",
            "col1": "Value 1 (Base/Min)",
            "col2": "Value 2 (Cap/Max)",
            "rows": [
                {"label": "Base Dispatch Config",    "notes": "Default 50/50. Base dispatch cost or capacity unit"},
                {"label": "Comrade Roster Limit",    "notes": "Default 10. Matches HUD People icon (10/10). Change to 50+ for bigger roster"},
                {"label": "Min Comrade Requirement", "notes": "Default 1. Minimum comrades needed to unlock dispatch"},
                {"label": "Dispatch Slots",          "notes": "Default 3/30. Active mission slots / max queue size"},
                {"label": "Resource Cap (Tier)",     "notes": "Default 200/200. Provision treasury cap at mid tier"},
                {"label": "Provision: Armaments?",   "notes": "Default 50/50. Likely armaments or gear provision cap"},
                {"label": "Provision: Food?",        "notes": "Default 50/50. Likely food or morale provision cap"},
                {"label": "Storage Capacity",        "notes": "Default 50/1000. Max private storage (1000 matches patch 1.02)"},
                {"label": "Provision: Timber?",      "notes": "Default 50/50. Likely timber or material provision cap"},
                {"label": "Initial Camp State",      "notes": "Default 1/2. Starting state (2 = Carl + Ross as initial comrades)"},
                {"label": "Base Tier Minimum",       "notes": "Default 1/1. Absolute minimum baseline for all systems"},
            ],
        },
    }

    def _build_simple_view(self) -> QWidget:
        """Build a user-friendly simple view for known .pabgb files."""
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QSpinBox

        widget = QWidget()
        vl = QVBoxLayout(widget)
        vl.setContentsMargins(4, 4, 4, 4)

        fname = self._table.file_name
        sdef = self._SIMPLE_DEFS.get(fname)

        if not sdef:
            lbl = QLabel(
                "<p style='color:#a6adc8; font-size:13px;'>"
                "No simple view available for this file yet.<br>"
                "Click <b>Expert</b> to use the raw table editor.</p>")
            lbl.setWordWrap(True)
            vl.addWidget(lbl)
            return widget

        # Title + description
        vl.addWidget(QLabel(f"<h3 style='color:#f38ba8;'>{sdef['title']}</h3>"))
        vl.addWidget(QLabel(f"<p style='color:#a6adc8;'>{sdef['desc']}</p>"))

        # Build editable table
        rows_def = sdef["rows"]
        num_rows = min(len(rows_def), len(self._table.rows))

        col1_name = sdef.get("col1", "Value 1")
        col2_name = sdef.get("col2", "Value 2")
        tbl = QTableWidget(num_rows, 4)
        tbl.setHorizontalHeaderLabels(["Setting", col1_name, col2_name, "Notes"])
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(True)
        tbl.setStyleSheet("QTableWidget { gridline-color: #45475a; }")

        self._simple_spinboxes = []  # Store spinbox references for saving

        for r in range(num_rows):
            rd = rows_def[r]
            row_data = self._table.rows[r] if r < len(self._table.rows) else None

            # Label
            item = QTableWidgetItem(rd["label"])
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setForeground(QColor("#f38ba8"))
            tbl.setItem(r, 0, item)

            # Value 1 (field index from 'fields' dict, key=1)
            spin1 = QSpinBox()
            spin1.setRange(0, 99999)
            if row_data and len(row_data.fields) > 1:
                v = row_data.fields[1].value if isinstance(row_data.fields[1].value, int) else 0
                spin1.setValue((v >> 16) & 0xFFFF if v > 0xFFFF else v)
            spin1.setStyleSheet("QSpinBox { background: #313244; color: #cdd6f4; border: 1px solid #585b70; padding: 2px; }")
            tbl.setCellWidget(r, 1, spin1)

            # Value 2 (field index from 'fields' dict, key=2)
            spin2 = QSpinBox()
            spin2.setRange(0, 99999)
            if row_data and len(row_data.fields) > 2:
                v = row_data.fields[2].value if isinstance(row_data.fields[2].value, int) else 0
                spin2.setValue((v >> 16) & 0xFFFF if v > 0xFFFF else v)
            spin2.setStyleSheet("QSpinBox { background: #313244; color: #cdd6f4; border: 1px solid #585b70; padding: 2px; }")
            tbl.setCellWidget(r, 2, spin2)

            self._simple_spinboxes.append((r, spin1, spin2))

            # Notes
            notes_item = QTableWidgetItem(rd["notes"])
            notes_item.setFlags(notes_item.flags() & ~Qt.ItemIsEditable)
            notes_item.setForeground(QColor("#6c7086"))
            tbl.setItem(r, 3, notes_item)

        tbl.resizeColumnsToContents()
        tbl.setColumnWidth(0, 150)
        tbl.setColumnWidth(1, 100)
        tbl.setColumnWidth(2, 100)
        vl.addWidget(tbl, 1)
        self._simple_table = tbl

        # Save button for simple mode
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        save_btn = QPushButton("Save + Patch to Game")
        save_btn.setObjectName("primary")
        save_btn.setStyleSheet(
            "QPushButton { background: #a6e3a1; color: #1e1e2e; font-weight: bold; "
            "padding: 8px 20px; border-radius: 6px; font-size: 13px; }"
            "QPushButton:hover { background: #94e2d5; }")
        save_btn.clicked.connect(self._simple_save)
        btn_row.addWidget(save_btn)
        vl.addLayout(btn_row)

        return widget

    def _simple_save(self):
        """Save simple mode edits back to the table and patch to game."""
        import struct as st
        # Write spinbox values back to table fields
        for r, spin1, spin2 in self._simple_spinboxes:
            if r >= len(self._table.rows):
                continue
            row = self._table.rows[r]
            if len(row.fields) > 1:
                old = row.fields[1].value if isinstance(row.fields[1].value, int) else 0
                low = old & 0xFFFF
                row.fields[1].value = (spin1.value() << 16) | low
                row.fields[1].raw = st.pack('<I', row.fields[1].value)
            if len(row.fields) > 2:
                old = row.fields[2].value if isinstance(row.fields[2].value, int) else 0
                low = old & 0xFFFF
                row.fields[2].value = (spin2.value() << 16) | low
                row.fields[2].raw = st.pack('<I', row.fields[2].value)

        # Now patch to game using existing method
        self._patch_to_game()

    def _toggle_mode(self, expert: bool):
        """Toggle between Simple and Expert views."""
        self._simple_widget.setVisible(not expert)
        self._expert_widget.setVisible(expert)
        self._mode_btn.setText("Expert" if not expert else "Simple")
