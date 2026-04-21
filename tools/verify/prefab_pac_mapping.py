"""Verify the PAC <-> prefab relationship against real game files.

Current 'Open Matching Prefab' heuristic in face_parts_dialog.py
assumes ``cd_foo_0001.pac`` lives next to ``cd_foo_0001.prefab``.
This script confirms whether that holds across the real corpus and,
if not, what the actual relationship is.
"""

import sys
import os
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.prefab_parser import parse_prefab


def main():
    temp = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    prefabs = sorted(set(p.name for p in temp.glob("crimsonforge_preview_*/*.prefab")))
    pacs = sorted(set(p.name for p in temp.glob("crimsonforge_preview_*/*.pac")))

    # Build a set of PAC basenames (without .pac) for quick containment check
    pac_bases = {p[:-4].lower() for p in pacs}

    print(f"{len(prefabs)} prefabs, {len(pacs)} PACs in temp cache\n")

    print("=== Prefab basename vs PAC basename pairs ===")
    exact_match = 0
    prefix_match = 0
    no_match = 0
    prefab_references: dict[str, list[str]] = {}

    for prefab_name in prefabs:
        prefab_base = prefab_name[:-7].lower()  # strip .prefab

        # Try 1: exact match with a PAC basename
        exact = prefab_base in pac_bases
        # Try 2: prefab_base is SUFFIX-extended of a PAC basename
        #   e.g. prefab = cd_phm_00_cloak_00_0208_t -> PAC cd_phm_00_cloak_00_0208
        candidates = [p for p in pac_bases if prefab_base.startswith(p + "_") or prefab_base == p]

        # Also check what PAC paths this prefab actually references internally
        path = next(temp.glob(f"crimsonforge_preview_*/{prefab_name}"), None)
        refs_inside = []
        if path:
            try:
                data = path.read_bytes()
                pf = parse_prefab(data, prefab_name)
                refs_inside = [s.value for s in pf.file_references()
                               if s.value.lower().endswith(".pac")]
                prefab_references[prefab_name] = refs_inside
            except Exception as e:
                refs_inside = [f"<parse failed: {e}>"]

        status = "EXACT" if exact else ("PREFIX" if candidates else "NONE")
        if exact:
            exact_match += 1
        elif candidates:
            prefix_match += 1
        else:
            no_match += 1
        print(f"  {status:6s} {prefab_name}")
        if candidates and not exact:
            print(f"         prefix-matches: {candidates[:3]}")
        if refs_inside:
            print(f"         INTERNAL REFS: {len(refs_inside)} -> {refs_inside[0] if refs_inside else '-'}")

    print(f"\n=== Summary ===")
    print(f"  exact basename match:  {exact_match}")
    print(f"  prefix + suffix match: {prefix_match}")
    print(f"  no match at all:       {no_match}")

    # What does the internal reference pattern look like?
    print("\n=== What do prefabs actually reference internally? ===")
    referenced_pacs = Counter()
    for refs in prefab_references.values():
        for r in refs:
            referenced_pacs[os.path.basename(r).lower()] += 1
    print(f"Unique PAC basenames referenced by prefabs: {len(referenced_pacs)}")
    for pac, n in referenced_pacs.most_common(5):
        # Is this PAC itself in our catalog?
        in_cache = pac in {p.lower() for p in pacs}
        print(f"  {pac:60s} referenced {n}x  {'(in cache)' if in_cache else ''}")


if __name__ == "__main__":
    main()
