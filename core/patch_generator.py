"""JSON / XML diff + patch generator.

Nexus-style mod distribution for Crimson Desert has largely settled on
JSON-patch archives — Phorge's JSON Mod Creator, for example, writes out
a compact diff of the modded file so users can install a few kilobytes of
instructions instead of a multi-megabyte replacement. This module provides
the core machinery so any CrimsonForge surface (UI tab, CLI, scripts) can
produce and apply those patches without a third-party dependency.

Patch format
------------

We emit RFC 6902 JSON Patch, because:

  * every mod tool on nexusmods.com understands it,
  * it round-trips through unmodified JSON libraries,
  * the ops we actually need (add / remove / replace) cover 99 % of mods.

XML is handled by converting attributes / children into a canonical dict
structure (``_xml_to_canonical``) and then diffing *that*. Emitting XPath-based
XML patches would be strictly more powerful but would also ship a second
patch grammar we'd have to maintain and document; the canonical-dict path
keeps one format on the wire and loses nothing for the kinds of edits
modders actually perform (attribute value swaps, adding / removing
elements, replacing text content).

Scope
-----

Supported ops when emitting a patch:

  * ``add``      — new key or list entry
  * ``remove``   — gone key or list entry
  * ``replace``  — existing key's value changed

Intentionally unsupported (would decode / apply fine if a third party
emitted them):

  * ``move``, ``copy``, ``test`` — rare in modding, not worth the diff
    heuristics needed to detect them correctly.

The apply side does accept the full RFC 6902 op set so third-party patches
still work.
"""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable

from utils.logger import get_logger

logger = get_logger("core.patch_generator")


# ---------------------------------------------------------------------------
# JSON Pointer helpers (RFC 6901)
# ---------------------------------------------------------------------------

def _escape_pointer_token(token: str) -> str:
    # RFC 6901 §4: ~ -> ~0, / -> ~1. Order matters.
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _build_pointer(parts: Iterable[str | int]) -> str:
    """Join path components into an RFC 6901 JSON Pointer."""
    segments = ["" ""]
    segments.clear()  # start from "" for leading "/"
    for part in parts:
        segments.append(_escape_pointer_token(str(part)))
    if not segments:
        return ""
    return "/" + "/".join(segments)


def _split_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchApplyError(f"invalid pointer {pointer!r}: must start with '/'")
    return [_unescape_pointer_token(tok) for tok in pointer[1:].split("/")]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PatchApplyError(ValueError):
    """Raised when an RFC 6902 patch cannot be applied cleanly."""


# ---------------------------------------------------------------------------
# JSON diff
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PatchOp:
    """One RFC 6902 operation. kept minimal — the dict form is the wire
    format, but ``PatchOp`` is easier to reason about in tests."""
    op: str
    path: str
    value: Any = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"op": self.op, "path": self.path}
        if self.op in ("add", "replace"):
            out["value"] = self.value
        return out


def diff_json(old: Any, new: Any) -> list[PatchOp]:
    """Produce a list of RFC 6902 operations that turns ``old`` into ``new``.

    Structural rules:

      * dict vs dict      → recurse per key, emit add / remove / replace.
      * list vs list      → position-wise diff (no LCS alignment; RFC 6902
                            doesn't express it anyway). Length changes
                            turn into trailing add / remove ops.
      * type mismatch     → replace outright.
      * equal scalars     → no op.

    The no-LCS choice is deliberate: it's deterministic, fast, and matches
    what RFC 6902 can actually express without synthesising `move` ops.
    """
    ops: list[PatchOp] = []
    _diff(old, new, [], ops)
    return ops


def _diff(old: Any, new: Any, path: list[str | int], ops: list[PatchOp]) -> None:
    # Fast path: equal, nothing to do.
    if type(old) is type(new) and old == new:
        return

    if isinstance(old, dict) and isinstance(new, dict):
        _diff_dicts(old, new, path, ops)
        return

    if isinstance(old, list) and isinstance(new, list):
        _diff_lists(old, new, path, ops)
        return

    # Type mismatch or scalar change — a single replace.
    ops.append(PatchOp("replace", _build_pointer(path), copy.deepcopy(new)))


def _diff_dicts(old: dict, new: dict, path: list[str | int], ops: list[PatchOp]) -> None:
    # Removed keys first — apply order matters for users reading the diff,
    # and stable order also simplifies golden-file tests.
    for key in sorted(old.keys() - new.keys()):
        ops.append(PatchOp("remove", _build_pointer(path + [key])))

    # Added keys next.
    for key in sorted(new.keys() - old.keys()):
        ops.append(PatchOp("add", _build_pointer(path + [key]), copy.deepcopy(new[key])))

    # Shared keys — recurse.
    for key in sorted(old.keys() & new.keys()):
        _diff(old[key], new[key], path + [key], ops)


def _diff_lists(old: list, new: list, path: list[str | int], ops: list[PatchOp]) -> None:
    overlap = min(len(old), len(new))
    for i in range(overlap):
        _diff(old[i], new[i], path + [i], ops)

    # Excess tail in old → remove from the end backwards so indices stay
    # valid as we apply them.
    for i in range(len(old) - 1, overlap - 1, -1):
        ops.append(PatchOp("remove", _build_pointer(path + [i])))

    # Missing tail in new → append.
    for i in range(overlap, len(new)):
        # "-" is RFC 6902 shorthand for "one past the end"; using it avoids
        # index-range bugs when patches are applied against an evolving list.
        ops.append(PatchOp("add", _build_pointer(path + ["-"]), copy.deepcopy(new[i])))


# ---------------------------------------------------------------------------
# JSON patch application
# ---------------------------------------------------------------------------

def apply_patch(obj: Any, patch: list[dict] | list[PatchOp]) -> Any:
    """Return a deep-copied ``obj`` with every op in ``patch`` applied.

    Accepts either raw dict form (what ``json.load`` produces) or
    :class:`PatchOp` instances. Raises :class:`PatchApplyError` on any
    pointer / op that cannot be resolved — we never silently ignore a
    broken operation because that hides mod conflicts.
    """
    result = copy.deepcopy(obj)
    for raw in patch:
        op_dict = raw.to_dict() if isinstance(raw, PatchOp) else dict(raw)
        op = op_dict.get("op")
        path = op_dict.get("path", "")

        if op == "add":
            result = _apply_add(result, path, op_dict.get("value"))
        elif op == "remove":
            result = _apply_remove(result, path)
        elif op == "replace":
            result = _apply_replace(result, path, op_dict.get("value"))
        elif op == "move":
            from_path = op_dict.get("from", "")
            value = _resolve_pointer(result, from_path)
            result = _apply_remove(result, from_path)
            result = _apply_add(result, path, copy.deepcopy(value))
        elif op == "copy":
            from_path = op_dict.get("from", "")
            value = _resolve_pointer(result, from_path)
            result = _apply_add(result, path, copy.deepcopy(value))
        elif op == "test":
            actual = _resolve_pointer(result, path)
            if actual != op_dict.get("value"):
                raise PatchApplyError(
                    f"test op failed at {path}: {actual!r} != {op_dict.get('value')!r}"
                )
        else:
            raise PatchApplyError(f"unknown op {op!r} at {path}")

    return result


def _resolve_pointer(obj: Any, pointer: str) -> Any:
    if pointer == "":
        return obj
    parts = _split_pointer(pointer)
    current = obj
    for part in parts:
        current = _descend(current, part)
    return current


def _descend(container: Any, token: str) -> Any:
    if isinstance(container, list):
        if token == "-":
            raise PatchApplyError("cannot descend into list end marker '-'")
        idx = _parse_list_index(token, len(container))
        return container[idx]
    if isinstance(container, dict):
        if token not in container:
            raise PatchApplyError(f"missing key {token!r} during pointer descent")
        return container[token]
    raise PatchApplyError(f"cannot descend into scalar with token {token!r}")


def _parse_list_index(token: str, length: int) -> int:
    if not re.fullmatch(r"\d+", token):
        raise PatchApplyError(f"list index must be an unsigned integer, got {token!r}")
    idx = int(token)
    if idx < 0 or idx >= length:
        raise PatchApplyError(f"list index {idx} out of range (length {length})")
    return idx


def _apply_add(obj: Any, pointer: str, value: Any) -> Any:
    if pointer == "":
        return copy.deepcopy(value)
    parts = _split_pointer(pointer)
    parent_parts, last = parts[:-1], parts[-1]
    parent = _resolve_pointer(obj, _build_pointer(parent_parts))
    if isinstance(parent, list):
        if last == "-":
            parent.append(copy.deepcopy(value))
        else:
            idx = _parse_list_index(last, len(parent) + 1)  # allow one-past-end
            parent.insert(idx, copy.deepcopy(value))
    elif isinstance(parent, dict):
        parent[last] = copy.deepcopy(value)
    else:
        raise PatchApplyError(f"cannot add into scalar at {pointer!r}")
    return obj


def _apply_remove(obj: Any, pointer: str) -> Any:
    if pointer == "":
        raise PatchApplyError("cannot remove the root document")
    parts = _split_pointer(pointer)
    parent_parts, last = parts[:-1], parts[-1]
    parent = _resolve_pointer(obj, _build_pointer(parent_parts))
    if isinstance(parent, list):
        idx = _parse_list_index(last, len(parent))
        del parent[idx]
    elif isinstance(parent, dict):
        if last not in parent:
            raise PatchApplyError(f"remove: missing key {last!r} at {pointer!r}")
        del parent[last]
    else:
        raise PatchApplyError(f"cannot remove from scalar at {pointer!r}")
    return obj


def _apply_replace(obj: Any, pointer: str, value: Any) -> Any:
    if pointer == "":
        return copy.deepcopy(value)
    parts = _split_pointer(pointer)
    parent_parts, last = parts[:-1], parts[-1]
    parent = _resolve_pointer(obj, _build_pointer(parent_parts))
    if isinstance(parent, list):
        idx = _parse_list_index(last, len(parent))
        parent[idx] = copy.deepcopy(value)
    elif isinstance(parent, dict):
        if last not in parent:
            raise PatchApplyError(f"replace: missing key {last!r} at {pointer!r}")
        parent[last] = copy.deepcopy(value)
    else:
        raise PatchApplyError(f"cannot replace scalar at {pointer!r}")
    return obj


# ---------------------------------------------------------------------------
# XML support
# ---------------------------------------------------------------------------

def _xml_to_canonical(element: ET.Element) -> dict:
    """Turn an XML Element tree into a canonical dict for diffing.

    Layout (stable keys, ordering-sensitive where XML itself is ordering
    sensitive — i.e. children lists)::

        {
            "tag":       str,
            "attrs":     dict[str, str],  # sorted by key on insertion
            "text":      str | None,      # .strip() applied; empty -> None
            "tail":      str | None,      # .strip() applied; empty -> None
            "children":  list[dict]       # recursively canonicalised
        }

    Attributes are stored in a plain dict; Python 3.7+ preserves insertion
    order so sorting keys before insertion keeps the representation stable
    across runs.
    """
    attrs = dict(sorted(element.attrib.items()))
    children = [_xml_to_canonical(child) for child in element]
    return {
        "tag": element.tag,
        "attrs": attrs,
        "text": (element.text or "").strip() or None,
        "tail": (element.tail or "").strip() or None,
        "children": children,
    }


def _canonical_to_xml(node: dict) -> ET.Element:
    """Inverse of ``_xml_to_canonical`` — used for round-trip tests."""
    element = ET.Element(node["tag"], attrib=dict(node.get("attrs") or {}))
    element.text = node.get("text")
    element.tail = node.get("tail")
    for child in node.get("children") or []:
        element.append(_canonical_to_xml(child))
    return element


def diff_xml(old_xml: str, new_xml: str) -> list[PatchOp]:
    """Diff two XML documents by canonicalising both sides into dicts.

    See module docstring for why we diff on the canonical form instead of
    emitting an XPath-based patch grammar.
    """
    old_tree = ET.fromstring(old_xml)
    new_tree = ET.fromstring(new_xml)
    return diff_json(_xml_to_canonical(old_tree), _xml_to_canonical(new_tree))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def ops_to_json(ops: list[PatchOp], *, indent: int | None = 2) -> str:
    """Render a patch list as pretty-printed JSON for on-disk storage."""
    return json.dumps([op.to_dict() for op in ops], indent=indent, ensure_ascii=False)


def ops_from_json(text: str) -> list[PatchOp]:
    """Parse a JSON patch document (``ops_to_json`` output) back to ``PatchOp``."""
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise PatchApplyError("patch document must be a JSON array of operations")
    result: list[PatchOp] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PatchApplyError(f"patch entry must be an object, got {type(entry).__name__}")
        result.append(PatchOp(
            op=entry.get("op", ""),
            path=entry.get("path", ""),
            value=entry.get("value"),
        ))
    return result
