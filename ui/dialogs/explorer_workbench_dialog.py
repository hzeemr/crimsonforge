"""Popup dialog for the Explorer live navigator/workbench."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ui.widgets.explorer_workbench import ExplorerWorkbench


class ExplorerWorkbenchDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Explorer Navigator")
        self.resize(1460, 940)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        info = QLabel(
            "Browse live characters, items, and families from the game, then filter Explorer to the related files."
        )
        info.setWordWrap(True)
        top.addWidget(info, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        top.addWidget(close_btn)
        layout.addLayout(top)

        self._workbench = ExplorerWorkbench(config, self)
        layout.addWidget(self._workbench, 1)

        self.setModal(False)
        self.setWindowModality(Qt.NonModal)

    @property
    def workbench(self) -> ExplorerWorkbench:
        return self._workbench
