"""Deep cross-character analysis of characterinfo.pabgb fields.

Reads ogre_row_dump.csv plus sibling dumps, classifies each field with
the best evidence we have:

  * REAL float vs ASCII-text-misclassified-as-float
    (the heuristic in pabgb_parser flags any u32 whose bytes look like
    a 0.0001-100000 float -- but ASCII digit runs '2569' encode as
    0.000174 too. We separate these.)
  * Zero / padding
  * Likely small-int enum / count
  * Likely 32-bit FNV / Pearl-Abyss hash (foreign key)
  * Stable-across-characters (likely structural / format constant)
  * Variable-across-characters (likely a real per-character value)
  * Adjacent identical values (likely array, default fill, or
    duplicate flag)

Writes a single human-readable report at:
  tools/ogre_field_analysis.md

Run: python tools/analyze_ogre_row.py
"""
from __future__ import annotations

import csv
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"

OGRE_CSV     = TOOLS / "ogre_row_dump.csv"
SIBLING_CSVS = [
    TOOLS / "sibling_0_Boss_Geumgangbulgwe_1_row_dump.csv",
    TOOLS / "sibling_1_Boss_Caliburn_Awakening_Clone_3_row_dump.csv",
]

# Field names CONFIRMED from CrimsonDesert.exe string scan
# (tools/schema_hints.txt sections 3-4).
KNOWN_FIELD_VOCAB = [
    # Order roughly matches what the schema_hints derivation suggests
    "CharacterTier", "CharacterKey", "FactionKey", "FactionInfo",
    "PrefabPath", "SkeletonPath", "SkeletonVariationPath",
    "MorphTargetSet", "LocalizationKey",
    "MaxHp", "BreakableHp", "MinImpulseDamage",
    "AttackPower", "Defence", "MoveSpeed",
    "AggroRange", "SightRange",
    "VehicleKey", "MainVehicleCharacterKey", "DefaultGimmickCharacter",
    "InteractionInfo", "ConvertItemKey", "DropSetKey",
    "ApplySkillKeyBySpawn", "ApplySkillKeyByRevive",
    "ApplySkillKeyWhenAlive", "ApplySkillKeyWhenPlayer",
    "CharacterInfoAliveSkill", "CharacterInfoPlayerSkill",
    "CharacterInfoSpawnSkill",
]

# Known per-field hypotheses from the previous bisection round
# (these are values OBSERVED on the Boss_Ogre_55515 row + matched to
# in-game stats by the user during gameplay testing).
PRIOR_HYPOTHESES: dict[int, str] = {
    18:  "MaxHp                = 3008.0    (verified — matches in-game HP bar)",
    24:  "AggroRange           = 13.97     (verified — boss aggros at this radius in m)",
    29:  "MoveSpeed            = 0.275     (matches MoveSpeed string in EXE)",
    155: "??? scale-like 2.3   = 2.3       (suspected scale, but EDITING IT DID NOT VISUALLY RESIZE THE BOSS — likely a different effect, see notes)",
    161: "Defence              = 81.0      (matches Defence string in EXE)",
    176: "AttackPower / Damage = 32.25     (matches Attack string family)",
    179: "AttackRange A        = 2.0       (paired with 191)",
    191: "AttackRange B        = 2.0       (paired with 179 -- two attack ranges? min/max? or different attacks?)",
    608: "SpawnPos.x           = 54.0",
    610: "SpawnPos.y           = 51.65",
    612: "SpawnPos.z           = 51.90     (from ogre quest sequencer .paseqc cross-ref)",
}


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_ascii_digit_run(raw_hex: str) -> bool:
    """True if all 4 bytes are printable ASCII (digit/letter/punct).

    These are almost certainly part of a multi-chunk string, not a real
    numeric value. 0x20..0x7e range.
    """
    if len(raw_hex) != 8:
        return False
    bs = bytes.fromhex(raw_hex)
    return all(0x20 <= b < 0x7f for b in bs)


def is_likely_string_header(raw_hex: str) -> tuple[bool, int]:
    """Some 'u32' fields are actually [strlen:u32] preceding a string
    that the parser missed. If the value is a small length AND the
    next field's bytes look ASCII, this is probably a string header.
    Returns (is_string_header, declared_length).
    """
    if len(raw_hex) != 8:
        return False, 0
    val = int.from_bytes(bytes.fromhex(raw_hex), "little")
    if 1 <= val <= 200:
        return True, val
    return False, 0


def is_zero(row: dict) -> bool:
    return row["raw_hex"] == "00000000"


def looks_like_hash(row: dict) -> bool:
    """Big u32 with no obvious numeric meaning => probable PA name hash."""
    if row["kind"] != "u32":
        return False
    val = int(row["value_u32"]) if row["value_u32"] else 0
    if val < 0x10000:
        return False
    if is_ascii_digit_run(row["raw_hex"]):
        return False
    return True


def classify(field_idx: int, ogre: dict, sibs: list[dict | None]) -> str:
    raw = ogre["raw_hex"]
    kind = ogre["kind"]

    # Sentinel
    if is_zero(ogre):
        return "ZERO (padding/unset)"

    # ASCII-text false-positive (parser misclassified a multi-chunk
    # string fragment as f32 because its bytes happened to fall in
    # the "looks_like_float" range)
    if kind == "f32" and is_ascii_digit_run(raw):
        bs = bytes.fromhex(raw)
        return f"ASCII-TEXT (mis-typed as f32; bytes = {bs!r})"

    # Real floating-point stat
    if kind == "f32":
        return f"REAL f32 = {float(ogre['value_f32']):g}"

    # String
    if kind == "str":
        return f"STRING = {ogre['value_str']!r}"

    # u32 cases
    if kind == "u32":
        val = int(ogre["value_u32"])
        if is_ascii_digit_run(raw):
            bs = bytes.fromhex(raw)
            return f"ASCII-TEXT chunk (bytes = {bs!r})"
        is_str_hdr, slen = is_likely_string_header(raw)
        if is_str_hdr and slen <= 200:
            return f"u32 = {val}  (small -- maybe enum or string-length prefix)"
        if val == 0xFFFFFFFF:
            return "0xFFFFFFFF (sentinel / 'none')"
        if val < 0x10000:
            return f"small u32 = {val}  (enum/count/index?)"
        if val == 0xFF000000 or val == 0x00FF00FF or val == 0xFFFFFF00:
            return f"u32 = 0x{val:08X}  (likely RGBA color or bitmask)"
        # Likely PA hash
        return f"PA hash 0x{val:08X}  (foreign-key probably)"

    return f"{kind} = {raw}"


def build_per_field_report(
    ogre_rows: list[dict], sib_tables: list[list[dict]]
) -> str:
    lines: list[str] = []
    lines.append("# Boss_Ogre_55515 — characterinfo.pabgb deep-trace")
    lines.append("")
    lines.append("Row index 6168, hash 0x000F492A, 2,478 bytes, 615 fields parsed.")
    lines.append("")
    lines.append("## Reading guide")
    lines.append("")
    lines.append(
        "Each field is reported with: index, byte offset, parser-decided "
        "kind, raw hex, decoded value, classification."
    )
    lines.append(
        "Parser caveat: the heuristic in `core/pabgb_parser.py` flags "
        "any 4-byte chunk that *looks like* a 0.0001-100000 float as "
        "f32. Multi-chunk ASCII text (e.g. an embedded 16-digit hash "
        "string `4302569388113968`) gets sliced into 4 'f32' fields "
        "by accident. The classification column flags those as "
        "**ASCII-TEXT** so you don't try to edit them as numbers."
    )
    lines.append("")
    lines.append(
        "Per-field cross-character delta uses two siblings: "
        "**Geum** = Boss_Geumgangbulgwe_1, **Cali** = "
        "Boss_Caliburn_Awakening_Clone_3."
    )
    lines.append("")

    # Map sibling rows by index (they may have different field counts).
    sib_index_maps: list[dict[int, dict]] = [
        {int(r["field_idx"]): r for r in tbl} for tbl in sib_tables
    ]

    # ── Section 1: high-confidence / known fields
    lines.append("## Section A — Verified-by-prior-research fields")
    lines.append("")
    for idx in sorted(PRIOR_HYPOTHESES):
        if idx >= len(ogre_rows):
            continue
        ogre = ogre_rows[idx]
        lines.append(f"### Field [{idx:3d}] @ offset {ogre['offset']}")
        lines.append(f"- Hypothesis: **{PRIOR_HYPOTHESES[idx]}**")
        lines.append(f"- Kind / raw: `{ogre['kind']}` `{ogre['raw_hex']}`")
        ours = ogre['value_f32'] or ogre['value_u32'] or ogre['value_str']
        lines.append(f"- Ogre value: `{ours}`")
        for tag, sm in zip(["Geum", "Cali"], sib_index_maps):
            other = sm.get(idx)
            if other:
                ov = other['value_f32'] or other['value_u32'] or other['value_str']
                lines.append(f"- {tag} at same idx: `{ov}` ({other['kind']})")
        lines.append("")

    # ── Section 2: Big stat-table view
    lines.append("## Section B — Real f32 stats (excluding ASCII-text false positives)")
    lines.append("")
    lines.append("These are the 4-byte chunks the parser called f32 whose")
    lines.append("bytes are *not* pure ASCII -- so they're plausibly real")
    lines.append("numeric stats / scales / rates the engine reads as floats.")
    lines.append("")
    lines.append("| idx | offset | value | Geum | Cali | notes |")
    lines.append("|-----|--------|-------|------|------|-------|")
    for idx, ogre in enumerate(ogre_rows):
        if ogre["kind"] != "f32":
            continue
        if is_ascii_digit_run(ogre["raw_hex"]):
            continue
        oval = ogre["value_f32"]
        gv = sib_index_maps[0].get(idx)
        cv = sib_index_maps[1].get(idx)
        gv_s = (gv["value_f32"] or gv["value_u32"] or "?") if gv else "-"
        cv_s = (cv["value_f32"] or cv["value_u32"] or "?") if cv else "-"
        note = PRIOR_HYPOTHESES.get(idx, "")
        lines.append(
            f"| {idx} | {ogre['offset']} | {oval} | {gv_s} | {cv_s} | {note} |"
        )
    lines.append("")

    # ── Section 3: hashes (foreign keys)
    lines.append("## Section C — Likely PA-hash foreign keys")
    lines.append("")
    lines.append(
        "Big u32 values that aren't ASCII fragments and aren't small "
        "enums. Each row in characterinfo joins to many other tables "
        "via these 32-bit name hashes. Same field index across "
        "characters with DIFFERENT hashes -> per-character key. Same "
        "hash across characters -> shared default."
    )
    lines.append("")
    lines.append("| idx | offset | Ogre 0x | Geum 0x | Cali 0x | guess |")
    lines.append("|-----|--------|---------|---------|---------|-------|")
    hash_rows = []
    for idx, ogre in enumerate(ogre_rows):
        if not looks_like_hash(ogre):
            continue
        ov = int(ogre["value_u32"])
        gv = sib_index_maps[0].get(idx)
        cv = sib_index_maps[1].get(idx)
        gv_h = f"{int(gv['value_u32']):08X}" if gv and gv["value_u32"] else "-"
        cv_h = f"{int(cv['value_u32']):08X}" if cv and cv["value_u32"] else "-"
        # Cheap guess: if all three are the SAME value, probably a
        # default constant. If all three differ, character-specific FK.
        same_g = gv and gv["value_u32"] and int(gv["value_u32"]) == ov
        same_c = cv and cv["value_u32"] and int(cv["value_u32"]) == ov
        if same_g and same_c:
            guess = "constant across chars (default/sentinel)"
        elif not same_g and not same_c:
            guess = "per-character FK"
        else:
            guess = "varies in some characters"
        hash_rows.append(
            f"| {idx} | {ogre['offset']} | {ov:08X} | {gv_h} | {cv_h} | {guess} |"
        )
    lines.extend(hash_rows[:120])
    if len(hash_rows) > 120:
        lines.append(f"\n_({len(hash_rows) - 120} more hash-like fields not shown — see CSV)_")
    lines.append("")

    # ── Section 4: full per-field log (compact)
    lines.append("## Section D — Compact per-field listing (all 615)")
    lines.append("")
    lines.append("```")
    lines.append("idx  off  kind raw       Ogre               Class")
    lines.append("---- ---- ---- --------- ------------------ -------------------------------------------------")
    for idx, ogre in enumerate(ogre_rows):
        c = classify(idx, ogre, [sib_index_maps[0].get(idx), sib_index_maps[1].get(idx)])
        v = (ogre["value_f32"] or ogre["value_u32"] or ogre["value_str"] or "")[:18]
        lines.append(
            f"{idx:4d} {int(ogre['offset']):4d} {ogre['kind']:<4s} {ogre['raw_hex']} {v:<18s} {c}"
        )
    lines.append("```")

    return "\n".join(lines) + "\n"


def main() -> None:
    if not OGRE_CSV.exists():
        print(f"Missing {OGRE_CSV} -- run tools/dump_ogre_row.py first.")
        return

    ogre = load_csv(OGRE_CSV)
    sibs = []
    for p in SIBLING_CSVS:
        if p.exists():
            sibs.append(load_csv(p))
        else:
            sibs.append([])

    report = build_per_field_report(ogre, sibs)
    out = TOOLS / "ogre_field_analysis.md"
    out.write_text(report, encoding="utf-8")
    print(f"Wrote {out} ({len(report.splitlines())} lines)")
    real_floats = sum(
        1 for r in ogre
        if r["kind"] == "f32" and not is_ascii_digit_run(r["raw_hex"])
    )
    ascii_floats = sum(
        1 for r in ogre
        if r["kind"] == "f32" and is_ascii_digit_run(r["raw_hex"])
    )
    print(f"  {real_floats} real f32 stats, "
          f"{ascii_floats} ASCII-misclassified-as-f32 false positives")


if __name__ == "__main__":
    main()
