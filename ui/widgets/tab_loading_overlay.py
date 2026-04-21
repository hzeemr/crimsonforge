"""Per-tab loading overlay for background initialisation.

Problem we solve
----------------

When a lazy-initialised tab is first clicked, its ``initialize_from_game()``
method runs heavy I/O (parsing `iteminfo.pabgb`, indexing 107K+ audio
entries, cross-referencing 172K+ paloc strings across 17 languages,
etc.). The old code called this synchronously on the Qt UI thread,
which made the whole window lock up for 5-30 seconds and Windows
showed "Not Responding".

Design
------

The overlay is a full-widget covering `QWidget` with:
  * a progress bar
  * a status label
  * a retry button (hidden unless the background init fails)

It sits on top of the tab's real contents via a `QStackedLayout`.
While init is running, the stack shows the overlay. When init
succeeds, the stack flips to the real contents. When init fails,
the overlay shows the error + a Retry button.

Clients call `.start_loading(tab_label)` before the worker starts,
then `.set_progress(pct, msg)` from the worker's progress signal,
then `.finish_success()` to flip to the real contents or
`.finish_error(msg)` to show the error + retry UI.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QLabel, QProgressBar, QPushButton, QStackedLayout, QVBoxLayout,
    QWidget, QHBoxLayout, QSizePolicy,
)


class TabLoadingOverlay(QWidget):
    """A full-tab loading panel with progress bar + optional retry.

    ``retry_requested`` fires when the user clicks the Retry button
    after a failed init. Tabs wire this up to restart the background
    worker.
    """

    retry_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setAlignment(Qt.AlignCenter)

        # Title
        self._title = QLabel()
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._title)

        # Status subtitle
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color: #a6adc8;")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        layout.addSpacing(16)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedWidth(360)
        self._progress.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        row.addWidget(self._progress)
        layout.addLayout(row)

        layout.addSpacing(12)

        # Retry button (hidden unless we hit an error)
        self._retry_btn = QPushButton("Retry")
        self._retry_btn.setVisible(False)
        self._retry_btn.clicked.connect(self.retry_requested)
        self._retry_btn.setStyleSheet(
            "QPushButton { background:#89b4fa; color:#1e1e2e; "
            "padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background:#7aa2f7; }"
        )
        self._retry_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        retry_row = QHBoxLayout()
        retry_row.setAlignment(Qt.AlignCenter)
        retry_row.addWidget(self._retry_btn)
        layout.addLayout(retry_row)

        layout.addStretch()

    # ---- public API ------------------------------------------------------

    def start_loading(self, tab_label: str) -> None:
        """Enter the 'loading' state with 0% progress."""
        self._title.setText(f"Loading {tab_label}…")
        self._status.setText("Initialising in background — the app stays responsive.")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._retry_btn.setVisible(False)

    def start_indeterminate(self, tab_label: str) -> None:
        """Switch to busy-indicator mode when progress isn't known."""
        self._title.setText(f"Loading {tab_label}…")
        self._status.setText("")
        self._progress.setRange(0, 0)   # Qt interprets this as busy
        self._retry_btn.setVisible(False)

    def set_progress(self, percentage: int, message: str = "") -> None:
        """Update progress. Safe to call from a signal from a worker thread."""
        if self._progress.maximum() == 0:
            # Switch back to determinate if we have a real number
            self._progress.setRange(0, 100)
        self._progress.setValue(max(0, min(100, int(percentage))))
        if message:
            self._status.setText(message)

    def finish_error(self, title: str, detail: str) -> None:
        """Display an error + Retry button."""
        self._title.setText(title)
        self._status.setText(detail)
        self._progress.setValue(0)
        self._progress.setRange(0, 100)
        self._retry_btn.setVisible(True)


class TabInitContainer(QWidget):
    """Wrapper that hosts a tab's real widget + a loading overlay
    on top. Call :meth:`show_overlay` before starting a background
    worker and :meth:`show_content` when it's done.

    Using ``QStackedLayout`` rather than a manual show/hide flip
    means the content widget's geometry is preserved across states,
    so the tab isn't re-laid-out each time.

    ``content`` may be ``None`` at construction time — callers that
    need to defer expensive widget construction (e.g. a tab that
    imports a heavy module + builds a big splitter hierarchy in
    ``__init__``) can create an overlay-only container first, give
    Qt a tick to paint the overlay, and then install the real
    content widget via :meth:`set_content`. That is the whole point
    of the two-phase materialisation flow in
    :mod:`ui.main_window`.
    """

    # Stable layout indices — the overlay is always at the bottom
    # of the stack so late-installed content lands on top of it.
    _IDX_OVERLAY = 0
    _IDX_CONTENT = 1

    def __init__(self, content: QWidget | None = None, parent=None):
        super().__init__(parent)
        self._content: QWidget | None = None
        self.overlay = TabLoadingOverlay(self)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self.overlay)        # index 0
        self._stack.setCurrentIndex(self._IDX_OVERLAY)

        if content is not None:
            self.set_content(content)

    # ---- content management ---------------------------------------------

    def set_content(self, content: QWidget) -> None:
        """Install (or replace) the real tab widget.

        Idempotent if the exact same widget instance is installed
        again. Replacing a previously-installed content widget
        schedules the old one for deletion via ``deleteLater`` so
        signal connections on it are torn down next event-loop tick.
        """
        if content is None:
            raise ValueError("set_content(None) is not supported")
        if self._content is content:
            return

        old = self._content
        if old is not None:
            self._stack.removeWidget(old)
            old.deleteLater()

        self._content = content
        self._stack.addWidget(content)             # index 1

    def has_content(self) -> bool:
        """True once :meth:`set_content` has installed a widget."""
        return self._content is not None

    # ---- state flipping --------------------------------------------------

    def show_overlay(self, label: str = "") -> None:
        if label:
            self.overlay.start_loading(label)
        self._stack.setCurrentIndex(self._IDX_OVERLAY)

    def show_content(self) -> None:
        """Flip to the content pane. No-op if no content is installed yet."""
        if self._content is None:
            # Keep the overlay visible — flipping to a nonexistent
            # widget would leave the tab blank.
            return
        self._stack.setCurrentIndex(self._IDX_CONTENT)

    def content_widget(self) -> QWidget | None:
        return self._content
