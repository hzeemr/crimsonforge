# CrimsonForge Character Unlock Mod

**Game**: Crimson Desert (v1.03.00+)
**Version**: v8 (April 2026)
**Author**: hzeem / CrimsonForge

Unlocks all playable characters for all chapters. Play as Damiane, Oongka, or Yahn from the very beginning — no more "Comrade is on an important mission" or "This is Kliff's story" restrictions.

---

## What It Does

### Patch 1: Popup Bypass (ChangeCharacterNotice)
- Removes the "This is Kliff's story / Switch / Return" popup that appears when entering a chapter owned by a different character
- Allows any character to walk freely into any chapter's content
- Movement, combat, buying/selling all work normally

### Patch 2: Forbidden Character List Bypass
- Removes the "Comrade is on an important mission" lock on the character switch screen
- The game server sends a packet (`TrocTrUpdateForbiddenCharacterListAck`) that marks certain characters as unavailable during specific story chapters
- This patch makes the client ignore that packet — all characters appear as available at all times

---

## How It Works (Technical)

The mod is a DLL loaded as an ASI plugin. It patches two C++ class vtables at runtime:

### ChangeCharacterNotice Vtable
- **Class**: `UIGamePlayControl_Root_ChangeCharacterNotice`
- **RTTI**: `.?AVUIGamePlayControl_Root_ChangeCharacterNotice@uiCommonScript@pa@@`
- **Vtable RVA**: `0x48C9C30`
- **COL RVA**: `0x4F0D220`
- **Patched**: `vfunc[4]` (RVA `0xB49F40`) and `vfunc[5]` (RVA `0xB49FD0`)
- **Method**: Both replaced with a stub that returns NULL (`xor rax,rax; ret`)
- **Effect**: The HTML panel `changecharacternoticepanel.html` never activates

### ForbiddenCharacterList Vtable
- **Class**: `TrocTrUpdateForbiddenCharacterListAck`
- **RTTI**: `.?AVTrocTrUpdateForbiddenCharacterListAck@pa@@`
- **Vtable RVA**: `0x4843160`
- **COL RVA**: `0x4EE6098`
- **Patched**: `vfunc[2]` (RVA `0x981840`)
- **Method**: Replaced with a stub that returns NULL
- **Effect**: The client ignores the server's forbidden character list packet. All characters appear as available on the switch screen.

### RTTI Verification
Before patching, the mod verifies each vtable by checking that `vtable[-1]` points to the correct Complete Object Locator (COL). If the game is updated and the vtable moves, the patch safely aborts instead of crashing.

---

## Installation

### Requirements
- Crimson Desert (Steam, v1.03.00)
- The xinput1_4 ASI loader proxy (included in the CrimsonForge RTL pack, or any compatible ASI loader)

### Steps

1. **Install the ASI loader** (if not already installed):
   ```
   Copy system xinput1_4.dll -> bin64/xinput1_4_orig.dll
   Copy xinput1_4_debug_asi_loader.dll -> bin64/xinput1_4.dll
   ```

2. **Install the mod**:
   ```
   Create folder: bin64/scripts/ (if it doesn't exist)
   Copy CrimsonForgeCharUnlock.asi -> bin64/scripts/CrimsonForgeRTL.asi
   ```

3. **Launch the game** — characters are unlocked automatically

### Uninstall

- Delete `bin64/scripts/CrimsonForgeRTL.asi`
- Or verify game files through Steam (restores everything)

---

## Log Output

The mod writes a log to `bin64/crimson_charunlock.log`:

```
=== CrimsonForge Character Unlock v7 (dual vtable) ===
PID: 104152
Exe: 0x0000000140000000 size 0x185B8000
[ChangeCharacterNotice] vfunc[4]: 0xB49F40 -> stub
[ChangeCharacterNotice] vfunc[5]: 0xB49FD0 -> stub
[ChangeCharacterNotice] 2 vfuncs patched
[ForbiddenCharacterList] vfunc[2]: 0x981840 -> stub
[ForbiddenCharacterList] 1 vfuncs patched
=== Done ===
```

If the log shows "RTTI mismatch — skipped", the game has been updated and the vtable addresses need to be refreshed.

---

## Known Limitations

- **Quest ownership**: Quests already accepted by Kliff remain owned by Kliff. The Talk/Interact prompt for quest NPCs only works for the quest owner. Accept new quests as your desired character.
- **Cutscenes**: Story cutscenes may still show Kliff's model (they're pre-rendered sequences tied to specific characters).
- **Skills**: Each character has their own skill tree. Damiane's skills work normally; she doesn't gain Kliff's skills.
- **Game updates**: A game update may change vtable addresses. The mod will safely abort (no crash) but won't function until updated.

---

## Reverse Engineering Notes

### How the character lock system works

The game has multiple layers of character restrictions:

1. **Server-side forbidden list** (`TrocTrUpdateForbiddenCharacterListAck`)
   - The game server sends a packet with a list of characters marked as "forbidden" for the current story chapter
   - The client stores this list and uses it to grey out characters on the switch screen
   - Message shown: "Comrade is on an important mission"

2. **Stage chart popup** (`UIGamePlayControl_Root_ChangeCharacterNotice`)
   - When entering a stage/quest that belongs to a different character, the game shows "This is Kliff's story" with Switch/Return buttons
   - Controlled by the C++ ScriptObject bound to `changecharacternoticepanel.html`

3. **Condition checks** (`conditioninfo.pabgb`)
   - `CheckCharacterKey(Kliff)` conditions in game data tables gate NPC dialogue and UI behavior
   - 29 conditions for Kliff, 2 for Damian, 2 for Oongka
   - Can be patched via CrimsonForge's condition patcher

4. **Stage forbidden functions** (`stageinfo.pabgb`)
   - `Func_Damiane_Forbidden_*` entries (14 total) mark specific chapters as forbidden for Damiane
   - These are the data-level restrictions that feed into the server's forbidden list

5. **Character change requirements** (`characterchange.pabgb`)
   - `Job_A` through `Job_E` define stage-based unlock progression
   - `Tier` field = level requirement (default 30)
   - `Gender_A` = gender swap availability

### Key RVAs (v1.03.00)

| Item | RVA |
|------|-----|
| ChangeCharacterNotice vtable | `0x48C9C30` |
| ChangeCharacterNotice COL | `0x4F0D220` |
| ForbiddenCharacterList vtable | `0x4843160` |
| ForbiddenCharacterList COL | `0x4EE6098` |
| MercenaryQuickSlot vtable | `0x48E1430` |
| Root_Mercenary vtable | `0x4910518` |
| RootQuickSlot vtable | `0x4908410` |
| LocalStringInfoManager vtable | `0x4BA92E0` |
| BuildTextRun function | `0x3450BE0` |
| Character ownership flag | `[rbp+0x2B8]` (17 sites) |
| UI string "ImportantQuest" | `0x4BA8E78` |
| RTTI ChangeCharacterNotice | `0x5A8CF90` |
| RTTI ForbiddenCharacterList | `0x5A6DA38` |

### NonSwitchable Reasons (15 enum values)

The game has 15 reasons a character can't be switched:
1. Wanted
2. RidingVehicle
3. Stealth
4. Far
5. NotAccompanying
6. Battle
7. AlreadyCall
8. ActiveSpecialMode
9. **ImportantQuest** (the one we bypass)
10. Interacting
11. Action
12. InvalidPosition
13. Groggy
14. Dead
15. Target (variant of ImportantQuest)

---

## Building from Source

Requires Visual Studio Build Tools with C++ compiler.

```powershell
.\build_unlock.ps1
```

Output: `CrimsonForgeCharUnlock.asi`

---

## Files

| File | Description |
|------|-------------|
| `CrimsonForgeCharUnlock.asi` | The mod (rename to CrimsonForgeRTL.asi for deployment) |
| `crimson_character_unlock.c` | Source code |
| `build_unlock.ps1` | Build script |
| `README.md` | This file |

---

## Credits

- **hzeem** — CrimsonForge, reverse engineering, mod development
- **CrimsonForge** — Enterprise Crimson Desert modding studio
- Community researchers for the initial `[rbp+0x2B8]` AOB and WorldSystem findings
