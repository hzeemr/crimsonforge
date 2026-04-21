# Face Morph / Shapekeys — Reverse Engineering Findings

## Investigation scope

Ran `morph_scan.py` against every character head / eye / beard PAC we had:

  * cd_ptm_00_head_0001.pac       (232,719 bytes)
  * cd_ptm_00_head_0003.pac       (224,661 bytes)
  * cd_ptm_00_head_sub_00_0001.pac (232,626 bytes)
  * cd_ptm_00_head_sub_00_0002.pac (554,645 bytes)
  * cd_ppdm_00_eyeleft_00_0001.pac ( 15,357 bytes)
  * cd_ptm_00_beard_00_0013_01.pac (1,592,632 bytes)

Looked for:

  * ASCII string tokens: `morph`, `shape`, `blend`, `BlendShape`,
    `ShapeKey`, `NoseHeight`, `EyeOpen`, `MouthOpen`, `Brow`, `Cheek`,
    `Chin`, `Jaw`, `Lip`, `Teeth`, `Forehead`, `Smile`, `Frown`, `Wink`
  * Sustained runs of `3 × fp16` triples in `[-1.0, 1.0]` range
    (classic per-vertex displacement delta signature)

## Result — no classic blend-shape data in head PACs

Every `shape`-flavoured string hit turned out to be a **Havok physics
collision shape** reference (`hknpShape`, `hknpCompoundShape`,
`hknpConvexHull`, `shapeTagCodecInfo`, `numShapeKeyBits`). The
`ShapeKey` references are Havok broad-phase shape indexing, not
vertex morph keys.

No strings matched `NoseHeight` / `EyeOpen` / `MouthOpen` / etc.

## Hypothesis — face customisation is bone-driven, not blendshape-driven

Pearl Abyss's character-creation morph sliders almost certainly
drive **facial rig bones** (tiny scale/translate bones embedded in
the skeleton), not classic vertex blend shapes. Evidence:

  1. Head PACs contain Havok physics but no named vertex deltas
  2. The PAB skeleton for character heads would need inspecting —
     if it has bones named `BN_Face_*`, `BN_Brow_L`, `BN_Nose_*`
     etc., that confirms the bone-driven model
  3. This matches other Pearl Abyss titles (Black Desert Online's
     beauty album is bone-driven as well)

## Path forward

A proper "face morph editor" would need:

  1. **PAB facial-bone inventory** — walk the head skeleton and
     flag bones whose names match facial regions (not yet surveyed)
  2. **Per-character morph value file** — Black Desert stores this
     as `.character` serialised blob; Crimson Desert likely has
     an equivalent format we haven't located yet. The appearance
     might live in `characterappearanceindexinfo.pabgb` (25 rows
     spotted in the corpus) or a sidecar file per character slot
  3. **UI** — slider-per-facial-bone dialog that writes adjusted
     bone scale/translate values back into the appearance blob

Deferred to a future release. The state-machine browser (same
release) was scoped to ship cleanly rather than half-build a
morph feature based on false signals.

## Community context

> **b.monk** (Discord, yesterday):
> "Now if we can just figure out how to get the shapekeys/morph
> parameters exported for faces, we'll have a lot more options for
> custom looks and outfits"

We're still on this — the .pabgb editor shipped in v1.18.0 and the
prefab editor shipped in v1.19.0 are the foundations. Next pieces
needed are the PAB facial-bone scanner and the appearance-blob
parser.
