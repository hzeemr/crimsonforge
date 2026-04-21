"""Find all editable strings inside a .prefab.

A prefab is a binary tree of components, each with typed properties.
Full parsing needs the whole type schema. But for the community's
actual modding use-cases, we only need to EDIT a few strings:

  * .pac / .pab / .pah file paths (mesh/skeleton/physics references)
  * _shrinktag value (qq_Hikka body-part-hiding workflow)
  * Socket names, enum values, type-prefixed tokens

This script locates every length-prefixed string in the file. Two
heuristics:
  * [uint32 len][ASCII] where len < 512 and chars are printable
  * Looks for known anchor strings like ".pac", ".pab", "Upperbody"
"""

import sys
import struct
import re


def find_length_prefixed_strings(data, min_len=3, max_len=256):
    """Scan for [uint32 len][ASCII bytes] patterns anywhere in the file."""
    results = []
    i = 0
    while i + 4 < len(data):
        n = struct.unpack_from("<I", data, i)[0]
        if min_len <= n <= max_len and i + 4 + n <= len(data):
            chunk = data[i + 4: i + 4 + n]
            # All-printable ASCII?
            if all(32 <= b < 127 for b in chunk):
                try:
                    s = chunk.decode("ascii")
                    # Skip if looks like junk (e.g., all same char)
                    if len(set(s)) >= 2:
                        results.append((i, n, s))
                        i += 4 + n
                        continue
                except UnicodeDecodeError:
                    pass
        i += 1
    return results


def main():
    if len(sys.argv) < 2:
        paths = [
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_d8exzp3t\cd_phm_00_cloak_00_0208_t.prefab",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_cgfqfw59\cd_phm_02_sword_0034.prefab",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_e7cedrha\cd_phm_00_cloak_00_0008_03_s.prefab",
            r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_e23qmmws\cd_t0000_boardpaper_0006.prefab",
        ]
    else:
        paths = sys.argv[1:]

    for p in paths:
        data = open(p, "rb").read()
        print(f"\n{'=' * 70}")
        print(f"FILE: {p}")
        print(f"SIZE: {len(data)}")
        print(f"{'=' * 70}")
        strs = find_length_prefixed_strings(data)
        print(f"Found {len(strs)} length-prefixed strings\n")

        # Group by CATEGORY so the important stuff jumps out
        file_refs = []      # .pac, .pab, .pam, .xml etc
        type_names = []     # ends in Component, Transform, Ptr, etc
        property_names = [] # starts with _
        other = []
        for off, n, s in strs:
            if re.search(r"\.(pac|pab|pam|pamlod|xml|dds|pah|pac\w*)$", s, re.I):
                file_refs.append((off, n, s))
            elif s.startswith("_"):
                property_names.append((off, n, s))
            elif re.search(r"(Component|Transform|Ptr|Reference|Uid|Uuid|String|Vector|Color|Bool|Int|Float|Type)$", s):
                type_names.append((off, n, s))
            else:
                other.append((off, n, s))

        def dump(label, rows):
            if not rows: return
            print(f"-- {label} ({len(rows)}) --")
            for off, n, s in rows:
                print(f"  @0x{off:04x}  len={n:3d}  {s!r}")

        dump("FILE REFERENCES (mesh / skeleton / xml paths)", file_refs)
        dump("TYPE NAMES", type_names)
        dump("PROPERTY NAMES (starts with _)", property_names)
        dump("OTHER (enums, tag values, etc.)", other)


if __name__ == "__main__":
    main()
