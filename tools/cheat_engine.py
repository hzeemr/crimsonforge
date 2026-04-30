"""Cheat-Engine-style HP finder for Crimson Desert.

The probe_boss_*.py family chases the row hash 0x000F492A and finds
only static references. Those don't change when you hit the boss.
The actual live HP value is somewhere else and the only way to find
it is the classic "first scan / next scan" workflow:

  1. Note the boss's current HP from the HUD.
  2. Scan all of process memory for that fp32 value.
  3. Hit the boss once -- HP drops a bit.
  4. Re-scan ONLY the addresses from step 2, looking for the new value.
  5. Keep narrowing until 1-3 addresses survive.
  6. That's the live HP. Optionally WRITE to it for instant kill.

Workflow
--------
    python -X utf8 tools/cheat_engine.py scan 3008
        -> finds every memory address whose fp32 == 3008.0
        -> saves to tools/ce_state.json

    # hit boss, HUD now reads e.g. 2980
    python -X utf8 tools/cheat_engine.py refine 2980
        -> checks each saved address; keeps only those whose value
           changed to ~2980; rewrites tools/ce_state.json

    # repeat until 1-5 addresses remain. Then peek:
    python -X utf8 tools/cheat_engine.py peek
        -> dumps the surviving addresses + values

    # Once you're confident which is HP, write to it:
    python -X utf8 tools/cheat_engine.py write 0xADDR 1.0

You can pass --type i32, --type u32 for non-float values.

Tolerance: refine accepts hits within +/- 0.5 by default (so HP regen
ticks don't lose them). Use --exact for strict matching.

This script is read-mostly. The "write" command is the ONLY one that
modifies game memory and requires the address argument explicitly.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import struct
import sys
import time
from pathlib import Path

THIS = Path(__file__).resolve().parent
STATE = THIS / "ce_state.json"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_VM_OPERATION      = 0x0008
MEM_COMMIT      = 0x1000
PAGE_GUARD      = 0x100
PAGE_NOACCESS   = 0x01
TH32CS_SNAPPROCESS = 0x00000002

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_ulonglong),
        ("AllocationBase",    ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__align",           wt.DWORD),
        ("RegionSize",        ctypes.c_ulonglong),
        ("State",             wt.DWORD),
        ("Protect",           wt.DWORD),
        ("Type",              wt.DWORD),
        ("__align2",          wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.c_void_p),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_wchar * 260),
    ]


def find_process(name: str) -> int:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return 0
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(pe)
    if not k32.Process32FirstW(snap, ctypes.byref(pe)):
        k32.CloseHandle(snap)
        return 0
    while True:
        if pe.szExeFile.lower() == name.lower():
            pid = pe.th32ProcessID
            k32.CloseHandle(snap)
            return pid
        if not k32.Process32NextW(snap, ctypes.byref(pe)):
            break
    k32.CloseHandle(snap)
    return 0


def open_handle(pid: int, write: bool = False) -> int:
    rights = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if write:
        rights |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    return k32.OpenProcess(rights, False, pid)


def read_memory(handle, addr: int, size: int) -> bytes:
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(read)
    ) and read.value > 0:
        return bytes(buf[: read.value])
    if size <= 4:
        return b""
    half = size // 2
    a = read_memory(handle, addr, half)
    if len(a) < half:
        return a
    b = read_memory(handle, addr + half, size - half)
    return a + b


def write_memory(handle, addr: int, data: bytes) -> bool:
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    written = ctypes.c_size_t(0)
    return bool(k32.WriteProcessMemory(
        handle, ctypes.c_void_p(addr), buf, len(data),
        ctypes.byref(written),
    )) and written.value == len(data)


def iter_regions(handle, start: int, end: int):
    addr = start
    mbi = MEMORY_BASIC_INFORMATION64()
    while addr < end:
        ok = k32.VirtualQueryEx(
            handle, ctypes.c_void_p(addr),
            ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ok:
            break
        next_addr = mbi.BaseAddress + mbi.RegionSize
        readable = (
            mbi.State == MEM_COMMIT
            and not (mbi.Protect & PAGE_GUARD)
            and mbi.Protect != PAGE_NOACCESS
        )
        if readable and mbi.RegionSize <= 1024 * 1024 * 1024:
            yield mbi.BaseAddress, mbi.RegionSize
        if next_addr <= addr:
            break
        addr = next_addr


def encode(value: float | int, type_: str) -> bytes:
    if type_ == "f32":
        return struct.pack("<f", float(value))
    if type_ == "f64":
        return struct.pack("<d", float(value))
    if type_ == "i32":
        return struct.pack("<i", int(value))
    if type_ == "u32":
        return struct.pack("<I", int(value))
    if type_ == "u16":
        return struct.pack("<H", int(value))
    raise ValueError(f"unknown type: {type_}")


def decode(raw: bytes, type_: str) -> float | int:
    if type_ == "f32":  return struct.unpack("<f", raw[:4])[0]
    if type_ == "f64":  return struct.unpack("<d", raw[:8])[0]
    if type_ == "i32":  return struct.unpack("<i", raw[:4])[0]
    if type_ == "u32":  return struct.unpack("<I", raw[:4])[0]
    if type_ == "u16":  return struct.unpack("<H", raw[:2])[0]
    return 0


def matches(actual: float | int, expected: float | int,
            type_: str, exact: bool, tol: float) -> bool:
    if type_ in ("f32", "f64"):
        if exact:
            return actual == expected
        return abs(actual - expected) <= tol
    return actual == expected


def cmd_scan(args):
    pid = find_process("CrimsonDesert.exe")
    if not pid:
        print("CrimsonDesert.exe not running")
        return 1
    handle = open_handle(pid)
    if not handle:
        print(f"OpenProcess failed: {ctypes.get_last_error()}")
        return 1
    print(f"opened pid {pid}")

    needle = encode(args.value, args.type)
    width = len(needle)
    print(f"scanning for {args.type}={args.value} ({needle.hex()}) ...")
    t0 = time.time()
    hits: list[int] = []
    region_count = 0
    for base, size in iter_regions(handle, args.start, args.end):
        region_count += 1
        CHUNK = 4 * 1024 * 1024
        offset = 0
        while offset < size:
            n = min(CHUNK, size - offset)
            data = read_memory(handle, base + offset, n)
            if not data:
                offset += n
                continue
            i = 0
            while True:
                i = data.find(needle, i)
                if i == -1:
                    break
                hits.append(base + offset + i)
                i += 1
                if len(hits) >= args.max:
                    print(f"  hit cap {args.max} -- stopping early")
                    offset = size
                    break
            offset += n - (width - 1) if n == CHUNK else n
        if len(hits) >= args.max:
            break

    dt = time.time() - t0
    print(f"scan done: {len(hits)} hits in {dt:.1f}s "
          f"across {region_count} regions")
    state = {
        "type": args.type,
        "value_at_scan": args.value,
        "addrs": [f"0x{a:x}" for a in hits],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    STATE.write_text(json.dumps(state, indent=2))
    print(f"state written to {STATE}  (run 'refine NEW_VALUE' next)")
    k32.CloseHandle(handle)
    return 0


def cmd_refine(args):
    if not STATE.exists():
        print(f"no state file -- run 'scan VALUE' first")
        return 1
    state = json.loads(STATE.read_text())
    type_ = state["type"]
    addrs = [int(a, 16) for a in state["addrs"]]
    print(f"refining {len(addrs)} addresses for {type_}={args.value} "
          f"(tol +/- {args.tol})")

    pid = find_process("CrimsonDesert.exe")
    if not pid:
        print("CrimsonDesert.exe not running")
        return 1
    handle = open_handle(pid)
    width = len(encode(args.value, type_))
    survivors = []
    for a in addrs:
        raw = read_memory(handle, a, width)
        if len(raw) < width:
            continue
        v = decode(raw, type_)
        if matches(v, args.value, type_, args.exact, args.tol):
            survivors.append((a, v))
    print(f"{len(survivors)} addresses survive")
    for a, v in survivors[:30]:
        print(f"  0x{a:016x}  {v}")
    if len(survivors) > 30:
        print(f"  ... and {len(survivors) - 30} more")
    state["addrs"] = [f"0x{a:x}" for a, _ in survivors]
    state["value_at_scan"] = args.value
    state["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE.write_text(json.dumps(state, indent=2))
    k32.CloseHandle(handle)
    return 0


def cmd_peek(args):
    if not STATE.exists():
        print(f"no state file")
        return 1
    state = json.loads(STATE.read_text())
    type_ = state["type"]
    addrs = [int(a, 16) for a in state["addrs"]]
    pid = find_process("CrimsonDesert.exe")
    if not pid:
        print("CrimsonDesert.exe not running")
        return 1
    handle = open_handle(pid)
    width = 8 if type_ == "f64" else 4
    print(f"{len(addrs)} surviving addresses (last value: "
          f"{state['value_at_scan']}, last refined: {state['ts']}):")
    for a in addrs[:50]:
        raw = read_memory(handle, a, width)
        if len(raw) >= width:
            v = decode(raw, type_)
            # Also dump nearby u32s for context
            ctx = read_memory(handle, max(0, a - 16), 48)
            ctx_words = []
            for i in range(0, len(ctx) - 3, 4):
                ctx_words.append(
                    f"{struct.unpack('<f', ctx[i:i+4])[0]:.3g}"
                )
            print(f"  0x{a:016x}  = {v}    nearby f32: {ctx_words}")
    if len(addrs) > 50:
        print(f"  ... and {len(addrs) - 50} more")
    k32.CloseHandle(handle)
    return 0


def cmd_write(args):
    pid = find_process("CrimsonDesert.exe")
    if not pid:
        print("CrimsonDesert.exe not running")
        return 1
    handle = open_handle(pid, write=True)
    if not handle:
        print(f"OpenProcess(write) failed: {ctypes.get_last_error()}")
        return 1
    addr = int(args.addr, 16) if args.addr.startswith("0x") else int(args.addr)
    payload = encode(args.value, args.type)
    print(f"writing {args.type}={args.value} ({payload.hex()}) "
          f"to 0x{addr:016x}")
    ok = write_memory(handle, addr, payload)
    print("OK" if ok else f"FAILED: {ctypes.get_last_error()}")
    k32.CloseHandle(handle)
    return 0 if ok else 1


def cmd_read(args):
    pid = find_process("CrimsonDesert.exe")
    if not pid:
        print("CrimsonDesert.exe not running")
        return 1
    handle = open_handle(pid)
    addr = int(args.addr, 16) if args.addr.startswith("0x") else int(args.addr)
    width = 8 if args.type == "f64" else (2 if args.type == "u16" else 4)
    raw = read_memory(handle, addr, width)
    if len(raw) < width:
        print("read failed")
        return 1
    v = decode(raw, args.type)
    print(f"0x{addr:016x}  {args.type}={v}  raw={raw.hex()}")
    k32.CloseHandle(handle)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s_scan = sub.add_parser("scan", help="initial value scan")
    s_scan.add_argument("value", type=float)
    s_scan.add_argument("--type", default="f32",
                        choices=["f32", "f64", "i32", "u32", "u16"])
    s_scan.add_argument("--max", type=int, default=200000,
                        help="hit-count cap (default 200000)")
    s_scan.add_argument("--start", type=lambda s: int(s, 0),
                        default=0x140000000)
    s_scan.add_argument("--end", type=lambda s: int(s, 0),
                        default=0x800000000)
    s_scan.set_defaults(func=cmd_scan)

    s_ref = sub.add_parser("refine", help="filter saved addresses by new value")
    s_ref.add_argument("value", type=float)
    s_ref.add_argument("--exact", action="store_true",
                       help="require exact match (no tolerance)")
    s_ref.add_argument("--tol", type=float, default=2.0,
                       help="tolerance for fp32/fp64 (default 2.0 -- "
                            "covers HP regen ticks)")
    s_ref.set_defaults(func=cmd_refine)

    s_peek = sub.add_parser("peek", help="show current values at survivors")
    s_peek.set_defaults(func=cmd_peek)

    s_write = sub.add_parser("write", help="write value to address")
    s_write.add_argument("addr", help="hex (0x...) or decimal")
    s_write.add_argument("value", type=float)
    s_write.add_argument("--type", default="f32",
                         choices=["f32", "f64", "i32", "u32", "u16"])
    s_write.set_defaults(func=cmd_write)

    s_read = sub.add_parser("read", help="read value at address")
    s_read.add_argument("addr")
    s_read.add_argument("--type", default="f32",
                        choices=["f32", "f64", "i32", "u32", "u16"])
    s_read.set_defaults(func=cmd_read)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
