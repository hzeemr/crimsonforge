"""Verify parse_prefab + apply_edits on EVERY available real prefab.

Tests three things per file:
  1. Parser accepts the bytes without raising
  2. Identity round-trip (no edits) produces bytes == original
  3. Same-length edit to the first tag_value re-parses correctly
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.prefab_parser import parse_prefab, apply_edits, PrefabEdit


def main():
    TEMP = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    prefabs = list(TEMP.glob("crimsonforge_preview_*/*.prefab"))
    if not prefabs:
        print("No .prefab files found in temp dirs")
        sys.exit(1)
    print(f"Testing {len(prefabs)} prefabs\n")

    passed = 0
    for p in prefabs:
        try:
            data = p.read_bytes()
            pf = parse_prefab(data, p.name)
        except Exception as e:
            print(f"  FAIL [parse]   {p.name}: {e}")
            continue

        # Identity round-trip
        try:
            rt = apply_edits(pf, [], allow_length_change=False)
        except Exception as e:
            print(f"  FAIL [rt]      {p.name}: {e}")
            continue
        if rt != data:
            print(f"  FAIL [rt-diff] {p.name}: {len(rt)} vs {len(data)}")
            continue

        # Same-length edit (swap 1 char of first editable string)
        editable = [s for s in pf.strings if s.category in ("tag_value", "file_ref")]
        if editable:
            target = editable[0]
            if len(target.value) >= 2:
                new_val = target.value[:-1] + ("X" if target.value[-1] != "X" else "Y")
                try:
                    edited = apply_edits(
                        pf,
                        [PrefabEdit(prefix_offset=target.prefix_offset, new_value=new_val)],
                        allow_length_change=False,
                    )
                    pf2 = parse_prefab(edited, p.name)
                    if not pf2.find_string(new_val):
                        print(f"  FAIL [edit]    {p.name}: edited value not found after re-parse")
                        continue
                except Exception as e:
                    print(f"  FAIL [edit]    {p.name}: {e}")
                    continue

        print(f"  OK [{len(data):>6d}B {len(pf.strings):>3d}str "
              f"{len(pf.file_references()):>2d}fr {len(pf.tag_values()):>2d}tv] {p.name}")
        passed += 1

    print(f"\n{passed}/{len(prefabs)} prefabs passed all checks")


if __name__ == "__main__":
    main()
