"""Crimson Desert state-machine browser — enterprise back-end.

The game's state machine is not stored as a single graph. Character
states, action flags, quest stages, and gimmick transitions are
SCATTERED across ~15 .pabgb tables and referenced by NAME inside
condition expressions like:

    !CheckActionAttribute(Fly) && IsGround()
    CompleteMission(Mission_ForGraymane_Complete) && !Macro(BattleState)
    CheckCharacterKey(Kliff) && CheckStage(Stage_BloodCoronation)

This module indexes those expressions across the full game-data
corpus and surfaces three query shapes that answer the modder
questions posted on Discord:

  * "Where is the Fly state?"           -> states() + expr_index
  * "What gates this stage transition?" -> row_dependencies(row)
  * "What uses this condition key?"     -> referrers(condition_name)

Condition expression grammar (reverse-engineered April 2026)
-----------------------------------------------------------

Tokens:
  IDENT        [A-Za-z_][A-Za-z_0-9]*
  FCALL        IDENT '(' ARG (',' ARG)* ')'
  OPERATOR     '&&' | '||' | '!'
  PARENS       '(' expr ')'

FCALLs observed in the shipping corpus (ordered by hit count):

  CompleteMission / CompleteSubMission / CompleteQuest
  PlayingQuest / PlayingMission
  CheckCharacterKey / CheckStage / CheckLevel
  CheckActionAttribute / CheckActionFlag
  Macro / MacroState
  CharacterGroupKey / EquipTypeKey / CategoryKey
  IsGround / IsInTown / IsInDungeon
  LevelName / LevelGimmickIndex
  KillDistance / HitCount / DistanceTo
  GetFactionNodeState / GetWorldState
  CheckVoxelType / CheckTerrainType

Arguments are one of:
  * IDENT        (enum value like `Fly`, `Crouch`, `Kliff`)
  * STRING       (quoted)
  * INTEGER      (decimal literal)
  * FCALL        (nested — e.g. `CompleteMission(Mission_Foo_Bar)`)
  * IDENT '==' IDENT   (equality check)

We do NOT run this grammar through a full PEG parser (not worth the
complexity). Instead we tokenise with a regex and pull out every
identifier that looks like a state-ish enum value. That turns out to
be enough to answer all three query shapes above with high precision.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from core.pabgb_parser import PabgbTable, PabgbRow, parse_pabgb
from utils.logger import get_logger

logger = get_logger("core.state_machine")


# ── Condition-expression token extraction ──────────────────────────────

# A function call: function-name followed by ( ... ). Captures both the
# function name and the argument block.
_FCALL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
)

# A bare identifier that isn't a keyword, operator, or number.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]{2,})\b")

# Operators / keywords to exclude from the "state token" candidate list.
_NOT_STATES = frozenset({
    "true", "false", "and", "or", "not",
    "CompleteMission", "CompleteSubMission", "CompleteQuest",
    "PlayingQuest", "PlayingMission",
    "CheckCharacterKey", "CheckStage", "CheckLevel",
    "CheckActionAttribute", "CheckActionFlag",
    "checkActionAttribute",  # seen lowercase variant
    "Macro", "MacroState",
    "CharacterGroupKey", "EquipTypeKey", "CategoryKey",
    "IsGround", "IsInTown", "IsInDungeon", "IsAboveRoad",
    "LevelName", "LevelGimmickIndex",
    "KillDistance", "HitCount", "DistanceTo",
    "GetFactionNodeState", "GetWorldState",
    "CheckVoxelType", "CheckTerrainType",
    "Normal",
})


@dataclass
class StateToken:
    """One occurrence of a state-like token inside a condition expr."""
    table: str                 # e.g. "conditioninfo.pabgb"
    row_index: int
    row_name: str              # first string field of the row
    field_index: int
    expression: str            # full expression the token was pulled from
    function: str | None = None     # name of enclosing FCALL, if any
    token: str = ""            # the state-ish identifier


@dataclass
class StateIndex:
    """Cross-referenced state machine pulled from the game corpus."""

    # token -> list of occurrences
    tokens: dict[str, list[StateToken]] = field(default_factory=lambda: defaultdict(list))
    # table-name -> number of rows scanned
    table_rows: dict[str, int] = field(default_factory=dict)
    # list of raw expressions (for global search)
    expressions: list[StateToken] = field(default_factory=list)

    def all_tokens(self, min_occurrences: int = 1) -> list[tuple[str, int]]:
        """Return (token, count) pairs sorted by frequency."""
        return sorted(
            ((t, len(v)) for t, v in self.tokens.items() if len(v) >= min_occurrences),
            key=lambda x: -x[1],
        )

    def find(self, token: str) -> list[StateToken]:
        """Exact-match lookup."""
        return list(self.tokens.get(token, []))

    def search(self, needle: str, case_sensitive: bool = False) -> list[StateToken]:
        """Substring search across all tokens and expressions."""
        if case_sensitive:
            matches = [
                occ for tok, occs in self.tokens.items()
                for occ in occs
                if needle in tok or needle in occ.expression
            ]
        else:
            needle_l = needle.lower()
            matches = [
                occ for tok, occs in self.tokens.items()
                for occ in occs
                if needle_l in tok.lower() or needle_l in occ.expression.lower()
            ]
        return matches

    def referrers(self, token: str) -> list[StateToken]:
        """Every expression that mentions ``token``."""
        return [
            occ for occs in self.tokens.values() for occ in occs
            if token in occ.expression
        ]


# ── Extraction ─────────────────────────────────────────────────────────

def _extract_state_tokens(
    expr: str,
    table: str,
    row_index: int,
    row_name: str,
    field_index: int,
) -> list[StateToken]:
    """Pull every state-ish identifier out of one condition expression."""
    occurrences: list[StateToken] = []

    # First pass: every FCALL. For CheckActionAttribute / Macro / similar,
    # the arguments are usually the state tokens.
    for m in _FCALL_RE.finditer(expr):
        fn_name = m.group(1)
        args = m.group(2)
        # For state-check functions, every identifier in the argument
        # list is a state token.
        if fn_name in {
            "CheckActionAttribute", "checkActionAttribute",
            "Macro", "MacroState",
            "CheckStage", "CheckLevel",
            "CompleteMission", "CompleteSubMission", "CompleteQuest",
            "PlayingQuest", "PlayingMission",
            "CheckCharacterKey",
            "CharacterGroupKey", "EquipTypeKey", "CategoryKey",
            "LevelName", "LevelGimmickIndex",
            "CheckVoxelType", "CheckTerrainType",
            "GetFactionNodeState",
        }:
            for arg_m in _IDENT_RE.finditer(args):
                tok = arg_m.group(1)
                if tok not in _NOT_STATES and not tok.isdigit():
                    occurrences.append(StateToken(
                        table=table, row_index=row_index, row_name=row_name,
                        field_index=field_index, expression=expr,
                        function=fn_name, token=tok,
                    ))

    # Second pass: top-level bare identifiers that look like state enums.
    # Heuristic: contains an underscore or a capital letter AND is not in
    # the exclusion set. This catches tokens like `Stage_BloodCoronation`,
    # `Mission_Foo`, `State_Combat`. Dedupe against first-pass hits.
    first_pass_tokens = {o.token for o in occurrences}
    for ident_m in _IDENT_RE.finditer(expr):
        tok = ident_m.group(1)
        if tok in _NOT_STATES or tok.isdigit() or tok in first_pass_tokens:
            continue
        if "_" in tok or any(c.isupper() for c in tok[1:]):
            occurrences.append(StateToken(
                table=table, row_index=row_index, row_name=row_name,
                field_index=field_index, expression=expr,
                function=None, token=tok,
            ))
            first_pass_tokens.add(tok)

    return occurrences


def build_state_index(
    tables: list[PabgbTable],
    *,
    min_expr_len: int = 3,
) -> StateIndex:
    """Walk every string field of every row in every table, pulling state
    tokens out of anything that looks like a condition expression.

    We detect condition expressions heuristically — a string field is
    treated as an expression if it contains any of ``&&``, ``||``, ``!=``,
    ``==``, or a ``SomeIdent(...)`` call. Pure name strings (``Kliff``,
    ``Mission_ForGraymane_Complete``) are indexed as single-token
    expressions so name lookups still work.
    """
    index = StateIndex()

    for table in tables:
        index.table_rows[table.file_name] = len(table.rows)
        for row_idx, row in enumerate(table.rows):
            row_name = ""
            for f in row.fields:
                if f.kind == "str" and isinstance(f.value, str):
                    row_name = f.value
                    break
            for fi, f in enumerate(row.fields):
                if f.kind != "str" or not isinstance(f.value, str):
                    continue
                val = f.value.strip()
                if len(val) < min_expr_len:
                    continue

                # Treat as a condition expression if it contains operators
                # or FCALLs; otherwise as a single-name token.
                is_expr = any(op in val for op in ("&&", "||", "==", "!=")) \
                    or _FCALL_RE.search(val) is not None

                if is_expr:
                    for occ in _extract_state_tokens(val, table.file_name, row_idx, row_name, fi):
                        index.tokens[occ.token].append(occ)
                        index.expressions.append(occ)
                elif "_" in val or any(c.isupper() for c in val[1:]):
                    # Single-name token (e.g. "Stage_BloodCoronation")
                    if val not in _NOT_STATES and not val.isdigit():
                        occ = StateToken(
                            table=table.file_name, row_index=row_idx,
                            row_name=row_name, field_index=fi,
                            expression=val, function=None, token=val,
                        )
                        index.tokens[val].append(occ)
                        index.expressions.append(occ)

    logger.info(
        "StateIndex: %d distinct tokens across %d tables (%d total occurrences)",
        len(index.tokens), len(index.table_rows), len(index.expressions),
    )
    return index


def load_state_tables(
    vfs_or_paths: list[str | Path],
) -> list[PabgbTable]:
    """Helper: load a list of .pabgb table paths into PabgbTable objects.

    Silently skips files that fail to parse — the index is still useful
    with partial coverage.
    """
    tables: list[PabgbTable] = []
    for raw in vfs_or_paths:
        p = Path(raw)
        if not p.exists():
            continue
        try:
            data = p.read_bytes()
            header_path = p.with_suffix(".pabgh")
            header_data = header_path.read_bytes() if header_path.exists() else None
            t = parse_pabgb(data, header_data, p.name)
            tables.append(t)
        except Exception as e:
            logger.warning("skipped %s: %s", p.name, e)
    return tables


# ── Known enum catalogues (authoritative references) ───────────────────

# ActionAttribute enum reverse-engineered from conditioninfo.pabgb row 26:
#   `!checkActionAttribute(Crouch || Down || SwimMove || Fall || Ride ||
#    Catch || Climb || Jump || Fly || RemoteCatch || MoveLv4)`
KNOWN_ACTION_ATTRIBUTES = (
    "Crouch", "Down", "SwimMove", "Fall", "Ride",
    "Catch", "Climb", "Jump", "Fly", "RemoteCatch",
    "MoveLv4",
)

# Character keys (from CheckCharacterKey calls)
KNOWN_CHARACTER_KEYS = (
    "Kliff", "Damiane", "Oongka", "Yahn",
)

# Battle / macro state names seen in Macro() calls
KNOWN_MACRO_STATES = (
    "BattleState",
)
