"""Live character and item workbench for Explorer.

This widget adds enterprise-style navigation on top of Explorer without
replacing the Explorer file table. It resolves live game records to exact
archive paths, then asks Explorer to scope the existing table to those paths
so all current preview/export/import/patch actions keep working.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.asset_catalog import (
    CharacterRecord,
    CharacterWorkbenchData,
    FamilyProfile,
    WorkbenchItemRecord,
    build_character_workbench_from_vfs,
)
from core.dds_reader import decode_dds_to_rgba, read_dds_info
from core.vfs_manager import VfsManager
from ui.widgets.search_history_line_edit import SearchHistoryLineEdit
from utils.platform_utils import format_file_size
from utils.thread_worker import FunctionWorker


_CHAR_HEADERS = ["Name", "App ID", "Family", "Gender", "Human", "Slots", "Media", "Files"]
_ITEM_HEADERS = ["Item", "Top", "Category", "Type", "Wearable For", "Named", "PACs"]
_FAMILY_HEADERS = ["Family", "Gender", "Human", "Characters", "Named", "Images"]


@dataclass(slots=True)
class _UiImageRecord:
    path: str
    label: str
    score: int


class _ZoomableImagePane(QWidget):
    def __init__(self, empty_text: str, parent=None):
        super().__init__(parent)
        self._base_pixmap = QPixmap()
        self._scale_factor = 1.0
        self._fit_mode = True
        self._empty_text = empty_text

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        toolbar = QHBoxLayout()
        self._hint_label = QLabel("Wheel to zoom | Fit to view")
        self._hint_label.setStyleSheet("color: #a6adc8;")
        toolbar.addWidget(self._hint_label, 1)

        zoom_out = QPushButton("-")
        zoom_out.setFixedWidth(28)
        zoom_out.clicked.connect(lambda: self._step_zoom(0.85))
        toolbar.addWidget(zoom_out)

        zoom_in = QPushButton("+")
        zoom_in.setFixedWidth(28)
        zoom_in.clicked.connect(lambda: self._step_zoom(1.15))
        toolbar.addWidget(zoom_in)

        actual_btn = QPushButton("100%")
        actual_btn.clicked.connect(self._show_actual_size)
        toolbar.addWidget(actual_btn)

        fit_btn = QPushButton("Fit")
        fit_btn.clicked.connect(self._fit_to_view)
        toolbar.addWidget(fit_btn)
        layout.addLayout(toolbar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.viewport().installEventFilter(self)
        self._label = QLabel(self._empty_text)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setMinimumSize(280, 180)
        self._scroll.setWidget(self._label)
        layout.addWidget(self._scroll, 1)

        self.setStyleSheet("QScrollArea { border: 1px solid #313244; border-radius: 8px; }")

    def eventFilter(self, watched, event):
        if watched is self._scroll.viewport() and event.type() == QEvent.Wheel and not self._base_pixmap.isNull():
            delta = event.angleDelta().y()
            if delta:
                self._step_zoom(1.1 if delta > 0 else 0.9)
                return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode and not self._base_pixmap.isNull():
            self._render_pixmap()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._base_pixmap = pixmap
        self._scale_factor = 1.0
        self._fit_mode = True
        self._render_pixmap()

    def clear_message(self, text: str) -> None:
        self._base_pixmap = QPixmap()
        self._scale_factor = 1.0
        self._fit_mode = True
        self._label.clear()
        self._label.setText(text)
        self._label.setMinimumSize(280, 180)

    def _step_zoom(self, factor: float) -> None:
        if self._base_pixmap.isNull():
            return
        self._fit_mode = False
        self._scale_factor = max(0.1, min(self._scale_factor * factor, 12.0))
        self._render_pixmap()

    def _show_actual_size(self) -> None:
        if self._base_pixmap.isNull():
            return
        self._fit_mode = False
        self._scale_factor = 1.0
        self._render_pixmap()

    def _fit_to_view(self) -> None:
        if self._base_pixmap.isNull():
            return
        self._fit_mode = True
        self._render_pixmap()

    def _render_pixmap(self) -> None:
        if self._base_pixmap.isNull():
            return
        if self._fit_mode:
            viewport = self._scroll.viewport().size()
            target = self._base_pixmap.scaled(
                max(100, viewport.width() - 8),
                max(100, viewport.height() - 8),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self._label.setPixmap(target)
            self._label.resize(target.size())
        else:
            width = max(1, int(self._base_pixmap.width() * self._scale_factor))
            height = max(1, int(self._base_pixmap.height() * self._scale_factor))
            target = self._base_pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._label.setPixmap(target)
            self._label.resize(target.size())


class _CharacterTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._records: list[CharacterRecord] = []
        self._filtered: list[int] = []
        self._search = ""
        self._scope = "All Characters"
        self._gender = "All"
        self._family = "All Families"
        self._slot = "All Parts"
        self._status = "All Records"

    def set_records(self, records: list[CharacterRecord]) -> None:
        self.beginResetModel()
        self._records = records
        self._refilter()
        self.endResetModel()

    def set_filters(self, *, search: str, scope: str, gender: str, family: str, slot: str, status: str) -> None:
        self.beginResetModel()
        self._search = search.strip().lower()
        self._scope = scope
        self._gender = gender
        self._family = family
        self._slot = slot
        self._status = status
        self._refilter()
        self.endResetModel()

    @staticmethod
    def has_preview(record: CharacterRecord) -> bool:
        return any(
            linked.kind == "Mesh"
            and linked.resolved
            and linked.path.lower().endswith((".pac", ".pam", ".pamlod"))
            for linked in record.files
        )

    @staticmethod
    def has_image(record: CharacterRecord) -> bool:
        return any(media.media_type == "Image" for media in record.media)

    @staticmethod
    def has_video(record: CharacterRecord) -> bool:
        return any(media.media_type == "Video" for media in record.media)

    def _refilter(self) -> None:
        filtered: list[int] = []
        for index, record in enumerate(self._records):
            if self._scope == "Human Only" and not record.likely_human:
                continue
            if self._gender != "All" and record.gender != self._gender:
                continue
            if self._family != "All Families" and record.family_code != self._family:
                continue
            if self._slot != "All Parts" and not any(linked.slot == self._slot for linked in record.files):
                continue
            if self._status == "Named Only" and record.name_source == "identity":
                continue
            if self._status == "Has Image" and not self.has_image(record):
                continue
            if self._status == "Has Video" and not self.has_video(record):
                continue
            if self._status == "Has Media" and not record.media:
                continue
            if self._status == "Has Preview" and not self.has_preview(record):
                continue
            if self._status == "Has Unresolved" and not any(not linked.resolved for linked in record.files):
                continue
            if self._status == "Clean Only" and any(not linked.resolved for linked in record.files):
                continue
            if self._search and self._search not in record.search_text:
                continue
            filtered.append(index)
        self._filtered = filtered

    def row_at(self, row: int) -> CharacterRecord | None:
        if 0 <= row < len(self._filtered):
            return self._records[self._filtered[row]]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_CHAR_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _CHAR_HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        record = self.row_at(index.row())
        if record is None:
            return None

        if role == Qt.DisplayRole:
            if index.column() == 0:
                return record.display_name
            if index.column() == 1:
                return record.app_id
            if index.column() == 2:
                return record.family_code
            if index.column() == 3:
                return record.gender
            if index.column() == 4:
                return "Yes" if record.likely_human else "No"
            if index.column() == 5:
                return len(record.slots)
            if index.column() == 6:
                return len(record.media)
            if index.column() == 7:
                return len(record.files)

        if role == Qt.ToolTipRole:
            aliases = ", ".join(record.aliases) if record.aliases else "-"
            return (
                f"App ID: {record.app_id}\n"
                f"Name source: {record.name_source}\n"
                f"Family: {record.family_code}\n"
                f"Gender: {record.gender}\n"
                f"Likely human: {'Yes' if record.likely_human else 'No'}\n"
                f"Aliases: {aliases}\n"
                f"Media assets: {len(record.media):,}\n"
                f"Linked files: {len(record.files):,}"
            )

        if role == Qt.ForegroundRole and not record.likely_human:
            return QBrush(QColor("#9399b2"))

        if role == Qt.UserRole:
            return record
        return None

    def sort(self, column, order=Qt.AscendingOrder) -> None:
        reverse = order == Qt.DescendingOrder
        self.beginResetModel()
        key_funcs = {
            0: lambda record: record.display_name.lower(),
            1: lambda record: record.app_id.lower(),
            2: lambda record: record.family_code.lower(),
            3: lambda record: record.gender.lower(),
            4: lambda record: record.likely_human,
            5: lambda record: len(record.slots),
            6: lambda record: len(record.media),
            7: lambda record: len(record.files),
        }
        key_fn = key_funcs.get(column, key_funcs[0])
        self._filtered.sort(key=lambda idx: key_fn(self._records[idx]), reverse=reverse)
        self.endResetModel()

    @property
    def filtered_count(self) -> int:
        return len(self._filtered)


class _ItemTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[WorkbenchItemRecord] = []
        self._filtered: list[int] = []
        self._search = ""
        self._top = "Equipment Only"
        self._wearable = "Any Target"
        self._confidence = "All Confidence"
        self._selected_record: CharacterRecord | None = None

    def set_items(self, items: list[WorkbenchItemRecord]) -> None:
        self.beginResetModel()
        self._items = items
        self._refilter()
        self.endResetModel()

    def set_filters(
        self,
        *,
        search: str,
        top: str,
        wearable: str,
        confidence: str,
        selected_record: CharacterRecord | None,
    ) -> None:
        self.beginResetModel()
        self._search = search.strip().lower()
        self._top = top
        self._wearable = wearable
        self._confidence = confidence
        self._selected_record = selected_record
        self._refilter()
        self.endResetModel()

    def _matches_selected_record(self, item: WorkbenchItemRecord) -> bool:
        record = self._selected_record
        if record is None:
            return False
        return record.display_name in item.direct_name_matches or record.family_code in item.family_codes

    def _refilter(self) -> None:
        filtered: list[int] = []
        for idx, item in enumerate(self._items):
            if self._top == "Equipment Only" and item.top_category != "Equipment":
                continue
            if self._top == "Weapons" and item.category != "Weapon":
                continue
            if self._top == "Armor" and item.category != "Armor":
                continue
            if self._top == "Accessories" and item.category != "Accessory":
                continue
            if self._top == "Materials" and item.top_category != "Material":
                continue
            if self._wearable == "Has Wearability" and not (item.family_codes or item.direct_name_matches):
                continue
            if self._wearable == "Selected Character" and not self._matches_selected_record(item):
                continue
            if self._wearable == "Selected Family":
                if self._selected_record is None or self._selected_record.family_code not in item.family_codes:
                    continue
            if self._confidence != "All Confidence" and item.compatibility_confidence != self._confidence.lower():
                continue
            if self._search and self._search not in item.search_text:
                continue
            filtered.append(idx)
        self._filtered = filtered

    def row_at(self, row: int) -> WorkbenchItemRecord | None:
        if 0 <= row < len(self._filtered):
            return self._items[self._filtered[row]]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_ITEM_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _ITEM_HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.row_at(index.row())
        if item is None:
            return None

        if role == Qt.DisplayRole:
            if index.column() == 0:
                return item.internal_name
            if index.column() == 1:
                return item.top_category
            if index.column() == 2:
                return item.category
            if index.column() == 3:
                return item.raw_type
            if index.column() == 4:
                return ", ".join(item.family_codes) if item.family_codes else "-"
            if index.column() == 5:
                return len(item.direct_name_matches)
            if index.column() == 6:
                return len(item.effective_pac_files)

        if role == Qt.ToolTipRole:
            return (
                f"{item.internal_name}\n"
                f"Top: {item.top_category}\n"
                f"Category: {item.category} > {item.subcategory} > {item.subsubcategory}\n"
                f"Raw type: {item.raw_type}\n"
                f"Family codes: {', '.join(item.family_codes) or '-'}\n"
                f"Named matches: {', '.join(item.direct_name_matches) or '-'}\n"
                f"PACs: {len(item.effective_pac_files)}\n"
                f"Compatibility confidence: {item.compatibility_confidence}"
            )

        if role == Qt.ForegroundRole and item.compatibility_confidence == "unknown":
            return QBrush(QColor("#9399b2"))
        if role == Qt.ForegroundRole and item.compatibility_confidence == "medium":
            return QBrush(QColor("#f9e2af"))
        if role == Qt.ForegroundRole and item.compatibility_confidence == "high":
            return QBrush(QColor("#a6e3a1"))

        if role == Qt.UserRole:
            return item
        return None

    def sort(self, column, order=Qt.AscendingOrder) -> None:
        reverse = order == Qt.DescendingOrder
        self.beginResetModel()
        key_funcs = {
            0: lambda item: item.internal_name.lower(),
            1: lambda item: item.top_category.lower(),
            2: lambda item: item.category.lower(),
            3: lambda item: item.raw_type.lower(),
            4: lambda item: ",".join(item.family_codes).lower(),
            5: lambda item: len(item.direct_name_matches),
            6: lambda item: len(item.effective_pac_files),
        }
        key_fn = key_funcs.get(column, key_funcs[0])
        self._filtered.sort(key=lambda idx: key_fn(self._items[idx]), reverse=reverse)
        self.endResetModel()

    @property
    def filtered_count(self) -> int:
        return len(self._filtered)


class _FamilyTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._profiles: list[FamilyProfile] = []
        self._filtered: list[int] = []
        self._search = ""
        self._human_only = False
        self._named_only = False
        self._family_to_records: dict[str, list[CharacterRecord]] = {}

    def set_profiles(self, profiles: list[FamilyProfile], family_to_records: dict[str, list[CharacterRecord]]) -> None:
        self.beginResetModel()
        self._profiles = profiles
        self._family_to_records = family_to_records
        self._refilter()
        self.endResetModel()

    def set_filters(self, *, search: str, human_only: bool, named_only: bool) -> None:
        self.beginResetModel()
        self._search = search.strip().lower()
        self._human_only = human_only
        self._named_only = named_only
        self._refilter()
        self.endResetModel()

    def _named_count(self, profile: FamilyProfile) -> int:
        return sum(1 for record in self._family_to_records.get(profile.family_code, []) if record.name_source != "identity")

    def _image_count(self, profile: FamilyProfile) -> int:
        return sum(
            1
            for record in self._family_to_records.get(profile.family_code, [])
            if any(media.media_type == "Image" for media in record.media)
        )

    def _refilter(self) -> None:
        filtered: list[int] = []
        for idx, profile in enumerate(self._profiles):
            if self._human_only and not profile.likely_human:
                continue
            if self._named_only and self._named_count(profile) == 0:
                continue
            haystack = " ".join([profile.family_code, profile.label, " ".join(profile.example_names)]).lower()
            if self._search and self._search not in haystack:
                continue
            filtered.append(idx)
        self._filtered = filtered

    def row_at(self, row: int) -> FamilyProfile | None:
        if 0 <= row < len(self._filtered):
            return self._profiles[self._filtered[row]]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_FAMILY_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _FAMILY_HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        profile = self.row_at(index.row())
        if profile is None:
            return None

        if role == Qt.DisplayRole:
            if index.column() == 0:
                return profile.family_code
            if index.column() == 1:
                return profile.gender
            if index.column() == 2:
                return "Yes" if profile.likely_human else "No"
            if index.column() == 3:
                return profile.character_count
            if index.column() == 4:
                return self._named_count(profile)
            if index.column() == 5:
                return self._image_count(profile)

        if role == Qt.ToolTipRole:
            examples = ", ".join(profile.example_names) if profile.example_names else "-"
            return (
                f"Family: {profile.family_code}\n"
                f"Label: {profile.label}\n"
                f"Gender: {profile.gender}\n"
                f"Likely human: {'Yes' if profile.likely_human else 'No'}\n"
                f"Characters: {profile.character_count}\n"
                f"Examples: {examples}"
            )

        if role == Qt.ForegroundRole and not profile.likely_human:
            return QBrush(QColor("#9399b2"))

        if role == Qt.UserRole:
            return profile
        return None

    def sort(self, column, order=Qt.AscendingOrder) -> None:
        reverse = order == Qt.DescendingOrder
        self.beginResetModel()
        key_funcs = {
            0: lambda profile: profile.family_code.lower(),
            1: lambda profile: profile.gender.lower(),
            2: lambda profile: profile.likely_human,
            3: lambda profile: profile.character_count,
            4: lambda profile: self._named_count(profile),
            5: lambda profile: self._image_count(profile),
        }
        key_fn = key_funcs.get(column, key_funcs[0])
        self._filtered.sort(key=lambda idx: key_fn(self._profiles[idx]), reverse=reverse)
        self.endResetModel()

    @property
    def filtered_count(self) -> int:
        return len(self._filtered)


class ExplorerWorkbench(QWidget):
    """Explorer-side live character, family, and item navigator."""

    scope_requested = Signal(list, str, str)
    clear_scope_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._vfs: VfsManager | None = None
        self._worker: FunctionWorker | None = None
        self._workbench: CharacterWorkbenchData | None = None
        self._characters: list[CharacterRecord] = []
        self._items: list[WorkbenchItemRecord] = []
        self._families: list[FamilyProfile] = []
        self._family_to_records: dict[str, list[CharacterRecord]] = {}
        self._family_profiles_by_code: dict[str, FamilyProfile] = {}
        self._ui_entry_map: dict[str, object] = {}
        self._char_model = _CharacterTableModel(self)
        self._item_model = _ItemTableModel(self)
        self._family_model = _FamilyTableModel(self)
        self._building = False
        self._loaded_signature = ""
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Navigator Workbench")
        title.setStyleSheet("font-weight: 700; font-size: 12px; padding: 2px;")
        header.addWidget(title)

        self._summary_label = QLabel("Load game data to build live character and item navigation.")
        self._summary_label.setStyleSheet("color: #a6adc8; padding-left: 4px;")
        header.addWidget(self._summary_label, 1)

        self._reload_btn = QPushButton("Reload")
        self._reload_btn.clicked.connect(self._reload)
        header.addWidget(self._reload_btn)

        self._clear_btn = QPushButton("Clear Scope")
        self._clear_btn.clicked.connect(self.clear_scope_requested.emit)
        header.addWidget(self._clear_btn)
        outer.addLayout(header)

        self._status_label = QLabel("Workbench idle")
        self._status_label.setStyleSheet("color: #89b4fa; padding: 0 2px;")
        outer.addWidget(self._status_label)

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_character_tab(), "Characters")
        self._tabs.addTab(self._build_item_tab(), "Items")
        self._tabs.addTab(self._build_family_tab(), "Families")

    def _build_character_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        filters_top = QHBoxLayout()
        self._char_scope_combo = QComboBox()
        self._char_scope_combo.addItems(["All Characters", "Human Only"])
        self._char_scope_combo.currentTextChanged.connect(self._on_char_scope_changed)
        filters_top.addWidget(self._char_scope_combo)

        self._char_gender_combo = QComboBox()
        self._char_gender_combo.addItems(["All", "Male", "Female"])
        self._char_gender_combo.currentTextChanged.connect(self._apply_character_filters)
        self._char_gender_combo.setEnabled(False)
        filters_top.addWidget(self._char_gender_combo)

        self._char_family_combo = QComboBox()
        self._char_family_combo.addItem("All Families", "All Families")
        self._char_family_combo.currentTextChanged.connect(self._apply_character_filters)
        filters_top.addWidget(self._char_family_combo)
        layout.addLayout(filters_top)

        filters_bottom = QHBoxLayout()
        self._char_slot_combo = QComboBox()
        self._char_slot_combo.addItem("All Parts")
        self._char_slot_combo.currentTextChanged.connect(self._apply_character_filters)
        filters_bottom.addWidget(self._char_slot_combo)

        self._char_status_combo = QComboBox()
        self._char_status_combo.addItems(
            ["All Records", "Named Only", "Has Image", "Has Video", "Has Media", "Has Preview", "Has Unresolved", "Clean Only"]
        )
        self._char_status_combo.currentTextChanged.connect(self._apply_character_filters)
        filters_bottom.addWidget(self._char_status_combo)
        layout.addLayout(filters_bottom)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Explorer Scope:"))
        self._char_scope_mode_combo = QComboBox()
        self._char_scope_mode_combo.addItems(["All Related", "Meshes Only", "Media Only", "Selected Part"])
        self._char_scope_mode_combo.currentTextChanged.connect(self._emit_character_scope)
        scope_row.addWidget(self._char_scope_mode_combo)

        self._char_scope_part_combo = QComboBox()
        self._char_scope_part_combo.addItem("All Parts")
        self._char_scope_part_combo.currentTextChanged.connect(self._emit_character_scope)
        scope_row.addWidget(self._char_scope_part_combo)
        layout.addLayout(scope_row)

        self._char_search = SearchHistoryLineEdit(self._config, "explorer_workbench_characters")
        self._char_search.setPlaceholderText("Search name, app id, family, alias, slot, file...")
        self._char_search.textChanged.connect(self._apply_character_filters)
        layout.addWidget(self._char_search)

        splitter = QSplitter(Qt.Vertical)

        self._char_table = QTableView()
        self._char_table.setModel(self._char_model)
        self._char_table.setSortingEnabled(True)
        self._char_table.setSelectionBehavior(QTableView.SelectRows)
        self._char_table.setSelectionMode(QTableView.SingleSelection)
        self._char_table.setAlternatingRowColors(True)
        self._char_table.verticalHeader().setVisible(False)
        self._char_table.horizontalHeader().setStretchLastSection(True)
        self._char_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 8):
            self._char_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._char_table.selectionModel().currentRowChanged.connect(self._on_character_selected)
        splitter.addWidget(self._char_table)

        bottom = QSplitter(Qt.Horizontal)

        image_panel = QWidget()
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(4)

        media_row = QHBoxLayout()
        media_row.addWidget(QLabel("Live Image:"))
        self._char_image_combo = QComboBox()
        self._char_image_combo.currentIndexChanged.connect(self._on_character_image_changed)
        media_row.addWidget(self._char_image_combo, 1)
        image_layout.addLayout(media_row)

        self._char_image_info = QLabel("Select a character to inspect live portraits and UI media.")
        self._char_image_info.setWordWrap(True)
        image_layout.addWidget(self._char_image_info)

        self._char_image_view = _ZoomableImagePane("No image selected")
        image_layout.addWidget(self._char_image_view, 1)
        bottom.addWidget(image_panel)

        details_panel = QWidget()
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(4)
        self._char_details = QTextEdit()
        self._char_details.setReadOnly(True)
        self._char_details.setLineWrapMode(QTextEdit.NoWrap)
        details_layout.addWidget(self._char_details, 1)
        bottom.addWidget(details_panel)

        bottom.setStretchFactor(0, 4)
        bottom.setStretchFactor(1, 5)
        bottom.setSizes([420, 520])
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([420, 300])
        layout.addWidget(splitter, 1)
        return page

    def _build_item_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self._item_top_combo = QComboBox()
        self._item_top_combo.addItems(["All Items", "Equipment Only", "Weapons", "Armor", "Accessories", "Materials"])
        self._item_top_combo.setCurrentText("Equipment Only")
        self._item_top_combo.currentTextChanged.connect(self._apply_item_filters)
        top.addWidget(self._item_top_combo)

        self._item_wearable_combo = QComboBox()
        self._item_wearable_combo.addItems(["Any Target", "Has Wearability", "Selected Character", "Selected Family"])
        self._item_wearable_combo.currentTextChanged.connect(self._apply_item_filters)
        top.addWidget(self._item_wearable_combo)

        self._item_confidence_combo = QComboBox()
        self._item_confidence_combo.addItems(["All Confidence", "High", "Medium", "Low", "Unknown"])
        self._item_confidence_combo.currentTextChanged.connect(self._apply_item_filters)
        top.addWidget(self._item_confidence_combo)
        layout.addLayout(top)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Explorer Scope:"))
        self._item_scope_mode_combo = QComboBox()
        self._item_scope_mode_combo.addItems(["All Related", "Meshes Only", "Icons Only"])
        self._item_scope_mode_combo.currentTextChanged.connect(self._emit_item_scope)
        scope_row.addWidget(self._item_scope_mode_combo)
        layout.addLayout(scope_row)

        self._item_search = SearchHistoryLineEdit(self._config, "explorer_workbench_items")
        self._item_search.setPlaceholderText("Search items, categories, PACs, named matches...")
        self._item_search.textChanged.connect(self._apply_item_filters)
        layout.addWidget(self._item_search)

        splitter = QSplitter(Qt.Vertical)

        self._item_table = QTableView()
        self._item_table.setModel(self._item_model)
        self._item_table.setSortingEnabled(True)
        self._item_table.setSelectionBehavior(QTableView.SelectRows)
        self._item_table.setSelectionMode(QTableView.SingleSelection)
        self._item_table.setAlternatingRowColors(True)
        self._item_table.verticalHeader().setVisible(False)
        self._item_table.horizontalHeader().setStretchLastSection(True)
        self._item_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 7):
            self._item_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._item_table.selectionModel().currentRowChanged.connect(self._on_item_selected)
        splitter.addWidget(self._item_table)

        bottom = QSplitter(Qt.Horizontal)

        icon_panel = QWidget()
        icon_layout = QVBoxLayout(icon_panel)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_layout.setSpacing(4)

        icon_row = QHBoxLayout()
        icon_row.addWidget(QLabel("Live Item Icon:"))
        self._item_icon_combo = QComboBox()
        self._item_icon_combo.currentIndexChanged.connect(self._on_item_icon_changed)
        icon_row.addWidget(self._item_icon_combo, 1)
        icon_layout.addLayout(icon_row)

        self._item_icon_info = QLabel("Select an item to inspect live icon candidates.")
        self._item_icon_info.setWordWrap(True)
        icon_layout.addWidget(self._item_icon_info)

        self._item_icon_view = _ZoomableImagePane("No icon selected")
        icon_layout.addWidget(self._item_icon_view, 1)
        bottom.addWidget(icon_panel)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)
        self._item_details = QTextEdit()
        self._item_details.setReadOnly(True)
        self._item_details.setLineWrapMode(QTextEdit.NoWrap)
        detail_layout.addWidget(self._item_details, 1)
        bottom.addWidget(detail_panel)

        bottom.setStretchFactor(0, 4)
        bottom.setStretchFactor(1, 5)
        bottom.setSizes([420, 520])
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([420, 300])
        layout.addWidget(splitter, 1)
        return page

    def _build_family_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self._family_search = SearchHistoryLineEdit(self._config, "explorer_workbench_families")
        self._family_search.setPlaceholderText("Search family code, label, example names...")
        self._family_search.textChanged.connect(self._apply_family_filters)
        top.addWidget(self._family_search, 1)

        self._family_human_combo = QComboBox()
        self._family_human_combo.addItems(["All Families", "Human Families"])
        self._family_human_combo.currentTextChanged.connect(self._apply_family_filters)
        top.addWidget(self._family_human_combo)

        self._family_named_combo = QComboBox()
        self._family_named_combo.addItems(["All Naming", "Named Families"])
        self._family_named_combo.currentTextChanged.connect(self._apply_family_filters)
        top.addWidget(self._family_named_combo)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Vertical)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Explorer Scope:"))
        self._family_scope_mode_combo = QComboBox()
        self._family_scope_mode_combo.addItems(["Character Files", "Meshes Only", "Media Only"])
        self._family_scope_mode_combo.currentTextChanged.connect(self._emit_family_scope)
        scope_row.addWidget(self._family_scope_mode_combo)
        layout.addLayout(scope_row)

        self._family_table = QTableView()
        self._family_table.setModel(self._family_model)
        self._family_table.setSortingEnabled(True)
        self._family_table.setSelectionBehavior(QTableView.SelectRows)
        self._family_table.setSelectionMode(QTableView.SingleSelection)
        self._family_table.setAlternatingRowColors(True)
        self._family_table.verticalHeader().setVisible(False)
        self._family_table.horizontalHeader().setStretchLastSection(True)
        for column in range(0, 6):
            mode = QHeaderView.Stretch if column == 0 else QHeaderView.ResizeToContents
            self._family_table.horizontalHeader().setSectionResizeMode(column, mode)
        self._family_table.selectionModel().currentRowChanged.connect(self._on_family_selected)
        splitter.addWidget(self._family_table)

        bottom = QSplitter(Qt.Horizontal)

        image_panel = QWidget()
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(4)

        self._family_image_info = QLabel("Select a family to inspect its primary live image.")
        self._family_image_info.setWordWrap(True)
        image_layout.addWidget(self._family_image_info)

        self._family_image_view = _ZoomableImagePane("No family image selected")
        image_layout.addWidget(self._family_image_view, 1)
        bottom.addWidget(image_panel)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)
        self._family_details = QTextEdit()
        self._family_details.setReadOnly(True)
        self._family_details.setLineWrapMode(QTextEdit.NoWrap)
        detail_layout.addWidget(self._family_details, 1)
        bottom.addWidget(detail_panel)

        bottom.setStretchFactor(0, 4)
        bottom.setStretchFactor(1, 5)
        bottom.setSizes([420, 520])
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([420, 300])
        layout.addWidget(splitter, 1)
        return page

    def set_vfs(self, vfs: VfsManager | None) -> None:
        self._vfs = vfs
        self._ui_entry_map = {}
        if vfs is None:
            self._workbench = None
            self._characters = []
            self._items = []
            self._families = []
            self._family_to_records = {}
            self._family_profiles_by_code = {}
            self._loaded_signature = ""
            self._status_label.setText("Workbench idle")
            self._summary_label.setText("Load game data to build live character and item navigation.")
            self._char_model.set_records([])
            self._item_model.set_items([])
            self._family_model.set_profiles([], {})
            return
        signature = self._current_signature()
        if self._workbench is not None and self._loaded_signature == signature:
            self._load_ui_entry_map()
            self._status_label.setText("Workbench ready")
            return
        self._start_load()

    def _reload(self) -> None:
        if self._vfs is not None:
            self._start_load()

    def _start_load(self) -> None:
        if self._vfs is None or self._building:
            return
        packages_path = getattr(self._vfs, "_packages_path", "")
        if not packages_path:
            self._status_label.setText("Workbench unavailable: missing packages path")
            return

        self._building = True
        self._reload_btn.setEnabled(False)
        self._status_label.setText("Building live workbench...")
        self._summary_label.setText("Scanning live game character, family, and item data...")

        cached_vfs = VfsManager(packages_path)
        try:
            cached_vfs._pamt_cache = dict(getattr(self._vfs, "_pamt_cache", {}))
        except Exception:
            pass

        self._worker = FunctionWorker(lambda worker, vfs=cached_vfs: build_character_workbench_from_vfs(worker, vfs))
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_result.connect(self._on_loaded)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self._status_label.setText(f"{percent}% - {message}")

    def _on_error(self, message: str) -> None:
        self._building = False
        self._reload_btn.setEnabled(True)
        self._status_label.setText(f"Workbench error: {message}")
        self._summary_label.setText("Workbench build failed")

    def _on_loaded(self, workbench: CharacterWorkbenchData) -> None:
        self._building = False
        self._reload_btn.setEnabled(True)
        self._workbench = workbench
        self._loaded_signature = self._current_signature()
        self._characters = workbench.characters.records
        self._items = workbench.items
        self._families = workbench.families
        self._family_to_records = defaultdict(list)
        for record in self._characters:
            self._family_to_records[record.family_code].append(record)
        self._family_to_records = dict(self._family_to_records)
        self._family_profiles_by_code = {profile.family_code: profile for profile in self._families}

        self._populate_family_combo()
        self._populate_slot_combo()
        self._char_model.set_records(self._characters)
        self._item_model.set_items(self._items)
        self._family_model.set_profiles(self._families, self._family_to_records)
        self._apply_character_filters()
        self._apply_item_filters()
        self._apply_family_filters()
        self._load_ui_entry_map()

        named = sum(1 for record in self._characters if record.name_source != "identity")
        with_images = sum(1 for record in self._characters if any(media.media_type == "Image" for media in record.media))
        self._summary_label.setText(
            f"{len(self._characters):,} characters | {len(self._families):,} families | "
            f"{len(self._items):,} items | {named:,} named | {with_images:,} with images"
        )
        self._status_label.setText("Workbench ready")

    def _current_signature(self) -> str:
        if self._vfs is None:
            return ""
        return getattr(self._vfs, "_packages_path", "")

    def _populate_family_combo(self) -> None:
        current = self._char_family_combo.currentData()
        self._char_family_combo.blockSignals(True)
        self._char_family_combo.clear()
        self._char_family_combo.addItem("All Families", "All Families")
        for profile in self._families:
            self._char_family_combo.addItem(profile.label, profile.family_code)
        if current:
            index = self._char_family_combo.findData(current)
            if index >= 0:
                self._char_family_combo.setCurrentIndex(index)
        self._char_family_combo.blockSignals(False)

    def _populate_slot_combo(self) -> None:
        current = self._char_slot_combo.currentText()
        slots = sorted({slot for record in self._characters for slot in record.slots})
        self._char_slot_combo.blockSignals(True)
        self._char_slot_combo.clear()
        self._char_slot_combo.addItem("All Parts")
        for slot in slots:
            self._char_slot_combo.addItem(slot)
        index = self._char_slot_combo.findText(current)
        if index >= 0:
            self._char_slot_combo.setCurrentIndex(index)
        self._char_slot_combo.blockSignals(False)

    def _load_ui_entry_map(self) -> None:
        self._ui_entry_map = {}
        if self._vfs is None:
            return
        try:
            pamt = self._vfs.load_pamt("0012")
        except Exception:
            return
        self._ui_entry_map = {
            entry.path.replace("\\", "/").lower(): entry
            for entry in pamt.file_entries
        }

    def _on_char_scope_changed(self, scope: str) -> None:
        self._char_gender_combo.setEnabled(scope == "Human Only")
        if scope != "Human Only" and self._char_gender_combo.currentText() != "All":
            self._char_gender_combo.setCurrentText("All")
        self._apply_character_filters()

    def _apply_character_filters(self) -> None:
        family = self._char_family_combo.currentData() or "All Families"
        self._char_model.set_filters(
            search=self._char_search.text(),
            scope=self._char_scope_combo.currentText(),
            gender=self._char_gender_combo.currentText(),
            family=family,
            slot=self._char_slot_combo.currentText(),
            status=self._char_status_combo.currentText(),
        )
        self._status_label.setText(f"Characters: {self._char_model.filtered_count:,} visible")

    def _apply_item_filters(self) -> None:
        self._item_model.set_filters(
            search=self._item_search.text(),
            top=self._item_top_combo.currentText(),
            wearable=self._item_wearable_combo.currentText(),
            confidence=self._item_confidence_combo.currentText(),
            selected_record=self._selected_character(),
        )
        self._status_label.setText(
            f"Characters: {self._char_model.filtered_count:,} | Items: {self._item_model.filtered_count:,} visible"
        )

    def _apply_family_filters(self) -> None:
        self._family_model.set_filters(
            search=self._family_search.text(),
            human_only=self._family_human_combo.currentText() == "Human Families",
            named_only=self._family_named_combo.currentText() == "Named Families",
        )
        self._status_label.setText(
            f"Characters: {self._char_model.filtered_count:,} | Families: {self._family_model.filtered_count:,} visible"
        )

    def _selected_character(self) -> CharacterRecord | None:
        index = self._char_table.currentIndex()
        return self._char_model.row_at(index.row()) if index.isValid() else None

    def _selected_item(self) -> WorkbenchItemRecord | None:
        index = self._item_table.currentIndex()
        return self._item_model.row_at(index.row()) if index.isValid() else None

    def _selected_family(self) -> FamilyProfile | None:
        index = self._family_table.currentIndex()
        return self._family_model.row_at(index.row()) if index.isValid() else None

    @staticmethod
    def _name_confidence(name_source: str) -> str:
        if name_source in {"meshparam", "customization"}:
            return "High"
        if name_source in {"decoration", "identity_phrase"}:
            return "Medium"
        return "Low"

    def _format_character_details(self, record: CharacterRecord) -> str:
        aliases = ", ".join(record.aliases) if record.aliases else "-"
        slot_lines = [f"  - {slot}: {', '.join(names)}" for slot, names in record.slots.items()] or ["  - none"]
        resolved_files = sum(1 for linked in record.files if linked.resolved)
        unresolved_files = len(record.files) - resolved_files
        mesh_count = sum(1 for linked in record.files if linked.kind == "Mesh")
        image_count = sum(1 for media in record.media if media.media_type == "Image")
        video_count = sum(1 for media in record.media if media.media_type == "Video")
        compatible_items = [
            item
            for item in self._items
            if item.top_category == "Equipment"
            and (record.display_name in item.direct_name_matches or record.family_code in item.family_codes)
        ]
        weapon_count = sum(1 for item in compatible_items if item.category == "Weapon")
        armor_count = sum(1 for item in compatible_items if item.category == "Armor")
        accessory_count = sum(1 for item in compatible_items if item.category == "Accessory")
        return "\n".join(
            [
                f"Name: {record.display_name}",
                f"App ID: {record.app_id}",
                f"App Path: {record.app_path}",
                f"Name Source: {record.name_source}",
                f"Name Confidence: {self._name_confidence(record.name_source)}",
                f"Family Code: {record.family_code}",
                f"Gender: {record.gender}",
                f"Likely Human: {'Yes' if record.likely_human else 'No'}",
                f"Identity: {record.identity}",
                f"Variant: {record.variant or '-'}",
                f"Aliases: {aliases}",
                f"Customization File: {record.customization_file or '-'}",
                f"MeshParam File: {record.mesh_param_file or '-'}",
                f"Decoration Param File: {record.decoration_param_file or '-'}",
                "",
                f"Slots: {len(record.slots)}",
                *slot_lines,
                "",
                f"Resolved Files: {resolved_files}",
                f"Unresolved Files: {unresolved_files}",
                f"Mesh Files: {mesh_count}",
                f"Image Media: {image_count}",
                f"Video Media: {video_count}",
                "",
                f"Compatible Equipment: {len(compatible_items)}",
                f"Weapons: {weapon_count}",
                f"Armor: {armor_count}",
                f"Accessories: {accessory_count}",
            ]
        )

    def _compatible_records_for_item(self, item: WorkbenchItemRecord) -> list[CharacterRecord]:
        result: list[CharacterRecord] = []
        for record in self._characters:
            if record.display_name in item.direct_name_matches or record.family_code in item.family_codes:
                result.append(record)
        result.sort(key=lambda record: (record.display_name.lower(), record.app_id.lower()))
        return result

    def _family_label(self, family_code: str) -> str:
        profile = self._family_profiles_by_code.get(family_code)
        return profile.label if profile is not None else family_code

    def _format_item_details(self, item: WorkbenchItemRecord) -> str:
        compatible_records = self._compatible_records_for_item(item)
        family_lines = [f"  - {self._family_label(code)}" for code in item.family_codes] or ["  - none"]
        name_lines = [f"  - {name}" for name in item.direct_name_matches] or ["  - none"]
        character_lines = [
            f"  - {record.display_name} [{record.family_code}] ({record.app_id})"
            for record in compatible_records[:60]
        ] or ["  - none"]
        pac_lines = [f"  - {path}" for path in item.effective_pac_files[:40]] or ["  - none"]
        return "\n".join(
            [
                f"Item: {item.internal_name}",
                f"Top Category: {item.top_category}",
                f"Category: {item.category}",
                f"Subcategory: {item.subcategory}",
                f"Sub-Subcategory: {item.subsubcategory}",
                f"Raw Type: {item.raw_type}",
                f"Source: {item.source}",
                f"Item ID: {item.item_id if item.item_id is not None else '-'}",
                f"Localization Key: {item.loc_key or '-'}",
                f"Variant Base: {item.variant_base_name}",
                f"Variant Level: {item.variant_level if item.variant_level is not None else '-'}",
                f"Classification Confidence: {item.classification_confidence}",
                f"Compatibility Confidence: {item.compatibility_confidence}",
                f"Inherited Visuals: {'Yes' if item.inherited_visuals else 'No'}",
                f"Direct PAC Files: {len(item.pac_files)}",
                f"Effective PAC Files: {len(item.effective_pac_files)}",
                f"Item Icon Candidates: {len(item.icon_records)}",
                "",
                "Wearable Families:",
                *family_lines,
                "",
                "Named Matches:",
                *name_lines,
                "",
                "Compatible Characters:",
                *character_lines,
                "",
                "Effective Visual Files:",
                *pac_lines,
            ]
        )

    def _format_family_details(self, profile: FamilyProfile) -> str:
        records = sorted(
            self._family_to_records.get(profile.family_code, []),
            key=lambda item: (item.display_name.lower(), item.app_id.lower()),
        )
        image_count = sum(1 for record in records if any(media.media_type == "Image" for media in record.media))
        preview_count = sum(1 for record in records if self._char_model.has_preview(record))
        named_count = sum(1 for record in records if record.name_source != "identity")
        compatible_items = [
            item for item in self._items if item.top_category == "Equipment" and profile.family_code in item.family_codes
        ]
        top_categories: dict[str, int] = {}
        top_types: dict[str, int] = {}
        for item in compatible_items:
            top_categories[item.category] = top_categories.get(item.category, 0) + 1
            top_types[item.raw_type] = top_types.get(item.raw_type, 0) + 1
        category_lines = [
            f"  - {name}: {count:,}"
            for name, count in sorted(top_categories.items(), key=lambda pair: (-pair[1], pair[0].lower()))[:10]
        ] or ["  - none"]
        type_lines = [
            f"  - {name}: {count:,}"
            for name, count in sorted(top_types.items(), key=lambda pair: (-pair[1], pair[0].lower()))[:14]
        ] or ["  - none"]
        member_lines = [f"  - {record.display_name} ({record.app_id})" for record in records[:60]] or ["  - none"]
        return "\n".join(
            [
                f"Family Code: {profile.family_code}",
                f"Label: {profile.label}",
                f"Gender: {profile.gender}",
                f"Likely Human: {'Yes' if profile.likely_human else 'No'}",
                f"Character Count: {profile.character_count}",
                f"Named Characters: {named_count}",
                f"Records With Images: {image_count}",
                f"Records With Mesh Preview: {preview_count}",
                f"Compatible Equipment Items: {len(compatible_items)}",
                "",
                "Top Equipment Categories:",
                *category_lines,
                "",
                "Top Equipment Raw Types:",
                *type_lines,
                "",
                "Example Members:",
                *member_lines,
            ]
        )

    def _on_character_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        record = self._char_model.row_at(current.row()) if current.isValid() else None
        if record is None:
            self._char_details.clear()
            self._char_image_combo.clear()
            self._clear_image_label(self._char_image_view, "Select a character to inspect live portraits and UI media.")
            self._char_image_info.setText("Select a character to inspect live portraits and UI media.")
            self._apply_item_filters()
            return

        self._char_details.setPlainText(self._format_character_details(record))
        self._populate_character_image_sources(record)
        self._populate_character_scope_parts(record)
        self._apply_item_filters()
        self._emit_character_scope()

    def _populate_character_image_sources(self, record: CharacterRecord) -> None:
        images = [
            _UiImageRecord(path=media.path, label=f"{media.category} | score {media.score}", score=media.score)
            for media in record.media
            if media.media_type == "Image"
        ]
        self._char_image_combo.blockSignals(True)
        self._char_image_combo.clear()
        for image in images:
            self._char_image_combo.addItem(image.label, image)
        self._char_image_combo.blockSignals(False)
        if images:
            self._char_image_combo.setCurrentIndex(0)
            self._load_ui_image(images[0], self._char_image_view, self._char_image_info)
        else:
            self._clear_image_label(self._char_image_view, "No live UI image found for this character.")
            self._char_image_info.setText("No live UI image found for this character.")

    def _populate_character_scope_parts(self, record: CharacterRecord) -> None:
        current = self._char_scope_part_combo.currentText()
        self._char_scope_part_combo.blockSignals(True)
        self._char_scope_part_combo.clear()
        self._char_scope_part_combo.addItem("All Parts")
        for slot in record.slots:
            self._char_scope_part_combo.addItem(slot)
        index = self._char_scope_part_combo.findText(current)
        if index >= 0:
            self._char_scope_part_combo.setCurrentIndex(index)
        else:
            self._char_scope_part_combo.setCurrentText("All Parts")
        self._char_scope_part_combo.blockSignals(False)

    def _on_character_image_changed(self, index: int) -> None:
        image = self._char_image_combo.itemData(index)
        if image is not None:
            self._load_ui_image(image, self._char_image_view, self._char_image_info)

    def _load_ui_image(self, record: _UiImageRecord, target_label: _ZoomableImagePane, info_label: QLabel) -> None:
        if self._vfs is None:
            self._clear_image_label(target_label, "Game data is not loaded.")
            info_label.setText("Game data is not loaded.")
            return
        entry = self._ui_entry_map.get(record.path.lower())
        if entry is None:
            self._clear_image_label(target_label, f"Image file not found:\n{record.path}")
            info_label.setText(f"Missing UI image: {record.path}")
            return
        try:
            data = self._vfs.read_entry_data(entry)
            pixmap = self._pixmap_from_dds_bytes(data)
            if pixmap.isNull():
                raise ValueError("Unsupported or unreadable DDS image")
            info = read_dds_info(data)
            pixmap = self._crop_transparent_bounds(pixmap).scaled(360, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            target_label.set_pixmap(pixmap)
            info_label.setText(f"{record.path}\n{info.width}x{info.height} | {format_file_size(len(data))}")
        except Exception as exc:
            self._clear_image_label(target_label, f"Failed to load image:\n{exc}")
            info_label.setText(f"Failed to load image: {record.path}")

    @staticmethod
    def _clear_image_label(label: _ZoomableImagePane, text: str) -> None:
        label.clear_message(text)

    def _pixmap_from_dds_bytes(self, data: bytes) -> QPixmap:
        try:
            width, height, rgba = decode_dds_to_rgba(data)
            if len(rgba) < width * height * 4:
                return QPixmap()
            image = QImage(rgba, width, height, width * 4, QImage.Format_RGBA8888)
            if image.isNull():
                return QPixmap()
            return QPixmap.fromImage(image.copy())
        except Exception:
            pass

        try:
            from PIL import Image, ImageFile

            ImageFile.LOAD_TRUNCATED_IMAGES = True
            image = Image.open(BytesIO(data))
            image.load()
            image = image.convert("RGBA")
            raw = image.tobytes("raw", "RGBA")
            qimage = QImage(raw, image.width, image.height, image.width * 4, QImage.Format_RGBA8888)
            pixmap = QPixmap.fromImage(qimage.copy())
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            return pixmap
        except Exception:
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            except Exception:
                pass
            return QPixmap()

    def _crop_transparent_bounds(self, pixmap: QPixmap) -> QPixmap:
        if pixmap.isNull():
            return pixmap

        image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
        if image.isNull() or not image.hasAlphaChannel():
            return pixmap

        width = image.width()
        height = image.height()
        min_x = width
        min_y = height
        max_x = -1
        max_y = -1

        for y in range(height):
            for x in range(width):
                if image.pixelColor(x, y).alpha() > 0:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)

        if max_x < min_x or max_y < min_y:
            return pixmap

        crop_width = max_x - min_x + 1
        crop_height = max_y - min_y + 1
        if crop_width >= width and crop_height >= height:
            return pixmap

        crop_area = crop_width * crop_height
        full_area = width * height
        if crop_area / max(full_area, 1) > 0.9:
            return pixmap

        return pixmap.copy(min_x, min_y, crop_width, crop_height)

    def _character_scope_paths(self, record: CharacterRecord) -> tuple[list[str], str, str]:
        mode = self._char_scope_mode_combo.currentText()
        selected_part = self._char_scope_part_combo.currentText()
        paths: list[str] = []

        def add(path: str) -> None:
            normalized = path.replace("\\", "/")
            if normalized not in paths:
                paths.append(normalized)

        for linked in record.files:
            if not linked.resolved:
                continue
            if mode == "Meshes Only" and linked.kind != "Mesh":
                continue
            if mode == "Selected Part":
                if selected_part == "All Parts":
                    continue
                if linked.slot != selected_part:
                    continue
            add(linked.path)

        if mode in {"All Related", "Media Only"}:
            if mode == "Media Only":
                paths = []
            for media in record.media:
                add(media.path)

        preferred_path = next(
            (
                linked.path.replace("\\", "/")
                for linked in record.files
                if linked.resolved and linked.kind == "Mesh"
            ),
            paths[0] if paths else "",
        )
        title = record.display_name
        if mode == "Selected Part" and selected_part != "All Parts":
            title = f"{record.display_name} | {selected_part}"
        elif mode != "All Related":
            title = f"{record.display_name} | {mode}"
        return paths, preferred_path, title

    def _emit_character_scope(self) -> None:
        record = self._selected_character()
        if record is None:
            return
        paths, preferred_path, title = self._character_scope_paths(record)
        if paths:
            self.scope_requested.emit(paths, preferred_path, title)

    def _on_item_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        item = self._item_model.row_at(current.row()) if current.isValid() else None
        if item is None:
            self._item_details.clear()
            self._item_icon_combo.clear()
            self._clear_image_label(self._item_icon_view, "Select an item to inspect live icon candidates.")
            self._item_icon_info.setText("Select an item to inspect live icon candidates.")
            return

        self._item_details.setPlainText(self._format_item_details(item))
        self._populate_item_icon_sources(item)
        self._emit_item_scope()

    def _populate_item_icon_sources(self, item: WorkbenchItemRecord) -> None:
        icons = [
            _UiImageRecord(path=icon.path, label=f"{icon.match_key} | score {icon.score}", score=icon.score)
            for icon in item.icon_records
        ]
        self._item_icon_combo.blockSignals(True)
        self._item_icon_combo.clear()
        for icon in icons:
            self._item_icon_combo.addItem(icon.label, icon)
        self._item_icon_combo.blockSignals(False)
        if icons:
            self._item_icon_combo.setCurrentIndex(0)
            self._load_ui_image(icons[0], self._item_icon_view, self._item_icon_info)
        else:
            self._clear_image_label(self._item_icon_view, "No live icon candidate found for this item.")
            self._item_icon_info.setText("No live icon candidate found for this item.")

    def _on_item_icon_changed(self, index: int) -> None:
        icon = self._item_icon_combo.itemData(index)
        if icon is not None:
            self._load_ui_image(icon, self._item_icon_view, self._item_icon_info)

    def _item_scope_paths(self, item: WorkbenchItemRecord) -> tuple[list[str], str, str]:
        mode = self._item_scope_mode_combo.currentText()
        paths: list[str] = []
        for path in item.effective_pac_files:
            normalized = path.replace("\\", "/")
            if normalized not in paths:
                paths.append(normalized)
        if mode in {"All Related", "Icons Only"}:
            if mode == "Icons Only":
                paths = []
            for icon in item.icon_records:
                normalized = icon.path.replace("\\", "/")
                if normalized not in paths:
                    paths.append(normalized)
        preferred_path = next((path for path in item.effective_pac_files), paths[0] if paths else "")
        title = item.internal_name if mode == "All Related" else f"{item.internal_name} | {mode}"
        return paths, preferred_path, title

    def _emit_item_scope(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        paths, preferred_path, title = self._item_scope_paths(item)
        if paths:
            self.scope_requested.emit(paths, preferred_path, title)

    def _on_family_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        profile = self._family_model.row_at(current.row()) if current.isValid() else None
        if profile is None:
            self._family_details.clear()
            self._clear_image_label(self._family_image_view, "Select a family to inspect its primary live image.")
            self._family_image_info.setText("Select a family to inspect its primary live image.")
            return

        self._family_details.setPlainText(self._format_family_details(profile))
        self._load_family_image(profile)
        self._emit_family_scope()

    def _load_family_image(self, profile: FamilyProfile) -> None:
        records = sorted(
            self._family_to_records.get(profile.family_code, []),
            key=lambda item: (item.name_source == "identity", item.display_name.lower(), item.app_id.lower()),
        )
        media = next(
            (
                _UiImageRecord(path=media.path, label=media.category, score=media.score)
                for record in records
                for media in record.media
                if media.media_type == "Image"
            ),
            None,
        )
        if media is None:
            self._clear_image_label(self._family_image_view, "No live family image found.")
            self._family_image_info.setText("No live family image found.")
            return
        self._load_ui_image(media, self._family_image_view, self._family_image_info)

    def _family_scope_paths(self, profile: FamilyProfile) -> tuple[list[str], str, str]:
        mode = self._family_scope_mode_combo.currentText()
        records = self._family_to_records.get(profile.family_code, [])
        paths: list[str] = []

        def add(path: str) -> None:
            normalized = path.replace("\\", "/")
            if normalized not in paths:
                paths.append(normalized)

        for record in records:
            if mode in {"Character Files", "Meshes Only"}:
                for linked in record.files:
                    if not linked.resolved:
                        continue
                    if mode == "Meshes Only" and linked.kind != "Mesh":
                        continue
                    add(linked.path)
            if mode in {"Character Files", "Media Only"}:
                for media in record.media:
                    add(media.path)

        preferred_path = next(
            (
                linked.path.replace("\\", "/")
                for record in records
                for linked in record.files
                if linked.resolved and linked.kind == "Mesh"
            ),
            paths[0] if paths else "",
        )
        title = profile.family_code if mode == "Character Files" else f"{profile.family_code} | {mode}"
        return paths, preferred_path, title

    def _emit_family_scope(self) -> None:
        profile = self._selected_family()
        if profile is None:
            return
        paths, preferred_path, title = self._family_scope_paths(profile)
        if paths:
            self.scope_requested.emit(paths, preferred_path, title)

    def _on_tab_changed(self, _index: int) -> None:
        if self._tabs.currentIndex() == 0:
            self._emit_character_scope()
        elif self._tabs.currentIndex() == 1:
            self._emit_item_scope()
        else:
            self._emit_family_scope()
