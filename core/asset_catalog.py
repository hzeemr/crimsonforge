"""Live asset catalog builder for Crimson Desert.

This module builds character, family, media, and item workbench data from
real installed game files. It powers the main Explorer Navigator and can also
be reused by any future asset-discovery tools.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from core.item_catalog import ItemCatalogRecord, build_item_catalog
from core.pamt_parser import PamtFileEntry
from core.vfs_manager import VfsManager


_PREFAB_TOKEN_RE = re.compile(
    rb"CD_[A-Za-z0-9_]+|character/model/[^\x00]+?\.(?:pac|pam|pamlod)",
    re.IGNORECASE,
)
_STOPWORD_TOKENS = {
    "appearance",
    "character",
    "customization",
    "cd",
    "decorationparam",
    "example",
    "meshparam",
    "param",
    "player",
}
_GENERIC_ALIAS_TOKENS = {
    "adult",
    "age",
    "ally",
    "armor",
    "character",
    "clone",
    "customization",
    "damaged",
    "decorationparam",
    "defaultcustomization",
    "desert",
    "elite",
    "empty",
    "example",
    "female",
    "hair",
    "head",
    "human",
    "inner",
    "male",
    "meshparam",
    "north",
    "nude",
    "npc",
    "oldman",
    "outer",
    "player",
    "south",
    "support",
    "underwear",
    "young",
}
_SLOT_ORDER = {"Core": -1, "Nude": 0, "Head": 1, "Hair": 2, "Armor": 3}
_CORE_KIND_ORDER = {
    "Appearance XML": 0,
    "Customization File": 1,
    "MeshParam File": 2,
    "DecorationParam File": 3,
}
_SLOT_FILE_KIND_ORDER = {
    "Prefab": 0,
    "Prefab Data": 1,
    "Mesh": 2,
    "Mesh Sidecar": 3,
    "Support File": 4,
    "Unresolved Reference": 5,
}
_UI_MEDIA_PREFIXES = (
    ("Portrait Image", "Image", "ui/cd_portraitimage_chracter_", ".dds", 180),
    ("Character Quest Image", "Image", "ui/cd_questimage_character_", ".dds", 170),
    ("Knowledge Image", "Image", "ui/cd_knowledgeimage_knowledge_", ".dds", 160),
    ("Quest Image", "Image", "ui/cd_questimage_", ".dds", 150),
    ("Playguide Image", "Image", "ui/cd_playguideimage_advice_play_", ".dds", 140),
    ("Wanted Image", "Image", "ui/cd_wanted_", ".dds", 120),
    ("Advice Video", "Video", "ui/advice_", ".mp4", 110),
    ("Skill Video", "Video", "ui/skill_", ".mp4", 100),
    ("Knowledge Video", "Video", "ui/knowledge_", ".mp4", 95),
)
_MEDIA_TYPE_ORDER = {"Image": 0, "Video": 1}


@dataclass(slots=True)
class LinkedFileRecord:
    order: int
    slot: str
    label: str
    kind: str
    path: str
    source: str
    resolved: bool = True
    notes: str = ""


@dataclass(slots=True)
class CharacterMediaRecord:
    category: str
    media_type: str
    path: str
    match_key: str
    score: int


@dataclass(slots=True)
class ItemIconRecord:
    path: str
    match_key: str
    score: int


@dataclass(slots=True)
class CharacterRecord:
    app_id: str
    display_name: str
    name_source: str
    family_code: str
    gender: str
    likely_human: bool
    app_path: str
    identity: str
    variant: str
    aliases: list[str] = field(default_factory=list)
    customization_file: str = ""
    mesh_param_file: str = ""
    decoration_param_file: str = ""
    slots: dict[str, list[str]] = field(default_factory=dict)
    files: list[LinkedFileRecord] = field(default_factory=list)
    media: list[CharacterMediaRecord] = field(default_factory=list)
    search_text: str = ""


@dataclass(slots=True)
class CharacterCatalog:
    records: list[CharacterRecord]
    slot_counts: dict[str, int]
    family_counts: dict[str, int]
    human_count: int
    male_count: int
    female_count: int


@dataclass(slots=True)
class FamilyProfile:
    family_code: str
    label: str
    gender: str
    likely_human: bool
    character_count: int
    example_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkbenchItemRecord:
    internal_name: str
    source: str
    item_id: int | None
    loc_key: str
    top_category: str
    category: str
    subcategory: str
    subsubcategory: str
    raw_type: str
    variant_base_name: str
    variant_level: int | None
    classification_confidence: str
    pac_files: list[str] = field(default_factory=list)
    effective_pac_files: list[str] = field(default_factory=list)
    family_codes: list[str] = field(default_factory=list)
    direct_name_matches: list[str] = field(default_factory=list)
    icon_records: list[ItemIconRecord] = field(default_factory=list)
    inherited_visuals: bool = False
    compatibility_confidence: str = "unknown"
    search_text: str = ""


@dataclass(slots=True)
class CharacterWorkbenchData:
    characters: CharacterCatalog
    families: list[FamilyProfile]
    items: list[WorkbenchItemRecord]


@dataclass(slots=True)
class _AliasCandidate:
    name: str
    source: str
    score: int


@dataclass(slots=True)
class _PrefabRecord:
    prefab_path: str
    mesh_links: list[tuple[str, str]]
    prefabdata_path: str = ""
    support_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _IndexedMediaRecord:
    category: str
    media_type: str
    path: str
    match_key: str
    base_score: int


def _decode_xml(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _basename_lower(path: str) -> str:
    return os.path.basename(path.replace("\\", "/")).lower()


def _humanize_text(value: str) -> str:
    cleaned = value.replace("_", " ").strip()
    if not cleaned:
        return value
    return " ".join(part.capitalize() if not part.isdigit() else part for part in cleaned.split())


def _normalize_lookup_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _extract_alias_candidates(*values: str) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for token in re.split(r"[^A-Za-z0-9]+", value):
            lower = token.lower()
            if len(lower) < 4 or lower.isdigit() or lower in _STOPWORD_TOKENS:
                continue
            alias = token.capitalize()
            key = alias.lower()
            if key in seen:
                continue
            seen.add(key)
            aliases.append(alias)
    return aliases


def _split_alias_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for token in re.split(r"[^A-Za-z0-9]+", value):
        lower = token.lower()
        if len(lower) < 4 or lower.isdigit():
            continue
        if lower in _STOPWORD_TOKENS:
            continue
        tokens.append(token)
    return tokens


def _infer_gender(family_code: str) -> str:
    lower = family_code.lower()
    if lower.endswith("m"):
        return "Male"
    if lower.endswith("w"):
        return "Female"
    return "Unknown"


def _infer_likely_human(family_code: str, prefabs_by_slot: dict[str, list[str]]) -> bool:
    lower = family_code.lower()
    if len(lower) == 3 and lower[1] == "h" and lower[-1] in {"m", "w"}:
        return True
    all_prefabs = " ".join(name.lower() for names in prefabs_by_slot.values() for name in names)
    return "_phm_" in all_prefabs or "_phw_" in all_prefabs


def _parse_known_character_names(vfs: VfsManager) -> set[str]:
    known: set[str] = set()
    try:
        entry = next(
            e
            for e in vfs.load_pamt("0008").file_entries
            if e.path.lower().replace("\\", "/") == "gamedata/characterinfo.pabgb"
        )
    except StopIteration:
        return known

    data = vfs.read_entry_data(entry)
    for match in re.finditer(rb"[A-Za-z][A-Za-z0-9_]{3,80}", data):
        token = match.group(0).decode("ascii", errors="ignore")
        lower = token.lower()
        if lower in _GENERIC_ALIAS_TOKENS:
            continue
        if re.fullmatch(r"[a-z]\d{3,}", lower):
            continue
        if token.isupper():
            continue
        if "_" in token:
            parts = token.split("_")
            if len(parts) == 1:
                continue
            # Keep internal unique names like NHM_Citizen_Dyer_3002 available
            # for search, but they should not automatically win display-name
            known.add(_humanize_text(token).lower())
            continue
        known.add(token.lower())
    return known


def _parse_app_id(app_path: str) -> tuple[str, str, str, str]:
    base = Path(app_path).name[:-8]
    parts = base.split("_")
    family_parts: list[str] = [parts[1]] if len(parts) > 1 else []
    identity_start = 2
    if len(parts) > 2 and re.fullmatch(r"\d{2}", parts[2] or "") and parts[1].lower().startswith(("m", "r")):
        family_parts.append(parts[2])
        identity_start = 3
    family_code = "_".join(part for part in family_parts if part)
    variant = parts[-1] if parts and parts[-1].isdigit() else ""
    if variant:
        identity_parts = parts[identity_start:-1]
    else:
        identity_parts = parts[identity_start:]
    identity = "_".join(identity_parts) if identity_parts else base
    return base, family_code, identity, variant


def _resolve_reference(
    ref_value: str,
    entry_map: dict[str, PamtFileEntry],
    basename_map: dict[str, list[str]],
) -> str:
    if not ref_value:
        return ""
    normalized = ref_value.replace("\\", "/").strip().lower()
    candidate = normalized
    if not candidate.startswith("character/"):
        candidate = f"character/{candidate}"
    if candidate in entry_map:
        return entry_map[candidate].path.replace("\\", "/")
    if "." not in os.path.basename(normalized):
        for suffix in (".paccd", ".xml", ".pabc", ".pab", ".papr", ".pamt"):
            variant = candidate + suffix
            if variant in entry_map:
                return entry_map[variant].path.replace("\\", "/")
    base = os.path.basename(normalized)
    matches = basename_map.get(base, [])
    if matches:
        return matches[0]
    if "." not in base:
        for suffix in (".paccd", ".xml", ".pabc", ".pab", ".papr", ".pamt"):
            matches = basename_map.get(base + suffix, [])
            if matches:
                return matches[0]
    return ""


def _parse_prefab_mesh_links(data: bytes) -> list[tuple[str, str]]:
    current_label = ""
    mesh_links: list[tuple[str, str]] = []
    for match in _PREFAB_TOKEN_RE.finditer(data):
        token = match.group(0).decode("utf-8", errors="replace")
        if token.startswith("CD_"):
            current_label = token
            continue
        mesh_links.append((current_label or "Mesh", token.replace("\\", "/")))
    return mesh_links


def _parse_prefabdata_refs(xml_text: str) -> list[str]:
    refs: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return refs
    for element in root.iter():
        file_name = (element.attrib.get("FileName") or "").strip()
        if file_name:
            refs.append(file_name)
    return refs


def _load_prefab_record(
    vfs: VfsManager,
    prefab_name: str,
    entry_map: dict[str, PamtFileEntry],
    basename_map: dict[str, list[str]],
    prefab_cache: dict[str, _PrefabRecord | None],
) -> _PrefabRecord | None:
    key = prefab_name.lower()
    cached = prefab_cache.get(key)
    if cached is not None:
        return cached

    prefab_path = f"character/{key}.prefab"
    prefab_entry = entry_map.get(prefab_path)
    if prefab_entry is None:
        prefab_cache[key] = None
        return None

    prefab_data = vfs.read_entry_data(prefab_entry)
    mesh_links: list[tuple[str, str]] = []
    for label, raw_path in _parse_prefab_mesh_links(prefab_data):
        resolved = _resolve_reference(raw_path, entry_map, basename_map)
        if resolved:
            mesh_links.append((label, resolved))
        else:
            mesh_links.append((label, raw_path))

    prefabdata_path = ""
    support_refs: list[str] = []
    prefabdata_entry = entry_map.get(f"character/{key}.prefabdata.xml")
    if prefabdata_entry is not None:
        prefabdata_path = prefabdata_entry.path.replace("\\", "/")
        xml_text = _decode_xml(vfs.read_entry_data(prefabdata_entry))
        for ref in _parse_prefabdata_refs(xml_text):
            support_refs.append(_resolve_reference(ref, entry_map, basename_map) or ref)

    record = _PrefabRecord(
        prefab_path=prefab_entry.path.replace("\\", "/"),
        mesh_links=mesh_links,
        prefabdata_path=prefabdata_path,
        support_refs=support_refs,
    )
    prefab_cache[key] = record
    return record


def _build_search_text(record: CharacterRecord) -> str:
    parts = [
        record.display_name,
        record.app_id,
        record.family_code,
        record.identity,
        record.variant,
        record.gender,
        "human" if record.likely_human else "nonhuman",
        " ".join(record.aliases),
        record.customization_file,
        record.mesh_param_file,
        record.decoration_param_file,
    ]
    for slot, names in record.slots.items():
        parts.append(slot)
        parts.extend(names)
    for linked in record.files:
        parts.extend([linked.slot, linked.label, linked.kind, linked.path, linked.notes])
    for media in record.media:
        parts.extend([media.category, media.media_type, media.path, media.match_key])
    return " ".join(part.lower() for part in parts if part)


def _sort_files(files: list[LinkedFileRecord]) -> list[LinkedFileRecord]:
    return sorted(
        files,
        key=lambda item: (
            _SLOT_ORDER.get(item.slot, 99),
            item.slot.lower(),
            _CORE_KIND_ORDER.get(item.kind, 99) if item.slot == "Core" else _SLOT_FILE_KIND_ORDER.get(item.kind, 99),
            item.order,
            item.label.lower(),
            item.path.lower(),
        ),
    )


def _sort_media(media: list[CharacterMediaRecord]) -> list[CharacterMediaRecord]:
    return sorted(
        media,
        key=lambda item: (
            _MEDIA_TYPE_ORDER.get(item.media_type, 9),
            -item.score,
            item.category.lower(),
            item.path.lower(),
        ),
    )


def _make_alias_candidates(
    *,
    identity: str,
    mesh_param_file: str,
    customization_file: str,
    decoration_param_file: str,
    known_character_names: set[str],
) -> list[_AliasCandidate]:
    candidates: list[_AliasCandidate] = []
    seen: dict[tuple[str, str], int] = {}

    def add(raw_name: str, source: str, base_score: int) -> None:
        cleaned = _humanize_text(raw_name).strip()
        if not cleaned:
            return
        lower = cleaned.lower()
        if lower in _GENERIC_ALIAS_TOKENS:
            return
        if re.fullmatch(r"[a-z]\d{3,}", lower):
            return
        if re.fullmatch(r"[a-z]{2,4}_\d{2,}", lower):
            return
        score = base_score
        if " " in cleaned:
            score += 4
        if lower in known_character_names:
            score += 12
        key = (lower, source)
        if score <= seen.get(key, -999):
            return
        seen[key] = score
        candidates.append(_AliasCandidate(cleaned, source, score))

    for token in _split_alias_tokens(identity):
        add(token, "identity", 35)
    add(identity, "identity_phrase", 45)

    for token in _split_alias_tokens(os.path.basename(mesh_param_file)):
        add(token, "meshparam", 90)
    for token in _split_alias_tokens(os.path.basename(customization_file)):
        add(token, "customization", 85)
    for token in _split_alias_tokens(os.path.basename(decoration_param_file)):
        add(token, "decoration", 55)

    return sorted(candidates, key=lambda item: (-item.score, item.name.lower(), item.source))


def _choose_display_name(
    *,
    identity: str,
    variant: str,
    likely_human: bool,
    alias_candidates: list[_AliasCandidate],
) -> tuple[str, str, list[str]]:
    alias_names: list[str] = []
    seen: set[str] = set()
    for candidate in alias_candidates:
        key = candidate.name.lower()
        if key in seen:
            continue
        seen.add(key)
        alias_names.append(candidate.name)

    best = alias_candidates[0] if alias_candidates else None
    identity_label = _humanize_text(identity)
    if best is not None:
        if likely_human and best.score >= 70:
            return best.name, best.source, alias_names
        if best.score >= 82:
            return best.name, best.source, alias_names

    if variant:
        return f"{identity_label} #{variant}", "identity", alias_names
    return identity_label, "identity", alias_names


def _iter_media_index_tokens(match_key: str) -> set[str]:
    tokens = {match_key}
    parts = [part for part in match_key.split("_") if len(part) >= 4 and part not in _GENERIC_ALIAS_TOKENS]
    tokens.update(parts)
    collapsed = match_key.replace("_", "")
    if len(collapsed) >= 4:
        tokens.add(collapsed)
    return {token for token in tokens if token}


def _build_ui_media_index(vfs: VfsManager) -> tuple[list[_IndexedMediaRecord], dict[str, list[int]]]:
    try:
        pamt = vfs.load_pamt("0012")
    except Exception:
        return [], {}

    media_items: list[_IndexedMediaRecord] = []
    media_token_index: dict[str, list[int]] = defaultdict(list)
    for entry in pamt.file_entries:
        path = entry.path.replace("\\", "/")
        lower = path.lower()
        for category, media_type, prefix, suffix, base_score in _UI_MEDIA_PREFIXES:
            if not lower.startswith(prefix) or not lower.endswith(suffix):
                continue
            token = lower[len(prefix) : -len(suffix)]
            if not token:
                continue
            indexed = _IndexedMediaRecord(
                category=category,
                media_type=media_type,
                path=path,
                match_key=token,
                base_score=base_score,
            )
            media_items.append(indexed)
            media_index = len(media_items) - 1
            for lookup_token in _iter_media_index_tokens(token):
                media_token_index[lookup_token].append(media_index)
            break
    return media_items, dict(media_token_index)


def _build_item_icon_index(vfs: VfsManager) -> tuple[list[ItemIconRecord], dict[str, list[int]]]:
    try:
        pamt = vfs.load_pamt("0012")
    except Exception:
        return [], {}

    icons: list[ItemIconRecord] = []
    token_index: dict[str, list[int]] = defaultdict(list)
    prefix = "ui/itemicon_"
    suffix = ".dds"
    for entry in pamt.file_entries:
        path = entry.path.replace("\\", "/")
        lower = path.lower()
        if not lower.startswith(prefix) or not lower.endswith(suffix):
            continue
        token = lower[len(prefix) : -len(suffix)]
        if not token:
            continue
        icon = ItemIconRecord(path=path, match_key=token, score=0)
        icons.append(icon)
        icon_idx = len(icons) - 1
        for lookup_token in _iter_media_index_tokens(token):
            token_index[lookup_token].append(icon_idx)
    return icons, dict(token_index)


def _make_media_lookup_keys(
    *,
    display_name: str,
    identity: str,
    aliases: list[str],
    alias_candidates: list[_AliasCandidate],
) -> dict[str, int]:
    lookup: dict[str, int] = {}

    def add(raw_value: str, weight: int) -> None:
        normalized = _normalize_lookup_key(raw_value)
        if not normalized or normalized in _GENERIC_ALIAS_TOKENS:
            return
        lookup[normalized] = max(lookup.get(normalized, 0), weight)
        collapsed = normalized.replace("_", "")
        if len(collapsed) >= 4:
            lookup[collapsed] = max(lookup.get(collapsed, 0), max(weight - 6, 1))

    add(display_name, 120)
    add(identity, 60)
    for alias in aliases:
        add(alias, 90)
    for candidate in alias_candidates:
        add(candidate.name, max(candidate.score, 40))
    return lookup


def _score_media_match(match_key: str, lookup_keys: dict[str, int], base_score: int) -> int:
    best = 0
    compact_match = match_key.replace("_", "")
    for key, weight in lookup_keys.items():
        if match_key == key:
            best = max(best, base_score + weight + 70)
            continue
        if match_key.startswith(key + "_") or match_key.endswith("_" + key):
            best = max(best, base_score + weight + 55)
            continue
        if f"_{key}_" in match_key:
            best = max(best, base_score + weight + 48)
            continue
        if key in match_key:
            best = max(best, base_score + weight + 36)
            continue
        if key in compact_match:
            best = max(best, base_score + weight + 28)
    return best


def _match_media_for_record(
    *,
    display_name: str,
    identity: str,
    aliases: list[str],
    alias_candidates: list[_AliasCandidate],
    media_items: list[_IndexedMediaRecord],
    media_token_index: dict[str, list[int]],
) -> list[CharacterMediaRecord]:
    lookup_keys = _make_media_lookup_keys(
        display_name=display_name,
        identity=identity,
        aliases=aliases,
        alias_candidates=alias_candidates,
    )
    candidate_indices: set[int] = set()
    for key in lookup_keys:
        candidate_indices.update(media_token_index.get(key, []))

    matched: dict[str, CharacterMediaRecord] = {}
    for media_index in sorted(candidate_indices):
        indexed = media_items[media_index]
        score = _score_media_match(indexed.match_key, lookup_keys, indexed.base_score)
        if score <= 0:
            continue
        current = matched.get(indexed.path.lower())
        if current is not None and current.score >= score:
            continue
        matched[indexed.path.lower()] = CharacterMediaRecord(
            category=indexed.category,
            media_type=indexed.media_type,
            path=indexed.path,
            match_key=indexed.match_key,
            score=score,
        )
    return _sort_media(list(matched.values()))


def _build_family_profiles(records: list[CharacterRecord]) -> list[FamilyProfile]:
    grouped: dict[str, list[CharacterRecord]] = defaultdict(list)
    for record in records:
        grouped[record.family_code].append(record)

    profiles: list[FamilyProfile] = []
    for family_code, family_records in grouped.items():
        likely_human = any(record.likely_human for record in family_records)
        genders = {record.gender for record in family_records if record.gender != "Unknown"}
        if len(genders) == 1:
            gender = next(iter(genders))
        elif genders:
            gender = "Mixed"
        else:
            gender = "Unknown"
        examples: list[str] = []
        seen_names: set[str] = set()
        for record in sorted(family_records, key=lambda item: (item.display_name.lower(), item.app_id.lower())):
            key = record.display_name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            examples.append(record.display_name)
            if len(examples) >= 6:
                break
        label_parts = [family_code.upper()]
        if likely_human:
            label_parts.append("Human")
        if gender != "Unknown":
            label_parts.append(gender)
        profiles.append(
            FamilyProfile(
                family_code=family_code,
                label=" | ".join(label_parts),
                gender=gender,
                likely_human=likely_human,
                character_count=len(family_records),
                example_names=examples,
            )
        )

    profiles.sort(key=lambda item: (-item.character_count, item.family_code.lower()))
    return profiles


def _build_variant_pac_map(items: list[ItemCatalogRecord]) -> dict[str, list[str]]:
    pac_map: dict[str, list[str]] = {}
    for item in items:
        if not item.pac_files:
            continue
        bucket = pac_map.setdefault(item.variant_base_name.lower(), [])
        for pac in item.pac_files:
            if pac not in bucket:
                bucket.append(pac)
    return pac_map


def _extract_item_family_codes(paths: list[str], known_codes: list[str]) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for code in known_codes:
        marker = f"_{code.lower()}_"
        for path in paths:
            if marker in path.lower():
                if code not in seen:
                    seen.add(code)
                    matches.append(code)
                break
    return matches


def _build_character_name_lookup(records: list[CharacterRecord]) -> dict[str, list[str]]:
    lookup: dict[str, list[str]] = defaultdict(list)
    for record in records:
        candidates = list(record.aliases)
        if record.name_source != "identity" and "#" not in record.display_name:
            candidates.append(record.display_name)
        for candidate in candidates:
            key = _normalize_lookup_key(candidate)
            if len(key) < 4 or key in _GENERIC_ALIAS_TOKENS:
                continue
            values = lookup[key]
            if record.display_name not in values:
                values.append(record.display_name)
    return dict(lookup)


def _match_item_names_to_characters(internal_name: str, name_lookup: dict[str, list[str]]) -> list[str]:
    normalized = _normalize_lookup_key(internal_name)
    if not normalized:
        return []
    matched: list[str] = []
    seen: set[str] = set()
    for key, display_names in name_lookup.items():
        if normalized == key or normalized.startswith(key + "_") or normalized.endswith("_" + key) or f"_{key}_" in normalized:
            for display_name in display_names:
                lower = display_name.lower()
                if lower not in seen:
                    seen.add(lower)
                    matched.append(display_name)
    return matched


def _item_lookup_tokens(record: ItemCatalogRecord) -> dict[str, int]:
    lookup: dict[str, int] = {}

    def add(raw_value: str, weight: int) -> None:
        normalized = _normalize_lookup_key(raw_value)
        if not normalized:
            return
        lookup[normalized] = max(lookup.get(normalized, 0), weight)
        collapsed = normalized.replace("_", "")
        if len(collapsed) >= 5:
            lookup[collapsed] = max(lookup.get(collapsed, 0), max(weight - 8, 1))
        parts = [
            part
            for part in normalized.split("_")
            if len(part) >= 4 and part not in _GENERIC_ALIAS_TOKENS and not part.isdigit()
        ]
        for part in parts:
            lookup[part] = max(lookup.get(part, 0), max(weight - 14, 1))

    add(record.internal_name, 100)
    add(record.variant_base_name, 92)
    add(record.raw_type, 36)
    add(record.subsubcategory, 28)
    add(record.subcategory, 24)
    return lookup


def _score_item_icon_match(match_key: str, lookup_keys: dict[str, int]) -> int:
    best = 0
    compact_match = match_key.replace("_", "")
    match_parts = [part for part in match_key.split("_") if len(part) >= 4]
    for key, weight in lookup_keys.items():
        if match_key == key:
            best = max(best, weight + 120)
            continue
        if match_key.startswith(key + "_") or match_key.endswith("_" + key):
            best = max(best, weight + 90)
            continue
        if f"_{key}_" in match_key:
            best = max(best, weight + 72)
            continue
        if key in match_key:
            best = max(best, weight + 55)
            continue
        if key in compact_match:
            best = max(best, weight + 46)

    strong_overlap = 0
    overlap_score = 0
    for part in match_parts:
        if part in lookup_keys:
            strong_overlap += 1
            overlap_score += min(lookup_keys[part], 45)
    if strong_overlap >= 2:
        best = max(best, 70 + overlap_score)
    return best


def _match_item_icons(
    *,
    item: ItemCatalogRecord,
    icon_items: list[ItemIconRecord],
    icon_token_index: dict[str, list[int]],
) -> list[ItemIconRecord]:
    lookup_keys = _item_lookup_tokens(item)
    candidate_indices: set[int] = set()
    for key in lookup_keys:
        candidate_indices.update(icon_token_index.get(key, []))

    matched: list[ItemIconRecord] = []
    for icon_idx in candidate_indices:
        indexed = icon_items[icon_idx]
        score = _score_item_icon_match(indexed.match_key, lookup_keys)
        if score < 120:
            continue
        matched.append(ItemIconRecord(path=indexed.path, match_key=indexed.match_key, score=score))

    matched.sort(key=lambda item: (-item.score, item.path.lower()))
    return matched[:8]


def _build_item_search_text(record: WorkbenchItemRecord) -> str:
    parts = [
        record.internal_name,
        record.source,
        record.loc_key,
        record.top_category,
        record.category,
        record.subcategory,
        record.subsubcategory,
        record.raw_type,
        record.variant_base_name,
        " ".join(record.pac_files),
        " ".join(record.effective_pac_files),
        " ".join(record.family_codes),
        " ".join(record.direct_name_matches),
        " ".join(icon.path for icon in record.icon_records),
        record.compatibility_confidence,
    ]
    return " ".join(part.lower() for part in parts if part)


def _build_workbench_items(
    *,
    item_records: list[ItemCatalogRecord],
    character_records: list[CharacterRecord],
    icon_items: list[ItemIconRecord],
    icon_token_index: dict[str, list[int]],
) -> list[WorkbenchItemRecord]:
    variant_pac_map = _build_variant_pac_map(item_records)
    known_family_codes = sorted(
        {
            record.family_code.lower()
            for record in character_records
            if record.family_code and record.likely_human
        },
        key=len,
        reverse=True,
    )
    name_lookup = _build_character_name_lookup(character_records)

    records: list[WorkbenchItemRecord] = []
    for item in item_records:
        effective_pac_files = list(item.pac_files)
        inherited_visuals = False
        if not effective_pac_files:
            inherited = variant_pac_map.get(item.variant_base_name.lower(), [])
            if inherited:
                effective_pac_files = list(inherited)
                inherited_visuals = True
        family_codes = _extract_item_family_codes(effective_pac_files, known_family_codes)
        direct_name_matches = _match_item_names_to_characters(item.internal_name, name_lookup)
        icon_records = _match_item_icons(
            item=item,
            icon_items=icon_items,
            icon_token_index=icon_token_index,
        )
        if family_codes and direct_name_matches:
            compatibility_confidence = "high"
        elif family_codes:
            compatibility_confidence = "high" if item.top_category == "Equipment" else "medium"
        elif direct_name_matches:
            compatibility_confidence = "medium"
        elif effective_pac_files:
            compatibility_confidence = "low"
        else:
            compatibility_confidence = "unknown"

        record = WorkbenchItemRecord(
            internal_name=item.internal_name,
            source=item.source,
            item_id=item.item_id,
            loc_key=item.loc_key,
            top_category=item.top_category,
            category=item.category,
            subcategory=item.subcategory,
            subsubcategory=item.subsubcategory,
            raw_type=item.raw_type,
            variant_base_name=item.variant_base_name,
            variant_level=item.variant_level,
            classification_confidence=item.classification_confidence,
            pac_files=list(item.pac_files),
            effective_pac_files=effective_pac_files,
            family_codes=family_codes,
            direct_name_matches=direct_name_matches,
            icon_records=icon_records,
            inherited_visuals=inherited_visuals,
            compatibility_confidence=compatibility_confidence,
        )
        record.search_text = _build_item_search_text(record)
        records.append(record)

    records.sort(
        key=lambda item: (
            item.top_category.lower(),
            item.category.lower(),
            item.subcategory.lower(),
            item.subsubcategory.lower(),
            item.internal_name.lower(),
        )
    )
    return records


def build_character_catalog_from_vfs(worker, vfs: VfsManager) -> CharacterCatalog:
    pamt = vfs.load_pamt("0009")
    known_character_names = _parse_known_character_names(vfs)
    ui_media_items, ui_media_token_index = _build_ui_media_index(vfs)
    character_entries = [
        entry
        for entry in pamt.file_entries
        if entry.path.lower().startswith("character/")
    ]
    entry_map = {
        entry.path.replace("\\", "/").lower(): entry
        for entry in character_entries
    }
    basename_map: dict[str, list[str]] = defaultdict(list)
    for entry in character_entries:
        basename_map[_basename_lower(entry.path)].append(entry.path.replace("\\", "/"))

    app_entries = [
        entry
        for entry in character_entries
        if entry.path.lower().endswith(".app.xml")
    ]
    total = max(len(app_entries), 1)
    prefab_cache: dict[str, _PrefabRecord | None] = {}
    records: list[CharacterRecord] = []

    for idx, entry in enumerate(sorted(app_entries, key=lambda item: item.path.lower()), start=1):
        if worker.is_cancelled():
            return CharacterCatalog([], {}, {}, 0, 0, 0)

        if idx == 1 or idx % 50 == 0 or idx == total:
            pct = int(idx / total * 100)
            worker.report_progress(pct, f"Scanning character appearance {idx:,}/{total:,}")

        xml_text = _decode_xml(vfs.read_entry_data(entry))
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue

        app_id, family_code, identity, variant = _parse_app_id(entry.path.replace("\\", "/"))
        slots: dict[str, list[str]] = {}
        files: list[LinkedFileRecord] = []
        order = 0

        def add_file(
            *,
            slot: str,
            label: str,
            kind: str,
            path: str,
            source: str,
            resolved: bool = True,
            notes: str = "",
        ) -> None:
            nonlocal order
            order += 1
            files.append(
                LinkedFileRecord(
                    order=order,
                    slot=slot,
                    label=label,
                    kind=kind,
                    path=path,
                    source=source,
                    resolved=resolved,
                    notes=notes,
                )
            )

        app_path = entry.path.replace("\\", "/")
        add_file(
            slot="Core",
            label="Appearance",
            kind="Appearance XML",
            path=app_path,
            source=app_path,
        )

        customization_file = ""
        mesh_param_file = ""
        decoration_param_file = ""
        customization_node = root.find("Customization")
        if customization_node is not None:
            customization_file = customization_node.attrib.get("CustomizationFile", "").strip()
            mesh_param_file = customization_node.attrib.get("MeshParamFile", "").strip()
            decoration_param_file = customization_node.attrib.get("DecorationParamFile", "").strip()
            for label, kind, raw_ref in (
                ("Customization", "Customization File", customization_file),
                ("Mesh Param", "MeshParam File", mesh_param_file),
                ("Decoration Param", "DecorationParam File", decoration_param_file),
            ):
                if not raw_ref:
                    continue
                resolved = _resolve_reference(raw_ref, entry_map, basename_map)
                add_file(
                    slot="Core",
                    label=label,
                    kind=kind,
                    path=resolved or raw_ref,
                    source=app_path,
                    resolved=bool(resolved),
                    notes="" if resolved else "Referenced from app.xml",
                )

        for child in list(root):
            slot_name = child.tag.split("}")[-1]
            prefab_names: list[str] = []
            for prefab_node in list(child):
                if prefab_node.tag.split("}")[-1] != "Prefab":
                    continue
                prefab_name = (prefab_node.attrib.get("Name") or "").strip()
                if not prefab_name:
                    continue
                prefab_names.append(prefab_name)
            if not prefab_names:
                continue

            slots[slot_name] = prefab_names
            for prefab_name in prefab_names:
                prefab_record = _load_prefab_record(vfs, prefab_name, entry_map, basename_map, prefab_cache)
                prefab_label = prefab_name
                if prefab_record is None:
                    add_file(
                        slot=slot_name,
                        label=prefab_label,
                        kind="Prefab",
                        path=f"character/{prefab_name}.prefab",
                        source=app_path,
                        resolved=False,
                        notes="Prefab file not found",
                    )
                    continue

                add_file(
                    slot=slot_name,
                    label=prefab_label,
                    kind="Prefab",
                    path=prefab_record.prefab_path,
                    source=app_path,
                )

                if prefab_record.prefabdata_path:
                    add_file(
                        slot=slot_name,
                        label=f"{prefab_label} Data",
                        kind="Prefab Data",
                        path=prefab_record.prefabdata_path,
                        source=prefab_record.prefab_path,
                    )

                for mesh_label, mesh_path in prefab_record.mesh_links:
                    resolved_mesh = mesh_path in entry_map or mesh_path.lower() in entry_map
                    add_file(
                        slot=slot_name,
                        label=mesh_label,
                        kind="Mesh",
                        path=mesh_path,
                        source=prefab_record.prefab_path,
                        resolved=resolved_mesh,
                        notes="" if resolved_mesh else "Unresolved prefab mesh reference",
                    )
                    if resolved_mesh:
                        for sidecar_name in (
                            f"{mesh_path}.xml",
                            f"{mesh_path}.hkx",
                            f"{mesh_path}.wrinkle.xml",
                        ):
                            sidecar_entry = entry_map.get(sidecar_name.lower())
                            if sidecar_entry is None:
                                continue
                            add_file(
                                slot=slot_name,
                                label=os.path.basename(sidecar_entry.path),
                                kind="Mesh Sidecar",
                                path=sidecar_entry.path.replace("\\", "/"),
                                source=mesh_path,
                            )

                for support_ref in prefab_record.support_refs:
                    resolved = support_ref.lower() in entry_map
                    add_file(
                        slot=slot_name,
                        label=os.path.basename(support_ref),
                        kind="Support File" if resolved else "Unresolved Reference",
                        path=support_ref,
                        source=prefab_record.prefabdata_path or prefab_record.prefab_path,
                        resolved=resolved,
                        notes="" if resolved else "Referenced from prefabdata.xml",
                    )

        alias_candidates = _make_alias_candidates(
            identity=identity,
            mesh_param_file=mesh_param_file,
            customization_file=customization_file,
            decoration_param_file=decoration_param_file,
            known_character_names=known_character_names,
        )
        likely_human = _infer_likely_human(family_code, slots)
        gender = _infer_gender(family_code)
        display_name, name_source, aliases = _choose_display_name(
            identity=identity,
            variant=variant,
            likely_human=likely_human,
            alias_candidates=alias_candidates,
        )
        media = _match_media_for_record(
            display_name=display_name,
            identity=identity,
            aliases=aliases,
            alias_candidates=alias_candidates,
            media_items=ui_media_items,
            media_token_index=ui_media_token_index,
        )

        record = CharacterRecord(
            app_id=app_id,
            display_name=display_name,
            name_source=name_source,
            family_code=family_code,
            gender=gender,
            likely_human=likely_human,
            app_path=app_path,
            identity=identity,
            variant=variant,
            aliases=aliases,
            customization_file=customization_file,
            mesh_param_file=mesh_param_file,
            decoration_param_file=decoration_param_file,
            slots=slots,
            files=_sort_files(files),
            media=media,
        )
        record.search_text = _build_search_text(record)
        records.append(record)

    records.sort(key=lambda item: (item.display_name.lower(), item.app_id.lower()))

    slot_counts = Counter()
    family_counts = Counter()
    male_count = 0
    female_count = 0
    human_count = 0
    for record in records:
        family_counts[record.family_code] += 1
        if record.likely_human:
            human_count += 1
        if record.gender == "Male":
            male_count += 1
        elif record.gender == "Female":
            female_count += 1
        for slot in record.slots:
            slot_counts[slot] += 1

    return CharacterCatalog(
        records=records,
        slot_counts=dict(slot_counts),
        family_counts=dict(family_counts),
        human_count=human_count,
        male_count=male_count,
        female_count=female_count,
    )


def build_character_catalog(worker, packages_path: str) -> CharacterCatalog:
    return build_character_catalog_from_vfs(worker, VfsManager(packages_path))


def build_character_workbench_from_vfs(worker, vfs: VfsManager) -> CharacterWorkbenchData:
    worker.report_progress(1, "Building live character catalog...")
    characters = build_character_catalog_from_vfs(worker, vfs)

    if worker.is_cancelled():
        return CharacterWorkbenchData(characters=characters, families=[], items=[])

    worker.report_progress(82, "Building live item catalog...")
    item_catalog = build_item_catalog(vfs)
    icon_items, icon_token_index = _build_item_icon_index(vfs)

    if worker.is_cancelled():
        return CharacterWorkbenchData(characters=characters, families=[], items=[])

    worker.report_progress(92, "Linking item wearability to character families...")
    families = _build_family_profiles(characters.records)
    items = _build_workbench_items(
        item_records=item_catalog.items,
        character_records=characters.records,
        icon_items=icon_items,
        icon_token_index=icon_token_index,
    )
    worker.report_progress(100, "Live workbench is ready")
    return CharacterWorkbenchData(
        characters=characters,
        families=families,
        items=items,
    )


def build_character_workbench(worker, packages_path: str) -> CharacterWorkbenchData:
    return build_character_workbench_from_vfs(worker, VfsManager(packages_path))
