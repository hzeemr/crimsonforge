"""Proper TAG0 binary parser for Havok 2024.2 (SDK 20240200).

The CrimsonForge codebase already ships a heuristic Havok parser
(``core.havok_parser``) that scans for class-name ASCII substrings to
surface enough information for the physics-risk assessor. That parser
is intentionally loose because the TAG0 format wasn't mapped.

This module is the proper mapping, reverse-engineered against shipping
Crimson Desert HKX files (April 2026). It gives us a section tree we
can walk, edit, and re-serialise — the foundation for the rest of the
Havok enterprise-level work (type reflection, instance walking, full
round-trip, mesh-topology rebinding).

Binary structure
----------------

The whole file is a tree of sections. Every section starts with an
8-byte header::

    struct SectionHeader {
        uint32_be  header;   // top 4 bits = flags, low 28 bits = size
                             //   bit 30 (0x4 in the flag nibble) = "leaf"
                             //                                     i.e. no child sections
                             // low 28 bits = total section size including
                             // this 8-byte header
        char[4]    tag;      // ASCII 4-char identifier, e.g. "TAG0", "DATA"
    }

Observed section tags
---------------------

  Root:        TAG0
      SDKV    ASCII SDK version string (leaf). e.g. "20240200"
      DATA    Binary instance data (leaf).
      TYPE    Container for the type-reflection tables:
          TPTR / MTTP   "Memory Type Table Pointers"
          TST1 / TSTR   Type / field string table
          TNA1 / TNAM   Type-name definitions (class registry)
          TBDY / TBOD   Type body (field offsets + kinds)
          TPAD          Padding to 16-byte alignment
      INDX    Container for the object index:
          ITEM          Instance header table (offset + type + count)
          PTCH          Pointer patch table (relocations inside DATA)

Section flags
-------------

Bit 0x4 (in the top-nibble flag field) marks a **leaf section** — it
has no sub-sections, its contents are raw bytes the callee is free to
interpret. Bit 0x4 being clear means the section is a **container**
and its body is more SectionHeaders.

We don't yet know what the other flag bits mean in the wild; they're
zero on every shipping file we've checked.

Usage
-----

    blob = open("character/phm_base.hkx", "rb").read()
    doc = parse_tag0(blob)
    for sec in doc.walk():
        print(sec.tag, sec.offset, sec.size, sec.depth)

    data_section = doc.find("DATA")
    type_section = doc.find("TYPE")
    items = doc.find("INDX/ITEM")  # path query

``parse_tag0`` is zero-dependency and stdlib only, so it's safe to
import from tests.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Iterator


TAG0_MAGIC = b"TAG0"
HEADER_SIZE = 8

# Flag bit masks. The header is a BE u32 where the top nibble carries
# flags and the low 28 bits carry the section size.
_LEAF_FLAG = 0x4   # section has no children, body is raw bytes


class Tag0Error(ValueError):
    """Raised when the byte stream does not match the TAG0 grammar."""


@dataclass
class Tag0Section:
    """One section of a TAG0 document.

    ``offset`` and ``size`` are absolute positions into the original
    byte stream so downstream decoders can address raw bytes by offset
    without carrying the buffer around. ``children`` is only populated
    for non-leaf sections.
    """
    tag: str                 # 4-char ASCII tag (e.g. "DATA")
    offset: int              # absolute offset of the header in the source buffer
    size: int                # total section size in bytes, INCLUDES the 8-byte header
    flags: int               # raw top-nibble flags (0x4 set means leaf)
    depth: int = 0           # nesting depth — 0 for the root TAG0
    children: list["Tag0Section"] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return bool(self.flags & _LEAF_FLAG)

    @property
    def body_offset(self) -> int:
        """Absolute offset where the section's payload starts (past the 8-byte header)."""
        return self.offset + HEADER_SIZE

    @property
    def body_size(self) -> int:
        return self.size - HEADER_SIZE

    def body_slice(self, buffer: bytes) -> bytes:
        return buffer[self.body_offset:self.body_offset + self.body_size]

    def find(self, path: str) -> "Tag0Section | None":
        """Return the first descendant matching a slash-delimited tag path.

        ``find("INDX/ITEM")`` returns the ITEM section inside INDX. Path
        segments are matched against :attr:`tag` exactly (case-
        sensitive, ASCII). Returns ``None`` when no path matches.
        """
        parts = path.split("/")
        cur: Tag0Section | None = self
        for part in parts:
            if cur is None:
                return None
            cur = next((c for c in cur.children if c.tag == part), None)
        return cur

    def find_all(self, tag: str) -> list["Tag0Section"]:
        """Return every descendant section whose ``tag`` matches."""
        out: list[Tag0Section] = []
        for sec in self.walk():
            if sec.tag == tag:
                out.append(sec)
        return out

    def walk(self) -> Iterator["Tag0Section"]:
        """Depth-first iterator starting at ``self`` (inclusive)."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Tag0Document:
    """Parsed TAG0 document — one root section plus byte-offset metadata."""
    root: Tag0Section
    sdk_version: str = ""
    total_size: int = 0

    def walk(self) -> Iterator[Tag0Section]:
        return self.root.walk()

    def find(self, path: str) -> Tag0Section | None:
        """Path lookup rooted at the TAG0 section.

        Prefixing the path with ``TAG0/`` is optional — the root is
        implied. ``find("DATA")`` and ``find("TAG0/DATA")`` both work.
        """
        if path.startswith("TAG0/"):
            path = path[len("TAG0/"):]
        if path == "TAG0":
            return self.root
        return self.root.find(path)

    def find_all(self, tag: str) -> list[Tag0Section]:
        return self.root.find_all(tag)


# ---------------------------------------------------------------------------
# Header codec
# ---------------------------------------------------------------------------

def encode_section_header(tag: str, size: int, *, leaf: bool) -> bytes:
    """Encode a TAG0 section header.

    ``size`` is the **total** section size including the 8-byte header.
    Used by the round-trip writer — callers of the parser never need it.
    """
    if len(tag) != 4:
        raise ValueError(f"section tag must be 4 chars, got {tag!r}")
    if size < HEADER_SIZE:
        raise ValueError(f"section size {size} cannot be less than the 8-byte header")
    if size >= (1 << 28):
        raise ValueError(f"section size {size} exceeds 28-bit limit")
    flags = _LEAF_FLAG if leaf else 0
    header = (flags << 28) | (size & 0x0FFFFFFF)
    return struct.pack(">I", header) + tag.encode("ascii")


def decode_section_header(buffer: bytes, offset: int) -> tuple[str, int, int]:
    """Decode one section header. Returns ``(tag, size, flags)``.

    ``flags`` is the raw top nibble (0..15); :data:`_LEAF_FLAG` masks
    the bit we understand.
    """
    if offset + HEADER_SIZE > len(buffer):
        raise Tag0Error(
            f"truncated section header at 0x{offset:x} "
            f"(needs {HEADER_SIZE} bytes, only {len(buffer) - offset} available)"
        )
    raw = struct.unpack_from(">I", buffer, offset)[0]
    flags = (raw >> 28) & 0xF
    size = raw & 0x0FFFFFFF
    tag = buffer[offset + 4:offset + 8].decode("ascii", errors="replace")
    return tag, size, flags


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_tag0(buffer: bytes) -> Tag0Document:
    """Parse a TAG0 binary blob into a :class:`Tag0Document`.

    Raises :class:`Tag0Error` when the byte layout breaks the grammar —
    truncated sections, a child that overflows its parent, a section
    size that doesn't include its own header, etc. Callers that want
    a forgiving surface should use :func:`safe_parse_tag0` instead.
    """
    if len(buffer) < HEADER_SIZE:
        raise Tag0Error("buffer is too small to hold a TAG0 header")

    tag, size, flags = decode_section_header(buffer, 0)
    if tag != "TAG0":
        raise Tag0Error(f"expected TAG0 root section, got {tag!r}")
    if size == 0 or size > len(buffer):
        raise Tag0Error(f"root size {size} exceeds buffer length {len(buffer)}")

    root = Tag0Section(tag=tag, offset=0, size=size, flags=flags, depth=0)
    _parse_children(buffer, root, end=size, depth=1)

    sdk_section = root.find("SDKV")
    sdk_version = ""
    if sdk_section is not None:
        body = sdk_section.body_slice(buffer)
        # SDKV payloads end with zero padding. Keep the printable head only.
        text = body.decode("ascii", errors="replace")
        sdk_version = text.split("\x00", 1)[0].strip()

    return Tag0Document(root=root, sdk_version=sdk_version, total_size=size)


def safe_parse_tag0(buffer: bytes) -> Tag0Document | None:
    """Wrapper that returns ``None`` on any parse failure.

    Useful in UI paths where a malformed file shouldn't crash the app.
    """
    try:
        return parse_tag0(buffer)
    except Tag0Error:
        return None


def _parse_children(buffer: bytes, parent: Tag0Section, *, end: int, depth: int) -> None:
    """Recursive descent into a non-leaf section.

    ``end`` is the absolute offset where ``parent``'s body finishes. We
    refuse to read past it so a corrupted outer section can't drag the
    parser into the wrong location.
    """
    if parent.is_leaf:
        return
    pos = parent.body_offset
    while pos < end:
        tag, size, flags = decode_section_header(buffer, pos)
        if size < HEADER_SIZE:
            raise Tag0Error(
                f"section {tag!r} at 0x{pos:x} declares size {size} "
                f"(must include its own 8-byte header)"
            )
        if pos + size > end:
            raise Tag0Error(
                f"section {tag!r} at 0x{pos:x} overflows its parent "
                f"(pos+size={pos + size} > end={end})"
            )
        child = Tag0Section(tag=tag, offset=pos, size=size, flags=flags, depth=depth)
        parent.children.append(child)
        _parse_children(buffer, child, end=pos + size, depth=depth + 1)
        pos += size


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def format_tree(doc: Tag0Document) -> str:
    """Human-readable tree print of every section.

    Intended for CLI / REPL inspection::

        >>> print(format_tree(parse_tag0(data)))
        TAG0  size=35280  offset=0x0
            SDKV  leaf  size=16  offset=0x8
            DATA  leaf  size=30584  offset=0x18
            TYPE  size=1272  offset=0x7790
                MTTP  leaf  size=400  offset=0x7798
                ...
    """
    lines: list[str] = []
    for sec in doc.walk():
        indent = "    " * sec.depth
        leaf = "  leaf" if sec.is_leaf else ""
        lines.append(
            f"{indent}{sec.tag}{leaf}  size={sec.size}  offset=0x{sec.offset:x}"
        )
    return "\n".join(lines)


def iter_leaf_sections(doc: Tag0Document) -> Iterable[Tag0Section]:
    """Yield every leaf section in document order — convenient for payload walkers."""
    for sec in doc.walk():
        if sec.is_leaf:
            yield sec
