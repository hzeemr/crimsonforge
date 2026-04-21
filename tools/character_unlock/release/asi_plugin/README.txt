CrimsonForge Character Unlock
=============================

Unlocks all playable characters for all chapters in Crimson Desert.

WHAT IT DOES:
- Removes "This is Kliff's story" popup
- Removes "Comrade is on an important mission" lock
- Damiane available from Chapter 3 onward (all chapters)
- Oongka available from Chapter 7 onward (all chapters)
- Free roam, combat, merchants, new quests as any character

INSTALL (via CDUMM):
1. Open Crimson Desert Ultimate Mods Manager
2. Go to ASI Plugins tab
3. Click Install Plugin
4. Select CrimsonForgeCharUnlock.asi
5. CDUMM will auto-install ASI Loader if needed

INSTALL (manual):
1. Copy your system xinput1_4.dll to bin64/xinput1_4_orig.dll
2. Copy the ASI loader proxy to bin64/xinput1_4.dll
3. Create bin64/scripts/ folder
4. Copy CrimsonForgeCharUnlock.asi to bin64/scripts/CrimsonForgeRTL.asi

GAME UPDATES:
- The mod checks RTTI before patching
- If a game update moves the vtable, the mod safely disables itself
- No crash, no corruption — just stops working until updated
- Check bin64/crimson_charunlock.log for status

KNOWN LIMITATIONS:
- Quest ! marker and Talk button only work for quests accepted by the current character
- Accept new quests as Damiane for full functionality
- Cutscenes still show Kliff's model

UNINSTALL:
- Delete the ASI file, or disable in CDUMM
- Or verify game files through Steam

LOG FILE:
- bin64/crimson_charunlock.log shows patch status

Game version: v1.03.00
Mod version: v8 (April 2026)
Author: hzeem / CrimsonForge
