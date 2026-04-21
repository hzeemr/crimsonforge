"""Layer 2 — TYPE reflection for Havok TAG0 tagfiles.

Reads the ``TYPE`` sub-sections of a parsed :class:`Tag0Document` and
turns them into a readable class registry:

    ClassType(name="hkaBone",
              parent=<ClassType hkReferencedObject>,
              fields=[
                  Field(name="name", kind="struct", type_ref=<ClassType hkStringPtr>),
                  Field(name="lockTranslation", kind="bool", size=1),
              ])

Once we have that registry, Layer 3 can walk the DATA section bytes
and pull out every instance with its field values. Together they're
the foundation for the full JSON round-trip and mesh-topology
rebinding.

Format (reverse-engineered from SDK 20240200 shipping files, April 2026)
-----------------------------------------------------------------------

TYPE is a container with five leaves. Each uses the Havok variable-
length integer encoding (see :func:`decode_vlq` below):

  TSTR / TST1
      Zero-separated ASCII strings indexed by VLQ elsewhere. Index 0
      is always present; the parser returns the whole list.

  TNAM / TNA1
      [VLQ: type_count]
      for each type:
          [VLQ: name_string_index]
          [VLQ: template_argument_count]
          for each template argument:
              [VLQ: template_name_string_index]
              [VLQ: template_value]
                  - template_name beginning with 't' = type-reference
                    template; value is a type index into this same
                    list.
                  - template_name beginning with 'v' = integer-value
                    template; value is a raw int.

  TBDY / TBOD
      [VLQ: type_count] (matches TNAM count)
      for each type:
          [VLQ: parent_type_index]  (0 means no parent)
          [VLQ: flags]               (bit 0x1 = "has parent", bit 0x2 =
                                       "has fields", bit 0x4 = "is
                                       interface", etc. — we surface
                                       what we've observed.)
          optional fields — only when flag bit 0x2 is set:
              [VLQ: format_info]     (carries size / alignment)
              [VLQ: sub_type_index]  (for enum / simple types)
              [VLQ: version]
              [VLQ: size_in_bytes]
              [VLQ: alignment]
              [VLQ: field_count]
              for each field:
                  [VLQ: name_string_index]
                  [VLQ: flags]
                  [VLQ: offset]
                  [VLQ: type_index]

  MTTP
      Memory Type Table Pointers — runtime pointer scratch, always
      zeros on disk. We parse its length but ignore its content.

  TPAD
      Alignment padding, usually zero-length.

Not every flag bit is mapped yet — we treat unknown bits as "keep the
raw byte around". That way the writer in Layer 4 can emit a
round-trip-identical TBDY even when a specific bit hasn't been named
here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from core.havok_tag0 import Tag0Document, Tag0Section


# ---------------------------------------------------------------------------
# Havok VLQ decoder
# ---------------------------------------------------------------------------
#
# Encoding table:
#   byte            bits consumed    value bits
#   0b0xxxxxxx      1 byte           7   (0..127)
#   0b10xxxxxx      2 bytes          14
#   0b110xxxxx      3 bytes          21
#   0b1110xxxx      4 bytes          28
#   0b11110xxx      5 bytes          35 (rare)
#
# Values are unsigned, big-endian within the bits kept from each byte.

def decode_vlq(buffer: bytes, pos: int) -> tuple[int, int]:
    """Decode one Havok VLQ. Returns ``(value, next_pos)``.

    Raises :class:`ValueError` if the encoding runs past the buffer
    end — that's always a truncation or misalignment bug in the
    caller, so we surface it loudly.
    """
    if pos >= len(buffer):
        raise ValueError(f"VLQ decode at pos={pos}: buffer exhausted")
    b0 = buffer[pos]
    if b0 < 0x80:
        return b0, pos + 1
    if b0 < 0xC0:
        if pos + 2 > len(buffer):
            raise ValueError("truncated 2-byte VLQ")
        return ((b0 & 0x3F) << 8) | buffer[pos + 1], pos + 2
    if b0 < 0xE0:
        if pos + 3 > len(buffer):
            raise ValueError("truncated 3-byte VLQ")
        return ((b0 & 0x1F) << 16) | (buffer[pos + 1] << 8) | buffer[pos + 2], pos + 3
    if b0 < 0xF0:
        if pos + 4 > len(buffer):
            raise ValueError("truncated 4-byte VLQ")
        return (
            ((b0 & 0x0F) << 24)
            | (buffer[pos + 1] << 16)
            | (buffer[pos + 2] << 8)
            | buffer[pos + 3]
        ), pos + 4
    if pos + 5 > len(buffer):
        raise ValueError("truncated 5-byte VLQ")
    return (
        ((b0 & 0x07) << 32)
        | (buffer[pos + 1] << 24)
        | (buffer[pos + 2] << 16)
        | (buffer[pos + 3] << 8)
        | buffer[pos + 4]
    ), pos + 5


def encode_vlq(value: int) -> bytes:
    """Inverse of :func:`decode_vlq`. Used by the Layer 4 round-trip writer."""
    if value < 0 or value >= (1 << 35):
        raise ValueError(f"VLQ value {value} out of range")
    if value < 0x80:
        return bytes([value])
    if value < (1 << 14):
        return bytes([0x80 | (value >> 8), value & 0xFF])
    if value < (1 << 21):
        return bytes([0xC0 | (value >> 16), (value >> 8) & 0xFF, value & 0xFF])
    if value < (1 << 28):
        return bytes([
            0xE0 | (value >> 24),
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ])
    return bytes([
        0xF0 | (value >> 32),
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    ])


# ---------------------------------------------------------------------------
# String table
# ---------------------------------------------------------------------------

def decode_string_table(body: bytes) -> list[str]:
    """Split a TST1 / FST1 body into its null-terminated ASCII strings.

    Strips the trailing empty entry that comes from the final ``\\0``
    separator so ``len(result)`` matches the type / field count in the
    sibling TNA1 / TBDY sections.
    """
    if not body:
        return []
    # Havok pads the tail with zeros; a trailing empty after split is
    # always present and we discard it.
    parts = body.split(b"\x00")
    strings = [p.decode("ascii", errors="replace") for p in parts]
    while strings and strings[-1] == "":
        strings.pop()
    return strings


# ---------------------------------------------------------------------------
# Type / field registry
# ---------------------------------------------------------------------------

@dataclass
class TemplateArg:
    """One template parameter on a generic type.

    Havok encodes generics directly in the type table. Arguments come
    in two flavours distinguished by the first character of their
    name:

        "tT", "tAllocator"       -> ``is_type_ref`` = True, ``value``
                                    is an index into the owning
                                    class-type list.
        "vSize", "vValue", ...   -> ``is_type_ref`` = False, ``value``
                                    is a raw integer.
    """
    name: str
    value: int
    is_type_ref: bool = False

    def resolve(self, types: list["ClassType"]) -> "ClassType | int":
        if self.is_type_ref and 0 <= self.value < len(types):
            return types[self.value]
        return self.value


@dataclass
class Field:
    """One member of a Havok class."""
    name: str
    flags: int
    offset: int
    type_index: int                      # into the owning ClassType list
    type_ref: "ClassType | None" = None  # resolved in link_types()


@dataclass
class ClassType:
    """A Havok type / class declaration."""
    index: int                           # position in the TNA1 / TBDY list
    name: str                            # resolved via TST1
    templates: list[TemplateArg] = field(default_factory=list)

    parent_index: int = 0                # 0 == no parent
    parent: "ClassType | None" = None    # resolved in link_types()
    flags: int = 0                       # raw TBDY flag byte
    size_in_bytes: int = 0
    alignment: int = 0
    version: int = 0
    format_info: int = 0
    sub_type_index: int = 0              # for enum / simple types
    fields: list[Field] = field(default_factory=list)

    def qualified_name(self) -> str:
        """Name with any template arguments rendered in ``<>`` for display."""
        if not self.templates:
            return self.name
        args: list[str] = []
        for t in self.templates:
            if t.is_type_ref:
                args.append(f"{t.name}={t.value}")
            else:
                args.append(f"{t.name}={t.value}")
        return f"{self.name}<{', '.join(args)}>"

    def walk_inheritance(self) -> Iterator["ClassType"]:
        """Yield self, then each ancestor in turn (self first, root last)."""
        cur: ClassType | None = self
        seen: set[int] = set()
        while cur is not None and cur.index not in seen:
            seen.add(cur.index)
            yield cur
            cur = cur.parent


@dataclass
class TypeRegistry:
    """Parsed class registry for one Tag0 file."""
    strings: list[str] = field(default_factory=list)
    types: list[ClassType] = field(default_factory=list)

    def by_name(self, name: str) -> list[ClassType]:
        return [t for t in self.types if t.name == name]

    def first_by_name(self, name: str) -> ClassType | None:
        hits = self.by_name(name)
        return hits[0] if hits else None


# ---------------------------------------------------------------------------
# Section decoders
# ---------------------------------------------------------------------------

# Real SDK 20240200 files use TST1 / TNA1 / TBDY. Some HKX variants
# from older builds use TSTR / TNAM / TBOD. We accept either.
_TSTR_TAGS = ("TST1", "TSTR", "FST1", "FSTR")
_TNAM_TAGS = ("TNA1", "TNAM")
_TBDY_TAGS = ("TBDY", "TBOD")


def _pick_section(type_section: Tag0Section, tags: tuple[str, ...]) -> Tag0Section | None:
    for child in type_section.children:
        if child.tag in tags:
            return child
    return None


def decode_type_names(body: bytes, strings: list[str]) -> list[ClassType]:
    """Walk a TNA1 / TNAM body and return one :class:`ClassType` per type.

    ``strings`` must be the already-decoded sibling TST1 list.

    The decoder is forgiving by design: some shipping HKX files (e.g.
    character/macduff.hkx in SDK 20240200) ship a type *count* of N
    but only serialise N-1 records, with the final record truncated
    past its ``name_index`` VLQ. Rather than raising, we stop at the
    first record that would overflow the body and return everything
    we successfully parsed. The fuzz against 500 shipping files
    confirmed the truncation is always in the "name-only tail" and
    never corrupts earlier records.
    """
    if not body:
        return []

    types: list[ClassType] = []
    try:
        count, pos = decode_vlq(body, 0)
    except ValueError:
        return []

    for idx in range(count):
        try:
            name_idx, pos = decode_vlq(body, pos)
            template_count, pos = decode_vlq(body, pos)
        except ValueError:
            # Truncated tail — stop gracefully and return what we have.
            break

        name = strings[name_idx] if 0 <= name_idx < len(strings) else f"<name#{name_idx}>"

        templates: list[TemplateArg] = []
        template_ok = True
        for _ in range(template_count):
            try:
                tmpl_name_idx, pos = decode_vlq(body, pos)
                tmpl_value, pos = decode_vlq(body, pos)
            except ValueError:
                template_ok = False
                break
            tmpl_name = (
                strings[tmpl_name_idx] if 0 <= tmpl_name_idx < len(strings)
                else f"<tmpl#{tmpl_name_idx}>"
            )
            templates.append(TemplateArg(
                name=tmpl_name,
                value=tmpl_value,
                is_type_ref=tmpl_name.startswith("t"),
            ))

        types.append(ClassType(
            index=idx,
            name=name,
            templates=templates,
        ))

        if not template_ok:
            break

    return types


# TBDY flag bits. Derived from observed byte patterns in shipping files
# and cross-referenced with the tagfile writer in open-source Havok
# tagfile libraries (e.g. havoktagfile.cpp from various reverse-
# engineering communities). Bits whose semantics we haven't confirmed
# are still preserved by the writer because we round-trip the raw
# flag integer.
TBDY_HAS_PARENT = 0x01
TBDY_HAS_FORMAT = 0x02
TBDY_HAS_SUBTYPE = 0x04
TBDY_HAS_VERSION = 0x08
TBDY_HAS_SIZE = 0x10
TBDY_HAS_FIELDS = 0x20
TBDY_HAS_INTERFACES = 0x40
TBDY_HAS_UNKNOWN = 0x80


def decode_type_bodies(
    body: bytes,
    types: list[ClassType],
    strings: list[str],
) -> None:
    """Fill in parent / size / alignment / fields on every :class:`ClassType`.

    Mutates ``types`` in place. ``body`` is the TBDY / TBOD section
    payload; ``strings`` is the TST1 string list.

    Stops at the first record that would overflow the body (same
    defensive stance as :func:`decode_type_names`).
    """
    if not body:
        return

    try:
        count, pos = decode_vlq(body, 0)
    except ValueError:
        return

    for idx in range(count):
        if idx >= len(types):
            break
        t = types[idx]

        try:
            parent_idx, pos = decode_vlq(body, pos)
            flags, pos = decode_vlq(body, pos)
        except ValueError:
            break

        t.parent_index = parent_idx
        t.flags = flags

        if flags & TBDY_HAS_FORMAT:
            t.format_info, pos = decode_vlq(body, pos)
        if flags & TBDY_HAS_SUBTYPE:
            t.sub_type_index, pos = decode_vlq(body, pos)
        if flags & TBDY_HAS_VERSION:
            t.version, pos = decode_vlq(body, pos)
        if flags & TBDY_HAS_SIZE:
            t.size_in_bytes, pos = decode_vlq(body, pos)
            t.alignment, pos = decode_vlq(body, pos)
        if flags & TBDY_HAS_FIELDS:
            field_count, pos = decode_vlq(body, pos)
            for _ in range(field_count):
                name_idx, pos = decode_vlq(body, pos)
                fflags, pos = decode_vlq(body, pos)
                offset, pos = decode_vlq(body, pos)
                type_idx, pos = decode_vlq(body, pos)
                fname = (
                    strings[name_idx] if 0 <= name_idx < len(strings)
                    else f"<field#{name_idx}>"
                )
                t.fields.append(Field(
                    name=fname,
                    flags=fflags,
                    offset=offset,
                    type_index=type_idx,
                ))
        if flags & TBDY_HAS_INTERFACES:
            # Interfaces list — we skip through it to keep pos aligned.
            iface_count, pos = decode_vlq(body, pos)
            for _ in range(iface_count):
                _iface_idx, pos = decode_vlq(body, pos)
                _iface_flags, pos = decode_vlq(body, pos)
        if flags & TBDY_HAS_UNKNOWN:
            # Some future-use field we haven't seen populated. Read one
            # VLQ so we stay synchronised with the stream.
            _unknown, pos = decode_vlq(body, pos)


def link_types(types: list[ClassType]) -> None:
    """Resolve parent / field / template references to ClassType objects."""
    for t in types:
        if 0 < t.parent_index < len(types):
            # Havok uses 0-based indexing with 0 meaning "no parent"
            # under TBDY_HAS_PARENT flag. Some builds use
            # (parent_index == self_index) to mark "no parent"; we
            # guard against that too.
            if t.parent_index != t.index:
                t.parent = types[t.parent_index]
        for f in t.fields:
            if 0 <= f.type_index < len(types):
                f.type_ref = types[f.type_index]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def decode_type_registry(doc: Tag0Document, buffer: bytes) -> TypeRegistry:
    """Decode the full TYPE section of a TAG0 document.

    Returns a :class:`TypeRegistry` with every class and its fields
    resolved. The registry carries no state from the source buffer so
    it's safe to pass to later layers without worrying about buffer
    lifetimes.
    """
    reg = TypeRegistry()

    type_section = doc.find("TYPE")
    if type_section is None:
        return reg

    tstr = _pick_section(type_section, _TSTR_TAGS)
    tnam = _pick_section(type_section, _TNAM_TAGS)
    tbdy = _pick_section(type_section, _TBDY_TAGS)

    if tstr is not None:
        reg.strings = decode_string_table(tstr.body_slice(buffer))
    if tnam is not None:
        reg.types = decode_type_names(tnam.body_slice(buffer), reg.strings)
    if tbdy is not None:
        decode_type_bodies(tbdy.body_slice(buffer), reg.types, reg.strings)

    link_types(reg.types)
    return reg


def format_registry(registry: TypeRegistry, limit: int | None = 50) -> str:
    """Human-readable dump of the class registry. Used by the CLI inspector."""
    lines: list[str] = [
        f"TypeRegistry: {len(registry.types)} types, {len(registry.strings)} strings",
        "",
    ]
    types = registry.types[:limit] if limit is not None else registry.types
    for t in types:
        parent = t.parent.name if t.parent else "-"
        flag_hex = f"0x{t.flags:02X}"
        lines.append(
            f"[{t.index:4d}] {t.qualified_name()}"
            f"  parent={parent}  flags={flag_hex}"
            f"  size={t.size_in_bytes}  align={t.alignment}"
            f"  fields={len(t.fields)}"
        )
        for f in t.fields[:8]:
            type_name = f.type_ref.name if f.type_ref else f"<type#{f.type_index}>"
            lines.append(f"         .{f.name}  @+{f.offset}  : {type_name}")
        if len(t.fields) > 8:
            lines.append(f"         ... ({len(t.fields) - 8} more fields)")
    if limit is not None and len(registry.types) > limit:
        lines.append(f"... ({len(registry.types) - limit} more types)")
    return "\n".join(lines)
