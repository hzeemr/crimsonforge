# Ready-to-run: inject helper.dll into Crimson Desert

**Pre-built binaries are already in the repo.** No compilation needed.

## Quick start (3 commands)

```powershell
# 1. Launch Crimson Desert through Steam. Wait for the main menu.

# 2. From any PowerShell window (admin NOT required):
cd C:\Users\hzeem\Desktop\crimsonforge
.\tools\metabin_re\injector\injector.exe "$PWD\tools\metabin_re\helper_dll\helper.dll"

# 3. Watch the trace log populate in real time:
Get-Content "$env:USERPROFILE\Desktop\metabin_trace.log" -Wait -Tail 50
```

## Expected output from step 2

```
CrimsonDesert DLL injector
  DLL: C:\Users\hzeem\Desktop\crimsonforge\tools\metabin_re\helper_dll\helper.dll
  Target PID: 12345
  Remote thread started. Waiting for LoadLibraryA to return...
  LoadLibraryA returned HMODULE lo32 = 0x7ff8aabb0000
  DLL loaded successfully.
```

Exit code is `0` on success; non-zero error codes:

| code | meaning |
|---:|---|
| 1 | wrong number of arguments |
| 2 | can't resolve absolute DLL path |
| 3 | DLL file doesn't exist |
| 4 | CrimsonDesert.exe not running |
| 5-6 | Win32 API failure — see stderr message |
| 7 | DLL loaded partially but `LoadLibraryA` returned NULL — check log |

## What the log shows

The helper DLL writes `%USERPROFILE%\Desktop\metabin_trace.log`. On
every hook hit it records:

```
vt0.vf0 hit   this=0x000001A2FB8D3020
  this+0x00: 00 00 00 00 5c 14 bb 50 00 00 ff ff ff ff ff ff
  this+0x10: 4b 00 00 00 06 00 00 00 00 00 00 00 00 00 00 80
  ...
```

`vtN.vfM` tells you which vtable + vfunc fired (we hook the first 4
vfuncs of each of the 3 AnimationMetaData vtables = 12 total hooks).

`this=0x...` is the class-instance pointer. The hex dump that follows
is the first 128 bytes of `*this` — i.e. what the deserializer has
populated inside the instance. Cross-reference these bytes against
the raw `.paa_metabin` file to identify which metabin offsets map to
which class fields.

## Triggering hook hits

AnimationMetaData instances are created when:

  * **Switching character** (Kliff ↔ Damiane ↔ Oongka) — fresh
    animation pool loads for the new character's skeleton.
  * **Entering a new zone / fast-travelling** — zone-specific
    animation sets load.
  * **Opening a menu with animated previews** — e.g. character
    customisation, inventory with item-use animations.
  * **Playing cutscenes / dialogue** — each cutscene loads its
    own AnimationMetaData bundle.

Any of these should fire at least one vfunc hit. If nothing appears
in the log after 30 seconds of in-game activity, see Troubleshooting.

## Troubleshooting

### Injector says "CrimsonDesert.exe is not running"
Steam sometimes launches the game via an intermediate launcher
process. Wait until the game window actually appears (not the Steam
launch spinner). If it still can't find it, open Task Manager →
Details tab → verify `CrimsonDesert.exe` is listed.

### Injector says "LoadLibraryA returned NULL"
Most common cause: the DLL has a dependency the game process can't
resolve. This won't happen with the committed `helper.dll` because
it only imports `kernel32.dll`, `user32.dll`, and standard CRT —
all of which the game already has loaded.

Next most common: path is wrong. The injector resolves the DLL path
relative to the injector's CWD (not the game's). Always pass an
absolute path; the `$PWD` PowerShell variable makes this easy.

### Log file doesn't appear
The DLL writes to `%USERPROFILE%\Desktop\metabin_trace.log`. On
Windows with a non-English locale the Desktop path may differ —
the DLL falls back to `C:\metabin_trace.log` if
`ExpandEnvironmentStrings` fails, but usually the Desktop path works.

If neither appears, the DLL's `CreateFileA` failed. Check whether
your account has write permission to the Desktop. You can also run
the injector as administrator (right-click → Run as admin) to rule
out UAC issues.

### Log appears but hooks never fire
The hooks are installed on the vtable, not the function body, so
only *newly-constructed* AnimationMetaData instances route through
them. If the game cached its AnimationMetaData instances at startup
and never creates new ones during the session, you won't see hits.
Workarounds:

  * Switch character (forces a full animation-pool reload).
  * Reload the save (zone change → fresh AnimationMetaData).
  * Kill the game, relaunch, and inject before opening any menus —
    the earliest you inject, the fewer cached instances exist.

### Game updated; addresses changed
Re-run the Python analyzer and rebuild:

```powershell
# Find the new RTTI addresses:
python tools\metabin_re\pe_analyzer.py `
    --exe "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe" `
    --out tools\metabin_re\output

# Read the new vtable VAs out of output/rtti_report.json:
#   .vtables[*].vtable_va
# Edit tools/metabin_re/helper_dll/helper.c and update g_vtable_vas[] to match.
# Rebuild:
.\tools\metabin_re\build.ps1
```

## After you've seen some hits

Copy the first 200 lines of `metabin_trace.log` to a gist /
workspace chat. The byte patterns will let us deduce which offsets
hold:

  * `bone_count`
  * `frame_count`
  * The per-bone index table (the big prize — this is what the
    current heuristic parser can't recover)
  * `duration` in a canonical location

Once identified, port the offsets into `core/paa_metabin_parser.py`
replacing the heuristic extractors. The full PAA pipeline will pick
up the improvement automatically.
