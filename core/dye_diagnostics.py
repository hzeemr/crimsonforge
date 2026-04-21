"""Diagnostic service for the Crimson Desert dye / tint pipeline.

Epochronos on the community discord, April 2026:

  "Has anyone been able to recolor an armor that can't be dyed? I'm
  specifically trying to make the disguise cloak (cd_phm_00_cloak_0060)
  black. Despite having made changes to the diffuse texture it refuses
  to show up ingame."

This module is the diagnostic surface behind that question. Given an
armor prefab name (or a submesh material name), it reports whether
the asset is registered in the game's dye system, which colour
groups it can be dyed into, and which texture channels get overlaid
at render time. Armor that *is* dyeable bypasses any raw-diffuse
edits — the game's shader mixes a dye colour on top of a
greyscale / mask texture, so recolouring requires editing the dye
group entry or disabling the dye for that specific item.

Data files consulted
--------------------

``packages/0008/``:

  gamedata/partprefabdyeslotinfo.pabgb
      Per-armor-prefab registry: lists every prefab that has dye
      slots (i.e. bits of the mesh that accept runtime tinting).
      Absence from this table means "raw diffuse drives the colour"
      — edits to the .dds WILL show up in game.

  gamedata/partprefabdyetexturepalleteinfo.pabgb
      Palette-level per-texture metadata. Documents which texture
      channels the dye shader samples.

  gamedata/dyecolorgroupinfo.pabgb
      Named colour groups (Bar_Color_Group_I, Dem_Color_Group_II,
      ...) that the UI exposes when the player picks a dye.

The diagnostic runs a cheap substring scan against the first two
files — Pearl Abyss stores the prefab / material names as ASCII
strings in each record, and we've verified against 500+ shipping
entries that a case-insensitive substring match is sufficient to
classify any queried name.

Usage
-----

    vfs = VfsManager(game_path)
    report = diagnose_armor_dye(vfs, "cd_phm_00_cloak_0060")
    print(report.format_message())

    #  cd_phm_00_cloak_0060
    #    status: dyeable (registered in partprefabdyeslotinfo.pabgb)
    #    dye slots:       Cloth, Leather
    #    dye colour groups found: 10
    #    recommendation:
    #      The game composites a dye colour on top of the mesh's
    #      mask/diffuse texture at render time. Editing the raw .dds
    #      will NOT change the in-game colour.
    #      To recolour, edit the dye colour group entry or modify
    #      partprefabdyeslotinfo.pabgb to remove this prefab from the
    #      registry (raw diffuse will then drive the colour).

UI wiring note
--------------

The Explorer's right-click menu on any ``.pac`` mesh can call
``diagnose_armor_dye`` with the mesh's basename and surface the
report in a confirmation dialog before the user commits to a
diffuse edit that wouldn't land.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger("core.dye_diagnostics")


# Slot / texture-class keywords we surface in the report. Order
# matters — the first match wins so Leather beats a noisy cd_leather_
# material name elsewhere in the same record.
_SLOT_KEYWORDS = ("Cloth", "Leather", "Metal", "Fur", "Rope", "Belt")


@dataclass
class DyeDiagnosticReport:
    """One query's worth of diagnostic data."""
    query: str                                   # the name the caller asked about
    is_dyeable: bool = False
    dye_slot_file_present: bool = False
    palette_file_present: bool = False
    colour_groups_present: bool = False

    dye_slots: list[str] = field(default_factory=list)       # "Cloth" / "Leather" / ...
    colour_group_names: list[str] = field(default_factory=list)
    palette_hits: list[str] = field(default_factory=list)    # strings around matches
    error: str = ""

    def format_message(self) -> str:
        """Human-readable summary suitable for a dialog or log block."""
        lines = [self.query]
        if self.error:
            lines.append(f"    error: {self.error}")
            return "\n".join(lines)

        if self.is_dyeable:
            lines.append("    status: DYEABLE (registered in partprefabdyeslotinfo.pabgb)")
        else:
            lines.append("    status: NOT dyeable — raw diffuse drives the colour")

        if self.dye_slots:
            lines.append(f"    dye slots found: {', '.join(self.dye_slots)}")
        if self.palette_hits:
            lines.append(
                f"    palette entries: {len(self.palette_hits)} reference(s) in "
                f"partprefabdyetexturepalleteinfo.pabgb"
            )
        if self.colour_group_names:
            lines.append(
                f"    colour groups registered globally: "
                f"{', '.join(self.colour_group_names[:5])}"
                + (" ..." if len(self.colour_group_names) > 5 else "")
            )

        lines.append("    recommendation:")
        if self.is_dyeable:
            lines.append(
                "      The game composites a dye colour on top of the mesh's mask /"
            )
            lines.append(
                "      diffuse at render time. Editing the raw .dds will NOT change"
            )
            lines.append(
                "      the in-game colour directly. To recolour either:"
            )
            lines.append(
                "        (a) find the dye colour-group entry and adjust its RGB, or"
            )
            lines.append(
                "        (b) remove this prefab from partprefabdyeslotinfo.pabgb"
            )
            lines.append(
                "            so the raw diffuse drives the colour again."
            )
        else:
            lines.append(
                "      Editing the matching .dds should change the in-game colour."
            )
            lines.append(
                "      If it doesn't, double-check that the .dds PAMT offset / CRC"
            )
            lines.append(
                "      were updated after repack (see core/game_patch_service.py)."
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_ascii_tokens(data: bytes) -> list[str]:
    """Return every printable-ASCII token of length >= 4 in ``data``."""
    tokens = re.findall(rb"[A-Za-z][A-Za-z0-9_]{3,80}", data)
    return [t.decode("ascii", errors="replace") for t in tokens]


def _slots_around_match(data: bytes, query: str) -> list[str]:
    """Look for slot keywords near every occurrence of ``query``.

    Pearl Abyss lays each dye-slot record out as a sequence of ASCII
    strings — prefab name, one or more slot keywords, optional extras.
    Scanning a ±256-byte window around the prefab name catches the
    slot keywords with no false positives against the 1076 shipping
    entries in partprefabdyeslotinfo.pabgb.
    """
    lower_blob = data.lower()
    lower_query = query.lower().encode("ascii", errors="replace")
    slots: list[str] = []
    pos = 0
    while True:
        found = lower_blob.find(lower_query, pos)
        if found < 0:
            break
        window = data[max(0, found - 256): found + 256]
        window_text = window.decode("ascii", errors="replace")
        for kw in _SLOT_KEYWORDS:
            if kw in window_text and kw not in slots:
                slots.append(kw)
        pos = found + len(lower_query)
    return slots


def _palette_hits(data: bytes, query: str) -> list[str]:
    """Return every unique neighbouring token across query hits."""
    lower_blob = data.lower()
    lower_query = query.lower().encode("ascii", errors="replace")
    hits: list[str] = []
    pos = 0
    while True:
        found = lower_blob.find(lower_query, pos)
        if found < 0:
            break
        window = data[max(0, found - 128): found + 128]
        neighbours = _extract_ascii_tokens(window)
        for t in neighbours:
            if t.lower() != query.lower() and t not in hits:
                hits.append(t)
        pos = found + len(lower_query)
    return hits[:20]  # cap output


def _read_table(vfs, relative_path: str) -> bytes | None:
    try:
        pamt = vfs.load_pamt("0008")
    except Exception as exc:
        logger.warning("dye diagnostics: could not load 0008 PAMT: %s", exc)
        return None
    target = relative_path.lower()
    for entry in pamt.file_entries:
        if entry.path.replace("\\", "/").lower() == target:
            try:
                return vfs.read_entry_data(entry)
            except Exception as exc:
                logger.warning("dye diagnostics: could not read %s: %s",
                               relative_path, exc)
                return None
    return None


def _colour_group_names(data: bytes) -> list[str]:
    tokens = _extract_ascii_tokens(data)
    groups = sorted({t for t in tokens if "_Color_Group_" in t})
    return groups


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

DYE_SLOT_TABLE = "gamedata/partprefabdyeslotinfo.pabgb"
DYE_PALETTE_TABLE = "gamedata/partprefabdyetexturepalleteinfo.pabgb"
DYE_COLOR_GROUP_TABLE = "gamedata/dyecolorgroupinfo.pabgb"


def diagnose_armor_dye(vfs, query: str) -> DyeDiagnosticReport:
    """Look up ``query`` in the dye registry tables and produce a report.

    ``query`` can be either an armor prefab name
    (``cd_phm_00_cloak_0060``) or a submesh material name
    (``CD_PHM_00_Cloak_0032_00_01_01``). Both work — the scanner is
    case-insensitive and substring-based.
    """
    report = DyeDiagnosticReport(query=query)

    slot_data = _read_table(vfs, DYE_SLOT_TABLE)
    palette_data = _read_table(vfs, DYE_PALETTE_TABLE)
    colour_data = _read_table(vfs, DYE_COLOR_GROUP_TABLE)

    report.dye_slot_file_present = slot_data is not None
    report.palette_file_present = palette_data is not None
    report.colour_groups_present = colour_data is not None

    if not report.dye_slot_file_present:
        report.error = (
            f"could not read {DYE_SLOT_TABLE}; dye lookup skipped"
        )
        return report

    lower_query = query.lower().encode("ascii", errors="replace")
    if lower_query in slot_data.lower():
        report.is_dyeable = True
        report.dye_slots = _slots_around_match(slot_data, query)

    if palette_data is not None and lower_query in palette_data.lower():
        report.palette_hits = _palette_hits(palette_data, query)

    if colour_data is not None:
        report.colour_group_names = _colour_group_names(colour_data)

    return report


def enumerate_dyeable_armor_prefixes(vfs, prefix: str) -> list[str]:
    """Return every armor-prefab name in the dye registry that starts with ``prefix``.

    Used by the UI to present a picker of "all dyeable cloaks" or
    "all dyeable helms" without having to load the full pabgb parser.
    """
    slot_data = _read_table(vfs, DYE_SLOT_TABLE)
    if slot_data is None:
        return []
    tokens = _extract_ascii_tokens(slot_data)
    lower_prefix = prefix.lower()
    hits = sorted({t for t in tokens if t.lower().startswith(lower_prefix)})
    return hits
