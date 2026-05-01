"""Structured item catalog and raw game-data table index.

Builds a deeper taxonomy for item records directly from game packages.
The catalog stays grounded in raw game fields such as internal item names,
equip types, and variant chains, while also exposing a normalized hierarchy
for browsing and filtering in the UI.
"""

from __future__ import annotations

import csv
import json
import re
import struct
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from core.item_index import build_hash_table
from core.vfs_manager import VfsManager


ITEMINFO_MARKER = b"\x00\x01\x00\x00\x00\x00\x00\x00\x00\x07\x70\x00\x00\x00"
ASCII_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_]{3,80}")

ACCESSORY_TYPES = {"Earring", "Necklace", "Ring", "Bracelet", "Belt"}
ARMOR_TYPES = {"Helm", "Upperbody", "Hand", "Foot", "Cloak", "Mask", "Glass", "Gauntlet"}
SHIELD_TYPES = {"OneHandShield", "OneHandShieldRight", "OneHandTowerShield", "Shield"}
MOUNT_PET_TYPES = {
    "PetHelm",
    "PetArmor",
    "HorseArmor",
    "HorseHelm",
    "HorseSaddle",
    "HorseStirrup",
    "HorseShoe",
    "Wheel",
    "BackPack",
}
WEAPON_TYPES = {
    "OneHandSword",
    "OneHandBow",
    "OneHandDagger",
    "TwoHandAxe",
    "OneHandAxe",
    "TwoHandSword",
    "TwoHandGiantSword",
    "OneHandMace",
    "TwoHandWarHammer",
    "TwoHandSpear",
    "TwoHandGiantSpear",
    "TwoHandPike",
    "OneHandTorch",
    "TwoHandRod",
    "TwoHandScythe",
    "OneHandFlail",
    "TwoHandHammer",
    "OneHandFist",
    "OneHandDrill",
    "OneHandSaw",
    "TwoHandHalberd",
    "OneHandPistol",
    "OneHandMusket",
    "OneHandShotgun",
    "OneHandCannon",
    "TwoHandCannon",
    "OneHandRapier",
    "TwoHandFlail",
    "TwoHandMace",
    "TwoHandGiantMace",
    "OneHandFan",
    "OneHandHammer",
    "TwoHandGiantAxe",
    "TwoHandGiantHammer",
    "OneHandCrossBow",
    "TwoHandFlamethrower",
    "TwoHandIcethrower",
    "TwoHandLightningthrower",
    "TwoHandBlowPipe",
    "OneHandBomb",
    "TwoHandFlag",
    "Lantern",
    "OneHandBola",
    "RobotFist",
    "RobotCannon",
    "RobotGatling",
    "RobotLaser",
    "RobotFlameThrower",
    "RobotTongs",
    "RobotWelding",
    "Weapon",
    "GhostWeapon",
    "Battery",
}

WEAPON_GROUPS = {
    "OneHandSword": ("Weapon", "Melee", "Sword"),
    "TwoHandSword": ("Weapon", "Melee", "Sword"),
    "TwoHandGiantSword": ("Weapon", "Melee", "Sword"),
    "OneHandRapier": ("Weapon", "Melee", "Rapier"),
    "OneHandDagger": ("Weapon", "Melee", "Dagger"),
    "OneHandAxe": ("Weapon", "Melee", "Axe"),
    "TwoHandAxe": ("Weapon", "Melee", "Axe"),
    "TwoHandGiantAxe": ("Weapon", "Melee", "Axe"),
    "OneHandMace": ("Weapon", "Melee", "Mace"),
    "OneHandHammer": ("Weapon", "Melee", "Hammer"),
    "TwoHandHammer": ("Weapon", "Melee", "Hammer"),
    "TwoHandWarHammer": ("Weapon", "Melee", "Hammer"),
    "TwoHandMace": ("Weapon", "Melee", "Hammer"),
    "TwoHandGiantHammer": ("Weapon", "Melee", "Hammer"),
    "TwoHandGiantMace": ("Weapon", "Melee", "Hammer"),
    "OneHandFist": ("Weapon", "Melee", "Fist"),
    "RobotFist": ("Weapon", "Mechanical", "Robot Weapon"),
    "OneHandBow": ("Weapon", "Ranged", "Bow"),
    "OneHandCrossBow": ("Weapon", "Ranged", "Crossbow"),
    "OneHandPistol": ("Weapon", "Firearm", "Pistol"),
    "OneHandMusket": ("Weapon", "Firearm", "Musket"),
    "OneHandShotgun": ("Weapon", "Firearm", "Shotgun"),
    "OneHandCannon": ("Weapon", "Firearm", "Cannon"),
    "TwoHandCannon": ("Weapon", "Firearm", "Cannon"),
    "RobotCannon": ("Weapon", "Mechanical", "Robot Weapon"),
    "RobotGatling": ("Weapon", "Mechanical", "Robot Weapon"),
    "RobotLaser": ("Weapon", "Mechanical", "Robot Weapon"),
    "RobotFlameThrower": ("Weapon", "Mechanical", "Robot Weapon"),
    "RobotTongs": ("Weapon", "Mechanical", "Robot Weapon"),
    "RobotWelding": ("Weapon", "Mechanical", "Robot Weapon"),
    "TwoHandSpear": ("Weapon", "Polearm", "Spear"),
    "TwoHandGiantSpear": ("Weapon", "Polearm", "Spear"),
    "TwoHandPike": ("Weapon", "Polearm", "Pike"),
    "TwoHandHalberd": ("Weapon", "Polearm", "Halberd"),
    "OneHandTorch": ("Weapon", "Special", "Torch"),
    "TwoHandRod": ("Weapon", "Special", "Rod"),
    "TwoHandScythe": ("Weapon", "Special", "Scythe"),
    "OneHandFlail": ("Weapon", "Special", "Flail"),
    "TwoHandFlail": ("Weapon", "Special", "Flail"),
    "OneHandFan": ("Weapon", "Special", "Fan"),
    "Lantern": ("Weapon", "Special", "Lantern"),
    "OneHandBola": ("Weapon", "Special", "Bola"),
    "OneHandBomb": ("Weapon", "Special", "Bomb"),
    "TwoHandFlag": ("Weapon", "Special", "Flag"),
    "TwoHandFlamethrower": ("Weapon", "Special", "Elemental Thrower"),
    "TwoHandIcethrower": ("Weapon", "Special", "Elemental Thrower"),
    "TwoHandLightningthrower": ("Weapon", "Special", "Elemental Thrower"),
    "TwoHandBlowPipe": ("Weapon", "Special", "Blow Pipe"),
    "OneHandDrill": ("Weapon", "Utility Weapon", "Drill"),
    "OneHandSaw": ("Weapon", "Utility Weapon", "Saw"),
    "Weapon": ("Weapon", "Generic", "Unknown Weapon"),
    "GhostWeapon": ("Weapon", "Special", "Ghost Weapon"),
    "Battery": ("Weapon", "Special", "Battery Weapon"),
}

ARMOR_GROUPS = {
    "Helm": ("Armor", "Head", "Helm"),
    "Upperbody": ("Armor", "Body", "Chest"),
    "Hand": ("Armor", "Hands", "Gloves"),
    "Foot": ("Armor", "Feet", "Boots"),
    "Cloak": ("Armor", "Back", "Cloak"),
    "Mask": ("Armor", "Face", "Mask"),
    "Glass": ("Armor", "Face", "Glasses"),
    "Gauntlet": ("Armor", "Hands", "Gauntlet"),
}

MOUNT_PET_GROUPS = {
    "PetHelm": ("Mount & Pet Gear", "Pet", "Head"),
    "PetArmor": ("Mount & Pet Gear", "Pet", "Body"),
    "HorseArmor": ("Mount & Pet Gear", "Horse", "Body"),
    "HorseHelm": ("Mount & Pet Gear", "Horse", "Head"),
    "HorseSaddle": ("Mount & Pet Gear", "Horse", "Saddle"),
    "HorseStirrup": ("Mount & Pet Gear", "Horse", "Stirrup"),
    "HorseShoe": ("Mount & Pet Gear", "Horse", "Shoe"),
    "Wheel": ("Mount & Pet Gear", "Vehicle", "Wheel"),
    "BackPack": ("Mount & Pet Gear", "Pack", "BackPack"),
}

RAW_TABLE_DOMAINS = {
    "Items & Economy": (
        "item",
        "equip",
        "inventory",
        "store",
        "craft",
        "socket",
        "drop",
        "royalsupply",
        "reserve",
    ),
    "Quests & Knowledge": ("quest", "mission", "knowledge", "wanted", "gameadvice"),
    "Combat & Skills": ("skill", "buff", "status", "effect", "quicktime", "pattern", "specialmode"),
    "Characters & NPCs": ("character", "npc", "ally", "mercenary", "tribe", "job", "relation"),
    "Factions": ("faction",),
    "World & Map": (
        "field",
        "level",
        "stage",
        "region",
        "terrainregion",
        "triggerregion",
        "uimap",
        "bitmapposition",
    ),
    "Gameplay & Spawning": (
        "actionpoint",
        "interaction",
        "detect",
        "gameplay",
        "gimmick",
        "globalgameevent",
        "autospawn",
        "spawn",
        "formation",
        "sequencer",
    ),
    "Materials & Appearance": ("material", "dye", "partprefab", "elementalmaterial"),
    "Text, Dialogue & UI": ("string", "dialog", "localstring", "failmessage", "uisocialaction", "keymap"),
    "System & Platform": ("platform", "entitlement", "vehicle", "vibrate"),
}

MATERIAL_TOKENS = {
    "ore": "Ore & Stone",
    "stone": "Ore & Stone",
    "crystal": "Gem & Crystal",
    "gem": "Gem & Crystal",
    "socket": "Socket Material",
    "leather": "Hide & Leather",
    "hide": "Hide & Leather",
    "fur": "Hide & Leather",
    "bone": "Bone & Horn",
    "horn": "Bone & Horn",
    "wood": "Wood & Fiber",
    "log": "Wood & Fiber",
    "lumber": "Wood & Fiber",
    "cloth": "Cloth & Fabric",
    "fabric": "Cloth & Fabric",
    "thread": "Cloth & Fabric",
    "silk": "Cloth & Fabric",
    "seed": "Cooking Ingredient",
    "fruit": "Cooking Ingredient",
    "vegetable": "Cooking Ingredient",
    "meat": "Cooking Ingredient",
    "fish": "Cooking Ingredient",
    "seafood": "Cooking Ingredient",
    "egg": "Cooking Ingredient",
    "dairy": "Cooking Ingredient",
    "milk": "Cooking Ingredient",
    "powder": "Alchemy Material",
}

PROGRESS_FN = Callable[[str], None]


@dataclass(slots=True)
class ItemCatalogRecord:
    source: str
    item_id: int | None
    loc_key: str
    internal_name: str
    variant_base_name: str
    variant_level: int | None
    top_category: str
    category: str
    subcategory: str
    subsubcategory: str
    raw_type: str
    classification_source: str
    classification_confidence: str
    pac_files: list[str] = field(default_factory=list)
    prefab_hashes: list[int] = field(default_factory=list)
    search_text: str = ""


@dataclass(slots=True)
class GameDataTableRecord:
    path: str
    file_name: str
    extension: str
    domain: str
    subdomain: str
    has_header_pair: bool
    package_group: str


@dataclass(slots=True)
class ItemCatalogData:
    items: list[ItemCatalogRecord]
    tables: list[GameDataTableRecord]

    def to_dict(self) -> dict:
        return {
            "items": [asdict(item) for item in self.items],
            "tables": [asdict(table) for table in self.tables],
            "summary": {
                "item_count": len(self.items),
                "table_count": len(self.tables),
                "top_category_counts": dict(Counter(item.top_category for item in self.items)),
                "category_counts": dict(Counter(item.category for item in self.items)),
                "subcategory_counts": dict(Counter(item.subcategory for item in self.items)),
            },
        }


def _find_entry(vfs: VfsManager, group: str, needle: str):
    pamt = vfs.load_pamt(group)
    needle = needle.lower()
    for entry in pamt.file_entries:
        if needle in entry.path.lower():
            return entry
    return None


def _progress(progress_fn: PROGRESS_FN | None, message: str) -> None:
    if progress_fn:
        progress_fn(message)


def parse_equip_types(vfs: VfsManager) -> list[str]:
    entry = _find_entry(vfs, "0008", "equiptypeinfo.pabgb")
    if not entry:
        return []

    data = vfs.read_entry_data(entry)
    equip_types: list[str] = []
    seen: set[str] = set()

    for match in ASCII_RE.finditer(data):
        text = match.group().decode("ascii", errors="ignore")
        if text in seen or len(text) < 4 or not text[0].isalpha():
            continue
        seen.add(text)
        equip_types.append(text)

    equip_types.sort(key=len, reverse=True)
    return equip_types


def _pretty_label(value: str) -> str:
    value = value.replace(".am", "")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = value.replace("_", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _normalize_base_name(name: str) -> str:
    base = name.removesuffix(".am")
    return re.sub(r"_\d+$", "", base)


def _variant_level_from_name(name: str) -> int | None:
    match = re.search(r"_(\d+)\.am$", name)
    if not match:
        return None
    return int(match.group(1))


_BODY_ARMOR_SUFFIX_RE = re.compile(r"_armor$")
_MOUNT_PET_ARMOR_RE = re.compile(r"(?:^|_)(horse|pet)_?armor")


def _resolve_raw_type(base_name: str, equip_types: list[str]) -> tuple[str, str, str]:
    for equip_type in equip_types:
        if equip_type in base_name:
            return equip_type, "equiptypeinfo", "high"

    lower = base_name.lower()

    # Body/chest armor: names ending in "_Armor" (e.g. "Dartus_Leather_Armor",
    # "Sir_Catfish_PlateArmor_Armor"). The substring "armor" alone is ambiguous
    # because it also appears inside compound adjectives like "PlateArmor_Helm",
    # so we anchor on the trailing "_armor" token. Mount and pet variants are
    # excluded here so they flow into the specialised HorseArmor / PetArmor
    # branches of token_map below, which carry the correct slot semantics.
    if _BODY_ARMOR_SUFFIX_RE.search(lower) and not _MOUNT_PET_ARMOR_RE.search(lower):
        return "Upperbody", "name:_armor", "medium"

    token_map = [
        ("fishingrod", "Tool"),
        ("pickaxe", "Tool"),
        ("shovel", "Tool"),
        ("hoe", "Tool"),
        ("rake", "Tool"),
        ("broom", "Tool"),
        ("chainsaw", "Tool"),
        ("drill", "Tool"),
        ("towershield", "OneHandTowerShield"),
        ("shield", "Shield"),
        ("helmet", "Helm"),
        ("helm", "Helm"),
        ("hood", "Helm"),
        ("hat", "Helm"),
        ("crown", "Helm"),
        ("glove", "Hand"),
        ("gauntlet", "Gauntlet"),
        ("boot", "Foot"),
        ("shoe", "Foot"),
        ("cloak", "Cloak"),
        ("mask", "Mask"),
        ("glass", "Glass"),
        ("necklace", "Necklace"),
        ("earring", "Earring"),
        ("ring", "Ring"),
        ("bracelet", "Bracelet"),
        ("belt", "Belt"),
        ("crossbow", "OneHandCrossBow"),
        ("musket", "OneHandMusket"),
        ("shotgun", "OneHandShotgun"),
        ("pistol", "OneHandPistol"),
        ("cannon", "OneHandCannon"),
        ("rapier", "OneHandRapier"),
        ("dagger", "OneHandDagger"),
        ("halberd", "TwoHandHalberd"),
        ("pike", "TwoHandPike"),
        ("spear", "TwoHandSpear"),
        ("scythe", "TwoHandScythe"),
        ("flail", "OneHandFlail"),
        ("hammer", "TwoHandHammer"),
        ("mace", "OneHandMace"),
        ("axe", "OneHandAxe"),
        ("sword", "OneHandSword"),
        ("bow", "OneHandBow"),
        ("torch", "OneHandTorch"),
        ("rod", "TwoHandRod"),
        ("fan", "OneHandFan"),
        ("fist", "OneHandFist"),
        ("lantern", "Lantern"),
        ("bola", "OneHandBola"),
        ("bomb", "OneHandBomb"),
        ("saw", "OneHandSaw"),
        ("horsearmor", "HorseArmor"),
        ("horsehelm", "HorseHelm"),
        ("horsesaddle", "HorseSaddle"),
        ("horsestirrup", "HorseStirrup"),
        ("horseshoe", "HorseShoe"),
        ("petarmor", "PetArmor"),
        ("pethelm", "PetHelm"),
        ("backpack", "BackPack"),
    ]
    for token, resolved in token_map:
        if token in lower:
            return resolved, f"name:{token}", "medium"

    return "Unknown", "fallback", "low"


def _classify_from_raw_type(raw_type: str) -> tuple[str, str, str]:
    if raw_type in WEAPON_GROUPS:
        return WEAPON_GROUPS[raw_type]
    if raw_type in SHIELD_TYPES:
        return "Shield", "Defensive", _pretty_label(raw_type)
    if raw_type in ARMOR_GROUPS:
        return ARMOR_GROUPS[raw_type]
    if raw_type in ACCESSORY_TYPES:
        return "Accessory", "Jewelry", _pretty_label(raw_type)
    if raw_type in MOUNT_PET_GROUPS:
        return MOUNT_PET_GROUPS[raw_type]
    if raw_type == "Ammo":
        return "Ammo", "Projectile", "Ammo"
    if raw_type == "Tool":
        return "Tool", "Utility", "Tool"
    return "Misc", "Unknown", "Unknown"


def _classify_non_equipment(base_name: str) -> tuple[str, str, str, str, str]:
    lower = base_name.lower()
    if lower.startswith("quest_wantedpaper") or lower.startswith("wantedpaper") or lower.startswith("noticepaper"):
        return "Document & Quest", "Wanted", "Poster", "name-pattern", "high"
    if lower.startswith("quest_paper"):
        return "Document & Quest", "Quest", "Quest Paper", "name-pattern", "high"
    if lower.startswith("item_skill_"):
        return "Special", "Enhancement", "Skill Item", "name-pattern", "high"
    if lower.startswith("item_stat_"):
        return "Special", "Enhancement", "Stat Item", "name-pattern", "high"
    if lower.startswith("collection_"):
        return "Special", "Collection", "Collectible", "name-pattern", "high"
    if lower.startswith("sealed_"):
        return "Special", "Sealed", "Container", "name-pattern", "high"
    if lower.startswith("boss_reward"):
        return "Special", "Reward", "Boss Reward", "name-pattern", "high"
    if lower.startswith("recipe_book_"):
        return "Document & Quest", "Recipe", "Recipe Book", "name-pattern", "high"
    if lower.startswith("quest_"):
        return "Document & Quest", "Quest", "Quest Item", "name-pattern", "high"

    doc_patterns = [
        (("lostletter", "letter"), ("Document & Quest", "Letter", "Letter")),
        (("diary", "memo", "record", "report"), ("Document & Quest", "Record", "Diary / Report")),
        (("treasuremap",), ("Document & Quest", "Treasure Map", "Treasure Map")),
        (("recipe_book", "craft_recipe", "recipe"), ("Document & Quest", "Recipe", "Recipe Book")),
        (("manual",), ("Document & Quest", "Manual", "Manual")),
        (("permit",), ("Document & Quest", "Permit", "Permit")),
        (("book",), ("Document & Quest", "Book", "Book")),
        (("document",), ("Document & Quest", "Document", "Document")),
        (("artifact",), ("Document & Quest", "Artifact", "Artifact")),
        (("key",), ("Document & Quest", "Key", "Key")),
    ]
    for tokens, result in doc_patterns:
        if any(token in lower for token in tokens):
            return *result, "name-pattern", "high"

    if any(token in lower for token in ("food_", "fish", "shrimp", "crab", "squid", "seahorse", "starfish")):
        sub = "Seafood" if any(token in lower for token in ("shrimp", "crab", "squid", "seahorse", "starfish")) else "Fish"
        return "Consumable", "Food", sub, "name-pattern", "high"

    if any(token in lower for token in ("potion", "elixir", "medicine")):
        return "Consumable", "Potion", "Potion", "name-pattern", "high"

    if any(token in lower for token in ("bomb", "dart", "trap")):
        return "Consumable", "Throwable", "Bomb / Dart", "name-pattern", "high"

    if any(token in lower for token in ("pickaxe", "fishingrod", "hoe", "shovel", "rake", "broom", "drill", "chainsaw")):
        return "Tool", "Gathering", "Tool", "name-pattern", "high"

    for token, family in MATERIAL_TOKENS.items():
        if token in lower:
            return "Material", family, _pretty_label(token), "name-pattern", "medium"

    if "money" in lower or "coin" in lower:
        return "Utility", "Currency", "Money", "name-pattern", "medium"
    if "bag" in lower:
        return "Utility", "Container", "Bag", "name-pattern", "medium"

    return "Misc", "Unknown", "Unknown", "fallback", "low"


# Unambiguous equipment slot suffixes. When an item name ends with one of these,
# the equipment classifier runs first — otherwise "Dartus_Leather_Armor" falls
# into "Throwable/Bomb" (because "dart" appears inside "Dartus"), "Yorkey_Fabric_Armor"
# falls into "Key" (because of "key" inside "Yorkey"), and so on.
_EQUIP_SLOT_SUFFIX_RE = re.compile(
    r"_(armor|helm|helmet|hood|crown|cloak|gloves?|gauntlets?|boots?|shoes?|"
    r"mask|glass(?:es)?|belt|ring|necklace|earring|bracelet)$"
)


def classify_item_name(name: str, equip_types: list[str]) -> tuple[str, str, str, str, str, str]:
    base_name = _normalize_base_name(name)
    lower_base = base_name.lower()

    # Equipment slot suffixes are unambiguous — e.g. "Dartus_Leather_Armor" clearly
    # ends with "_Armor" and should be an Upperbody item regardless of the fact that
    # "dart" appears inside "Dartus". Prefer equipment classification for these cases
    # before falling back to the substring-based non-equipment classifier.
    prefer_equipment = bool(_EQUIP_SLOT_SUFFIX_RE.search(lower_base))

    if not prefer_equipment:
        top, category, subcategory, source, confidence = _classify_non_equipment(base_name)
        if confidence == "high":
            return top, category, subcategory, subcategory, "Unknown", source

    raw_type, raw_source, raw_confidence = _resolve_raw_type(base_name, equip_types)

    if raw_type in WEAPON_TYPES or raw_type in SHIELD_TYPES or raw_type in ARMOR_TYPES or raw_type in ACCESSORY_TYPES or raw_type in MOUNT_PET_TYPES:
        category, subcategory, subsubcategory = _classify_from_raw_type(raw_type)
        return "Equipment", category, subcategory, subsubcategory, raw_type, raw_source or raw_confidence

    if raw_type == "Ammo":
        return "Utility", "Ammo", "Projectile", "Ammo", raw_type, raw_source
    if raw_type == "Tool":
        return "Utility", "Tool", "Utility", "Tool", raw_type, raw_source

    top, category, subcategory, source, _confidence = _classify_non_equipment(base_name)
    return top, category, subcategory, subcategory, raw_type, source


def _build_search_text(record: ItemCatalogRecord) -> str:
    bits = [
        record.internal_name.lower(),
        record.variant_base_name.lower(),
        record.source.lower(),
        record.loc_key.lower(),
        record.top_category.lower(),
        record.category.lower(),
        record.subcategory.lower(),
        record.subsubcategory.lower(),
        record.raw_type.lower(),
        " ".join(p.lower() for p in record.pac_files),
        " ".join(str(h) for h in record.prefab_hashes),
    ]
    return " ".join(bit for bit in bits if bit).strip()


def _finalize_record(
    *,
    source: str,
    item_id: int | None,
    loc_key: str,
    internal_name: str,
    variant_base_name: str,
    variant_level: int | None,
    pac_files: list[str],
    prefab_hashes: list[int],
    equip_types: list[str],
) -> ItemCatalogRecord:
    top_category, category, subcategory, subsubcategory, raw_type, class_source = classify_item_name(
        internal_name,
        equip_types,
    )
    confidence = "high" if class_source == "equiptypeinfo" else ("medium" if class_source.startswith("name") else "low")
    record = ItemCatalogRecord(
        source=source,
        item_id=item_id,
        loc_key=loc_key,
        internal_name=internal_name,
        variant_base_name=variant_base_name,
        variant_level=variant_level,
        top_category=top_category,
        category=category,
        subcategory=subcategory,
        subsubcategory=subsubcategory,
        raw_type=raw_type,
        classification_source=class_source,
        classification_confidence=confidence,
        pac_files=pac_files,
        prefab_hashes=prefab_hashes,
    )
    record.search_text = _build_search_text(record)
    return record


def parse_iteminfo_records(vfs: VfsManager, equip_types: list[str]) -> list[ItemCatalogRecord]:
    entry = _find_entry(vfs, "0008", "iteminfo.pabgb")
    if not entry:
        return []

    data = vfs.read_entry_data(entry)
    pamt_0009 = vfs.load_pamt("0009")
    hash_table = build_hash_table(pamt_0009.file_entries)
    records: list[ItemCatalogRecord] = []
    seen_ids: set[int] = set()
    idx = 0

    while True:
        pos = data.find(ITEMINFO_MARKER, idx)
        if pos == -1:
            break
        idx = pos + len(ITEMINFO_MARKER)
        null_pos = pos
        name_start = null_pos
        while name_start > 0 and 0x21 <= data[name_start - 1] <= 0x7E:
            name_start -= 1
            if null_pos - name_start > 150:
                break

        if null_pos - name_start < 3 or name_start < 8:
            continue

        internal_name = data[name_start:null_pos].decode("ascii", errors="replace")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", internal_name):
            continue

        name_len = struct.unpack_from("<I", data, name_start - 4)[0]
        item_id = struct.unpack_from("<I", data, name_start - 8)[0]
        if name_len not in (len(internal_name), len(internal_name) + 1):
            continue
        if item_id < 100 or item_id > 100000000 or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        loc_key = ""
        loc_off = pos + 18
        if loc_off + 4 < len(data):
            loc_len = struct.unpack_from("<I", data, loc_off)[0]
            if 5 < loc_len < 25 and loc_off + 4 + loc_len <= len(data):
                loc_bytes = data[loc_off + 4:loc_off + 4 + loc_len]
                if all(0x30 <= b <= 0x39 for b in loc_bytes):
                    loc_key = loc_bytes.decode("ascii")

        prefab_hashes: list[int] = []
        search_end = min(len(data), pos + 800)
        for scan in range(pos + 14, search_end - 15):
            if data[scan] != 0x0E:
                continue
            count1 = struct.unpack_from("<I", data, scan + 3)[0]
            count2 = struct.unpack_from("<I", data, scan + 7)[0]
            if not (0 < count1 <= 5 and 0 < count2 <= 5):
                continue
            for hash_idx in range(count2):
                value = struct.unpack_from("<I", data, scan + 11 + hash_idx * 4)[0]
                if value != 0:
                    prefab_hashes.append(value)
            if prefab_hashes:
                break

        pac_files: list[str] = []
        for prefab_hash in prefab_hashes:
            resolved = hash_table.get(prefab_hash)
            if not resolved:
                continue
            base = resolved
            for suffix in ("_l", "_r", "_u", "_s", "_t", "_index01", "_index02", "_index03"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            pac_name = base + ".pac"
            if pac_name not in pac_files:
                pac_files.append(pac_name)

        records.append(
            _finalize_record(
                source="iteminfo",
                item_id=item_id,
                loc_key=loc_key,
                internal_name=internal_name,
                variant_base_name=_normalize_base_name(internal_name),
                variant_level=None,
                pac_files=pac_files,
                prefab_hashes=prefab_hashes,
                equip_types=equip_types,
            )
        )

    return records


def parse_multichange_records(vfs: VfsManager, equip_types: list[str]) -> list[ItemCatalogRecord]:
    entry = _find_entry(vfs, "0008", "multichangeinfo.pabgb")
    if not entry:
        return []

    data = vfs.read_entry_data(entry)
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    pattern = re.compile(r"(\d{16,20}).{0,200}?([A-Za-z][A-Za-z0-9_]+_\d+\.am)")
    seen_names: set[str] = set()
    records: list[ItemCatalogRecord] = []

    for loc_key, internal_name in pattern.findall(text):
        if internal_name in seen_names:
            continue
        seen_names.add(internal_name)
        records.append(
            _finalize_record(
                source="multichange",
                item_id=None,
                loc_key=loc_key,
                internal_name=internal_name,
                variant_base_name=_normalize_base_name(internal_name),
                variant_level=_variant_level_from_name(internal_name),
                pac_files=[],
                prefab_hashes=[],
                equip_types=equip_types,
            )
        )

    return records


def _infer_table_domain(file_name: str) -> tuple[str, str]:
    lower = file_name.lower()
    for domain, keywords in RAW_TABLE_DOMAINS.items():
        for keyword in keywords:
            if keyword in lower:
                return domain, keyword
    return "Other", "other"


def build_game_data_tables(vfs: VfsManager) -> list[GameDataTableRecord]:
    pamt = vfs.load_pamt("0008")
    file_set = {entry.path.lower() for entry in pamt.file_entries}
    tables: list[GameDataTableRecord] = []

    for entry in sorted(pamt.file_entries, key=lambda e: e.path.lower()):
        path = entry.path.replace("\\", "/")
        if not path.startswith("gamedata/"):
            continue
        lower_path = path.lower()
        if not lower_path.endswith(".pabgb"):
            continue
        file_name = Path(path).name
        domain, subdomain = _infer_table_domain(file_name)
        header_pair = lower_path[:-1] + "h"
        tables.append(
            GameDataTableRecord(
                path=path,
                file_name=file_name,
                extension=".pabgb",
                domain=domain,
                subdomain=subdomain,
                has_header_pair=header_pair in file_set,
                package_group="0008",
            )
        )

    return tables


def build_item_catalog(vfs: VfsManager, progress_fn: PROGRESS_FN | None = None) -> ItemCatalogData:
    """Pure (uncached) build. Always reparses + rebuilds.

    Most callers should use :func:`build_item_catalog_cached` instead
    — it returns the same data but skips the full rebuild on every
    open by reusing a pickled snapshot from the previous run.
    """
    _progress(progress_fn, "Loading equip types from game data...")
    equip_types = parse_equip_types(vfs)

    _progress(progress_fn, "Parsing base item records from iteminfo.pabgb...")
    base_items = parse_iteminfo_records(vfs, equip_types)

    _progress(progress_fn, "Parsing variant item records from multichangeinfo.pabgb...")
    variant_items = parse_multichange_records(vfs, equip_types)

    _progress(progress_fn, "Indexing structured game-data tables...")
    tables = build_game_data_tables(vfs)

    items = base_items + variant_items
    items.sort(
        key=lambda item: (
            item.top_category.lower(),
            item.category.lower(),
            item.subcategory.lower(),
            item.subsubcategory.lower(),
            item.variant_base_name.lower(),
            item.internal_name.lower(),
        )
    )
    tables.sort(key=lambda table: (table.domain.lower(), table.file_name.lower()))
    return ItemCatalogData(items=items, tables=tables)


def build_item_catalog_cached(
    vfs: VfsManager,
    progress_fn: PROGRESS_FN | None = None,
) -> ItemCatalogData:
    """Disk-cached wrapper around :func:`build_item_catalog`.

    First call parses every PABGB game-data file (slow); subsequent
    calls deserialize the pickled result in ~100 ms. The fingerprint
    covers PAMTs the build reads (0008 game-data + 0009 hash table) —
    a Steam patch bumps mtime there, invalidates the cache, and the
    next open transparently rebuilds.
    """
    from utils import build_cache

    pkg = Path(vfs.packages_path)
    fingerprint = build_cache.fingerprint_paths([
        pkg / "0008" / "0.pamt",
        pkg / "0009" / "0.pamt",
    ])

    cached = build_cache.load_cached("item_catalog", fingerprint)
    if cached is not None:
        _progress(progress_fn, "Loaded item catalog from cache.")
        return cached

    data = build_item_catalog(vfs, progress_fn=progress_fn)
    _progress(progress_fn, "Caching item catalog for next launch...")
    build_cache.save_cached("item_catalog", fingerprint, data)
    return data


def write_catalog_exports(data: ItemCatalogData, output_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    items_json = out_dir / "item_catalog_enriched.json"
    tables_json = out_dir / "game_data_tables.json"
    items_csv = out_dir / "item_catalog_enriched.csv"

    items_json.write_text(
        json.dumps(data.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tables_json.write_text(
        json.dumps([asdict(table) for table in data.tables], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with items_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "source",
            "item_id",
            "loc_key",
            "internal_name",
            "variant_base_name",
            "variant_level",
            "top_category",
            "category",
            "subcategory",
            "subsubcategory",
            "raw_type",
            "classification_source",
            "classification_confidence",
            "pac_files",
            "prefab_hashes",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in data.items:
            row = asdict(item)
            row["pac_files"] = ";".join(item.pac_files)
            row["prefab_hashes"] = ";".join(str(value) for value in item.prefab_hashes)
            row.pop("search_text", None)
            writer.writerow(row)

    return {
        "items_json": items_json,
        "tables_json": tables_json,
        "items_csv": items_csv,
    }
