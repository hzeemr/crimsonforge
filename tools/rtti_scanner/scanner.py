"""Static RTTI scanner for x64 PE binaries.

MSVC emits a pair of structures per polymorphic class:

  RTTI TypeDescriptor    — contains the mangled class name string
                           (.?AV<class>@<namespace>@@).
  CompleteObjectLocator  — points at the TypeDescriptor and carries a
                           "signature" field which is always 1 for x64
                           (as opposed to 0 for x86). This discriminator
                           is how we reliably find COLs in a large exe.

The vtable itself lives elsewhere in .rdata and its first slot (index -1
relative to the vtable pointer the compiler stores in objects) points at
the COL. So the scan is:

  1. Walk .rdata for ASCII needles of the form ".?AV<class>@...@@".
  2. For each match, the byte before the needle is 0x00 (name is
     null-terminated from the prior field). Back up 0x10 bytes — that's
     the TypeDescriptor start — and record its RVA.
  3. Walk .rdata for 32-byte CompleteObjectLocator candidates whose
     pTypeDescriptor RVA matches any known TypeDescriptor from step 2.
  4. Walk the entire image for a qword pointer equal to the COL's
     absolute virtual address. Each such pointer is a vtable[-1] slot;
     the vtable itself starts at (pointer + 8).

All offsets reported are RVAs (file-image virtual addresses before
applying the image base). CLAUDE.md documents the current canonical
class names and expected RVAs.

Zero dependencies beyond the stdlib so this tool runs on any Python 3.11+
install — no pefile, no lief, no tk/Qt.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# PE parsing — only what we need to locate sections and read bytes by RVA.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PeSection:
    """One PE section, enough to map RVAs back to file offsets."""
    name: str
    virtual_address: int
    virtual_size: int
    raw_data_offset: int
    raw_data_size: int

    def contains_rva(self, rva: int) -> bool:
        return self.virtual_address <= rva < self.virtual_address + self.virtual_size

    def rva_to_file_offset(self, rva: int) -> int:
        return self.raw_data_offset + (rva - self.virtual_address)


@dataclass(slots=True)
class PeImage:
    """Parsed PE file. Keep the raw bytes around so callers can read by RVA."""
    data: bytes
    image_base: int
    sections: list[PeSection]

    def find_section(self, rva: int) -> PeSection | None:
        for sec in self.sections:
            if sec.contains_rva(rva):
                return sec
        return None

    def read(self, rva: int, length: int) -> bytes:
        sec = self.find_section(rva)
        if sec is None:
            return b""
        off = sec.rva_to_file_offset(rva)
        if off < 0 or off + length > sec.raw_data_offset + sec.raw_data_size:
            return b""
        return self.data[off:off + length]

    def section_bytes(self, name: str) -> tuple[PeSection, bytes] | None:
        for sec in self.sections:
            if sec.name == name:
                return sec, self.data[sec.raw_data_offset:sec.raw_data_offset + sec.raw_data_size]
        return None


def parse_pe(data: bytes) -> PeImage:
    """Parse a minimal PE structure: sections + image base.

    We skip lots of PE details we don't need (data directories, imports,
    relocations). The goal is just to map RVAs to file bytes.
    """
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("not a PE image (missing MZ signature)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew <= 0 or e_lfanew + 24 > len(data):
        raise ValueError("PE header offset out of range")
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise ValueError("not a PE image (missing PE signature)")

    # COFF header at e_lfanew + 4
    coff_off = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff_off + 2)[0]
    opt_header_size = struct.unpack_from("<H", data, coff_off + 16)[0]

    opt_header_off = coff_off + 20
    magic = struct.unpack_from("<H", data, opt_header_off)[0]
    if magic == 0x20B:
        # PE32+ (x64) — image base is at opt_header + 24, a qword.
        image_base = struct.unpack_from("<Q", data, opt_header_off + 24)[0]
    elif magic == 0x10B:
        # PE32 (x86) — image base is at opt_header + 28, a dword.
        image_base = struct.unpack_from("<I", data, opt_header_off + 28)[0]
    else:
        raise ValueError(f"unknown optional-header magic {magic:#x}")

    sections_off = opt_header_off + opt_header_size
    sections: list[PeSection] = []
    for i in range(num_sections):
        sec_off = sections_off + i * 40
        if sec_off + 40 > len(data):
            break
        name = data[sec_off:sec_off + 8].rstrip(b"\x00").decode("ascii", errors="replace")
        virtual_size = struct.unpack_from("<I", data, sec_off + 8)[0]
        virtual_address = struct.unpack_from("<I", data, sec_off + 12)[0]
        raw_size = struct.unpack_from("<I", data, sec_off + 16)[0]
        raw_offset = struct.unpack_from("<I", data, sec_off + 20)[0]
        sections.append(
            PeSection(
                name=name,
                virtual_address=virtual_address,
                virtual_size=virtual_size,
                raw_data_offset=raw_offset,
                raw_data_size=raw_size,
            )
        )

    return PeImage(data=data, image_base=image_base, sections=sections)


# ---------------------------------------------------------------------------
# Class name mangling
# ---------------------------------------------------------------------------

def mangle_class_name(unmangled: str, *, namespaces: Iterable[str] = ("pa",)) -> str:
    """Return the MSVC RTTI mangled form for a simple class name.

    Example::

        >>> mangle_class_name("UIGamePlayControl_Root_ChangeCharacterNotice")
        '.?AVUIGamePlayControl_Root_ChangeCharacterNotice@pa@@'

    The game's classes all live under the ``pa::`` namespace per
    CLAUDE.md's conventions, so that's the default. Pass ``namespaces=()``
    for classes in the global namespace.
    """
    parts = [f".?AV{unmangled}"]
    parts.extend(list(namespaces))
    return "@".join(parts) + "@@"


# ---------------------------------------------------------------------------
# Scanner core
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ClassMatch:
    """Everything we discovered about one RTTI-exposed class."""
    class_name: str
    mangled_name: str
    type_descriptor_rva: int
    col_rvas: list[int] = field(default_factory=list)
    # Vtable addresses discovered by scanning for references to each COL.
    # Stored as a flat list of RVAs in increasing order.
    vtable_rvas: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "class_name": self.class_name,
            "mangled_name": self.mangled_name,
            "type_descriptor_rva": f"0x{self.type_descriptor_rva:X}",
            "col_rvas": [f"0x{r:X}" for r in self.col_rvas],
            "vtable_rvas": [f"0x{r:X}" for r in self.vtable_rvas],
        }


@dataclass(slots=True)
class ScanResult:
    """Full scan report."""
    image_base: int
    matches: list[ClassMatch]
    missing: list[str]  # class names the caller asked about but we never found

    def to_json(self, indent: int = 2) -> str:
        payload = {
            "image_base": f"0x{self.image_base:X}",
            "matches": [m.to_dict() for m in self.matches],
            "missing": list(self.missing),
        }
        return json.dumps(payload, indent=indent)


def _file_offset_to_rva(image: PeImage, file_offset: int) -> int | None:
    for sec in image.sections:
        if sec.raw_data_offset <= file_offset < sec.raw_data_offset + sec.raw_data_size:
            return sec.virtual_address + (file_offset - sec.raw_data_offset)
    return None


def _find_type_descriptors(
    image: PeImage,
    wanted: dict[str, str],
) -> dict[str, int]:
    """Locate each requested class's TypeDescriptor RVA.

    ``wanted`` is a map ``class_name -> mangled_name``. The returned dict
    maps ``class_name -> type_descriptor_rva`` for every class we
    actually found. Missing classes are left out so the caller can
    surface them to the user.

    Implementation: scan .rdata bytes for each needle. When a name is
    found, the MSVC TypeDescriptor layout puts a pVFTable qword (+ spare
    qword) in front of the ASCII name, so the TypeDescriptor base sits
    16 bytes before the name.
    """
    rdata = image.section_bytes(".rdata")
    if rdata is None:
        raise ValueError("PE image has no .rdata section — nothing to scan")
    sec, blob = rdata

    found: dict[str, int] = {}
    for class_name, mangled in wanted.items():
        needle = mangled.encode("ascii") + b"\x00"
        pos = blob.find(needle)
        if pos < 0:
            continue
        # The TypeDescriptor starts 16 bytes before the ASCII name
        # (qword pVFTable + qword spare). Need to back up at least that
        # many bytes inside the .rdata section for the match to be valid.
        if pos < 16:
            continue
        td_file_off = sec.raw_data_offset + pos - 16
        td_rva = _file_offset_to_rva(image, td_file_off)
        if td_rva is None:
            continue
        found[class_name] = td_rva
    return found


def _scan_col_candidates(
    image: PeImage,
    td_rva_by_class: dict[str, int],
) -> dict[str, list[int]]:
    """Walk .rdata for CompleteObjectLocator candidates.

    For x64, a valid COL structure is 28 bytes with layout:

        uint32 signature   == 1
        uint32 offset
        uint32 cdOffset
        uint32 pTypeDescriptor  (RVA)
        uint32 pClassHierarchy  (RVA)
        uint32 pSelf            (RVA of this COL) — x64-specific

    We scan on 4-byte boundaries, verify the signature == 1 invariant,
    and check pTypeDescriptor against the set of TDs we care about. The
    pSelf field lets us confirm the candidate is genuinely a COL and
    reject false positives.
    """
    rdata = image.section_bytes(".rdata")
    if rdata is None:
        return {}
    sec, blob = rdata

    wanted_tds = {rva: cls for cls, rva in td_rva_by_class.items()}
    results: dict[str, list[int]] = {cls: [] for cls in td_rva_by_class}

    size = len(blob)
    for i in range(0, size - 24, 4):
        signature = struct.unpack_from("<I", blob, i)[0]
        if signature != 1:
            continue
        td_rva = struct.unpack_from("<I", blob, i + 12)[0]
        if td_rva not in wanted_tds:
            continue
        self_rva = struct.unpack_from("<I", blob, i + 20)[0]
        col_rva = sec.virtual_address + i
        if self_rva != col_rva:
            continue
        results[wanted_tds[td_rva]].append(col_rva)
    return results


def _scan_vtables_for_cols(
    image: PeImage,
    col_rvas_by_class: dict[str, list[int]],
) -> dict[str, list[int]]:
    """Find every vtable referencing each COL.

    Vtable[-1] is a qword pointing at the COL's absolute address. We
    walk .rdata on 8-byte boundaries and compare each qword against the
    set of (image_base + col_rva) targets we care about. Every hit
    represents a vtable reference; the vtable itself starts at the qword
    immediately following (hit_address + 8).
    """
    rdata = image.section_bytes(".rdata")
    if rdata is None:
        return {}
    sec, blob = rdata

    all_targets: dict[int, str] = {}
    for cls, rvas in col_rvas_by_class.items():
        for rva in rvas:
            all_targets[image.image_base + rva] = cls

    results: dict[str, list[int]] = {cls: [] for cls in col_rvas_by_class}
    size = len(blob)
    for i in range(0, size - 8, 8):
        val = struct.unpack_from("<Q", blob, i)[0]
        cls = all_targets.get(val)
        if cls is None:
            continue
        # Vtable starts at the qword AFTER this reference.
        vtable_rva = sec.virtual_address + i + 8
        results[cls].append(vtable_rva)
    return results


def scan_pe_for_classes(
    pe_path: Path | str,
    class_names: Iterable[str],
    *,
    namespaces: Iterable[str] = ("pa",),
) -> ScanResult:
    """Scan ``pe_path`` for every class in ``class_names``.

    Returns a :class:`ScanResult` with one :class:`ClassMatch` per class
    that was located (plus a ``missing`` list for the ones the scanner
    could not find — usually because the class is compiled away in the
    shipping build or its namespace differs from the default).
    """
    data = Path(pe_path).read_bytes()
    image = parse_pe(data)

    wanted_mangled = {
        cls: mangle_class_name(cls, namespaces=namespaces)
        for cls in class_names
    }

    td_by_class = _find_type_descriptors(image, wanted_mangled)
    col_by_class = _scan_col_candidates(image, td_by_class)
    vt_by_class = _scan_vtables_for_cols(image, col_by_class)

    matches: list[ClassMatch] = []
    missing: list[str] = []
    for cls, mangled in wanted_mangled.items():
        td_rva = td_by_class.get(cls)
        if td_rva is None:
            missing.append(cls)
            continue
        matches.append(
            ClassMatch(
                class_name=cls,
                mangled_name=mangled,
                type_descriptor_rva=td_rva,
                col_rvas=sorted(col_by_class.get(cls, [])),
                vtable_rvas=sorted(vt_by_class.get(cls, [])),
            )
        )

    return ScanResult(image_base=image.image_base, matches=matches, missing=missing)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

DEFAULT_CLASS_NAMES = (
    # Character unlock: the three classes CLAUDE.md documents.
    "UIGamePlayControl_Root_ChangeCharacterNotice",
    "TrocTrUpdateForbiddenCharacterListAck",
    "TrocTrChangePlayerbleCharacterFailAck",
    "TrocTrChangePlayerbleCharacterAck",
)


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Locate RTTI-exposed classes inside a PE image (e.g. CrimsonDesert.exe).",
    )
    parser.add_argument("pe", help="Path to the PE file to scan")
    parser.add_argument(
        "--class",
        dest="classes",
        action="append",
        default=None,
        help="Class name to locate (can be passed multiple times; defaults to the "
             "canonical Crimson Desert character-unlock set).",
    )
    parser.add_argument(
        "--namespace",
        action="append",
        dest="namespaces",
        default=None,
        help="Namespace the classes live in (default: pa). Pass multiple times "
             "for nested namespaces.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    classes = args.classes or list(DEFAULT_CLASS_NAMES)
    namespaces = tuple(args.namespaces) if args.namespaces else ("pa",)

    result = scan_pe_for_classes(args.pe, classes, namespaces=namespaces)

    if args.json:
        print(result.to_json())
        return 0

    print(f"PE: {args.pe}")
    print(f"Image base: 0x{result.image_base:X}")
    print()
    for match in result.matches:
        print(f"  {match.class_name}")
        print(f"    type descriptor RVA : 0x{match.type_descriptor_rva:X}")
        for rva in match.col_rvas:
            print(f"    COL RVA             : 0x{rva:X}")
        for rva in match.vtable_rvas:
            print(f"    vtable RVA          : 0x{rva:X}")
        print()
    if result.missing:
        print("Classes not found (check namespace / compilation):")
        for cls in result.missing:
            print(f"  - {cls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
