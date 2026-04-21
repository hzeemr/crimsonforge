"""Catalog every character face-part PAC across the whole game VFS.

Crimson Desert face customization is submesh-swapping, not blend shapes.
Each PAC file name encodes WHICH part it is:

  cd_ptm_00_head_0001.pac           -> head variant 0001
  cd_ptm_00_head_sub_00_0001.pac    -> head sub-parts (eyes/teeth)
  cd_ppdm_00_eyeleft_00_0001.pac    -> separate eye model

This script walks every extracted PAC and bins them by part type
(head, eye, eyebrow, nose, mouth, tooth, beard, hair, ...) so
modders can see what variants exist for each slot.
"""

import sys
import os
import re
from pathlib import Path
from collections import defaultdict


def classify(filename: str) -> tuple[str, str]:
    """Return (category, subtype) for a face-part PAC filename.

    Categories: Head, EyeLeft, EyeRight, Eyebrow, Eyelash, Tooth,
                Nose, Lip, Mouth, Beard, Hair, Ear, SubAssembly, Other
    """
    name = filename.lower()
    # Strip prefixes and .pac
    if name.endswith(".pac"):
        name = name[:-4]

    # Whole-head
    if re.match(r"cd_ptm_\d+_head_\d+$", name):
        return ("Head", "whole")
    if re.match(r"cd_ptm_\d+_head_sub_", name):
        return ("SubAssembly", "head_sub")

    # Specific facial parts
    for cat, tokens in (
        ("EyeLeft",  ("eyeleft",)),
        ("EyeRight", ("eyeright",)),
        ("Eye",      ("_eye_", "eyeball")),
        ("Eyebrow",  ("eyebrow",)),
        ("Eyelash",  ("eyelash",)),
        ("Tooth",    ("tooth", "teeth")),
        ("Tongue",   ("tongue",)),
        ("Nose",     ("nose",)),
        ("Lip",      ("lip",)),
        ("Mouth",    ("mouth",)),
        ("Beard",    ("beard",)),
        ("Mustache", ("mustache", "moustache")),
        ("Hair",     ("hair",)),
        ("Ear",      ("_ear_",)),
        ("Face",     ("_face_", "facial")),
    ):
        for tok in tokens:
            if tok in name:
                return (cat, tok.strip("_"))
    # Not a face part
    return ("Other", "")


_VARIANT_RE = re.compile(r"_(\d{3,4})(?:_|$)")


def extract_variant(filename: str) -> int | None:
    """Pull the last 3-4 digit number out of a PAC filename (the variant ID)."""
    stem = filename.lower()
    if stem.endswith(".pac"):
        stem = stem[:-4]
    # Last numeric group is the variant id
    matches = _VARIANT_RE.findall(stem)
    if matches:
        return int(matches[-1])
    return None


def main():
    root = Path(r"C:\Users\hzeem\AppData\Local\Temp")
    pacs = sorted(set(
        os.path.basename(str(p))
        for p in root.glob("crimsonforge_preview_*/*.pac")
    ))
    print(f"Scanning {len(pacs)} unique PAC filenames\n")

    catalog: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    for name in pacs:
        cat, subtype = classify(name)
        if cat == "Other":
            continue
        var_id = extract_variant(name)
        catalog[cat].append((name, var_id))

    total_face = sum(len(v) for v in catalog.values())
    print(f"Total face-part PACs (across the temp cache): {total_face}\n")

    for cat, entries in sorted(catalog.items(), key=lambda x: -len(x[1])):
        print(f"=== {cat} ({len(entries)} variants) ===")
        # Dedupe variant IDs
        ids = sorted(set(v for _, v in entries if v is not None))
        print(f"  variant IDs: {ids[:30]}{'...' if len(ids) > 30 else ''}")
        for name, var_id in entries[:5]:
            print(f"    {name}  (id={var_id})")
        if len(entries) > 5:
            print(f"    ... ({len(entries) - 5} more)")
        print()


if __name__ == "__main__":
    main()
