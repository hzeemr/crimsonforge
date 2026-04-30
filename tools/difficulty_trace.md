# Crimson Desert difficulty system — investigation trace

Date: 2026-04-25
Game build: April 2026 (post-1.22.9)
Investigator: Claude (CrimsonForge repo)

## TL;DR

- The "Difficulty" enum is exposed by the engine option **`_gameDifficultyOption`**
  (saved to `user_engine_option_save.xml` only when changed from default — your
  current XML doesn't contain it, so you are on the default = `0` = Normal/Easy).
- Difficulty is queried by gameplay scripts via the condition
  **`GetDifficultyOption()`**.
  The only condition rows are `GetDifficultyOption()<2` and `>=2`, so the enum
  has **at least 3 levels (0, 1, 2)** — almost certainly Easy/Normal/Hard.
- Difficulty SCALING is done by **applying a buff** from
  `gamedata/buffinfo.pabgb` to the actor:
  - `BuffLevel_Difficulty`        (hash `0x000F4354`) — generic NPC scaling
  - `BuffLevel_Difficulty_Boss`   (hash `0x000F4355`) — boss-specific scaling
  - `BuffLevel_Difficulty_PC`     (hash `0x000F4356`) — player-character scaling
  - `BuffLevel_AIDifficulty`      (hash `0x000F4278`) — AI behavior tuning
- The buff has **per-difficulty-level sub-blocks** baked into one row. Multiple
  difficulty levels live in the same row, indexed by the current
  `_gameDifficultyOption` value at runtime.
- The **buff is wired into the actor at characterinfo-row level** — i.e. each
  NPC's `_gameDifficultyBuffInfo` field stores the BuffLevel hash. This is
  applied at SPAWN, not every frame. **2188 NPC rows reference Difficulty,
  24 named bosses reference Difficulty_Boss.**
- **The Ogre (`Boss_Ogre_55515`) is NOT in any difficulty buff list.** The
  Ogre is a scripted story boss whose stats are baked directly into its row
  and modified by the mission/sequencer layer, not by the global difficulty
  buff. Editing Ogre stats requires editing `Boss_Ogre_55515` directly — not
  the difficulty system.

## Where the data lives

### 1. Difficulty option storage

| Where               | What                                   |
|---------------------|----------------------------------------|
| EXE string          | `_gameDifficultyOption` (offset 0x04A87910) |
| EXE class           | `EnumOptionGameDifficultyOption`, `EnumSelectGameDifficultyOption` |
| User XML file       | `user_engine_option_save.xml` — would appear inside `<EngineOptionLanguage Name="_languageOption">` block as a child like `<EnumSelectGameDifficultyOption Name="_gameDifficultyOption" _select="..."/>` if changed from default |
| Network RPC         | `TrocTrChangeGameDifficultyReq` (server-validated) |

### 2. Scaling tables

`gamedata/buffinfo.pabgb` (group `0008`) — header-flavour `hashed`, 279 rows.

| Row | Name | Hash | Size (bytes) | Fields | Multiplier slots found |
|----:|------|------|-------------:|-------:|-----------------------|
| 1   | `BuffLevel_Difficulty`      | `0x000F4354` | 1221 | 299 | fp32 50.5, 50.75 |
| 2   | `BuffLevel_Difficulty_Boss` | `0x000F4355` | 1537 | 377 | fp32 48.5, 50.5, 50.75, 58.75 |
| 3   | `BuffLevel_Difficulty_PC`   | `0x000F4356` |  478 | 113 | fp32 50.0 |
| 44  | `BuffLevel_AIDifficulty`    | `0x000F4278` |  911 | 221 | fp32 49.0, 60.5 |

These rows contain **multiple stat-block sub-records**, each preceded by a buff
hash reference into the same buffinfo table:

```
BuffLevel_Difficulty_Boss raw (excerpts):
  off= 1014  fp32  48.5000     ← stat-block #1 multiplier (Easy?)
  off= 1020  u32   0x000F4242  ← child buff hash
  off= 1170  fp32  50.5000     ← stat-block #2 multiplier (Normal?)
  off= 1176  u32   0x000F424A  ← child buff hash
  off= 1326  fp32  50.7500     ← stat-block #3 multiplier (Hard?)
  off= 1332  u32   0x000F424B  ← child buff hash
```

The pattern `[multiplier:fp32][child_buff_hash:u32]` repeats per difficulty
level. The `child_buff_hash` values point to *other* rows in `buffinfo.pabgb`
(e.g. an `IncreaseHp` or `IncreaseDamage` buff). The fp32 in front is the
**strength** of that effect at this difficulty.

> NOTE: 48.5 / 50.5 / 50.75 do NOT mean "1.5x HP". PA's buff system uses
> additive percentage stacks. 50.5 means "+50.5% of base", which combined
> with a base 1.0 would give a 1.505x effective HP. The fact that the three
> tier values are 48.5 / 50.5 / 50.75 is suspiciously flat — it suggests
> the engine layers MULTIPLE buffs (one per difficulty level) and only one
> is active at a time, or the Boss table tier-spread is intentionally narrow
> for normal play. Will need in-game memory delta to confirm.

### 3. Where the buff is applied

Each `CharacterInfo` row has a property `_gameDifficultyBuffInfo` (EXE string at
offset 0x04AE3311) that stores the buff key. From the xref scan:

| Buff                   | Carriers in characterinfo | Carriers in charactergroupinfo |
|------------------------|--------------------------:|-------------------------------:|
| BuffLevel_Difficulty       | 2188 | 112 |
| BuffLevel_Difficulty_Boss  | 24   | 213 |
| BuffLevel_Difficulty_PC    | 0    | 109 |
| BuffLevel_AIDifficulty     | 2    | 428 |

The `Difficulty_PC` count of 0 in characterinfo + 109 in charactergroupinfo
suggests it's wired through the *group* layer (player loadout / mercenary
party).

### 4. The 24 named bosses with data-driven difficulty

```
row 5937  Caliburn_Clone
row 6001  Boss_EntHunter_SwordTowerShield_52851
row 6003  Boss_Nivalis_TwoHandSword_53115
row 6009  Boss_GearhornRENW_55510
row 6035  MiddleBoss_Balthazar_52302
row 6036  MiddleBoss_TheFaceless_DualDagger_52451
row 6037  MiddleBoss_Tristan_SwordShield_52601
row 6042  Boss_AncientPriscus_60014
row 6043  Boss_AncientPraevus_55516
row 6044  Boss_AncientPrimus_60015
row 6061  MiddleBoss_GoldenKnightMerrick_TwoHandHammer_55037
row 6675  MiddleBoss_Kailok_OneHandSword_50856
row 6676  MiddleBoss_Past_Kailok_1
row 6689  Boss_CrowCaller_DualDagger_53183
row 6690  Boss_CrowCaller_DualDagger_53183_1
row 6714  Boss_Ludvig_OneArmed
row 6718  Boss_FlameMyurdin_OneHandSword_54002
row 6722  Boss_Caliburn_OnehandSword
row 6725  MiddleBoss_Silver_Armor_1
row 6731  Boss_Old_Kliff_SwordShield_55111
row 6739  Boss_Hexe_Marie_51312
row 6746  Boss_Caliburn_PreAwakening
row 6749  Possesion_Myurdin_OneHandSword_1
row 6759  MiddleBoss_Stefan_Lanford_SwordShield_53261
```

Note the absence of `Boss_Ogre_55515`. The Ogre uses **bespoke per-row stats**.

## Where to PATCH

To globally change difficulty multipliers for the entire game:

**Edit `gamedata/buffinfo.pabgb` rows 1, 2, 3, 44.** Specifically for boss
scaling, edit row 2 (`BuffLevel_Difficulty_Boss` hash `0x000F4355`):

- Field [247] @ off=1014, fp32 = `48.5` — Easy boss multiplier %
- Field [286] @ off=1170, fp32 = `50.5` — Normal boss multiplier %
- Field [325] @ off=1326, fp32 = `50.75` — Hard boss multiplier %

These are multiplier strengths (additive %). To make Hard noticeably harder
than Normal, change `50.75` to e.g. `100.0` (= +100% HP/dmg above base).

**Round-trip safety:** The repo's existing `core/pabgb_parser.py` v1.22.10
splice-on-dirty mechanic preserves byte-exact layout for unmodified fields,
so editing only fp32 fields in this row is safe.

**Caveat:** Because the Ogre and many quest bosses are NOT in this list,
patching the difficulty buffs will *not* affect the user's Ogre fight.
For Ogre-specific edits, continue editing `Boss_Ogre_55515` directly in
`characterinfo.pabgb` (already documented in `OGRE_DEEP_TRACE.md`).

## When does the multiplier apply?

**At spawn.** Evidence:

1. The buff is stored in `_gameDifficultyBuffInfo` on `CharacterInfo`.
   `CharacterInfo` is consumed at spawn time by the engine when an actor is
   instantiated.
2. The buff system in PA games applies effects through `BuffComponent` which
   evaluates only on stat-recalc events (spawn, equip change, level change).
3. There is no per-frame `Update()` reference to `_gameDifficultyOption`
   in the EXE — the option is read once when the buff is queued, not every
   frame.
4. The `TrocTrChangeGameDifficultyReq` RPC implies the difficulty change is
   transactional and probably re-spawns affected actors when applied
   mid-session (PA's standard pattern).

So **editing the multipliers in `buffinfo.pabgb` and then forcing the user to
trigger a respawn (e.g. fast travel, scene reload) is sufficient** to see the
new values without restarting the game. Existing actors keep the old buff
strength until they're rebuilt.

## What is in the user's save?

The user's save files are PA's standard encrypted format:

- `slot0/lobby.save` — 506 bytes, header `SAVE` then encrypted payload
- `slot0/save.save` — 1,236,220 bytes, same format

Plain-text scan for `Difficulty`, `_gameDifficulty`, `GameDifficulty`,
`DifficultyOption`, `_difficulty` returned **zero hits in either file**. The
contents are encrypted. We cannot read the user's chosen difficulty from
the save without implementing the PA save crypto.

Likewise `user_engine_option_save.xml` does NOT contain a difficulty entry,
which means the user is on the **default value** for `_gameDifficultyOption`
— most likely `0` (the engine omits options at default). Best inference:
**user is on Normal**.

## Sequencer / quest layer

The Ogre quest sequencer (`cd_seq_quest_ogre_9000.paseqc`) was scanned for
difficulty references. Only `_gamePlayLevel` appears (level-of-the-world
gimmick, NOT the difficulty option). No `if-difficulty=Hard-then-spawn-X`
switches. The Ogre's challenge level is fully encoded in its `Boss_Ogre_55515`
characterinfo row — confirming the Ogre patch path is row-edit, not
difficulty-system.

## Files of evidence

All under `tools/`:

- `difficulty_strings.txt`  — 110 filtered EXE string hits
- `difficulty_fp32.txt`     — fp32 hunt around EXE difficulty regions (RTTI noise mostly)
- `difficulty_enum_dump.txt` — narrowed enum/property string contexts
- `difficulty_pabgb_hits.txt` — every pabgb table with difficulty-shaped row names
- `difficulty_table_rows.txt` — first rows of each candidate pabgb
- `difficulty_focused_rows.txt` — full field dumps of the 6 key rows
- `difficulty_buff_levels.txt` — hex dumps + fp32 candidates of the 4 buff rows
- `difficulty_buff_xrefs.txt` — count of every pabgb row that references each buff hash
- `difficulty_boss_carriers.txt` — the 24 boss characterinfo rows that carry Difficulty_Boss
- `save_difficulty_dump.txt`  — confirms saves are encrypted, no plaintext difficulty
