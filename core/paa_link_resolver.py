"""Resolve link-variant PAA files through the PAMT virtual filesystem.

Reverse-engineering context (Apr 2026)
--------------------------------------

Link-variant PAAs are PAA files whose flag low-byte is
``0x4A``, ``0xCA``, ``0x4F``, or ``0xCF``. They embed a
``%character/...`` path reference to ANOTHER PAA / PAB asset
rather than carrying their own animation data. Roughly 19% of the
shipping corpus (surveyed in v1.18.0) uses this form.

Historical handling
-------------------

v1.18.0's :mod:`core.animation_parser` detected the variant but
merely logged the target path — the caller got back an empty
``ParsedAnimation`` and the FBX exporter produced a bare skeleton.
v1.20.3 improved detection (bounded ``0x14..0x100`` scan with
path-prefix validation) but still didn't RESOLVE the reference.

What this module does
---------------------

Given a resolved ``%char/...`` link target:

  1. Translate the ``%`` prefix to a package archive path (the
     ``%`` is shorthand for the game's content root; each top-level
     directory like ``character/`` is stored in a specific package
     group)
  2. Look the resulting path up in every loaded PAMT
  3. If found, return the bytes so the caller can re-run
     :func:`parse_paa` on the real data
  4. Guard against infinite loops when a chain of link-variants
     references itself

The package-group map (``_PATH_TO_GROUP``) is a conservative
empirical table: entries discovered so far ship the main content
in groups 0000 and 0009. Entries without a mapping are searched
across EVERY loaded PAMT as a fallback.
"""

from __future__ import annotations

import os
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.paa_link_resolver")


# Top-level directory -> likely package group(s). When the target
# isn't found via the primary group, we scan every loaded PAMT.
_PATH_TO_GROUP: dict[str, tuple[str, ...]] = {
    "character": ("0000", "0009"),
    "effect":    ("0000",),
    "map":       ("0012",),
    "pc":        ("0000", "0009"),
    "weapon":    ("0009",),
    "ui":        ("0012",),
}


def normalise_link_target(raw: str) -> str:
    """Turn a raw ``%character/...`` token into a clean relative path.

    Strips the ``%`` prefix + any embedded trailing null / garbage.
    Returns the normalised path as a lowercase archive path.
    """
    s = raw
    if s.startswith("%"):
        s = s[1:]
    # Trim at first non-path character (null, CR/LF, etc.)
    end = len(s)
    for i, ch in enumerate(s):
        if ch in ("\x00", "\r", "\n") or ord(ch) < 0x20:
            end = i
            break
    return s[:end].lower().replace("\\", "/")


def _candidate_groups(path: str) -> list[str]:
    """Return likely package groups to search first for a given path."""
    lead = path.split("/", 1)[0] if "/" in path else path
    return list(_PATH_TO_GROUP.get(lead, ()))


def resolve_link(
    raw_link: str,
    vfs,
    *,
    max_hops: int = 5,
) -> Optional[bytes]:
    """Follow a ``%character/...`` reference through the VFS.

    Returns the bytes of the target asset, or ``None`` if the
    target isn't found after trying both the candidate package
    groups and (as fallback) every loaded PAMT.

    ``max_hops`` limits chained link -> link -> link references
    so a malformed/malicious asset can't hang the caller.
    """
    path = normalise_link_target(raw_link)
    if not path:
        return None

    seen: set[str] = set()
    current = path
    for hop in range(max_hops):
        if current in seen:
            logger.warning("link-variant loop detected at %r", current)
            return None
        seen.add(current)

        bytes_found = _find_in_vfs(current, vfs)
        if bytes_found is None:
            logger.info(
                "link-variant target not found: %r (hop %d)", current, hop,
            )
            return None

        # If the target is ALSO a link-variant, keep chasing.
        from core.animation_parser import parse_paa  # delay import
        try:
            inner = parse_paa(bytes_found, os.path.basename(current))
        except Exception:
            # Not a PAA at all (could be a PAB or PAC) — return raw bytes
            return bytes_found

        if getattr(inner, "is_link", False) and getattr(inner, "link_target", ""):
            current = normalise_link_target(inner.link_target)
            continue
        return bytes_found

    logger.warning("link-variant max_hops %d exceeded starting from %r",
                   max_hops, path)
    return None


def _find_in_vfs(path: str, vfs) -> Optional[bytes]:
    """Look up ``path`` in every loaded PAMT + candidate groups."""
    needle = path.lower()

    # Try the candidate groups first for efficiency
    for group in _candidate_groups(path):
        try:
            pamt = vfs.get_pamt(group) or vfs.load_pamt(group)
        except Exception:
            continue
        if pamt is None:
            continue
        for entry in pamt.file_entries:
            if entry.path.lower() == needle:
                try:
                    return vfs.read_entry_data(entry)
                except Exception as e:
                    logger.debug("read failed for %s: %s", entry.path, e)
                    return None

    # Fallback: walk every available group
    try:
        groups = vfs.list_package_groups()
    except Exception:
        return None
    for group in groups:
        try:
            pamt = vfs.get_pamt(group) or vfs.load_pamt(group)
        except Exception:
            continue
        if pamt is None:
            continue
        for entry in pamt.file_entries:
            if entry.path.lower() == needle:
                try:
                    return vfs.read_entry_data(entry)
                except Exception as e:
                    logger.debug("read failed for %s: %s", entry.path, e)
                    return None
    return None
