"""Layer 5 — HkxDocument facade combining Layers 1-4 into one object.

Layers 1-4 split the TAG0 format by concern:

    L1  core.havok_tag0              section framing
    L2  core.havok_tag0_types        TYPE reflection (class registry)
    L3  core.havok_tag0_index        INDX walker (instance table + patches)
    L4  core.havok_tag0_writer       binary-identical rewriter

Layer 5 stitches those together into a single class that downstream
features can depend on without repeating the parse / link / serialise
dance every time. Concretely:

    hkx = HkxDocument.load(raw_bytes)
    print(hkx.registry.first_by_name("hknpLegacyCompressedMeshShape"))
    for instance in hkx.iter_instances():
        print(instance.item.type_ref.name, len(instance.payload))
    patched = hkx.replace_instance(instance.item.index, new_payload)
    patched_bytes = patched.to_bytes()

It's the surface every later semantic editor (mesh-topology rebinder,
cloth constraint remap, rigid-body transform editor) will use, and
the surface our future UI dialog hooks into.

Public helpers
--------------

    HkxDocument.load(buffer)          parse everything
    HkxDocument.iter_instances()      Layer 3 walker as Instance records
    HkxDocument.instance(index)       random-access instance lookup
    HkxDocument.instances_of(name)    filtered by class name
    HkxDocument.replace_data(new_data)
                                      produces a NEW document with its
                                      DATA section replaced; binary-
                                      identical everywhere else.
    HkxDocument.replace_instance(idx, payload)
                                      helper that splices ``payload``
                                      into the DATA bytes at the
                                      target instance's offset without
                                      disturbing neighbouring bytes.
    HkxDocument.to_bytes()            emit the file as a byte string.
    HkxDocument.to_json(indent=2)     editable JSON representation
                                      keyed by instance index. Useful
                                      for humans inspecting a file and
                                      for diffing two files in git.

The class is intentionally *immutable* — every mutation returns a new
``HkxDocument``. That makes it safe to hand one HkxDocument to
multiple editors in parallel without fearing cross-contamination, and
it matches how we already structured the writer (``rewrite_leaf``
returns a new bytes object rather than mutating in place).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Iterator

from core.havok_tag0 import Tag0Document, parse_tag0, safe_parse_tag0
from core.havok_tag0_index import (
    InstanceIndex,
    Item,
    decode_instance_index,
    iter_instances,
)
from core.havok_tag0_types import (
    ClassType,
    TypeRegistry,
    decode_type_registry,
    format_registry,
)
from core.havok_tag0_writer import rewrite_leaf


@dataclass(frozen=True, slots=True)
class Instance:
    """One serialised Havok instance, plus its payload bytes."""
    item: Item
    payload: bytes

    @property
    def class_name(self) -> str:
        if self.item.type_ref is not None:
            return self.item.type_ref.name
        return f"<type#{self.item.type_index}>"

    @property
    def offset(self) -> int:
        return self.item.data_offset

    @property
    def is_array(self) -> bool:
        return self.item.is_array

    @property
    def flags(self) -> int:
        return self.item.flags


class HkxDocument:
    """Parsed HKX document with read-only field views and ``to_bytes``.

    Instances are discovered via :meth:`iter_instances`. Mutations
    produce a new :class:`HkxDocument` — the original never changes.
    """

    __slots__ = ("_buffer", "_tag0", "_registry", "_index", "_data_body_offset")

    def __init__(
        self,
        buffer: bytes,
        tag0: Tag0Document,
        registry: TypeRegistry,
        index: InstanceIndex,
    ) -> None:
        self._buffer = buffer
        self._tag0 = tag0
        self._registry = registry
        self._index = index
        data = tag0.find("DATA")
        self._data_body_offset = data.body_offset if data is not None else 0

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, buffer: bytes) -> "HkxDocument":
        """Parse an HKX byte string. Raises on malformed input."""
        tag0 = parse_tag0(buffer)
        registry = decode_type_registry(tag0, buffer)
        index = decode_instance_index(tag0, buffer, registry)
        return cls(buffer, tag0, registry, index)

    @classmethod
    def safe_load(cls, buffer: bytes) -> "HkxDocument | None":
        """Parse or return ``None`` on any error."""
        tag0 = safe_parse_tag0(buffer)
        if tag0 is None:
            return None
        try:
            registry = decode_type_registry(tag0, buffer)
            index = decode_instance_index(tag0, buffer, registry)
        except Exception:
            return None
        return cls(buffer, tag0, registry, index)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def buffer(self) -> bytes:
        return self._buffer

    @property
    def sdk_version(self) -> str:
        return self._tag0.sdk_version

    @property
    def total_size(self) -> int:
        return self._tag0.total_size

    @property
    def tag0(self) -> Tag0Document:
        return self._tag0

    @property
    def registry(self) -> TypeRegistry:
        return self._registry

    @property
    def index(self) -> InstanceIndex:
        return self._index

    @property
    def data_body(self) -> bytes:
        data = self._tag0.find("DATA")
        return data.body_slice(self._buffer) if data is not None else b""

    # ------------------------------------------------------------------
    # Instance walkers
    # ------------------------------------------------------------------

    def iter_instances(self) -> Iterator[Instance]:
        """Yield :class:`Instance` for every non-placeholder item."""
        data_body = self.data_body
        for item, payload in iter_instances(self._index, data_body):
            yield Instance(item=item, payload=payload)

    def instance(self, index: int) -> Instance | None:
        """Return the instance at ``ITEM`` index ``index`` (non-placeholder)."""
        if not (0 <= index < len(self._index.items)):
            return None
        item = self._index.items[index]
        if item.is_placeholder:
            return None
        payload = self._index.payload_slice(item, self.data_body)
        return Instance(item=item, payload=payload)

    def instances_of(self, class_name: str) -> list[Instance]:
        """All instances whose :class:`ClassType` name matches ``class_name``."""
        return [inst for inst in self.iter_instances() if inst.class_name == class_name]

    def types_by_name(self, name: str) -> list[ClassType]:
        return self._registry.by_name(name)

    # ------------------------------------------------------------------
    # Mutation helpers (always return a new HkxDocument)
    # ------------------------------------------------------------------

    def replace_data(self, new_data_body: bytes) -> "HkxDocument":
        """Return a new document with the ``DATA`` section body replaced.

        The INDX / TYPE / SDKV sections are preserved byte-for-byte.
        Offsets inside ``new_data_body`` must match what the existing
        INDX/ITEM table says — this method does **not** validate that,
        it's up to the caller (semantic editors know which bytes they
        just rewrote).
        """
        patched = rewrite_leaf(self._tag0, self._buffer, "DATA", new_data_body)
        return HkxDocument.load(patched)

    def replace_instance(self, item_index: int, new_payload: bytes) -> "HkxDocument":
        """Replace one instance's payload with ``new_payload``.

        ``new_payload`` must be the same length as the original payload
        so neighbouring instance offsets stay valid. The writer raises
        :class:`ValueError` on a size mismatch — resizing an instance
        is allowed, but only through the lower-level ``replace_data``
        call that lets the editor recompute every subsequent offset.
        """
        inst = self.instance(item_index)
        if inst is None:
            raise ValueError(f"no non-placeholder instance at index {item_index}")
        original_payload = inst.payload
        if len(new_payload) != len(original_payload):
            raise ValueError(
                f"instance {item_index} payload is {len(original_payload)} bytes; "
                f"new payload is {len(new_payload)} bytes. Use replace_data for "
                f"resizing edits — it lets the caller recompute neighbouring offsets."
            )

        # Splice into the existing DATA body at this instance's offset.
        existing = bytearray(self.data_body)
        start = inst.item.data_offset
        existing[start:start + len(original_payload)] = new_payload
        return self.replace_data(bytes(existing))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Emit the file as raw bytes. Byte-identical for an unchanged document."""
        return self._buffer

    def to_json(self, *, indent: int | None = 2, max_payload_bytes: int = 256) -> str:
        """Editable JSON representation of the whole document.

        Format::

            {
              "sdk_version": "20240200",
              "total_size":  35280,
              "types":       [{"index": 0, "name": "...", "parent": "...",
                               "fields": [...], "templates": [...]}],
              "instances":   [{"index": 1, "class": "hkaSkeleton",
                               "offset": 0x100, "count": 1,
                               "flags": "0x10", "payload_b64": "..."}],
              "patches":     [{"type": "hkStringPtr",
                               "offsets": [0x4, 0x8]}]
            }

        ``max_payload_bytes`` caps how many bytes of each payload end
        up in the JSON. Pass a very large value (or ``None``) to
        include the full payload — useful for forensic dumps, noisy
        for regular editing.
        """
        def _payload_b64(payload: bytes) -> str:
            sliced = payload if max_payload_bytes is None else payload[:max_payload_bytes]
            return base64.b64encode(sliced).decode("ascii")

        types_json: list[dict] = []
        for t in self._registry.types:
            types_json.append({
                "index": t.index,
                "name": t.name,
                "qualified_name": t.qualified_name(),
                "parent": t.parent.name if t.parent is not None else None,
                "flags": f"0x{t.flags:02X}",
                "size_in_bytes": t.size_in_bytes,
                "alignment": t.alignment,
                "version": t.version,
                "templates": [
                    {"name": a.name, "value": a.value, "is_type_ref": a.is_type_ref}
                    for a in t.templates
                ],
                "fields": [
                    {
                        "name": f.name,
                        "offset": f.offset,
                        "type_index": f.type_index,
                        "type_name": f.type_ref.name if f.type_ref is not None else None,
                        "flags": f"0x{f.flags:02X}",
                    }
                    for f in t.fields
                ],
            })

        instances_json: list[dict] = []
        for inst in self.iter_instances():
            entry = {
                "index": inst.item.index,
                "class": inst.class_name,
                "offset": f"0x{inst.offset:06X}",
                "count": inst.item.count,
                "flags": f"0x{inst.flags:02X}",
                "is_array": inst.is_array,
                "payload_size": len(inst.payload),
                "payload_b64": _payload_b64(inst.payload),
            }
            if max_payload_bytes is not None and len(inst.payload) > max_payload_bytes:
                entry["payload_truncated_to"] = max_payload_bytes
            instances_json.append(entry)

        patches_json: list[dict] = []
        for block in self._index.patches:
            type_name = block.type_ref.name if block.type_ref is not None else f"<type#{block.type_index}>"
            patches_json.append({
                "type": type_name,
                "type_index": block.type_index,
                "offsets": [f"0x{o:06X}" for o in block.offsets],
            })

        doc = {
            "sdk_version": self.sdk_version,
            "total_size": self.total_size,
            "types": types_json,
            "instances": instances_json,
            "patches": patches_json,
        }
        return json.dumps(doc, indent=indent, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Short human summary — used by CLI / REPL inspection."""
        return (
            f"HkxDocument  sdk={self.sdk_version}  total_size={self.total_size}\n"
            f"  types: {len(self._registry.types)} distinct\n"
            f"  instances: {sum(1 for _ in self.iter_instances())} (plus 1 placeholder)\n"
            f"  patches: {len(self._index.patches)} blocks, "
            f"{sum(len(b.offsets) for b in self._index.patches)} fix-ups"
        )

    def format_types(self, limit: int | None = 50) -> str:
        return format_registry(self._registry, limit=limit)

    def __repr__(self) -> str:  # pragma: no cover — REPL niceness
        return (
            f"<HkxDocument sdk={self.sdk_version!r} "
            f"types={len(self._registry.types)} "
            f"instances={sum(1 for _ in self.iter_instances())}>"
        )
