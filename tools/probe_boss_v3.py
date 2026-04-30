"""Runtime memory probe v3 -- robust reads + immediate dumps.

v2 found 7 hash hits but failed to dump any of them (read returned 0 bytes
seconds later -- pages got recycled or the read crossed a region boundary
going backward). v3 fixes that by:

  * reading IMMEDIATELY when each hit is found (no batched dump)
  * reading FORWARD only (no addr-32 -- avoids spanning allocations)
  * trying smaller reads if the big read fails
  * scanning is hash-only by default (string scan was 47-131s/iter, hash
    is the strong signal anyway)
  * polling each known live hit every 1 s and logging any value that
    changes -- so when you swing at the boss the HP byte will be obvious

Usage
-----
    python -X utf8 tools/probe_boss_v3.py

    # Or with a longer rescan interval (default 30s -- the heap-wide
    # scan is expensive)
    python -X utf8 tools/probe_boss_v3.py --rescan 60

Output
------
    tools/probe_v3_log.csv         every event + every value change
    tools/probe_v3_hits.txt        latest dump for every known hit
    stdout: live status + change notifications
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
from pathlib import Path

THIS = Path(__file__).resolve().parent
LOG_CSV  = THIS / "probe_v3_log.csv"
DUMP_TXT = THIS / "probe_v3_hits.txt"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010
MEM_COMMIT      = 0x1000
PAGE_GUARD      = 0x100
PAGE_NOACCESS   = 0x01
TH32CS_SNAPPROCESS = 0x00000002

OGRE_HASH_LE = bytes.fromhex("2A490F00")    # 0x000F492A LE

WINDOW_BYTES = 1024   # how many bytes to dump after each hit

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


def try_read(handle, addr: int, size: int) -> bytes:
    """Best-effort read; returns truncated bytes if a large read fails."""
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(read)
    ) and read.value > 0:
        return bytes(buf[: read.value])
    # Big read failed -- bisect down.
    if size <= 16:
        return b""
    half = size // 2
    a = try_read(handle, addr, half)
    if len(a) < half:
        return a
    b = try_read(handle, addr + half, size - half)
    return a + b


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
        if readable and mbi.RegionSize <= 1024 * 1024 * 1024:
            yield mbi.BaseAddress, mbi.RegionSize
        if next_addr <= addr:
            break
        addr = next_addr


def scan_for_hash(handle, needle: bytes, on_hit, start: int, end: int):
    """Scan for needle, calling on_hit(addr, immediate_window) on each
    match BEFORE moving on. immediate_window is the WINDOW_BYTES bytes
    starting at addr -- captured right away so heap reuse can't lose
    them."""
    found = 0
    for base, size in iter_regions(handle, start, end):
        CHUNK = 4 * 1024 * 1024
        OVERLAP = len(needle) - 1
        offset_in_region = 0
        while offset_in_region < size:
            n = min(CHUNK, size - offset_in_region)
            data = try_read(handle, base + offset_in_region, n)
            if not data:
                offset_in_region += n
                continue
            i = 0
            while True:
                i = data.find(needle, i)
                if i == -1:
                    break
                addr = base + offset_in_region + i
                # Read the window IMMEDIATELY -- before we move on to
                # the next chunk, this hit's pages are most likely
                # still committed.
                window = try_read(handle, addr, WINDOW_BYTES)
                on_hit(addr, window)
                found += 1
                i += 1
            offset_in_region += n - OVERLAP if n == CHUNK else n
    return found


def annotate_window(addr: int, window: bytes) -> str:
    """Render window as 'offset hex u32 f32' lines, marking HP-shaped
    fp32s and the hit position."""
    out = [f"  hit @ 0x{addr:016x}  -- {len(window)} bytes\n"]
    pos = 0
    while pos + 4 <= len(window):
        word = window[pos: pos + 4]
        u32 = struct.unpack("<I", word)[0]
        try:
            f32 = struct.unpack("<f", word)[0]
        except Exception:
            f32 = float("nan")
        marker = ""
        if pos == 0:
            marker = "  <<< HASH"
        elif 1 <= f32 <= 100000 and not (
            f32 == int(f32) and int(f32) in (1, 2, 3, 100, 1000)
        ):
            marker = "  <-- HP-shaped fp32"
        elif u32 in (0x000F492A,):
            marker = "  <-- another ogre hash"
        out.append(
            f"    +{pos:04x}  {word.hex()}  u32=0x{u32:08x}  "
            f"f32={f32:.4g}{marker}\n"
        )
        pos += 4
    return "".join(out)


def hp_candidates(window: bytes) -> list[tuple[int, float]]:
    """Return all (offset, f32_value) where the float looks HP-shaped."""
    out = []
    pos = 0
    while pos + 4 <= len(window):
        try:
            f32 = struct.unpack("<f", window[pos: pos + 4])[0]
        except Exception:
            pos += 4
            continue
        if (
            10 <= f32 <= 1_000_000
            and not (f32 == int(f32) and int(f32) in (1, 2, 3, 100, 1000))
        ):
            out.append((pos, f32))
        pos += 4
    return out


def csv_init():
    new = not LOG_CSV.exists()
    if new:
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["timestamp_ms", "kind", "addr", "offset", "old", "new"]
            )


def csv_event(*row):
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def now_ms() -> int:
    return int(time.time() * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--rescan", type=float, default=30.0,
        help="Seconds between heap-wide rescans (default 30)",
    )
    ap.add_argument(
        "--poll", type=float, default=1.0,
        help="Seconds between value-change polls of known hits "
             "(default 1)",
    )
    args = ap.parse_args()

    csv_init()
    print(f"probe_boss_v3 -- waiting for CrimsonDesert.exe ...")
    csv_event(now_ms(), "boot", "", "", "", "")

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
    csv_event(now_ms(), "open", str(pid), "", "", "")

    SCAN_START = 0x140000000
    SCAN_END   = 0x800000000

    # Map: addr -> last-known window bytes (for change detection)
    hits: dict[int, bytes] = {}
    last_scan = 0.0
    iteration = 0

    try:
        while True:
            now = time.time()
            if now - last_scan >= args.rescan:
                iteration += 1
                last_scan = now
                print(
                    f"\n[iter {iteration}] heap scan for ogre hash "
                    f"0x000F492A ..."
                )
                t0 = time.time()
                new_hits: list[tuple[int, bytes]] = []

                def on_hit(addr, window):
                    new_hits.append((addr, window))

                scan_for_hash(handle, OGRE_HASH_LE, on_hit, SCAN_START, SCAN_END)
                dt = time.time() - t0
                print(f"  scan complete: {len(new_hits)} hits in {dt:.1f}s")
                csv_event(now_ms(), "scan", str(iteration),
                          str(len(new_hits)), "", "")

                # Update hits dict
                added = 0
                for addr, window in new_hits:
                    if addr not in hits and len(window) > 16:
                        hits[addr] = window
                        added += 1
                        csv_event(now_ms(), "new_hit", f"0x{addr:016x}",
                                  "", "", str(len(window)))

                print(f"  tracking {len(hits)} addresses ({added} new)")

                # Dump current state with HP candidates highlighted
                with open(DUMP_TXT, "w", encoding="utf-8") as f:
                    f.write(
                        f"=== probe_v3 iter {iteration} @ "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                    )
                    f.write(f"tracking {len(hits)} live hits\n\n")
                    for addr, win in list(hits.items())[:20]:
                        f.write(f"--- @ 0x{addr:016x} ---\n")
                        cands = hp_candidates(win)
                        f.write(
                            f"  HP-shaped fp32s in window: "
                            f"{[(hex(o), round(v,2)) for o, v in cands][:20]}\n"
                        )
                        f.write(annotate_window(addr, win[:256]))
                        f.write("\n")

            # Poll known hits for value changes
            for addr in list(hits.keys()):
                window = try_read(handle, addr, WINDOW_BYTES)
                if len(window) < 16:
                    continue
                old = hits[addr]
                if window != old:
                    # Find which 4-byte word(s) changed
                    diffs = []
                    for off in range(0, min(len(window), len(old)) - 3, 4):
                        if window[off:off+4] != old[off:off+4]:
                            o_u = struct.unpack("<I", old[off:off+4])[0]
                            n_u = struct.unpack("<I", window[off:off+4])[0]
                            o_f = struct.unpack("<f", old[off:off+4])[0]
                            n_f = struct.unpack("<f", window[off:off+4])[0]
                            diffs.append((off, o_u, n_u, o_f, n_f))
                    if diffs:
                        print(
                            f"  CHANGE @ 0x{addr:016x}: "
                            f"{len(diffs)} word(s) differ"
                        )
                        for off, ou, nu, of_, nf_ in diffs[:8]:
                            print(
                                f"    +{off:04x}: u32 0x{ou:08x}->0x{nu:08x}  "
                                f"f32 {of_:.4g}->{nf_:.4g}"
                            )
                            csv_event(now_ms(), "change",
                                      f"0x{addr:016x}", f"+0x{off:04x}",
                                      f"f32:{of_:.6g}", f"f32:{nf_:.6g}")
                    hits[addr] = window
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nstopped")
        csv_event(now_ms(), "stop", "", "", "", "ctrl_c")
    finally:
        k32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main() or 0)
