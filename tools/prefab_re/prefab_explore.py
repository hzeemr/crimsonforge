"""Walk a Crimson Desert .prefab byte-by-byte, v2 — correct structure.

Discovered layout after careful hex analysis:

  Header (0x00..0x11):
    0x00..0x05  magic      ff ff 04 00 00 00
    0x06..0x09  hash1      uint32 LE (per-file unique)
    0x0A..0x0D  hash2      uint32 LE (per-file unique)
    0x0E..0x11  uint32 = 15  (either version or root component count)

  After header, components begin. Each component:
    uint16  component_index?   (4 at 0x12 in first sample)
    uint32  name_len
    bytes   component_type_name ('SceneObject', 11 bytes)
    uint16  property_count      (13 in SceneObject sample)

  Then for each property:
    uint32  name_len
    bytes   property_name (e.g. '_attachedSocketName')
    uint32  type_len
    bytes   property_type (e.g. 'IndexedStringA', 'bool', 'Transform')
    bytes   type-specific trailer (variable size per type)

So the exploration needs to track what each TYPE's trailer looks like.
"""

import sys
import struct


def u16(d, o): return struct.unpack_from("<H", d, o)[0] if o + 2 <= len(d) else 0
def u32(d, o): return struct.unpack_from("<I", d, o)[0] if o + 4 <= len(d) else 0
def f32(d, o): return struct.unpack_from("<f", d, o)[0] if o + 4 <= len(d) else 0.0


def read_lenstr(data, off):
    if off + 4 > len(data):
        return None, off
    n = u32(data, off)
    if n > 0x10000 or off + 4 + n > len(data):
        return None, off
    try:
        s = data[off + 4: off + 4 + n].decode("utf-8", errors="replace")
    except Exception:
        s = data[off + 4: off + 4 + n].hex()
    return s, off + 4 + n


def main():
    if len(sys.argv) < 2:
        path = r"C:\Users\hzeem\AppData\Local\Temp\crimsonforge_preview_d8exzp3t\cd_phm_00_cloak_00_0208_t.prefab"
    else:
        path = sys.argv[1]
    data = open(path, "rb").read()
    print(f"FILE: {path}  SIZE: {len(data)}\n")

    # Header
    assert data[:6] == b"\xff\xff\x04\x00\x00\x00", "bad magic"
    h1 = u32(data, 0x06)
    h2 = u32(data, 0x0A)
    version_or_count = u32(data, 0x0E)
    print(f"hash1={h1:08x}  hash2={h2:08x}  marker@0x0E={version_or_count}")

    off = 0x12
    # Try to walk components — component 0 starts with a uint16 and
    # a length-prefixed type name.
    component_idx = u16(data, off)
    off += 2
    print(f"\n@0x{off-2:04x}: component_idx uint16 = {component_idx}")
    comp_name, off = read_lenstr(data, off)
    print(f"@*: component_type = {comp_name!r}   new off 0x{off:04x}")

    # Next 2 bytes: property count uint16
    prop_count = u16(data, off)
    off += 2
    print(f"@0x{off-2:04x}: property_count uint16 = {prop_count}")

    # Walk properties
    print("\n--- PROPERTIES ---")
    for p in range(min(prop_count, 50)):
        p_start = off
        name, off = read_lenstr(data, off)
        if name is None:
            print(f"  FAILED at 0x{p_start:04x}")
            break
        type_name, off = read_lenstr(data, off)
        if type_name is None:
            print(f"  FAILED reading type at 0x{off:04x}")
            break
        # Type-specific trailer. Let's show the next 20 bytes so we
        # can pattern-match.
        trailer_hex = data[off:off + 20].hex(" ")
        trailer_printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data[off:off + 20])
        print(f"  [{p:2d}] @0x{p_start:04x}  name={name:30s} type={type_name:25s}")
        print(f"        trailer: {trailer_hex}")
        print(f"                 {trailer_printable}")
        # Try to advance based on type
        if type_name == "bool":
            # 00 00 01 00 00 00 00 00 (8 bytes: seen consistently)
            off += 8
        elif type_name == "IndexedStringA":
            # 01 00 01 00 XX XX XX XX XX XX XX XX (12 bytes)
            off += 12
        elif type_name == "Transform":
            # 00 00 28 00 00 00 00 00 — 40 bytes might follow (look at len)
            # Actually "28 00" = 40 = size indicator. Let's read it.
            size_hint = u16(data, off + 2)
            print(f"        Transform size_hint@+2 = {size_hint}")
            # trailer is 8 bytes + 40 bytes of matrix data? But these tiny
            # samples show 00 00 28 00 00 00 00 00 and then immediately
            # next property. So maybe 8 bytes total.
            off += 8
        elif type_name == "TiledTransform":
            # 00 00 2c 00 20 00 00 00 ... or similar
            off += 8
        elif type_name == "Color":
            off += 16  # guess: RGBA float
        elif type_name == "Vector":
            # could be 12-float or smaller — walk cautiously
            off += 12
        elif type_name in ("int", "uint32", "float", "Enum", "uint8", "uint16"):
            off += 8
        elif type_name == "String":
            # Variable — might be length-prefixed string trailer
            s, new_off = read_lenstr(data, off + 2)
            print(f"        String trailer value: {s!r}")
            off = new_off if s is not None else off + 8
        else:
            # Unknown — look at next 32 bytes, try to find next property
            print(f"        ** UNKNOWN TYPE ** — remaining 32 bytes:")
            more = data[off:off + 32].hex(" ")
            print(f"        {more}")
            break

    print(f"\n--- Remaining bytes from 0x{off:04x} to EOF ({len(data)-off} total) ---")
    remainder = data[off:]
    # Scan for readable strings
    for i in range(0, len(remainder), 16):
        chunk = remainder[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {off + i:04x}  {hex_part:<48s}  {asc}")


if __name__ == "__main__":
    main()
