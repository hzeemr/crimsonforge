"""Layer 3 — INDX section walker (instance table + pointer patches).

Sits on top of Layers 1 (core/havok_tag0.py) and 2 (core/havok_tag0_types.py)
to enumerate every serialised instance in an HKX DATA section. With
this layer, we can give every instance a triple::

    (class_name, offset_in_DATA, byte_payload)

That's enough for Layer 4 (JSON dump and binary-identical writer) and
Layer 5 (semantic editors like the mesh-topology rebinder that will
fix fuse00_'s beard).

Binary layout
-------------

``INDX`` has two sub-sections:

  ITEM
      [12 bytes per record]
      record 0 is always zeros — a sentinel pointer-target that real
      ITEM entries can reference safely.

      struct ItemRecord {
          uint32_le  type_with_flags;  // low 24 bits = type index,
                                       // top 8 bits = flags (observed
                                       // values: 0x10 = "has payload",
                                       // 0x20 = "is array", 0x30 =
                                       // "array of containers", 0x40 =
                                       // "interface table")
          uint32_le  data_offset;      // byte offset within DATA
          uint32_le  count;            // array length for containers,
                                       // else 1 for plain instances
      }

  PTCH
      Variable-length fix-up table. Each block::

          uint32_le  type_index;
          uint32_le  offset_count;
          uint32_le  offsets[offset_count];

      Blocks are packed until the section ends. Each offset tells the
      runtime that the 4-byte / 8-byte pointer at that absolute byte
      in the DATA section needs to be rewritten with the memory
      address of the instance whose ITEM type matches ``type_index``.

Flag bits in ITEM's top byte
----------------------------

Observed across 500 shipping files:

  0x00   placeholder / sentinel (record 0)
  0x10   plain instance with a payload pointer
  0x20   instance whose payload is an array
  0x30   array-of-containers
  0x40   interface-table / vtable-ish pointer
  0x50   combined flags seen on containers of arrays

We surface the raw flag byte so downstream code (the JSON serialiser
and the mesh-rebinder) can make decisions without us having to map
every bit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

from core.havok_tag0 import Tag0Document, Tag0Section
from core.havok_tag0_types import ClassType, TypeRegistry


ITEM_RECORD_SIZE = 12


@dataclass(slots=True)
class Item:
    """One serialised instance in DATA."""
    index: int                 # position in the ITEM table
    type_index: int            # into TypeRegistry.types
    flags: int                 # top byte of the type_with_flags field
    data_offset: int           # absolute offset within DATA section payload
    count: int                 # array length (1 for scalar instances)

    type_ref: ClassType | None = None  # resolved via link_items()

    @property
    def is_placeholder(self) -> bool:
        """Record 0 is always zero so real records can point at it safely."""
        return self.index == 0 and self.type_index == 0 and self.data_offset == 0

    @property
    def is_array(self) -> bool:
        """Set when flag bit 0x20 is on — payload is a tightly packed array."""
        return bool(self.flags & 0x20)


@dataclass(slots=True)
class PatchBlock:
    """One (type, offsets) group in the PTCH section."""
    type_index: int
    offsets: list[int]
    type_ref: ClassType | None = None


@dataclass
class InstanceIndex:
    """Fully parsed INDX section."""
    items: list[Item] = field(default_factory=list)
    patches: list[PatchBlock] = field(default_factory=list)

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def items_by_type_name(self, name: str) -> list[Item]:
        return [it for it in self.items if it.type_ref is not None and it.type_ref.name == name]

    def payload_slice(self, item: Item, data_section_body: bytes) -> bytes:
        """Return the raw bytes for one item's payload inside DATA.

        Without knowing the per-class size we can only reliably slice
        from ``data_offset`` to the next item's ``data_offset`` (or to
        the end of DATA). That is always a valid upper bound and
        matches what the runtime loads into the pointer target.
        """
        if item.is_placeholder:
            return b""
        start = item.data_offset
        # Find the smallest offset strictly greater than this item's.
        next_offsets = [
            other.data_offset
            for other in self.items
            if other.data_offset > item.data_offset
        ]
        end = min(next_offsets) if next_offsets else len(data_section_body)
        end = max(start, min(end, len(data_section_body)))
        return data_section_body[start:end]


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------

def decode_item_table(body: bytes) -> list[Item]:
    """Walk an ITEM section body into a list of :class:`Item` records."""
    if not body:
        return []

    items: list[Item] = []
    record_count = len(body) // ITEM_RECORD_SIZE
    for idx in range(record_count):
        base = idx * ITEM_RECORD_SIZE
        type_with_flags = struct.unpack_from("<I", body, base)[0]
        data_offset = struct.unpack_from("<I", body, base + 4)[0]
        count = struct.unpack_from("<I", body, base + 8)[0]
        flags = (type_with_flags >> 24) & 0xFF
        type_index = type_with_flags & 0x00FFFFFF
        items.append(Item(
            index=idx,
            type_index=type_index,
            flags=flags,
            data_offset=data_offset,
            count=count,
        ))
    return items


def decode_patch_table(body: bytes) -> list[PatchBlock]:
    """Walk a PTCH section body into a list of :class:`PatchBlock` groups."""
    if not body:
        return []

    blocks: list[PatchBlock] = []
    pos = 0
    while pos + 8 <= len(body):
        type_index = struct.unpack_from("<I", body, pos)[0]
        offset_count = struct.unpack_from("<I", body, pos + 4)[0]
        pos += 8
        if offset_count == 0:
            blocks.append(PatchBlock(type_index=type_index, offsets=[]))
            continue
        end = pos + offset_count * 4
        if end > len(body):
            break
        offsets = list(struct.unpack_from(f"<{offset_count}I", body, pos))
        pos = end
        blocks.append(PatchBlock(type_index=type_index, offsets=offsets))

    return blocks


def link_index(index: InstanceIndex, registry: TypeRegistry) -> None:
    """Resolve ``type_index`` references to :class:`ClassType` objects.

    Items whose ``type_index`` falls outside the registry are left with
    ``type_ref=None`` — we don't raise here because malformed files
    should still show every other instance. The writer in Layer 4
    round-trips ``type_index`` verbatim so this is lossless.
    """
    for item in index.items:
        if 0 <= item.type_index < len(registry.types):
            item.type_ref = registry.types[item.type_index]
    for patch in index.patches:
        if 0 <= patch.type_index < len(registry.types):
            patch.type_ref = registry.types[patch.type_index]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def decode_instance_index(
    doc: Tag0Document,
    buffer: bytes,
    registry: TypeRegistry | None = None,
) -> InstanceIndex:
    """Decode the full INDX section into an :class:`InstanceIndex`.

    ``registry`` is optional; when provided, every :class:`Item` and
    :class:`PatchBlock` will have its ``type_ref`` resolved to the
    matching :class:`ClassType` from Layer 2.
    """
    index = InstanceIndex()

    item_section = doc.find("INDX/ITEM")
    patch_section = doc.find("INDX/PTCH")

    if item_section is not None:
        index.items = decode_item_table(item_section.body_slice(buffer))
    if patch_section is not None:
        index.patches = decode_patch_table(patch_section.body_slice(buffer))

    if registry is not None:
        link_index(index, registry)

    return index


def iter_instances(
    index: InstanceIndex,
    data_body: bytes,
) -> Iterator[tuple[Item, bytes]]:
    """Yield ``(item, payload_bytes)`` for every non-placeholder instance.

    This is the primary surface for the JSON dumper in Layer 4 and the
    mesh-topology rebinder in Layer 5 — every consumer of "give me
    each serialised Havok object in this file" goes through here.
    """
    for item in index.items:
        if item.is_placeholder:
            continue
        yield item, index.payload_slice(item, data_body)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_index(index: InstanceIndex, limit: int | None = 40) -> str:
    """Human-readable dump of the item / patch tables."""
    lines: list[str] = [
        f"InstanceIndex: {len(index.items)} items, "
        f"{len(index.patches)} patch blocks "
        f"({sum(len(b.offsets) for b in index.patches)} patch entries total)",
        "",
        "Items:",
    ]
    shown = 0
    for item in index.items:
        if item.is_placeholder:
            continue
        type_name = item.type_ref.name if item.type_ref is not None else f"<type#{item.type_index}>"
        lines.append(
            f"  [{item.index:4d}] type={type_name:<40s} "
            f"flags=0x{item.flags:02X}  offset=0x{item.data_offset:06X}  count={item.count}"
        )
        shown += 1
        if limit is not None and shown >= limit:
            lines.append(f"  ... ({len(index.items) - shown - 1} more items)")
            break

    lines.append("")
    lines.append("Patches:")
    for block in index.patches[:limit or len(index.patches)]:
        type_name = block.type_ref.name if block.type_ref is not None else f"<type#{block.type_index}>"
        lines.append(
            f"  type={type_name:<40s}  offsets={len(block.offsets)}"
        )
    return "\n".join(lines)
