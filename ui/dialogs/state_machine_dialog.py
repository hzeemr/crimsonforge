"""State-Machine Browser dialog.

Lets modders answer three community-reported questions:

  * Bambozu: 'Trying to find the Fly State for my enhanced flight mod'
  * sudafed: 'just skip didnt actually find the combat flag'
  * tkhquang: 'so yeah, no legit combat flag'

Layout
------
  Top bar:     search + category filter + row count
  Left pane:   token list (sorted by occurrence count), with frequency
  Right pane:  occurrences table for the selected token — every row
               that mentions the token, with the full expression
  Bottom bar:  Jump-to-row / Export CSV / Close
"""

from __future__ import annotations

import csv
import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableView, QHeaderView, QAbstractItemView, QApplication,
    QMessageBox, QSplitter, QWidget, QComboBox, QLineEdit,
    QListWidget, QListWidgetItem, QFileDialog,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer
from PySide6.QtGui import QColor, QFont

from core.state_machine import (
    StateIndex, StateToken, build_state_index, load_state_tables,
    KNOWN_ACTION_ATTRIBUTES, KNOWN_CHARACTER_KEYS,
)
from utils.logger import get_logger

logger = get_logger("ui.dialogs.state_machine")


# Catppuccin-matched colours for token kinds
_COLOR_ACTION_ATTR = QColor("#f9e2af")   # yellow — action flags
_COLOR_CHARACTER = QColor("#f38ba8")     # pink — character keys
_COLOR_STAGE = QColor("#a6e3a1")         # green — stages / quests
_COLOR_MISSION = QColor("#89b4fa")       # blue — missions
_COLOR_OTHER = QColor("#cdd6f4")         # base text


def _classify_token(token: str) -> tuple[str, QColor]:
    if token in KNOWN_ACTION_ATTRIBUTES:
        return "ActionAttribute", _COLOR_ACTION_ATTR
    if token in KNOWN_CHARACTER_KEYS:
        return "CharacterKey", _COLOR_CHARACTER
    if token.startswith("Mission_"):
        return "Mission", _COLOR_MISSION
    if token.startswith("Quest_"):
        return "Quest", _COLOR_MISSION
    if token.startswith("Stage_"):
        return "Stage", _COLOR_STAGE
    if token.startswith("Level_") or token.startswith("Level"):
        return "Level", _COLOR_STAGE
    if token.startswith("Macro") or token.endswith("State"):
        return "MacroState", _COLOR_OTHER
    if token.startswith("Animal_"):
        return "Creature", _COLOR_OTHER
    if "Gimmick" in token:
        return "Gimmick", _COLOR_OTHER
    return "Other", _COLOR_OTHER


class _OccurrenceModel(QAbstractTableModel):
    """Right-pane table: Table | Row | Row Name | Function | Expression."""

    COLS = ("Table", "Row", "Row Name", "Function", "Expression")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[StateToken] = []

    def set_rows(self, rows: list[StateToken]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def occurrence_at(self, row: int) -> StateToken | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_rows(self) -> list[StateToken]:
        return list(self._rows)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        t = self._rows[index.row()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            c = index.column()
            if c == 0:
                return t.table
            if c == 1:
                return str(t.row_index)
            if c == 2:
                return t.row_name[:80]
            if c == 3:
                return t.function or ""
            if c == 4:
                return t.expression[:200]
        if role == Qt.ToolTipRole:
            return (
                f"Table:    {t.table}\n"
                f"Row:      {t.row_index}\n"
                f"Name:     {t.row_name}\n"
                f"Function: {t.function or '-'}\n"
                f"Full expression:\n{t.expression}"
            )
        return None


class StateMachineDialog(QDialog):

    def __init__(
        self,
        index: StateIndex,
        vfs=None,
        parent=None,
    ):
        super().__init__(parent)
        self._index = index
        self._vfs = vfs

        self.setWindowTitle("State-Machine Browser")
        self.setMinimumSize(1100, 650)
        self.resize(1500, 850)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Info bar ──
        info = QHBoxLayout()
        total_tokens = len(index.tokens)
        total_occurrences = len(index.expressions)
        total_tables = len(index.table_rows)
        info.addWidget(QLabel(
            f"<b style='font-size:14px;'>State Machine</b> "
            f"<span style='color:#a6adc8;'>"
            f"{total_tokens:,} distinct tokens &nbsp;|&nbsp; "
            f"{total_occurrences:,} occurrences across "
            f"{total_tables} tables</span>"
        ))
        info.addStretch()
        layout.addLayout(info)

        # ── Filter bar ──
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Category:"))
        self._cat_combo = QComboBox()
        self._cat_combo.addItem("All", None)
        for cat in (
            "ActionAttribute", "CharacterKey",
            "Mission", "Quest", "Stage", "Level",
            "MacroState", "Creature", "Gimmick", "Other",
        ):
            self._cat_combo.addItem(cat, cat)
        self._cat_combo.currentIndexChanged.connect(self._refresh_tokens)
        filter_row.addWidget(self._cat_combo)

        filter_row.addWidget(QLabel("Search:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Type a token (Fly, BattleState, Mission_…) or "
            "free-text substring…"
        )
        self._search_input.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._refresh_tokens)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        filter_row.addWidget(self._search_input, 1)

        filter_row.addWidget(QLabel("Min occurrences:"))
        self._min_combo = QComboBox()
        for label, val in (("1", 1), ("2", 2), ("5", 5), ("10", 10), ("50", 50)):
            self._min_combo.addItem(label, val)
        self._min_combo.setCurrentIndex(0)
        self._min_combo.currentIndexChanged.connect(self._refresh_tokens)
        filter_row.addWidget(self._min_combo)

        layout.addLayout(filter_row)

        # ── Left-token + right-occurrences split ──
        splitter = QSplitter(Qt.Horizontal)

        self._token_list = QListWidget()
        self._token_list.itemSelectionChanged.connect(self._on_token_selected)
        self._token_list.setMinimumWidth(260)
        splitter.addWidget(self._token_list)

        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._occ_label = QLabel("<i>Select a token on the left to see every expression that mentions it.</i>")
        self._occ_label.setStyleSheet("color:#a6adc8; padding:4px;")
        right_layout.addWidget(self._occ_label)

        self._occ_model = _OccurrenceModel()
        self._occ_view = QTableView()
        self._occ_view.setModel(self._occ_model)
        self._occ_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._occ_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._occ_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._occ_view.setAlternatingRowColors(True)
        occ_header = self._occ_view.horizontalHeader()
        occ_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        occ_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        occ_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        occ_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        occ_header.setSectionResizeMode(4, QHeaderView.Stretch)
        self._occ_view.verticalHeader().setDefaultSectionSize(22)
        self._occ_view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._occ_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        right_layout.addWidget(self._occ_view, 1)
        splitter.addWidget(right_pane)
        splitter.setSizes([400, 900])
        layout.addWidget(splitter, 1)

        # ── Bottom bar ──
        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#a6adc8;")
        bottom.addWidget(self._status, 1)

        export_btn = QPushButton("Export Occurrences as CSV")
        export_btn.setToolTip(
            "Export every occurrence of the currently selected token "
            "(or all occurrences if no selection) to a .csv file."
        )
        export_btn.clicked.connect(self._export_csv)
        bottom.addWidget(export_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)

        layout.addLayout(bottom)

        self._refresh_tokens()

    # ---------------------------------------------------------------- filter

    def _refresh_tokens(self) -> None:
        cat = self._cat_combo.currentData()
        needle = self._search_input.text().strip().lower()
        min_occ = self._min_combo.currentData()

        # Rank tokens by frequency (global), then apply filters
        ranked = self._index.all_tokens(min_occurrences=min_occ)
        shown = []
        for token, count in ranked:
            kind, _ = _classify_token(token)
            if cat is not None and kind != cat:
                continue
            if needle and needle not in token.lower():
                continue
            shown.append((token, count, kind))

        self._token_list.clear()
        for token, count, kind in shown[:5000]:
            item = QListWidgetItem(f"{token}  ({count:,})")
            item.setData(Qt.UserRole, token)
            _, color = _classify_token(token)
            item.setForeground(color)
            item.setToolTip(
                f"{token}\nKind: {kind}\nOccurrences: {count:,}"
            )
            self._token_list.addItem(item)

        self._status.setText(
            f"Showing {len(shown):,} of {len(self._index.tokens):,} tokens"
        )

    # ---------------------------------------------------------------- right pane

    def _on_token_selected(self) -> None:
        items = self._token_list.selectedItems()
        if not items:
            self._occ_model.set_rows([])
            self._occ_label.setText(
                "<i>Select a token on the left to see every expression that mentions it.</i>"
            )
            return
        token = items[0].data(Qt.UserRole)
        kind, color = _classify_token(token)
        # Use referrers() so we catch expressions that mention the token
        # even outside a CheckActionAttribute()-style call.
        occs = self._index.referrers(token)
        self._occ_model.set_rows(occs)
        self._occ_label.setText(
            f"<b style='color:{color.name()};'>{token}</b> "
            f"<span style='color:#a6adc8;'>({kind})</span> &mdash; "
            f"<b>{len(occs):,}</b> occurrence(s)"
        )

    # ---------------------------------------------------------------- csv

    def _export_csv(self) -> None:
        rows = self._occ_model.all_rows()
        if not rows:
            rows = self._index.expressions
        if not rows:
            QMessageBox.information(self, "Nothing to export",
                                    "No occurrences to write.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export State Machine occurrences",
            "state_machine.csv", "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["token", "table", "row_index", "row_name",
                             "function", "expression"])
            for r in rows:
                writer.writerow([
                    r.token, r.table, r.row_index, r.row_name,
                    r.function or "", r.expression,
                ])
        self._status.setText(f"Exported {len(rows):,} rows to {path}")
