# Crimson Desert — Character Unlock Complete Guide

## Vanilla Game Character Availability

### 3 Playable Characters

| Character | Class | Unlock | Locked During |
|-----------|-------|--------|---------------|
| **Kliff** | Sword & Shield, versatile | Start of game (default) | Never locked |
| **Damiane** | Speed, ranged, group combat | Chapter 3 (Howling Hill, after camp) | Chapters 5-8 ("important mission") |
| **Oongka** | Heavy melee, dual-wield, tank | Chapter 7 (+ side quest "Gentle Sound of Flowing River") | Chapter 10+ ("important mission") |

### How to Switch (vanilla)
- **Keyboard**: Press F1 → quick slot wheel → select character
- **Controller**: Hold Up on D-pad → move cursor → select

### Vanilla Restrictions
1. **Damiane unlocks in Chapter 3** after Kliff sets up the Greymane camp at Howling Hill
2. **Damiane locks from Chapter 5 to Chapter 8** — shows "Comrade is on an important mission"
3. **Damiane returns at end of Chapter 8** for her mandatory solo mission (stealth + boss fight)
4. **Oongka unlocks in Chapter 7** but requires completing side quest first
5. **Oongka locks in Chapter 10+** — same "important mission" message
6. **"This is Kliff's story"** popup appears when entering Kliff-only quest content as another character
7. **Quest NPC talk** is restricted to the quest-owning character
8. **Items** can be character-locked (red X icons)

---

## What the Mod Fixes

### Fixed (Working)

| Restriction | Status | How |
|-------------|--------|-----|
| "Comrade is on an important mission" | **FIXED** | ForbiddenCharacterList packet bypass |
| "This is Kliff's story" popup | **FIXED** | ChangeCharacterNotice vtable patch |
| Damiane locked in Chapters 5-8 | **FIXED** | Server forbidden list ignored |
| Oongka locked in Chapter 10+ | **FIXED** | Server forbidden list ignored |
| Movement in other character's areas | **FIXED** | Popup bypass allows free movement |
| Buy/sell with merchants as any character | **FIXED** | Works normally |
| Accept NEW quests as any character | **FIXED** | Works normally |

### Partially Fixed

| Restriction | Status | Details |
|-------------|--------|---------|
| Quest NPC dialogue for Kliff quests | **PARTIAL** | New quests accepted as Damiane work fully. Quests already accepted by Kliff remain Kliff-owned in save data |
| CheckCharacterKey conditions | **PARTIAL** | Patchable via CrimsonForge condition patcher (29 conditions in conditioninfo + 185 in gimmickgroupinfo) |

### Not Fixed (Engine/Save Level)

| Restriction | Status | Why |
|-------------|--------|-----|
| Cutscene character models | **Not fixed** | Pre-baked cinematic data |
| Quest ownership in save files | **Not fixed** | Embedded in save data, not game files |
| Character-specific items (red X) | **Not fixed** | Item system checks character at engine level |
| Damiane unavailable before Chapter 3 | **Not fixed** | Character switching feature itself unlocks in Chapter 3 via story progression |

---

## Character Availability Timeline (with mod)

| Chapter | Kliff | Damiane | Oongka | Vanilla Damiane | Vanilla Oongka |
|---------|-------|---------|--------|-----------------|----------------|
| 1-2 | Yes | No* | No* | No | No |
| 3 | Yes | **Yes** | No* | Yes | No |
| 4 | Yes | **Yes** | No* | Yes | No |
| 5-6 | Yes | **Yes** | No* | **LOCKED** | No |
| 7 | Yes | **Yes** | **Yes** | **LOCKED** | Yes |
| 8 | Yes | **Yes** | **Yes** | **LOCKED** (until end) | Yes |
| 9 | Yes | **Yes** | **Yes** | Yes | Yes |
| 10+ | Yes | **Yes** | **Yes** | Yes | **LOCKED** |

*\*Character switching feature not yet unlocked in story (Chapter 1-2 for Damiane, Chapters 1-6 for Oongka)*

**Note**: The mod cannot make characters available before the game's character switching system is introduced in the story. Damiane requires reaching Chapter 3 where the switching mechanic is first taught. After that point, the mod keeps all characters available permanently.

---

## How the Restriction System Works

The game uses 5 layers of character restrictions:

### Layer 1: Server Forbidden List (runtime)
- **System**: `TrocTrUpdateForbiddenCharacterListAck` network packet
- **What it does**: Game server tells client which characters are forbidden for the current story chapter
- **Message**: "Comrade is on an important mission"
- **Mod fix**: Vtable patch ignores the packet

### Layer 2: Stage Popup (runtime)
- **System**: `UIGamePlayControl_Root_ChangeCharacterNotice` C++ ScriptObject
- **What it does**: Shows "This is Kliff's story" popup with Switch/Return when entering wrong-character content
- **UI**: `changecharacternoticepanel.html`
- **Mod fix**: Vtable patch stubs the panel's activate methods

### Layer 3: Stage Forbidden Functions (data)
- **System**: `stageinfo.pabgb` — `Func_Damiane_Forbidden_*` entries (14 total)
- **What it does**: Marks specific story stages as forbidden for Damiane
- **Stages**: ImpBanquet, Crowman, BrokenShield I-IV, Homecoming I-IV, BloodCoronation I-II, MusketSiege
- **Patchable**: Yes, via CrimsonForge (flip enable flag from 1 to 0)

### Layer 4: Condition Checks (data)
- **System**: `conditioninfo.pabgb` — `CheckCharacterKey(Kliff)` expressions
- **What it does**: Gates NPC dialogue, UI behavior, quest triggers by character
- **Count**: 29 Kliff conditions, 2 Damian, 2 Oongka
- **Patchable**: Yes, via CrimsonForge condition patcher

### Layer 5: Gimmick Conditions (data)
- **System**: `gimmickgroupinfo.pabgb` — 185 `CheckCharacterKey(Kliff)` entries
- **What it does**: Controls world object interactions (talk, interact, trigger) by character
- **Patchable**: Yes, via CrimsonForge condition patcher

### Layer 6: Character Change Requirements (data)
- **System**: `characterchange.pabgb` — Job_A through Job_E + Tier
- **What it does**: Defines which story stages unlock each character class
- **Note**: Patching this alone does NOT fix availability (server forbidden list overrides it)

---

## Sources

- [How to unlock each character in Crimson Desert — PC Gamer](https://www.pcgamer.com/games/action/crimson-desert-characters-unlock/)
- [All Playable Characters — GameSpot](https://www.gamespot.com/gallery/crimson-desert-playable-characters-damiane-oongka-kliff/2900-7600/)
- [How to unlock all Crimson Desert Characters — GamesRadar](https://www.gamesradar.com/games/rpg/crimson-desert-characters/)
- [Crimson Desert Guide — ClutchPoints](https://clutchpoints.com/gaming/crimson-desert-guide-how-to-unlock-damiane-and-oongka)
- [Damiane locked until after the story? — Steam Community](https://steamcommunity.com/app/3321460/discussions/0/805720464937921675/)
- [When do you get Damiane back at chapter 5 — Steam Community](https://steamcommunity.com/app/3321460/discussions/0/805720464937794701/)
- [How to Unlock Damiane — Game8](https://game8.co/games/Crimson-Desert/archives/586784)
- [How to Switch Characters — Gfinity](https://www.gfinityesports.com/article/how-to-switch-characters-in-crimson-desert-playing-as-kliff-damiane-and-oongka)
