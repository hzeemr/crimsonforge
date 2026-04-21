# Debugging Crimson Desert with ScyllaHide

Crimson Desert uses **Denuvo** anti-tamper which blocks standard debugger attachment. ScyllaHide bypasses these protections.

## Setup (already done)
- x64dbg installed at: `C:\Users\hzeem\Downloads\debug\release\`
- ScyllaHide plugin installed in x64dbg plugins folder
- Denuvo profile configured in scylla_hide.ini

## Step by Step

### 1. Launch x64dbg FIRST
- Open `C:\Users\hzeem\Downloads\debug\release\x96dbg.exe`
- Wait for it to load

### 2. Configure ScyllaHide
- Go to **Plugins → ScyllaHide → Options**
- Set profile to **Denuvo x64**
- Check **ALL boxes** especially:
  - PEB → BeingDebugged, HeapFlags, NtGlobalFlag, StartupInfo
  - NtClose, NtQueryInformationProcess, NtQueryObject
  - NtSetInformationThread, NtGetContextThread, NtSetContextThread
  - NtQuerySystemInformation, NtCreateThreadEx
  - DLL Stealth, DLL Normal
  - KiUserExceptionDispatcher, NtContinue
- Click **OK**

### 3. Attach to the game
- **Launch Crimson Desert normally** (through Steam)
- Wait for the game to fully load (get to the main menu or in-game)
- In x64dbg: **File → Attach** → select **CrimsonDesert.exe**
- ScyllaHide will automatically hide the debugger from Denuvo
- Press **F9** (Run) multiple times until the game unfreezes

### 4. If the game crashes on attach
- Close everything
- In x64dbg: **Options → Preferences → Events**
  - Uncheck: System Breakpoint
  - Uncheck: TLS Callbacks
  - Uncheck: Entry Breakpoint
  - Uncheck: Module Entry
- Try again

### 5. Set breakpoints for quest dialog tracing
Once the game is running under the debugger:
```
bp CrimsonDesert.exe+1BAB120
```
This is the HasQuestDialog.Evaluate function.

Then walk near a quest NPC — the debugger will break.

### 6. If breakpoints crash the game
Denuvo may detect code modifications from software breakpoints.
Use **hardware breakpoints** instead:
- Right-click the address → **Breakpoint → Set Hardware on Execution**
- Hardware breakpoints use CPU debug registers, undetectable by Denuvo

## What to Look For

When the breakpoint hits on `HasQuestDialog.Evaluate` (0x1BAB120):
1. Check **RCX** register = the condition object
2. Check **[RCX+0x10]** in the dump = quest dialog data pointer
3. **As Kliff**: [RCX+0x10] should be non-NULL (has quest dialog)
4. **As Damiane**: [RCX+0x10] should be NULL (no quest dialog)
5. Find the quest dialog data structure and copy it

## The Goal

Find the `TrocTrChangePlayerbleCharacterReq` handler and trace what check it performs before the server rejects the switch. The server sends `TrocTrChangePlayerbleCharacterFailAck` — we need to know WHY.
