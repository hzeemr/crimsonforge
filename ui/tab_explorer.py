"""Enterprise Explorer tab - unified Unpack + Browse + Edit.

Three-panel layout:
  Left:   Archive file list (QTableView + QAbstractTableModel = virtual rows)
  Center: Preview pane (click archive file = instant preview from PAZ)
  Right:  Text editor (for editable files)

Performance: Archive list uses QAbstractTableModel — only visible rows
are rendered. Filtering 1.45M files is instant (data stays in Python
lists, Qt only asks for visible rows). No QTreeWidgetItem objects.
"""

import os
import tempfile
import time
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QSplitter, QTableView, QHeaderView,
    QAbstractItemView, QApplication, QCheckBox, QMenu,
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QAbstractTableModel, QModelIndex,
)
from PySide6.QtGui import QColor, QBrush, QKeySequence, QShortcut

from core.vfs_manager import VfsManager
from core.pamt_parser import PamtData, PamtFileEntry
from core.file_detector import detect_file_type, get_syntax_type, is_text_file
from core.item_index import build_item_index
from ui.widgets.preview_pane import PreviewPane
from ui.widgets.search_history_line_edit import SearchHistoryLineEdit
from ui.widgets.syntax_editor import SyntaxEditor
from ui.widgets.progress_widget import ProgressWidget
from ui.dialogs.file_picker import pick_directory, pick_file, pick_save_file
from ui.dialogs.explorer_workbench_dialog import ExplorerWorkbenchDialog
from ui.dialogs.confirmation import show_error, show_info, confirm_action
from utils.thread_worker import FunctionWorker
from utils.platform_utils import format_file_size
from utils.logger import get_logger

logger = get_logger("ui.tab_explorer")

ALL_PACKAGES = "All Packages"

FILE_TYPE_FILTERS = {
    "All Files": set(),
    "3D Meshes": {".pam", ".pamlod", ".pac", ".pab", ".pabc", ".pami", ".meshinfo"},
    "Textures": {".dds", ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp", ".gif"},
    "Audio": {".wav", ".ogg", ".mp3", ".wem", ".bnk", ".pasound", ".flac", ".aac"},
    "Video": {".mp4", ".webm", ".avi", ".mkv", ".bk2", ".bik", ".usm"},
    "Animation": {".paa", ".paa_metabin", ".hkx", ".motionblending"},
    "Localization": {".paloc"},
    "UI / Web": {".css", ".html", ".thtml", ".xml", ".json", ".uianiminit"},
    "Materials": {".mi", ".material", ".technique", ".impostor"},
    "Fonts": {".ttf", ".otf", ".woff", ".woff2"},
    "Effects": {".pae", ".paem"},
    "Level / World": {".palevel", ".levelinfo", ".prefab", ".nav", ".road", ".roadsector", ".roadidx"},
    "Game Data": {".pabgb", ".pabgh", ".binarygimmick", ".binarystring", ".questgaugecount"},
    "Sequencer": {".paseq", ".paseqc", ".paseqh", ".seqmt", ".pastage", ".paschedule", ".paschedulepath"},
    "Physics": {".pbd", ".pat"},
    "Shaders": {".padxil"},
    "Splines": {".spline", ".spline2d"},
}

_COL_FILE = 0
_COL_SIZE = 1
_COL_TYPE = 2
_COL_PKG = 3
_COL_COUNT = 4
_HEADERS = ["File", "Size", "Type", "Pkg"]
_ITEM_MODEL_SUFFIXES = (
    "_l", "_r", "_u", "_s", "_t",
    "_index01", "_index02", "_index03",
)
_ITEM_MODEL_PREFIXES = (
    "itemicon_prefab_",
    "itemicon_",
    "prefab_",
)


class _ArchiveRow:
    """Lightweight data holder for one archive file entry. No Qt objects.

    Extension and path_lower are pre-computed for instant filtering.
    type_desc is lazy-computed on first access (only visible rows need it).
    """
    __slots__ = ("entry", "group", "ext", "path_lower", "stem_lower", "size_raw",
                 "_size_str", "_type_desc", "checked", "search_extra")

    def __init__(self, entry: PamtFileEntry, group: str):
        self.entry = entry
        self.group = group
        self.path_lower = entry.path.lower()
        self.ext = os.path.splitext(self.path_lower)[1]
        self.stem_lower = os.path.splitext(os.path.basename(self.path_lower))[0]
        self.size_raw = entry.orig_size
        self._size_str = None
        self._type_desc = None
        self.checked = True
        self.search_extra = ""

    @property
    def size_str(self) -> str:
        if self._size_str is None:
            self._size_str = format_file_size(self.size_raw)
        return self._size_str

    @property
    def type_desc(self) -> str:
        if self._type_desc is None:
            self._type_desc = detect_file_type(self.entry.path).description
        return self._type_desc


class _ArchiveModel(QAbstractTableModel):
    """Virtual model for archive file list. Only visible rows are rendered.

    Holds ALL entries in _all_rows. Filtering produces _filtered which
    is just a list of indices into _all_rows. Zero copying, instant.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_rows: list[_ArchiveRow] = []
        self._filtered: list[int] = []
        self._ext_set: set = set()
        self._search_text = ""
        self._scoped_paths: set[str] | None = None

    def set_data(self, rows: list[_ArchiveRow]):
        self.beginResetModel()
        self._all_rows = rows
        self._refilter()
        self.endResetModel()

    def set_filter(self, ext_set: set, search: str):
        self.beginResetModel()
        self._ext_set = ext_set
        self._search_text = search.strip().lower()
        self._refilter()
        self.endResetModel()

    def set_scope_paths(self, paths: list[str] | set[str] | None):
        self.beginResetModel()
        if paths:
            self._scoped_paths = {path.replace("\\", "/").lower() for path in paths}
        else:
            self._scoped_paths = None
        self._refilter()
        self.endResetModel()

    def _refilter(self):
        import fnmatch as _fn
        ext_set = self._ext_set
        search = self._search_text
        scoped_paths = self._scoped_paths
        if not ext_set and not search and not scoped_paths:
            self._filtered = list(range(len(self._all_rows)))
            return

        # Parse search: support wildcards and ext: prefix
        search_ext = ""
        search_text = search
        if search.startswith("ext:"):
            # ext:.dds or ext:dds
            search_ext = search[4:].strip()
            if search_ext and not search_ext.startswith("."):
                search_ext = "." + search_ext
            search_text = ""
        elif search.startswith(".") and " " not in search and len(search) < 15:
            # Typing just ".dds" filters by extension
            search_ext = search
            search_text = ""

        use_glob = "*" in search_text or "?" in search_text

        result = []
        for i, row in enumerate(self._all_rows):
            if scoped_paths is not None and row.path_lower not in scoped_paths:
                continue
            if ext_set and row.ext not in ext_set:
                continue
            if search_ext and row.ext != search_ext:
                continue
            if search_text:
                if use_glob:
                    if not (
                        _fn.fnmatch(row.path_lower, search_text)
                        or (row.search_extra and _fn.fnmatch(row.search_extra, search_text))
                    ):
                        continue
                elif search_text not in row.path_lower and search_text not in row.search_extra:
                    continue
            result.append(i)
        self._filtered = result

    def row_at(self, view_row: int) -> _ArchiveRow:
        if 0 <= view_row < len(self._filtered):
            return self._all_rows[self._filtered[view_row]]
        return None

    def get_checked_entries(self) -> list[PamtFileEntry]:
        return [self._all_rows[i].entry for i in self._filtered
                if self._all_rows[i].checked]

    def check_all(self, checked: bool):
        for i in self._filtered:
            self._all_rows[i].checked = checked
        if self._filtered:
            tl = self.index(0, 0)
            br = self.index(len(self._filtered) - 1, 0)
            self.dataChanged.emit(tl, br, [Qt.CheckStateRole])

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return _COL_COUNT

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row_idx = index.row()
        if row_idx >= len(self._filtered):
            return None
        row = self._all_rows[self._filtered[row_idx]]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == _COL_FILE:
                return row.entry.path
            elif col == _COL_SIZE:
                return row.size_str
            elif col == _COL_TYPE:
                return row.type_desc
            elif col == _COL_PKG:
                return row.group

        elif role == Qt.CheckStateRole and col == _COL_FILE:
            return Qt.Checked if row.checked else Qt.Unchecked

        elif role == Qt.ToolTipRole and col == _COL_FILE:
            comp = "LZ4" if row.entry.compression_type == 2 else "None"
            enc = " + ChaCha20" if row.entry.encrypted else ""
            return (f"{row.entry.path}\n"
                    f"Size: {row.size_str} (orig: {row.entry.orig_size:,})\n"
                    f"Compression: {comp}{enc}\n"
                    f"Package: {row.group}")

        elif role == Qt.UserRole:
            return row

        elif role == Qt.UserRole + 1:
            return row.size_raw

        return None

    def setData(self, index, value, role=Qt.CheckStateRole):
        if role == Qt.CheckStateRole and index.column() == _COL_FILE:
            row = self._all_rows[self._filtered[index.row()]]
            row.checked = (value == Qt.Checked)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            return True
        return False

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.column() == _COL_FILE:
            base |= Qt.ItemIsUserCheckable
        return base

    def sort(self, column, order=Qt.AscendingOrder):
        self.beginResetModel()
        reverse = (order == Qt.DescendingOrder)
        all_rows = self._all_rows
        if column == _COL_FILE:
            self._filtered.sort(key=lambda i: all_rows[i].entry.path.lower(), reverse=reverse)
        elif column == _COL_SIZE:
            self._filtered.sort(key=lambda i: all_rows[i].size_raw, reverse=reverse)
        elif column == _COL_TYPE:
            self._filtered.sort(key=lambda i: all_rows[i].type_desc, reverse=reverse)
        elif column == _COL_PKG:
            self._filtered.sort(key=lambda i: all_rows[i].group, reverse=reverse)
        self.endResetModel()

    @property
    def filtered_count(self) -> int:
        return len(self._filtered)

    @property
    def total_count(self) -> int:
        return len(self._all_rows)


class ExplorerTab(QWidget):
    """Unified Unpack + Browse + Edit enterprise explorer.

    Archive list uses QAbstractTableModel — instant filtering of 1M+ files.
    Click any file to preview from PAZ (no extraction needed).
    """

    files_extracted = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._vfs: VfsManager = None
        self._all_groups: list[str] = []
        self._worker: FunctionWorker = None
        self._item_index = None
        self._current_edit_file = ""
        self._pending_mesh_data: dict[str, dict] = {}
        self._temp_dir = tempfile.mkdtemp(prefix="crimsonforge_preview_")
        self._last_preview_path = ""
        self._last_preview_time = 0.0
        self._active_scope_title = ""
        self._pending_scope_request: tuple[list[str], str, str] | None = None
        self._workbench_dialog: ExplorerWorkbenchDialog | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_filter)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Package:"))
        self._group_combo = QComboBox()
        self._group_combo.setToolTip("Select a package group to browse, or 'All Packages' to see everything.")
        self._group_combo.setMinimumWidth(160)
        self._group_combo.currentTextChanged.connect(self._on_group_changed)
        toolbar.addWidget(self._group_combo)

        toolbar.addWidget(QLabel("Type:"))
        self._type_filter = QComboBox()
        self._type_filter.setToolTip(
            "Filter files by type:\n"
            "  All Files — show everything\n"
            "  Localization — .paloc translation files\n"
            "  Stylesheets — .css theme files\n"
            "  HTML/Templates — .html, .thtml UI templates\n"
            "  XML/Config — .xml configuration files\n"
            "  Fonts — .ttf, .otf font files"
        )
        for name in FILE_TYPE_FILTERS:
            self._type_filter.addItem(name)
        self._type_filter.currentTextChanged.connect(lambda _: self._apply_filter())
        toolbar.addWidget(self._type_filter)

        toolbar.addWidget(QLabel("Search:"))
        self._search_input = SearchHistoryLineEdit(self._config, "explorer")
        self._search_input.setPlaceholderText(
            "Search files or item names: Vow of the Dead King, *.dds, ext:.pam..."
        )
        self._search_input.setToolTip(
            "Search files or item names by name or pattern:\n"
            "  Vow of the Dead King - related PAC/prefab/model files\n"
            "  sword         — files containing 'sword'\n"
            "  *.dds         — wildcard glob pattern\n"
            "  *armor*sword* — multiple wildcards\n"
            "  .pam          — filter by extension\n"
            "  ext:.dds      — explicit extension filter\n"
            "Case-insensitive."
        )
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        toolbar.addWidget(self._search_input, 1)

        self._navigator_btn = QPushButton("Navigator")
        self._navigator_btn.setObjectName("primary")
        self._navigator_btn.setToolTip(
            "Open the live character/item/family navigator in a popup.\n"
            "Use it to filter Explorer to related files while keeping all normal Explorer actions."
        )
        self._navigator_btn.clicked.connect(self._open_workbench_dialog)
        toolbar.addWidget(self._navigator_btn)

        toolbar.addWidget(QLabel("Output:"))
        self._output_path = QLineEdit(self._config.get("general.last_output_path", ""))
        self._output_path.setPlaceholderText("Extraction output...")
        self._output_path.setToolTip("Directory where extracted files will be saved.\nFiles are placed in subdirectories matching the game's folder structure.")
        toolbar.addWidget(self._output_path, 1)
        out_browse = QPushButton("...")
        out_browse.setFixedWidth(30)
        out_browse.setToolTip("Browse for an output directory.")
        out_browse.clicked.connect(self._browse_output)
        toolbar.addWidget(out_browse)

        self._extract_sel_btn = QPushButton("Extract Selected")
        self._extract_sel_btn.setObjectName("primary")
        self._extract_sel_btn.setToolTip("Extract only the checked files to the output directory.\nFiles are automatically decrypted and decompressed.")
        self._extract_sel_btn.clicked.connect(self._extract_selected)
        toolbar.addWidget(self._extract_sel_btn)
        self._extract_all_btn = QPushButton("Extract All")
        self._extract_all_btn.setToolTip("Extract all visible (filtered) files to the output directory.")
        self._extract_all_btn.clicked.connect(self._extract_all)
        toolbar.addWidget(self._extract_all_btn)
        self._ship_mesh_btn = QPushButton("Ship to App")
        self._ship_mesh_btn.setObjectName("primary")
        self._ship_mesh_btn.setToolTip(
            "Generate a standalone ZIP installer for selected mesh mods.\n"
            "Select one or more .pac, .pam, or .pamlod rows, assign edited OBJ files,\n"
            "and package the patched PAZ/PAMT/PAPGT files for end users."
        )
        self._ship_mesh_btn.clicked.connect(self._ship_selected_meshes)
        toolbar.addWidget(self._ship_mesh_btn)
        layout.addLayout(toolbar)

        main_splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        arch_header = QHBoxLayout()
        arch_label = QLabel("Archive Contents")
        arch_label.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px;")
        arch_header.addWidget(arch_label)
        self._scope_label = QLabel("")
        self._scope_label.setStyleSheet("color: #f9e2af; font-weight: 600; padding: 0 4px;")
        self._scope_label.setVisible(False)
        arch_header.addWidget(self._scope_label)
        arch_header.addStretch()
        self._clear_scope_btn = QPushButton("Clear Scope")
        self._clear_scope_btn.setToolTip("Clear the current character/item/family scope and show all matching files again.")
        self._clear_scope_btn.setVisible(False)
        self._clear_scope_btn.clicked.connect(self._clear_workbench_scope)
        arch_header.addWidget(self._clear_scope_btn)
        sel_all_btn = QPushButton("Select All")
        sel_all_btn.setObjectName("primary")
        sel_all_btn.setToolTip("Check all visible files for extraction.")
        sel_all_btn.clicked.connect(lambda: self._model.check_all(True))
        arch_header.addWidget(sel_all_btn)
        desel_btn = QPushButton("Deselect All")
        desel_btn.setToolTip("Uncheck all files.")
        desel_btn.clicked.connect(lambda: self._model.check_all(False))
        arch_header.addWidget(desel_btn)
        self._archive_count = QLabel("0 files")
        self._archive_count.setStyleSheet("color: #89b4fa; font-weight: 600; padding: 0 4px;")
        arch_header.addWidget(self._archive_count)
        left_layout.addLayout(arch_header)

        self._model = _ArchiveModel(self)
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._view.setAlternatingRowColors(True)
        self._view.setSortingEnabled(True)
        self._view.setShowGrid(False)
        self._view.verticalHeader().setVisible(False)
        self._view.verticalHeader().setDefaultSectionSize(24)
        self._view.horizontalHeader().setSectionResizeMode(_COL_FILE, QHeaderView.Stretch)
        self._view.horizontalHeader().setSectionResizeMode(_COL_SIZE, QHeaderView.Fixed)
        self._view.horizontalHeader().setSectionResizeMode(_COL_TYPE, QHeaderView.Fixed)
        self._view.horizontalHeader().setSectionResizeMode(_COL_PKG, QHeaderView.Fixed)
        self._view.setColumnWidth(_COL_SIZE, 70)
        self._view.setColumnWidth(_COL_TYPE, 100)
        self._view.setColumnWidth(_COL_PKG, 50)
        self._view.clicked.connect(self._on_archive_clicked)
        self._view.doubleClicked.connect(self._on_archive_double_clicked)
        self._view.selectionModel().currentRowChanged.connect(self._on_archive_row_changed)
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        # Space bar toggles check state of all selected rows
        space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self._view)
        space_shortcut.activated.connect(self._toggle_selected_checks)
        left_layout.addWidget(self._view, 1)
        main_splitter.addWidget(left_panel)

        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(2)

        preview_label = QLabel("Preview")
        preview_label.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px;")
        center_layout.addWidget(preview_label)

        self._preview = PreviewPane()
        center_layout.addWidget(self._preview, 1)
        main_splitter.addWidget(center_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)

        edit_header = QHBoxLayout()
        self._edit_file_label = QLabel("Editor")
        self._edit_file_label.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px;")
        edit_header.addWidget(self._edit_file_label, 1)
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("primary")
        self._save_btn.setToolTip("Save the edited file back to its extracted location on disk.")
        self._save_btn.clicked.connect(self._save_file)
        edit_header.addWidget(self._save_btn)
        save_as_btn = QPushButton("Save As...")
        save_as_btn.setToolTip("Save the edited file with a new name or location.")
        save_as_btn.clicked.connect(self._save_as)
        edit_header.addWidget(save_as_btn)
        right_layout.addLayout(edit_header)

        self._editor = SyntaxEditor()
        right_layout.addWidget(self._editor, 1)

        edit_footer = QHBoxLayout()
        edit_footer.addWidget(QLabel("Syntax:"))
        self._syntax_combo = QComboBox()
        self._syntax_combo.setToolTip("Select syntax highlighting mode for the editor.")
        self._syntax_combo.addItems(["plain", "css", "html", "xml", "json", "paloc"])
        self._syntax_combo.currentTextChanged.connect(lambda s: self._editor.set_syntax(s))
        edit_footer.addWidget(self._syntax_combo)
        edit_footer.addWidget(QLabel("Enc:"))
        self._encoding_combo = QComboBox()
        self._encoding_combo.setToolTip("Character encoding used to read and write the file.\nMost game files use UTF-8. Use UTF-16 for some Asian language files.")
        self._encoding_combo.addItems(["utf-8", "utf-16", "latin-1", "ascii"])
        edit_footer.addWidget(self._encoding_combo)
        self._edit_status = QLabel("Ready")
        edit_footer.addStretch()
        edit_footer.addWidget(self._edit_status)
        right_layout.addLayout(edit_footer)
        main_splitter.addWidget(right_panel)

        main_splitter.setSizes([420, 400, 380])
        layout.addWidget(main_splitter, 1)

        self._progress = ProgressWidget()
        layout.addWidget(self._progress)

    def initialize_from_game(self, vfs: VfsManager, groups: list[str]) -> None:
        self._vfs = vfs
        self._all_groups = groups
        self._item_index = None
        self._pending_scope_request = None
        self._active_scope_title = ""
        self._model.set_scope_paths(None)
        self._update_scope_ui()
        self._load_item_index()
        self._group_combo.blockSignals(True)
        self._group_combo.clear()
        self._group_combo.addItem(ALL_PACKAGES)
        self._group_combo.addItems(groups)
        self._group_combo.blockSignals(False)
        self._group_combo.setCurrentText(ALL_PACKAGES)
        self._on_group_changed(ALL_PACKAGES)
        self._ensure_workbench_dialog().workbench.set_vfs(self._vfs)
        self._progress.set_status(f"Game loaded: {len(groups)} package groups")

    def _load_item_index(self) -> None:
        """Build the item-name search index from live game data."""
        if not self._vfs:
            return

        def _progress(message: str) -> None:
            self._progress.set_status(f"Building item search index... {message}")
            QApplication.processEvents()

        try:
            self._item_index = build_item_index(self._vfs, progress_fn=_progress)
            if self._item_index:
                self._progress.set_status(
                    f"Item search ready: {len(self._item_index.items):,} items"
                )
        except Exception as e:
            self._item_index = None
            logger.warning("Item search index unavailable: %s", e)
            self._progress.set_status(f"Item search unavailable: {e}")

    def _build_row(self, entry: PamtFileEntry, group: str) -> _ArchiveRow:
        """Create one archive row and attach item-name aliases when available."""
        row = _ArchiveRow(entry, group)
        if not self._item_index or not self._item_index.model_base_aliases:
            return row

        aliases = []
        seen = set()
        for key in self._candidate_item_alias_keys(row.stem_lower):
            alias = self._item_index.model_base_aliases.get(key)
            if alias and alias not in seen:
                seen.add(alias)
                aliases.append(alias)
        if aliases:
            row.search_extra = " ".join(aliases)
        return row

    def _candidate_item_alias_keys(self, stem_lower: str) -> list[str]:
        """Return normalized model keys that may map this row to an item name."""
        keys = []
        seen = set()

        def _add(value: str) -> None:
            if value and value not in seen:
                seen.add(value)
                keys.append(value)

        _add(stem_lower)

        for prefix in _ITEM_MODEL_PREFIXES:
            if stem_lower.startswith(prefix):
                _add(stem_lower[len(prefix):])

        snapshot = list(keys)
        for key in snapshot:
            for suffix in _ITEM_MODEL_SUFFIXES:
                if key.endswith(suffix) and len(key) > len(suffix) + 4:
                    _add(key[:-len(suffix)])

        return keys

    def _browse_output(self):
        path = pick_directory(self, "Select Output Directory")
        if path:
            self._output_path.setText(path)

    def _on_group_changed(self, group: str):
        if not self._vfs or not group:
            return
        try:
            if group == ALL_PACKAGES:
                self._load_all_packages()
            else:
                pamt = self._vfs.load_pamt(group)
                rows = [self._build_row(e, group) for e in pamt.file_entries]
                self._model.set_data(rows)
            self._apply_filter()
            if group == ALL_PACKAGES and self._pending_scope_request:
                paths, preferred_path, title = self._pending_scope_request
                self._pending_scope_request = None
                self._apply_workbench_scope(paths, preferred_path, title)
        except Exception as e:
            show_error(self, "Load Error", str(e))

    def _load_all_packages(self):
        self._progress.set_progress(0, "Loading all packages...")
        QApplication.processEvents()
        all_rows = []
        for i, group in enumerate(self._all_groups):
            try:
                pamt = self._vfs.load_pamt(group)
                for entry in pamt.file_entries:
                    all_rows.append(self._build_row(entry, group))
            except Exception as e:
                logger.warning("Error loading group %s: %s", group, e)
            if (i + 1) % 5 == 0:
                pct = int(((i + 1) / len(self._all_groups)) * 100)
                self._progress.set_progress(pct, f"Loading group {group}...")
                QApplication.processEvents()
        self._model.set_data(all_rows)
        self._progress.set_progress(100, f"Loaded {len(all_rows):,} files from {len(self._all_groups)} packages")

    def _apply_filter(self):
        filter_name = self._type_filter.currentText()
        ext_set = FILE_TYPE_FILTERS.get(filter_name, set())
        search = self._search_input.text()
        self._model.set_filter(ext_set, search)
        fc = self._model.filtered_count
        tc = self._model.total_count
        self._archive_count.setText(f"{fc:,} / {tc:,} files")
        self._update_scope_ui()

    def _update_scope_ui(self):
        scoped_paths = self._model._scoped_paths
        if scoped_paths:
            title = self._active_scope_title or "Workbench Scope"
            self._scope_label.setText(f"Scope: {title} ({len(scoped_paths):,})")
            self._scope_label.setVisible(True)
            self._clear_scope_btn.setVisible(True)
        else:
            self._scope_label.clear()
            self._scope_label.setVisible(False)
            self._clear_scope_btn.setVisible(False)

    def _ensure_workbench_dialog(self) -> ExplorerWorkbenchDialog:
        if self._workbench_dialog is None:
            self._workbench_dialog = ExplorerWorkbenchDialog(self._config, self)
            self._workbench_dialog.workbench.scope_requested.connect(self._apply_workbench_scope)
            self._workbench_dialog.workbench.clear_scope_requested.connect(self._clear_workbench_scope)
            if self._vfs is not None:
                self._workbench_dialog.workbench.set_vfs(self._vfs)
        return self._workbench_dialog

    def _open_workbench_dialog(self):
        if not self._vfs:
            show_error(self, "Explorer Navigator", "Load the game data first.")
            return
        dialog = self._ensure_workbench_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_workbench_scope(self, paths: list[str], preferred_path: str = "", title: str = ""):
        normalized = []
        seen = set()
        for path in paths:
            norm = path.replace("\\", "/")
            lower = norm.lower()
            if lower in seen:
                continue
            seen.add(lower)
            normalized.append(norm)
        if not normalized:
            self._clear_workbench_scope()
            return

        self._active_scope_title = title or "Workbench Scope"
        if self._group_combo.currentText() != ALL_PACKAGES:
            self._pending_scope_request = (normalized, preferred_path, self._active_scope_title)
            self._group_combo.setCurrentText(ALL_PACKAGES)
            return

        self._pending_scope_request = None
        self._model.set_scope_paths(normalized)
        self._apply_filter()
        self._select_entry_path(preferred_path)

    def _clear_workbench_scope(self):
        self._active_scope_title = ""
        self._pending_scope_request = None
        self._model.set_scope_paths(None)
        self._apply_filter()

    def _select_entry_path(self, preferred_path: str):
        if not preferred_path:
            return
        preferred_lower = preferred_path.replace("\\", "/").lower()
        for row in range(self._model.rowCount()):
            row_data = self._model.row_at(row)
            if row_data and row_data.entry.path.lower() == preferred_lower:
                index = self._model.index(row, _COL_FILE)
                self._view.setCurrentIndex(index)
                self._view.selectRow(row)
                self._view.scrollTo(index, QTableView.PositionAtCenter)
                return

    def _get_selected_rows(self) -> list[int]:
        """Return sorted list of unique selected view row indices."""
        return sorted({idx.row() for idx in self._view.selectedIndexes()})

    def _toggle_selected_checks(self):
        """Space: toggle check state of all currently selected rows."""
        rows = self._get_selected_rows()
        if not rows:
            return
        # Determine new state: if any selected row is unchecked, check all; else uncheck all
        any_unchecked = any(
            not (self._model.row_at(r).checked) for r in rows if self._model.row_at(r)
        )
        new_state = Qt.Checked if any_unchecked else Qt.Unchecked
        for r in rows:
            self._model.setData(self._model.index(r, _COL_FILE), new_state, Qt.CheckStateRole)

    def _show_context_menu(self, pos):
        rows = self._get_selected_rows()
        menu = QMenu(self)
        if rows:
            check_act = menu.addAction(f"Check {len(rows)} selected")
            check_act.triggered.connect(lambda: [
                self._model.setData(self._model.index(r, _COL_FILE), Qt.Checked, Qt.CheckStateRole)
                for r in rows
            ])
            uncheck_act = menu.addAction(f"Uncheck {len(rows)} selected")
            uncheck_act.triggered.connect(lambda: [
                self._model.setData(self._model.index(r, _COL_FILE), Qt.Unchecked, Qt.CheckStateRole)
                for r in rows
            ])
            menu.addSeparator()
        menu.addAction("Check All").triggered.connect(lambda: self._model.check_all(True))
        menu.addAction("Uncheck All").triggered.connect(lambda: self._model.check_all(False))

        # Mesh export options for .pam/.pamlod/.pac files
        # Use the row at the right-click position, not the selection
        click_index = self._view.indexAt(pos)
        if click_index.isValid():
            click_row_data = self._model.row_at(click_index.row())
            if click_row_data:
                from core.mesh_parser import is_mesh_file
                if is_mesh_file(click_row_data.entry.path):
                    menu.addSeparator()
                    entry = click_row_data.entry
                    export_obj_act = menu.addAction("Export as OBJ")
                    export_obj_act.triggered.connect(lambda _=False, e=entry: self._export_mesh(e, "obj"))
                    export_fbx_act = menu.addAction("Export as FBX")
                    export_fbx_act.triggered.connect(lambda _=False, e=entry: self._export_mesh(e, "fbx"))
                    menu.addSeparator()
                    import_act = menu.addAction("Import OBJ (preview rebuilt mesh)")
                    import_act.triggered.connect(lambda _=False, e=entry: self._import_mesh(e))
                    patch_act = menu.addAction("Import OBJ + Patch to Game")
                    patch_act.triggered.connect(lambda _=False, e=entry: self._import_and_patch_mesh(e))
                    ship_act = menu.addAction("Import OBJ + Ship to App")
                    ship_act.triggered.connect(lambda _=False, e=entry: self._ship_single_mesh(e))

        # Audio export/import for audio files
        if click_index.isValid():
            click_row_data = self._model.row_at(click_index.row())
            if click_row_data:
                audio_exts = {".wem", ".bnk", ".wav", ".ogg", ".mp3", ".pasound"}
                file_ext = os.path.splitext(click_row_data.entry.path.lower())[1]
                if file_ext in audio_exts:
                    menu.addSeparator()
                    entry = click_row_data.entry
                    exp_wav = menu.addAction("Export as WAV")
                    exp_wav.triggered.connect(lambda _=False, e=entry: self._export_audio_wav(e))
                    imp_wav = menu.addAction("Import WAV + Patch to Game")
                    imp_wav.triggered.connect(lambda _=False, e=entry: self._import_audio_patch(e))

        menu.exec(self._view.viewport().mapToGlobal(pos))

    def _export_audio_wav(self, entry: PamtFileEntry):
        """Export an audio file as WAV."""
        try:
            from core.audio_converter import wem_to_wav
            data = self._vfs.read_entry_data(entry)
            basename = os.path.splitext(os.path.basename(entry.path))[0]
            temp = os.path.join(self._temp_dir, os.path.basename(entry.path))
            with open(temp, "wb") as f:
                f.write(data)

            save_path = pick_save_file(self, "Export as WAV", f"{basename}.wav",
                                       filters="WAV Files (*.wav)")
            if not save_path:
                return

            ext = os.path.splitext(entry.path)[1].lower()
            if ext in (".wem", ".bnk"):
                result = wem_to_wav(temp, save_path)
                if not result:
                    show_error(self, "Export Error", "WEM to WAV conversion failed")
                    return
            else:
                import shutil
                shutil.copy2(temp, save_path)

            self._progress.set_status(f"Exported WAV: {save_path}")
            show_info(self, "Export Complete", f"Exported to:\n{save_path}")
        except Exception as e:
            show_error(self, "Export Error", str(e))

    def _import_audio_patch(self, entry: PamtFileEntry):
        """Import a WAV and patch to game."""
        wav_path = pick_file(self, "Select WAV File",
                             filters="Audio Files (*.wav *.ogg *.mp3);;All Files (*.*)")
        if not wav_path:
            return
        try:
            from core.audio_importer import import_audio
            original_data = self._vfs.read_entry_data(entry)
            new_data = import_audio(wav_path, entry, original_data)

            if not confirm_action(self, "Patch Audio",
                                  f"Replace {entry.path}?\n\n"
                                  f"Original: {format_file_size(len(original_data))}\n"
                                  f"New: {format_file_size(len(new_data))}"):
                return

            from core.repack_engine import RepackEngine, ModifiedFile
            game_path = os.path.dirname(os.path.dirname(entry.paz_file))
            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            paz_dir = os.path.basename(os.path.dirname(entry.paz_file))
            pamt_data = self._vfs.load_pamt(paz_dir)

            mod_file = ModifiedFile(
                data=new_data, entry=entry,
                pamt_data=pamt_data, package_group=paz_dir,
            )
            engine = RepackEngine(game_path)
            result = engine.repack([mod_file], papgt_path=papgt_path)

            if result.success:
                self._progress.set_status(f"Audio patched: {entry.path}")
                show_info(self, "Patch Complete", f"Patched {entry.path}")
            else:
                show_error(self, "Patch Error", f"Failed: {result.error}")
        except Exception as e:
            show_error(self, "Patch Error", str(e))

    def _on_archive_row_changed(self, current: QModelIndex, _previous: QModelIndex):
        """Preview when arrow keys change the current row."""
        if not current.isValid():
            return
        row = self._model.row_at(current.row())
        if row:
            self._preview_from_archive(row.entry)

    def _on_archive_clicked(self, index: QModelIndex):
        row = self._model.row_at(index.row())
        if not row:
            return
        self._preview_from_archive(row.entry)

    def _on_archive_double_clicked(self, index: QModelIndex):
        row = self._model.row_at(index.row())
        if not row:
            return
        if is_text_file(row.entry.path):
            self._open_archive_in_editor(row.entry)
        else:
            self._preview_from_archive(row.entry)

    def _preview_from_archive(self, entry: PamtFileEntry):
        try:
            now = time.monotonic()
            if entry.path == self._last_preview_path and (now - self._last_preview_time) < 0.25:
                return

            self._progress.set_status(f"Loading {os.path.basename(entry.path)}...")
            data = self._vfs.read_entry_data(entry)
            basename = os.path.basename(entry.path)
            temp_path = os.path.join(self._temp_dir, basename)
            with open(temp_path, "wb") as f:
                f.write(data)
            self._preview.preview_file(temp_path)
            self._last_preview_path = entry.path
            self._last_preview_time = now
            self._progress.set_status(f"Preview: {basename} ({format_file_size(len(data))})")
        except Exception as e:
            self._progress.set_status(f"Preview error: {e}")
            logger.error("Preview error for %s: %s", entry.path, e)

    def _export_mesh(self, entry: PamtFileEntry, fmt: str):
        """Export a mesh file to OBJ or FBX."""
        from ui.dialogs.file_picker import pick_directory
        output_dir = pick_directory(self, "Select Export Directory")
        if not output_dir:
            return

        try:
            self._progress.set_status(f"Exporting {os.path.basename(entry.path)} as {fmt.upper()}...")
            data = self._vfs.read_entry_data(entry)

            from core.mesh_parser import parse_mesh
            mesh = parse_mesh(data, entry.path)

            if not mesh.submeshes:
                from ui.dialogs.confirmation import show_error
                show_error(self, "Export Error", "No geometry found in this file.")
                return

            # Build unique output name: include parent dirs to avoid collisions
            # e.g. "character/warrior/body.pac" → "character_warrior_body"
            clean_path = entry.path.replace("\\", "/")
            basename = os.path.splitext(clean_path)[0].replace("/", "_")

            # Try to find matching skeleton (.pab) for PAC files
            skeleton = None
            bone_count = 0
            if entry.path.lower().endswith(".pac") and fmt == "fbx":
                pab_path = entry.path.replace(".pac", ".pab")
                # Search all loaded PAMTs for matching .pab
                for _g, pamt_data in self._vfs._pamt_cache.items():
                    from core.pamt_parser import find_file_entry
                    pab_entry = find_file_entry(pamt_data, pab_path)
                    if pab_entry:
                        try:
                            from core.skeleton_parser import parse_pab
                            pab_data = self._vfs.read_entry_data(pab_entry)
                            if pab_data[:4] == b"PAR ":
                                skeleton = parse_pab(pab_data, pab_path)
                                bone_count = len(skeleton.bones)
                        except Exception:
                            pass
                        break

            if fmt == "obj":
                from core.mesh_exporter import export_obj
                export_obj(mesh, output_dir, basename)
                self._progress.set_status(
                    f"Exported OBJ: {mesh.total_vertices:,} verts, {mesh.total_faces:,} faces"
                )
            elif skeleton and skeleton.bones:
                from core.mesh_exporter import export_fbx_with_skeleton
                export_fbx_with_skeleton(mesh, skeleton, output_dir, basename)
                self._progress.set_status(
                    f"Exported FBX: {mesh.total_vertices:,} verts, {mesh.total_faces:,} faces, {bone_count} bones"
                )
            else:
                from core.mesh_exporter import export_fbx
                export_fbx(mesh, output_dir, basename)
                self._progress.set_status(
                    f"Exported FBX: {mesh.total_vertices:,} verts, {mesh.total_faces:,} faces"
                )

            from ui.dialogs.confirmation import show_info
            bone_msg = f"\nBones: {bone_count}" if bone_count > 0 else ""
            show_info(self, "Export Complete",
                      f"Exported {basename}.{fmt} to:\n{output_dir}\n\n"
                      f"Vertices: {mesh.total_vertices:,}\n"
                      f"Faces: {mesh.total_faces:,}\n"
                      f"Submeshes: {len(mesh.submeshes)}\n"
                      f"UVs: {'Yes' if mesh.has_uvs else 'No'}{bone_msg}")

        except Exception as e:
            self._progress.set_status(f"Export error: {e}")
            logger.error("Mesh export error for %s: %s", entry.path, e)
            from ui.dialogs.confirmation import show_error
            show_error(self, "Export Error", str(e))

    def _import_mesh(self, entry: PamtFileEntry):
        """Import an OBJ file, rebuild the mesh, and preview the result."""
        obj_path = pick_file(self, "Select OBJ File", filters="OBJ Files (*.obj);;All Files (*.*)")
        if not obj_path:
            return

        try:
            self._progress.set_status(f"Importing {os.path.basename(obj_path)}...")

            from core.mesh_importer import import_obj, build_mesh, transfer_pam_edit_to_pamlod_mesh
            imported = import_obj(obj_path)

            if not imported.submeshes:
                show_error(self, "Import Error", "No geometry found in OBJ file.")
                return

            # Read original data for rebuild
            original_data = self._vfs.read_entry_data(entry)

            # Override source info from the target entry
            imported.path = entry.path
            ext = os.path.splitext(entry.path.lower())[1]
            imported.format = "pac" if ext == ".pac" else "pamlod" if ext == ".pamlod" else "pam"

            # Build new binary
            new_data = build_mesh(imported, original_data)

            # Preview the rebuilt mesh
            basename = os.path.basename(entry.path)
            temp_path = os.path.join(self._temp_dir, basename)
            with open(temp_path, "wb") as f:
                f.write(new_data)
            self._preview.preview_file(temp_path)

            # Store for potential patching
            self._pending_mesh_data[entry.path.lower()] = {
                "entry": entry,
                "new_data": new_data,
                "imported": imported,
                "obj_path": obj_path,
            }

            self._progress.set_status(
                f"Imported: {imported.total_vertices:,} verts, "
                f"{imported.total_faces:,} faces, {len(new_data):,} bytes. "
                f"Right-click > 'Import OBJ + Patch to Game' to apply."
            )
            show_info(self, "Import Complete",
                      f"Imported {os.path.basename(obj_path)}\n\n"
                      f"Vertices: {imported.total_vertices:,}\n"
                      f"Faces: {imported.total_faces:,}\n"
                      f"Submeshes: {len(imported.submeshes)}\n"
                      f"New size: {len(new_data):,} bytes\n\n"
                      f"This step only previews the rebuilt mesh.\n"
                      f"Use 'Import OBJ + Patch to Game' to write to game files.")

        except Exception as e:
            self._progress.set_status(f"Import error: {e}")
            logger.error("Mesh import error for %s: %s", entry.path, e)
            show_error(self, "Import Error", str(e))

    def _import_and_patch_mesh(self, entry: PamtFileEntry):
        """Import OBJ, rebuild binary, and patch directly into the game."""
        obj_path = pick_file(self, "Select OBJ File", filters="OBJ Files (*.obj);;All Files (*.*)")
        if not obj_path:
            return

        try:
            self._progress.set_status(f"Importing and patching {os.path.basename(obj_path)}...")

            from core.mesh_importer import import_obj, build_mesh, transfer_pam_edit_to_pamlod_mesh
            imported = import_obj(obj_path)

            if not imported.submeshes:
                show_error(self, "Import Error", "No geometry found in OBJ file.")
                return

            # Read original data
            original_data = self._vfs.read_entry_data(entry)

            # Set format from target entry
            imported.path = entry.path
            ext = os.path.splitext(entry.path.lower())[1]
            imported.format = "pac" if ext == ".pac" else "pamlod" if ext == ".pamlod" else "pam"

            # Build new binary
            new_data = build_mesh(imported, original_data)
            self._pending_mesh_data[entry.path.lower()] = {
                "entry": entry,
                "new_data": new_data,
                "imported": imported,
                "obj_path": obj_path,
            }

            # Find which package group this entry belongs to
            paz_dir = os.path.basename(os.path.dirname(entry.paz_file))

            # Load PAMT data for this group
            pamt_data = self._vfs.load_pamt(paz_dir)

            extra_mod_files = []
            pair_note = ""
            pair_warning = ""
            if imported.format == "pam":
                paired_path = entry.path[:-4] + ".pamlod"
                paired_entry = next(
                    (e for e in pamt_data.file_entries if e.path.lower() == paired_path.lower()),
                    None,
                )
                if paired_entry:
                    try:
                        paired_original = self._vfs.read_entry_data(paired_entry)
                        paired_mesh = transfer_pam_edit_to_pamlod_mesh(
                            imported, original_data, paired_original, paired_entry.path
                        )
                        paired_new_data = build_mesh(paired_mesh, paired_original)
                        from core.repack_engine import ModifiedFile
                        extra_mod_files.append(ModifiedFile(
                            data=paired_new_data,
                            entry=paired_entry,
                            pamt_data=pamt_data,
                            package_group=paz_dir,
                        ))
                        pair_note = f"\nPaired LOD: {paired_entry.path}"
                    except Exception as pair_exc:
                        pair_warning = f"\nPaired LOD not patched: {pair_exc}"
                        logger.warning("Paired PAMLOD patch skipped for %s: %s", entry.path, pair_exc)

            # Confirm with user
            if not confirm_action(self, "Patch to Game",
                                  f"Replace {entry.path} in game?\n\n"
                                  f"Original: {len(original_data):,} bytes\n"
                                  f"New: {len(new_data):,} bytes\n"
                                  f"Vertices: {imported.total_vertices:,}\n"
                                  f"Faces: {imported.total_faces:,}"
                                  f"{pair_note}"
                                  f"{pair_warning}\n\n"
                                  f"A backup will be created automatically."):
                return

            # Repack using the existing engine
            from core.repack_engine import RepackEngine, ModifiedFile
            game_path = os.path.dirname(os.path.dirname(entry.paz_file))
            papgt_path = os.path.join(game_path, "meta", "0.papgt")

            mod_file = ModifiedFile(
                data=new_data,
                entry=entry,
                pamt_data=pamt_data,
                package_group=paz_dir,
            )

            engine = RepackEngine(game_path)
            result = engine.repack(
                [mod_file, *extra_mod_files], papgt_path=papgt_path,
                create_backup=True, verify_after=True,
            )

            if result.success:
                basename = os.path.basename(entry.path)
                temp_path = os.path.join(self._temp_dir, basename)
                with open(temp_path, "wb") as f:
                    f.write(new_data)
                self._preview.preview_file(temp_path)
                self._progress.set_status(
                    f"Patched {entry.path}: {imported.total_vertices:,} verts, "
                    f"{imported.total_faces:,} faces"
                )
                show_info(self, "Patch Complete",
                          f"Successfully patched {entry.path}\n\n"
                          f"Vertices: {imported.total_vertices:,}\n"
                          f"Faces: {imported.total_faces:,}\n"
                          f"Size: {len(new_data):,} bytes"
                          f"{pair_note}"
                          f"{pair_warning}\n\n"
                          f"Launch the game to see your changes!")
            else:
                err_text = "; ".join(result.errors) if result.errors else "Unknown repack failure."
                show_error(self, "Patch Error", f"Repack failed: {err_text}")

        except Exception as e:
            self._progress.set_status(f"Patch error: {e}")
            logger.error("Mesh patch error for %s: %s", entry.path, e)
            show_error(self, "Patch Error", str(e))

    def _open_archive_in_editor(self, entry: PamtFileEntry):
        try:
            data = self._vfs.read_entry_data(entry)
            basename = os.path.basename(entry.path)
            temp_path = os.path.join(self._temp_dir, basename)
            with open(temp_path, "wb") as f:
                f.write(data)
            self._open_in_editor(temp_path)
        except Exception as e:
            show_error(self, "Open Error", f"Failed to read {entry.path}: {e}")

    def _get_checked_entries(self) -> list[PamtFileEntry]:
        return self._model.get_checked_entries()

    def _extract_selected(self):
        entries = self._get_checked_entries()
        if not entries:
            show_error(self, "Error", "No files selected for extraction.")
            return
        self._do_extract(entries)

    def _extract_all(self):
        entries = self._get_checked_entries()
        if not entries:
            show_error(self, "Error", "No files to extract.")
            return
        self._do_extract(entries)

    def _do_extract(self, entries: list[PamtFileEntry]):
        output = self._output_path.text().strip()
        if not output:
            show_error(self, "Error", "Select an output directory first.")
            return
        os.makedirs(output, exist_ok=True)
        self._config.set("general.last_output_path", output)
        self._config.save()
        self._extract_sel_btn.setEnabled(False)
        self._extract_all_btn.setEnabled(False)

        def extract_work(worker, _entries=entries, _output=output):
            total = len(_entries)
            results = {"extracted": 0, "errors": 0, "decrypted": 0, "decompressed": 0}
            for i, entry in enumerate(_entries):
                if worker.is_cancelled():
                    break
                try:
                    result = self._vfs.extract_entry(entry, _output)
                    results["extracted"] += 1
                    if result.get("decrypted"):
                        results["decrypted"] += 1
                    if result.get("decompressed"):
                        results["decompressed"] += 1
                except Exception as e:
                    results["errors"] += 1
                    logger.error("Extract error: %s - %s", entry.path, e)
                pct = int(((i + 1) / total) * 100)
                worker.report_progress(pct, f"Extracting {os.path.basename(entry.path)}...")
            return results

        self._worker = FunctionWorker(extract_work)
        self._worker.progress.connect(lambda p, m: self._progress.set_progress(p, m))
        self._worker.finished_result.connect(self._on_extract_done)
        self._worker.error_occurred.connect(lambda e: show_error(self, "Extract Error", e))
        self._worker.start()

    def _on_extract_done(self, results):
        self._extract_sel_btn.setEnabled(True)
        self._extract_all_btn.setEnabled(True)
        msg = f"Extracted {results['extracted']} files"
        if results["decrypted"]:
            msg += f", {results['decrypted']} decrypted"
        if results["decompressed"]:
            msg += f", {results['decompressed']} decompressed"
        if results["errors"]:
            msg += f", {results['errors']} errors"
        self._progress.set_progress(100, msg)
        show_info(self, "Extraction Complete", msg)
        output = self._output_path.text().strip()
        self.files_extracted.emit(output)

    def _open_in_editor(self, path: str):
        if self._current_edit_file and self._editor.modified:
            if self._current_edit_file != path:
                if not confirm_action(self, "Unsaved Changes",
                                      f"'{os.path.basename(self._current_edit_file)}' has unsaved changes. Discard?"):
                    return
        if path == self._current_edit_file and not self._editor.modified:
            return
        try:
            encoding = self._encoding_combo.currentText()
            self._editor.load_file(path, encoding)
            self._current_edit_file = path
            self._edit_file_label.setText(f"Editor: {os.path.basename(path)}")
            syntax = get_syntax_type(path)
            self._syntax_combo.setCurrentText(syntax)
            self._editor.set_syntax(syntax)
            self._editor.modified = False
            self._edit_status.setText(f"Opened: {os.path.basename(path)}")
        except Exception as e:
            show_error(self, "Open Error", f"Failed to open {path}: {e}")

    def _save_file(self):
        if not self._current_edit_file:
            self._save_as()
            return
        try:
            encoding = self._encoding_combo.currentText()
            self._editor.save_file(self._current_edit_file, encoding)
            self._edit_status.setText(f"Saved: {os.path.basename(self._current_edit_file)}")
        except Exception as e:
            show_error(self, "Save Error", f"Failed to save: {e}")

    def _save_as(self):
        path = pick_save_file(self, "Save As", self._current_edit_file or "")
        if path:
            try:
                encoding = self._encoding_combo.currentText()
                self._editor.save_file(path, encoding)
                self._current_edit_file = path
                self._edit_file_label.setText(f"Editor: {os.path.basename(path)}")
                self._edit_status.setText(f"Saved: {os.path.basename(path)}")
            except Exception as e:
                show_error(self, "Save Error", f"Failed to save: {e}")

    def set_root_path(self, path: str):
        self._output_path.setText(path)

    def _get_selected_mesh_entries(self) -> list[PamtFileEntry]:
        from core.mesh_parser import is_mesh_file

        result = []
        seen = set()
        for row in self._get_selected_rows():
            row_data = self._model.row_at(row)
            if not row_data or not is_mesh_file(row_data.entry.path):
                continue
            key = row_data.entry.path.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(row_data.entry)
        return result

    def _ship_selected_meshes(self):
        if not self._vfs:
            show_error(self, "Ship to App", "Load the game data first.")
            return

        entries = self._get_selected_mesh_entries()
        if not entries:
            show_error(
                self,
                "Ship to App",
                "Select one or more .pac, .pam, or .pamlod rows in Explorer first.",
            )
            return

        prefilled = {}
        for entry in entries:
            pending = self._pending_mesh_data.get(entry.path.lower())
            if pending and pending.get("obj_path"):
                prefilled[entry.path.lower()] = pending["obj_path"]

        from ui.dialogs.ship_mesh_dialog import ShipMeshDialog

        dlg = ShipMeshDialog(self._vfs, self._config, entries, prefilled, self._item_index, self)
        dlg.exec()

    def _ship_single_mesh(self, entry: PamtFileEntry):
        if not self._vfs:
            show_error(self, "Ship to App", "Load the game data first.")
            return

        obj_path = pick_file(self, "Select OBJ File", filters="OBJ Files (*.obj);;All Files (*.*)")
        if not obj_path:
            return

        from ui.dialogs.ship_mesh_dialog import ShipMeshDialog

        dlg = ShipMeshDialog(
            self._vfs,
            self._config,
            [entry],
            {entry.path.lower(): obj_path},
            self._item_index,
            self,
        )
        dlg.exec()
