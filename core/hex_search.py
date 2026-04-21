"""Cross-file hex / byte-pattern search across the game archive.

Phorge exposes a cross-file hex search that the CrimsonForge community has
been asking us to match. This module provides the core engine:

  * ``parse_hex_pattern``  — turn a user-entered hex string into a (needle, mask)
                             tuple that supports `??` wildcards per byte.
  * ``search_buffer``      — one-buffer scanner that returns every match offset.
  * ``search_vfs``         — walk every PAMT entry in one or more package groups,
                             decompress the payload, and stream (path, offset)
                             tuples to the caller.

Design notes
------------

* **Wildcards are byte-granular.** `AA ?? CC` matches any single byte between
  `AA` and `CC`. We deliberately don't add nibble-level wildcards (`A?`);
  nobody is asking for that yet and it doubles the matcher cost.

* **ASCII fallback.** If the user types a quoted ASCII literal (``"iteminfo"``)
  we fall back to byte-for-byte matching. Phorge supports the same shorthand.

* **Streaming.** Big archives hold tens of thousands of files. ``search_vfs``
  is a generator so the caller can stop early (e.g. on a UI cancel) and
  memory stays flat.

* **Compressed-data fidelity.** We scan the *decompressed* payload, not the
  raw PAZ bytes. That matches how every other CrimsonForge feature reads
  archive data and keeps offsets meaningful to the user.

The module has zero Qt dependencies so it can be unit-tested and reused from
CLI tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from utils.logger import get_logger

logger = get_logger("core.hex_search")


# ---------------------------------------------------------------------------
# Pattern parsing
# ---------------------------------------------------------------------------

_HEX_BYTE_RE = re.compile(r"^[0-9A-Fa-f]{2}$")
_ASCII_LITERAL_RE = re.compile(r'^"(.*)"$', re.DOTALL)


class HexPatternError(ValueError):
    """Raised when ``parse_hex_pattern`` receives syntactically invalid input."""


@dataclass(frozen=True, slots=True)
class HexPattern:
    """A parsed search pattern.

    ``needle`` and ``mask`` are always the same length. A mask byte of ``0xFF``
    means "must match exactly" and ``0x00`` means "wildcard". Any other mask
    byte would imply nibble-level wildcards which we intentionally don't
    support (see module docstring).
    """

    needle: bytes
    mask: bytes
    source: str  # original user input, kept for error messages / logs

    @property
    def length(self) -> int:
        return len(self.needle)

    @property
    def has_wildcards(self) -> bool:
        return any(m != 0xFF for m in self.mask)


def parse_hex_pattern(text: str) -> HexPattern:
    """Parse a user-entered pattern into a :class:`HexPattern`.

    Accepted shapes::

        "AA BB CC"         -> three literal bytes
        "AABBCC"           -> same, whitespace is optional
        "AA ?? CC"         -> wildcard middle byte (matches any byte)
        '"iteminfo"'       -> ASCII literal, matched byte-for-byte
        "41 42 ??"         -> mix of hex and wildcard

    Raises :class:`HexPatternError` for empty input, odd-nibble hex tokens,
    or tokens that are neither hex nor wildcards.
    """
    if text is None:
        raise HexPatternError("hex pattern is empty")

    source = text.strip()
    if not source:
        raise HexPatternError("hex pattern is empty")

    # ASCII literal — must be fully quoted to avoid ambiguity with hex tokens.
    literal_match = _ASCII_LITERAL_RE.match(source)
    if literal_match is not None:
        payload = literal_match.group(1)
        if not payload:
            raise HexPatternError("ASCII literal is empty")
        try:
            encoded = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HexPatternError(f"ASCII literal could not be encoded: {exc}") from exc
        return HexPattern(
            needle=encoded,
            mask=b"\xFF" * len(encoded),
            source=source,
        )

    # Hex tokens — one byte per token, ?? is a wildcard. A token may also be a
    # run of unspaced hex digits (even length), which is the form users copy
    # out of hex editors / debugger dumps. Unspaced runs cannot contain
    # wildcards — if you need wildcards, you need to space your bytes.
    tokens = re.split(r"\s+", source)
    needle = bytearray()
    mask = bytearray()
    for tok in tokens:
        if not tok:
            continue
        if tok in ("??", "**"):
            needle.append(0x00)
            mask.append(0x00)
            continue
        # Strip "0x" prefix — common in IDA / x64dbg paste.
        clean = tok[2:] if tok.startswith(("0x", "0X")) else tok

        if _HEX_BYTE_RE.match(clean):
            needle.append(int(clean, 16))
            mask.append(0xFF)
            continue

        # Multi-byte unspaced run: "AABBCC" -> [AA, BB, CC]. Must be even length
        # and fully hex; otherwise fall through to the error case so typos like
        # "AABZ" still get flagged instead of silently truncated.
        if len(clean) >= 2 and len(clean) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", clean):
            for byte_idx in range(0, len(clean), 2):
                needle.append(int(clean[byte_idx:byte_idx + 2], 16))
                mask.append(0xFF)
            continue

        raise HexPatternError(f"invalid token {tok!r}; expected 2 hex digits or '??'")

    if not needle:
        raise HexPatternError("hex pattern contained no tokens")

    return HexPattern(needle=bytes(needle), mask=bytes(mask), source=source)


# ---------------------------------------------------------------------------
# Single-buffer matcher
# ---------------------------------------------------------------------------

def search_buffer(buffer: bytes, pattern: HexPattern, max_matches: int | None = None) -> list[int]:
    """Return every offset in ``buffer`` where ``pattern`` matches.

    The matcher is linear in the length of the buffer for patterns without
    wildcards (delegates to ``bytes.find``), and still O(n*m) in the worst
    case with wildcards — acceptable given that patterns are typically < 32
    bytes and archives are scanned once per file.
    """
    if not buffer:
        return []
    if pattern.length == 0:
        return []

    offsets: list[int] = []

    if not pattern.has_wildcards:
        start = 0
        while True:
            pos = buffer.find(pattern.needle, start)
            if pos < 0:
                break
            offsets.append(pos)
            if max_matches is not None and len(offsets) >= max_matches:
                break
            start = pos + 1
        return offsets

    # Wildcard path — manual scan with the mask applied.
    needle = pattern.needle
    mask = pattern.mask
    plen = pattern.length
    blen = len(buffer)
    limit = blen - plen
    if limit < 0:
        return []

    for i in range(limit + 1):
        ok = True
        for j in range(plen):
            if mask[j] and buffer[i + j] != needle[j]:
                ok = False
                break
        if ok:
            offsets.append(i)
            if max_matches is not None and len(offsets) >= max_matches:
                break
    return offsets


# ---------------------------------------------------------------------------
# VFS-wide search
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HexMatch:
    """One hit for a cross-file search."""

    package_group: str
    path: str
    offset: int
    context_before: bytes
    match_bytes: bytes
    context_after: bytes

    def format_line(self) -> str:
        """Compact single-line rendering for logs / CLI output."""
        hex_match = self.match_bytes.hex()
        return f"{self.package_group}:{self.path}  @0x{self.offset:X}  {hex_match}"


def _extract_context(data: bytes, offset: int, length: int, context_bytes: int) -> tuple[bytes, bytes, bytes]:
    before = data[max(0, offset - context_bytes):offset]
    match = data[offset:offset + length]
    after = data[offset + length:offset + length + context_bytes]
    return before, match, after


def search_vfs(
    vfs,  # VfsManager — not imported to keep this module dependency-light
    pattern: HexPattern,
    package_groups: Iterable[str] | None = None,
    path_filter: Callable[[str], bool] | None = None,
    max_matches_per_file: int | None = 64,
    max_total_matches: int | None = None,
    context_bytes: int = 8,
    progress: Callable[[str, int, int], None] | None = None,
) -> Iterator[HexMatch]:
    """Yield a :class:`HexMatch` for every occurrence of ``pattern`` in the archives.

    Parameters
    ----------
    vfs:
        A ``core.vfs_manager.VfsManager`` instance. Passed in untyped to
        avoid a hard import here — keeps this module testable without Qt
        and without touching the filesystem.
    package_groups:
        Iterable of group directory names (``"0008"``, ``"0009"``, …). When
        ``None`` every group the VFS knows about is scanned.
    path_filter:
        Optional predicate that receives each entry's path (slash-normalised,
        lower case). Return ``False`` to skip the file cheaply — this runs
        before we spend CPU on decompression.
    max_matches_per_file / max_total_matches:
        Safety valves. 64 per file is enough to spot hot patterns without
        drowning the caller when a needle happens to be common.
    context_bytes:
        Bytes captured on either side of each match. 8 is enough context
        for most debugging; pass 0 to save memory on very large scans.
    progress:
        Optional ``(current_path, files_scanned, files_total)`` callback so
        the UI can show a busy indicator.
    """
    if pattern.length == 0:
        return

    # Defer the VFS import — the module is used from tests that build a fake.
    group_list = list(package_groups) if package_groups is not None else _list_all_groups(vfs)
    total_matches = 0

    for group in group_list:
        try:
            pamt = vfs.load_pamt(group)
        except Exception as exc:
            logger.warning("Could not load PAMT for group %s: %s", group, exc)
            continue

        entries = list(pamt.file_entries)
        entries_total = len(entries)
        for i, entry in enumerate(entries):
            path_lower = entry.path.replace("\\", "/").lower()
            if path_filter is not None and not path_filter(path_lower):
                continue

            if progress is not None:
                progress(entry.path, i, entries_total)

            try:
                data = vfs.read_entry_data(entry)
            except Exception as exc:
                logger.warning("Could not read %s: %s", entry.path, exc)
                continue

            offsets = search_buffer(data, pattern, max_matches=max_matches_per_file)
            for off in offsets:
                before, match_bytes, after = _extract_context(data, off, pattern.length, context_bytes)
                yield HexMatch(
                    package_group=group,
                    path=entry.path.replace("\\", "/"),
                    offset=off,
                    context_before=before,
                    match_bytes=match_bytes,
                    context_after=after,
                )
                total_matches += 1
                if max_total_matches is not None and total_matches >= max_total_matches:
                    return


def _list_all_groups(vfs) -> list[str]:
    """Best-effort discovery of package group names.

    VfsManager doesn't expose a "list groups" method today, so we look at
    the ``packages_path`` directory. This keeps the module usable even
    when the VfsManager API evolves.
    """
    import os
    try:
        packages_path = vfs.packages_path
    except AttributeError:
        return []
    if not os.path.isdir(packages_path):
        return []

    groups: list[str] = []
    for name in sorted(os.listdir(packages_path)):
        group_dir = os.path.join(packages_path, name)
        if not os.path.isdir(group_dir):
            continue
        # A valid package group always contains a 0.pamt index.
        if not os.path.isfile(os.path.join(group_dir, "0.pamt")):
            continue
        groups.append(name)
    return groups
