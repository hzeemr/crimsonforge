"""Runtime memory probe v2 -- finds the LIVE Ogre by hash, not by AOB.

The v1 probe (probe_boss.py) AOB-scanned for the double-2.3 fp32 pattern
and locked onto a STATIC config-table entry, not the live boss instance
(visible in tools/probe_snapshot.txt -- 104-byte repeating struct with
incrementing 0xCN000CN entry IDs). v2 takes a different approach:

  1. Scan committed heap for the row hash 0x000F492A (LE: 2A 49 0F 00).
     PA engines typically reference characters by 32-bit hash, not
     by string -- the in-game boss instance probably has this hash
     stored as one of its first few u32 fields.
  2. Fall back to scanning for the ASCII string "Boss_Ogre_55515"
     in case the engine keeps the key text live.
  3. For each hit, dump 256 bytes around it so the user can see
     candidate HP / damage / state floats.
  4. Re-scan every 5 seconds and report which addresses are NEW
     (just-allocated structs -- likely the instance the player just
     triggered).

Run BEFORE entering the boss arena (baseline scan), then trigger the
fight. The new addresses that appear are the Ogre instance.

Usage
-----
    python -X utf8 tools/probe_boss_v2.py

Output
------
    tools/probe_v2_log.csv       append-only event log
    tools/probe_v2_dump.txt      hex+f32 dump of every current candidate

Read-only -- never writes to game memory.
"""
from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time
from pathlib import Path

THIS = Path(__file__).resolve().parent
LOG_CSV  = THIS / "probe_v2_log.csv"
DUMP_TXT = THIS / "probe_v2_dump.txt"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
TH32CS_SNAPPROCESS = 0x00000002

OGRE_HASH_LE = bytes.fromhex("2A490F00")     # 0x000F492A LE
OGRE_KEY     = b"Boss_Ogre_55515"

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


def read_memory(handle, addr: int, size: int) -> bytes | None:
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return bytes(buf[: read.value])


def iter_regions(handle, start: int, end: int):
    """Yield (base, size) for committed readable pages."""
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
        # Skip giant images / huge alloc -- focus on heap
        if readable and mbi.RegionSize <= 256 * 1024 * 1024:
            yield mbi.BaseAddress, mbi.RegionSize
        if next_addr <= addr:
            break
        addr = next_addr


def scan_for(handle, needle: bytes, start: int, end: int, max_hits: int = 256):
    hits = []
    for base, size in iter_regions(handle, start, end):
        # Read in 4 MB chunks to avoid one huge alloc
        CHUNK = 4 * 1024 * 1024
        OVERLAP = len(needle) - 1
        offset_in_region = 0
        while offset_in_region < size:
            n = min(CHUNK, size - offset_in_region)
            data = read_memory(handle, base + offset_in_region, n)
            if not data:
                offset_in_region += n
                continue
            i = 0
            while True:
                i = data.find(needle, i)
                if i == -1:
                    break
                hits.append(base + offset_in_region + i)
                if len(hits) >= max_hits:
                    return hits
                i += 1
            # Advance with overlap so a needle straddling chunks is still
            # found
            offset_in_region += n - OVERLAP if n == CHUNK else n
    return hits


def dump_window(handle, addr: int, before: int = 64, after: int = 256) -> str:
    base = addr - before
    data = read_memory(handle, base, before + after)
    if not data:
        return f"  (cannot read @ 0x{addr:016x})\n"
    out = [f"  hit @ 0x{addr:016x}  -- {before}b before / {after}b after\n"]
    pos = 0
    while pos + 4 <= len(data):
        rel = pos - before
        word = data[pos: pos + 4]
        u32 = struct.unpack("<I", word)[0]
        try:
            f32 = struct.unpack("<f", word)[0]
        except Exception:
            f32 = float("nan")
        marker = "  <<<<" if rel == 0 else ""
        out.append(
            f"    {rel:+5d}  {word.hex()}  u32=0x{u32:08x}  "
            f"f32={f32:.4g}{marker}\n"
        )
        pos += 4
    return "".join(out)


def now_ms() -> int:
    return int(time.time() * 1000)


def csv_append(rows):
    new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(
                ["timestamp_ms", "kind", "addr", "context"]
            )
        for r in rows:
            w.writerow(r)


def write_dump(handle, hits: list[tuple[int, str]], iteration: int):
    with open(DUMP_TXT, "w", encoding="utf-8") as f:
        f.write(
            f"=== probe_boss_v2 iteration {iteration} @ "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        )
        f.write(f"hits: {len(hits)}\n\n")
        for addr, kind in hits[:32]:
            f.write(f"--- {kind} @ 0x{addr:016x} ---\n")
            f.write(dump_window(handle, addr, before=32, after=128))
            f.write("\n")
        if len(hits) > 32:
            f.write(f"... and {len(hits) - 32} more hits not dumped\n")


def main():
    print(f"probe_boss_v2 -- waiting for CrimsonDesert.exe ...")
    csv_append([(now_ms(), "boot", "", time.strftime("%Y-%m-%d %H:%M:%S"))])

    pid = 0
    while pid == 0:
        pid = find_process("CrimsonDesert.exe")
        if pid == 0:
            time.sleep(2)

    handle = k32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        print(f"OpenProcess failed: {ctypes.get_last_error()}")
        return 1
    print(f"opened pid {pid}")
    csv_append([(now_ms(), "open", str(pid), "")])

    SCAN_START = 0x140000000   # game heap region (Pearl Abyss layout)
    SCAN_END   = 0x800000000

    seen_hash_hits: set[int] = set()
    seen_str_hits:  set[int] = set()
    iteration = 0
    try:
        while True:
            iteration += 1
            print(f"\n[iter {iteration}] scanning hash 0x000F492A ...")
            t0 = time.time()
            hash_hits = scan_for(handle, OGRE_HASH_LE, SCAN_START, SCAN_END)
            print(f"  hash hits: {len(hash_hits)}  ({time.time()-t0:.1f}s)")
            csv_append([(now_ms(), "scan_hash",
                         str(len(hash_hits)),
                         f"iter={iteration}")])

            t0 = time.time()
            str_hits = scan_for(handle, OGRE_KEY, SCAN_START, SCAN_END)
            print(f"  string hits: {len(str_hits)}  ({time.time()-t0:.1f}s)")
            csv_append([(now_ms(), "scan_str",
                         str(len(str_hits)),
                         f"iter={iteration}")])

            new_hash = [a for a in hash_hits if a not in seen_hash_hits]
            new_str  = [a for a in str_hits  if a not in seen_str_hits]
            if new_hash:
                print(f"  NEW hash hits ({len(new_hash)}):")
                for a in new_hash[:10]:
                    print(f"    0x{a:016x}")
            if new_str:
                print(f"  NEW string hits ({len(new_str)}):")
                for a in new_str[:10]:
                    print(f"    0x{a:016x}")

            for a in new_hash:
                csv_append([(now_ms(), "new_hash", f"0x{a:016x}", "")])
            for a in new_str:
                csv_append([(now_ms(), "new_str", f"0x{a:016x}", "")])

            seen_hash_hits.update(hash_hits)
            seen_str_hits.update(str_hits)

            all_hits = (
                [(a, "hash") for a in hash_hits] +
                [(a, "string") for a in str_hits]
            )
            if all_hits:
                write_dump(handle, all_hits, iteration)

            print(f"[iter {iteration}] sleeping 8s -- "
                  f"go fight the boss now to see new addresses appear")
            time.sleep(8)
    except KeyboardInterrupt:
        print("\nstopped")
        csv_append([(now_ms(), "stop", "", "ctrl_c")])
    finally:
        k32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main() or 0)
