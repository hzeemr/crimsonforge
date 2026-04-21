"""Face-Part Browser dialog.

Shows the catalog of character face-part PAC files (and granular
sub-part names extracted from head-sub PACs) grouped by region.
Lets the modder:

  * See every available Head / EyeLeft / EyeRight / Eyebrow / Tooth /
    Nose / Lip / Beard / Hair / ... variant in one place
  * Filter by category + search substring + min-variant-count
  * Export a swap-ready CSV of ``(category, variant_id, filename,
    archive_path)`` for batch mod scripts
  * Jump straight to the Prefab editor with a chosen PAC path on the
    clipboard so the user pastes it into a `_skinnedMeshFile` slot

Design rationale
----------------

The Crimson Desert face-morph paradigm is SUBMESH SWAPPING, not
classic blend-shape interpolation. Community-character customisation
drives which discrete part variant (e.g. Eye_0002 vs Eye_0007) gets
loaded. This dialog surfaces that catalog directly rather than
pretending to sculpt non-existent blendshapes.
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
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer, Signal
from PySide6.QtGui import QColor, QFont

from core.face_parts import (
    FacePart, FacePartCatalog, build_catalog, scan_head_sub_submeshes,
)
from utils.logger import get_logger

logger = get_logger("ui.dialogs.face_parts")


# Catppuccin-matched category colours (mirrors the prefab editor tags)
_CATEGORY_COLORS = {
    "Head":     QColor("#a6e3a1"),
    "HeadSub":  QColor("#94e2d5"),
    "EyeLeft":  QColor("#f9e2af"),
    "EyeRight": QColor("#f9e2af"),
    "Eye":      QColor("#f9e2af"),
    "Eyelash":  QColor("#fab387"),
    "Eyebrow":  QColor("#fab387"),
    "Tooth":    QColor("#cdd6f4"),
    "Tongue":   QColor("#f38ba8"),
    "Nose":     QColor("#89b4fa"),
    "Lip":      QColor("#f38ba8"),
    "Mouth":    QColor("#f38ba8"),
    "Beard":    QColor("#cba6f7"),
    "Mustache": QColor("#cba6f7"),
    "Hair":     QColor("#cba6f7"),
    "Ear":      QColor("#a6adc8"),
    "Face":     QColor("#a6e3a1"),
}


class _VariantsModel(QAbstractTableModel):
    """Category / Variant ID / Filename / Archive Path."""

    COLS = ("Category", "Variant", "Filename", "Archive Path")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[FacePart] = []

    def set_rows(self, rows: list[FacePart]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def part_at(self, row: int) -> FacePart | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_rows(self) -> list[FacePart]:
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
        p = self._rows[index.row()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            c = index.column()
            if c == 0:
                return p.category
            if c == 1:
                return str(p.variant_id) if p.variant_id is not None else ""
            if c == 2:
                return p.filename
            if c == 3:
                return p.archive_path
        if role == Qt.ForegroundRole and index.column() == 0:
            return _CATEGORY_COLORS.get(p.category, QColor("#cdd6f4"))
        if role == Qt.ToolTipRole:
            return (
                f"Category:  {p.category}\n"
                f"Subtype:   {p.subtype or '-'}\n"
                f"Variant:   {p.variant_id}\n"
                f"Filename:  {p.filename}\n"
                f"Archive:   {p.archive_path}"
            )
        return None


class FacePartsDialog(QDialog):

    # Emitted when the user clicks 'Open Matching Prefab' with a PAC
    # selected — tab_explorer listens and routes through _edit_prefab().
    prefab_edit_requested = Signal(str)

    def __init__(
        self,
        catalog: FacePartCatalog,
        vfs=None,
        parent=None,
    ):
        super().__init__(parent)
        self._catalog = catalog
        self._vfs = vfs
        # Cache of granular sub-part scan results per head_sub PAC
        self._subpart_cache: dict[str, list[tuple[str, str, int | None]]] = {}

        self.setWindowTitle("Face-Part Browser")
        self.setMinimumSize(950, 600)
        self.resize(1300, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Info bar ──
        info = QHBoxLayout()
        total_parts = catalog.count()
        total_cats = len(catalog.categories())
        info.addWidget(QLabel(
            f"<b style='font-size:14px;'>Face Parts</b> "
            f"<span style='color:#a6adc8;'>"
            f"{total_parts:,} PACs across {total_cats} categories "
            f"&nbsp;|&nbsp; enumerated-variant face customisation "
            f"(not blendshapes)</span>"
        ))
        info.addStretch()
        layout.addLayout(info)

        # ── Filter bar ──
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Category:"))
        self._cat_combo = QComboBox()
        self._cat_combo.addItem("All", None)
        for cat in catalog.categories():
            self._cat_combo.addItem(
                f"{cat} ({len(catalog.parts_in(cat))})", cat,
            )
        self._cat_combo.currentIndexChanged.connect(self._refresh_variants)
        filter_row.addWidget(self._cat_combo)

        filter_row.addWidget(QLabel("Search:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Type filename / variant id / archive path…")
        self._search_input.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._refresh_variants)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        filter_row.addWidget(self._search_input, 1)

        layout.addLayout(filter_row)

        # ── Left category list + right variants table ──
        splitter = QSplitter(Qt.Horizontal)
        self._cat_list = QListWidget()
        self._cat_list.itemSelectionChanged.connect(self._on_category_selected)
        self._cat_list.setMinimumWidth(220)
        for cat in catalog.categories():
            parts = catalog.parts_in(cat)
            variants = catalog.variants_in(cat)
            it = QListWidgetItem(
                f"{cat}  ({len(parts)} PACs / {len(variants)} variants)"
            )
            it.setData(Qt.UserRole, cat)
            color = _CATEGORY_COLORS.get(cat, QColor("#cdd6f4"))
            it.setForeground(color)
            it.setToolTip(
                f"{cat}\n"
                f"PAC files: {len(parts)}\n"
                f"Unique variant IDs: {variants[:20]}"
                f"{'...' if len(variants) > 20 else ''}"
            )
            self._cat_list.addItem(it)
        splitter.addWidget(self._cat_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._header_label = QLabel(
            "<i>Select a category on the left to see every PAC variant.</i>"
        )
        self._header_label.setStyleSheet("color:#a6adc8; padding:4px;")
        right_layout.addWidget(self._header_label)

        self._model = _VariantsModel()
        self._table_view = QTableView()
        self._table_view.setModel(self._model)
        self._table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table_view.setAlternatingRowColors(True)
        h = self._table_view.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self._table_view.verticalHeader().setDefaultSectionSize(22)
        self._table_view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._table_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        right_layout.addWidget(self._table_view, 1)
        splitter.addWidget(right)
        splitter.setSizes([330, 970])
        layout.addWidget(splitter, 1)

        # ── Bottom bar ──
        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#a6adc8;")
        bottom.addWidget(self._status, 1)

        copy_btn = QPushButton("Copy Archive Path")
        copy_btn.setToolTip("Copy the selected variant's archive path to the clipboard — paste into the Prefab editor's _skinnedMeshFile slot to swap.")
        copy_btn.clicked.connect(self._copy_path)
        bottom.addWidget(copy_btn)

        self._show_subparts_btn = QPushButton("Show Sub-Parts")
        self._show_subparts_btn.setToolTip(
            "For HeadSub PAC files: extract the granular face-region "
            "submesh names bundled inside (EyeLeft_0001, Tooth_0001, "
            "Eyebrow_0004, ...)."
        )
        self._show_subparts_btn.clicked.connect(self._show_subparts)
        bottom.addWidget(self._show_subparts_btn)

        self._open_prefab_btn = QPushButton("Open Matching Prefab")
        self._open_prefab_btn.setToolTip(
            "Guess the prefab that uses this PAC and open it in the "
            "Prefab editor (derived from the PAC basename)."
        )
        self._open_prefab_btn.clicked.connect(self._open_matching_prefab)
        bottom.addWidget(self._open_prefab_btn)

        export_btn = QPushButton("Export Catalog as CSV")
        export_btn.setToolTip("Export the currently visible variants to a .csv file.")
        export_btn.clicked.connect(self._export_csv)
        bottom.addWidget(export_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)

        layout.addLayout(bottom)

        self._refresh_variants()

    # ---- filter & refresh -------------------------------------------------

    def _refresh_variants(self) -> None:
        cat = self._cat_combo.currentData()
        needle = self._search_input.text().strip().lower()

        if cat:
            parts = list(self._catalog.parts_in(cat))
        else:
            parts = list(self._catalog.parts)

        if needle:
            parts = [
                p for p in parts
                if needle in p.filename.lower()
                or (p.variant_id is not None and needle in str(p.variant_id))
                or needle in p.archive_path.lower()
            ]

        # Sort by category then variant_id
        parts.sort(key=lambda p: (p.category, p.variant_id or -1, p.filename))
        self._model.set_rows(parts)

        if cat:
            variants = sorted({p.variant_id for p in parts if p.variant_id is not None})
            self._header_label.setText(
                f"<b style='color:{_CATEGORY_COLORS.get(cat, QColor('#cdd6f4')).name()};'>"
                f"{cat}</b> &mdash; "
                f"<b>{len(parts):,}</b> PAC files &nbsp;|&nbsp; "
                f"variants: {variants[:50]}"
                f"{' …' if len(variants) > 50 else ''}"
            )
        else:
            self._header_label.setText(
                f"<b>All categories</b> &mdash; {len(parts):,} PAC files shown"
            )

        self._status.setText(
            f"Showing {len(parts):,} of {self._catalog.count():,} face parts"
        )

    # ---- ui callbacks -----------------------------------------------------

    def _on_category_selected(self) -> None:
        items = self._cat_list.selectedItems()
        if not items:
            return
        cat = items[0].data(Qt.UserRole)
        # Sync the combo (which drives the refresh)
        for i in range(self._cat_combo.count()):
            if self._cat_combo.itemData(i) == cat:
                self._cat_combo.setCurrentIndex(i)
                return

    def _copy_path(self) -> None:
        idx = self._table_view.currentIndex()
        part = self._model.part_at(idx.row())
        if part is None:
            QMessageBox.information(self, "No selection",
                                    "Select a variant row first.")
            return
        QApplication.clipboard().setText(part.archive_path)
        self._status.setText(
            f"Copied archive path: {part.archive_path}"
        )

    def _show_subparts(self) -> None:
        """Read the selected HeadSub PAC's bytes, extract the granular
        sub-part submesh names (EyeLeft_0001, Tooth_0001, ...) and
        show them in a popup."""
        idx = self._table_view.currentIndex()
        part = self._model.part_at(idx.row())
        if part is None:
            QMessageBox.information(self, "No selection",
                                    "Select a HeadSub or Head row first.")
            return
        if part.category not in ("HeadSub", "Head"):
            QMessageBox.information(
                self, "Not a bundle",
                "Granular sub-parts only live inside HeadSub or Head "
                "PAC files. Pick a row in those categories.",
            )
            return

        # Cache hit?
        cached = self._subpart_cache.get(part.filename)
        if cached is None:
            if self._vfs is None:
                QMessageBox.warning(
                    self, "Not available",
                    "This dialog was opened without a VFS context — "
                    "can't read the PAC bytes.",
                )
                return
            # Find the PAMT entry for this archive path and read bytes
            try:
                data = self._read_pac_bytes(part.archive_path)
            except Exception as e:
                QMessageBox.critical(self, "Read failed", str(e))
                return
            cached = scan_head_sub_submeshes(data)
            self._subpart_cache[part.filename] = cached

        if not cached:
            QMessageBox.information(
                self, "No sub-parts",
                f"{part.filename} did not expose any granular sub-part "
                f"names via the submesh scan.",
            )
            return

        # Pop a simple list dialog
        from PySide6.QtWidgets import QDialog as _QDialog
        d = _QDialog(self)
        d.setWindowTitle(f"Sub-parts in {part.filename}")
        d.resize(600, 400)
        lay = QVBoxLayout(d)
        lbl = QLabel(
            f"<b>{part.filename}</b> bundles <b>{len(cached)}</b> "
            f"granular face sub-parts:"
        )
        lay.addWidget(lbl)
        lst = QListWidget()
        for name, cat, vid in sorted(cached, key=lambda x: (x[1], x[2] or -1, x[0])):
            it = QListWidgetItem(
                f"[{cat:10s}] variant={vid if vid is not None else '?'}  {name}"
            )
            it.setForeground(_CATEGORY_COLORS.get(cat, QColor("#cdd6f4")))
            it.setData(Qt.UserRole, name)
            lst.addItem(it)
        lay.addWidget(lst, 1)
        btn_row = QHBoxLayout()
        copy_all = QPushButton("Copy All Names")
        copy_all.clicked.connect(lambda: QApplication.clipboard().setText(
            "\n".join(n for n, _, _ in cached)
        ))
        btn_row.addWidget(copy_all)
        btn_row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(d.accept)
        btn_row.addWidget(close)
        lay.addLayout(btn_row)
        d.exec()

    def _read_pac_bytes(self, archive_path: str) -> bytes:
        """Pull the PAC bytes out of the VFS for ``archive_path``."""
        needle = archive_path.lower()
        for grp in self._vfs.list_package_groups():
            pamt = self._vfs.get_pamt(grp) or self._vfs.load_pamt(grp)
            for e in pamt.file_entries:
                if e.path.lower() == needle:
                    return self._vfs.read_entry_data(e)
        raise FileNotFoundError(
            f"{archive_path} not found in any loaded PAMT"
        )

    def _open_matching_prefab(self) -> None:
        """Request that the Explorer open a prefab that REFERENCES this
        PAC via its full archive path. The Explorer builds a reverse-
        reference index over every prefab in the VFS and uses that to
        find the real user — prefabs rarely share a basename with the
        PAC they load (e.g. cd_phm_00_cloak_00_0208_t.prefab actually
        references cd_phm_00_cloak_00_0054_01.pac, a completely
        different variant number)."""
        idx = self._table_view.currentIndex()
        part = self._model.part_at(idx.row())
        if part is None:
            QMessageBox.information(self, "No selection",
                                    "Select a variant row first.")
            return
        if not part.filename.lower().endswith(".pac"):
            return
        self.prefab_edit_requested.emit(part.archive_path)
        self._status.setText(
            f"Looking up prefab(s) that reference {part.archive_path}…"
        )

    def _export_csv(self) -> None:
        rows = self._model.all_rows()
        if not rows:
            QMessageBox.information(self, "Nothing to export", "No rows visible.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Face-Part catalog", "face_parts.csv",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["category", "variant_id", "filename", "archive_path"])
            for r in rows:
                w.writerow([r.category, r.variant_id or "", r.filename, r.archive_path])
        self._status.setText(f"Exported {len(rows):,} rows to {path}")
