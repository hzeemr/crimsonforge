"""Layer 4 — binary-identical TAG0 writer.

With layer 1 (section framing) + layer 2 (TYPE reflection) + layer 3
(INDX walker) we can read any HKX. This layer re-emits bytes identical
to what we read, which is the prerequisite for layer 5 (semantic
editors that change specific fields while leaving the rest alone).

Approach
--------

Rather than re-synthesise each section from the parsed high-level
dataclasses, we keep the **original buffer** and rebuild the file by
copying the bytes of each leaf section verbatim. That way every raw
byte — including padding, runtime-only MTTP scratch, and any bits our
Layer 2 flag enumeration hasn't mapped — survives untouched.

Public surface
--------------

    serialize_tag0(doc, buffer)          -> bytes
        Reassembles the document. Callers that haven't modified
        anything get a byte-identical copy of the input.

    rewrite_leaf(doc, buffer, tag, new_body)
                                         -> bytes
        Produces a new file identical to ``buffer`` except that the
        first section with ``tag`` has its body replaced by
        ``new_body``. Used by the semantic editors in Layer 5 to
        apply targeted edits without touching unrelated sections.

    rewrite_leaves(doc, buffer, edits)   -> bytes
        Batched version: ``edits`` is ``{tag: new_body}``. Applied in
        document order so the resulting offsets stay consistent.

Both rewrite variants return raw bytes that parse cleanly through
:func:`core.havok_tag0.parse_tag0` and preserve the SDK version
string.

Every operation preserves the top-level TAG0 container structure:
the root's flags stay 0x0 (container), the SDKV subsection stays
first, and the total size field is recomputed. Container sections
(TYPE, INDX) have their sizes recomputed from their children's new
sizes; leaf sections are copied verbatim or replaced wholesale.
"""

from __future__ import annotations

from typing import Mapping

from core.havok_tag0 import (
    HEADER_SIZE,
    Tag0Document,
    Tag0Section,
    encode_section_header,
)


def _emit_section(section: Tag0Section, buffer: bytes, edits: Mapping[str, bytes] | None) -> bytes:
    """Recursively rebuild one section's bytes.

    For leaves, we either pull the body from the original buffer or
    substitute ``edits[tag]`` when the caller asked us to. Containers
    recurse into their children so any edit deep in the tree bubbles
    up through ever-larger header-size updates.
    """
    if section.is_leaf:
        body = section.body_slice(buffer)
        if edits is not None and section.tag in edits:
            # Consume the edit so later sections with the same tag
            # don't all get rewritten — users asked for "the first
            # leaf with this tag", matching rewrite_leaf's semantics.
            body = edits.pop(section.tag)  # type: ignore[assignment]
        new_size = HEADER_SIZE + len(body)
        return encode_section_header(section.tag, new_size, leaf=True) + body

    # Container — rebuild children, then wrap.
    child_bytes = b"".join(
        _emit_section(child, buffer, edits) for child in section.children
    )
    new_size = HEADER_SIZE + len(child_bytes)
    return encode_section_header(section.tag, new_size, leaf=False) + child_bytes


def serialize_tag0(doc: Tag0Document, buffer: bytes) -> bytes:
    """Emit a byte-identical copy of the document.

    Useful as the baseline for tests (assert ``serialize_tag0(parse_tag0(b), b) == b``)
    and as the foundation that ``rewrite_leaf`` extends.
    """
    # Important: use a dict=None so _emit_section takes the "no edits"
    # path and doesn't get confused with the mutation trick below.
    return _emit_section(doc.root, buffer, edits=None)


def rewrite_leaf(
    doc: Tag0Document,
    buffer: bytes,
    tag: str,
    new_body: bytes,
) -> bytes:
    """Return a new TAG0 blob with the first ``tag`` leaf body replaced.

    All other sections are copied verbatim from ``buffer``. Raises
    :class:`ValueError` if no leaf with that tag exists.
    """
    edits: dict[str, bytes] = {tag: new_body}
    result = _emit_section(doc.root, buffer, edits)
    if tag in edits:
        raise ValueError(f"no leaf section named {tag!r} found in document")
    return result


def rewrite_leaves(
    doc: Tag0Document,
    buffer: bytes,
    edits: Mapping[str, bytes],
) -> bytes:
    """Apply several leaf rewrites at once.

    Each entry in ``edits`` targets the first matching leaf the walker
    encounters in document order. Unused entries are reported via
    :class:`ValueError` so silent typos in the keys don't go unnoticed.
    """
    remaining: dict[str, bytes] = dict(edits)
    result = _emit_section(doc.root, buffer, remaining)
    if remaining:
        missing = ", ".join(sorted(remaining.keys()))
        raise ValueError(
            f"rewrite_leaves did not consume every edit — unknown tags: {missing}"
        )
    return result
