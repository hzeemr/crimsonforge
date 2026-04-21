"""Static PE analyzer for the AnimationMetaData class in CrimsonDesert.exe.

This script:

  1. Parses the PE file.
  2. Locates the ``AnimationMetaData`` RTTI type descriptor.
  3. Enumerates every CompleteObjectLocator that references it (multiple
     COLs means multi-inheritance or interface inheritance).
  4. For each COL, finds every vtable whose ``[-1]`` slot references it
     — those are concrete vtables for the class.
  5. Emits a JSON report listing all class addresses and generates a
     companion x64dbg script that sets breakpoints on every vtable
     function, lets the user trace the deserializer flow, and dumps
     register state + memory around each hit.

Usage::

    python tools/metabin_re/pe_analyzer.py \
        --exe "C:/Program Files (x86)/Steam/steamapps/common/Crimson Desert/bin64/CrimsonDesert.exe" \
        --out tools/metabin_re/output

Output files::

    output/rtti_report.json           — machine-readable class data
    output/breakpoint_script.x64dbg   — x64dbg script to set bps + log
    output/vtable_dump.txt            — human-readable vtable listing

Dependencies: stdlib only (no pefile, no lief). Runs on any Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Reuse the existing PE parser.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.rtti_scanner.scanner import PeImage, parse_pe, mangle_class_name  # noqa: E402


@dataclass
class VtableEntry:
    index: int
    rva: int
    va: int


@dataclass
class ClassVtable:
    col_rva: int           # COL that this vtable's -1 slot points to
    vtable_rva: int        # RVA of the vtable itself (first vfunc)
    vtable_va: int         # absolute VA in the running process
    entries: list[VtableEntry] = field(default_factory=list)


@dataclass
class ClassRttiInfo:
    class_name: str
    mangled_name: str
    type_descriptor_rva: int
    type_descriptor_va: int
    col_rvas: list[int] = field(default_factory=list)
    vtables: list[ClassVtable] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RTTI walking
# ---------------------------------------------------------------------------

def find_type_descriptor(img: PeImage, class_name: str,
                        namespaces=("pa",)) -> int | None:
    """Return the TypeDescriptor RVA for the given class, or None."""
    mangled = mangle_class_name(class_name, namespaces=namespaces).encode("ascii")
    for sec in img.sections:
        if not (sec.name.startswith(".rdata") or sec.name.startswith(".xdata")):
            continue
        start = sec.raw_data_offset
        end = start + sec.raw_data_size
        idx = img.data.find(mangled, start, end)
        if idx >= 0:
            # TypeDescriptor starts 0x10 bytes before the mangled string.
            string_rva = sec.virtual_address + (idx - sec.raw_data_offset)
            return string_rva - 0x10
    return None


def find_cols(img: PeImage, td_rva: int) -> list[int]:
    """Return every CompleteObjectLocator RVA that references this
    type descriptor."""
    cols = []
    for sec in img.sections:
        if not sec.name.startswith(".rdata"):
            continue
        start = sec.raw_data_offset
        end = start + sec.raw_data_size
        # Walk 4-byte-aligned positions.
        for off in range(start, end - 24, 4):
            sig = struct.unpack_from("<I", img.data, off)[0]
            if sig != 1:
                continue
            td_ref = struct.unpack_from("<I", img.data, off + 12)[0]
            if td_ref == td_rva:
                col_rva = sec.virtual_address + (off - sec.raw_data_offset)
                cols.append(col_rva)
    return cols


def find_vtables(img: PeImage, col_rva: int,
                 max_entries: int = 32) -> list[ClassVtable]:
    """For a COL at ``col_rva``, return every vtable whose ``[-1]`` slot
    holds the COL's absolute VA. Dump the first ``max_entries`` vfuncs."""
    col_va = img.image_base + col_rva
    target = struct.pack("<Q", col_va)
    vtables = []
    for sec in img.sections:
        if not sec.name.startswith(".rdata"):
            continue
        start = sec.raw_data_offset
        end = start + sec.raw_data_size
        pos = start
        while True:
            i = img.data.find(target, pos, end)
            if i < 0:
                break
            vt_rva = sec.virtual_address + (i - sec.raw_data_offset) + 8
            vt = ClassVtable(col_rva=col_rva, vtable_rva=vt_rva,
                             vtable_va=img.image_base + vt_rva)
            for k in range(max_entries):
                vt_off = i + 8 + k * 8
                if vt_off + 8 > end:
                    break
                fn_va = struct.unpack_from("<Q", img.data, vt_off)[0]
                if fn_va == 0 or fn_va < img.image_base:
                    break
                fn_rva = fn_va - img.image_base
                # Sanity: function VA must land inside a section. Denuvo
                # renames sections so we can't rely on ".text" being
                # present — just check any section contains the RVA.
                in_image = any(
                    (s.virtual_address <= fn_rva < s.virtual_address + s.virtual_size)
                    for s in img.sections
                )
                if not in_image:
                    break
                vt.entries.append(VtableEntry(index=k, rva=fn_rva, va=fn_va))
            vtables.append(vt)
            pos = i + 8
    return vtables


def analyze(exe_path: str, class_name: str = "AnimationMetaData") -> ClassRttiInfo:
    data = Path(exe_path).read_bytes()
    img = parse_pe(data)
    td_rva = find_type_descriptor(img, class_name)
    if td_rva is None:
        raise RuntimeError(f"RTTI TypeDescriptor for {class_name} not found")
    info = ClassRttiInfo(
        class_name=class_name,
        mangled_name=mangle_class_name(class_name),
        type_descriptor_rva=td_rva,
        type_descriptor_va=img.image_base + td_rva,
    )
    info.col_rvas = find_cols(img, td_rva)
    for col_rva in info.col_rvas:
        info.vtables.extend(find_vtables(img, col_rva))
    return info


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------

def emit_json_report(info: ClassRttiInfo, path: str) -> None:
    report = {
        "class_name": info.class_name,
        "mangled_name": info.mangled_name,
        "type_descriptor": {
            "rva": f"0x{info.type_descriptor_rva:X}",
            "va": f"0x{info.type_descriptor_va:X}",
        },
        "col_rvas": [f"0x{r:X}" for r in info.col_rvas],
        "vtables": [
            {
                "col_rva": f"0x{vt.col_rva:X}",
                "vtable_rva": f"0x{vt.vtable_rva:X}",
                "vtable_va": f"0x{vt.vtable_va:X}",
                "entries": [
                    {
                        "index": e.index,
                        "rva": f"0x{e.rva:X}",
                        "va": f"0x{e.va:X}",
                    }
                    for e in vt.entries
                ],
            }
            for vt in info.vtables
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def emit_vtable_dump(info: ClassRttiInfo, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# AnimationMetaData class RTTI dump\n\n")
        f.write(f"TypeDescriptor:\n")
        f.write(f"  RVA  : 0x{info.type_descriptor_rva:X}\n")
        f.write(f"  VA   : 0x{info.type_descriptor_va:X}\n\n")
        for i, col_rva in enumerate(info.col_rvas):
            f.write(f"COL[{i}]: RVA 0x{col_rva:X}\n")
        f.write("\n")
        for i, vt in enumerate(info.vtables):
            f.write(f"\n===== Vtable {i} =====\n")
            f.write(f"  COL     : RVA 0x{vt.col_rva:X}\n")
            f.write(f"  vtable  : RVA 0x{vt.vtable_rva:X}  VA 0x{vt.vtable_va:X}\n")
            f.write(f"  entries : {len(vt.entries)}\n")
            for e in vt.entries:
                f.write(f"    vfunc[{e.index:2d}] = RVA 0x{e.rva:X}  VA 0x{e.va:X}\n")


def emit_x64dbg_script(info: ClassRttiInfo, path: str) -> None:
    """Generate an x64dbg command script that sets breakpoints on every
    AnimationMetaData vfunc. When a bp hits, x64dbg logs the first few
    registers and the bytes at rcx (which is the ``this`` pointer in x64
    Windows calling convention) — giving the user an instant view into
    the class instance being deserialized."""
    lines: list[str] = []
    lines.append("// AnimationMetaData breakpoint script")
    lines.append("// Run with: x64dbg → Plugins → Script → Load")
    lines.append("// Or paste in Commands tab")
    lines.append("")
    lines.append("log \"=== AnimationMetaData RTTI ===\"")
    lines.append(f"log \"TypeDescriptor: 0x{info.type_descriptor_va:X}\"")
    for col_rva in info.col_rvas:
        lines.append(f"log \"COL: 0x{info.type_descriptor_va - info.type_descriptor_rva + col_rva:X}\"")
    lines.append("")
    for vi, vt in enumerate(info.vtables):
        lines.append(f"// --- Vtable {vi} @ VA 0x{vt.vtable_va:X} ---")
        # Set bp on first 6 vfuncs only (rest is usually noise).
        for e in vt.entries[:6]:
            lines.append(f"bp 0x{e.va:X}    // vtable[{vi}].vfunc[{e.index}]")
            # Log-only handler that records rcx (this ptr) + first 0x80 bytes.
            lines.append(f"SetBreakpointCondition 0x{e.va:X}, \"0\"")
            lines.append(
                f"SetBreakpointLog 0x{e.va:X}, "
                f'"vt{vi}.vf{e.index} this=[rcx] bytes={{bytes(rcx, 0x20)}}"'
            )
        lines.append("")
    lines.append("log \"All breakpoints set. Press F9 to resume. Attach PAA trigger\"")
    lines.append("log \"in-game (e.g., play an animation) — watch the log.\"")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="AnimationMetaData class RTTI analyzer for CrimsonDesert.exe"
    )
    ap.add_argument(
        "--exe", required=True,
        help="Path to CrimsonDesert.exe",
    )
    ap.add_argument(
        "--class-name", default="AnimationMetaData",
        help="Class name to scan for (default: AnimationMetaData)",
    )
    ap.add_argument(
        "--out", default="tools/metabin_re/output",
        help="Output directory for generated artifacts",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.exe):
        print(f"error: {args.exe} not found", file=sys.stderr)
        return 1

    print(f"Analyzing {args.exe} for class {args.class_name}...")
    info = analyze(args.exe, args.class_name)
    print(f"  TypeDescriptor VA : 0x{info.type_descriptor_va:X}")
    print(f"  COLs              : {len(info.col_rvas)}")
    print(f"  Vtables           : {len(info.vtables)}")
    for i, vt in enumerate(info.vtables):
        print(f"    vtable {i} @ 0x{vt.vtable_va:X} ({len(vt.entries)} entries)")

    os.makedirs(args.out, exist_ok=True)
    emit_json_report(info, os.path.join(args.out, "rtti_report.json"))
    emit_vtable_dump(info, os.path.join(args.out, "vtable_dump.txt"))
    emit_x64dbg_script(info, os.path.join(args.out, "breakpoint_script.x64dbg"))

    print(f"\nGenerated files in {args.out}/:")
    print("  rtti_report.json           — machine-readable class data")
    print("  vtable_dump.txt            — human-readable vtable listing")
    print("  breakpoint_script.x64dbg   — x64dbg breakpoint commands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
