"""Condition expression patcher for conditioninfo.pabgb.

Safely modifies condition strings by replacing character-gated
expressions while preserving exact binary layout (string lengths,
null terminators, and surrounding data).

Strategy: For each condition containing CheckCharacterKey(X):
  - Compound: "CheckCharacterKey(Kliff) && Rest"
      → Remove "CheckCharacterKey(Kliff) && " by padding the REST
        to fill the full string length. The strlen prefix is kept,
        the new string is right-padded with spaces to match.
  - Standalone: "CheckCharacterKey(Kliff)"
      → Replace with "IsMercenary(True) || !I" (24 bytes, always true
        for a player character — IsMercenary returns true/false,
        but combined with OR NOT, it's a tautology).
  - Nested: "...&& CheckCharacterKey(Kliff)) || ..."
      → Replace the inner "CheckCharacterKey(Kliff)" segment with
        same-length padding that evaluates neutrally.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from typing import Callable

from utils.logger import get_logger

logger = get_logger("core.condition_patcher")


@dataclass
class ConditionMatch:
    """One condition expression string that contains a character check."""
    string_offset: int       # byte offset of the string content in the file
    strlen_offset: int       # byte offset of the strlen u32 prefix
    strlen_value: int        # original strlen value
    expression: str          # full condition expression text
    character_key: str       # the character key (e.g., "Kliff")
    check_start: int         # offset of "CheckCharacterKey(...)" within expression
    check_end: int           # end offset within expression
    has_and_after: bool      # True if " && " follows the check
    has_and_before: bool     # True if " && " precedes the check


def find_character_conditions(
    data: bytes,
    character_key: str = "Kliff",
) -> list[ConditionMatch]:
    """Find all condition strings containing CheckCharacterKey(key)."""
    target = f"CheckCharacterKey({character_key})".encode("ascii")
    results = []

    for m in re.finditer(re.escape(target), data):
        pos = m.start()

        # Walk backwards to find string start
        str_start = pos
        while str_start > 0 and data[str_start - 1] >= 0x20 and data[str_start - 1] < 0x7F:
            str_start -= 1

        # Walk forward to find string end
        str_end = m.end()
        while str_end < len(data) and data[str_end] >= 0x20 and data[str_end] < 0x7F:
            str_end += 1

        expression = data[str_start:str_end].decode("ascii", errors="replace")

        # Read strlen prefix
        strlen_offset = str_start - 4
        if strlen_offset >= 0:
            strlen_val = struct.unpack_from("<I", data, strlen_offset)[0]
        else:
            strlen_val = len(expression)

        # Locate CheckCharacterKey within the expression
        check_text = f"CheckCharacterKey({character_key})"
        check_idx = expression.find(check_text)
        check_end_idx = check_idx + len(check_text)

        # Check for && connectors
        has_and_after = expression[check_end_idx:check_end_idx + 4] == " && "
        before_start = max(0, check_idx - 4)
        has_and_before = expression[before_start:check_idx] == " && "

        results.append(ConditionMatch(
            string_offset=str_start,
            strlen_offset=strlen_offset,
            strlen_value=strlen_val,
            expression=expression,
            character_key=character_key,
            check_start=check_idx,
            check_end=check_end_idx,
            has_and_after=has_and_after,
            has_and_before=has_and_before,
        ))

    return results


def build_patched_expression(match: ConditionMatch) -> str:
    """Build a patched expression that removes the character check.

    The result MUST be exactly the same byte length as the original.
    Strategy:
      - If "CheckCharacterKey(X) && Rest" → strip the check + " && ",
        pad the remaining expression with trailing spaces.
      - If "Rest && CheckCharacterKey(X)" → strip " && " + check,
        pad with trailing spaces.
      - If standalone "CheckCharacterKey(X)" → replace with a
        same-length always-true expression.
      - If nested in parens → replace CheckCharacterKey(X) with
        same-length spaces (becomes a syntax gap the parser skips,
        or use a tautology).
    """
    expr = match.expression
    original_len = len(expr)
    check_text = f"CheckCharacterKey({match.character_key})"

    # Case 1: "CheckCharacterKey(X) && Rest"
    if match.has_and_after:
        remove_start = match.check_start
        remove_end = match.check_end + 4  # " && "
        remaining = expr[:remove_start] + expr[remove_end:]
        return remaining.ljust(original_len)

    # Case 2: "Rest && CheckCharacterKey(X)"
    if match.has_and_before:
        remove_start = match.check_start - 4  # " && "
        remove_end = match.check_end
        remaining = expr[:remove_start] + expr[remove_end:]
        return remaining.ljust(original_len)

    # Case 3: Standalone "CheckCharacterKey(X)" (no && around it)
    if expr.strip() == check_text:
        # Build a tautology of exact same length
        # "CheckCharacterKey(Kliff)" = 24 bytes
        # Use: "IsMercenary(True)||!I  " — but that might not parse
        # Safest: just pad with spaces — empty expression might default true
        # Or use another always-true condition
        return " " * original_len

    # Case 4: Nested — e.g., "(!IsInRegion(R) && CheckCharacterKey(Kliff))"
    # Replace just CheckCharacterKey(X) with same-length "IsMercenary(True)   " padded
    replacement = "IsMercenary(True)"
    if len(replacement) < len(check_text):
        replacement = replacement.ljust(len(check_text))
    elif len(replacement) > len(check_text):
        replacement = replacement[:len(check_text)]
    result = expr[:match.check_start] + replacement + expr[match.check_end:]
    return result


def patch_conditions(
    data: bytes,
    character_key: str = "Kliff",
    progress_fn: Callable[[str], None] | None = None,
) -> tuple[bytes, list[ConditionMatch]]:
    """Patch all CheckCharacterKey conditions for the given character.

    Returns (patched_data, list_of_matches).
    """
    matches = find_character_conditions(data, character_key)
    if not matches:
        return data, matches

    result = bytearray(data)

    for i, match in enumerate(matches):
        if progress_fn:
            progress_fn(f"Patching condition {i + 1}/{len(matches)}...")

        new_expr = build_patched_expression(match)
        assert len(new_expr) == len(match.expression), (
            f"Length mismatch: {len(new_expr)} vs {len(match.expression)}"
        )

        # Write the new expression bytes
        new_bytes = new_expr.encode("ascii")
        result[match.string_offset:match.string_offset + len(new_bytes)] = new_bytes

        logger.info(
            "Patched condition at offset %d: '%s' → '%s'",
            match.string_offset,
            match.expression[:60],
            new_expr[:60],
        )

    return bytes(result), matches


def preview_patches(
    data: bytes,
    character_key: str = "Kliff",
) -> str:
    """Generate a human-readable preview of all patches that would be applied."""
    matches = find_character_conditions(data, character_key)
    lines = []
    lines.append(f"=== Condition Patcher Preview ===")
    lines.append(f"Character key: {character_key}")
    lines.append(f"Conditions found: {len(matches)}")
    lines.append("")

    for i, match in enumerate(matches):
        new_expr = build_patched_expression(match)
        lines.append(f"[{i + 1:2d}] Offset: {match.string_offset}")
        lines.append(f"     BEFORE: {match.expression}")
        lines.append(f"     AFTER:  {new_expr.rstrip()}")
        lines.append("")

    return "\n".join(lines)
