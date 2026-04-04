"""Translation string table widget using QTableView + QAbstractTableModel.

Enterprise features:
- Virtual scrolling (100K+ entries, only visible rows rendered)
- Column sorting (click header to sort asc/desc)
- Double-click to open entry editor dialog
- Copy/paste support (Ctrl+C copies selected translation)
- Status auto-set to Translated after AI/manual edit
- Filter by status + search text (debounced 300ms)
- Bulk approve/review selected with progress
- Export selected/all to TXT
"""

import fnmatch
import re
import shlex
from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QHeaderView,
    QAbstractItemView, QLabel, QLineEdit, QComboBox, QPushButton,
    QStyledItemDelegate, QApplication, QMenu,
)
from PySide6.QtCore import (
    Qt, Signal, QAbstractTableModel, QModelIndex, QTimer, QSortFilterProxyModel,
)
from PySide6.QtGui import QColor, QBrush, QKeySequence, QShortcut, QAction

from translation.translation_state import TranslationEntry, StringStatus
from ui.widgets.search_history_line_edit import SearchHistoryLineEdit


STATUS_COLORS = {
    StringStatus.PENDING: QColor("#f9e2af"),
    StringStatus.TRANSLATED: QColor("#89b4fa"),
    StringStatus.REVIEWED: QColor("#a6e3a1"),
    StringStatus.APPROVED: QColor("#94e2d5"),
}

STATUS_LABELS = {
    StringStatus.PENDING: "Pending",
    StringStatus.TRANSLATED: "Translated",
    StringStatus.REVIEWED: "Reviewed",
    StringStatus.APPROVED: "Approved",
}

STATUS_FROM_LABEL = {v: k for k, v in STATUS_LABELS.items()}

COL_NUM = 0
COL_ORIGINAL = 1
COL_TRANSLATION = 2
COL_STATUS = 3
COLUMN_COUNT = 4
COLUMN_HEADERS = ["#", "Original", "Translation", "Status"]
USAGE_FILTER_ALL = "All"
USAGE_FILTER_UNCATEGORIZED = "Uncategorized"
VERSION_FILTER_ALL = "__all__"
CHANGE_FILTER_ALL = "All Changes"
CHANGE_FILTER_ADDED = "Added"
CHANGE_FILTER_CHANGED = "Changed"
CHANGE_FILTER_REMOVED = "Removed"
CHANGE_FILTER_BASELINE = "Baseline"
CHANGE_FILTER_OPTIONS = (
    CHANGE_FILTER_ALL,
    CHANGE_FILTER_ADDED,
    CHANGE_FILTER_CHANGED,
    CHANGE_FILTER_REMOVED,
    CHANGE_FILTER_BASELINE,
)
SEARCHABLE_FIELDS = ("key", "original", "translation", "usage", "status", "notes")
SEARCH_FIELD_ALIASES = {
    "key": "key",
    "k": "key",
    "orig": "original",
    "original": "original",
    "source": "original",
    "trans": "translation",
    "translation": "translation",
    "target": "translation",
    "usage": "usage",
    "tag": "usage",
    "status": "status",
    "note": "notes",
    "notes": "notes",
}
SEARCH_FIELD_WEIGHTS = {
    "original": 120,
    "translation": 115,
    "key": 110,
    "usage": 70,
    "status": 60,
    "notes": 50,
}

# Brace-token regex for {…} detection
_BRACE_TOKEN_RE = re.compile(r"\{[^}]+\}")


@dataclass
class _SearchQuery:
    raw: str = ""
    free_terms: list[str] = field(default_factory=list)
    field_terms: dict[str, list[str]] = field(default_factory=dict)
    # Boolean flags set by special operators
    locked_filter: int = 0        # 0=ignore, 1=locked only, -1=unlocked only
    empty_filter: int = 0         # 0=ignore, 1=empty translation, -1=has translation
    has_braces_filter: int = 0    # 0=ignore, 1=has {…} tokens, -1=no {…} tokens


@dataclass
class _SearchDocument:
    signature: tuple
    key: str
    original: str
    translation: str
    usage: str
    status: str
    notes: str = ""
    locked: bool = False
    has_braces: bool = False
    empty_translation: bool = True


def _normalize_search_text(value: str) -> str:
    return " ".join(value.lower().split())


_YES_VALUES = {"yes", "true", "1", "on"}
_NO_VALUES = {"no", "false", "0", "off"}


def _parse_search_query(raw_text: str) -> _SearchQuery:
    """Parse search text into a structured query.

    Supports:
      - Free text:          ``day``  ``hello world``
      - Quoted phrases:     ``"quest day"``
      - Field filters:      ``key:quest_``  ``original:sword``  ``status:pending``
      - Wildcards (glob):   ``key:quest*``  ``{*}``  ``*dragon*``
      - Boolean operators:  ``locked:yes``  ``empty:yes``  ``has:braces``
      - Brace shorthand:    ``{*``  ``{*}``  matches entries containing {…} tokens
    """
    query = _SearchQuery(raw=raw_text.strip())
    if not query.raw:
        return query

    try:
        tokens = shlex.split(query.raw)
    except ValueError:
        tokens = query.raw.split()

    for token in tokens:
        normalized = _normalize_search_text(token)
        if not normalized:
            continue

        # ── Special boolean operators ──
        field_name, sep, value = normalized.partition(":")
        if sep:
            # locked:yes / locked:no
            if field_name == "locked":
                query.locked_filter = 1 if value in _YES_VALUES else -1
                continue
            # empty:yes / empty:no
            if field_name == "empty":
                query.empty_filter = 1 if value in _YES_VALUES else -1
                continue
            # has:braces / has:{} / has:tokens
            if field_name == "has" and value in ("braces", "{}", "tokens", "brace"):
                query.has_braces_filter = 1
                continue

        # ── Brace shorthand: {* or {*} → filter entries with {…} tokens ──
        if normalized in ("{*", "{*}"):
            query.has_braces_filter = 1
            continue

        # ── Field-specific terms ──
        canonical_field = SEARCH_FIELD_ALIASES.get(field_name) if sep else None
        if sep and canonical_field and value:
            query.field_terms.setdefault(canonical_field, []).append(value)
        else:
            query.free_terms.append(normalized)

    return query


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _find_whole_term(text: str, term: str) -> int:
    start = 0
    term_len = len(term)
    while True:
        pos = text.find(term, start)
        if pos == -1:
            return -1

        before_ok = pos == 0 or not _is_word_char(text[pos - 1])
        after_index = pos + term_len
        after_ok = after_index == len(text) or not _is_word_char(text[after_index])
        if before_ok and after_ok:
            return pos
        start = pos + 1


def _score_text_match(text: str, query_text: str, field_weight: int) -> int:
    if not text or not query_text:
        return -1

    # Glob/wildcard mode when query contains * or ?
    if "*" in query_text or "?" in query_text:
        if fnmatch.fnmatch(text, query_text):
            return (90_000 * field_weight) - len(text)
        # Also try matching any word in the text
        for word in text.split():
            if fnmatch.fnmatch(word, query_text):
                return (60_000 * field_weight) - len(text)
        return -1

    if text == query_text:
        return (100_000 * field_weight) - len(text)

    whole_term_pos = _find_whole_term(text, query_text)
    if whole_term_pos != -1:
        return (70_000 * field_weight) - min(whole_term_pos, 256) - len(text)

    if text.startswith(query_text):
        return (50_000 * field_weight) - len(text)

    contains_pos = text.find(query_text)
    if contains_pos != -1:
        return (25_000 * field_weight) - min(contains_pos, 512) - min(len(text), 512)

    return -1


class _TranslationModel(QAbstractTableModel):
    """Model backing the translation table view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_entries: list[TranslationEntry] = []
        self._filtered: list[TranslationEntry] = []
        self._status_filter = "All"
        self._category_filter = USAGE_FILTER_ALL
        self._version_filter = VERSION_FILTER_ALL
        self._change_filter = CHANGE_FILTER_ALL
        self._search_text = ""
        self._search_query = _SearchQuery()
        self._search_doc_cache: dict[int, _SearchDocument] = {}

    def set_entries(self, entries: list[TranslationEntry]):
        self.beginResetModel()
        self._all_entries = entries
        self._search_doc_cache.clear()
        self._refilter()
        self.endResetModel()

    def set_filter(
        self,
        status: str,
        search: str,
        category: str,
        version: str = VERSION_FILTER_ALL,
        change: str = CHANGE_FILTER_ALL,
    ):
        self.beginResetModel()
        self._status_filter = status
        self._category_filter = category or USAGE_FILTER_ALL
        self._version_filter = version or VERSION_FILTER_ALL
        self._change_filter = change or CHANGE_FILTER_ALL
        self._search_text = search.strip()
        self._search_query = _parse_search_query(self._search_text)
        self._refilter()
        self.endResetModel()

    def _refilter(self):
        entries = self._all_entries
        if self._status_filter == "Locked":
            entries = [e for e in entries if getattr(e, "locked", False)]
        elif self._status_filter != "All":
            target = STATUS_FROM_LABEL.get(self._status_filter)
            if target is not None:
                entries = [e for e in entries if e.status == target]
        if self._category_filter != USAGE_FILTER_ALL:
            target_category = self._category_filter
            entries = [
                e for e in entries
                if target_category in (e.usage_tags or [USAGE_FILTER_UNCATEGORIZED])
            ]
        if self._version_filter != VERSION_FILTER_ALL or self._change_filter != CHANGE_FILTER_ALL:
            entries = [e for e in entries if self._entry_matches_version_filters(e)]
        if self._search_query.raw:
            scored_entries = []
            for entry in entries:
                score = self._score_entry(entry)
                if score >= 0:
                    scored_entries.append((score, entry.index, entry))
            scored_entries.sort(key=lambda item: (-item[0], item[1]))
            entries = [entry for _, _, entry in scored_entries]
        self._filtered = entries

    def _entry_search_document(self, entry: TranslationEntry) -> _SearchDocument:
        usage_tags = tuple(entry.usage_tags or [USAGE_FILTER_UNCATEGORIZED])
        locked = getattr(entry, "locked", False)
        notes = getattr(entry, "notes", "")
        signature = (
            entry.key,
            entry.original_text,
            entry.translated_text,
            entry.status.value,
            usage_tags,
            locked,
            notes,
        )
        cached = self._search_doc_cache.get(entry.index)
        if cached and cached.signature == signature:
            return cached

        orig_text = entry.original_text or ""
        doc = _SearchDocument(
            signature=signature,
            key=_normalize_search_text(entry.key),
            original=_normalize_search_text(orig_text),
            translation=_normalize_search_text(entry.translated_text),
            usage=_normalize_search_text(" ".join(usage_tags)),
            status=_normalize_search_text(STATUS_LABELS.get(entry.status, "Pending")),
            notes=_normalize_search_text(notes),
            locked=locked,
            has_braces=bool(_BRACE_TOKEN_RE.search(orig_text)),
            empty_translation=not entry.translated_text.strip(),
        )
        self._search_doc_cache[entry.index] = doc
        return doc

    def _entry_matches_version_filters(self, entry: TranslationEntry) -> bool:
        history = list(getattr(entry, "game_event_history", []) or [])
        if not history:
            return False

        filtered = history
        if self._version_filter != VERSION_FILTER_ALL:
            filtered = [event for event in filtered if event.get("version") == self._version_filter]
            if not filtered:
                return False

        if self._change_filter == CHANGE_FILTER_ALL:
            return bool(filtered)

        wanted_kind = {
            CHANGE_FILTER_ADDED: "added",
            CHANGE_FILTER_CHANGED: "changed",
            CHANGE_FILTER_REMOVED: "removed",
            CHANGE_FILTER_BASELINE: "baseline",
        }.get(self._change_filter, "")
        if not wanted_kind:
            return bool(filtered)
        return any(event.get("kind") == wanted_kind for event in filtered)

    def _score_entry(self, entry: TranslationEntry) -> int:
        query = self._search_query
        doc = self._entry_search_document(entry)

        # ── Boolean filters (fast reject before scoring) ──
        if query.locked_filter == 1 and not doc.locked:
            return -1
        if query.locked_filter == -1 and doc.locked:
            return -1
        if query.empty_filter == 1 and not doc.empty_translation:
            return -1
        if query.empty_filter == -1 and doc.empty_translation:
            return -1
        if query.has_braces_filter == 1 and not doc.has_braces:
            return -1
        if query.has_braces_filter == -1 and doc.has_braces:
            return -1

        total_score = 0

        # ── Field-specific terms (all must match) ──
        for field_name, values in query.field_terms.items():
            text = getattr(doc, field_name, "")
            field_weight = SEARCH_FIELD_WEIGHTS.get(field_name, 50)
            for value in values:
                match_score = _score_text_match(text, value, field_weight)
                if match_score < 0:
                    return -1
                total_score += match_score

        # ── Free terms (best field match wins per term) ──
        for term in query.free_terms:
            best_match = -1
            for field_name in SEARCHABLE_FIELDS:
                match_score = _score_text_match(
                    getattr(doc, field_name, ""),
                    term,
                    SEARCH_FIELD_WEIGHTS.get(field_name, 50),
                )
                if match_score > best_match:
                    best_match = match_score
            if best_match < 0:
                return -1
            total_score += best_match

        return total_score

    def entry_at(self, row: int) -> TranslationEntry:
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return COLUMN_COUNT

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMN_HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row >= len(self._filtered):
            return None
        entry = self._filtered[row]

        if role == Qt.DisplayRole:
            if col == COL_NUM:
                return str(entry.index + 1)
            elif col == COL_ORIGINAL:
                return entry.original_text[:500]
            elif col == COL_TRANSLATION:
                return entry.translated_text
            elif col == COL_STATUS:
                return STATUS_LABELS.get(entry.status, "Pending")

        elif role == Qt.EditRole:
            if col == COL_TRANSLATION:
                return entry.translated_text
            elif col == COL_STATUS:
                return STATUS_LABELS.get(entry.status, "Pending")

        elif role == Qt.ToolTipRole:
            if col == COL_ORIGINAL:
                return entry.original_text
            elif col == COL_TRANSLATION:
                return entry.translated_text or "(empty)"
            elif col == COL_NUM:
                usage = ", ".join(entry.usage_tags or [USAGE_FILTER_UNCATEGORIZED])
                return f"Key: {entry.key}\nUsage: {usage}"

        elif role == Qt.ForegroundRole:
            if col == COL_STATUS:
                color = STATUS_COLORS.get(entry.status)
                if color:
                    return QBrush(color)

        elif role == Qt.UserRole:
            return entry

        elif role == Qt.UserRole + 1:
            return entry.index

        return None

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.column() in (COL_TRANSLATION, COL_STATUS):
            row = index.row()
            if row < len(self._filtered) and self._filtered[row].locked:
                return base  # no editing for locked entries
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole:
            return False
        row = index.row()
        col = index.column()
        if row >= len(self._filtered):
            return False
        entry = self._filtered[row]
        if entry.locked:
            return False

        if col == COL_TRANSLATION:
            text = str(value)
            if text != entry.translated_text:
                entry.edit_translation(text)
                # Auto-promote Pending -> Translated when text is entered
                if text and entry.status == StringStatus.PENDING:
                    entry.status = StringStatus.TRANSLATED
                # Auto-revert to Pending when text is cleared
                if not text and entry.status != StringStatus.PENDING:
                    entry.revert_to_pending()
                self.dataChanged.emit(index, index, [Qt.DisplayRole])
                status_idx = self.index(row, COL_STATUS)
                self.dataChanged.emit(status_idx, status_idx, [Qt.DisplayRole, Qt.ForegroundRole])
            return True
        elif col == COL_STATUS:
            text = str(value)
            if text == "Pending":
                entry.revert_to_pending()
            elif text == "Translated":
                entry.status = StringStatus.TRANSLATED
            elif text == "Reviewed":
                entry.set_reviewed()
            elif text == "Approved":
                entry.set_approved()
            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.ForegroundRole])
            return True
        return False

    def sort(self, column, order=Qt.AscendingOrder):
        self.beginResetModel()
        reverse = (order == Qt.DescendingOrder)
        if column == COL_NUM:
            self._filtered.sort(key=lambda e: e.index, reverse=reverse)
        elif column == COL_ORIGINAL:
            self._filtered.sort(key=lambda e: e.original_text.lower(), reverse=reverse)
        elif column == COL_TRANSLATION:
            self._filtered.sort(key=lambda e: e.translated_text.lower(), reverse=reverse)
        elif column == COL_STATUS:
            order_map = {
                StringStatus.PENDING: 0, StringStatus.TRANSLATED: 1,
                StringStatus.REVIEWED: 2, StringStatus.APPROVED: 3,
            }
            self._filtered.sort(key=lambda e: order_map.get(e.status, 0), reverse=reverse)
        self.endResetModel()

    @property
    def filtered_count(self) -> int:
        return len(self._filtered)

    @property
    def total_count(self) -> int:
        return len(self._all_entries)

    @property
    def filtered_entries(self) -> list[TranslationEntry]:
        return list(self._filtered)


class _StatusDelegate(QStyledItemDelegate):
    """Delegate that shows a QComboBox for the Status column."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(list(STATUS_LABELS.values()))
        return combo

    def setEditorData(self, editor, index):
        current = index.data(Qt.EditRole)
        editor.setCurrentText(current)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


class TranslationTableWidget(QWidget):
    """Enterprise translation table with virtual scrolling, sorting, and editing."""

    translation_edited = Signal(int, str)
    ai_requested = Signal(int)
    ai_batch_requested = Signal(list)
    status_changed = Signal(int, str)
    entry_double_clicked = Signal(int)

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self._config = config
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self._do_filter)
        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        status_label = QLabel("Status:")
        status_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #a6adc8;")
        filter_row.addWidget(status_label)
        self._status_filter = QComboBox()
        self._status_filter.addItems(["All", "Pending", "Translated", "Reviewed", "Approved", "Locked"])
        self._status_filter.setToolTip(
            "Filter entries by workflow status:\n"
            "  Pending — not yet translated\n"
            "  Translated — AI or manually translated, needs review\n"
            "  Reviewed — human-reviewed, ready for approval\n"
            "  Approved — finalized, ready to patch into game\n"
            "  Locked — auto-locked entries (empty, placeholders) that cannot be edited"
        )
        self._status_filter.setFixedWidth(110)
        self._status_filter.currentTextChanged.connect(self._schedule_filter)
        filter_row.addWidget(self._status_filter)

        usage_label = QLabel("Usage:")
        usage_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #a6adc8;")
        filter_row.addWidget(usage_label)
        self._usage_filter = QComboBox()
        self._usage_filter.addItem("All", USAGE_FILTER_ALL)
        self._usage_filter.setToolTip(
            "Filter entries by game usage category.\n"
            "Categories are auto-detected from game data files:\n"
            "  Dialogue / Subtitle — NPC conversations, cutscene text\n"
            "  Quests — quest names, objectives, descriptions\n"
            "  Items — item names, tooltips, descriptions\n"
            "  Skills — skill names, buff descriptions\n"
            "  Knowledge / Codex — lore entries, codex pages\n"
            "  Documents / Books — in-game readable text\n"
            "  Factions — faction names, descriptions\n"
            "  Mount / Vehicle — mount and vehicle names"
        )
        self._usage_filter.setMinimumWidth(120)
        self._usage_filter.currentIndexChanged.connect(self._schedule_filter)
        filter_row.addWidget(self._usage_filter)

        version_label = QLabel("Version:")
        version_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #a6adc8;")
        filter_row.addWidget(version_label)
        self._version_filter = QComboBox()
        self._version_filter.addItem("All Versions", VERSION_FILTER_ALL)
        self._version_filter.setToolTip(
            "Filter entries by the game version where they were first tracked,\n"
            "added, changed, or removed."
        )
        self._version_filter.setMinimumWidth(180)
        self._version_filter.currentIndexChanged.connect(self._schedule_filter)
        filter_row.addWidget(self._version_filter)

        change_label = QLabel("Change:")
        change_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #a6adc8;")
        filter_row.addWidget(change_label)
        self._change_filter = QComboBox()
        for option in CHANGE_FILTER_OPTIONS:
            self._change_filter.addItem(option, option)
        self._change_filter.setToolTip(
            "Filter version history by change type.\n"
            "Added = new keys, Changed = source text changed,\n"
            "Removed = no longer in game, Baseline = first tracked build."
        )
        self._change_filter.setFixedWidth(120)
        self._change_filter.currentIndexChanged.connect(self._schedule_filter)
        filter_row.addWidget(self._change_filter)

        self._search_input = SearchHistoryLineEdit(self._config, "translate")
        self._search_input.setPlaceholderText(
            'Search: text, key:quest*, {*}, locked:yes, empty:yes ...'
        )
        self._search_input.setToolTip(
            "Search syntax:\n"
            '  day                     free text across all fields\n'
            '  "quest day"             exact phrase\n'
            "  key:quest*              wildcard glob on key\n"
            "  key:questdialog_*       all questdialog keys\n"
            "  original:*sword*        original text containing sword\n"
            "  {*  or  {*}            entries with {…} tokens\n"
            "  locked:yes / locked:no  locked entries\n"
            "  empty:yes / empty:no    empty translations\n"
            "  has:braces              entries with {…} placeholders\n"
            "  status:pending          by status\n"
            "  notes:auto-locked       by notes field\n"
            "\n"
            "Combine: key:quest* status:pending empty:yes"
        )
        self._search_input.textChanged.connect(self._schedule_filter)
        self._search_input.setClearButtonEnabled(True)
        filter_row.addWidget(self._search_input, 1)

        ai_sel_btn = QPushButton("AI Selected")
        ai_sel_btn.setObjectName("primary")
        ai_sel_btn.setToolTip(
            "Translate only the selected rows with AI.\n"
            "Ctrl+Click or Shift+Click to select multiple rows.\n"
            "Locked entries in the selection are skipped."
        )
        ai_sel_btn.clicked.connect(self._ai_selected)
        filter_row.addWidget(ai_sel_btn)

        review_all_btn = QPushButton("Review All")
        review_all_btn.setToolTip("Mark all 'Translated' entries as 'Reviewed'.\nWorkflow: Pending → Translated → Reviewed → Approved.")
        review_all_btn.clicked.connect(self._review_all)
        filter_row.addWidget(review_all_btn)

        approve_all_btn = QPushButton("Approve All")
        approve_all_btn.setObjectName("success")
        approve_all_btn.setToolTip("Mark all 'Reviewed' entries as 'Approved'.\nApproved entries are ready to patch into the game.")
        approve_all_btn.clicked.connect(self._approve_all)
        filter_row.addWidget(approve_all_btn)

        export_btn = QPushButton("Export TXT")
        export_btn.setToolTip("Export the currently filtered/visible entries to a .txt file.\nUse filters first to export only specific categories or statuses.")
        export_btn.clicked.connect(self._export_txt)
        filter_row.addWidget(export_btn)

        self._count_label = QLabel("0 entries")
        self._count_label.setToolTip("Number of entries matching the current filters.")
        self._count_label.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: #89b4fa; padding: 0 6px;"
        )
        self._count_label.setMinimumWidth(200)
        filter_row.addWidget(self._count_label)
        layout.addLayout(filter_row)

        self._model = _TranslationModel(self)
        self._model.dataChanged.connect(self._on_data_changed)

        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._view.setAlternatingRowColors(True)
        self._view.setSortingEnabled(True)
        self._view.setShowGrid(False)
        self._view.verticalHeader().setVisible(False)
        self._view.verticalHeader().setDefaultSectionSize(30)
        self._view.horizontalHeader().setSectionResizeMode(COL_NUM, QHeaderView.Fixed)
        self._view.horizontalHeader().setSectionResizeMode(COL_ORIGINAL, QHeaderView.Stretch)
        self._view.horizontalHeader().setSectionResizeMode(COL_TRANSLATION, QHeaderView.Stretch)
        self._view.horizontalHeader().setSectionResizeMode(COL_STATUS, QHeaderView.Fixed)
        self._view.setColumnWidth(COL_NUM, 60)
        self._view.setColumnWidth(COL_STATUS, 100)
        self._view.horizontalHeader().setSortIndicatorShown(True)

        self._view.setItemDelegateForColumn(COL_STATUS, _StatusDelegate(self._view))
        self._view.doubleClicked.connect(self._on_double_click)

        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)

        layout.addWidget(self._view, 1)

    def _setup_shortcuts(self):
        copy_sc = QShortcut(QKeySequence.Copy, self._view)
        copy_sc.activated.connect(self._copy_selection)
        paste_sc = QShortcut(QKeySequence.Paste, self._view)
        paste_sc.activated.connect(self._paste_to_selection)
        # Ctrl+A to select all visible rows
        select_all_sc = QShortcut(QKeySequence.SelectAll, self._view)
        select_all_sc.activated.connect(self._select_all)
        # Delete key to clear selected translations
        delete_sc = QShortcut(QKeySequence.Delete, self._view)
        delete_sc.activated.connect(self._clear_selected_translations)

    def load_entries(
        self,
        entries: list[TranslationEntry],
        usage_filter_options: list[tuple[str, str]] | None = None,
        version_filter_options: list[tuple[str, str]] | None = None,
    ) -> None:
        if usage_filter_options is not None:
            self.set_usage_filter_options(usage_filter_options)
        if version_filter_options is not None:
            self.set_version_filter_options(version_filter_options)
        self._model.set_entries(entries)
        self._update_count()

    def set_usage_filter_options(self, options: list[tuple[str, str]]) -> None:
        current_value = self._usage_filter.currentData() or USAGE_FILTER_ALL
        self._usage_filter.blockSignals(True)
        self._usage_filter.clear()
        for label, value in options:
            self._usage_filter.addItem(label, value)
        restore_index = self._usage_filter.findData(current_value)
        if restore_index == -1:
            restore_index = self._usage_filter.findData(USAGE_FILTER_ALL)
        if restore_index == -1 and self._usage_filter.count():
            restore_index = 0
        if restore_index != -1:
            self._usage_filter.setCurrentIndex(restore_index)
        self._usage_filter.blockSignals(False)

    def set_version_filter_options(self, options: list[tuple[str, str]]) -> None:
        current_value = self._version_filter.currentData() or VERSION_FILTER_ALL
        self._version_filter.blockSignals(True)
        self._version_filter.clear()
        for label, value in options:
            self._version_filter.addItem(label, value)
        restore_index = self._version_filter.findData(current_value)
        if restore_index == -1:
            restore_index = self._version_filter.findData(VERSION_FILTER_ALL)
        if restore_index == -1 and self._version_filter.count():
            restore_index = 0
        if restore_index != -1:
            self._version_filter.setCurrentIndex(restore_index)
        self._version_filter.blockSignals(False)

    def _schedule_filter(self):
        self._filter_timer.start()

    def _do_filter(self):
        status = self._status_filter.currentText()
        search = self._search_input.text()
        category = self._usage_filter.currentData() or USAGE_FILTER_ALL
        version = self._version_filter.currentData() or VERSION_FILTER_ALL
        change = self._change_filter.currentData() or CHANGE_FILTER_ALL
        self._model.set_filter(status, search, category, version, change)
        self._update_count()

    def _update_count(self):
        fc = self._model.filtered_count
        tc = self._model.total_count
        pending = 0
        translated = 0
        reviewed = 0
        approved = 0
        for i in range(fc):
            entry = self._model.entry_at(i)
            if not entry:
                continue
            if entry.status == StringStatus.PENDING:
                pending += 1
            elif entry.status == StringStatus.TRANSLATED:
                translated += 1
            elif entry.status == StringStatus.REVIEWED:
                reviewed += 1
            elif entry.status == StringStatus.APPROVED:
                approved += 1
        self._count_label.setText(
            f"{fc:,}/{tc:,} | P:{pending:,} T:{translated:,} R:{reviewed:,} A:{approved:,}"
        )

    def _on_data_changed(self, tl, br, roles):
        row = tl.row()
        col = tl.column()
        entry = self._model.entry_at(row)
        if not entry:
            return
        if col == COL_TRANSLATION:
            self.translation_edited.emit(entry.index, entry.translated_text)
        elif col == COL_STATUS:
            self.status_changed.emit(entry.index, STATUS_LABELS.get(entry.status, "Pending"))

    def _on_double_click(self, index):
        row = index.row()
        entry = self._model.entry_at(row)
        if entry:
            self.entry_double_clicked.emit(entry.index)

    def _ai_selected(self):
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return
        if len(indexes) == 1:
            entry = self._model.entry_at(indexes[0].row())
            if entry:
                self.ai_requested.emit(entry.index)
        else:
            indices = []
            for idx in indexes:
                entry = self._model.entry_at(idx.row())
                if entry:
                    indices.append(entry.index)
            if indices:
                self.ai_batch_requested.emit(indices)

    def _copy_selection(self):
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return
        lines = []
        for idx in indexes:
            entry = self._model.entry_at(idx.row())
            if entry:
                trans = entry.translated_text or entry.original_text
                lines.append(f"{entry.key}\t{entry.original_text}\t{trans}")
        clipboard = QApplication.clipboard()
        clipboard.setText("\n".join(lines))

    def _paste_to_selection(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if not text:
            return
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return
        lines = [ln for ln in text.split("\n") if ln.strip()]

        # If single line pasted to multiple rows, apply same text to all selected
        single_paste = len(lines) == 1
        pasted = 0

        for i, idx in enumerate(indexes):
            if single_paste:
                line = lines[0]
            elif i < len(lines):
                line = lines[i]
            else:
                break
            parts = line.split("\t")
            paste_text = parts[-1] if parts else line
            entry = self._model.entry_at(idx.row())
            if entry:
                self._model.setData(
                    self._model.index(idx.row(), COL_TRANSLATION),
                    paste_text, Qt.EditRole,
                )
                pasted += 1

        if pasted:
            self._update_count()
            self.status_changed.emit(-1, f"Pasted to {pasted} entries")

    def _select_all(self):
        """Select all visible rows in the table."""
        self._view.selectAll()

    def _clear_selected_translations(self):
        """Clear translation text for all selected rows and revert to Pending."""
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return
        count = 0
        for idx in indexes:
            entry = self._model.entry_at(idx.row())
            if entry and entry.translated_text:
                entry.revert_to_pending()
                count += 1
        if count:
            self._model.beginResetModel()
            self._model._refilter()
            self._model.endResetModel()
            self._update_count()
            self.status_changed.emit(-1, f"Cleared {count} entries")

    def _review_all(self):
        count = 0
        self._model.beginResetModel()
        for entry in self._model._all_entries:
            if entry.status == StringStatus.TRANSLATED:
                entry.status = StringStatus.REVIEWED
                count += 1
        self._model._refilter()
        self._model.endResetModel()
        self._update_count()
        if count:
            self.status_changed.emit(-1, f"Reviewed {count} entries")

    def _approve_all(self):
        count = 0
        self._model.beginResetModel()
        for entry in self._model._all_entries:
            if entry.status in (StringStatus.TRANSLATED, StringStatus.REVIEWED):
                entry.status = StringStatus.APPROVED
                count += 1
        self._model._refilter()
        self._model.endResetModel()
        self._update_count()
        if count:
            self.status_changed.emit(-1, f"Approved {count} entries")

    def _export_txt(self):
        from ui.dialogs.file_picker import pick_save_file
        path = pick_save_file(self, "Export to Text File", "", "Text Files (*.txt);;All Files (*.*)")
        if not path:
            return
        entries_to_export, export_scope = self.get_export_entries()
        if not entries_to_export:
            from ui.dialogs.confirmation import show_info

            show_info(self, "Export", "No entries match the current export scope.")
            return

        with open(path, "w", encoding="utf-8") as f:
            for entry in entries_to_export:
                trans = entry.translated_text or ""
                status = STATUS_LABELS.get(entry.status, "Pending")
                f.write(f"{entry.key}\t{entry.original_text}\t{trans}\t{status}\n")

        from ui.dialogs.confirmation import show_info
        show_info(
            self,
            "Export Complete",
            f"Exported {len(entries_to_export)} {export_scope} entries to {path}",
        )

    def _show_context_menu(self, pos):
        menu = QMenu(self._view)
        indexes = self._view.selectionModel().selectedRows()
        sel_count = len(indexes)

        copy_act = QAction(f"Copy ({sel_count} rows)  Ctrl+C", self)
        copy_act.triggered.connect(self._copy_selection)
        copy_act.setEnabled(sel_count > 0)
        menu.addAction(copy_act)

        paste_act = QAction("Paste to Selected  Ctrl+V", self)
        paste_act.triggered.connect(self._paste_to_selection)
        paste_act.setEnabled(sel_count > 0)
        menu.addAction(paste_act)

        menu.addSeparator()

        if sel_count > 0:
            ai_act = QAction(f"Translate with AI ({sel_count} selected)", self)
            ai_act.triggered.connect(self._ai_selected)
            menu.addAction(ai_act)

            menu.addSeparator()

            review_act = QAction(f"Set {sel_count} Selected to Reviewed", self)
            review_act.triggered.connect(lambda: self._set_selected_status(StringStatus.REVIEWED))
            menu.addAction(review_act)

            approve_act = QAction(f"Set {sel_count} Selected to Approved", self)
            approve_act.triggered.connect(lambda: self._set_selected_status(StringStatus.APPROVED))
            menu.addAction(approve_act)

            pending_act = QAction(f"Revert {sel_count} Selected to Pending", self)
            pending_act.triggered.connect(lambda: self._set_selected_status(StringStatus.PENDING))
            menu.addAction(pending_act)

            menu.addSeparator()

            clear_act = QAction(f"Clear {sel_count} Selected Translations  Del", self)
            clear_act.triggered.connect(self._clear_selected_translations)
            menu.addAction(clear_act)

        menu.addSeparator()

        select_all_act = QAction(f"Select All  Ctrl+A", self)
        select_all_act.triggered.connect(self._select_all)
        menu.addAction(select_all_act)

        export_act = QAction("Export to TXT...", self)
        export_act.triggered.connect(self._export_txt)
        menu.addAction(export_act)

        menu.popup(self._view.viewport().mapToGlobal(pos))

    def _set_selected_status(self, status: StringStatus):
        indexes = self._view.selectionModel().selectedRows()
        count = 0
        for idx in indexes:
            entry = self._model.entry_at(idx.row())
            if not entry:
                continue
            if status == StringStatus.PENDING:
                entry.revert_to_pending()
                count += 1
            elif status == StringStatus.REVIEWED:
                if entry.status in (StringStatus.TRANSLATED, StringStatus.APPROVED):
                    entry.status = StringStatus.REVIEWED
                    count += 1
            elif status == StringStatus.APPROVED:
                if entry.status in (StringStatus.TRANSLATED, StringStatus.REVIEWED):
                    entry.status = StringStatus.APPROVED
                    count += 1
        self._model.beginResetModel()
        self._model._refilter()
        self._model.endResetModel()
        self._update_count()
        if count:
            label = STATUS_LABELS.get(status, "Unknown")
            self.status_changed.emit(-1, f"Set {count} entries to {label}")

    def update_entry_row(self, entry_index: int) -> None:
        for row in range(self._model.rowCount()):
            entry = self._model.entry_at(row)
            if entry and entry.index == entry_index:
                tl = self._model.index(row, 0)
                br = self._model.index(row, COLUMN_COUNT - 1)
                self._model.dataChanged.emit(tl, br, [Qt.DisplayRole, Qt.ForegroundRole])
                break

    def refresh(self):
        self._model.beginResetModel()
        self._model._refilter()
        self._model.endResetModel()
        self._update_count()

    def get_filter_state(self) -> dict[str, str]:
        return {
            "status": self._status_filter.currentText(),
            "usage": self._usage_filter.currentData() or USAGE_FILTER_ALL,
            "version": self._version_filter.currentData() or VERSION_FILTER_ALL,
            "change": self._change_filter.currentData() or CHANGE_FILTER_ALL,
            "search": self._search_input.text().strip(),
        }

    def get_filtered_entries(self) -> list[TranslationEntry]:
        return self._model.filtered_entries

    def get_selected_entries(self) -> list[TranslationEntry]:
        entries = []
        for idx in self._view.selectionModel().selectedRows():
            entry = self._model.entry_at(idx.row())
            if entry:
                entries.append(entry)
        return entries

    def get_export_entries(self) -> tuple[list[TranslationEntry], str]:
        selected_entries = self.get_selected_entries()
        if selected_entries:
            return selected_entries, "selected"
        return self.get_filtered_entries(), "filtered"
