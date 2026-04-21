# AnimationMetaData reverse-engineering toolkit

Tools for cracking the `.paa_metabin` typed-binary schema by inspecting
CrimsonDesert.exe statically and instrumenting it at runtime.

## What's here

```
tools/metabin_re/
├── pe_analyzer.py          Python — static RTTI scan of CrimsonDesert.exe
├── injector/
│   └── injector.c          C      — external DLL injector (CreateRemoteThread)
├── helper_dll/
│   └── helper.c            C      — DLL that hooks AnimationMetaData vfuncs
├── build.ps1               PowerShell — builds injector + helper
├── output/                 generated — PE analysis results + x64dbg script
└── README.md               this file
```

## Three approaches, use whichever works

The game uses Denuvo anti-tamper which blocks the usual DLL-proxy-loading
trick (see `CLAUDE.md` for the history — xinput1_4.dll, winmm, version,
winhttp proxies all failed). The approaches below avoid proxy loading:

### Approach 1 — Static PE analysis (always works, no game needed)

Run the Python analyzer on a copy of CrimsonDesert.exe:

```powershell
python tools/metabin_re/pe_analyzer.py `
    --exe "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe" `
    --out tools/metabin_re/output
```

Output:

- **`output/rtti_report.json`** — machine-readable class data. Contains
  the TypeDescriptor VA, every COL VA, every vtable VA, and the first
  32 vfunc addresses per vtable.

- **`output/vtable_dump.txt`** — human-readable listing of the above.

- **`output/breakpoint_script.x64dbg`** — ready-to-run x64dbg script.
  Sets logging breakpoints on every vfunc and logs `rcx` (the `this`
  pointer) + the first 32 bytes of the class instance whenever a
  vfunc is called.

After each game update, re-run the analyzer to refresh the addresses.

### Approach 2 — x64dbg + ScyllaHide (live, manual)

**Prerequisites**

1. `x64dbg.exe` ([download](https://x64dbg.com))
2. `ScyllaHide` plugin with the **Denuvo x64 profile** enabled
   (see `CLAUDE.md` → Debugging Setup section)

**Steps**

1. Launch Crimson Desert normally through Steam.
2. Wait for the game to reach the main menu (Denuvo decrypts the
   code pages during launch; you need them decrypted before attaching).
3. In x64dbg: **File → Attach → CrimsonDesert.exe** → press **F9**
   until the debugger stops complaining.
4. Open the script: **Plugins → Script → Load →** select
   `tools/metabin_re/output/breakpoint_script.x64dbg`.
5. Press the **Run** button in the script panel. Every breakpoint is
   set as **log-only** (no stop), so the game continues running.
6. In-game, trigger a fresh AnimationMetaData load: switch character,
   enter a cutscene, or play a new animation.
7. Watch the **Log** tab in x64dbg — you'll see lines like:
   ```
   vt0.vf0 this=<ptr> bytes=ff ff 04 00 ...
   ```
   Each line captures the class instance at the moment the vfunc runs.
8. Save the log (**File → Save → Log as...**) and diff against a known
   metabin file to reverse-engineer the field layout.

### Approach 3 — Injected DLL (live, automatic tracing)

The injector + helper DLL combination does what x64dbg does, but
without you needing to babysit a debugger.

**Build the two artifacts** (one-time setup):

```powershell
# From a regular PowerShell prompt (x64 Native Tools or MinGW must be on PATH)
cd C:\Users\hzeem\Desktop\crimsonforge
.\tools\metabin_re\build.ps1
```

If neither MSVC nor MinGW is available, the script tells you where to
get them. The two resulting files:

- `tools/metabin_re/injector/injector.exe`
- `tools/metabin_re/helper_dll/helper.dll`

**Run it:**

1. Launch Crimson Desert normally. Wait for the main menu.
2. In a PowerShell window (doesn't need admin):
   ```powershell
   cd C:\Users\hzeem\Desktop\crimsonforge\tools\metabin_re
   .\injector\injector.exe "$PWD\helper_dll\helper.dll"
   ```
3. Expected output:
   ```
   CrimsonDesert DLL injector
     DLL: C:\Users\hzeem\Desktop\crimsonforge\tools\metabin_re\helper_dll\helper.dll
     Target PID: 12345
     Remote thread started. Waiting for LoadLibraryA to return...
     LoadLibraryA returned HMODULE lo32 = 0x7ff8aabb0000
     DLL loaded successfully.
   ```
4. A file appears on your Desktop: **`metabin_trace.log`**. Watch it
   with `Get-Content .\Desktop\metabin_trace.log -Wait` in PowerShell,
   or open in Notepad++.
5. Trigger animation loads in-game — the log will show vfunc hits
   with `this` pointer dumps.

## What you're looking for

The metabin's per-file data block (offset `0x50+`) contains per-animation
bone-index and bone-count fields that we haven't mapped yet. The
**vfunc most likely to deserialise these** is typically `vfunc[0]` or
`vfunc[2]` of the main vtable (typical MSVC C++ vtable layouts reserve
`vfunc[0]` as the destructor and put the serialiser/reader as one of the
first named virtuals).

Once a hook fires, the `this` pointer in rcx + 0..0x80 bytes of the
instance will show you the field values that correspond to what the
runtime parsed out of the metabin. Cross-reference those values against
the raw metabin bytes to deduce the schema.

## After you've cracked it

Patch the format understanding into `core/paa_metabin_parser.py` —
replace the heuristic extractors with structured field reads, and the
`AnimationExportPipeline` will automatically pick up the improvement
(the pipeline already consults `parse_metabin` on every export).

## Known gotchas

- **Denuvo** decrypts code pages on demand. An attached debugger must
  wait until the game has fully started (main menu) before setting
  breakpoints in previously-encrypted regions.
- **Game updates** move all addresses. Re-run `pe_analyzer.py` after
  every update and rebuild `helper.dll` with the new hardcoded VAs.
- **Vtable hooks** only fire for instances created *after* hooking.
  Existing AnimationMetaData instances retain their old vtable
  pointers (the vtable itself was modified, but C++ reads the
  pointer-to-vtable from the object each call — so this does in fact
  affect existing instances).
- **Log file location**: `%USERPROFILE%\Desktop\metabin_trace.log`.
  The DLL opens it with `CREATE_ALWAYS` so each injection truncates
  the previous log.

## Current hardcoded addresses (post Apr 2026 update)

```
TypeDescriptor VA : 0x145B5A6B8
COL 0             : 0x145036468
COL 1             : 0x145036720
COL 2             : 0x145036748
Vtable 0 VA       : 0x144C87298  (main)
Vtable 1 VA       : 0x144C87810  (secondary)
Vtable 2 VA       : 0x144C87288  (interface)
```

If these no longer match your game version, run `pe_analyzer.py` to
regenerate them and update `helper_dll/helper.c` accordingly.
