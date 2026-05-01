"""Audio index engine — maps WEM voice files to paloc text keys.

Reverse-engineered mapping rule:
  Audio filename: nhm_adult_noble_1_questdialog_hello_00000.wem
                  └── voice prefix ──┘ └── paloc key ────────┘

  Strip the NPC voice prefix → the rest IS the paloc localization key.
  94.9% of voice audio files match a paloc entry this way.

Voice language identification:
  Package 0004 → SFX/Music (no voice, numbered IDs)
  Package 0005 → Korean voice (original)
  Package 0006 → Japanese voice
  Package 0035 → English voice

Audio categories (from key prefix):
  questdialog_hello_*        → Quest Greeting
  questdialog_main_*         → Quest Main Dialogue
  questdialog_contents_*     → Quest Side Content
  questdialog_quest_*        → Quest Lines
  aidialogstringinfo_*       → AI Ambient (single NPC)
  aidialogstringinfogroup_*  → AI Ambient (group)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from core.vfs_manager import VfsManager
from core.pamt_parser import PamtFileEntry
from utils.logger import get_logger

logger = get_logger("core.audio_index")

# Voice language packages
# Fixed mapping for Crimson Desert v1.2.0:
#   0005 = Korean (original)
#   0006 = English
#   0035 = Chinese (CH)
VOICE_LANG_PACKAGES = {
    "0005": "ko",
    "0006": "en",
    "0035": "ch",
}

# SFX/Music packages (no voice language)
SFX_PACKAGES = {"0004"}

# Key prefixes that indicate dialogue categories
DIALOGUE_PREFIXES = [
    "questdialog", "aidialogstringinfo", "aidialogstringinfogroup",
]

# Category mapping from key prefix parts
CATEGORY_MAP = {
    ("questdialog", "hello"): "Quest Greeting",
    ("questdialog", "main"): "Quest Main Dialogue",
    ("questdialog", "contents"): "Quest Side Content",
    ("questdialog", "quest"): "Quest Lines",
    ("questdialog", "day2"): "Quest Day 2",
    ("questdialog", "pywel"): "Quest (Pywel)",
    ("aidialogstringinfo", "friendly"): "AI Friendly",
    ("aidialogstringinfo", "criminal"): "AI Criminal",
    ("aidialogstringinfo", "give"): "AI Trading",
    ("aidialogstringinfo", "general"): "AI General",
    ("aidialogstringinfogroup", "blame"): "AI Blame",
    ("aidialogstringinfogroup", "bump"): "AI Bump",
    ("aidialogstringinfogroup", "cheerup"): "AI Cheerful",
    ("aidialogstringinfogroup", "bindback"): "AI Callback",
    ("aidialogstringinfogroup", "dev"): "AI Dev/Debug",
    ("aidialogstringinfogroup", "criminal"): "AI Criminal (Group)",
}

# NPC voice type codes
VOICE_GENDER = {
    "nhm": "Human Male",
    "nhw": "Human Female",
    "ndm": "Dwarf Male",
    "ndw": "Dwarf Female",
    "ngm": "Giant Male",
    "ngw": "Giant Female",
    "ntm": "Troll Male",
    "ntw": "Troll Female",
}


@dataclass
class AudioEntry:
    """A single indexed audio file with all metadata."""
    entry: PamtFileEntry        # Original PAMT entry
    package_group: str          # Package group ID (0005, 0006, etc.)
    voice_lang: str             # Voice language: "ko", "ja", "en", or ""
    voice_prefix: str           # NPC voice: "nhm_adult_noble_1"
    paloc_key: str              # Matching paloc key: "questdialog_hello_00000"
    category: str               # Category: "Quest Greeting", "AI Friendly", etc.
    npc_gender: str             # "Human Male", "Human Female", etc.
    npc_class: str              # "noble", "citizen", "soldier", etc.
    npc_age: str                # "adult", "child", "elder"
    # Text from paloc (populated after linking)
    text_original: str = ""     # Text in the voice's language
    text_translations: dict = field(default_factory=dict)  # {lang_code: text}


def parse_audio_filename(filename: str):
    """Parse an audio filename into voice prefix and paloc key.

    Args:
        filename: e.g. "nhm_adult_noble_1_questdialog_hello_00000"

    Returns:
        (voice_prefix, paloc_key, npc_gender, npc_class, npc_age, category)
    """
    parts = filename.split("_")

    voice_prefix = ""
    paloc_key = ""
    npc_gender = ""
    npc_class = ""
    npc_age = ""
    category = ""

    # Find where the dialogue key starts
    key_start = -1
    for i, p in enumerate(parts):
        if p in ("questdialog", "aidialogstringinfo", "aidialogstringinfogroup"):
            key_start = i
            break
        # Also check for other patterns like "memory", "faction", "general"
        if p in ("memory", "faction", "general", "extinguishedfire"):
            key_start = i
            break

    if key_start < 0:
        return filename, "", "", "", "", "Other"

    voice_prefix = "_".join(parts[:key_start])
    paloc_key = "_".join(parts[key_start:])

    # Parse voice prefix: {gender_code}_{age}_{class}_{variant}
    if len(parts) >= 1 and parts[0] in VOICE_GENDER:
        npc_gender = VOICE_GENDER[parts[0]]
    if len(parts) >= 2:
        npc_age = parts[1]
    if len(parts) >= 3 and key_start >= 3:
        npc_class = parts[2]
    if parts[0] == "unique":
        npc_gender = "Unique NPC"
        npc_class = parts[1] if len(parts) > 1 else ""

    # Determine category
    cat_prefix = parts[key_start]
    cat_sub = parts[key_start + 1] if key_start + 1 < len(parts) else ""
    category = CATEGORY_MAP.get((cat_prefix, cat_sub), "")
    if not category:
        if cat_prefix == "questdialog":
            category = "Quest Dialogue"
        elif cat_prefix == "aidialogstringinfo":
            category = "AI Ambient"
        elif cat_prefix == "aidialogstringinfogroup":
            category = "AI Ambient (Group)"
        else:
            category = "Other"

    return voice_prefix, paloc_key, npc_gender, npc_class, npc_age, category


def build_audio_index(vfs: VfsManager, groups: list[str],
                      paloc_entries: dict = None,
                      progress_callback=None) -> list[AudioEntry]:
    """Build the complete audio index from game archives.

    Args:
        vfs: VfsManager with loaded game.
        groups: List of package group IDs.
        paloc_entries: Optional dict of {paloc_key: {lang: text}} for linking.
        progress_callback: Optional (percent, message) callback.

    Returns:
        List of AudioEntry objects with all metadata populated.
    """
    audio_exts = {".wem", ".bnk", ".wav", ".ogg"}
    entries = []

    voice_groups = [g for g in groups if g in VOICE_LANG_PACKAGES]
    sfx_groups = [g for g in groups if g in SFX_PACKAGES]
    other_groups = [g for g in groups
                    if g not in VOICE_LANG_PACKAGES and g not in SFX_PACKAGES]

    # Process voice groups first (these have the mapping)
    all_groups = voice_groups + sfx_groups + other_groups
    total = len(all_groups)

    for gi, group in enumerate(all_groups):
        try:
            pamt = vfs.load_pamt(group)
            lang = VOICE_LANG_PACKAGES.get(group, "")

            for entry in pamt.file_entries:
                ext = os.path.splitext(entry.path.lower())[1]
                if ext not in audio_exts:
                    continue

                basename = os.path.splitext(os.path.basename(entry.path))[0]

                if group in VOICE_LANG_PACKAGES:
                    voice_prefix, paloc_key, npc_gender, npc_class, npc_age, category = \
                        parse_audio_filename(basename)
                else:
                    voice_prefix = ""
                    paloc_key = ""
                    npc_gender = ""
                    npc_class = ""
                    npc_age = ""
                    category = "SFX" if group in SFX_PACKAGES else "Other"

                ae = AudioEntry(
                    entry=entry,
                    package_group=group,
                    voice_lang=lang,
                    voice_prefix=voice_prefix,
                    paloc_key=paloc_key,
                    category=category,
                    npc_gender=npc_gender,
                    npc_class=npc_class,
                    npc_age=npc_age,
                )

                # Link to paloc text
                if paloc_entries and paloc_key:
                    key_lower = paloc_key.lower()
                    if key_lower in paloc_entries:
                        text_data = paloc_entries[key_lower]
                        if isinstance(text_data, dict):
                            ae.text_translations = text_data
                            ae.text_original = text_data.get(lang, "")
                        elif isinstance(text_data, str):
                            ae.text_original = text_data

                entries.append(ae)

        except Exception as e:
            logger.warning("Error indexing audio group %s: %s", group, e)

        if progress_callback and total > 0:
            progress_callback(int(((gi + 1) / total) * 100),
                               f"Indexing audio: group {group}")

    linked = sum(1 for e in entries if e.text_original)
    pct = (linked / len(entries) * 100.0) if entries else 0.0
    logger.info("Audio index built: %d entries, %d with paloc text (%.1f%%)",
                len(entries), linked, pct)
    return entries


def build_paloc_lookup(vfs: VfsManager, groups: list[str],
                       progress_callback=None) -> dict:
    """Build a paloc key → {lang: text} lookup dict from all paloc files.

    Returns:
        Dict: {paloc_key_lower: {lang_code: text_value}}
    """
    from core.paloc_parser import parse_paloc

    lookup = {}  # key → {lang: text}
    lang_map = {
        "kor": "ko", "eng": "en", "jpn": "ja", "rus": "ru",
        "tur": "tr", "spa-es": "es", "spa-mx": "es-mx",
        "fre": "fr", "ger": "de", "ita": "it", "pol": "pl",
        "por-br": "pt-br", "zho-tw": "zh-tw", "zho-cn": "zh-cn",
    }

    for group in groups:
        try:
            pamt = vfs.load_pamt(group)
            for entry in pamt.file_entries:
                path_lower = entry.path.lower()
                if not path_lower.endswith(".paloc"):
                    continue

                # Extract language from filename
                basename = os.path.splitext(os.path.basename(entry.path))[0].lower()
                lang = ""
                for suffix, lang_code in lang_map.items():
                    if basename.endswith(f"_{suffix}"):
                        lang = lang_code
                        break
                if not lang:
                    lang_suffix = basename.split("_")[-1]
                    lang = lang_map.get(lang_suffix, "")
                if not lang:
                    lang = "generic"

                try:
                    data = vfs.read_entry_data(entry)
                    paloc_entries = parse_paloc(data)
                    for pe in paloc_entries:
                        key = pe.key.lower()
                        if key not in lookup:
                            lookup[key] = {}
                        lookup[key][lang] = pe.value
                except Exception:
                    pass

        except Exception:
            pass

        if progress_callback:
            progress_callback(0, f"Loading paloc: {group}")

    logger.info("Paloc lookup built: %d keys", len(lookup))
    return lookup


def build_audio_index_cached(
    vfs: VfsManager,
    groups: list[str],
    progress_callback=None,
) -> list[AudioEntry]:
    """Disk-cached wrapper that does paloc lookup + audio index together.

    The Audio tab's slow path (~30-90 s on a full game install) runs
    :func:`build_paloc_lookup` over ~14 paloc files and then
    :func:`build_audio_index` over hundreds of thousands of voice
    entries. Both produce data that's expensive to re-compute but
    cheap to serialize. We pickle the linked AudioEntry list keyed
    on the fingerprint of every PAMT we'd otherwise re-read; on a
    cache hit the second open of the Audio tab takes ~100 ms.

    The paloc lookup itself is not returned — callers don't keep it
    alive once linking is done — so we throw it away after writing
    the linked entries to cache.
    """
    from utils import build_cache
    from pathlib import Path as _Path

    pkg = _Path(vfs.packages_path)
    fingerprint = build_cache.fingerprint_paths([
        pkg / g / "0.pamt" for g in sorted(groups)
    ])

    cached = build_cache.load_cached("audio_index", fingerprint)
    if cached is not None:
        if progress_callback:
            progress_callback(100, "Loaded audio index from cache.")
        return cached

    if progress_callback:
        progress_callback(0, "Building paloc text lookup...")
    paloc_lookup = build_paloc_lookup(
        vfs, groups,
        progress_callback=lambda _p, m: progress_callback(_p, m) if progress_callback else None,
    )

    if progress_callback:
        progress_callback(0, "Indexing voice audio files...")
    entries = build_audio_index(
        vfs, groups, paloc_lookup,
        progress_callback=progress_callback,
    )

    if progress_callback:
        progress_callback(100, "Caching audio index for next launch...")
    build_cache.save_cached("audio_index", fingerprint, entries)
    return entries


def get_all_categories(entries: list[AudioEntry]) -> list[str]:
    """Get sorted list of all unique categories."""
    cats = sorted(set(e.category for e in entries if e.category))
    return cats


def get_all_npc_types(entries: list[AudioEntry]) -> list[str]:
    """Get sorted list of all unique NPC voice types."""
    types = sorted(set(e.voice_prefix for e in entries if e.voice_prefix))
    return types


def get_all_languages(entries: list[AudioEntry]) -> list[str]:
    """Get sorted list of all voice languages."""
    langs = sorted(set(e.voice_lang for e in entries if e.voice_lang))
    return langs
