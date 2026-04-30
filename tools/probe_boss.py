"""Runtime memory probe for Crimson Desert boss data — read-only.

How to run
----------
    python -X utf8 tools/probe_boss.py

Then play the game. The probe will:
  1. Wait for ``CrimsonDesert.exe`` to start.
  2. AOB-scan the heap for the double-fp32-2.3 pattern that marks
     the boss SCALE field (the same pattern auto_bisect_ogre_scale.py
     uses). Retries every 5 s until at least one candidate appears.
  3. Pick the candidate whose +72-byte offset reads as a plausible
     HP value (10 ≤ v ≤ 100 000). If multiple candidates remain,
     the addresses are written to ``tools/probe_choose.txt`` — edit
     that file to leave only one ``0x...`` line and the probe
     picks it up automatically.
  4. Poll the chosen struct every 250 ms. ANY change in HP,
     position vec3, scale vec3, damage, or any "looks-like-state"
     u32 in the surrounding ±512-byte window is logged.

Output files
------------
  tools/probe_log.csv          — append-only change log. Columns:
                                 timestamp_ms,addr_offset,kind,
                                 old_value,new_value
  tools/probe_snapshot.txt     — current state, overwritten every
                                 ~5 s. Open it any time to see
                                 where the boss is right now.

Stdout shows a heartbeat every second:
  [hh:mm:ss] tracking @ 0x... | HP=... Pos=(x,y,z) Scale=...

Press Ctrl+C to stop. The CSV stays on disk.

Important: this tool ONLY reads the game's memory. It never
writes. Safe with anti-cheat (no hooks, no DLL injection, no
WriteProcessMemory).
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.scan_game_memory import (   # noqa: E402
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    _kernel32, find_process, iter_readable_regions, read_region,
)


# ── Constants ────────────────────────────────────────────────────

PROCESS_NAME             = "CrimsonDesert.exe"
WAIT_INTERVAL_S          = 2.0       # how often to poll for the game when not running
RESCAN_INTERVAL_S        = 5.0       # how often to retry AOB scan when no candidates
POLL_INTERVAL_S          = 0.25      # 250 ms tick while tracking
SNAPSHOT_INTERVAL_S      = 5.0
HEARTBEAT_INTERVAL_S     = 1.0

# AOB pattern: two consecutive fp32 2.3 values, little-endian.
# Same signature auto_bisect_ogre_scale.py uses.
DOUBLE_2_3               = struct.pack("<ff", 2.3, 2.3)

# Offset hypothesis from earlier research.
HP_OFFSET                = 72
DAMAGE_OFFSET            = 704

# Search window around the struct for "interesting" u32 fields.
WATCH_WINDOW_BYTES       = 1024      # ±512 around the scale addr
WATCH_WINDOW_HALF        = WATCH_WINDOW_BYTES // 2

# HP-shape filter for picking among AOB candidates.
HP_PLAUSIBLE_MIN         = 10.0
HP_PLAUSIBLE_MAX         = 100_000.0

# Address-pick file the user can edit if multiple HP-shaped
# candidates survive filtering.
TOOLS_DIR                = Path(__file__).resolve().parent
LOG_PATH                 = TOOLS_DIR / "probe_log.csv"
SNAPSHOT_PATH            = TOOLS_DIR / "probe_snapshot.txt"
CHOOSE_PATH              = TOOLS_DIR / "probe_choose.txt"

START_ADDR               = 0x140000000


# ── Material/shader filter (carried over from auto_bisect) ──────

def _is_material_buffer(ctx: bytes) -> bool:
    bad = (b"texture", b"material", b"noiss", b".dds", b".material",
           b"mat.", b"_mat", b"shader")
    return any(b in ctx for b in bad)


# ── Wait helpers ────────────────────────────────────────────────

def _wait_for_game() -> int:
    """Block until the game is running. Returns its PID."""
    print(f"[probe] waiting for {PROCESS_NAME} ...")
    while True:
        pid = find_process(PROCESS_NAME)
        if pid is not None:
            print(f"[probe] found {PROCESS_NAME} pid={pid}")
            return pid
        time.sleep(WAIT_INTERVAL_S)


def _open_handle(pid: int) -> int:
    """Open the process for read-only access. Aborts on failure."""
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid,
    )
    if not handle:
        err = ctypes.get_last_error()
        raise RuntimeError(f"OpenProcess failed (Win32 error {err})")
    return handle


# ── AOB scan ────────────────────────────────────────────────────

def _scan_for_pattern(handle: int, pattern: bytes,
                      start_addr: int = START_ADDR,
                      max_region_mb: int = 1024
                      ) -> list[tuple[int, bytes]]:
    """Return [(absolute_address, 64-byte context)] for every match."""
    hits: list[tuple[int, bytes]] = []
    for addr, size in iter_readable_regions(handle):
        if addr + size <= start_addr:
            continue
        if size > max_region_mb * 1024 * 1024:
            continue
        data = read_region(handle, addr, size)
        if data is None:
            continue
        pos = 0
        while True:
            i = data.find(pattern, pos)
            if i < 0:
                break
            ctx_s = max(0, i - 16)
            ctx_e = min(len(data), i + len(pattern) + 48)
            hits.append((addr + i, data[ctx_s:ctx_e]))
            pos = i + 1
    return hits


def _read_f32(handle: int, addr: int) -> float | None:
    raw = read_region(handle, addr, 4)
    if raw is None or len(raw) != 4:
        return None
    return struct.unpack("<f", raw)[0]


def _read_u32(handle: int, addr: int) -> int | None:
    raw = read_region(handle, addr, 4)
    if raw is None or len(raw) != 4:
        return None
    return struct.unpack("<I", raw)[0]


def _read_block(handle: int, addr: int, size: int) -> bytes | None:
    return read_region(handle, addr, size)


def _hp_shape(handle: int, scale_addr: int) -> float | None:
    """Read HP candidate at scale_addr+72 if it's a plausible HP value."""
    v = _read_f32(handle, scale_addr + HP_OFFSET)
    if v is None:
        return None
    if not (HP_PLAUSIBLE_MIN <= v <= HP_PLAUSIBLE_MAX):
        return None
    return v


def _filter_candidates(handle: int,
                       raw_hits: list[tuple[int, bytes]]
                       ) -> list[int]:
    """Reduce a fresh AOB scan to plausible boss-struct addresses."""
    # Skip material/shader hits.
    candidates = [a for a, c in raw_hits if not _is_material_buffer(c)]
    candidates.sort()
    # Keep only those whose +72 looks like HP.
    hp_shaped = [a for a in candidates if _hp_shape(handle, a) is not None]
    return hp_shaped if hp_shaped else candidates


def _resolve_choice(candidates: list[int]) -> int | None:
    """If user already curated probe_choose.txt to a single address,
    use that. Otherwise return None and let the caller proceed.
    """
    if not CHOOSE_PATH.is_file():
        return None
    addrs: list[int] = []
    for line in CHOOSE_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            addrs.append(int(s, 0))
        except ValueError:
            continue
    if len(addrs) == 1 and addrs[0] in candidates:
        return addrs[0]
    if len(addrs) == 1:
        # User picked an address even if it's not in our current scan
        # (e.g. they noted it from a previous run). Trust them.
        return addrs[0]
    return None


def _write_choose_file(candidates: list[int]) -> None:
    lines = [
        "# probe_boss.py found multiple HP-shaped candidates.",
        "# Delete all but ONE of the lines below to lock the probe",
        "# onto that address. The probe re-reads this file every 5 s.",
        "",
    ]
    for a in candidates:
        lines.append(f"0x{a:016x}")
    CHOOSE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Snapshot reader ─────────────────────────────────────────────

class StructSnapshot:
    """One frozen read of the boss struct + watch window."""

    __slots__ = ("scale_addr", "scale", "pos", "hp", "damage",
                 "watch_u32s")

    def __init__(self, scale_addr: int, scale: tuple[float, float, float] | None,
                 pos: tuple[float, float, float] | None,
                 hp: float | None, damage: float | None,
                 watch_u32s: dict[int, int]):
        self.scale_addr = scale_addr
        self.scale = scale
        self.pos = pos
        self.hp = hp
        self.damage = damage
        self.watch_u32s = watch_u32s


def _read_snapshot(handle: int, scale_addr: int) -> StructSnapshot | None:
    """Read the full window in one RPM call when possible.

    Returns None if even the scale address is unreadable (boss
    despawned, area unloaded, etc.).
    """
    win_start = scale_addr - WATCH_WINDOW_HALF
    win_size  = WATCH_WINDOW_BYTES
    block = _read_block(handle, win_start, win_size)
    if block is None:
        # Window read failed (page unmapped). Try just the scale alone
        # so we know whether the struct itself is gone.
        anchor = _read_block(handle, scale_addr, 8)
        if anchor is None:
            return None
        # Treat as "alive but watch window unreadable".
        scale = struct.unpack("<ff", anchor)
        return StructSnapshot(
            scale_addr=scale_addr,
            scale=(scale[0], scale[1], 0.0),
            pos=None, hp=None, damage=None,
            watch_u32s={},
        )

    rel_scale = WATCH_WINDOW_HALF
    # Scale: 3 floats starting at the matched offset.
    sx, sy, sz = struct.unpack_from("<fff", block, rel_scale)
    # Position vec3: closest reasonable guess is the 12 bytes
    # immediately before scale (a common engine layout: pos, scale).
    px = py = pz = None
    if rel_scale - 12 >= 0:
        try:
            px, py, pz = struct.unpack_from("<fff", block, rel_scale - 12)
        except struct.error:
            px = py = pz = None
    pos = (px, py, pz) if px is not None else None
    # HP at +72 (read fresh from the block).
    hp_off = rel_scale + HP_OFFSET
    hp = None
    if 0 <= hp_off + 4 <= len(block):
        hp = struct.unpack_from("<f", block, hp_off)[0]
    # Damage at +704 — almost certainly outside the 1 KB window,
    # so do a separate RPM.
    damage = _read_f32(handle, scale_addr + DAMAGE_OFFSET)
    # Sample u32s from the window at every 4-byte stride within
    # ±256 bytes of the anchor (we don't need the full ±512 every
    # tick; that would be 256 u32s per poll which is fine but the
    # diff is noisy). Keep it to ±256.
    watch_u32s: dict[int, int] = {}
    for off in range(rel_scale - 256, rel_scale + 256, 4):
        if 0 <= off + 4 <= len(block):
            watch_u32s[off - rel_scale] = struct.unpack_from(
                "<I", block, off
            )[0]
    return StructSnapshot(
        scale_addr=scale_addr,
        scale=(sx, sy, sz),
        pos=pos,
        hp=hp,
        damage=damage,
        watch_u32s=watch_u32s,
    )


# ── Logger ──────────────────────────────────────────────────────

class ChangeLog:
    """Append-only CSV writer with a tiny diff helper."""

    HEADER = "timestamp_ms,addr_offset,kind,old_value,new_value\n"

    def __init__(self, path: Path):
        self._path = path
        if not path.is_file() or path.stat().st_size == 0:
            path.write_text(self.HEADER, encoding="utf-8")
        self._fp = path.open("a", encoding="utf-8", buffering=1)

    def log(self, addr_offset: int | str, kind: str,
            old_value, new_value) -> None:
        ts_ms = int(time.time() * 1000)
        # Stringify floats consistently so diffs are stable.
        def _fmt(v):
            if v is None:
                return ""
            if isinstance(v, float):
                return f"{v:.6g}"
            return str(v)
        row = f"{ts_ms},{addr_offset},{kind},{_fmt(old_value)},{_fmt(new_value)}\n"
        self._fp.write(row)

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


def _write_snapshot(snap: StructSnapshot) -> None:
    """Overwrite probe_snapshot.txt with the latest state."""
    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"=== probe_boss snapshot @ {ts} ===")
    lines.append(f"scale_addr   : 0x{snap.scale_addr:016x}")
    lines.append(f"scale        : {snap.scale}")
    lines.append(f"position     : {snap.pos}")
    lines.append(f"HP (+72)     : {snap.hp}")
    lines.append(f"damage (+704): {snap.damage}")
    lines.append("")
    lines.append("Watch-window u32 fields (offset relative to scale_addr):")
    for off in sorted(snap.watch_u32s):
        v = snap.watch_u32s[off]
        # Show u32 + alternative interpretations so the human reading
        # the snapshot can spot what kind of value it is.
        f = struct.unpack("<f", struct.pack("<I", v))[0]
        sign = "+" if off >= 0 else "-"
        lines.append(
            f"  {sign}0x{abs(off):04x}  u32=0x{v:08x}  "
            f"i32={struct.unpack('<i', struct.pack('<I', v))[0]:>11}  "
            f"f32={f:.6g}"
        )
    SNAPSHOT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Diff + emit ─────────────────────────────────────────────────

def _diff_and_log(prev: StructSnapshot | None,
                  curr: StructSnapshot, log: ChangeLog) -> None:
    if prev is None:
        log.log("", "init", "", f"scale_addr=0x{curr.scale_addr:016x}")
        return
    if curr.scale != prev.scale:
        log.log(0, "scale", prev.scale, curr.scale)
    if curr.pos != prev.pos:
        log.log(-12, "pos", prev.pos, curr.pos)
    if curr.hp != prev.hp:
        log.log(HP_OFFSET, "hp", prev.hp, curr.hp)
    if curr.damage != prev.damage:
        log.log(DAMAGE_OFFSET, "damage", prev.damage, curr.damage)
    # Watch-window u32s.
    prev_w = prev.watch_u32s
    for off, v in curr.watch_u32s.items():
        old = prev_w.get(off)
        if old is not None and old != v:
            log.log(off, "u32", f"0x{old:08x}", f"0x{v:08x}")


def _heartbeat(scale_addr: int, snap: StructSnapshot | None) -> None:
    ts = time.strftime("%H:%M:%S")
    if snap is None:
        print(f"[{ts}] tracking @ 0x{scale_addr:016x} | (struct unreadable)")
        return
    pos = snap.pos
    pos_s = (f"({pos[0]:.2f},{pos[1]:.2f},{pos[2]:.2f})"
             if pos is not None else "(?, ?, ?)")
    sc = snap.scale
    sc_s = (f"({sc[0]:.2f},{sc[1]:.2f},{sc[2]:.2f})"
            if sc is not None else "?")
    hp_s = f"{snap.hp:.2f}" if snap.hp is not None else "?"
    print(f"[{ts}] tracking @ 0x{scale_addr:016x} | "
          f"HP={hp_s} Pos={pos_s} Scale={sc_s}")


# ── Main loop ───────────────────────────────────────────────────

def _find_struct(handle: int, log: ChangeLog) -> int | None:
    """Run one AOB scan + filter pass. Returns a chosen address or
    None if we should retry later.
    """
    raw_hits = _scan_for_pattern(handle, DOUBLE_2_3)
    if not raw_hits:
        msg = "no candidates yet — boss not loaded"
        print(f"[probe] {msg}")
        log.log("", "scan", "", msg)
        return None
    candidates = _filter_candidates(handle, raw_hits)
    if not candidates:
        # All hits were material buffers.
        msg = f"all {len(raw_hits)} raw hits filtered out (material/shader)"
        print(f"[probe] {msg}")
        log.log("", "scan", "", msg)
        return None
    if len(candidates) == 1:
        addr = candidates[0]
        print(f"[probe] single candidate: 0x{addr:016x}")
        log.log("", "scan", "",
                f"single candidate 0x{addr:016x}")
        return addr
    # Multiple candidates — see if the user has already chosen.
    chosen = _resolve_choice(candidates)
    if chosen is not None:
        print(f"[probe] using user-picked address from "
              f"{CHOOSE_PATH.name}: 0x{chosen:016x}")
        log.log("", "scan", "",
                f"user-picked 0x{chosen:016x}")
        return chosen
    # Write the menu and back off.
    _write_choose_file(candidates)
    addr_list = ", ".join(f"0x{a:016x}" for a in candidates[:6])
    msg = (f"{len(candidates)} HP-shaped candidates; "
           f"edit {CHOOSE_PATH.name} to pick one")
    print(f"[probe] {msg}")
    print(f"[probe] candidates: {addr_list}"
          + (" ..." if len(candidates) > 6 else ""))
    log.log("", "scan", "", msg)
    return None


def _track(handle: int, scale_addr: int, log: ChangeLog) -> None:
    """Main 250 ms polling loop. Returns when struct vanishes."""
    prev: StructSnapshot | None = None
    last_snapshot_t = 0.0
    last_heartbeat_t = 0.0
    while True:
        loop_t = time.time()
        snap = _read_snapshot(handle, scale_addr)
        if snap is None:
            # Read returned None across the board → struct gone.
            print(f"[probe] struct at 0x{scale_addr:016x} "
                  f"unreadable; re-scanning")
            log.log("", "scan", "",
                    f"lost struct at 0x{scale_addr:016x}")
            return
        _diff_and_log(prev, snap, log)
        prev = snap
        if loop_t - last_snapshot_t >= SNAPSHOT_INTERVAL_S:
            try:
                _write_snapshot(snap)
            except Exception as exc:
                print(f"[probe] snapshot write failed: {exc}")
            last_snapshot_t = loop_t
        if loop_t - last_heartbeat_t >= HEARTBEAT_INTERVAL_S:
            _heartbeat(scale_addr, snap)
            last_heartbeat_t = loop_t
        # Sleep the remainder of the tick.
        elapsed = time.time() - loop_t
        nap = POLL_INTERVAL_S - elapsed
        if nap > 0:
            time.sleep(nap)


def main(argv: list[str] | None = None) -> int:
    print("=" * 64)
    print("probe_boss — read-only runtime memory probe")
    print(f"  log      : {LOG_PATH}")
    print(f"  snapshot : {SNAPSHOT_PATH}")
    print(f"  choose   : {CHOOSE_PATH}")
    print("=" * 64)

    log = ChangeLog(LOG_PATH)
    log.log("", "boot", "", time.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        while True:
            pid = _wait_for_game()
            try:
                handle = _open_handle(pid)
            except RuntimeError as exc:
                print(f"[probe] {exc}; retrying in {WAIT_INTERVAL_S}s")
                time.sleep(WAIT_INTERVAL_S)
                continue
            log.log("", "open", "", f"pid={pid}")
            try:
                # Find the boss struct (with retry).
                addr: int | None = None
                while addr is None:
                    if find_process(PROCESS_NAME) != pid:
                        # Game closed mid-scan.
                        raise _GameClosed()
                    addr = _find_struct(handle, log)
                    if addr is None:
                        time.sleep(RESCAN_INTERVAL_S)
                _track(handle, addr, log)
                # _track returned → struct gone, re-scan from scratch.
                continue
            except _GameClosed:
                print("[probe] game process closed; waiting again")
                log.log("", "close", "", f"pid={pid}")
            finally:
                try: _kernel32.CloseHandle(handle)
                except Exception: pass
    except KeyboardInterrupt:
        print("\n[probe] Ctrl+C — stopping")
        log.log("", "stop", "", "ctrl_c")
        return 0
    finally:
        log.close()


class _GameClosed(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
