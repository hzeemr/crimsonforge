"""Character face-part catalog — enumerated-variant face customization.

Pearl Abyss face customization in Crimson Desert is NOT blend-shape
based. Our byte-level investigation of 6 head / eye / beard PACs
(April 2026) found no classic per-vertex displacement data — every
``shape`` hit inside the PAC is Havok physics collision geometry
(``hknpShape``, ``hknpConvexHull``, ``numShapeKeyBits``) rather than
mesh morph data.

Instead, character appearance is **submesh swapping**:

  * Each face region has enumerated PAC files with a variant ID:
        cd_ptm_00_head_0001.pac
        cd_ptm_00_head_0003.pac
        cd_ptm_00_head_sub_00_0001.pac
        cd_ppdm_00_eyeleft_00_0001.pac
        cd_ptm_00_beard_00_0013_01.pac
  * Head-sub PACs BUNDLE granular face sub-parts as named submeshes
    inside their section 0, e.g.:
        CD_PTM_00_Head_Sub_00_0001_EyeLeft_0001
        CD_PTM_00_Head_Sub_00_0001_EyeRight_0001
        CD_PTM_00_Head_Sub_00_0001_Tooth_0001
        CD_PTM_00_Head_Sub_00_0002_Eyebrow_0004
  * Which variant + which submesh ships with a character is driven
    by ``characterappearanceindexinfo.pabgb`` (160 rows, one per
    character-appearance slot). Rewrite the prefab's ``.pac``
    reference or the appearance index entry to swap.

This module is the back-end catalog: it walks the file listing,
classifies each name by face region, and exposes a ``FacePartCatalog``
dataclass with convenient lookups by category + variant id. The UI
layer uses it to populate "Face Part Browser" so modders can see
what variants exist for each slot.

No heuristic fallback — names that don't match a region pattern are
dropped into the ``Other`` bucket and the editor skips them.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from utils.logger import get_logger

logger = get_logger("core.face_parts")


# Regions we recognise + matching substrings (longest-first to avoid
# accidental misclassification of `eyelash` as `eye`).
_REGION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EyeLeft",   ("eyeleft",)),
    ("EyeRight",  ("eyeright",)),
    ("Eyelash",   ("eyelash",)),
    ("Eyebrow",   ("eyebrow",)),
    ("Eye",       ("_eye_", "eyeball", "_eye")),
    ("Tooth",     ("tooth", "teeth")),
    ("Tongue",    ("tongue",)),
    ("Nose",      ("nose",)),
    ("Lip",       ("lip",)),
    ("Mouth",     ("mouth",)),
    ("Beard",     ("beard",)),
    ("Mustache",  ("mustache", "moustache")),
    ("Hair",      ("hair",)),
    ("Ear",       ("_ear_",)),
    ("Face",      ("_face_", "facial")),
)

# Regex for extracting the last 3- or 4-digit variant id out of a filename.
_VARIANT_RE = re.compile(r"_(\d{3,4})(?:_|$)")

# Regex for whole-head PACs (the main mesh, not sub-parts).
# Prefix groups we treat as character models:
#   ptm   Player / NPC Torso/Head Male template
#   phm   Player / NPC Hair/body Male rig
#   phw   Player / NPC Hair/body Female rig
#   pfm   Player / NPC Face Male (seen in some assets)
#   pfw   Player / NPC Face Female
#   ppdm  Player Pair Detail Male (eye variants)
#   ppdw  Player Pair Detail Female
#   pgm   Player Gear Male (gear-adjacent variants)
#   pgw   Player Gear Female
_CHAR_PREFIX = r"p(?:t|h|f|pd|g)[mw]"
_HEAD_PAC_RE = re.compile(rf"^cd_{_CHAR_PREFIX}_\d+_head_\d+(?:_[a-z0-9]+)?$")
_HEAD_SUB_RE = re.compile(rf"^cd_{_CHAR_PREFIX}_\d+_head_sub_")


@dataclass
class FacePart:
    """One face-part PAC entry in the catalog."""
    filename: str              # e.g. "cd_ptm_00_head_0001.pac"
    category: str              # Head / EyeLeft / Eyebrow / Tooth / Nose / …
    subtype: str = ""          # the token that matched (empty for Head)
    variant_id: int | None = None
    archive_path: str = ""     # full VFS path when available


@dataclass
class FacePartCatalog:
    """Full catalog of all face-part PAC files found.

    Grouped by category so the UI can show:  Head (5 variants), Eye
    (12 variants), Nose (8 variants), etc.
    """
    parts: list[FacePart] = field(default_factory=list)

    # Indexes populated by :meth:`_rebuild_indexes`
    _by_category: dict[str, list[FacePart]] = field(default_factory=dict)
    _by_filename: dict[str, FacePart] = field(default_factory=dict)

    def add(self, part: FacePart) -> None:
        self.parts.append(part)
        self._by_category.setdefault(part.category, []).append(part)
        self._by_filename[part.filename.lower()] = part

    # ---- public lookups -----------------------------------------------

    def categories(self) -> list[str]:
        return sorted(self._by_category.keys())

    def parts_in(self, category: str) -> list[FacePart]:
        return list(self._by_category.get(category, []))

    def variants_in(self, category: str) -> list[int]:
        return sorted(
            {p.variant_id for p in self.parts_in(category)
             if p.variant_id is not None}
        )

    def find(self, filename: str) -> FacePart | None:
        return self._by_filename.get(filename.lower())

    def count(self) -> int:
        return len(self.parts)

    def category_counts(self) -> dict[str, int]:
        return {c: len(v) for c, v in self._by_category.items()}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_face_part(filename: str) -> tuple[str, str] | None:
    """Return ``(category, subtype)`` for a face-part filename.

    Returns ``None`` if the filename doesn't look like a character
    face / body appearance PAC.
    """
    stem = filename.lower()
    if stem.endswith(".pac"):
        stem = stem[:-4]

    if _HEAD_PAC_RE.match(stem):
        return ("Head", "whole")
    if _HEAD_SUB_RE.match(stem):
        return ("HeadSub", "head_sub")

    for category, tokens in _REGION_PATTERNS:
        for tok in tokens:
            if tok in stem:
                return (category, tok.strip("_"))
    return None


def extract_variant_id(filename: str) -> int | None:
    """Pull the variant id (the last 3- or 4-digit group) out of a
    PAC filename. Returns ``None`` if no numeric group is present.
    """
    stem = filename.lower()
    if stem.endswith(".pac"):
        stem = stem[:-4]
    matches = _VARIANT_RE.findall(stem)
    return int(matches[-1]) if matches else None


# ---------------------------------------------------------------------------
# Building a catalog
# ---------------------------------------------------------------------------

def build_catalog(
    filenames: Iterable[str],
    *,
    archive_paths: dict[str, str] | None = None,
) -> FacePartCatalog:
    """Walk a set of PAC filenames and produce the catalog.

    ``filenames`` may be full paths or bare basenames — only the
    basename drives classification.
    ``archive_paths`` optionally maps basename (lowercase) -> full
    VFS path, for when the caller wants "patch to game" later.
    """
    catalog = FacePartCatalog()
    ap = {k.lower(): v for k, v in (archive_paths or {}).items()}
    for full in filenames:
        base = os.path.basename(full)
        classification = classify_face_part(base)
        if classification is None:
            continue
        category, subtype = classification
        part = FacePart(
            filename=base,
            category=category,
            subtype=subtype,
            variant_id=extract_variant_id(base),
            archive_path=ap.get(base.lower(), full),
        )
        catalog.add(part)
    logger.info(
        "FacePartCatalog: %d parts across %d categories (%s)",
        catalog.count(), len(catalog.categories()),
        ", ".join(f"{c}:{n}" for c, n in catalog.category_counts().items()),
    )
    return catalog


# ---------------------------------------------------------------------------
# Submesh-name inspection (granular face sub-parts)
# ---------------------------------------------------------------------------

def _classify_submesh_tail(name: str) -> tuple[str, int | None] | None:
    """Classify a head-sub submesh name by inspecting its TAIL.

    Head-sub submesh names look like:
        CD_PTM_00_Head_Sub_00_0001_EyeLeft_0001
        CD_PTM_00_Head_Sub_00_0001_Tooth_0001
        CD_PTM_00_Head_Sub_00_0002_Eyebrow_0004

    The region token + variant id sit at the end. We strip the common
    ``CD_PTM_NN_Head_Sub_NN_NNNN_`` prefix and classify what's left.
    Returns ``(category, variant_id)`` or ``None``.
    """
    low = name.lower()
    # Strip common head_sub prefix if present
    m = re.match(r"^cd_p[thw]m_\d+_head_sub_\d+_\d+_", low)
    tail = low[m.end():] if m else low
    # Extract region from the tail (before last digits)
    for category, tokens in _REGION_PATTERNS:
        for tok in tokens:
            if tok.strip("_") in tail:
                var = extract_variant_id(name + ".pac")
                return (category, var)
    return None


def scan_head_sub_submeshes(pac_bytes: bytes) -> list[tuple[str, str, int | None]]:
    """Pull granular face sub-part names out of a head_sub PAC.

    Returns a list of ``(submesh_name, sub_category, variant_id)``
    triples where ``submesh_name`` is the full in-file string (e.g.
    ``CD_PTM_00_Head_Sub_00_0001_EyeLeft_0001``).

    Head-sub PACs bundle Eye / Eyebrow / Tooth / Tongue variants as
    named submeshes inside their section 0. Each submesh name follows
    the pattern ``<PAC stem>_<region><variant>`` — e.g. the file
    ``cd_ptm_00_head_sub_00_0001.pac`` declares ``EyeLeft_0001``,
    ``EyeRight_0001``, ``Tooth_0001`` etc. as its submeshes.

    We find them by scanning for ASCII runs ≥ 20 chars that start
    with ``CD_`` and classify the TAIL of the string against known
    face-region tokens (so that ``..._EyeLeft_0001`` is recognised
    even though the full path also contains ``Head_Sub``).
    """
    # Scan every ``CD_`` occurrence independently so that strings
    # packed adjacently without a null separator (which happens in
    # real PACs where a 4-byte length prefix sits between names)
    # are picked up individually.
    hits: list[tuple[str, str, int | None]] = []
    seen: set[str] = set()

    i = 0
    while i < len(pac_bytes):
        idx = pac_bytes.find(b"CD_", i)
        if idx < 0:
            break
        # Extend while printable ASCII
        j = idx
        while j < len(pac_bytes) and 32 <= pac_bytes[j] < 127:
            j += 1
        run = pac_bytes[idx:j]
        if len(run) >= 20:
            try:
                s = run.decode("ascii")
            except UnicodeDecodeError:
                s = None
            if s:
                # The run may contain multiple concatenated CD_... strings.
                # Split on 'CD_' and classify each piece that looks like
                # a face-part name.
                pieces = [p for p in re.split(r"(?=CD_)", s) if p.startswith("CD_")]
                for piece in pieces:
                    # Strip trailing non-face ascii (e.g. padding chars
                    # that are printable but not part of the name)
                    clean = piece.rstrip()
                    if len(clean) < 20 or clean in seen:
                        continue
                    cls = _classify_submesh_tail(clean)
                    if cls is not None:
                        seen.add(clean)
                        hits.append((clean, cls[0], cls[1]))
        i = max(j, idx + 1)
    return hits
