"""Live memory scanner for the running Crimson Desert process.

Workflow
--------
1. Launch the game and load the area where the boss is visible.
2. Run this script (no admin needed for same-user process):
       python tools/scan_game_memory.py --value 2.3 --tolerance 0.0001
3. The script enumerates every readable memory region in the game
   process and reports every address holding ``2.3`` as fp32. Each
   match comes with a 64-byte hex+ASCII context dump so you can
   spot patterns / nearby values.
4. To bisect: with the game still running, pick a candidate address
   and overwrite it:
       python tools/scan_game_memory.py --write-address 0x12345678 \\
              --write-value 1.0 --type f32
   Then look at the boss in-game. If it shrinks, you've found
   the live scale variable. The next step (cross-correlating that
   variable to a source file/offset) is mechanical: the bytes
   around the match in memory will appear ~verbatim somewhere in
   the loaded .pabgb / .pabc / .hkx files (modulo decompression).

Why no DLL injection
--------------------
The Windows API ``OpenProcess`` + ``ReadProcessMemory`` /
``WriteProcessMemory`` is enough for our use case and doesn't
trigger anti-cheat. We don't hook functions, don't load any
foreign DLL into the game, don't call any game code. We just read
(and optionally write) bytes the OS lets us see.

Tested on
---------
Windows 10 / 11 + Crimson Desert (Steam build, April 2026).
Python 3.14 (the install shipped with CrimsonForge).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time
from typing import Iterator


# ── Win32 constants ────────────────────────────────────────────────

PROCESS_QUERY_INFORMATION       = 0x0400
PROCESS_VM_READ                 = 0x0010
PROCESS_VM_WRITE                = 0x0020
PROCESS_VM_OPERATION            = 0x0008

MEM_COMMIT                      = 0x1000
PAGE_READABLE_MASK              = 0x66       # READONLY|READWRITE|EXECUTE_READ|EXECUTE_READWRITE
PAGE_GUARD                      = 0x100
PAGE_NOACCESS                   = 0x01

# Skip ranges that are either irrelevant (image / shared) or huge
# (large mapped files, gigabytes of texture cache). Hard-coded so
# the scanner finishes in seconds, not hours.
_SKIP_REGION_IF_LARGER_THAN     = 256 * 1024 * 1024   # 256 MB

TH32CS_SNAPPROCESS              = 0x00000002
INVALID_HANDLE_VALUE            = ctypes.c_void_p(-1).value


# ── Win32 structs ──────────────────────────────────────────────────

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_void_p),
        ("AllocationBase",    ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1",      wt.DWORD),
        ("RegionSize",        ctypes.c_size_t),
        ("State",             wt.DWORD),
        ("Protect",           wt.DWORD),
        ("Type",              wt.DWORD),
        ("__alignment2",      wt.DWORD),
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
        ("pcPriClassBase",      wt.LONG),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           wt.WCHAR * 260),
    ]


# ── Win32 binding helpers ──────────────────────────────────────────

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.OpenProcess.restype  = wt.HANDLE

_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.CloseHandle.restype  = wt.BOOL

_kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]
_kernel32.VirtualQueryEx.restype = ctypes.c_size_t

_kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.ReadProcessMemory.restype = wt.BOOL

_kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.WriteProcessMemory.restype = wt.BOOL

_kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype  = wt.HANDLE

_kernel32.Process32FirstW.argtypes = [
    wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W),
]
_kernel32.Process32FirstW.restype = wt.BOOL

_kernel32.Process32NextW.argtypes = [
    wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W),
]
_kernel32.Process32NextW.restype = wt.BOOL


# ── Process discovery ─────────────────────────────────────────────

def find_process(name: str) -> int | None:
    """Return the PID of the first running process whose exe name
    matches ``name`` (case-insensitive). None if not found.
    """
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        target = name.lower()
        while True:
            if entry.szExeFile.lower() == target:
                return entry.th32ProcessID
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        _kernel32.CloseHandle(snapshot)


def list_processes() -> list[tuple[int, str]]:
    """Return [(pid, exe_name)] for every visible process."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out: list[tuple[int, str]] = []
    if snapshot == INVALID_HANDLE_VALUE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return out
        while True:
            out.append((entry.th32ProcessID, entry.szExeFile))
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return out
    finally:
        _kernel32.CloseHandle(snapshot)


# ── Memory iteration ──────────────────────────────────────────────

def iter_readable_regions(handle: int) -> Iterator[tuple[int, int]]:
    """Yield (address, size) for every committed, readable memory
    region in the open process. Skips guard pages, NOACCESS pages,
    and obnoxiously-large mappings.
    """
    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)
    while True:
        bytes_returned = _kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(address),
            ctypes.byref(mbi), mbi_size,
        )
        if bytes_returned == 0:
            break
        size = mbi.RegionSize
        protect = mbi.Protect
        readable = (
            mbi.State == MEM_COMMIT
            and protect != PAGE_NOACCESS
            and not (protect & PAGE_GUARD)
            and (protect & PAGE_READABLE_MASK)
        )
        if readable and size <= _SKIP_REGION_IF_LARGER_THAN:
            yield mbi.BaseAddress or address, size
        # Advance to the next region. Use AllocationBase + RegionSize
        # to step past the current region cleanly.
        next_addr = (mbi.BaseAddress or address) + size
        if next_addr <= address:   # safety against runaway loop
            break
        address = next_addr


def read_region(handle: int, address: int, size: int) -> bytes | None:
    """Read ``size`` bytes from ``address`` in the foreign process.
    Returns None on failure (page paged-out, partial read, etc.).
    """
    buf = (ctypes.c_ubyte * size)()
    n_read = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address),
        ctypes.byref(buf), size, ctypes.byref(n_read),
    )
    if not ok or n_read.value != size:
        return None
    return bytes(buf)


def write_region(handle: int, address: int, data: bytes) -> bool:
    """Write ``data`` to ``address`` in the foreign process.
    Returns True on success.
    """
    buf = (ctypes.c_ubyte * len(data))(*data)
    n_written = ctypes.c_size_t(0)
    ok = _kernel32.WriteProcessMemory(
        handle, ctypes.c_void_p(address),
        ctypes.byref(buf), len(data), ctypes.byref(n_written),
    )
    return bool(ok) and n_written.value == len(data)


# ── Scan ──────────────────────────────────────────────────────────

def scan_for_value(
    handle: int,
    needle_bytes: bytes,
    tolerance_match: callable | None = None,
    progress_cb=None,
    max_hits: int = 5000,
) -> list[tuple[int, bytes]]:
    """Scan every readable region for ``needle_bytes``.

    Returns a list of ``(absolute_address, context_64_bytes)`` for
    each hit. Stops at ``max_hits`` to keep output manageable.

    ``tolerance_match`` lets fp32 callers accept "near-2.3" floats
    rather than strict equality. It receives 4 bytes and returns
    True if those bytes pass the match. When provided, the
    ``needle_bytes``-based fast path is bypassed for that 4-byte
    sliding window. None disables tolerance and uses byte-exact.
    """
    out: list[tuple[int, bytes]] = []
    region_count = 0
    for addr, size in iter_readable_regions(handle):
        region_count += 1
        if progress_cb and region_count % 50 == 0:
            try: progress_cb(region_count, len(out))
            except Exception: pass
        data = read_region(handle, addr, size)
        if data is None:
            continue
        if tolerance_match is None:
            # Byte-exact; use bytes.find for speed.
            pos = 0
            while True:
                idx = data.find(needle_bytes, pos)
                if idx < 0:
                    break
                ctx_start = max(0, idx - 16)
                ctx_end = min(len(data), idx + 48)
                out.append((addr + idx, data[ctx_start:ctx_end]))
                if len(out) >= max_hits:
                    return out
                pos = idx + 1
        else:
            # Sliding-window 4-byte tolerance match (fp32 only).
            for i in range(0, len(data) - 3):
                if tolerance_match(data[i:i+4]):
                    ctx_start = max(0, i - 16)
                    ctx_end = min(len(data), i + 48)
                    out.append((addr + i, data[ctx_start:ctx_end]))
                    if len(out) >= max_hits:
                        return out
    return out


# ── CLI ────────────────────────────────────────────────────────────

def _hex_dump(b: bytes, base_addr: int = 0) -> str:
    """Format bytes as a single-line hex+ASCII dump."""
    h = " ".join(f"{x:02x}" for x in b)
    a = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
    return f"  {h}  |  {a}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Scan / write the live Crimson Desert process memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--process", default="CrimsonDesert.exe",
        help="Process name (default: CrimsonDesert.exe)",
    )
    p.add_argument(
        "--value", type=str, default=None,
        help="Value to search for. Examples: '2.3' (fp32), "
             "'81' (int), '0x42138000' (raw u32 hex).",
    )
    p.add_argument(
        "--type", choices=("f32", "u32", "i32", "bytes"), default="f32",
        help="How to interpret --value (default: f32).",
    )
    p.add_argument(
        "--tolerance", type=float, default=0.0,
        help="For f32 only: accept values within ±tolerance of --value. "
             "Default 0 = byte-exact.",
    )
    p.add_argument(
        "--max-hits", type=int, default=5000,
        help="Stop after this many hits (default 5000).",
    )
    p.add_argument(
        "--list-processes", action="store_true",
        help="List visible processes and exit.",
    )
    p.add_argument(
        "--write-address", type=str, default=None,
        help="Address (hex like 0xABCDEF) to OVERWRITE — pair with --write-value.",
    )
    p.add_argument(
        "--write-value", type=str, default=None,
        help="Value to write at --write-address (interpreted via --type).",
    )
    p.add_argument(
        "--out", default=None,
        help="Optional CSV file to write all hits to.",
    )
    args = p.parse_args(argv)

    if args.list_processes:
        for pid, name in list_processes():
            print(f"  {pid:>8}  {name}")
        return 0

    pid = find_process(args.process)
    if pid is None:
        print(f"Could not find a running process named '{args.process}'.")
        print("Use --list-processes to see what's running.")
        return 1
    print(f"Found {args.process} pid={pid}")

    desired_access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if args.write_address:
        desired_access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION

    handle = _kernel32.OpenProcess(desired_access, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        print(f"OpenProcess failed (Win32 error {err}). Need admin?")
        return 2
    try:
        # ── WRITE mode (single address) ─────────────
        if args.write_address:
            addr = int(args.write_address, 0)
            if args.type == "f32":
                data = struct.pack("<f", float(args.write_value))
            elif args.type == "u32":
                data = struct.pack("<I", int(args.write_value, 0) & 0xFFFFFFFF)
            elif args.type == "i32":
                data = struct.pack("<i", int(args.write_value, 0))
            else:
                data = bytes.fromhex(args.write_value.replace(" ", ""))
            # Show what's there now first.
            current = read_region(handle, addr, len(data))
            print(f"  Address  : 0x{addr:016x}")
            print(f"  Currently: {current.hex() if current else '(unreadable)'}")
            print(f"  Writing  : {data.hex()}")
            ok = write_region(handle, addr, data)
            print(f"  Result   : {'OK' if ok else 'FAILED'}")
            return 0 if ok else 3

        # ── SCAN mode ──────────────────────────────
        if args.value is None:
            print("Pass --value to search for, or --write-address to write.")
            return 1

        if args.type == "f32":
            needle = struct.pack("<f", float(args.value))
            tm = None
            if args.tolerance > 0:
                target = float(args.value)
                tol = args.tolerance
                def _tm(b):
                    try:
                        v = struct.unpack("<f", b)[0]
                    except Exception:
                        return False
                    return abs(v - target) <= tol
                tm = _tm
        elif args.type == "u32":
            needle = struct.pack("<I", int(args.value, 0) & 0xFFFFFFFF); tm = None
        elif args.type == "i32":
            needle = struct.pack("<i", int(args.value, 0)); tm = None
        else:
            needle = bytes.fromhex(args.value.replace(" ", "")); tm = None

        print(f"Scanning for {args.type} value {args.value!r} "
              f"({needle.hex()}) — please wait...")
        t0 = time.time()
        def progress(n_regions, n_hits):
            print(f"  ... {n_regions} regions scanned, {n_hits} hits so far")
        hits = scan_for_value(
            handle, needle, tolerance_match=tm,
            progress_cb=progress, max_hits=args.max_hits,
        )
        elapsed = time.time() - t0
        print()
        print(f"Done in {elapsed:.1f}s — {len(hits)} hit(s).")
        if not hits:
            return 0
        # Print first 50, save all to CSV if requested
        for addr, ctx in hits[:50]:
            print(f"\n  0x{addr:016x}")
            print(_hex_dump(ctx, addr))
        if len(hits) > 50:
            print(f"\n  ... +{len(hits)-50} more hits (use --out to save all)")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write("address_hex,address_dec,context_hex\n")
                for addr, ctx in hits:
                    f.write(f"0x{addr:016x},{addr},{ctx.hex()}\n")
            print(f"\nWrote {len(hits)} hits to {args.out}")
        return 0
    finally:
        _kernel32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())
