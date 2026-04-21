# Quest Dialog Live Trace — Step by Step

## Setup

1. Launch **Crimson Desert** and load your save as **Damiane**
2. Go near a quest NPC that has `!` for Kliff (verify by switching to Kliff, then switch back to Damiane)
3. **DO NOT close the game**

## Attach x64dbg

4. Open `C:\Users\hzeem\Downloads\debug\release\x96dbg.exe`
5. Click **File → Attach**
6. Find **CrimsonDesert.exe** in the process list → click **Attach**
7. The game will pause — that's normal

## Set Breakpoints

8. In the x64dbg **Command** bar (bottom), type these commands one by one:

```
bp CrimsonDesert.exe+1BAB120
bp CrimsonDesert.exe+1C29760
bp CrimsonDesert.exe+1C334F0
bp CrimsonDesert.exe+7981E0
bp CrimsonDesert.exe+7987C0
bp CrimsonDesert.exe+7F3E10
bp CrimsonDesert.exe+150C930
bp CrimsonDesert.exe+150CA10
bp CrimsonDesert.exe+435520
bp CrimsonDesert.exe+435680
```

9. After setting all breakpoints, press **F9** to resume the game

## Trigger the Check

10. Switch to the game
11. **Walk toward the quest NPC** (the one with `!` for Kliff)
12. The game should **freeze** when a breakpoint hits
13. Switch back to x64dbg

## Read the Results

14. Look at the **bottom status bar** — it shows which breakpoint was hit
15. Note the address (e.g., `CrimsonDesert.exe+1BAB120`)
16. Check the **Register** panel on the right:
    - **RCX** = first parameter (usually `this` pointer)
    - **RDX** = second parameter
    - **R8** = third parameter
17. Press **F9** to continue — it may hit more breakpoints

## Record What Happens

For each breakpoint hit:
- Write down the **address** that was hit
- Write down **RCX, RDX, R8** values
- Press F9 to continue

After the game resumes normally (no more hits):
- Now **switch to Kliff** in-game
- Walk toward the same NPC
- Same breakpoints should hit but with different register values

## Compare

The breakpoint(s) that hit for Kliff but NOT for Damiane = the character check.
The register value that differs = the character ID being compared.

## Important

- If the game is too slow with all breakpoints, remove some:
  ```
  bc CrimsonDesert.exe+ADDRESS
  ```
- To remove all breakpoints:
  ```
  bpc
  ```
- To detach without closing the game:
  **Debug → Detach**

## After Tracing

Tell me:
1. Which breakpoints were hit (addresses)
2. Were any hit for Kliff but NOT for Damiane?
3. What were the RCX/RDX/R8 values?
