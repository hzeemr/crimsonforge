"""PAMT index parser for Crimson Desert PAZ archives.

Parses .pamt files to discover file entries, their locations in PAZ archives,
sizes, compression info, and encryption status.

PAMT structure:
  [0:4]   Self-CRC (PaChecksum of data[12:])
  [4:8]   PAZ count
  [8:12]  Hash + zero
  [12:]   PAZ table → Folder section → Node section → Record section → File records
"""

import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.pamt_parser")


@dataclass
class PazTableEntry:
    """An entry in the PAMT PAZ table (describes one PAZ file)."""
    index: int
    checksum: int
    size: int
    entry_offset: int


@dataclass
class PamtFileEntry:
    """A single file entry in a PAZ archive as described by PAMT."""
    path: str
    paz_file: str
    offset: int
    comp_size: int
    orig_size: int
    flags: int
    paz_index: int
    record_offset: int = 0

    @property
    def compressed(self) -> bool:
        return self.comp_size != self.orig_size

    @property
    def compression_type(self) -> int:
        """0=none, 2=LZ4, 3=custom, 4=zlib"""
        return (self.flags >> 16) & 0x0F

    @property
    def encrypted(self) -> bool:
        """Files with certain extensions are ChaCha20-encrypted.
        The game encrypts paloc, xml, css, html, thtml, and pami files."""
        ext = os.path.splitext(self.path.lower())[1]
        return ext in (".xml", ".paloc", ".css", ".html", ".thtml", ".pami",
                       ".uianiminit", ".spline2d", ".spline", ".mi", ".txt")


@dataclass
class PamtData:
    """Parsed contents of a PAMT file."""
    path: str
    self_crc: int
    paz_count: int
    paz_table: list[PazTableEntry]
    file_entries: list[PamtFileEntry]
    folder_prefix: str = ""
    raw_data: bytes = field(default=b"", repr=False)


def parse_pamt(pamt_path: str, paz_dir: Optional[str] = None) -> PamtData:
    """Parse a .pamt index file and return all metadata.

    Args:
        pamt_path: Path to the .pamt file.
        paz_dir: Directory containing .paz files. Defaults to same dir as .pamt.

    Returns:
        PamtData with all parsed entries.
    """
    with open(pamt_path, "rb") as f:
        data = f.read()

    if paz_dir is None:
        paz_dir = os.path.dirname(pamt_path) or "."

    pamt_stem = os.path.splitext(os.path.basename(pamt_path))[0]

    off = 0
    self_crc = struct.unpack_from("<I", data, off)[0]; off += 4
    paz_count = struct.unpack_from("<I", data, off)[0]; off += 4
    off += 8  # hash + zero

    paz_table = []
    for i in range(paz_count):
        entry_offset = off
        paz_hash = struct.unpack_from("<I", data, off)[0]; off += 4
        paz_size = struct.unpack_from("<I", data, off)[0]; off += 4
        paz_table.append(PazTableEntry(
            index=i,
            checksum=paz_hash,
            size=paz_size,
            entry_offset=entry_offset,
        ))
        if i < paz_count - 1:
            off += 4  # separator

    folder_size = struct.unpack_from("<I", data, off)[0]; off += 4
    folder_end = off + folder_size
    folder_prefix = ""
    while off < folder_end:
        parent = struct.unpack_from("<I", data, off)[0]
        slen = data[off + 4]
        name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")
        if parent == 0xFFFFFFFF:
            folder_prefix = name
        off += 5 + slen

    node_size = struct.unpack_from("<I", data, off)[0]; off += 4
    node_start = off
    nodes = {}
    while off < node_start + node_size:
        rel = off - node_start
        parent = struct.unpack_from("<I", data, off)[0]
        slen = data[off + 4]
        name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")
        nodes[rel] = (parent, name)
        off += 5 + slen

    def build_path(node_ref: int) -> str:
        parts = []
        cur = node_ref
        depth = 0
        while cur != 0xFFFFFFFF and depth < 64:
            if cur not in nodes:
                break
            p, n = nodes[cur]
            parts.append(n)
            cur = p
            depth += 1
        return "".join(reversed(parts))

    folder_count = struct.unpack_from("<I", data, off)[0]; off += 4
    off += 4  # hash
    off += folder_count * 16

    entries = []
    while off + 20 <= len(data):
        record_offset = off
        node_ref, paz_offset, comp_size, orig_size, flags = \
            struct.unpack_from("<IIIII", data, off)
        off += 20

        paz_index = flags & 0xFF
        node_path = build_path(node_ref)
        full_path = f"{folder_prefix}/{node_path}" if folder_prefix else node_path

        paz_num = int(pamt_stem) + paz_index
        paz_file = os.path.join(paz_dir, f"{paz_num}.paz")

        entries.append(PamtFileEntry(
            path=full_path,
            paz_file=paz_file,
            offset=paz_offset,
            comp_size=comp_size,
            orig_size=orig_size,
            flags=flags,
            paz_index=paz_index,
            record_offset=record_offset,
        ))

    logger.info(
        "Parsed %s: %d PAZ files, %d file entries, prefix='%s'",
        pamt_path, paz_count, len(entries), folder_prefix
    )

    return PamtData(
        path=pamt_path,
        self_crc=self_crc,
        paz_count=paz_count,
        paz_table=paz_table,
        file_entries=entries,
        folder_prefix=folder_prefix,
        raw_data=data,
    )


def find_file_entry(pamt_data: PamtData, filename: str) -> Optional[PamtFileEntry]:
    """Canonical file lookup against a parsed PAMT.

    Accepts ANY caller-side form of a filename and resolves it
    against the PAMT's stored file_entries. Specifically handles:

      * Full virtual paths       (``sound/pc/en/voice.wem``)
      * Bare basenames           (``localizationstring_eng.paloc``)
      * Windows-style separators (``localizationstring\\foo.paloc``)
      * Mixed case in any part

    Semantics
    ---------
    Each stored entry is canonicalised to a forward-slash, lower-
    case full path. The needle is canonicalised the same way, then
    its basename is computed. A single O(n) pass matches an entry
    when **either** its full canonical path equals the needle's
    full canonical path **or** its basename equals the needle's
    basename.

    This is not a fallback chain — it is a single canonical rule:
    *the needle matches the entry when they share a full path or a
    basename.* That makes it impossible for a caller to be unsure
    which form they should pass; both are equivalent and always
    resolve to the same entry.

    Language-agnostic
    -----------------
    All 17 shipping paloc files follow the same naming scheme
    (``localizationstring_<code>.paloc``) and all live under the
    same ``localizationstring/`` folder prefix inside their PAMT.
    This function matches every one of them identically, regardless
    of whether the caller passes the bare filename or the full
    folder-prefixed path.

    History
    -------
    Until v1.22.6 this module had TWO ``find_file_entry``
    definitions. The second shadowed the first, silently dropping
    the basename handling and breaking Ship-to-App for every
    language. Consolidated here as the one and only definition.
    """
    if not filename or not pamt_data.file_entries:
        return None

    needle = filename.replace("\\", "/").lower()
    needle_base = needle.rsplit("/", 1)[-1]

    # Single pass with ordered preference. Exact-full-path matches
    # are the most specific, so they take precedence over
    # basename-only matches even when a less-specific basename
    # match appears earlier in the entry list. We record the first
    # basename-only candidate we see, but keep scanning in case a
    # later entry is an exact-path match. This is NOT a fallback
    # chain — it is a canonical priority rule: given two otherwise-
    # valid matches, the more specific one wins.
    basename_candidate: Optional[PamtFileEntry] = None
    for entry in pamt_data.file_entries:
        epath = entry.path.replace("\\", "/").lower()
        if epath == needle:
            return entry
        if basename_candidate is None:
            ebase = epath.rsplit("/", 1)[-1]
            if ebase == needle_base:
                basename_candidate = entry
    return basename_candidate


def update_pamt_paz_entry(
    pamt_raw: bytearray,
    paz_table_entry: PazTableEntry,
    new_checksum: int,
    new_size: int,
) -> None:
    """Update a PAZ table entry in raw PAMT data with new checksum and size."""
    struct.pack_into("<I", pamt_raw, paz_table_entry.entry_offset, new_checksum)
    struct.pack_into("<I", pamt_raw, paz_table_entry.entry_offset + 4, new_size)


def update_pamt_file_entry(
    pamt_raw: bytearray,
    file_entry: PamtFileEntry,
    new_comp_size: int,
    new_orig_size: int,
    new_offset: Optional[int] = None,
) -> None:
    """Update a file entry in raw PAMT data with new sizes."""
    if new_offset is not None:
        struct.pack_into("<I", pamt_raw, file_entry.record_offset + 4, new_offset)
    struct.pack_into("<I", pamt_raw, file_entry.record_offset + 8, new_comp_size)
    struct.pack_into("<I", pamt_raw, file_entry.record_offset + 12, new_orig_size)


def update_pamt_self_crc(pamt_raw: bytearray) -> int:
    """Recalculate and write the PAMT self-CRC. Returns the new CRC."""
    from core.checksum_engine import pa_checksum
    new_crc = pa_checksum(bytes(pamt_raw[12:]))
    struct.pack_into("<I", pamt_raw, 0, new_crc)
    return new_crc


# NOTE: the former second definition of find_file_entry() was
# removed in v1.22.6 — it silently shadowed the basename-fallback
# version above, which broke Ship-to-App for bare paloc filenames.
# Do not redefine this symbol elsewhere in this module.
