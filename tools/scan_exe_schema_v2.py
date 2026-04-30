"""Targeted second-pass scan for characterinfo schema fields.

Strategy: rather than guess field names, mine the EXE for:
  (a) C++ mangled CharacterInfo accessor methods  -> ?GetXxx@CharacterInfo@pa@@
  (b) [CharacterInfo(%#)]: ... format strings     -> field names appear after the colon
  (c) Logging strings that mention BreakableHp / MinImpulseDamage / etc.
  (d) Sibling tables (?Info@pa@@ class names) so we know what other rows the schema joins to.
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import Counter

EXE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonDesert.exe")
OUT = Path(r"C:\Users\hzeem\Desktop\crimsonforge\tools\schema_hints.txt")

# Read once
print(f"Reading {EXE.stat().st_size/1024/1024:.1f} MB ...")
data = EXE.read_bytes()

# All printable ASCII runs >= 5
runs = [r.decode("ascii") for r in re.findall(rb"[\x20-\x7e]{5,400}", data)]
print(f"runs: {len(runs):,}")

# Dedupe
unique_runs = list(set(runs))
print(f"unique: {len(unique_runs):,}")

# ---------------------------------------------------------------
# Pass A — C++ mangled methods on CharacterInfo (and sibling Info classes)
# Pattern: ?<MethodName>@<ClassName>@pa@@
# We extract MethodName for class CharacterInfo, BossInfo, MonsterInfo, NpcInfo,
# BuffInfo, ConditionInfo, FactionInfo, etc.
# ---------------------------------------------------------------
mangled_re = re.compile(r"\?([A-Za-z_][A-Za-z0-9_]+)@([A-Za-z_][A-Za-z0-9_]*Info)@pa@@")
# Matches `.?AVClassName@pa@@` or `.?AUStructName@pa@@` anywhere in run.
pa_class_re = re.compile(r"\.\?A[VU]([A-Za-z_][A-Za-z0-9_]+)@pa@@")
# Inline template parameter:  V<Name>@pa@@   or   V<Name>@2@   (back-reference)
# In Itanium-style mangling, `@2@` is a substitution back-reference for
# namespace pa. Both forms reference a real pa-namespace class.
template_param_re = re.compile(r"V([A-Za-z_][A-Za-z0-9_]*Info)(?:@pa@@|@\d+@)")
char_methods: dict[str, set[str]] = {}
all_info_classes: set[str] = set()
all_pa_classes: set[str] = set()

for s in unique_runs:
    for m in mangled_re.finditer(s):
        method, klass = m.group(1), m.group(2)
        char_methods.setdefault(klass, set()).add(method)
        all_info_classes.add(klass)
    for m2 in pa_class_re.finditer(s):
        cls = m2.group(1)
        all_pa_classes.add(cls)
        if cls.endswith("Info"):
            all_info_classes.add(cls)
    # Pull *Info names out of template parameters too.
    for m3 in template_param_re.finditer(s):
        all_info_classes.add(m3.group(1))

# ---------------------------------------------------------------
# Pass B — log format strings of the shape "[<Class>(%#)]: <fieldname> ..."
# These are debug / error messages that name actual struct members.
# ---------------------------------------------------------------
# Match  [Class] ...   /   [Class(%#)]: ...   /   [Class(%lld)]: ...
# Class can be *Info OR *Data (ConditionData uses the same convention)
log_re = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*(?:Info|Data))(?:\([^\]]*\))?\]\s*:?\s*(.+)")
log_field_re = re.compile(r"([A-Z][A-Za-z0-9_]{2,40})")
class_log_messages: dict[str, list[str]] = {}
for s in unique_runs:
    m = log_re.search(s)
    if m:
        klass = m.group(1)
        rest = m.group(2).strip()
        if rest:
            class_log_messages.setdefault(klass, []).append(rest)

# ---------------------------------------------------------------
# Pass C — likely member-name tokens harvested from runs that mention
# CharacterInfo within the same run.
# ---------------------------------------------------------------
member_tokens: Counter = Counter()
member_candidate_re = re.compile(r"\b([A-Z][a-z][A-Za-z0-9]{2,30})\b")
for s in unique_runs:
    if "characterinfo" in s.lower() or "[CharacterInfo" in s:
        for m in member_candidate_re.finditer(s):
            tok = m.group(1)
            # Filter out obvious non-fields (English words from log msgs)
            if tok not in {
                "Failed", "Length", "Constraint", "Open", "Close", "Need", "Cannot",
                "Cant", "Read", "Write", "Found", "Check", "Update", "Insert",
                "Delete", "Init", "Destroy", "Begin", "End", "Start", "Stop",
                "True", "False", "None", "Null", "This", "That", "When", "While",
                "Where", "What", "Which", "After", "Before", "Then", "Other",
                "From", "Into", "Onto", "Some", "More", "Less", "Same", "Both",
                "Type", "Data", "List", "Item", "Value", "Count", "Size",
                "Error", "ErrNo", "Success", "Result", "Status", "Mode",
                "Static", "Dynamic", "Const", "Public", "Private", "Action",
                "Actor", "Manager", "Wrapper", "Template", "Function",
                "Object", "String", "Float", "Double", "Boolean", "Number",
                "Index", "Range", "Group", "Set", "Map", "Vector",
            }:
                member_tokens[tok] += 1

# ---------------------------------------------------------------
# Pass D — explicit field-like tokens sniffed from the entire string set
# that look like CharStat / BossStat / common MMO field names.
# ---------------------------------------------------------------
notable_fields_re = re.compile(
    r"\b(BreakableHp|MinImpulseDamage|MaxHp|MaxHP|CurHp|CurHP|"
    r"Attack|AttackPower|AttackDamage|AttackRange|AttackSpeed|"
    r"Defense|Defence|MoveSpeed|RotateSpeed|JumpHeight|"
    r"AggroRange|SightRange|HearingRange|"
    r"BoneScale|BoneScaleBuffer|BodyScale|MorphTargetSet|"
    r"WeaponType|HelmType|AccessoryType|"
    r"VehicleKey|InteractionInfo|MainVehicleCharacterKey|"
    r"DefaultGimmickCharacter|ConvertItemKey|"
    r"ApplySkillKeyBySpawn|ApplySkillKeyByRevive|"
    r"ApplySkillKeyWhenAlive|ApplySkillKeyWhenPlayer|"
    r"CharacterInfoAliveSkill|CharacterInfoPlayerSkill|"
    r"CharacterInfoSpawnSkill|"
    r"PrefabPath|MeshPath|SkeletonPath|SkeletonVariationPath|"
    r"AnimationPath|MaterialPath|TexturePath|"
    r"FactionKey|FactionInfo|GroupKey|"
    r"CharacterTier|CharacterClass|CharacterRace|CharacterGender|"
    r"DropSetKey|LootTable|ExpReward|"
    r"AINodeKey|AIChartKey|StateMachineKey|"
    r"PhysicsKey|HkxPath|RagdollKey|"
    r"LocalizationKey|LocaleKey|LocaleName)\b"
)
notable_hits: Counter = Counter()
notable_contexts: dict[str, list[str]] = {}
for s in unique_runs:
    for m in notable_fields_re.finditer(s):
        tok = m.group(1)
        notable_hits[tok] += 1
        if len(notable_contexts.get(tok, [])) < 6:
            notable_contexts.setdefault(tok, []).append(s)

# ---------------------------------------------------------------
# Pass E — does Boss_Ogre_55515 (or just 55515) appear?
# ---------------------------------------------------------------
boss_full = any("Boss_Ogre_55515" in s for s in unique_runs)
boss_id = any("55515" in s for s in unique_runs)

# ---------------------------------------------------------------
# Pass F — table names referenced as ".pabgb" or "<name>info" with paths
# ---------------------------------------------------------------
table_re = re.compile(r"([a-z_][a-z0-9_/]*?info)\.pabgb", re.IGNORECASE)
table_re2 = re.compile(r"gamedata/([a-z_][a-z0-9_]+)", re.IGNORECASE)
tables: Counter = Counter()
for s in unique_runs:
    for m in table_re.finditer(s):
        tables[m.group(1).lower()] += 1
    for m in table_re2.finditer(s):
        tables["gamedata/" + m.group(1).lower()] += 1

# ---------------------------------------------------------------
# Write findings
# ---------------------------------------------------------------
OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8") as fh:
    fh.write("# Crimson Desert characterinfo.pabgb schema hints\n")
    fh.write(f"# Source: {EXE}\n")
    fh.write(f"# Unique ASCII runs scanned: {len(unique_runs):,}\n")
    fh.write(f"# Boss_Ogre_55515 found in EXE: {boss_full}\n")
    fh.write(f"# 55515 substring found anywhere: {boss_id}\n\n")

    # --- 1. Sibling Info classes ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 1 — All `*Info` C++ classes in pa namespace\n")
    fh.write("            (each maps to a *.pabgb table type)\n")
    fh.write("=" * 70 + "\n\n")
    for k in sorted(all_info_classes):
        fh.write(f"  {k}\n")
    fh.write(f"\n(total: {len(all_info_classes)})\n\n")

    # All pa-namespace classes that mention "Character" — these are the
    # candidates for the row struct, plus related sub-records.
    char_classes = sorted(c for c in all_pa_classes if "Character" in c)
    fh.write("## All pa-namespace classes containing 'Character':\n\n")
    for c in char_classes[:200]:
        fh.write(f"  {c}\n")
    fh.write(f"\n(total: {len(char_classes)})\n\n")

    # --- 2. Methods on CharacterInfo specifically ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 2 — Mangled methods on `CharacterInfo` (and siblings)\n")
    fh.write("            method name often mirrors the underlying field name\n")
    fh.write("            (e.g. GetMaxHp, GetAttackRange, IsBreakable ...)\n")
    fh.write("=" * 70 + "\n\n")
    interesting_classes = ["CharacterInfo", "BossInfo", "MonsterInfo", "NpcInfo",
                            "BuffInfo", "ConditionInfo", "DropSetInfo", "ItemInfo",
                            "FactionInfo", "InteractionInfo"]
    for klass in interesting_classes:
        if klass in char_methods:
            ms = sorted(char_methods[klass])
            fh.write(f"## {klass}  ({len(ms)} methods)\n")
            for m in ms:
                fh.write(f"  {m}\n")
            fh.write("\n")
    # all other Info classes with methods
    other = sorted(set(char_methods.keys()) - set(interesting_classes))
    if other:
        fh.write("## Other *Info classes with extracted methods:\n")
        for klass in other:
            ms = sorted(char_methods[klass])
            fh.write(f"  {klass}: {', '.join(ms[:30])}{'...' if len(ms)>30 else ''}\n")
        fh.write("\n")

    # --- 3. CharacterInfo log messages (named field hints) ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 3 — `[CharacterInfo(%#)]: ...` log messages\n")
    fh.write("            field names + actions appear in plain text\n")
    fh.write("=" * 70 + "\n\n")
    for klass in interesting_classes + sorted(set(class_log_messages) - set(interesting_classes)):
        if klass not in class_log_messages:
            continue
        msgs = class_log_messages[klass]
        fh.write(f"## {klass}  ({len(msgs)} log messages)\n")
        for m in sorted(set(msgs))[:60]:
            fh.write(f"  {m}\n")
        fh.write("\n")

    # --- 4. Notable confirmed field-name hits ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 4 — Confirmed character-stat field names found in EXE\n")
    fh.write("=" * 70 + "\n\n")
    for tok, count in sorted(notable_hits.items(), key=lambda kv: (-kv[1], kv[0])):
        fh.write(f"  {tok:40s} (x{count})\n")
        for ctx in notable_contexts.get(tok, [])[:3]:
            ctx_short = ctx[:120].replace("\n", " ")
            fh.write(f"      e.g. {ctx_short}\n")
    fh.write(f"\n(total: {len(notable_hits)})\n\n")

    # --- 5. Most-frequent CamelCase tokens near CharacterInfo ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 5 — CamelCase tokens appearing in strings that mention\n")
    fh.write("            CharacterInfo — sorted by frequency, top 100\n")
    fh.write("=" * 70 + "\n\n")
    for tok, count in member_tokens.most_common(100):
        fh.write(f"  {tok:35s} (x{count})\n")
    fh.write(f"\n(total unique: {len(member_tokens)})\n\n")

    # --- 6. Tables seen referenced ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 6 — *.pabgb table names referenced in EXE\n")
    fh.write("=" * 70 + "\n\n")
    for t, c in tables.most_common():
        fh.write(f"  {t:50s} (x{c})\n")
    fh.write(f"\n(total: {len(tables)})\n\n")

    # --- 7. Boss_Ogre_55515 verdict ---
    fh.write("=" * 70 + "\n")
    fh.write("SECTION 7 — Specific key check\n")
    fh.write("=" * 70 + "\n\n")
    fh.write(f"  Boss_Ogre_55515 string in EXE: {boss_full}\n")
    fh.write(f"  Bare '55515' substring anywhere: {boss_id}\n")
    if boss_id:
        for s in unique_runs:
            if "55515" in s:
                fh.write(f"    {s[:200]}\n")
        fh.write("\n")

print(f"Wrote {OUT}")
print(f"Boss_Ogre_55515 in EXE: {boss_full}")
print(f"  '55515' alone: {boss_id}")
print(f"Total *Info classes: {len(all_info_classes)}")
print(f"CharacterInfo methods: {len(char_methods.get('CharacterInfo', set()))}")
print(f"Notable confirmed fields: {len(notable_hits)}")
print(f"Tables referenced: {len(tables)}")
