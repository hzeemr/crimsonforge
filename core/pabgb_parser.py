"""Parser for Pearl Abyss .pabgb / .pabgh game-data binary tables.

Reverse-engineered binary layout (Crimson Desert, April 2026):

.pabgh header
--------------
  [row_count : uint16-LE]
  Followed by *row_count* row descriptors.  Two flavours:

  * **Simple** (sequential 1-based IDs, 5 bytes each):
      [row_id : uint8] [data_offset : uint32-LE]
      Detected when first id byte == 0x01 AND 2 + count*5 == file size.
      Data is row-major with fixed row size.

  * **Hashed** (most tables, 8 bytes each):
      [row_hash : uint32-LE] [data_offset : uint32-LE]
      Each row_hash is repeated as the first 4 bytes at its data_offset.
      Data is row-major: [row_hash:4] [field0] [field1] ... [fieldN]

.pabgb data  (hashed flavour)
------------------------------
  Each row region starts with its own hash (matching the header) and
  contains all fields for that row packed sequentially.  Field values
  are a mix of:
    - uint32 integers  (4 bytes LE)
    - float32 values   (4 bytes LE, IEEE 754)
    - null-terminated strings: [strlen:u32-LE] [chars:strlen] [null:1]
    - packed sub-structures / arrays

  The first field after the hash is typically either:
    - A string (name/identifier) — detected by checking if the bytes
      following the length look like printable ASCII.
    - An integer/float value.

.pabgb data  (simple flavour)
-----------------------------
  Row-major fixed-size layout.  Row size = offset[1] - offset[0].
  Each row: [field0:N bytes] [field1:N bytes] ...  (no type tags).
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

logger = get_logger("core.pabgb_parser")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class PabgbField:
    """One field value within a row."""
    offset: int          # byte offset within the row region
    size: int            # byte length
    raw: bytes           # original bytes
    kind: str            # "u32", "f32", "i32", "str", "hash", "blob"
    value: Any           # interpreted value

    def display_value(self) -> str:
        if self.kind == "str":
            return str(self.value)
        if self.kind == "f32":
            return f"{self.value:.4f}"
        if self.kind == "u32" or self.kind == "hash":
            v = self.value
            if isinstance(v, int):
                if v > 0xFFFF:
                    return f"0x{v:08X}"
                return str(v)
            return str(v)
        if self.kind == "i32":
            return str(self.value)
        if self.kind == "blob":
            return self.raw.hex()[:40] + ("..." if len(self.raw) > 20 else "")
        return str(self.value)


@dataclass
class PabgbRow:
    """One row (record) in the table."""
    index: int
    row_hash: int        # hash from header (also first 4 bytes of data)
    data_offset: int     # byte offset in .pabgb
    data_size: int       # byte length of this row's region
    name: str            # first string field (if any), else hex hash
    fields: list[PabgbField]
    raw: bytes           # original row bytes

    @property
    def display_name(self) -> str:
        return self.name if self.name else f"0x{self.row_hash:08X}"


@dataclass
class PabgbTable:
    """Parsed game-data table."""
    file_name: str
    rows: list[PabgbRow]
    raw_data: bytes
    is_simple: bool
    row_size: int = 0    # only for simple tables
    field_count: int = 0 # detected uniform field count


# ---------------------------------------------------------------------------
# Field detection heuristics
# ---------------------------------------------------------------------------
def _looks_like_string(data: bytes, pos: int) -> bool:
    """Check if data at pos looks like [strlen:u32][ascii chars]."""
    if pos + 8 > len(data):
        return False
    slen = struct.unpack_from("<I", data, pos)[0]
    if slen == 0 or slen > 500:
        return False
    if pos + 4 + slen > len(data):
        return False
    chunk = data[pos + 4:pos + 4 + min(slen, 20)]
    printable = sum(1 for b in chunk if 32 <= b < 127)
    return printable >= len(chunk) * 0.8


def _looks_like_float(val_u32: int, val_f32: float) -> bool:
    """Heuristic: is this uint32 more likely a float?"""
    if val_u32 == 0 or val_u32 == 0xFFFFFFFF:
        return False
    if math.isnan(val_f32) or math.isinf(val_f32):
        return False
    abs_f = abs(val_f32)
    # Reasonable float range for game data
    return 0.0001 < abs_f < 100000.0 and val_u32 > 0xFF


def _parse_row_fields(data: bytes) -> list[PabgbField]:
    """Parse fields from a row's data region (after the 4-byte hash prefix)."""
    fields = []
    pos = 0
    dlen = len(data)

    while pos < dlen:
        remaining = dlen - pos

        # Try string detection first
        if remaining >= 8 and _looks_like_string(data, pos):
            slen = struct.unpack_from("<I", data, pos)[0]
            str_start = pos + 4
            str_end = str_start + slen
            # Find null terminator
            raw_str = data[str_start:str_end]
            if raw_str and raw_str[-1] == 0:
                text = raw_str[:-1].decode("utf-8", errors="replace")
            else:
                text = raw_str.decode("utf-8", errors="replace")
            # Include the null terminator byte if present
            total_size = 4 + slen
            if str_end < dlen and data[str_end] == 0:
                total_size += 1
            fields.append(PabgbField(pos, total_size, data[pos:pos + total_size], "str", text))
            pos += total_size
            continue

        # Default: read as uint32 / float32
        if remaining >= 4:
            raw = data[pos:pos + 4]
            val_u32 = struct.unpack_from("<I", raw, 0)[0]
            val_f32 = struct.unpack_from("<f", raw, 0)[0]

            if _looks_like_float(val_u32, val_f32):
                fields.append(PabgbField(pos, 4, raw, "f32", round(val_f32, 6)))
            else:
                fields.append(PabgbField(pos, 4, raw, "u32", val_u32))
            pos += 4
        else:
            # Trailing bytes
            raw = data[pos:]
            fields.append(PabgbField(pos, len(raw), raw, "blob", raw.hex()))
            pos += len(raw)

    return fields


# ---------------------------------------------------------------------------
# Simple table parsing (row-major, fixed-size rows)
# ---------------------------------------------------------------------------
def _parse_simple_table(
    row_defs: list[tuple[int, int]],
    data: bytes,
    file_name: str,
) -> PabgbTable:
    if len(row_defs) < 2:
        return PabgbTable(file_name, [], data, True, 0)

    row_size = row_defs[1][1] - row_defs[0][1]
    if row_size <= 0:
        return PabgbTable(file_name, [], data, True, 0)

    num_rows = len(data) // row_size
    rows = []
    for r in range(num_rows):
        start = r * row_size
        end = start + row_size
        if end > len(data):
            break
        raw = data[start:end]
        fields = _parse_row_fields(raw)
        name = ""
        for f in fields:
            if f.kind == "str":
                name = f.value
                break
        rows.append(PabgbRow(
            index=r,
            row_hash=r + 1,
            data_offset=start,
            data_size=row_size,
            name=name or f"row_{r}",
            fields=fields,
            raw=raw,
        ))

    return PabgbTable(file_name, rows, data, True, row_size, len(fields) if rows else 0)


# ---------------------------------------------------------------------------
# Hashed table parsing (row-per-header-entry)
# ---------------------------------------------------------------------------
def _parse_hashed_table(
    row_defs: list[tuple[int, int]],
    data: bytes,
    file_name: str,
) -> PabgbTable:
    rows = []
    for i, (row_hash, offset) in enumerate(row_defs):
        # Determine row size from offset to next row (or end of data)
        if i + 1 < len(row_defs):
            end = row_defs[i + 1][1]
        else:
            end = len(data)

        if offset >= len(data):
            continue

        size = end - offset
        if size <= 0:
            continue

        raw = data[offset:end]

        # First 4 bytes = row hash (skip it for field parsing)
        if len(raw) >= 4:
            actual_hash = struct.unpack_from("<I", raw, 0)[0]
        else:
            actual_hash = 0

        field_data = raw[4:] if len(raw) > 4 else b""
        fields = _parse_row_fields(field_data)

        # Extract name from the first string field
        name = ""
        for f in fields:
            if f.kind == "str" and len(f.value) > 2:
                name = f.value
                break

        rows.append(PabgbRow(
            index=i,
            row_hash=row_hash,
            data_offset=offset,
            data_size=size,
            name=name,
            fields=fields,
            raw=raw,
        ))

    # Detect uniform field count
    field_counts = [len(r.fields) for r in rows if r.fields]
    uniform = max(set(field_counts), key=field_counts.count) if field_counts else 0

    return PabgbTable(file_name, rows, data, False, 0, uniform)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------
def parse_header(header_data: bytes) -> tuple[list[tuple[int, int]], bool]:
    """Parse .pabgh header. Returns (list of (hash/id, offset), is_simple)."""
    if len(header_data) < 2:
        return [], False

    count = struct.unpack_from("<H", header_data, 0)[0]
    if count == 0 or count > 50000:
        return [], False

    expected_simple = 2 + count * 5
    expected_hashed = 2 + count * 8

    is_simple = False
    if expected_simple == len(header_data) and expected_hashed != len(header_data):
        is_simple = True
    elif expected_hashed == len(header_data):
        is_simple = False
    else:
        is_simple = expected_simple == len(header_data)

    defs = []
    for i in range(count):
        if is_simple:
            off = 2 + i * 5
            if off + 5 > len(header_data):
                break
            rid = header_data[off]
            doffset = struct.unpack_from("<I", header_data, off + 1)[0]
        else:
            off = 2 + i * 8
            if off + 8 > len(header_data):
                break
            rid = struct.unpack_from("<I", header_data, off)[0]
            doffset = struct.unpack_from("<I", header_data, off + 4)[0]
        defs.append((rid, doffset))

    return defs, is_simple


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_pabgb(
    data: bytes,
    header_data: bytes | None = None,
    file_name: str = "",
) -> PabgbTable:
    """Parse a .pabgb game-data table.

    Args:
        data: Raw .pabgb file content.
        header_data: Optional .pabgh header.
        file_name: Display name.

    Returns:
        PabgbTable with parsed rows and fields.
    """
    if not data:
        return PabgbTable(file_name, [], data, False)

    if not header_data:
        return PabgbTable(file_name, [], data, False)

    row_defs, is_simple = parse_header(header_data)
    if not row_defs:
        return PabgbTable(file_name, [], data, False)

    if is_simple:
        return _parse_simple_table(row_defs, data, file_name)
    else:
        return _parse_hashed_table(row_defs, data, file_name)


def serialize_pabgb(table: PabgbTable) -> bytes:
    """Serialize a PabgbTable back to .pabgb binary format.

    Reconstructs by concatenating each row's raw bytes.
    If a row was edited, its fields are re-serialized.
    """
    if not table.rows:
        return table.raw_data

    parts = []
    for row in table.rows:
        if table.is_simple:
            # Simple: just raw bytes per row
            row_bytes = b""
            for f in row.fields:
                row_bytes += _serialize_field(f)
            if table.row_size > 0:
                if len(row_bytes) < table.row_size:
                    row_bytes += b"\x00" * (table.row_size - len(row_bytes))
                elif len(row_bytes) > table.row_size:
                    row_bytes = row_bytes[:table.row_size]
            parts.append(row_bytes)
        else:
            # Hashed: [row_hash:4] + field data
            row_bytes = struct.pack("<I", row.row_hash)
            for f in row.fields:
                row_bytes += _serialize_field(f)
            parts.append(row_bytes)

    return b"".join(parts)


def _serialize_field(f: PabgbField) -> bytes:
    """Serialize one field back to bytes."""
    if f.kind == "str":
        s = str(f.value).encode("utf-8")
        # [strlen:u32] [string bytes] [null]
        return struct.pack("<I", len(s) + 1) + s + b"\x00"
    elif f.kind == "f32":
        return struct.pack("<f", float(f.value))
    elif f.kind == "u32" or f.kind == "hash":
        try:
            return struct.pack("<I", int(f.value) & 0xFFFFFFFF)
        except (ValueError, TypeError):
            return f.raw if f.raw else struct.pack("<I", 0)
    elif f.kind == "i32":
        return struct.pack("<i", int(f.value))
    elif f.kind == "blob":
        return f.raw
    else:
        return f.raw if f.raw else b"\x00" * f.size


def serialize_header(table: PabgbTable, is_simple: bool = False) -> bytes:
    """Serialize a .pabgh header from the table's row definitions."""
    count = len(table.rows)
    out = struct.pack("<H", count)

    offset = 0
    for row in table.rows:
        if is_simple:
            out += bytes([row.row_hash & 0xFF])
            out += struct.pack("<I", offset)
            offset += table.row_size
        else:
            out += struct.pack("<II", row.row_hash, offset)
            # Recompute row size
            row_bytes = 4  # hash prefix
            for f in row.fields:
                row_bytes += _serialize_field(f).__len__()
            offset += row_bytes

    return out


# ---------------------------------------------------------------------------
# Text preview
# ---------------------------------------------------------------------------
def format_table_preview(table: PabgbTable, max_rows: int = 100) -> str:
    """Format a PabgbTable as human-readable text for the preview pane."""
    lines = []
    lines.append(f"=== Game Data Table: {table.file_name} ===")
    lines.append(
        f"Rows: {len(table.rows)}  |  "
        f"Format: {'simple (fixed-size)' if table.is_simple else 'hashed (variable-length)'}  |  "
        f"Data size: {len(table.raw_data):,} bytes"
    )
    if table.is_simple and table.row_size:
        lines.append(f"Row size: {table.row_size} bytes")
    lines.append("")

    if not table.rows:
        lines.append("No rows parsed.")
        return "\n".join(lines)

    display_rows = min(len(table.rows), max_rows)

    for r_idx in range(display_rows):
        row = table.rows[r_idx]
        lines.append(f"--- Row [{r_idx}] {row.display_name}  "
                      f"(hash=0x{row.row_hash:08X}, {row.data_size} bytes, "
                      f"{len(row.fields)} fields) ---")

        for f_idx, f in enumerate(row.fields):
            val = f.display_value()
            if len(val) > 80:
                val = val[:77] + "..."
            lines.append(f"  [{f_idx:3d}] {f.kind:<4s}  {val}")

        if r_idx < display_rows - 1:
            lines.append("")

    if len(table.rows) > max_rows:
        lines.append(f"\n... ({len(table.rows) - max_rows} more rows)")

    return "\n".join(lines)
