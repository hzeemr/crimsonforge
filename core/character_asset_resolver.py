"""Resolve every game file related to a single character.

Given a search string ("ogre", "hexe", "marie") or a character key
(``CD_M0001_00_Ogre``), this module walks the live game archives
and returns a categorised :class:`CharacterAssetBundle` listing
every mesh, animation, texture, morph file, physics file, effect,
sequencer, prefab, XML config, and database row that mentions the
character.

Why this exists
---------------
The Explorer right-click flow surfaces files one at a time. Modders
who want to retex / reskin / re-rig a single character end up
hunting for the related files manually — the Ogre alone has 506
files with "ogre" in the name PLUS 19 ``.pabgb`` tables that
reference it by character-key only. Asking the user to find all
those by hand is not a workflow.

The resolver does that hunt in one pass: enumerate every PAMT,
filter by name match, then content-scan the database tables for
the character-key. Result is a single object you can render in the
Character Hub, bulk-export to OBJ, or hand to the Blender helper.

Implementation notes
--------------------
* Two-phase search: name-based filename match first (cheap), then
  binary content search of the ~20 most-relevant ``.pabgb`` /
  ``.xml`` system files (medium cost). The combined result is the
  exhaustive view modders ask for.
* Categorisation is presentation-driven, not file-extension-driven
  — we want the Hub to show "Mesh / Skeleton / Morph / Animation /
  Physics / Effects / Cutscene / Texture / UI / Database / Other"
  not 22 different extensions.
* Safe to call repeatedly — the resolver does not cache, so each
  call sees fresh game state. Callers that want caching should
  wrap the result themselves.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.character_asset_resolver")


# ── Categories the Hub renders ──────────────────────────────────
#
# Order is presentation order — first entries are most important
# for modders, later ones are reference / database.

CATEGORIES = (
    "Mesh",
    "Skeleton",
    "Morph",
    "Appearance / Prefab",
    "Animation",
    "Physics",
    "Effects",
    "Sequencer / Cutscene",
    "Texture",
    "UI",
    "Database (game data)",
    "Localization",
    "Audio",
    "Other",
)

# Extension → category mapping. Anything not listed falls into "Other".
_EXT_TO_CAT = {
    ".pac": "Mesh",
    ".pam": "Mesh",
    ".pamlod": "Mesh",
    ".pami": "Mesh",
    ".meshinfo": "Mesh",
    ".pac_xml": "Mesh",
    ".pab": "Skeleton",
    ".paatt": "Skeleton",
    ".papr": "Skeleton",
    ".pabc": "Morph",
    ".pabv": "Morph",
    ".paccd": "Morph",
    ".prefab": "Appearance / Prefab",
    ".prefabdata_xml": "Appearance / Prefab",
    ".app_xml": "Appearance / Prefab",
    ".paa": "Animation",
    ".paa_metabin": "Animation",
    ".motionblending": "Animation",
    ".paac": "Animation",
    ".paasmt": "Animation",
    ".pabc_anim": "Animation",
    ".hkx": "Physics",
    ".pae": "Effects",
    ".paseq": "Sequencer / Cutscene",
    ".paseqc": "Sequencer / Cutscene",
    ".pastage": "Sequencer / Cutscene",
    ".uianiminit": "Sequencer / Cutscene",
    ".dds": "Texture",
    ".tex": "Texture",
    ".png": "Texture",
    ".html": "UI",
    ".css": "UI",
    ".mp4": "UI",
    ".pabgb": "Database (game data)",
    ".paloc": "Localization",
    ".wem": "Audio",
    ".bnk": "Audio",
    ".pasound": "Audio",
}


def _category_for(path: str) -> str:
    ext = os.path.splitext(path.lower())[1]
    return _EXT_TO_CAT.get(ext, "Other")


# ── Database tables we content-scan ────────────────────────────
#
# These are the ~20 system .pabgb tables that join characters by
# character-key. Limiting the scan to this list keeps the resolver
# fast (~3 seconds on a stock install) while still catching every
# table modders care about.

_SYSTEM_PABGB_TABLES = (
    "gamedata/characterinfo.pabgb",
    "gamedata/dropsetinfo.pabgb",
    "gamedata/iteminfo.pabgb",
    "gamedata/questinfo.pabgb",
    "gamedata/missioninfo.pabgb",
    "gamedata/knowledgeinfo.pabgb",
    "gamedata/gimmickinfo.pabgb",
    "gamedata/gimmickgroupinfo.pabgb",
    "gamedata/dialogvoiceinfo.pabgb",
    "gamedata/buffinfo.pabgb",
    "gamedata/conditioninfo.pabgb",
    "gamedata/faction.pabgb",
    "gamedata/gameadviceinfo.pabgb",
    "gamedata/gameeventhandler.pabgb",
    "gamedata/multichangeinfo.pabgb",
    "gamedata/partprefabdyeslotinfo.pabgb",
    "gamedata/stageinfo.pabgb",
    "gamedata/stringinfo.pabgb",
    "gamedata/uimaptextureinfo.pabgb",
)

# Same idea for engine system XMLs.
_SYSTEM_XML_FILES = (
    "actionchart/characteractionpackagedescription.xml",
    "character/animationdirectoryskeletonredirectioninfo.xml",
    "effect/effect_action.xml",
    "effect/effect_sequencer.xml",
    "sound/soundbanksinfo.xml",
    "ui/uigameconfig2.xml",
    "ui/cd_image_knowledgeimage_character.xml",
    "ui/cd_image_questimage_00.xml",
    "ui/cd_item_icon.xml",
)


# ── Public types ───────────────────────────────────────────────

@dataclass
class AssetEntry:
    """One file related to the resolved character."""
    path: str           # canonical VFS path
    category: str       # one of CATEGORIES
    package_group: str  # the PAMT group it belongs to
    size: int = 0
    # Why we included this file. Either "name match" (filename
    # contained the search needle) or "content match: <pabgb path>"
    # so users can trace why each file is listed.
    reason: str = ""


@dataclass
class CharacterAssetBundle:
    """Complete resolution result for a character search."""
    needle: str                                 # the original search term
    canonical_key: str = ""                     # e.g. "CD_M0001_00_Ogre"
    entries: list[AssetEntry] = field(default_factory=list)

    # Convenience accessor — entries grouped by category in
    # presentation order. Categories with zero entries are
    # omitted from the dict so the UI can iterate cleanly.
    @property
    def by_category(self) -> dict[str, list[AssetEntry]]:
        out: dict[str, list[AssetEntry]] = {}
        for cat in CATEGORIES:
            hits = [e for e in self.entries if e.category == cat]
            if hits:
                out[cat] = sorted(hits, key=lambda x: x.path.lower())
        return out

    @property
    def total_files(self) -> int:
        return len(self.entries)

    @property
    def total_size_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def first_mesh(self) -> Optional[AssetEntry]:
        """First .pac entry — useful for previewing in the Hub."""
        for e in self.entries:
            if e.path.lower().endswith(".pac"):
                return e
        return None


# ── Public API ─────────────────────────────────────────────────

def resolve_character_assets(vfs, needle: str) -> CharacterAssetBundle:
    """Walk the live game archives and return every file
    related to ``needle``.

    Parameters
    ----------
    vfs
        A loaded :class:`core.vfs_manager.VfsManager`.
    needle
        Search term — character name fragment ("ogre"), full
        character key ("CD_M0001_00_Ogre"), or any substring that
        identifies the character. Case-insensitive.

    Returns
    -------
    CharacterAssetBundle
        Categorised entries + canonical key. Empty bundle (no
        entries, empty key) if nothing matches.
    """
    if not needle or len(needle.strip()) < 2:
        return CharacterAssetBundle(needle=needle)

    needle_lower = needle.strip().lower()
    bundle = CharacterAssetBundle(needle=needle)

    # Phase 1 — name match across every PAMT.
    seen_paths: set[str] = set()
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception as exc:
            logger.debug("skipping group %s: %s", group_dir, exc)
            continue
        for entry in pamt.file_entries:
            path_lower = entry.path.lower()
            if needle_lower in path_lower and path_lower not in seen_paths:
                seen_paths.add(path_lower)
                bundle.entries.append(AssetEntry(
                    path=entry.path,
                    category=_category_for(entry.path),
                    package_group=group_dir,
                    size=entry.orig_size,
                    reason="name match",
                ))

    # Phase 2 — content scan of the system .pabgb + .xml tables for
    # the character-key. We only add the table itself (one entry
    # per table per match), not every row inside it.
    needle_bytes_variants = (
        needle.encode("utf-8"),
        needle.lower().encode("utf-8"),
        needle.upper().encode("utf-8"),
        needle.capitalize().encode("utf-8"),
    )
    for sys_path in _SYSTEM_PABGB_TABLES + _SYSTEM_XML_FILES:
        if sys_path.lower() in seen_paths:
            continue
        # Locate the entry in the relevant group.
        entry = _find_entry(vfs, sys_path)
        if entry is None:
            continue
        try:
            data = vfs.read_entry_data(entry)
        except Exception:
            continue
        if any(n in data for n in needle_bytes_variants):
            seen_paths.add(sys_path.lower())
            bundle.entries.append(AssetEntry(
                path=entry.path,
                category=_category_for(entry.path),
                package_group=_group_of(entry, vfs),
                size=len(data),
                reason="content match (database / system XML)",
            ))

    # Phase 3 — localization paloc files. These almost always
    # contain the character name in every shipped language. We
    # do a light content scan and flag any paloc that mentions
    # the needle.
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception:
            continue
        for entry in pamt.file_entries:
            if not entry.path.lower().endswith(".paloc"):
                continue
            if entry.path.lower() in seen_paths:
                continue
            try:
                data = vfs.read_entry_data(entry)
            except Exception:
                continue
            if any(n in data for n in needle_bytes_variants):
                seen_paths.add(entry.path.lower())
                bundle.entries.append(AssetEntry(
                    path=entry.path,
                    category="Localization",
                    package_group=group_dir,
                    size=len(data),
                    reason="content match (localization)",
                ))

    # Derive a canonical key from the most-common ``cd_<family>_*``
    # filename prefix in the bundle. Best-effort — used as a label
    # in the Hub header, not for any lookup logic.
    bundle.canonical_key = _derive_canonical_key(bundle)

    logger.info(
        "resolved %d files for needle %r (canonical key: %r)",
        len(bundle.entries), needle, bundle.canonical_key,
    )
    return bundle


# ── Internal helpers ─────────────────────────────────────────

def _find_entry(vfs, path: str):
    """Look up a single VFS entry by its full path. Returns the
    PamtFileEntry or None.
    """
    for group_dir in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group_dir)
        except Exception:
            continue
        for e in pamt.file_entries:
            if e.path.lower() == path.lower():
                return e
    return None


def _group_of(entry, vfs) -> str:
    """Best-effort: derive the package-group label for an entry by
    matching its paz_file path against the VFS packages root.
    """
    try:
        return os.path.basename(os.path.dirname(entry.paz_file))
    except Exception:
        return ""


def _derive_canonical_key(bundle: CharacterAssetBundle) -> str:
    """Derive ``CD_<Family>_NN_<Name>`` from the most-common file
    prefix in the bundle.

    Example: bundle full of files starting with
    ``character/cd_m0001_00_ogre_*`` returns ``CD_M0001_00_Ogre``.
    Returns the empty string if no prefix dominates.
    """
    counts: dict[str, int] = defaultdict(int)
    for e in bundle.entries:
        bn = os.path.basename(e.path).lower()
        # Walk underscore-separated tokens and accumulate prefix
        # candidates of length 4 (cd_<fam>_<NN>_<name>).
        parts = bn.split("_")
        if len(parts) >= 4 and parts[0] == "cd":
            prefix = "_".join(parts[:4])
            counts[prefix] += 1
    if not counts:
        return ""
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    # Capitalise to title case so the Hub label reads nicely.
    return "_".join(p.upper() if p == "cd" else p.capitalize()
                    for p in best.split("_"))
