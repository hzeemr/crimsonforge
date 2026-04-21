"""Reverse-index prefab -> PAC references.

Prefabs carry internal `.pac` path references (via
``ParsedPrefab.file_references()``). A given PAC mesh is rarely
referenced by a prefab with the *same* basename — e.g.

    cd_phm_00_cloak_00_0208_t.prefab      (variant 0208_t)
      references cd_phm_00_cloak_00_0054_01.pac      (variant 0054!)

    cd_t0000_boardpaper_0006.prefab        (variant 0006)
      references cd_t0000_boardpaper_0003.pac        (variant 0003)

So "open the prefab that uses this PAC" needs a REVERSE lookup, not
a basename match. This module builds that index by scanning every
.prefab entry in a PAMT and recording which PAC paths each one
references.

The index is O(N) to build (N = prefab count in the archives) and
then O(1) to query by PAC archive path. It's cached per VFS instance
so successive "Open Matching Prefab" clicks are instant.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from core.prefab_parser import parse_prefab
from utils.logger import get_logger

logger = get_logger("core.prefab_reference_index")


@dataclass
class PrefabReferenceIndex:
    """Reverse map: PAC archive path (lowercased) -> list of prefab paths.

    Use :meth:`prefabs_referencing` for case-insensitive lookup and
    :meth:`pacs_in` to get the outgoing refs for a prefab.
    """
    _by_pac: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _by_prefab: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, prefab_path: str, pac_path: str) -> None:
        """Record that ``prefab_path`` references ``pac_path``."""
        pac_key = pac_path.lower()
        prefab_key = prefab_path.lower()
        if prefab_key not in self._by_pac[pac_key]:
            self._by_pac[pac_key].append(prefab_key)
        if pac_key not in self._by_prefab[prefab_key]:
            self._by_prefab[prefab_key].append(pac_key)

    # ---- public lookups -----------------------------------------------

    def prefabs_referencing(self, pac_path: str) -> list[str]:
        """Return every prefab path that references ``pac_path``.

        Matches on the FULL path first (case-insensitive); if no full
        match is found, falls back to BASENAME match so users can
        pass either form. Returns the stored (lowercased) paths.
        """
        key = pac_path.lower()
        hits = list(self._by_pac.get(key, []))
        if hits:
            return hits
        # Fallback: basename match across every stored PAC key
        base = os.path.basename(key)
        for stored_key, prefabs in self._by_pac.items():
            if os.path.basename(stored_key) == base:
                hits.extend(prefabs)
        # Dedupe preserving order
        seen: set[str] = set()
        out: list[str] = []
        for p in hits:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def pacs_in(self, prefab_path: str) -> list[str]:
        """Return every PAC path that ``prefab_path`` references."""
        return list(self._by_prefab.get(prefab_path.lower(), []))

    def prefab_count(self) -> int:
        return len(self._by_prefab)

    def pac_count(self) -> int:
        return len(self._by_pac)


def build_reference_index(
    prefab_entries: Iterable[tuple[str, bytes]],
) -> PrefabReferenceIndex:
    """Scan a sequence of ``(prefab_archive_path, prefab_bytes)`` pairs
    and produce the reverse index.

    Failures on individual prefabs are logged and skipped — partial
    coverage is still useful.
    """
    index = PrefabReferenceIndex()
    total = 0
    parsed = 0
    for prefab_path, data in prefab_entries:
        total += 1
        try:
            pf = parse_prefab(data, prefab_path)
        except Exception as e:
            logger.debug("skipping %s: %s", prefab_path, e)
            continue
        parsed += 1
        for ref in pf.file_references():
            if ref.value.lower().endswith(".pac"):
                index.add(prefab_path, ref.value)
    logger.info(
        "PrefabReferenceIndex: %d prefabs parsed of %d scanned, "
        "%d PACs covered",
        parsed, total, index.pac_count(),
    )
    return index


def build_reference_index_from_vfs(vfs) -> PrefabReferenceIndex:
    """Walk the entire VFS, read every .prefab entry, build the index.

    This is O(N) in prefab count. On a full game install with ~10k
    prefabs it takes a few seconds; cache the result on the caller.
    """
    def gen():
        for grp in vfs.list_package_groups():
            try:
                pamt = vfs.load_pamt(grp)
            except Exception:
                continue
            for entry in pamt.file_entries:
                if not entry.path.lower().endswith(".prefab"):
                    continue
                try:
                    yield (entry.path, vfs.read_entry_data(entry))
                except Exception as e:
                    logger.debug("read failed for %s: %s", entry.path, e)
    return build_reference_index(gen())
