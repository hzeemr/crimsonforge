"""Enterprise Audio tab — browse, play, transcribe, translate, TTS, export, import.

Features:
- 107K+ voice audio files indexed with paloc text linking (94.9% match)
- 3 voice languages identified: Korean (0005), Japanese (0006), English (0035)
- Category filter: Quest Dialogue, AI Ambient, etc.
- NPC voice type filter: Human Male/Female, Dwarf, Giant, etc.
- Language filter: KO, JA, EN
- Linked paloc text shown for each audio file in all game languages
- Linked paloc text in all 14 game languages
- TTS generation with multi-provider support
- Export WAV / Import WAV / Patch to Game
- Generated audio history with playback
- Virtual scrolling table for 100K+ files
"""

import os
import tempfile
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QSplitter, QTableView, QHeaderView, QPlainTextEdit,
    QAbstractItemView, QApplication, QMenu, QSlider, QListWidget,
    QListWidgetItem, QGroupBox, QFormLayout, QCheckBox, QSpinBox,
    QDoubleSpinBox, QCompleter,
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QAbstractTableModel, QModelIndex,
)
from PySide6.QtGui import QColor

# ── OmniVoice Supported Languages (Extracted from OmniVoice_api.md) ──
OMNIVOICE_LANGUAGES = [
    'Auto', 'Abadi', 'Abkhazian', 'Abron', 'Abua', 'Adamawa Fulfulde', 'Adyghe', 'Afade', 'Afrikaans', 'Agwagwune',
    'Aja (Benin)', 'Akebu', 'Alago', 'Albanian', 'Algerian Arabic', 'Algerian Saharan Arabic', 'Ambo-Pasco Quechua',
    'Ambonese Malay', 'Amdo Tibetan', 'Amharic', 'Anaang', 'Angika', 'Antankarana Malagasy', 'Aragonese',
    'Arbëreshë Albanian', 'Arequipa-La Unión Quechua', 'Armenian', 'Ashe', 'Ashéninka Perené', 'Askopan',
    'Assamese', 'Asturian', 'Atayal', 'Awak', 'Ayacucho Quechua', 'Azerbaijani', 'Baatonum', 'Bacama', 'Bade', 'Bafia',
    'Bafut', 'Bagirmi Fulfulde', 'Bago-Kusuntu', 'Baharna Arabic', 'Bakoko', 'Balanta-Ganja', 'Balti', 'Bamenyam',
    'Bamun', 'Bangwinji', 'Banjar', 'Bankon', 'Baoulé', 'Bara Malagasy', 'Barok', 'Basa (Cameroon)', 'Basa (Nigeria)',
    'Bashkir', 'Basque', 'Batak Mandailing', 'Batanga', 'Bateri', 'Bats', 'Bayot', 'Bebele', 'Belarusian', 'Bengali',
    'Betawi', 'Bhili', 'Bhojpuri', 'Bilur', 'Bima', 'Bodo', 'Boghom', 'Bokyi', 'Bomu', 'Bondei', 'Borgu Fulfulde',
    'Bosnian', 'Brahui', 'Braj', 'Breton', 'Buduma', 'Buginese', 'Bukharic', 'Bulgarian', 'Bulu (Cameroon)', 'Bundeli',
    'Bunun', 'Bura-Pabir', 'Burak', 'Burmese', 'Burushaski', 'Cacaloxtepec Mixtec', 'Cajatambo North Lima Quechua',
    'Cakfem-Mushere', 'Cameroon Pidgin', 'Campidanese Sardinian', 'Cantonese', 'Catalan', 'Cebuano', 'Cen',
    'Central Kurdish', 'Central Nahuatl', 'Central Pame', 'Central Pashto', 'Central Puebla Nahuatl',
    'Central Tarahumara', 'Central Yupik', 'Central-Eastern Niger Fulfulde', 'Chadian Arabic', 'Chichewa',
    'Chichicapan Zapotec', 'Chiga', 'Chimalapa Zoque', 'Chimborazo Highland Quichua', 'Chinese',
    'Chiquián Ancash Quechua', 'Chitwania Tharu', 'Chokwe', 'Chuvash', 'Cibak', 'Coastal Konjo', 'Copainalá Zoque',
    'Cornish', 'Corongo Ancash Quechua', 'Croatian', 'Cross River Mbembe', 'Cuyamecalco Mixtec', 'Czech', 'Dadiya',
    'Dagbani', 'Dameli', 'Danish', 'Dargwa', 'Dazaga', 'Deccan', 'Degema', 'Dera (Nigeria)', 'Dghwede', 'Dhatki',
    'Dhivehi', 'Dhofari Arabic', 'Dijim-Bwilim', 'Dogri', 'Domaaki', 'Dotyali', 'Duala', 'Dutch', 'DũYa', 'Dyula',
    'Eastern Balochi', 'Eastern Bolivian Guaraní', 'Eastern Egyptian Bedawi Arabic', 'Eastern Krahn', 'Eastern Mari',
    'Eastern Yiddish', 'Ebrié', 'Eggon', 'Egyptian Arabic', 'Ejagham', 'Eleme', 'Eloyi', 'Embu', 'English', 'Erzya',
    'Esan', '開通', 'Estonian', 'Eton (Cameroon)', 'Ewondo', 'Extremaduran', 'Fang (Equatorial Guinea)', 'Fanti',
    'Farefare', 'Fe\'fe\'', 'Filipino', 'Filomena Mata-Coahuitlán Totonac', 'Finnish', 'Fipa', 'French', 'Fulah',
    'Galician', 'Gambian Wolof', 'Ganda', 'Garhwali', 'Gawar-Bati', 'Gawri', 'Gbagyi', 'Gbari', 'Geji', 'Gen',
    'Georgian', 'German', 'Geser-Gorom', 'Gheg Albanian', 'Ghomálá\'', 'Gidar', 'Glavda', 'Goan Konkani', 'Goaria',
    'Goemai', 'Gola', 'Greek', 'Guarani', 'Guduf-Gava', 'Guerrero Amuzgo', 'Gujarati', 'Gujari', 'Gulf Arabic',
    'Gurgula', 'Gusii', 'Gusilay', 'Gweno', 'Güilá Zapotec', 'Hadothi', 'Hahon', 'Haitian', 'Hakha Chin', 'Hakö',
    'Halia', 'Hausa', 'Hawaiian', 'Hazaragi', 'Hebrew', 'Hemba', 'Herero', 'Highland Konjo', 'Hijazi Arabic', 'Hindi',
    'Huarijio', 'Huautla Mazatec', 'Huaxcaleca Nahuatl', 'Huba', 'Huitepec Mixtec', 'Hula', 'Hungarian',
    'Hunjara-Kaina Ke', 'Hwana', 'Ibibio', 'Icelandic', 'Idakho-Isukha-Tiriki', 'Idoma', 'Igbo', 'Igo', 'Ikposo',
    'Ikwere', 'Imbabura Highland Quichua', 'Indonesian', 'Indus Kohistani',
    'Interlingua (International Auxiliary Language Association)', 'Inupiaq', 'Irish', 'Iron Ossetic', 'Isekiri',
    'Isoko', 'Italian', 'Ito', 'Itzá', 'Ixtayutla Mixtec', 'Izon', 'Jambi Malay', 'Japanese', 'Jaqaru', 'Jauja Wanca Quechua',
    'Jaunsari', 'Javanese', 'Jiba', 'Jju', 'Judeo-Moroccan Arabic', 'Juxtlahuaca Mixtec', 'Kabardian', 'Kabras',
    'Kabuverdianu', 'Kabyle', 'Kachi Koli', 'Kairak', 'Kalabari', 'Kalasha', 'Kalenjin', 'Kalkoti', 'Kamba', 'Kamo',
    'Kanauji', 'Kanembu', 'Kannada', 'Karekare', 'Kashmiri', 'Kathoriya Tharu', 'Kati', 'Kazakh', 'Keiyo', 'Khams Tibetan',
    'Khana', 'Khetrani', 'Khmer', 'Khowar', 'Kinga', 'Kinnauri', 'Kinyarwanda', 'Kirghiz', 'Kirya-Konzəl', 'Kochila Tharu',
    'Kohistani Shina', 'Kohumono', 'Kok Borok', 'Kol (Papua New Guinea)', 'Kom (Cameroon)', 'Koma', 'Konkani', 'Konzo',
    'Korean', 'Korwa', 'Kota (India)', 'Koti', 'Kuanua', 'Kuanyama', 'Kui (India)', 'Kulung (Nigeria)', 'Kuot', 'Kushi',
    'Kwambi', 'Kwasio', 'Lala-Roba', 'Lamang', 'Lao', 'Larike-Wakasihu', 'Lasi', 'Latgalian', 'Latvian', 'Levantine Arabic',
    'Liana-Seti', 'Liberia Kpelle', 'Liberian English', 'Libyan Arabic', 'Ligurian', 'Lijili', 'Lingala', 'Lithuanian',
    'Loarki', 'Logooli', 'Logudorese Sardinian', 'Loja Highland Quichua', 'Loloda', 'Longuda', 'Loxicha Zapotec',
    'Luba-Lulua', 'Luo', 'Lushai', 'Luxembourgish', 'Maasina Fulfulde', 'Maba (Chad)', 'Macedo-Romanian', 'Macedonian',
    'Mada (Cameroon)', 'Mafa', 'Maithili', 'Malay', 'Malayalam', 'Mali', 'Malinaltepec Me\'phaa', 'Maltese', 'Mandara',
    'Mandjak', 'Manggarai', 'Manipuri', 'Mansoanka', 'Manx', 'Maori', 'Marathi', 'Marghi Central', 'Marghi South',
    'Maria (India)', 'Marwari (Pakistan)', 'Masana', 'Masikoro Malagasy', 'Matsés', 'Mazaltepec Zapotec',
    'Mazatlán Mazatec', 'Mazatlán Mixe', 'Mbe', 'Mbo (Cameroon)', 'Mbum', 'Medumba', 'Mekeo', 'Meru', 'Mesopotamian Arabic',
    'Mewari', 'Min Nan Chinese', 'Mingrelian', 'Mitlatongo Mixtec', 'Miya', 'Mokpwe', 'Moksha', 'Mom Jango', 'Mongolian',
    'Moroccan Arabic', 'Motu', 'Mpiemo', 'Mpumpong', 'Mundang', 'Mungaka', 'Musey', 'Musgu', 'Musi', 'Naba', 'Najdi Arabic',
    'Nalik', 'Nawdm', 'Ndonga', 'Neapolitan', 'Nepali', 'Ngamo', 'Ngas', 'Ngiemboon', 'Ngizim', 'Ngomba', 'Ngombale',
    'Nigerian Fulfulde', 'Nigerian Pidgin', 'Nimadi', 'Nobiin', 'North Mesopotamian Arabic', 'North Moluccan Malay',
    'Northern Betsimisaraka Malagasy', 'Northern Hindko', 'Northern Kurdish', 'Northern Pame', 'Northern Pashto',
    'Northern Uzbek', 'Northwest Gbaya', 'Norwegian', 'Norwegian Bokmål', 'Norwegian Nynorsk', 'Notsi', 'Nyankpa',
    'Nyungwe', 'Nzanyi', 'Nüpode Huitoto', 'Occitan', 'Od', 'Odia', 'Odual', 'Omani Arabic', 'Orizaba Nahuatl', 'Orma',
    'Ormuri', 'Oromo', 'Pahari-Potwari', 'Paiwan', 'Panjabi', 'Papuan Malay', 'Parkari Koli', 'Pedi', 'Pero', 'Persian',
    'Petats', 'Phalura', 'Piemontese', 'Piya-Kwonci', 'Plateau Malagasy', 'Polish', 'Poqomam', 'Portuguese', 'Pulaar',
    'Pular', 'Puno Quechua', 'Pushto', 'Pökoot', 'Qaqet', 'Quiotepec Chinantec', 'Rana Tharu', 'Rangi', 'Rapoisi',
    'Ratahan', 'Rayón Zoque', 'Romanian', 'Romansh', 'Rombo', 'Rotokas', 'Rukai', 'Russian', 'Sacapulteco',
    'Saidi Arabic', 'Sakalava Malagasy', 'Sakizaya', 'Saleman', 'Samba Daka', 'Samba Leko', 'San Felipe Otlaltepec Popoloca',
    'San Francisco Del Mar Huave', 'San Juan Atzingo Popoloca', 'San Martín Itunyoso Triqui', 'San Miguel El Grande Mixtec',
    'Sansi', 'Sanskrit', 'Santa Ana de Tusi Pasco Quechua', 'Santa Catarina Albarradas Zapotec', 'Santali',
    'Santiago del Estero Quichua', 'Saposa', 'Saraiki', 'Sardinian', 'Saya', 'Sediq', 'Serbian', 'Seri', 'Shina', 'Shona',
    'Siar-Lak', 'Sibe', 'Sicilian', 'Sihuas Ancash Quechua', 'Sikkimese', 'Sinaugoro', 'Sindhi', 'Sindhi Bhil', 'Sinhala',
    'Sinicahua Mixtec', 'Sipacapense', 'Siwai', 'Slovak', 'Slovenian', 'Solos', 'Somali', 'Soninke', 'South Giziga',
    'South Ucayali Ashéninka', 'Southeastern Nochixtlán Mixtec', 'Southern Betsimisaraka Malagasy', 'Southern Pashto',
    'Southern Pastaza Quechua', 'Soyaltepec Mazatec', 'Spanish', 'Standard Arabic', 'Standard Moroccan Tamazight',
    'Sudanese Arabic', 'Sulka', 'Svan', 'Swahili', 'Swedish', 'Tae\'', 'Tahaggart Tamahaq', 'Taita', 'Tajik', 'Tamil',
    'Tandroy-Mahafaly Malagasy', 'Tangale', 'Tanosy Malagasy', 'Tarok', 'Tatar', 'Tedaga', 'Telugu', 'Tem', 'Teop',
    'Tepeuxila Cuicatec', 'Tepinapa Chinantec', 'Tera', 'Terei', 'Termanu', 'Tesaka Malagasy', 'Tetelcingo Nahuatl',
    'Teutila Cuicatec', 'Thai', 'Tibetan', 'Tidaá Mixtec', 'Tidore', 'Tigak', 'Tigre', 'Tigrinya', 'Tilquiapan Zapotec',
    'Tinputz', 'Tlacoapa Me\'phaa', 'Tlacoatzintepec Chinantec', 'Tlingit', 'Toki Pona', 'Tomoip', 'Tondano', 'Tonsea',
    'Tooro', 'Torau', 'Torwali', 'Tsimihety Malagasy', 'Tsotso', 'Tswana', 'Tugen', 'Tuki', 'Tula', 'Tulu', 'Tunen',
    'Tungag', 'Tunisian Arabic', 'Tupuri', 'Turkana', 'Turkish', 'Turkmen', 'Tututepec Mixtec', 'Twi', 'Ubaghara', 'Uighur',
    'Ukrainian', 'Umbundu', 'Upper Sorbian', 'Urdu', 'Ushojo', 'Uzbek', 'Vai', 'Vietnamese', 'Votic', 'Võro', 'Waci Gbe',
    'Wadiyara Koli', 'Waja', 'Wakhi', 'Wanga', 'Wapan', 'Warji', 'Welsh', 'Wemale', 'Western Frisian',
    'Western Highland Purepecha', 'Western Juxtlahuaca Mixtec', 'Western Maninkakan', 'Western Mari',
    'Western Niger Fulfulde', 'Western Panjabi', 'Wolof', 'Wuzlam', 'Xanaguía Zapotec', 'Xhosa', 'Yace', 'Yakut',
    'Yalahatan', 'Yanahuanca Pasco Quechua', 'Yangben', 'Yaqui', 'Yauyos Quechua', 'Yekhee', 'Yiddish', 'Yidgha',
    'Yoruba', 'Yutanduchi Mixtec', 'Zacatlán-Ahuacatlán-Tepetzintla Nahuatl', 'Zarma', 'Zaza', 'Zulu', 'Ömie'
]


from core.vfs_manager import VfsManager
from core.pamt_parser import PamtFileEntry
from core.audio_converter import wem_to_wav, get_audio_info, audio_to_wav
from core.audio_index import (
    AudioEntry, build_audio_index, build_paloc_lookup,
    get_all_categories, get_all_languages, VOICE_LANG_PACKAGES,
)
from ui.widgets.audio_player import AudioPlayerWidget
from ui.widgets.progress_widget import ProgressWidget
from ui.widgets.search_history_line_edit import SearchHistoryLineEdit
from ui.dialogs.file_picker import pick_directory, pick_file, pick_save_file
from ui.dialogs.confirmation import show_error, show_info, confirm_action
from utils.thread_worker import FunctionWorker
from utils.platform_utils import format_file_size
from utils.logger import get_logger

logger = get_logger("ui.tab_audio")

ALL_PACKAGES = "All"
ALL_CATEGORIES = "All Categories"
ALL_LANGUAGES = "All Languages"

_COL_FILE = 0
_COL_LANG = 1
_COL_CATEGORY = 2
_COL_TEXT = 3
_COL_SIZE = 4
_COL_NPC = 5
_COL_LOCATION = 6
_COL_COUNT = 7
_HEADERS = ["File", "Lang", "Category", "Text", "Size", "NPC Voice", "Location"]


class _AudioModel(QAbstractTableModel):
    """Virtual model for audio file list with paloc text linking."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: list[AudioEntry] = []
        self._filtered: list[int] = []
        self._cat_filter = ""
        self._lang_filter = ""
        self._search = ""

    def set_data(self, entries: list[AudioEntry]):
        self.beginResetModel()
        self._all = entries
        self._refilter()
        self.endResetModel()

    def set_filter(self, category: str = "", language: str = "", search: str = ""):
        self.beginResetModel()
        self._cat_filter = category
        self._lang_filter = language
        self._search = search.strip().lower()
        self._refilter()
        self.endResetModel()

    def _refilter(self):
        cat = self._cat_filter
        lang = self._lang_filter
        search = self._search
        if not cat and not lang and not search:
            self._filtered = list(range(len(self._all)))
            return
        result = []
        for i, e in enumerate(self._all):
            if cat and e.category != cat:
                continue
            if lang and e.voice_lang != lang:
                continue
            if search:
                # Search across filename, key, all translation texts, and NPC voice
                all_texts = " ".join(e.text_translations.values()) if e.text_translations else e.text_original
                haystack = f"{e.entry.path} {e.paloc_key} {e.text_original} {all_texts} {e.voice_prefix}".lower()
                if search not in haystack:
                    continue
            result.append(i)
        self._filtered = result

    def row_at(self, view_row: int) -> AudioEntry:
        if 0 <= view_row < len(self._filtered):
            return self._all[self._filtered[view_row]]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return _COL_COUNT

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._filtered):
            return None
        e = self._all[self._filtered[index.row()]]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == _COL_FILE:
                return os.path.basename(e.entry.path)
            elif col == _COL_LANG:
                lang = e.voice_lang.lower() if e.voice_lang else ""
                if lang == "ja":
                    return "CH"
                return lang.upper()
            elif col == _COL_CATEGORY:
                return e.category
            elif col == _COL_TEXT:
                return e.text_original[:80] if e.text_original else ""
            elif col == _COL_SIZE:
                return format_file_size(e.entry.orig_size)
            elif col == _COL_NPC:
                return e.voice_prefix
            elif col == _COL_LOCATION:
                return e.entry.paz_file

        elif role == Qt.ForegroundRole:
            if col == _COL_LANG:
                colors = {"ko": QColor("#f9e2af"), "ja": QColor("#f38ba8"), "en": QColor("#89b4fa")}
                return colors.get(e.voice_lang)
            if col == _COL_TEXT and e.text_original:
                return QColor("#a6e3a1")

        elif role == Qt.ToolTipRole:
            lines = [
                f"Game Path: {e.entry.path}",
                f"Archive: {e.entry.paz_file}",
                f"Key: {e.paloc_key}"
            ]
            if e.text_original:
                lines.append(f"Text: {e.text_original[:200]}")
            if e.npc_gender:
                lines.append(f"NPC: {e.npc_gender} {e.npc_class} ({e.npc_age})")
            return "\n".join(lines)

        return None

    def sort(self, column, order=Qt.AscendingOrder):
        self.beginResetModel()
        rev = order == Qt.DescendingOrder
        a = self._all
        if column == _COL_FILE:
            self._filtered.sort(key=lambda i: a[i].entry.path.lower(), reverse=rev)
        elif column == _COL_LANG:
            self._filtered.sort(key=lambda i: a[i].voice_lang, reverse=rev)
        elif column == _COL_CATEGORY:
            self._filtered.sort(key=lambda i: a[i].category, reverse=rev)
        elif column == _COL_TEXT:
            self._filtered.sort(key=lambda i: a[i].text_original, reverse=rev)
        elif column == _COL_SIZE:
            self._filtered.sort(key=lambda i: a[i].entry.orig_size, reverse=rev)
        elif column == _COL_NPC:
            self._filtered.sort(key=lambda i: a[i].voice_prefix, reverse=rev)
        elif column == _COL_LOCATION:
            self._filtered.sort(key=lambda i: a[i].entry.paz_file, reverse=rev)
        self.endResetModel()

    @property
    def filtered_count(self): return len(self._filtered)
    @property
    def total_count(self): return len(self._all)


class AudioTab(QWidget):
    """Enterprise audio tab."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._vfs: VfsManager = None
        self._all_groups: list[str] = []
        self._tts_engine = None
        self._wav_cache: dict = {}
        self._generated_files: list[dict] = []
        self._batch_worker = None
        self._temp_dir = tempfile.mkdtemp(prefix="crimsonforge_audio_")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── Toolbar ──
        tb = QHBoxLayout()

        tb.addWidget(QLabel("Language:"))
        self._lang_filter = QComboBox()
        self._lang_filter.addItem(ALL_LANGUAGES, "")
        self._lang_filter.addItem("Korean (KO)", "ko")
        self._lang_filter.addItem("Chinois (CH)", "ja")
        self._lang_filter.addItem("English (EN)", "en")
        self._lang_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        self._lang_filter.setMinimumWidth(120)
        tb.addWidget(self._lang_filter)

        tb.addWidget(QLabel("Category:"))
        self._cat_filter = QComboBox()
        self._cat_filter.addItem(ALL_CATEGORIES, "")
        self._cat_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        self._cat_filter.setMinimumWidth(140)
        tb.addWidget(self._cat_filter)

        tb.addWidget(QLabel("Search:"))
        self._search_input = SearchHistoryLineEdit(self._config, "audio")
        self._search_input.setPlaceholderText("Search by filename, key, text, NPC voice...")
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        tb.addWidget(self._search_input, 1)

        self._count_label = QLabel("0 files")
        self._count_label.setStyleSheet("color: #89b4fa; font-weight: 600; padding: 0 4px;")
        tb.addWidget(self._count_label)
        layout.addLayout(tb)

        # ── Main splitter ──
        splitter = QSplitter(Qt.Horizontal)

        # LEFT: Audio table
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)

        lh = QHBoxLayout()
        lbl = QLabel("Voice Audio Files")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px;")
        lh.addWidget(lbl)
        lh.addStretch()
        exp_btn = QPushButton("Export Selected WAV")
        exp_btn.setObjectName("primary")
        exp_btn.clicked.connect(self._export_selected)
        lh.addWidget(exp_btn)
        ll.addLayout(lh)

        self._model = _AudioModel(self)
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._view.setAlternatingRowColors(True)
        self._view.setSortingEnabled(True)
        self._view.setShowGrid(False)
        self._view.verticalHeader().setVisible(False)
        self._view.verticalHeader().setDefaultSectionSize(22)
        self._view.horizontalHeader().setSectionResizeMode(_COL_FILE, QHeaderView.Interactive)
        self._view.horizontalHeader().setSectionResizeMode(_COL_TEXT, QHeaderView.Interactive)
        self._view.setColumnWidth(_COL_FILE, 280)
        self._view.setColumnWidth(_COL_LANG, 40)
        self._view.setColumnWidth(_COL_CATEGORY, 120)
        self._view.setColumnWidth(_COL_TEXT, 400)
        self._view.setColumnWidth(_COL_SIZE, 60)
        self._view.setColumnWidth(_COL_NPC, 140)
        self._view.setColumnWidth(_COL_LOCATION, 500)
        self._view.horizontalHeader().setSectionResizeMode(_COL_LOCATION, QHeaderView.Interactive)
        self._view.clicked.connect(self._on_row_clicked)
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        ll.addWidget(self._view, 1)
        splitter.addWidget(left)

        # CENTER: Player + Text + Generated
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)

        cl.addWidget(QLabel("Player"))
        self._audio_player = AudioPlayerWidget(standalone=True)
        cl.addWidget(self._audio_player)

        # Linked text display
        self._text_display = QPlainTextEdit()
        self._text_display.setReadOnly(True)
        self._text_display.setMaximumHeight(180)
        self._text_display.setPlaceholderText("Click an audio file to see linked dialogue text...")
        self._text_display.setStyleSheet("font-size: 12px;")
        cl.addWidget(self._text_display)

        # Generated audio history
        gh = QHBoxLayout()
        gh.addWidget(QLabel("Generated Audio"))
        gh.addStretch()
        clear_gen_btn = QPushButton("Clear All")
        clear_gen_btn.clicked.connect(self._clear_generated)
        gh.addWidget(clear_gen_btn)
        cl.addLayout(gh)

        self._gen_list = QListWidget()
        self._gen_list.setAlternatingRowColors(True)
        self._gen_list.setMaximumHeight(120)
        self._gen_list.itemClicked.connect(self._play_generated)
        cl.addWidget(self._gen_list)

        splitter.addWidget(center)

        # RIGHT: TTS Generator
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        rl.addWidget(QLabel("TTS Generator"))

        # Style for dropdown arrows to match user request (#87CEFA)
        combo_style = """
            QComboBox::drop-down {
                background-color: #87CEFA;
                border-top-right-radius: 6px;
                border-bottom-right-radius: 6px;
                width: 24px;
            }
            QComboBox::down-arrow {
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #1e1e2e;
                width: 0;
                height: 0;
                margin-top: 2px;
            }
        """

        pr = QHBoxLayout()
        pr.addWidget(QLabel("Provider:"))
        self._tts_provider = QComboBox()
        self._tts_provider.setStyleSheet(combo_style)
        self._tts_provider.currentIndexChanged.connect(self._on_tts_provider_changed)
        pr.addWidget(self._tts_provider, 1)
        rl.addLayout(pr)

        mr = QHBoxLayout()
        mr.addWidget(QLabel("Model:"))
        self._tts_model = QComboBox()
        self._tts_model.setEditable(True)
        self._tts_model.setStyleSheet(combo_style)
        mr.addWidget(self._tts_model, 1)
        self._tts_refresh_models_btn = QPushButton("Refresh")
        self._tts_refresh_models_btn.setFixedWidth(55)
        self._tts_refresh_models_btn.clicked.connect(self._refresh_tts_models)
        mr.addWidget(self._tts_refresh_models_btn)
        rl.addLayout(mr)

        self._tts_status_label = QLabel("Status: Ready")
        self._tts_status_label.setStyleSheet("color: #89b4fa; padding: 2px;")
        rl.addWidget(self._tts_status_label)

        vr = QHBoxLayout()
        vr.addWidget(QLabel("Voice:"))
        self._voice_combo = QComboBox()
        self._voice_combo.setEditable(True)
        self._voice_combo.setStyleSheet(combo_style)
        vr.addWidget(self._voice_combo, 1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(55)
        refresh_btn.clicked.connect(self._refresh_tts_voices)
        vr.addWidget(refresh_btn)
        rl.addLayout(vr)

        lr = QHBoxLayout()
        lr.addWidget(QLabel("Language:"))
        self._tts_lang = QComboBox()
        self._tts_lang.setEditable(True)
        self._tts_lang.setStyleSheet(combo_style)
        # Default starter list, will be populated by _refresh_provider_specific_ui
        self._tts_lang.addItems(["Auto", "en-US", "ko-KR", "ja-JP", "zh-CN"])
        self._tts_lang.setCurrentText("Auto")
        self._tts_lang.currentTextChanged.connect(lambda _: self._refresh_tts_voices())
        self._tts_lang.currentTextChanged.connect(self._on_tts_lang_changed)
        
        # Make language dropdown searchable/filterable
        self._tts_lang.setInsertPolicy(QComboBox.NoInsert)
        if self._tts_lang.completer():
            self._tts_lang.completer().setCompletionMode(QCompleter.PopupCompletion)
            self._tts_lang.completer().setFilterMode(Qt.MatchContains)
            
        lr.addWidget(self._tts_lang, 1)
        rl.addLayout(lr)

        sr = QHBoxLayout()
        sr.addWidget(QLabel("Speed:"))
        self._speed = QSlider(Qt.Horizontal)
        self._speed.setRange(50, 200)
        self._speed.setValue(100)
        self._speed_label = QLabel("1.0x")
        self._speed.valueChanged.connect(lambda v: self._speed_label.setText(f"{v/100:.1f}x"))
        sr.addWidget(self._speed, 1)
        sr.addWidget(self._speed_label)
        rl.addLayout(sr)

        self._omnivoice_clone_group = QGroupBox("OmniVoice Cloning")
        self._omnivoice_clone_group.setVisible(False)
        clone_form = QFormLayout(self._omnivoice_clone_group)

        self._omnivoice_mode = QComboBox()
        self._omnivoice_mode.addItem("One-Shot Clone", "one_shot")
        self._omnivoice_mode.addItem("Saved Profile", "saved_profile")
        self._omnivoice_mode.addItem("Voice / Design / Auto", "voice")
        clone_form.addRow("Mode:", self._omnivoice_mode)

        self._omnivoice_profile_name = QLineEdit()
        self._omnivoice_profile_name.setPlaceholderText("Auto-filled from selected NPC / voice")
        clone_form.addRow("Profile Name:", self._omnivoice_profile_name)

        ref_row = QHBoxLayout()
        self._omnivoice_ref_audio = QLineEdit()
        self._omnivoice_ref_audio.setPlaceholderText("Auto-filled from selected game voice audio")
        ref_row.addWidget(self._omnivoice_ref_audio, 1)
        self._omnivoice_ref_browse_btn = QPushButton("Browse")
        self._omnivoice_ref_browse_btn.clicked.connect(self._browse_omnivoice_reference_audio)
        ref_row.addWidget(self._omnivoice_ref_browse_btn)
        self._omnivoice_use_selected_btn = QPushButton("Use Row")
        self._omnivoice_use_selected_btn.clicked.connect(self._use_selected_audio_as_reference)
        ref_row.addWidget(self._omnivoice_use_selected_btn)
        clone_form.addRow("Ref Audio:", ref_row)

        self._omnivoice_ref_text = QPlainTextEdit()
        self._omnivoice_ref_text.setMaximumHeight(60)
        self._omnivoice_ref_text.setPlaceholderText("Leave empty unless you have the exact reference transcript")
        clone_form.addRow("Ref Text:", self._omnivoice_ref_text)

        self._omnivoice_refresh_profile = QCheckBox("Refresh / overwrite profile before synthesis")
        self._omnivoice_refresh_profile.setChecked(True)
        clone_form.addRow("", self._omnivoice_refresh_profile)

        profile_btn_row = QHBoxLayout()
        self._omnivoice_check_btn = QPushButton("Check Server")
        self._omnivoice_check_btn.clicked.connect(self._check_omnivoice_server)
        profile_btn_row.addWidget(self._omnivoice_check_btn)
        self._omnivoice_save_profile_btn = QPushButton("Save / Update Profile")
        self._omnivoice_save_profile_btn.clicked.connect(self._save_omnivoice_profile)
        profile_btn_row.addWidget(self._omnivoice_save_profile_btn)
        clone_form.addRow("", profile_btn_row)
        rl.addWidget(self._omnivoice_clone_group)

        self._omnivoice_advanced_group = QGroupBox("OmniVoice Advanced")
        self._omnivoice_advanced_group.setVisible(False)
        adv_form = QFormLayout(self._omnivoice_advanced_group)

        self._omnivoice_num_step = QSpinBox()
        self._omnivoice_num_step.setRange(1, 64)
        self._omnivoice_num_step.setValue(int(self._config.get("tts.omnivoice_num_step", 32)))
        adv_form.addRow("Inference Steps:", self._omnivoice_num_step)

        self._omnivoice_guidance = QDoubleSpinBox()
        self._omnivoice_guidance.setRange(0.0, 10.0)
        self._omnivoice_guidance.setDecimals(2)
        self._omnivoice_guidance.setSingleStep(0.1)
        self._omnivoice_guidance.setValue(float(self._config.get("tts.omnivoice_guidance_scale", 3.0)))
        adv_form.addRow("Guidance Scale:", self._omnivoice_guidance)

        self._omnivoice_denoise = QCheckBox()
        self._omnivoice_denoise.setChecked(bool(self._config.get("tts.omnivoice_denoise", True)))
        adv_form.addRow("Denoise:", self._omnivoice_denoise)

        self._omnivoice_duration = QDoubleSpinBox()
        self._omnivoice_duration.setRange(0.0, 120.0)
        self._omnivoice_duration.setDecimals(2)
        self._omnivoice_duration.setSingleStep(0.1)
        self._omnivoice_duration.setSpecialValueText("Auto")
        self._omnivoice_duration.setValue(float(self._config.get("tts.omnivoice_duration_seconds", 0.0)))
        adv_form.addRow("Fixed Duration:", self._omnivoice_duration)

        self._omnivoice_t_shift = QDoubleSpinBox()
        self._omnivoice_t_shift.setRange(0.0, 2.0)
        self._omnivoice_t_shift.setDecimals(2)
        self._omnivoice_t_shift.setSingleStep(0.05)
        self._omnivoice_t_shift.setValue(float(self._config.get("tts.omnivoice_t_shift", 0.1)))
        adv_form.addRow("t_shift:", self._omnivoice_t_shift)

        self._omnivoice_position_temp = QDoubleSpinBox()
        self._omnivoice_position_temp.setRange(0.0, 10.0)
        self._omnivoice_position_temp.setDecimals(2)
        self._omnivoice_position_temp.setSingleStep(0.1)
        self._omnivoice_position_temp.setValue(float(self._config.get("tts.omnivoice_position_temperature", 5.0)))
        adv_form.addRow("Position Temp:", self._omnivoice_position_temp)

        self._omnivoice_class_temp = QDoubleSpinBox()
        self._omnivoice_class_temp.setRange(0.0, 10.0)
        self._omnivoice_class_temp.setDecimals(2)
        self._omnivoice_class_temp.setSingleStep(0.1)
        self._omnivoice_class_temp.setValue(float(self._config.get("tts.omnivoice_class_temperature", 0.0)))
        adv_form.addRow("Class Temp:", self._omnivoice_class_temp)

        self._omnivoice_gender_use = QCheckBox()
        self._omnivoice_gender_use.setChecked(bool(self._config.get("tts.omnivoice_gender_use", False)))
        self._omnivoice_gender = QComboBox()
        self._omnivoice_gender.addItems(['Auto', 'Male / 男', 'Female / 女'])
        self._omnivoice_gender.setStyleSheet(combo_style)
        self._omnivoice_gender.setEnabled(self._omnivoice_gender_use.isChecked())
        self._omnivoice_gender_use.toggled.connect(self._omnivoice_gender.setEnabled)
        h1 = QHBoxLayout()
        h1.addWidget(self._omnivoice_gender_use)
        h1.addWidget(self._omnivoice_gender)
        adv_form.addRow("Gender:", h1)

        self._omnivoice_age_use = QCheckBox()
        self._omnivoice_age_use.setChecked(bool(self._config.get("tts.omnivoice_age_use", False)))
        self._omnivoice_age = QComboBox()
        self._omnivoice_age.addItems(['Auto', 'Child / 儿童', 'Teenager / 少年', 'Young Adult / 青年', 'Middle-aged / 中年', 'Elderly / 老年'])
        self._omnivoice_age.setStyleSheet(combo_style)
        self._omnivoice_age.setEnabled(self._omnivoice_age_use.isChecked())
        self._omnivoice_age_use.toggled.connect(self._omnivoice_age.setEnabled)
        h2 = QHBoxLayout()
        h2.addWidget(self._omnivoice_age_use)
        h2.addWidget(self._omnivoice_age)
        adv_form.addRow("Age:", h2)

        self._omnivoice_pitch_use = QCheckBox()
        self._omnivoice_pitch_use.setChecked(bool(self._config.get("tts.omnivoice_pitch_use", False)))
        self._omnivoice_pitch = QComboBox()
        self._omnivoice_pitch.addItems(['Auto', 'Very Low Pitch / 极低音调', 'Low Pitch / 低音调', 'Moderate Pitch / 中音调', 'High Pitch / 高音调', 'Very High Pitch / 极高音调'])
        self._omnivoice_pitch.setStyleSheet(combo_style)
        self._omnivoice_pitch.setEnabled(self._omnivoice_pitch_use.isChecked())
        self._omnivoice_pitch_use.toggled.connect(self._omnivoice_pitch.setEnabled)
        h3 = QHBoxLayout()
        h3.addWidget(self._omnivoice_pitch_use)
        h3.addWidget(self._omnivoice_pitch)
        adv_form.addRow("Pitch:", h3)

        self._omnivoice_style_use = QCheckBox()
        self._omnivoice_style_use.setChecked(bool(self._config.get("tts.omnivoice_style_use", False)))
        self._omnivoice_style = QComboBox()
        self._omnivoice_style.addItems(['Auto', 'Whisper / 耳语'])
        self._omnivoice_style.setStyleSheet(combo_style)
        self._omnivoice_style.setEnabled(self._omnivoice_style_use.isChecked())
        self._omnivoice_style_use.toggled.connect(self._omnivoice_style.setEnabled)
        h4 = QHBoxLayout()
        h4.addWidget(self._omnivoice_style_use)
        h4.addWidget(self._omnivoice_style)
        adv_form.addRow("Style:", h4)

        self._omnivoice_accent_use = QCheckBox()
        self._omnivoice_accent_use.setChecked(bool(self._config.get("tts.omnivoice_accent_use", False)))
        self._omnivoice_accent = QComboBox()
        self._omnivoice_accent.addItems(['Auto', 'American Accent / 美式口音', 'Australian Accent / 澳大利亚口音', 'British Accent / 英国口音', 'Chinese Accent / 中国口音', 'Canadian Accent / 加拿大口音', 'Indian Accent / 印度口音', 'Korean Accent / 韩国口音', 'Portuguese Accent / 葡萄牙口音', 'Russian Accent / 俄罗斯口音', 'Japanese Accent / 日本口音'])
        self._omnivoice_accent.setStyleSheet(combo_style)
        self._omnivoice_accent.setEnabled(self._omnivoice_accent_use.isChecked())
        self._omnivoice_accent_use.toggled.connect(self._omnivoice_accent.setEnabled)
        h5 = QHBoxLayout()
        h5.addWidget(self._omnivoice_accent_use)
        h5.addWidget(self._omnivoice_accent)
        adv_form.addRow("English Accent:", h5)
        rl.addWidget(self._omnivoice_advanced_group)

        rl.addWidget(QLabel("Text:"))
        self._tts_text = QPlainTextEdit()
        self._tts_text.setPlaceholderText("Enter text or click audio to load linked text...")
        self._tts_text.setMaximumHeight(100)
        rl.addWidget(self._tts_text)

        btns = QHBoxLayout()
        gen_btn = QPushButton("Generate")
        gen_btn.setObjectName("primary")
        gen_btn.clicked.connect(self._generate_tts)
        btns.addWidget(gen_btn)
        patch_btn = QPushButton("Generate + Patch")
        patch_btn.clicked.connect(self._generate_and_patch)
        btns.addWidget(patch_btn)
        rl.addLayout(btns)

        batch_btns = QHBoxLayout()
        self._batch_generate_btn = QPushButton("Batch Generate")
        self._batch_generate_btn.clicked.connect(self._batch_generate)
        batch_btns.addWidget(self._batch_generate_btn)
        self._batch_patch_btn = QPushButton("Generate All + Patch")
        self._batch_patch_btn.clicked.connect(self._batch_generate_and_patch)
        batch_btns.addWidget(self._batch_patch_btn)
        rl.addLayout(batch_btns)
        rl.addStretch()

        splitter.addWidget(right)
        splitter.setSizes([480, 300, 250])
        layout.addWidget(splitter, 1)

        self._progress = ProgressWidget()
        layout.addWidget(self._progress)

    # ── Initialization ──

    def initialize_from_game(self, vfs: VfsManager, groups: list[str]) -> None:
        self._vfs = vfs
        self._all_groups = groups

        self._progress.set_progress(0, "Building audio index...")
        QApplication.processEvents()

        # Build paloc lookup for text linking
        paloc_lookup = build_paloc_lookup(vfs, groups,
            progress_callback=lambda p, m: (self._progress.set_status(m), QApplication.processEvents()))

        self._progress.set_status("Indexing voice audio files...")
        QApplication.processEvents()

        # Build audio index with paloc linking
        audio_entries = build_audio_index(vfs, groups, paloc_lookup,
            progress_callback=lambda p, m: (self._progress.set_progress(p, m), QApplication.processEvents()))

        self._model.set_data(audio_entries)

        # Populate category filter
        cats = get_all_categories(audio_entries)
        self._cat_filter.blockSignals(True)
        self._cat_filter.clear()
        self._cat_filter.addItem(ALL_CATEGORIES, "")
        for c in cats:
            self._cat_filter.addItem(c, c)
        self._cat_filter.blockSignals(False)

        self._apply_filter()

        linked = sum(1 for e in audio_entries if e.text_original)
        self._progress.set_progress(100,
            f"Indexed {len(audio_entries):,} audio files, {linked:,} linked to text")

        self.refresh_from_settings()

    def refresh_from_settings(self):
        from ai.tts_engine import TTSEngine

        current_provider = self._tts_provider.currentData()
        current_model = self._tts_model.currentData() or self._tts_model.currentText()
        current_voice = self._voice_combo.currentData() or self._voice_combo.currentText()

        self._tts_engine = TTSEngine()
        self._tts_engine.initialize_from_config(self._config)
        self._populate_tts_provider_list(current_provider)
        self._refresh_tts_models(preferred_model=current_model)
        self._refresh_tts_voices(preferred_voice=current_voice)

    def _populate_tts_provider_list(self, preferred_provider: str = ""):
        from ai.tts_engine import TTS_KEY_SHARING

        self._tts_provider.blockSignals(True)
        self._tts_provider.clear()
        if not self._tts_engine:
            self._tts_provider.blockSignals(False)
            return

        for provider_info in self._tts_engine.list_providers():
            pid = provider_info["id"]
            if not provider_info.get("requires_api_key", True):
                self._tts_provider.addItem(provider_info["name"], pid)
                continue

            shared = TTS_KEY_SHARING.get(pid)
            if shared:
                if self._config.get(f"ai_providers.{shared}.enabled", False) and \
                        self._config.get(f"ai_providers.{shared}.api_key", ""):
                    self._tts_provider.addItem(provider_info["name"], pid)
                continue

            if self._config.get(f"tts.{pid}_api_key", ""):
                self._tts_provider.addItem(provider_info["name"], pid)

        self._tts_provider.blockSignals(False)
        selected = False
        if preferred_provider:
            for i in range(self._tts_provider.count()):
                if self._tts_provider.itemData(i) == preferred_provider:
                    self._tts_provider.setCurrentIndex(i)
                    selected = True
                    break
        if not selected and self._tts_provider.count():
            self._tts_provider.setCurrentIndex(0)

    def _model_config_key(self, provider_id: str) -> str:
        from ai.tts_engine import get_tts_model_config_key
        return get_tts_model_config_key(provider_id)

    def _current_tts_language_query(self) -> str:
        lang = (self._tts_lang.currentText() or "").strip()
        if not lang or lang.lower() == "auto":
            return ""
        return lang

    def _current_tts_language_code(self) -> str:
        lang = self._current_tts_language_query()
        if not lang:
            return ""
            
        # Mapping from OmniVoice list names to internal game codes
        mapping = {
            "Korean": "ko",
            "English": "en",
            "Japanese": "ja",
            "Russian": "ru",
            "Turkish": "tr",
            "Spanish": "es",
            "French": "fr",
            "German": "de",
            "Italian": "it",
            "Polish": "pl",
        }
        
        if lang in mapping:
            return mapping[lang]
            
        # Special cases and variations
        l = lang.lower()
        if "spanish" in l:
            return "es-mx" if "mx" in l else "es"
        if "chinese" in l:
            return "zh-tw" if "tw" in l else "zh-cn"
        if "portuguese" in l:
            return "pt-br"
            
        # Fallback to standard BCP-47 split
        return lang.split("-")[0].lower()

    def _is_omnivoice_provider(self, provider_id: str = "") -> bool:
        pid = provider_id or self._tts_provider.currentData() or ""
        return pid == "omnivoice_tts"

    @staticmethod
    def _sanitize_profile_name(text: str) -> str:
        import re
        value = re.sub(r"[^a-zA-Z0-9_-]+", "_", (text or "").strip())
        value = value.strip("_")
        return value[:80]

    def _suggest_omnivoice_profile_name(self, ae: AudioEntry) -> str:
        if not ae:
            return ""
        prefix = ae.voice_prefix or os.path.splitext(os.path.basename(ae.entry.path))[0]
        if prefix.startswith("unique_"):
            return self._sanitize_profile_name(prefix)
        if ae.npc_class:
            return self._sanitize_profile_name(f"{prefix}_{ae.npc_class}")
        return self._sanitize_profile_name(prefix)

    def _suggest_omnivoice_voice(self, ae: AudioEntry) -> str:
        if not ae:
            return "auto"
        if ae.voice_prefix.startswith("unique_"):
            profile = self._suggest_omnivoice_profile_name(ae)
            return f"clone:{profile}" if profile else "auto"
        if "female" in (ae.npc_gender or "").lower():
            if ae.npc_age == "adult":
                return "design:female,young adult,medium pitch"
            return "design:female"
        if "male" in (ae.npc_gender or "").lower() or ae.npc_age == "adult":
            return "design:male,young adult,low pitch"
        return "auto"

    def _persist_omnivoice_ui_state(self):
        self._config.set("tts.omnivoice_num_step", self._omnivoice_num_step.value())
        self._config.set("tts.omnivoice_guidance_scale", self._omnivoice_guidance.value())
        self._config.set("tts.omnivoice_denoise", self._omnivoice_denoise.isChecked())
        self._config.set("tts.omnivoice_duration_seconds", self._omnivoice_duration.value())
        self._config.set("tts.omnivoice_t_shift", self._omnivoice_t_shift.value())
        self._config.set("tts.omnivoice_position_temperature", self._omnivoice_position_temp.value())
        self._config.set("tts.omnivoice_class_temperature", self._omnivoice_class_temp.value())
        self._config.set("tts.omnivoice_clone_mode", self._omnivoice_mode.currentData() or "one_shot")
        self._config.set("tts.omnivoice_profile_name", self._omnivoice_profile_name.text().strip())
        self._config.set("tts.omnivoice_refresh_profile", self._omnivoice_refresh_profile.isChecked())
        self._config.set("tts.omnivoice_gender", self._omnivoice_gender.currentText())
        self._config.set("tts.omnivoice_age", self._omnivoice_age.currentText())
        self._config.set("tts.omnivoice_pitch", self._omnivoice_pitch.currentText())
        self._config.set("tts.omnivoice_style", self._omnivoice_style.currentText())
        self._config.set("tts.omnivoice_accent", self._omnivoice_accent.currentText())
        self._config.set("tts.omnivoice_gender_use", self._omnivoice_gender_use.isChecked())
        self._config.set("tts.omnivoice_age_use", self._omnivoice_age_use.isChecked())
        self._config.set("tts.omnivoice_pitch_use", self._omnivoice_pitch_use.isChecked())
        self._config.set("tts.omnivoice_style_use", self._omnivoice_style_use.isChecked())
        self._config.set("tts.omnivoice_accent_use", self._omnivoice_accent_use.isChecked())

    def _apply_omnivoice_ui_state(self):
        mode = self._config.get("tts.omnivoice_clone_mode", "one_shot")
        for i in range(self._omnivoice_mode.count()):
            if self._omnivoice_mode.itemData(i) == mode:
                self._omnivoice_mode.setCurrentIndex(i)
                break
        self._omnivoice_profile_name.setText(self._config.get("tts.omnivoice_profile_name", ""))
        self._omnivoice_refresh_profile.setChecked(bool(self._config.get("tts.omnivoice_refresh_profile", True)))
        self._omnivoice_gender.setCurrentText(self._config.get("tts.omnivoice_gender", "Auto"))
        self._omnivoice_age.setCurrentText(self._config.get("tts.omnivoice_age", "Auto"))
        self._omnivoice_pitch.setCurrentText(self._config.get("tts.omnivoice_pitch", "Auto"))
        self._omnivoice_style.setCurrentText(self._config.get("tts.omnivoice_style", "Auto"))
        self._omnivoice_accent.setCurrentText(self._config.get("tts.omnivoice_accent", "Auto"))

        self._omnivoice_gender_use.setChecked(bool(self._config.get("tts.omnivoice_gender_use", False)))
        self._omnivoice_age_use.setChecked(bool(self._config.get("tts.omnivoice_age_use", False)))
        self._omnivoice_pitch_use.setChecked(bool(self._config.get("tts.omnivoice_pitch_use", False)))
        self._omnivoice_style_use.setChecked(bool(self._config.get("tts.omnivoice_style_use", False)))
        self._omnivoice_accent_use.setChecked(bool(self._config.get("tts.omnivoice_accent_use", False)))

        # Explicitly apply enabled state based on loaded checkbox values
        self._omnivoice_gender.setEnabled(self._omnivoice_gender_use.isChecked())
        self._omnivoice_age.setEnabled(self._omnivoice_age_use.isChecked())
        self._omnivoice_pitch.setEnabled(self._omnivoice_pitch_use.isChecked())
        self._omnivoice_style.setEnabled(self._omnivoice_style_use.isChecked())
        self._omnivoice_accent.setEnabled(self._omnivoice_accent_use.isChecked())

    def _refresh_provider_specific_ui(self):
        is_omni = self._is_omnivoice_provider()
        self._omnivoice_clone_group.setVisible(is_omni)
        self._omnivoice_advanced_group.setVisible(is_omni)

        # Update language list based on provider
        self._tts_lang.blockSignals(True)
        current = self._tts_lang.currentText()
        self._tts_lang.clear()

        if is_omni:
            self._tts_lang.addItems(OMNIVOICE_LANGUAGES)
        else:
            self._tts_lang.addItems([
                "Auto",
                "en-US", "en-GB", "ar-SA", "ko-KR", "ja-JP", "zh-CN",
                "de-DE", "fr-FR", "es-ES", "it-IT", "pt-BR", "ru-RU",
            ])

        if current:
            self._tts_lang.setCurrentText(current)
        else:
            self._tts_lang.setCurrentText("Auto")
        self._tts_lang.blockSignals(False)

        if is_omni:
            self._apply_omnivoice_ui_state()
            self._check_omnivoice_server()
        else:
            self._tts_status_label.setText("Status: Ready")



    # ── Filtering ──

    def _apply_filter(self):
        cat = self._cat_filter.currentData() or ""
        lang = self._lang_filter.currentData() or ""
        search = self._search_input.text()
        self._model.set_filter(cat, lang, search)
        self._count_label.setText(f"{self._model.filtered_count:,} / {self._model.total_count:,}")

    # ── Playback + Text Display ──

    def _on_row_clicked(self, index: QModelIndex):
        ae = self._model.row_at(index.row())
        if ae:
            self._play_and_show(ae)
            self._update_tts_text_from_selection(ae)

    def _play_and_show(self, ae: AudioEntry):
        """Play audio and show linked text."""
        try:
            entry = ae.entry
            basename = os.path.basename(entry.path)

            # Cache decoded audio — key includes package group to distinguish languages
            ck = f"{ae.package_group}:{entry.path}"
            if ck in self._wav_cache:
                play_path = self._wav_cache[ck]
            else:
                # Use group prefix in temp filename to avoid overwriting between languages
                tmp = os.path.join(self._temp_dir, f"{ae.package_group}_{basename}")
                data = self._vfs.read_entry_data(entry)
                with open(tmp, "wb") as f:
                    f.write(data)
                play_path = tmp
                ext = os.path.splitext(basename)[1].lower()
                if ext in (".wem", ".bnk"):
                    wav_out = os.path.join(self._temp_dir, f"{ae.package_group}_{os.path.splitext(basename)[0]}.wav")
                    wav = wem_to_wav(tmp, wav_out)
                    if wav:
                        play_path = wav
                self._wav_cache[ck] = play_path

            self._audio_player.load_file(play_path)

            # Build text display
            lines = []
            lines.append(f"File: {entry.path}")
            display_lang = ae.voice_lang.upper()
            if ae.voice_lang.lower() == "ja":
                display_lang = "CH"
            lines.append(f"Language: {display_lang} | Category: {ae.category}")
            lines.append(f"NPC: {ae.npc_gender} {ae.npc_class} ({ae.npc_age})")
            lines.append(f"Paloc Key: {ae.paloc_key}")
            lines.append("")

            if ae.text_translations:
                lang_names = {
                    "ko": "Korean", "en": "English", "ja": "Chinois",
                    "ru": "Russian", "tr": "Turkish", "es": "Spanish",
                    "es-mx": "Spanish (MX)", "fr": "French", "de": "German",
                    "it": "Italian", "pl": "Polish", "pt-br": "Portuguese (BR)",
                    "zh-tw": "Chinese (TW)", "zh-cn": "Chinese (CN)",
                }
                for lang, text in sorted(ae.text_translations.items()):
                    name = lang_names.get(lang, lang.upper())
                    lines.append(f"[{name}] {text}")
            elif ae.text_original:
                lines.append(f"[Text] {ae.text_original}")
            else:
                lines.append("[No linked text found]")

            self._text_display.setPlainText("\n".join(lines))

            # Manual entry for generation prompt preferred by user
            # Main prompt (_tts_text) remains empty for user input

            self._autofill_omnivoice_context(ae)

            self._progress.set_status(f"Playing: {basename}")

        except Exception as e:
            self._progress.set_status(f"Error: {e}")
            logger.error("Audio play error: %s", e)

    # ── Context Menu ──

    def _show_context_menu(self, pos):
        idx = self._view.indexAt(pos)
        if not idx.isValid():
            return
        ae = self._model.row_at(idx.row())
        if not ae:
            return

        menu = QMenu(self)
        entry = ae.entry

        menu.addAction("Play").triggered.connect(lambda: self._play_and_show(ae))
        menu.addSeparator()
        menu.addAction("Export as WAV").triggered.connect(lambda: self._export_wav(entry))
        menu.addAction("Import WAV (replace)").triggered.connect(lambda: self._import_wav(entry))
        menu.addAction("Import WAV + Patch to Game").triggered.connect(lambda: self._import_and_patch(entry))
        menu.addSeparator()
        menu.addAction("Copy paloc key").triggered.connect(
            lambda: QApplication.clipboard().setText(ae.paloc_key))
        menu.addAction("Copy text").triggered.connect(
            lambda: QApplication.clipboard().setText(ae.text_original))
        menu.addAction("Copy file path").triggered.connect(
            lambda: QApplication.clipboard().setText(ae.entry.path))

        menu.exec(self._view.viewport().mapToGlobal(pos))

    # ── Export / Import ──

    def _export_wav(self, entry: PamtFileEntry):
        basename = os.path.splitext(os.path.basename(entry.path))[0]
        save = pick_save_file(self, "Export as WAV", f"{basename}.wav", filters="WAV (*.wav)")
        if not save:
            return
        try:
            data = self._vfs.read_entry_data(entry)
            tmp = os.path.join(self._temp_dir, os.path.basename(entry.path))
            with open(tmp, "wb") as f:
                f.write(data)
            ext = os.path.splitext(entry.path)[1].lower()
            if ext in (".wem", ".bnk"):
                r = wem_to_wav(tmp, save)
                if not r:
                    show_error(self, "Error", "WEM decode failed")
                    return
            else:
                import shutil
                shutil.copy2(tmp, save)
            show_info(self, "Exported", f"Saved to:\n{save}")
        except Exception as e:
            show_error(self, "Error", str(e))

    def _export_selected(self):
        out = pick_directory(self, "Export Directory")
        if not out:
            return
        rows = sorted({i.row() for i in self._view.selectedIndexes()})
        ok = err = 0
        for r in rows:
            ae = self._model.row_at(r)
            if not ae:
                continue
            try:
                data = self._vfs.read_entry_data(ae.entry)
                bn = os.path.splitext(os.path.basename(ae.entry.path))[0]
                tmp = os.path.join(self._temp_dir, os.path.basename(ae.entry.path))
                with open(tmp, "wb") as f:
                    f.write(data)
                ext = os.path.splitext(ae.entry.path)[1].lower()
                wav_out = os.path.join(out, f"{bn}.wav")
                if ext in (".wem", ".bnk"):
                    wem_to_wav(tmp, wav_out)
                else:
                    import shutil
                    shutil.copy2(tmp, wav_out)
                ok += 1
            except Exception:
                err += 1
        show_info(self, "Batch Export", f"Exported {ok} files ({err} errors)")

    def _import_wav(self, entry: PamtFileEntry):
        wav = pick_file(self, "Select WAV", filters="Audio (*.wav *.ogg *.mp3);;All (*.*)")
        if not wav:
            return
        try:
            from core.audio_importer import import_audio
            orig = self._vfs.read_entry_data(entry)
            new = import_audio(wav, entry, orig)
            show_info(self, "Imported",
                      f"Original: {format_file_size(len(orig))}\nNew: {format_file_size(len(new))}")
        except Exception as e:
            show_error(self, "Error", str(e))

    def _import_and_patch(self, entry: PamtFileEntry):
        wav = pick_file(self, "Select Audio File",
                        filters="Audio (*.wav *.ogg *.wem);;All (*.*)")
        if not wav:
            return
        try:
            from core.repack_engine import RepackEngine, ModifiedFile
            from core.audio_converter import wav_to_wem
            orig = self._vfs.read_entry_data(entry)

            ext = os.path.splitext(wav)[1].lower()
            if ext == ".wem":
                # Already WEM — use directly
                with open(wav, "rb") as f:
                    new = f.read()
            else:
                # Check Wwise
                from utils.wwise_installer import is_wwise_installed
                if not is_wwise_installed():
                    show_error(self, "Wwise Required",
                               "Audio patching requires Wwise (free) for Vorbis encoding.\n\n"
                               "Install from audiokinetic.com (free account)")
                    return
                # Convert WAV/OGG → WEM via Wwise
                self._progress.set_status("Converting to WEM (Vorbis) via Wwise...")
                QApplication.processEvents()
                wem_path = wav_to_wem(wav, orig)
                if not wem_path:
                    show_error(self, "Error", "Wwise conversion failed.")
                    return
                with open(wem_path, "rb") as f:
                    new = f.read()

            if not confirm_action(self, "Patch Audio",
                                  f"Replace {entry.path}?\n"
                                  f"Orig: {format_file_size(len(orig))}\n"
                                  f"New: {format_file_size(len(new))}"):
                return
            game = os.path.dirname(os.path.dirname(entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            grp = os.path.basename(os.path.dirname(entry.paz_file))
            pamt = self._vfs.load_pamt(grp)
            mf = ModifiedFile(data=new, entry=entry, pamt_data=pamt, package_group=grp)
            result = RepackEngine(game).repack([mf], papgt_path=papgt)
            if result.success:
                show_info(self, "Patched", f"Patched {entry.path}")
            else:
                show_error(self, "Error", "\n".join(result.errors) if getattr(result, "errors", None) else "Patch failed")
        except Exception as e:
            show_error(self, "Error", str(e))

    # ── TTS ──

    def _on_tts_provider_changed(self, _=None):
        pid = self._tts_provider.currentData()
        if not pid:
            return
        self._config.set("tts.active_provider", pid)
        self._refresh_provider_specific_ui()
        self._refresh_tts_models()
        self._refresh_tts_voices()

    def _refresh_tts_models(self, preferred_model: str = ""):
        if not self._tts_engine:
            return
        pid = self._tts_provider.currentData()
        if not pid:
            return

        model_key = self._model_config_key(pid)
        saved_model = preferred_model or self._config.get(model_key, "")

        self._tts_model.blockSignals(True)
        self._tts_model.clear()
        try:
            for model in self._tts_engine.list_models(pid):
                self._tts_model.addItem(model.name, model.model_id)
        except Exception:
            pass
        if self._tts_model.count() == 0 and saved_model:
            self._tts_model.addItem(saved_model, saved_model)
        elif self._tts_model.count() == 0:
            fallback = "omnivoice" if pid == "omnivoice_tts" else ""
            if fallback:
                self._tts_model.addItem(fallback, fallback)
        if saved_model:
            self._tts_model.setCurrentText(saved_model)
        elif self._tts_model.count():
            self._tts_model.setCurrentIndex(0)
        self._tts_model.blockSignals(False)

    def _refresh_tts_voices(self, preferred_voice: str = ""):
        if not self._tts_engine:
            return
        pid = self._tts_provider.currentData()
        if not pid:
            return

        self._voice_combo.blockSignals(True)
        self._voice_combo.clear()
        lang = self._current_tts_language_query()
        try:
            voices = self._tts_engine.list_voices(pid, lang)
            for voice in voices[:500]:
                label = voice.name
                if voice.gender:
                    label += f" ({voice.gender})"
                self._voice_combo.addItem(label, voice.voice_id)
        except Exception as e:
            logger.warning("Failed to refresh TTS voices for %s: %s", pid, e)
        if self._voice_combo.count() == 0:
            self._voice_combo.addItem("(enter custom voice)", "")
        if preferred_voice:
            self._voice_combo.setCurrentText(preferred_voice)
        elif self._is_omnivoice_provider():
            saved_voice = self._config.get("tts.omnivoice_voice_mode", "auto")
            self._voice_combo.setCurrentText(saved_voice)
        self._voice_combo.blockSignals(False)

    def _check_omnivoice_server(self):
        if not self._tts_engine or not self._is_omnivoice_provider():
            return
        provider = self._tts_engine.get_provider("omnivoice_tts")
        if not provider:
            return
        try:
            status = provider.get_status()
        except Exception as e:
            status = None
            self._tts_status_label.setText(f"Status: Offline ({e})")
            return

        if status and status.connected:
            pieces = ["Status: OmniVoice online"]
            if status.device:
                pieces.append(status.device)
            if status.model:
                pieces.append(status.model)
            self._tts_status_label.setText(" | ".join(pieces))
        else:
            msg = status.message if status else "unreachable"
            self._tts_status_label.setText(f"Status: OmniVoice offline ({msg})")

    def _browse_omnivoice_reference_audio(self):
        path = pick_file(self, "Select Reference Audio", filters="Audio (*.wav *.wem *.bnk *.ogg *.mp3);;All (*.*)")
        if path:
            self._omnivoice_ref_audio.setText(path)

    def _current_audio_entry(self) -> AudioEntry:
        rows = sorted({i.row() for i in self._view.selectedIndexes()})
        if rows:
            return self._model.row_at(rows[0])
        return None

    def _use_selected_audio_as_reference(self):
        ae = self._current_audio_entry()
        if not ae:
            show_error(self, "Reference Audio", "Select an audio row first.")
            return
        try:
            path = self._ensure_reference_audio_for_entry(ae)
            self._omnivoice_ref_audio.setText(path)
            
            # Force auto-fill reference transcript from entry's original text (matching the audio language)
            ref_text = ae.text_original or ""
            self._omnivoice_ref_text.setPlainText(ref_text)
                
            self._autofill_omnivoice_context(ae)
            self._progress.set_status(f"Reference ready: {os.path.basename(path)}")
        except Exception as e:
            show_error(self, "Reference Audio", str(e))

    def _update_tts_text_from_selection(self, ae: AudioEntry = None):
        if not ae:
            ae = self._current_audio_entry()
        if not ae:
            return
            
        # If user hasn't manually typed anything or we are auto-filling...
        # We'll overwrite the text if it's currently empty or previously auto-filled.
        # But for simplicity as requested, we'll sync it when clicked.
        text = self._build_tts_text_for_entry(ae)
        if text:
            self._tts_text.setPlainText(text)

    def _on_tts_lang_changed(self, lang: str):
        ae = self._current_audio_entry()
        if ae:
            self._update_tts_text_from_selection(ae)

    def _ensure_reference_audio_for_entry(self, ae: AudioEntry) -> str:
        entry = ae.entry
        basename = os.path.basename(entry.path)
        cache_key = f"ref:{ae.package_group}:{entry.path}"
        cached = self._wav_cache.get(cache_key)
        if cached and os.path.isfile(cached):
            return cached

        tmp = os.path.join(self._temp_dir, f"ref_{ae.package_group}_{basename}")
        data = self._vfs.read_entry_data(entry)
        with open(tmp, "wb") as f:
            f.write(data)

        ext = os.path.splitext(basename)[1].lower()
        out = tmp
        if ext in (".wem", ".bnk"):
            wav_out = os.path.join(self._temp_dir, f"ref_{ae.package_group}_{os.path.splitext(basename)[0]}.wav")
            out = wem_to_wav(tmp, wav_out) or ""
            if not out:
                raise RuntimeError(f"Failed to decode reference audio: {entry.path}")
        elif ext != ".wav":
            wav_out = os.path.join(self._temp_dir, f"ref_{ae.package_group}_{os.path.splitext(basename)[0]}.wav")
            out = audio_to_wav(tmp, wav_out) or ""
            if not out:
                raise RuntimeError(f"Failed to convert reference audio: {entry.path}")

        self._wav_cache[cache_key] = out
        return out

    def _autofill_omnivoice_context(self, ae: AudioEntry):
        if not ae or not self._is_omnivoice_provider():
            return
        if self._config.get("tts.omnivoice_auto_reference", True):
            try:
                self._omnivoice_ref_audio.setText(self._ensure_reference_audio_for_entry(ae))
            except Exception:
                pass
        
        # Sync reference transcript with current selection's original text
        self._omnivoice_ref_text.setPlainText(ae.text_original or "")

        if not self._omnivoice_profile_name.text().strip():
            self._omnivoice_profile_name.setText(self._suggest_omnivoice_profile_name(ae))
        if not (self._voice_combo.currentText() or "").strip():
            self._voice_combo.setCurrentText(self._suggest_omnivoice_voice(ae))
        elif (self._voice_combo.currentText() or "").strip() in {"auto", "design:"}:
            self._voice_combo.setCurrentText(self._suggest_omnivoice_voice(ae))

    def _save_omnivoice_profile(self):
        if not self._tts_engine or not self._is_omnivoice_provider():
            return
        profile_name = self._sanitize_profile_name(self._omnivoice_profile_name.text())
        ref_audio = self._omnivoice_ref_audio.text().strip()
        if not profile_name:
            show_error(self, "OmniVoice", "Enter a profile name first.")
            return
        if not ref_audio:
            show_error(self, "OmniVoice", "Select or auto-fill a reference audio file first.")
            return
        provider = self._tts_engine.get_provider("omnivoice_tts")
        try:
            provider.save_profile(
                profile_name,
                ref_audio,
                ref_text=self._omnivoice_ref_text.toPlainText().strip(),
                overwrite=True,
            )
            self._voice_combo.setCurrentText(f"clone:{profile_name}")
            self._progress.set_status(f"Saved OmniVoice profile: {profile_name}")
            self._refresh_tts_voices(preferred_voice=f"clone:{profile_name}")
        except Exception as e:
            show_error(self, "OmniVoice", str(e))

    def _selected_model_id(self) -> str:
        return (self._tts_model.currentData() or self._tts_model.currentText() or "").strip()

    def _selected_voice_id(self) -> str:
        return (self._voice_combo.currentData() or self._voice_combo.currentText() or "").strip()

    def _build_tts_text_for_entry(self, ae: AudioEntry, language_override: str = "") -> str:
        lang_code = language_override or self._current_tts_language_code()
        if lang_code:
            text = ae.text_translations.get(lang_code, "")
            if not text:
                for key, value in ae.text_translations.items():
                    if key.startswith(lang_code):
                        text = value
                        break
            # If a specific language was requested but not found, return empty (per user request)
            return text if text else ""
            
        # No specific language requested (Auto), return original text
        return ae.text_original or ""

    def _build_omnivoice_options(self, ae: AudioEntry = None, batch_mode: bool = False) -> dict:
        self._persist_omnivoice_ui_state()
        mode = self._omnivoice_mode.currentData() or "one_shot"
        profile_name = self._sanitize_profile_name(
            self._omnivoice_profile_name.text().strip() or
            (self._suggest_omnivoice_profile_name(ae) if ae else "")
        )
        ref_audio_path = self._omnivoice_ref_audio.text().strip()
        if batch_mode and mode == "one_shot" and ae:
            try:
                ref_audio_path = self._ensure_reference_audio_for_entry(ae)
            except Exception:
                ref_audio_path = ref_audio_path

        options = {
            "clone_mode": mode,
            "profile_id": profile_name,
            "ref_audio_path": ref_audio_path,
            "ref_text": self._omnivoice_ref_text.toPlainText().strip(),
            "refresh_profile": self._omnivoice_refresh_profile.isChecked(),
            "overwrite_profile": True,
            "num_step": self._omnivoice_num_step.value(),
            "guidance_scale": self._omnivoice_guidance.value(),
            "denoise": self._omnivoice_denoise.isChecked(),
            "duration": self._omnivoice_duration.value(),
            "t_shift": self._omnivoice_t_shift.value(),
            "position_temperature": self._omnivoice_position_temp.value(),
            "class_temperature": self._omnivoice_class_temp.value(),
            "param_9": self._omnivoice_gender.currentText() if self._omnivoice_gender_use.isChecked() else "Auto",
            "param_10": self._omnivoice_age.currentText() if self._omnivoice_age_use.isChecked() else "Auto",
            "param_11": self._omnivoice_pitch.currentText() if self._omnivoice_pitch_use.isChecked() else "Auto",
            "param_12": self._omnivoice_style.currentText() if self._omnivoice_style_use.isChecked() else "Auto",
            "param_13": self._omnivoice_accent.currentText() if self._omnivoice_accent_use.isChecked() else "Auto",
            "response_format": "wav",
            "stream": False,
        }
        if mode == "voice" and ae:
            suggested = self._suggest_omnivoice_voice(ae)
            if not self._selected_voice_id():
                self._voice_combo.setCurrentText(suggested)
        self._config.set("tts.omnivoice_voice_mode", self._selected_voice_id() or "auto")
        return options

    def _write_tts_result_audio(self, result, text: str, subdir: str = "") -> str:
        import time as _time

        output_dir = self._temp_dir
        if subdir:
            output_dir = os.path.join(self._temp_dir, subdir)
            os.makedirs(output_dir, exist_ok=True)
        ext = result.audio_format or "wav"
        raw_path = os.path.join(output_dir, f"tts_{int(_time.time() * 1000)}.{ext}")
        with open(raw_path, "wb") as f:
            f.write(result.audio_data)

        final_path = raw_path
        if ext.lower() != "wav":
            wav_path = os.path.splitext(raw_path)[0] + ".wav"
            converted = audio_to_wav(raw_path, wav_path)
            if converted:
                final_path = converted
        return final_path

    def _generate_tts(self):
        text = self._tts_text.toPlainText().strip()
        if not text:
            show_error(self, "TTS Error", "Generate file audio failed, please put text on the text section and try again.")
            return
        if not self._tts_engine:
            return

        pid = self._tts_provider.currentData() or "edge_tts"
        model = self._selected_model_id() or self._config.get(self._model_config_key(pid), "")
        voice = self._selected_voice_id() or ""
        lang = self._current_tts_language_query()
        spd = self._speed.value() / 100.0
        ae = self._current_audio_entry()
        options = self._build_omnivoice_options(ae=ae) if self._is_omnivoice_provider(pid) else None

        self._progress.set_status("Generating TTS...")
        QApplication.processEvents()

        result = self._tts_engine.synthesize(text, pid, model, voice, lang, spd, options=options)
        if result.success and result.audio_data:
            try:
                path = self._write_tts_result_audio(result, text)
            except Exception as e:
                show_error(self, "TTS Error", f"Audio normalization failed: {e}")
                return

            self._audio_player.load_file(path)
            voice_name = (self._voice_combo.currentText() or result.voice or pid).split(" (")[0]
            item = QListWidgetItem(
                f"{voice_name} | {format_file_size(os.path.getsize(path))} | "
                f"{result.latency_ms:.0f}ms\n{text[:50]}"
            )
            item.setData(Qt.UserRole, path)
            self._gen_list.insertItem(0, item)
            self._generated_files.insert(0, {
                "path": path,
                "text": text,
                "provider": pid,
                "voice": result.voice,
                "model": model,
            })
            self._progress.set_status(f"Generated: {os.path.basename(path)}")
        else:
            show_error(self, "TTS Error", result.error or "Failed")

    def _generate_and_patch(self):
        rows = sorted({i.row() for i in self._view.selectedIndexes()})
        if not rows:
            show_error(self, "Error", "Select an audio file first")
            return
        ae = self._model.row_at(rows[0])
        if not ae:
            return

        self._generate_tts()
        if not self._generated_files:
            return

        tts_wav_path = self._generated_files[0]["path"]
        try:
            from core.repack_engine import RepackEngine, ModifiedFile
            from core.audio_converter import wav_to_wem
            entry = ae.entry
            orig_data = self._vfs.read_entry_data(entry)

            from utils.wwise_installer import is_wwise_installed
            if not is_wwise_installed():
                show_error(self, "Wwise Required",
                           "Audio patching requires Wwise (free) for Vorbis encoding.\n\n"
                           "The game only accepts Vorbis-encoded WEM audio.\n"
                           "Without Wwise, patched audio will be silent in-game.\n\n"
                           "Install Wwise:\n"
                           "1. Go to audiokinetic.com and create a free account\n"
                           "2. Download the Audiokinetic Launcher\n"
                           "3. Install Wwise (any version, ~2GB)\n"
                           "4. Restart CrimsonForge — it will auto-detect Wwise")
                return

            self._progress.set_status("Converting WAV to WEM (Vorbis)...")
            QApplication.processEvents()

            wem_path = wav_to_wem(tts_wav_path, orig_data)
            if not wem_path or not os.path.isfile(wem_path):
                show_error(self, "Error",
                           "WAV to WEM conversion failed.\n"
                           "Check Wwise installation and try again.")
                return

            with open(wem_path, "rb") as f:
                new_data = f.read()

            orig_text = ae.text_original or "[No text linked]"
            new_text = self._tts_text.toPlainText().strip()
            
            if not confirm_action(self, "Patch Audio",
                                  f"Replace {entry.path}?\n\n"
                                  f"Original Dialogue:\n{orig_text[:200]}\n\n"
                                  f"New TTS Text:\n{new_text[:200]}\n\n"
                                  f"Original: {format_file_size(len(orig_data))} (WEM Vorbis)\n"
                                  f"New: {format_file_size(len(new_data))} (WEM Vorbis)\n\n"
                                  f"The generated audio will be written directly into the game archive."):
                return

            game = os.path.dirname(os.path.dirname(entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            grp = os.path.basename(os.path.dirname(entry.paz_file))
            pamt = self._vfs.load_pamt(grp)
            mf = ModifiedFile(data=new_data, entry=entry, pamt_data=pamt, package_group=grp)

            self._progress.set_status("Patching to game...")
            QApplication.processEvents()

            result = RepackEngine(game).repack([mf], papgt_path=papgt)
            if result.success:
                self._progress.set_status(f"Patched: {entry.path}")
                show_info(self, "Patched",
                          f"TTS audio patched to {entry.path}\n\n"
                          f"Original: {format_file_size(len(orig_data))}\n"
                          f"New: {format_file_size(len(new_data))}\n\n"
                          f"Launch the game to hear your changes!")
            else:
                show_error(self, "Error", "\n".join(result.errors) if getattr(result, "errors", None) else "Patch failed")
        except Exception as e:
            show_error(self, "Error", str(e))

    def _resolve_batch_entries(self) -> list[AudioEntry]:
        selected_rows = sorted({i.row() for i in self._view.selectedIndexes()})
        if selected_rows:
            return [ae for ae in (self._model.row_at(r) for r in selected_rows) if ae]
        return [self._model.row_at(i) for i in range(self._model.filtered_count) if self._model.row_at(i)]

    def _batch_generate(self):
        entries = self._resolve_batch_entries()
        if not entries:
            show_error(self, "Batch Generate", "Select audio rows or apply a filter first.")
            return
        out_dir = pick_directory(self, "Batch Generate Output")
        if not out_dir:
            return
        self._start_batch_worker("generate", entries, out_dir=out_dir)

    def _batch_generate_and_patch(self):
        entries = self._resolve_batch_entries()
        if not entries:
            show_error(self, "Generate All + Patch", "Select audio rows or apply a filter first.")
            return
        self._start_batch_worker("patch", entries)

    def _start_batch_worker(self, mode: str, entries: list[AudioEntry], out_dir: str = ""):
        if self._batch_worker and self._batch_worker.isRunning():
            show_error(self, "Batch Operation", "Another batch job is already running.")
            return

        pid = self._tts_provider.currentData() or "edge_tts"
        if mode == "patch":
            from utils.wwise_installer import is_wwise_installed
            if not is_wwise_installed():
                show_error(self, "Wwise Required",
                           "Batch patching requires Wwise for Vorbis WEM encoding.")
                return

        payload = {
            "provider_id": pid,
            "model_id": self._selected_model_id() or self._config.get(self._model_config_key(pid), ""),
            "voice_id": self._selected_voice_id() or "",
            "language": self._current_tts_language_query(),
            "speed": self._speed.value() / 100.0,
            "mode": mode,
            "entries": entries,
            "out_dir": out_dir,
            "omnivoice": self._build_omnivoice_options(batch_mode=True) if self._is_omnivoice_provider(pid) else None,
            "config_data": self._config.data,
        }

        self._progress.set_indeterminate("Starting batch operation...")
        self._batch_worker = FunctionWorker(self._batch_worker_task, payload)
        self._batch_worker.progress.connect(lambda pct, msg: self._progress.set_progress(pct, msg))
        self._batch_worker.finished_result.connect(self._on_batch_finished)
        self._batch_worker.error_occurred.connect(lambda msg: show_error(self, "Batch Operation", msg))
        self._batch_worker.error_occurred.connect(lambda _: self._progress.reset())
        self._batch_worker.start()

    def _batch_worker_task(self, worker, payload: dict):
        from ai.tts_engine import TTSEngine
        from core.repack_engine import ModifiedFile, RepackEngine
        from core.audio_converter import wav_to_wem

        engine = TTSEngine()
        engine.initialize_from_config(payload["config_data"])
        entries: list[AudioEntry] = payload["entries"]
        provider_id = payload["provider_id"]
        model_id = payload["model_id"]
        voice_id = payload["voice_id"]
        language = payload["language"]
        speed = payload["speed"]
        mode = payload["mode"]
        out_dir = payload["out_dir"]
        omni_base = dict(payload.get("omnivoice") or {})

        generated = []
        modified_files = []
        errors = []
        pamt_cache = {}
        total = max(len(entries), 1)

        if mode == "generate":
            os.makedirs(out_dir, exist_ok=True)

        for idx, ae in enumerate(entries, start=1):
            if worker.is_cancelled():
                return {"cancelled": True}

            text = self._build_tts_text_for_entry(ae, language_override=language.split("-")[0].lower() if language else "")
            if not text:
                errors.append(f"{ae.entry.path}: no linked text available")
                worker.report_progress(int((idx / total) * 45), f"Skipping {idx}/{total}: no text")
                continue

            worker.report_progress(int(((idx - 1) / total) * 45), f"Generating {idx}/{total}: {os.path.basename(ae.entry.path)}")

            options = dict(omni_base)
            entry_voice_id = voice_id
            if provider_id == "omnivoice_tts":
                if options.get("clone_mode") == "one_shot":
                    try:
                        options["ref_audio_path"] = self._ensure_reference_audio_for_entry(ae)
                    except Exception as e:
                        errors.append(f"{ae.entry.path}: {e}")
                        continue
                elif options.get("clone_mode") == "saved_profile" and not options.get("profile_id"):
                    options["profile_id"] = self._suggest_omnivoice_profile_name(ae)
                if entry_voice_id in {"", "auto", "design:"}:
                    entry_voice_id = self._suggest_omnivoice_voice(ae)

            result = engine.synthesize(text, provider_id, model_id, entry_voice_id, language, speed, options=options)
            if not result.success or not result.audio_data:
                errors.append(f"{ae.entry.path}: {result.error or 'synthesis failed'}")
                continue

            out_path = self._write_tts_result_audio(result, text, subdir="batch")
            generated.append({"entry": ae.entry.path, "path": out_path, "text": text})

            if mode == "generate":
                final_name = os.path.splitext(os.path.basename(ae.entry.path))[0] + ".wav"
                final_path = os.path.join(out_dir, final_name)
                import shutil
                shutil.copy2(out_path, final_path)
            else:
                orig_data = self._vfs.read_entry_data(ae.entry)
                wem_path = wav_to_wem(out_path, orig_data)
                if not wem_path or not os.path.isfile(wem_path):
                    errors.append(f"{ae.entry.path}: WAV to WEM conversion failed")
                    continue
                with open(wem_path, "rb") as f:
                    new_data = f.read()
                grp = os.path.basename(os.path.dirname(ae.entry.paz_file))
                pamt = pamt_cache.get(grp)
                if pamt is None:
                    pamt = self._vfs.load_pamt(grp)
                    pamt_cache[grp] = pamt
                modified_files.append(ModifiedFile(data=new_data, entry=ae.entry, pamt_data=pamt, package_group=grp))

        if mode == "patch" and modified_files:
            first_entry = entries[0].entry
            game = os.path.dirname(os.path.dirname(first_entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            repack = RepackEngine(game)
            repack_result = repack.repack(
                modified_files,
                papgt_path=papgt,
                progress_callback=lambda pct, msg: worker.report_progress(45 + int(pct * 0.55), msg),
            )
            if not repack_result.success:
                errors.extend(repack_result.errors)

        return {
            "mode": mode,
            "generated": generated,
            "errors": errors,
            "patched": len(modified_files),
            "output_dir": out_dir,
            "total": len(entries),
        }

    def _on_batch_finished(self, result: dict):
        self._progress.reset()
        if result.get("cancelled"):
            self._progress.set_status("Batch cancelled")
            return

        for item in reversed(result.get("generated", [])[:25]):
            path = item["path"]
            list_item = QListWidgetItem(
                f"Batch | {os.path.basename(path)} | {item['text'][:50]}"
            )
            list_item.setData(Qt.UserRole, path)
            self._gen_list.insertItem(0, list_item)
            self._generated_files.insert(0, {"path": path, "text": item["text"]})

        total = result.get("total", 0)
        errors = result.get("errors", [])
        if result.get("mode") == "generate":
            show_info(
                self,
                "Batch Generate",
                f"Generated {len(result.get('generated', []))}/{total} WAV files.\n"
                f"Output: {result.get('output_dir')}\n"
                f"Errors: {len(errors)}"
            )
        else:
            show_info(
                self,
                "Generate All + Patch",
                f"Generated and patched {result.get('patched', 0)}/{total} entries.\n"
                f"Errors: {len(errors)}"
            )
        if errors:
            logger.warning("Batch TTS completed with %d errors: %s", len(errors), errors[:10])

    def _play_generated_legacy_unused(self, item):
        path = item.data(Qt.UserRole)
        if path and os.path.isfile(path):
            self._audio_player.load_file(path)

    def _clear_generated_legacy_unused(self):
        self._gen_list.clear()
        self._generated_files.clear()


    def _generate_and_patch_legacy_unused(self):
        rows = sorted({i.row() for i in self._view.selectedIndexes()})
        if not rows:
            show_error(self, "Error", "Select an audio file first")
            return
        ae = self._model.row_at(rows[0])
        if not ae:
            return

        self._generate_tts()
        if not self._generated_files:
            return

        tts_wav_path = self._generated_files[0]["path"]
        try:
            from core.repack_engine import RepackEngine, ModifiedFile
            from core.audio_converter import wav_to_wem
            entry = ae.entry
            orig_data = self._vfs.read_entry_data(entry)

            # Convert TTS WAV → WEM (matching original format: Vorbis, 48kHz, mono)
            # Check if Wwise is installed for proper Vorbis encoding
            from utils.wwise_installer import is_wwise_installed
            if not is_wwise_installed():
                show_error(self, "Wwise Required",
                           "Audio patching requires Wwise (free) for Vorbis encoding.\n\n"
                           "The game only accepts Vorbis-encoded WEM audio.\n"
                           "Without Wwise, patched audio will be silent in-game.\n\n"
                           "Install Wwise:\n"
                           "1. Go to audiokinetic.com and create a free account\n"
                           "2. Download the Audiokinetic Launcher\n"
                           "3. Install Wwise (any version, ~2GB)\n"
                           "4. Restart CrimsonForge — it will auto-detect Wwise")
                return

            self._progress.set_status("Converting WAV to WEM (Vorbis)...")
            QApplication.processEvents()

            wem_path = wav_to_wem(tts_wav_path, orig_data)
            if not wem_path or not os.path.isfile(wem_path):
                show_error(self, "Error",
                           "WAV to WEM conversion failed.\n"
                           "Check Wwise installation and try again.")
                return

            with open(wem_path, "rb") as f:
                new_data = f.read()

            if not confirm_action(self, "Patch Audio",
                                  f"Replace {entry.path}?\n\n"
                                  f"Original: {format_file_size(len(orig_data))} (WEM Vorbis)\n"
                                  f"New: {format_file_size(len(new_data))} (WEM Vorbis)\n\n"
                                  f"ffmpeg converted TTS audio to match game format."):
                return

            game = os.path.dirname(os.path.dirname(entry.paz_file))
            papgt = os.path.join(game, "meta", "0.papgt")
            grp = os.path.basename(os.path.dirname(entry.paz_file))
            pamt = self._vfs.load_pamt(grp)
            mf = ModifiedFile(data=new_data, entry=entry, pamt_data=pamt, package_group=grp)

            self._progress.set_status("Patching to game...")
            QApplication.processEvents()

            result = RepackEngine(game).repack([mf], papgt_path=papgt)
            if result.success:
                self._progress.set_status(f"Patched: {entry.path}")
                show_info(self, "Patched",
                          f"TTS audio patched to {entry.path}\n\n"
                          f"Original: {format_file_size(len(orig_data))}\n"
                          f"New: {format_file_size(len(new_data))}\n\n"
                          f"Launch the game to hear your changes!")
            else:
                show_error(self, "Error", "\n".join(result.errors) if getattr(result, "errors", None) else "Patch failed")
        except Exception as e:
            show_error(self, "Error", str(e))

    def _play_generated(self, item):
        path = item.data(Qt.UserRole)
        if path and os.path.isfile(path):
            self._audio_player.load_file(path)

    def _clear_generated(self):
        self._gen_list.clear()
        self._generated_files.clear()
