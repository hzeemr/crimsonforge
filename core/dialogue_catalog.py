"""Enterprise-style dialogue catalog from live Crimson Desert game data.

This module builds a broad dialogue index directly from the game's symbolic
localization keys. It is intentionally grounded in live package data rather
than project-side notes or hand-built lists.

Coverage goals:
- quest dialogue families
- AI ambient dialogue families
- cutscenes (`intro_*`, `epilogue_*`)
- memory / node / onetimequest / quest_node families
- generic scene families such as `greymanecamp_*`, `okuro_boss_00_*`,
  `bloodcoronation_*`, `black_wall_*`, and similar speaker-tagged keys

Important truth:
Exact speaker names are not available for every line. The game often exposes
speaker slots (`npc_00`, `boss_00`, `player`) rather than explicit names. The
catalog therefore separates:
- exact speaker name when known
- speaker label / slot
- confidence of that mapping
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from core.paloc_parser import PalocEntry, parse_paloc
from core.vfs_manager import VfsManager


LOCALIZATION_PATH = "gamedata/localizationstring_eng.paloc"
HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
STATIC_TOKEN_RE = re.compile(
    r"\{Staticinfo:([^:#}]+):([^#}]+)#([^}]+)\}",
    re.IGNORECASE,
)

SINGLE_ROLE_TOKENS = {
    "player",
    "npc",
    "enemy",
    "narration",
    "globalgametrack",
    "subtitlegroup",
    "enemytarget0",
    "boss",
}
SLOT_ROLE_PREFIXES = {"npc", "enemy", "boss"}

KNOWN_CATEGORY_MAP = {
    "questdialog": ("Quest Dialogue", "Quest Dialogue", "Quest Dialogue"),
    "aidialogstringinfo": ("AI Dialogue", "AI Ambient Dialogue", "AI Ambient Dialogue"),
    "aidialogstringinfogroup": ("AI Dialogue", "AI Group Dialogue", "AI Group Dialogue"),
    "intro": ("Cutscene", "Intro", "Intro"),
    "epilogue": ("Cutscene", "Epilogue", "Epilogue"),
    "onetimequest": ("Quest Scene", "One-Time Quest", "One-Time Quest"),
    "node": ("Quest Scene", "Node Scene", "Node Scene"),
    "quest_node": ("Quest Scene", "Quest Node", "Quest Node"),
    "memory": ("Memory Scene", "Memory", "Memory"),
}

CATEGORY_ORDER = {
    "Cutscene": 0,
    "Quest Scene": 1,
    "Quest Dialogue": 2,
    "Memory Scene": 3,
    "Scene Dialogue": 4,
    "AI Dialogue": 5,
}


@dataclass(slots=True)
class DialogueMention:
    kind: str
    token: str
    label: str


@dataclass(slots=True)
class DialogueRecord:
    key: str
    source_path: str
    text_raw: str
    text_clean: str
    family: str
    family_display: str
    category: str
    subcategory: str
    story_group: str
    chapter_code: str
    chapter_label: str
    conversation_key: str
    conversation_label: str
    scene_key: str
    scene_label: str
    scene_group: str
    scene_part_a: str
    scene_part_b: str
    line_index: int | None
    dialogue_type: str
    speaker_name: str
    speaker_role: str
    speaker_display: str
    speaker_key: str
    speaker_confidence: str
    speaker_bucket: str
    speaker_slot: int | None
    mentions: list[DialogueMention] = field(default_factory=list)
    search_text: str = ""


@dataclass(slots=True)
class DialogueConversationRecord:
    conversation_key: str
    conversation_label: str
    story_group: str
    chapter_code: str
    chapter_label: str
    family: str
    family_display: str
    category: str
    subcategory: str
    line_count: int
    non_empty_line_count: int
    scene_count: int
    speaker_keys: list[str]
    speaker_labels: list[str]


@dataclass(slots=True)
class DialogueSpeakerRecord:
    speaker_key: str
    speaker_display: str
    speaker_name: str
    speaker_bucket: str
    speaker_confidence: str
    line_count: int
    conversation_count: int
    family_count: int
    story_groups: list[str]


@dataclass(slots=True)
class DialogueCatalogData:
    records: list[DialogueRecord]
    conversations: list[DialogueConversationRecord]
    speakers: list[DialogueSpeakerRecord]

    def to_dict(self) -> dict:
        return {
            "records": [
                {
                    **asdict(record),
                    "mentions": [asdict(mention) for mention in record.mentions],
                }
                for record in self.records
            ],
            "conversations": [asdict(conversation) for conversation in self.conversations],
            "speakers": [asdict(speaker) for speaker in self.speakers],
            "summary": {
                "record_count": len(self.records),
                "conversation_count": len(self.conversations),
                "speaker_count": len(self.speakers),
                "family_counts": dict(Counter(record.family for record in self.records)),
                "category_counts": dict(Counter(record.category for record in self.records)),
                "story_group_counts": dict(Counter(record.story_group for record in self.records)),
                "speaker_bucket_counts": dict(Counter(record.speaker_bucket for record in self.records)),
            },
        }


PROGRESS_FN = Callable[[str], None]


def _progress(progress_fn: PROGRESS_FN | None, message: str) -> None:
    if progress_fn:
        progress_fn(message)


def _load_localization_entries(vfs: VfsManager) -> list[PalocEntry]:
    entry = next(
        (e for e in vfs.load_pamt("0020").file_entries if LOCALIZATION_PATH in e.path.replace("\\", "/").lower()),
        None,
    )
    if entry is None:
        return []
    return parse_paloc(vfs.read_entry_data(entry))


def _clean_text(text: str) -> str:
    cleaned = HTML_BREAK_RE.sub("\n", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


def _pretty_label(value: str) -> str:
    if not value:
        return ""
    value = value.replace("_", " ").strip()
    return " ".join(part.capitalize() for part in value.split())


def _extract_mentions(text: str) -> list[DialogueMention]:
    mentions: list[DialogueMention] = []
    seen: set[tuple[str, str, str]] = set()
    for match in STATIC_TOKEN_RE.finditer(text):
        kind = match.group(1)
        token = match.group(2)
        label = match.group(3)
        triplet = (kind.lower(), token, label)
        if triplet in seen:
            continue
        seen.add(triplet)
        mentions.append(DialogueMention(kind=kind, token=token, label=label))
    return mentions


def _speaker_info(role: str) -> tuple[str, str, str, str, int | None]:
    """Return (speaker_name, display, confidence, bucket, slot)."""
    if not role:
        return "", "Unknown", "unknown", "Unknown", None

    lowered = role.lower()
    if lowered == "player":
        return "", "Player", "role", "Player", None
    if lowered == "narration":
        return "", "Narration", "role", "Narration", None
    if lowered == "globalgametrack":
        return "", "Global Game Track", "role", "Global Track", None
    if lowered == "subtitlegroup":
        return "", "Subtitle Group", "role", "Subtitle Group", None
    if lowered == "enemytarget0":
        return "", "Enemy Target 0", "role", "Enemy", 0
    if lowered == "quest_npc":
        return "", "Quest NPC", "family", "Quest NPC", None
    if lowered == "ai_npc":
        return "", "AI NPC", "family", "AI NPC", None
    if lowered == "ai_group":
        return "", "AI Group", "family", "AI Group", None
    if lowered in {"npc", "enemy", "boss"}:
        display = lowered.upper()
        bucket = "NPC" if lowered == "npc" else "Enemy" if lowered == "enemy" else "Boss"
        return "", display, "role", bucket, None

    prefix, _, suffix = lowered.partition("_")
    if prefix in SLOT_ROLE_PREFIXES and suffix.isdigit():
        slot = int(suffix)
        if prefix == "npc":
            return "", f"NPC {slot:02d}", "role", "NPC", slot
        if prefix == "enemy":
            return "", f"Enemy {slot:02d}", "role", "Enemy", slot
        return "", f"Boss {slot:02d}", "role", "Boss", slot

    display = _pretty_label(lowered)
    return "", display or lowered, "family", "Other", None


def _extract_line_index(parts: list[str]) -> tuple[list[str], int | None]:
    if parts and parts[-1].isdigit():
        return parts[:-1], int(parts[-1])
    return parts, None


def _extract_role_from_tail(parts_no_line: list[str]) -> tuple[str, int]:
    if not parts_no_line:
        return "", 0

    tail = parts_no_line[-1].lower()
    if tail in SINGLE_ROLE_TOKENS:
        return tail, 1

    if len(parts_no_line) >= 2:
        prefix = parts_no_line[-2].lower()
        suffix = parts_no_line[-1]
        if prefix in SLOT_ROLE_PREFIXES and suffix.isdigit():
            return f"{prefix}_{int(suffix):02d}", 2

    return "", 0


def _first_numeric_token(parts: list[str]) -> str:
    for token in parts:
        if token.isdigit():
            return token
    return ""


def _chapter_sort_value(chapter_code: str) -> tuple[int, str]:
    if chapter_code.isdigit():
        return int(chapter_code), ""
    if not chapter_code:
        return 999_999_999, ""
    return 999_999_999, chapter_code.lower()


def _category_tuple_for_family(family: str) -> tuple[str, str, str]:
    if family in KNOWN_CATEGORY_MAP:
        return KNOWN_CATEGORY_MAP[family]
    return "Scene Dialogue", _pretty_label(family), _pretty_label(family)


def _make_record(
    *,
    key: str,
    family: str,
    family_display: str,
    category: str,
    subcategory: str,
    story_group: str,
    chapter_code: str,
    chapter_label: str,
    conversation_key: str,
    conversation_label: str,
    scene_key: str,
    scene_label: str,
    scene_group: str,
    scene_part_a: str,
    scene_part_b: str,
    line_index: int | None,
    dialogue_type: str,
    speaker_role: str,
) -> DialogueRecord:
    speaker_name, speaker_display, speaker_confidence, speaker_bucket, speaker_slot = _speaker_info(speaker_role)
    speaker_key = (speaker_name or speaker_display or "Unknown").strip() or "Unknown"
    return DialogueRecord(
        key=key,
        source_path=LOCALIZATION_PATH,
        text_raw="",
        text_clean="",
        family=family,
        family_display=family_display,
        category=category,
        subcategory=subcategory,
        story_group=story_group,
        chapter_code=chapter_code,
        chapter_label=chapter_label,
        conversation_key=conversation_key,
        conversation_label=conversation_label,
        scene_key=scene_key,
        scene_label=scene_label,
        scene_group=scene_group,
        scene_part_a=scene_part_a,
        scene_part_b=scene_part_b,
        line_index=line_index,
        dialogue_type=dialogue_type,
        speaker_name=speaker_name,
        speaker_role=speaker_role,
        speaker_display=speaker_display,
        speaker_key=speaker_key,
        speaker_confidence=speaker_confidence,
        speaker_bucket=speaker_bucket,
        speaker_slot=speaker_slot,
    )


def _parse_questdialog(key: str) -> DialogueRecord | None:
    match = re.match(r"^questdialog_([^_]+)(?:_(.+))?$", key)
    if not match:
        return None

    dialogue_type = match.group(1)
    rest = match.group(2) or ""
    parts = rest.split("_") if rest else []
    parts, line_index = _extract_line_index(parts)
    chapter_code = _first_numeric_token(parts)
    chapter_label = f"Quest {chapter_code}" if chapter_code else "Quest Dialogue"
    scene_group = "_".join(parts)
    scene_part_a = parts[0] if parts else dialogue_type
    scene_part_b = "_".join(parts[1:]) if len(parts) > 1 else ""
    subcategory_map = {
        "hello": "Quest Greeting",
        "main": "Quest Main",
        "contents": "Quest Side Content",
        "quest": "Quest Dialogue",
        "faction": "Faction Quest Dialogue",
        "pywel": "Pywel Dialogue",
        "day2": "Quest Dialogue",
    }
    scene_key = key.rsplit("_", 1)[0] if line_index is not None else key
    return _make_record(
        key=key,
        family="questdialog",
        family_display="Questdialog",
        category="Quest Dialogue",
        subcategory=subcategory_map.get(dialogue_type, "Quest Dialogue"),
        story_group="Quest Dialogue",
        chapter_code=chapter_code,
        chapter_label=chapter_label,
        conversation_key=scene_key,
        conversation_label=scene_group or dialogue_type,
        scene_key=scene_key,
        scene_label=scene_group or dialogue_type,
        scene_group=scene_group,
        scene_part_a=scene_part_a,
        scene_part_b=scene_part_b,
        line_index=line_index,
        dialogue_type=dialogue_type,
        speaker_role="quest_npc",
    )


def _parse_ai_dialogue(key: str, grouped: bool) -> DialogueRecord | None:
    prefix = "aidialogstringinfogroup_" if grouped else "aidialogstringinfo_"
    if not key.startswith(prefix):
        return None

    rest = key[len(prefix):]
    parts = rest.split("_")
    parts, line_index = _extract_line_index(parts)
    dialogue_type = "_".join(parts)
    scene_key = key.rsplit("_", 1)[0] if line_index is not None else key
    return _make_record(
        key=key,
        family="aidialogstringinfogroup" if grouped else "aidialogstringinfo",
        family_display="Aidialogstringinfogroup" if grouped else "Aidialogstringinfo",
        category="AI Dialogue",
        subcategory="AI Group Dialogue" if grouped else "AI Ambient Dialogue",
        story_group="AI Dialogue",
        chapter_code="",
        chapter_label="Ambient Dialogue",
        conversation_key=scene_key,
        conversation_label=dialogue_type or prefix.rstrip("_"),
        scene_key=scene_key,
        scene_label=dialogue_type or prefix.rstrip("_"),
        scene_group=dialogue_type,
        scene_part_a=parts[0] if parts else "",
        scene_part_b="_".join(parts[1:]) if len(parts) > 1 else "",
        line_index=line_index,
        dialogue_type=dialogue_type,
        speaker_role="ai_group" if grouped else "ai_npc",
    )


def _parse_structured_dialogue(key: str) -> DialogueRecord | None:
    parts = key.split("_")
    if len(parts) < 3:
        return None

    parts_no_line, line_index = _extract_line_index(parts)
    if line_index is None:
        return None

    speaker_role, role_tokens = _extract_role_from_tail(parts_no_line)
    if not speaker_role:
        return None

    family = parts_no_line[0].lower()
    family_display = _pretty_label(parts_no_line[0])
    category, subcategory, story_group = _category_tuple_for_family(family)
    conversation_parts = parts_no_line[:-role_tokens]
    conversation_key = "_".join(conversation_parts)
    scene_key = "_".join(parts_no_line)
    scene_body = conversation_parts[1:]
    scene_group = "_".join(scene_body)
    scene_part_a = scene_body[0] if scene_body else ""
    scene_part_b = "_".join(scene_body[1:]) if len(scene_body) > 1 else ""
    chapter_code = _first_numeric_token(scene_body)
    chapter_label = f"{story_group} {chapter_code}" if chapter_code else story_group
    conversation_label = scene_group or family_display
    scene_label = "_".join(parts_no_line[1:]) or family_display
    return _make_record(
        key=key,
        family=family,
        family_display=family_display,
        category=category,
        subcategory=subcategory,
        story_group=story_group,
        chapter_code=chapter_code,
        chapter_label=chapter_label,
        conversation_key=conversation_key,
        conversation_label=conversation_label,
        scene_key=scene_key,
        scene_label=scene_label,
        scene_group=scene_group,
        scene_part_a=scene_part_a,
        scene_part_b=scene_part_b,
        line_index=line_index,
        dialogue_type=speaker_role,
        speaker_role=speaker_role,
    )


def _parse_record_shell(key: str) -> DialogueRecord | None:
    if key.startswith("questdialog_"):
        return _parse_questdialog(key)
    if key.startswith("aidialogstringinfo_"):
        return _parse_ai_dialogue(key, grouped=False)
    if key.startswith("aidialogstringinfogroup_"):
        return _parse_ai_dialogue(key, grouped=True)
    return _parse_structured_dialogue(key)


def _build_search_text(record: DialogueRecord) -> str:
    parts = [
        record.key,
        record.family,
        record.family_display,
        record.category,
        record.subcategory,
        record.story_group,
        record.chapter_code,
        record.chapter_label,
        record.conversation_key,
        record.conversation_label,
        record.scene_key,
        record.scene_label,
        record.scene_group,
        record.scene_part_a,
        record.scene_part_b,
        record.dialogue_type,
        record.speaker_name,
        record.speaker_role,
        record.speaker_display,
        record.speaker_key,
        record.speaker_bucket,
        record.text_clean,
    ]
    for mention in record.mentions:
        parts.extend([mention.kind, mention.token, mention.label])
    return " ".join(part for part in parts if part).lower()


def _apply_scene_level_speaker_inference(records: list[DialogueRecord]) -> None:
    """Upgrade speaker names when a conversation exposes one clear character.

    Example that this helps:
    - `okuro_boss_00_boss_00_*`
    - another line in the same conversation references
      `{StaticInfo:Character:Boss_Forgotten_General_55066#the forgotten general}`

    This stays conservative:
    - only boss-role lines are upgraded
    - only if the conversation references exactly one unique Character label
    """
    by_conversation: dict[str, list[DialogueRecord]] = defaultdict(list)
    for record in records:
        by_conversation[record.conversation_key].append(record)

    for conversation_records in by_conversation.values():
        character_labels = {
            mention.label
            for record in conversation_records
            for mention in record.mentions
            if mention.kind.lower() == "character"
        }
        if len(character_labels) != 1:
            continue
        inferred_name = next(iter(character_labels))
        for record in conversation_records:
            if not record.speaker_role.startswith("boss"):
                continue
            record.speaker_name = inferred_name
            record.speaker_display = inferred_name
            record.speaker_key = inferred_name
            record.speaker_confidence = "scene_character"


def _build_conversation_records(records: list[DialogueRecord]) -> list[DialogueConversationRecord]:
    by_conversation: dict[str, list[DialogueRecord]] = defaultdict(list)
    for record in records:
        by_conversation[record.conversation_key].append(record)

    conversations: list[DialogueConversationRecord] = []
    for conversation_key, group in by_conversation.items():
        first = group[0]
        speaker_map: dict[str, str] = {}
        for record in group:
            speaker_map.setdefault(record.speaker_key, record.speaker_display)
        conversations.append(
            DialogueConversationRecord(
                conversation_key=conversation_key,
                conversation_label=first.conversation_label,
                story_group=first.story_group,
                chapter_code=first.chapter_code,
                chapter_label=first.chapter_label,
                family=first.family,
                family_display=first.family_display,
                category=first.category,
                subcategory=first.subcategory,
                line_count=len(group),
                non_empty_line_count=sum(1 for record in group if record.text_clean),
                scene_count=len({record.scene_key for record in group}),
                speaker_keys=sorted(speaker_map),
                speaker_labels=[speaker_map[key] for key in sorted(speaker_map)],
            )
        )

    conversations.sort(
        key=lambda conv: (
            CATEGORY_ORDER.get(conv.category, 99),
            conv.story_group.lower(),
            _chapter_sort_value(conv.chapter_code),
            conv.conversation_key.lower(),
        )
    )
    return conversations


def _build_speaker_records(records: list[DialogueRecord]) -> list[DialogueSpeakerRecord]:
    by_speaker: dict[str, list[DialogueRecord]] = defaultdict(list)
    for record in records:
        by_speaker[record.speaker_key].append(record)

    speakers: list[DialogueSpeakerRecord] = []
    for speaker_key, group in by_speaker.items():
        first = group[0]
        confidence_order = {"scene_character": 0, "role": 1, "family": 2, "unknown": 3}
        best_confidence = min((record.speaker_confidence for record in group), key=lambda v: confidence_order.get(v, 9))
        speakers.append(
            DialogueSpeakerRecord(
                speaker_key=speaker_key,
                speaker_display=first.speaker_display,
                speaker_name=first.speaker_name,
                speaker_bucket=first.speaker_bucket,
                speaker_confidence=best_confidence,
                line_count=len(group),
                conversation_count=len({record.conversation_key for record in group}),
                family_count=len({record.family for record in group}),
                story_groups=sorted({record.story_group for record in group}),
            )
        )

    speakers.sort(key=lambda speaker: (speaker.speaker_bucket.lower(), speaker.speaker_display.lower()))
    return speakers


def build_dialogue_catalog(vfs: VfsManager, progress_fn: PROGRESS_FN | None = None) -> DialogueCatalogData:
    """Pure (uncached) build. Always reparses + rebuilds.

    Most callers should use :func:`build_dialogue_catalog_cached`
    instead — it returns the same data but skips the 30-90 s rebuild
    on second open.
    """
    _progress(progress_fn, "Loading localization entries...")
    entries = _load_localization_entries(vfs)

    _progress(progress_fn, "Parsing dialogue families from live keys...")
    records: list[DialogueRecord] = []
    for entry in entries:
        if entry.key[:1].isdigit():
            continue
        record = _parse_record_shell(entry.key)
        if record is None:
            continue
        record.text_raw = entry.value
        record.text_clean = _clean_text(entry.value)
        record.mentions = _extract_mentions(entry.value)
        records.append(record)

    _progress(progress_fn, "Applying scene-level speaker inference...")
    _apply_scene_level_speaker_inference(records)

    _progress(progress_fn, "Building search index and sort order...")
    for record in records:
        record.search_text = _build_search_text(record)

    records.sort(
        key=lambda record: (
            CATEGORY_ORDER.get(record.category, 99),
            record.story_group.lower(),
            _chapter_sort_value(record.chapter_code),
            record.conversation_key.lower(),
            record.scene_key.lower(),
            record.line_index if record.line_index is not None else -1,
            record.key.lower(),
        )
    )

    _progress(progress_fn, "Summarizing conversations and speakers...")
    conversations = _build_conversation_records(records)
    speakers = _build_speaker_records(records)
    return DialogueCatalogData(records=records, conversations=conversations, speakers=speakers)


def build_dialogue_catalog_cached(
    vfs: VfsManager,
    progress_fn: PROGRESS_FN | None = None,
) -> DialogueCatalogData:
    """Disk-cached wrapper around :func:`build_dialogue_catalog`.

    The first call on a fresh game install runs the full build
    (30-90 s on a real install) and pickles the result to
    ``~/.crimsonforge/cache/dialogue_catalog.pkl``. Subsequent calls
    that find a matching fingerprint deserialize in ~100 ms.

    The fingerprint covers the only PAMT this build reads ("0020").
    When Steam patches the game, the PAMT's mtime changes, the
    fingerprint mismatches, and we transparently rebuild.
    """
    from utils import build_cache

    pamt_path = Path(vfs.packages_path) / "0020" / "0.pamt"
    fingerprint = build_cache.fingerprint_paths([pamt_path])

    cached = build_cache.load_cached("dialogue_catalog", fingerprint)
    if cached is not None:
        _progress(progress_fn, "Loaded dialogue catalog from cache.")
        return cached

    data = build_dialogue_catalog(vfs, progress_fn=progress_fn)
    _progress(progress_fn, "Caching dialogue catalog for next launch...")
    build_cache.save_cached("dialogue_catalog", fingerprint, data)
    return data


def write_dialogue_exports(data: DialogueCatalogData, output_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    families_dir = out_dir / "families"
    categories_dir = out_dir / "categories"
    families_dir.mkdir(parents=True, exist_ok=True)
    categories_dir.mkdir(parents=True, exist_ok=True)

    all_json = out_dir / "dialogue_catalog.json"
    all_csv = out_dir / "dialogue_catalog.csv"
    conversation_json = out_dir / "dialogue_conversations.json"
    conversation_csv = out_dir / "dialogue_conversations.csv"
    speakers_json = out_dir / "dialogue_speakers.json"
    speakers_csv = out_dir / "dialogue_speakers.csv"
    summary_json = out_dir / "dialogue_summary.json"

    payload = data.to_dict()
    all_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_json.write_text(json.dumps(payload["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    conversation_json.write_text(json.dumps(payload["conversations"], ensure_ascii=False, indent=2), encoding="utf-8")
    speakers_json.write_text(json.dumps(payload["speakers"], ensure_ascii=False, indent=2), encoding="utf-8")

    record_fields = [
        "key",
        "family",
        "family_display",
        "category",
        "subcategory",
        "story_group",
        "chapter_code",
        "chapter_label",
        "conversation_key",
        "conversation_label",
        "scene_key",
        "scene_label",
        "scene_group",
        "scene_part_a",
        "scene_part_b",
        "line_index",
        "dialogue_type",
        "speaker_name",
        "speaker_role",
        "speaker_display",
        "speaker_key",
        "speaker_bucket",
        "speaker_slot",
        "speaker_confidence",
        "mentions",
        "text_raw",
        "text_clean",
        "source_path",
    ]
    conversation_fields = [
        "conversation_key",
        "conversation_label",
        "story_group",
        "chapter_code",
        "chapter_label",
        "family",
        "family_display",
        "category",
        "subcategory",
        "line_count",
        "non_empty_line_count",
        "scene_count",
        "speaker_keys",
        "speaker_labels",
    ]
    speaker_fields = [
        "speaker_key",
        "speaker_display",
        "speaker_name",
        "speaker_bucket",
        "speaker_confidence",
        "line_count",
        "conversation_count",
        "family_count",
        "story_groups",
    ]

    def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    record_rows: list[dict] = []
    for record in data.records:
        record_rows.append(
            {
                "key": record.key,
                "family": record.family,
                "family_display": record.family_display,
                "category": record.category,
                "subcategory": record.subcategory,
                "story_group": record.story_group,
                "chapter_code": record.chapter_code,
                "chapter_label": record.chapter_label,
                "conversation_key": record.conversation_key,
                "conversation_label": record.conversation_label,
                "scene_key": record.scene_key,
                "scene_label": record.scene_label,
                "scene_group": record.scene_group,
                "scene_part_a": record.scene_part_a,
                "scene_part_b": record.scene_part_b,
                "line_index": record.line_index,
                "dialogue_type": record.dialogue_type,
                "speaker_name": record.speaker_name,
                "speaker_role": record.speaker_role,
                "speaker_display": record.speaker_display,
                "speaker_key": record.speaker_key,
                "speaker_bucket": record.speaker_bucket,
                "speaker_slot": record.speaker_slot,
                "speaker_confidence": record.speaker_confidence,
                "mentions": "; ".join(f"{m.kind}:{m.token}:{m.label}" for m in record.mentions),
                "text_raw": record.text_raw,
                "text_clean": record.text_clean,
                "source_path": record.source_path,
            }
        )
    write_csv(all_csv, record_fields, record_rows)

    write_csv(
        conversation_csv,
        conversation_fields,
        [
            {
                "conversation_key": conversation.conversation_key,
                "conversation_label": conversation.conversation_label,
                "story_group": conversation.story_group,
                "chapter_code": conversation.chapter_code,
                "chapter_label": conversation.chapter_label,
                "family": conversation.family,
                "family_display": conversation.family_display,
                "category": conversation.category,
                "subcategory": conversation.subcategory,
                "line_count": conversation.line_count,
                "non_empty_line_count": conversation.non_empty_line_count,
                "scene_count": conversation.scene_count,
                "speaker_keys": "; ".join(conversation.speaker_keys),
                "speaker_labels": "; ".join(conversation.speaker_labels),
            }
            for conversation in data.conversations
        ],
    )
    write_csv(
        speakers_csv,
        speaker_fields,
        [
            {
                "speaker_key": speaker.speaker_key,
                "speaker_display": speaker.speaker_display,
                "speaker_name": speaker.speaker_name,
                "speaker_bucket": speaker.speaker_bucket,
                "speaker_confidence": speaker.speaker_confidence,
                "line_count": speaker.line_count,
                "conversation_count": speaker.conversation_count,
                "family_count": speaker.family_count,
                "story_groups": "; ".join(speaker.story_groups),
            }
            for speaker in data.speakers
        ],
    )

    by_family: dict[str, list[dict]] = defaultdict(list)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in record_rows:
        by_family[row["family"]].append(row)
        by_category[row["category"]].append(row)

    for family, rows in by_family.items():
        write_csv(families_dir / f"{family}.csv", record_fields, rows)
    for category, rows in by_category.items():
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", category.strip()).strip("_") or "category"
        write_csv(categories_dir / f"{safe_name}.csv", record_fields, rows)

    return {
        "all_json": all_json,
        "all_csv": all_csv,
        "conversations_json": conversation_json,
        "conversations_csv": conversation_csv,
        "speakers_json": speakers_json,
        "speakers_csv": speakers_csv,
        "summary_json": summary_json,
        "families_dir": families_dir,
        "categories_dir": categories_dir,
    }
