"""Pearl Abyss .prefab parser and writer (Crimson Desert).

Reverse-engineered April 2026 from byte-level analysis of real
shipped prefabs (cloak, sword, board-paper, cockpit samples).

File structure
==============

Header (14 bytes, fixed across the entire shipped corpus)
  [0x00..0x05]  magic  ``ff ff 04 00 00 00``
  [0x06..0x09]  uint32 LE  file hash #1
  [0x0A..0x0D]  uint32 LE  file hash #2
  [0x0E..0x11]  uint32 LE  version / component count (always 15 in
                           samples — possibly schema version)

Body is a linear stream of Pearl Abyss "ReflectObject" serialisation:
a sequence of typed components, each declaring its own properties
with length-prefixed names and types. Values are stored immediately
after the type metadata as type-specific trailers, and sometimes as
standalone length-prefixed strings at the end of the file (for
Resource-reference properties that point to meshes / skeletons).

We do NOT need to fully reconstruct the property tree to support the
modding workflows the community has asked for. Every user-editable
value lands in one of three categories:

  1. **File references** — length-prefixed paths ending in ``.pac``,
     ``.pab``, ``.pam``, ``.pamlod``, ``.xml``, ``.dds``. These are
     the mesh / skeleton / material references a model-swap wants to
     change.

  2. **Tag / enum string values** — short alphanumeric strings like
     ``Upperbody``, ``Cloak``, ``Nude``, ``CD_Cloak`` that appear as
     the trailing values of ``_shrinkTag``, ``_boneOffsetTag``, and
     similar enum-coded properties. This is qq_Hikka's body-part-
     hiding workflow: change ``Upperbody`` to a custom preset in
     ``partshrinkdesc.xml`` to stop the game from hiding skin under
     this outfit.

  3. **Property / type names** — the structural labels (``_shrinkTag``,
     ``SkinnedMeshComponent``, ``SceneObject``) that we expose so
     users can SEE which value belongs to which property, even though
     the names themselves are not user-editable.

Everything outside those three categories is preserved byte-for-byte
on write, so edits don't corrupt the binary layout.

The same-length constraint
==========================

Pearl Abyss's serialiser writes length-prefixed UTF-8 strings:

    [uint32 length LE][char bytes]

An edit that CHANGES a string length shifts all downstream offsets.
We support two write modes:

  * ``rewrite_same_length``: only accept edits where the new value is
    exactly the same byte length as the old. No layout changes. This
    is the safe mode qq_Hikka's guide documents
    (*"It's best to keep the preset name to the same length as the
    word"*). Default.

  * ``rewrite_any_length``: accept longer/shorter values and update
    the length prefix + shift downstream bytes. The length prefix is
    the 4 bytes immediately before the string, so changing it is
    mechanical. No internal offset table exists in the prefab (we
    checked), so downstream shifting works.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger("core.prefab_parser")

PREFAB_MAGIC = b"\xff\xff\x04\x00\x00\x00"

# Recognised file-path extensions — ordered longest-first so
# ``.pamlod`` matches before ``.pam``.
_FILE_EXTS = (".pamlod", ".pac", ".pab", ".pam", ".xml", ".dds", ".pah")


@dataclass
class PrefabString:
    """A length-prefixed UTF-8 string discovered inside the prefab.

    Offsets are absolute byte positions in the raw file.
    ``prefix_offset`` is where the uint32 length marker begins;
    ``value_offset = prefix_offset + 4`` is where the text starts.
    """
    prefix_offset: int
    value_offset: int
    length: int
    value: str
    # Classification — set by the parser based on content and context
    category: str = "other"          # "file_ref" | "tag_value" | "property_name" | "type_name" | "other"
    property_name: str | None = None # when category == "tag_value", which property this is the value for


@dataclass
class ParsedPrefab:
    """Everything the editor needs to surface + round-trip a prefab."""
    path: str = ""
    hash1: int = 0
    hash2: int = 0
    marker: int = 0
    strings: list[PrefabString] = field(default_factory=list)
    raw: bytes = b""

    # Convenience accessors -------------------------------------------------

    def file_references(self) -> list[PrefabString]:
        return [s for s in self.strings if s.category == "file_ref"]

    def tag_values(self) -> list[PrefabString]:
        return [s for s in self.strings if s.category == "tag_value"]

    def property_names(self) -> list[PrefabString]:
        return [s for s in self.strings if s.category == "property_name"]

    def type_names(self) -> list[PrefabString]:
        return [s for s in self.strings if s.category == "type_name"]

    def find_string(self, value: str) -> PrefabString | None:
        for s in self.strings:
            if s.value == value:
                return s
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _scan_lenstrings(data: bytes, min_len: int = 1, max_len: int = 1024) -> list[tuple[int, int, str]]:
    """Walk the file looking for [uint32 len][ASCII bytes] patterns.

    Returns a list of ``(prefix_offset, length, value)`` triples. The
    scan is non-overlapping: once a valid string is found, we skip
    past its end so we don't re-match its middle as another string.
    """
    out: list[tuple[int, int, str]] = []
    i = 0
    while i + 4 < len(data):
        n = struct.unpack_from("<I", data, i)[0]
        if min_len <= n <= max_len and i + 4 + n <= len(data):
            chunk = data[i + 4: i + 4 + n]
            # Require mostly printable ASCII + at least 2 distinct
            # characters so noise (e.g. `20 20 20 20`) is rejected.
            if all(32 <= b < 127 for b in chunk) and len(set(chunk)) >= 2:
                try:
                    s = chunk.decode("ascii")
                    out.append((i, n, s))
                    i += 4 + n
                    continue
                except UnicodeDecodeError:
                    pass
        i += 1
    return out


# Known PA primitive type tokens (case-sensitive) that show up in the
# typed-property stream. Anything in this set is a TYPE, not a value.
_PRIMITIVE_TYPE_TOKENS = frozenset({
    "bool", "float", "double",
    "uint8", "uint16", "uint32", "uint64",
    "int8", "int16", "int32", "int64",
    "staticstringA", "staticstringW",
    "IndexedStringA", "IndexedStringW",
    "NormalizedPathA", "NormalizedPathW",
    "String", "StringA", "StringW",
})

# Component-type suffixes that mark class names (not values).
_COMPONENT_SUFFIX_RE = re.compile(
    r"(Component|Transform|Ptr|Reference\d?|Uid|Uuid|"
    r"Vector|Color|Bool|Int|Float|Type|PathA?|Enum|"
    r"ShapeType|CollisionShape|Container|Collection|Array|List)$"
)

# Compound resource-path prefixes like ``ResourceReferencePath_*``.
_RESOURCE_PREFIX_RE = re.compile(r"^Resource(Reference)?Path_")

# Root component names seen in the shipped corpus. Used to distinguish
# "SceneObject" (a class name) from "Cloak" (an enum value).
_KNOWN_COMPONENT_NAMES = frozenset({
    "SceneObject",
    "SkinnedMeshComponent",
    "StaticMeshComponent",
    "MeshRendererComponent",
    "AnimationComponent",
})


def _classify(value: str) -> str:
    """Label a string by structural role.

    Rules (in order):
      * ends in a known file extension                 -> "file_ref"
      * starts with ``_``                              -> "property_name"
      * is a known component class / primitive type    -> "type_name"
      * matches component-suffix or resource-prefix RE -> "type_name"
      * otherwise                                      -> "other"
        (real editable values land here and get promoted by
         :func:`_annotate_tag_values` below)
    """
    low = value.lower()
    for ext in _FILE_EXTS:
        if low.endswith(ext):
            return "file_ref"
    if value.startswith("_"):
        return "property_name"
    if value in _PRIMITIVE_TYPE_TOKENS:
        return "type_name"
    if value in _KNOWN_COMPONENT_NAMES:
        return "type_name"
    if _COMPONENT_SUFFIX_RE.search(value) or _RESOURCE_PREFIX_RE.match(value):
        return "type_name"
    return "other"


def _annotate_tag_values(prefab: "ParsedPrefab") -> None:
    """Promote ``category=other`` strings to ``tag_value`` when they
    look like actual property values.

    By the time this runs, primitives (``bool``, ``staticstringA`` …)
    and class names (``SkinnedMeshComponent``) are already labelled as
    ``type_name``. Anything left in ``other`` after that filter is
    almost certainly an enum/tag VALUE — the strings like ``Cloak``,
    ``Upperbody``, ``CD_Cloak``, ``CD_Cloak_Acc_01`` that show up
    near the end of every prefab.

    We try to associate each value with the most recent tag-typed
    property (``_shrinkTag``, ``_boneOffsetTag``, ``_customGameData``,
    etc.) so the editor can show ``_shrinkTag = Cloak`` instead of
    just ``Cloak``.
    """
    TAG_PROPS = (
        "_shrinkTag",
        "_boneOffsetTag",
        "_attachedSocketName",
        "_pivotSocketName",
        "_socketFileName",
        "_modelBoneAnimationScriptKey",
        "_customGameData",
        "_skinnedMeshFile",
        "_skeletonFileName",
        "_skinnedMeshFileName",
    )
    # Remember the last-seen tag property by offset. When we hit an
    # "other" string at a LATER offset, pair them.
    last_tag_prop: str | None = None
    for s in prefab.strings:
        if s.category == "property_name" and s.value in TAG_PROPS:
            last_tag_prop = s.value
            continue
        if s.category == "other":
            s.category = "tag_value"
            s.property_name = last_tag_prop


def parse_prefab(data: bytes, filename: str = "") -> ParsedPrefab:
    """Parse a .prefab into a :class:`ParsedPrefab`.

    Raises ``ValueError`` if the magic is wrong — no heuristic
    fallback. The byte-level string scan is tolerant to unknown
    property types, so format drift between game patches only loses
    us annotations, never the ability to edit known-position strings.
    """
    if len(data) < len(PREFAB_MAGIC) + 12 or not data.startswith(PREFAB_MAGIC):
        raise ValueError(f"not a .prefab file: magic={data[:6]!r}")

    hash1 = struct.unpack_from("<I", data, 0x06)[0]
    hash2 = struct.unpack_from("<I", data, 0x0A)[0]
    marker = struct.unpack_from("<I", data, 0x0E)[0]

    prefab = ParsedPrefab(
        path=filename, hash1=hash1, hash2=hash2, marker=marker, raw=data,
    )

    for prefix_offset, length, value in _scan_lenstrings(data):
        prefab.strings.append(PrefabString(
            prefix_offset=prefix_offset,
            value_offset=prefix_offset + 4,
            length=length,
            value=value,
            category=_classify(value),
        ))

    _annotate_tag_values(prefab)

    logger.info(
        "Parsed prefab %s: %d strings  (%d file_refs, %d tag_values, "
        "%d property_names, %d type_names)",
        filename,
        len(prefab.strings),
        len(prefab.file_references()),
        len(prefab.tag_values()),
        len(prefab.property_names()),
        len(prefab.type_names()),
    )
    return prefab


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@dataclass
class PrefabEdit:
    """One single-string edit. ``prefix_offset`` identifies which
    length-prefixed string to rewrite."""
    prefix_offset: int
    new_value: str


def apply_edits(
    prefab: ParsedPrefab,
    edits: list[PrefabEdit],
    *,
    allow_length_change: bool = False,
) -> bytes:
    """Return a new byte stream with all ``edits`` applied.

    When ``allow_length_change`` is False (default), an edit that
    would change the string's byte length raises ``ValueError``. This
    is the safe mode the community's guides recommend.

    When True, length changes are applied by rewriting the uint32
    prefix and shifting all downstream bytes. Prefabs carry no
    internal offset table (verified via byte-level inspection on 4
    samples) so downstream shift is safe; we DO regenerate the file's
    hash fields after to stay consistent with any integrity check the
    game might run — but since the two header hashes do not match any
    common CRC algorithms we could identify (CRC32, FNV, Adler,
    Murmur), they are left untouched. See the doc-string comment
    block for the rationale.
    """
    # Sort edits by offset so we rewrite in a single forward pass and
    # can accumulate length deltas.
    edits_sorted = sorted(edits, key=lambda e: e.prefix_offset)

    # Build an offset-indexed lookup for fast access
    by_offset = {s.prefix_offset: s for s in prefab.strings}

    new_bytes = bytearray()
    cursor = 0  # how many bytes of the ORIGINAL we've copied so far

    for edit in edits_sorted:
        original = by_offset.get(edit.prefix_offset)
        if original is None:
            raise ValueError(
                f"edit references unknown offset 0x{edit.prefix_offset:04x}"
            )
        new_value_bytes = edit.new_value.encode("utf-8")
        if not allow_length_change and len(new_value_bytes) != original.length:
            raise ValueError(
                f"length change {original.length} -> {len(new_value_bytes)} "
                f"rejected for {original.value!r} -> {edit.new_value!r} "
                f"(pass allow_length_change=True to override)"
            )

        # Copy untouched bytes BEFORE this string's length prefix
        new_bytes.extend(prefab.raw[cursor: edit.prefix_offset])
        # Write new uint32 length + UTF-8 bytes
        new_bytes.extend(struct.pack("<I", len(new_value_bytes)))
        new_bytes.extend(new_value_bytes)
        # Advance the cursor past the ORIGINAL string (so any length
        # change naturally drops the old bytes and picks up downstream
        # content unchanged).
        cursor = original.prefix_offset + 4 + original.length

    # Trailing untouched bytes
    new_bytes.extend(prefab.raw[cursor:])
    return bytes(new_bytes)


def rewrite_prefab_string(
    data: bytes,
    old_value: str,
    new_value: str,
    *,
    allow_length_change: bool = False,
) -> bytes:
    """Convenience helper: replace the first occurrence of ``old_value``.

    Raises ValueError if ``old_value`` isn't found as a length-prefixed
    string.
    """
    prefab = parse_prefab(data)
    target = prefab.find_string(old_value)
    if target is None:
        raise ValueError(f"string {old_value!r} not found in prefab")
    return apply_edits(
        prefab,
        [PrefabEdit(prefix_offset=target.prefix_offset, new_value=new_value)],
        allow_length_change=allow_length_change,
    )
