"""Viewer dialog for ``.pabc`` / ``.pabv`` morph-target files.

Shows the parsed header, payload statistics, and the per-row fp32
delta grid. Designed for community RE work: you can spot which
rows are "all zero" (unused morph targets), which ones have
extreme values (active customisation deltas), and export the
grid to CSV / JSON for offline analysis.

Layout
------
  Header panel (top)        — magic / version / flags / count / sizes
  Stats panel (top right)   — n_floats / row hint / in-range % /
                              min / max / mean
  Float grid (centre)       — count rows × row-hint columns of fp32
  Action row (bottom)       — Save as CSV / Save as JSON / Close

Why CSV export
--------------
The point of this dialog is to give modders + the community a
window into the morph data so they can correlate rows with
character-creator sliders. The fastest workflow is: open dialog,
spot a row of interest, export to CSV, diff against another
character's CSV in their editor of choice. We could embed a diff
view but CSV is the universal interchange that everyone has
tooling for already.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from core.pabc_parser import (
    PabcFile, PabcFormatError, parse_pabc, serialize_pabc,
)
from utils.logger import get_logger

logger = get_logger("ui.dialogs.pabc_viewer")


# ── Float-grid model ─────────────────────────────────────────────

class _FloatGridModel(QAbstractTableModel):
    """Surface the fp32 payload as a 2-D row × column table.

    Number of columns is taken from :attr:`PabcFile.row_floats_hint`
    so a v4 file shows a clean 49-column grid, v5 shows 98, etc.
    Empty-payload stubs (count > 0 but no floats) collapse to a
    zero-row table.
    """

    def __init__(self, parsed: PabcFile):
        super().__init__()
        self._parsed = parsed
        # Columns = row-floats-hint, falling back to n_floats for
        # files where the hint is 0 (empty stubs / unrecognised
        # version).
        cols = parsed.row_floats_hint or 1
        rows = parsed.n_floats // cols if cols else 0
        # Stash the trailing residual floats (e.g. odd extra
        # values that don't fit into cols × rows) for display in a
        # sidebar — we don't drop them silently.
        self._cols = cols
        self._rows = rows
        self._tail = parsed.floats[rows * cols :] if cols else parsed.floats[:]

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else self._cols

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self._column_label(section)
            if role == Qt.ToolTipRole:
                return self._column_tooltip(section)
            return None
        if orientation == Qt.Vertical:
            if role == Qt.DisplayRole:
                return f"row {section:03d}"
            if role == Qt.ToolTipRole:
                return (
                    f"Row {section:03d} — likely a per-bone morph "
                    f"record. The PABC viewer doesn't yet know which "
                    f"physical bone in the head PAB this maps to."
                )
        return None

    def _column_label(self, c: int) -> str:
        """Return a hint label for column ``c`` based on the
        v4 ``49 = 1 + 12 × 4`` layout hypothesis (head .pabc).

        For non-v4 / non-49-column files we fall back to the plain
        ``fNN`` label so users aren't misled.
        """
        if self._cols != 49:
            return f"f{c:02d}"
        if c == 0:
            return "f00 ID?"
        # cols 1..48 split into 12 vec4 transforms in groups of 12.
        # We label by transform group (A/B/C/D) + axis (x/y/z/w).
        rel = c - 1   # 0..47
        group = rel // 12          # 0..3 → A, B, C, D
        axis_in_group = rel % 12   # 0..11 → 3 vec4s
        vec_idx = axis_in_group // 4    # 0..2
        comp = axis_in_group % 4   # 0..3 → x, y, z, w
        comp_label = "xyzw"[comp]
        group_label = "ABCD"[group]
        slot_label = "TRS"[vec_idx]   # T=translate, R=rotation, S=scale (guess)
        return f"f{c:02d} {group_label}.{slot_label}.{comp_label}"

    def _column_tooltip(self, c: int) -> str:
        if self._cols != 49:
            return f"Column {c} (raw fp32, layout unknown for this file)"
        if c == 0:
            return (
                f"Column {c} — bone identifier (likely a u32 re-interpreted "
                "as float; the byte pattern matches a hash / index, not a "
                "real number). Magnitudes ≥ 1e30 are the giveaway."
            )
        rel = c - 1
        group = "ABCD"[rel // 12]
        slot = ["Translate", "Rotation", "Scale"][rel % 12 // 4]
        comp = "xyzw"[rel % 4]
        return (
            f"Column {c} — hypothesised slot: transform {group}, "
            f"{slot}.{comp}. The 49-float row almost certainly packs "
            "4 transforms × 3 vec4 + 1 ID; which transform is "
            "rest / min / max / bind is not yet confirmed."
        )

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        idx = index.row() * self._cols + index.column()
        if idx >= len(self._parsed.floats):
            return None
        v = self._parsed.floats[idx]

        if role == Qt.DisplayRole:
            # Compact fixed-precision so the grid stays readable
            # even with 49 columns. Use scientific notation for
            # extreme values so they stay visible in narrow cells.
            if v == 0.0:
                return "0"
            if abs(v) >= 1e6 or (abs(v) < 1e-4 and v != 0):
                return f"{v:.2e}"
            return f"{v:.4f}"

        if role == Qt.BackgroundRole:
            # Catppuccin-mocha tint — green for positive, red for
            # negative, intensity scaled by magnitude up to ±1.0.
            # Zero gets no background so unused entries fade out.
            if v == 0.0:
                return None
            mag = min(abs(v), 1.0)
            alpha = int(20 + 70 * mag)
            if v > 0:
                return QColor(166, 227, 161, alpha)   # green
            return QColor(243, 139, 168, alpha)        # red

        if role == Qt.ToolTipRole:
            return f"row={index.row()}  col={index.column()}  value={v!r}"

        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.FontRole:
            return _mono_font()

        return None

    @property
    def tail(self) -> list[float]:
        """Trailing floats that didn't fit into rows × cols."""
        return self._tail

    @property
    def grid_dims(self) -> tuple[int, int]:
        return (self._rows, self._cols)


def _mono_font() -> QFont:
    f = QFont("Consolas")
    if not f.exactMatch():
        f = QFont("Courier New")
    f.setPointSize(9)
    return f


# ── Dialog ────────────────────────────────────────────────────────

class PabcViewerDialog(QDialog):
    """Read-only viewer for a parsed :class:`PabcFile`.

    The dialog is purely informational + diagnostic. Editing /
    round-tripping back to the game archives is a future feature
    (it needs the per-byte slider mapping in :file:`.paccd` before
    it's safe to expose).
    """

    def __init__(
        self,
        parsed: PabcFile,
        source_label: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._parsed = parsed
        self._source_label = source_label
        self.setWindowTitle(
            f"Morph Data Viewer — {source_label or 'PABC file'}"
        )
        self.setModal(False)
        self.resize(1200, 760)
        self._setup_ui()

    # ── construct ─────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # Build the grid model first — the stats panel reads
        # ``grid_dims`` off it.
        self._model = _FloatGridModel(self._parsed)

        # Top: header + stats side-by-side.
        top_row = QHBoxLayout()
        top_row.addWidget(self._build_header_panel(), 1)
        top_row.addWidget(self._build_stats_panel(), 1)
        root.addLayout(top_row)

        # Layout-hypothesis banner — honest about what's known and
        # what's still guesswork. Without this users would assume
        # the column labels (A.T.x, B.R.w, etc.) are authoritative.
        hyp = self._layout_hypothesis_text()
        if hyp:
            banner = QLabel(hyp)
            banner.setWordWrap(True)
            banner.setTextFormat(Qt.RichText)
            banner.setStyleSheet(
                "color: #cdd6f4; padding: 6px 10px;"
                "background: #1e1e2e; border: 1px solid #45475a;"
                "border-radius: 6px; font-size: 11px;"
            )
            root.addWidget(banner)

        # Float grid (the main surface).
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        h = self._table.horizontalHeader()
        h.setDefaultSectionSize(72)
        h.setSectionResizeMode(QHeaderView.Interactive)
        v = self._table.verticalHeader()
        v.setDefaultSectionSize(20)
        root.addWidget(self._table, 1)

        # If there's a tail of unaligned floats, show them in a
        # small banner at the bottom — most files have none.
        tail = self._model.tail
        if tail:
            banner = QLabel(
                f"Tail floats (didn't fit grid): "
                f"{', '.join(f'{v:.4f}' for v in tail[:8])}"
                + ("…" if len(tail) > 8 else "")
            )
            banner.setStyleSheet(
                "color: #f9e2af; padding: 4px 8px;"
                "background: #1e1e2e; border-radius: 4px;"
                "font-size: 11px;"
            )
            root.addWidget(banner)

        # If trailer bytes exist, show those too.
        if self._parsed.trailer:
            trailer_label = QLabel(
                f"Trailer bytes ({len(self._parsed.trailer)}): "
                f"{self._parsed.trailer.hex()}"
            )
            trailer_label.setStyleSheet(
                "color: #fab387; padding: 4px 8px;"
                "background: #1e1e2e; border-radius: 4px;"
                "font-size: 11px;"
            )
            root.addWidget(trailer_label)

        # Action row.
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        csv_btn = QPushButton("Export Grid to CSV…")
        csv_btn.setObjectName("primary")
        csv_btn.setToolTip(
            "Export the float grid to a CSV file for diff / "
            "spreadsheet analysis."
        )
        csv_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(csv_btn)

        json_btn = QPushButton("Export All to JSON…")
        json_btn.setToolTip(
            "Export the parsed file (header + floats + trailer + "
            "stats) to a JSON file."
        )
        json_btn.clicked.connect(self._export_json)
        btn_row.addWidget(json_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _build_header_panel(self) -> QWidget:
        h = self._parsed.header
        rows = (
            ("Source", self._source_label or "(in-memory bytes)"),
            ("Magic", h.magic.decode("ascii", errors="replace")),
            ("Version", str(h.version)),
            ("Flags", h.flags.hex()),
            ("Signature", h.signature_run.hex()),
            ("Morph rows (count)", f"{h.count:,}"),
            ("File size", f"{self._parsed.raw_size:,} bytes"),
            ("Payload bytes", f"{self._parsed.payload_byte_size:,}"),
        )
        return self._labelled_panel("Header", rows)

    def _build_stats_panel(self) -> QWidget:
        floats = self._parsed.floats
        # Stat helpers tolerate inf / nan: we filter them out for
        # the moment-based stats (mean / stdev) but still surface
        # the raw min / max so users see the extreme outliers.
        # Without the filter, statistics.stdev() on a list with a
        # single inf raises ValueError and the dialog won't open.
        import math
        finite = [f for f in floats if math.isfinite(f)]
        if floats:
            in_range = [f for f in floats if -2.0 < f < 2.0]
            mean = statistics.mean(finite) if finite else float("nan")
            stdev = statistics.stdev(finite) if len(finite) > 1 else 0.0
            mn = min(floats)
            mx = max(floats)
        else:
            in_range = []
            mean = stdev = mn = mx = 0.0
        rows_dim, cols_dim = self._model.grid_dims
        rows = (
            ("Total fp32", f"{self._parsed.n_floats:,}"),
            ("Floats in (-2, 2)",
             f"{len(in_range):,} ({100 * self._parsed.in_range_ratio:.1f}%)"),
            ("Grid", f"{rows_dim} rows × {cols_dim} cols"),
            ("Row hint", str(self._parsed.row_floats_hint)),
            ("Min / Max", f"{mn:.4g} / {mx:.4g}"),
            ("Mean / Stdev", f"{mean:.4g} / {stdev:.4g}"),
            ("Trailer", f"{len(self._parsed.trailer)} bytes"),
        )
        return self._labelled_panel("Statistics", rows)

    def _layout_hypothesis_text(self) -> str:
        """Honest 'what we know vs don't' summary for the layout."""
        cols = self._model._cols
        if cols == 49:
            return (
                "<b>Layout (hypothesised, not confirmed):</b> "
                "each row encodes one bone's morph record as "
                "<code>1 ID float + 12 vec4 components</code>, "
                "split into 4 transform groups (A / B / C / D), "
                "each with 3 vec4 slots — translate, rotation, "
                "scale. Column tooltips show the per-cell guess. "
                "<span style='color:#fab387;'>What we don't know "
                "yet:</span> which transform is rest / min / max / "
                "bind, which row maps to which physical bone, and "
                "how each bone wires to the 170 sliders in the "
                "matching <code>.paccd</code>. The way to find "
                "out is bisect-edit (clone a .paccd, flip a byte, "
                "run the game, screenshot, diff)."
            )
        if cols == 98:
            return (
                "<b>Layout:</b> v5 file = 98 floats per row, almost "
                "certainly two LOD copies of the v4 49-float-per-row "
                "record stacked together (LOD 0 then LOD 1). "
                "Column tooltips are off for v5 until the LOD split "
                "is confirmed."
            )
        return (
            "<b>Layout:</b> non-standard row width "
            f"({cols} cols). This file's row layout hasn't been "
            "characterised yet — values shown raw."
        )

    @staticmethod
    def _labelled_panel(title: str, rows: tuple) -> QWidget:
        frame = QFrame()
        frame.setObjectName("pabcInfoFrame")
        frame.setStyleSheet(
            "QFrame#pabcInfoFrame {"
            "  background-color: #181825;"
            "  border: 1px solid #313244;"
            "  border-radius: 6px;"
            "  padding: 6px;"
            "}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            "color: #cba6f7; font-weight: 600; font-size: 12px;"
        )
        layout.addWidget(title_lbl)
        for k, v in rows:
            row = QLabel(
                f"<span style='color:#a6adc8;'>{k}:</span> "
                f"<span style='color:#cdd6f4;'>{v}</span>"
            )
            row.setStyleSheet("font-size: 11px;")
            row.setTextFormat(Qt.RichText)
            layout.addWidget(row)
        layout.addStretch()
        return frame

    # ── exports ──────────────────────────────────────

    def _export_csv(self) -> None:
        default = self._suggest_filename(".csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export grid as CSV",
            default, "CSV files (*.csv)",
        )
        if not path:
            return
        try:
            rows, cols = self._model.grid_dims
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["row"] + [f"f{i:02d}" for i in range(cols)])
                for r in range(rows):
                    base = r * cols
                    w.writerow([r] + [
                        f"{v:.6g}"
                        for v in self._parsed.floats[base : base + cols]
                    ])
            QMessageBox.information(
                self, "Export Complete",
                f"Wrote {rows:,} rows × {cols} cols to:\n{path}",
            )
        except Exception as exc:
            logger.exception("CSV export failed: %s", exc)
            QMessageBox.warning(self, "Export Error", str(exc))

    def _export_json(self) -> None:
        default = self._suggest_filename(".json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export full parse as JSON",
            default, "JSON files (*.json)",
        )
        if not path:
            return
        try:
            doc = {
                "source": self._source_label,
                "header": {
                    "magic": self._parsed.header.magic.decode("ascii", "replace"),
                    "version": self._parsed.header.version,
                    "flags_hex": self._parsed.header.flags.hex(),
                    "signature_hex": self._parsed.header.signature_run.hex(),
                    "count": self._parsed.header.count,
                },
                "raw_size": self._parsed.raw_size,
                "n_floats": self._parsed.n_floats,
                "row_floats_hint": self._parsed.row_floats_hint,
                "trailer_hex": self._parsed.trailer.hex(),
                "floats": list(self._parsed.floats),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2)
            QMessageBox.information(
                self, "Export Complete",
                f"Wrote {self._parsed.n_floats:,} floats to:\n{path}",
            )
        except Exception as exc:
            logger.exception("JSON export failed: %s", exc)
            QMessageBox.warning(self, "Export Error", str(exc))

    def _suggest_filename(self, ext: str) -> str:
        stem = "pabc_export"
        if self._source_label:
            base = os.path.basename(self._source_label)
            stem = os.path.splitext(base)[0] or stem
        return stem + ext


# ── Convenience entry point ──────────────────────────────────────

def open_pabc_viewer(
    data: bytes,
    source_label: str = "",
    parent=None,
) -> Optional[PabcViewerDialog]:
    """Parse ``data`` and pop a viewer dialog.

    Returns the dialog (already shown) on success, ``None`` if
    the bytes don't parse — in which case a QMessageBox is shown
    to the user with the parser's error.

    This is the single call site explorer / preview should hit so
    error reporting + dialog construction is uniform.
    """
    try:
        parsed = parse_pabc(data)
    except PabcFormatError as exc:
        QMessageBox.warning(
            parent, "Not a PABC file",
            f"Could not parse {source_label or 'this file'} as a "
            f".pabc / .pabv morph-target file:\n\n{exc}",
        )
        return None
    dlg = PabcViewerDialog(parsed, source_label=source_label, parent=parent)
    dlg.show()
    return dlg
