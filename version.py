"""CrimsonForge version and changelog registry.

Single source of truth for the application version. Every module that needs
the version string imports it from here. The CHANGELOG list is rendered in the
About tab so users (and developers) always see what changed.

VERSION BUMPING RULES
---------------------
- Bump PATCH (1.x.Y) for bug fixes, small tweaks, and safe improvements.
- Bump MINOR (1.X.0) for new features, new tabs, new AI providers, or
  significant workflow changes.
- Bump MAJOR (X.0.0) for breaking changes to project files, settings
  format, or game-patch pipeline.
- Always add a new entry at the TOP of CHANGELOG when changing code.
"""

__all__ = ["APP_VERSION", "APP_NAME", "CHANGELOG"]

APP_NAME = "CrimsonForge"
APP_VERSION = "1.22.6"

# Each entry: (version, date, list_of_changes)
# Newest first. `date` is YYYY-MM-DD.
CHANGELOG: list[tuple[str, str, list[str]]] = [
    (
        "1.22.6", "2026-04-22", [
            "[Fix] Ship-to-App no longer fails with \"'localizationstring_*.paloc' not in PAMT\" for ANY language. Root cause: core/pamt_parser.py had TWO `find_file_entry` definitions; the later one silently shadowed the earlier, dropping the basename-fallback that every Ship-to-App caller relies on. Consolidated into a single canonical lookup that handles full paths, bare basenames, Windows slashes, and mixed case in one O(n) pass. Works identically for every one of the 17 shipping languages (eng / kor / jpn / rus / tur / spa-es / spa-mx / fre / ger / ita / pol / por-br / zho-tw / zho-cn / tha / vie / ara).",
            "[Fix] DeepL translation no longer fails with \"DeepL SDK not installed, run: pip install deepl\" in the shipped exe. The deepl package wasn't in requirements.txt so it was never bundled by PyInstaller. Added deepl>=1.17.0 to requirements.txt and 'ai.provider_deepl' (plus every other provider module) to the spec's hiddenimports to guarantee all 11 providers ship with every future exe build.",
            "[Enhancement] 21 new regression tests in tests/test_find_file_entry.py pin down the canonical lookup contract: every shipping language's paloc resolves from its bare basename, full path, Windows slashes, and mixed case forms; a module-level guard asserts there is EXACTLY ONE `find_file_entry` definition so the shadowing bug cannot reappear.",
            "[Enhancement] Full test suite now 410 tests + 136 subtests = 546 scenarios passing (was 508 in v1.22.5).",
        ],
    ),
    (
        "1.22.5", "2026-04-22", [
            "[Fix] pa_checksum no longer triggers false-positive virus flags from Windows Defender / some third-party AVs. The previous MinGW-compiled ctypes DLL (core/pa_checksum.dll) matched heuristic patterns AVs associate with malware loaders. Switched to a MSVC-compiled Python C extension (core/_pa_checksum.cp*-win_amd64.pyd) loaded via Python's own import machinery, which inherits Python's AV trust path. No observable performance difference — same Bob Jenkins Lookup3 C core, cross-verified bit-for-bit against the old DLL's output.",
            "[Enhancement] Removed the ctypes fallback branch from core/checksum_engine.py — the dispatcher is now C extension when compiled, pure Python otherwise. Cleaner two-path logic instead of three.",
            "[Enhancement] CrimsonForge.spec simplified — no explicit binaries entry, the .pyd is auto-discovered through a hiddenimport. PyInstaller bundles it into the frozen exe via the Python extension machinery rather than as a loose DLL.",
        ],
    ),
    (
        "1.22.4", "2026-04-22", [
            "[Fix] FBX export of character meshes (cd_phm_*, cd_phw_*, cd_ppdm_*, cd_pgm_*, cd_pfm_*, etc.) now correctly finds the shared class-level skeleton. Previously the mesh export path used a sibling/basename search that was guaranteed to miss for character PACs (they share phm_01.pab / phw_01.pab / ppdm_01.pab class rigs, not per-mesh PABs). Root cause of the reported 'no armature in exported FBX' bug.",
            "[Feature] New core/skeleton_resolver.py — single source of truth for mapping a PAC/PAA asset path to its shared rig. Knows all 16 known rig prefixes (phm, phw, ptm, ptw, pfm, pfw, ppdm, ppdw, pgm, pgw, prh, nhm, nhw, ngm, ngw, rd). Used by both the mesh FBX export and the animation FBX export paths for consistency.",
            "[Feature] Manual 'Browse for .pab...' fallback when auto-resolve misses. The picker opens a filterable list of every PAB visible through the VFS, sorted by prefix-match first. User's choice is saved per rig class in config so future exports of the same character class skip the dialog automatically.",
            "[Feature] New core/crash_handler.py — diagnostic layer for silent early-boot failures. faulthandler.enable() captures native C-level crashes (access violations, DLL load failures). sys.excepthook captures uncaught Python exceptions. Windows ctypes MessageBoxW provides a native dialog fallback when Qt itself is what failed to initialise. Next time the exe exits silently we will have a full traceback in the log instead of guessing.",
            "[Fix] main.py now wraps QApplication(sys.argv) in try/except that surfaces a clear error message when Qt fails to initialise — typically after a hard reboot corrupted the PyInstaller extraction (%TEMP%\\_MEI*) or when VC++ 2015-2022 redistributable is missing.",
            "[Feature] New core/mesh_preflight.py — pre-flight memory check that warns before starting a mesh repack when available RAM is below the projected peak. Uses psutil primary path + Windows GlobalMemoryStatusEx fallback. Prevents the 'whole system drops to 1 FPS' symptom when Forge + Blender + JMM + JMM Creator are all open at once.",
            "[Performance] build_pac() in core/mesh_importer.py replaces a full copy.deepcopy(mesh) with a shallow wrapper copy + fresh submesh list. On a 20k-vertex character mesh the deepcopy walked every vertex/face/uv/normal tuple — hundreds of megabytes of allocation. Shallow copy is O(n_submeshes) instead.",
            "[Test] 508 test scenarios across the v1.22.4 changes: 179 for the skeleton resolver (every prefix, ranking rules, VFS integration, manual override), 33 for crash diagnostics (install/reset/excepthook/message-box fallback), 37 for the pre-flight memory check (estimator, probe chain, decision matrix), 34 for the OBJ sidecar round-trip, 29 for vertex-split propagation + shallow-copy regression, 109 for checksum engine, 87 for file type detection.",
        ],
    ),
    (
        "1.22.3", "2026-04-21", [
            "[Fix] OBJ re-import no longer loses skin weights on UV-seam vertices. When Blender splits a vertex for multiple UV/normal corners, the clone now inherits its source slot's bone indices and bone weights. Root cause of the reported 'model exploded after import' symptom.",
            "[Feature] New `.cfmeta.json` sidecar written next to every exported OBJ carries the original skin data (bone indices + weights per vertex). On re-import, the sidecar populates the source-vertex map so the PAC rebuilder picks the correct donor record for each vertex — survives user edits that move vertices far from any original position. Falls back gracefully to positional matching when the sidecar is absent.",
            "[Fix] Repack tab no longer appears empty after game load. `initialize_from_game()` now restores the last-used Modified Files directory from config and auto-scans it; a clear next-step hint is shown when nothing is configured.",
            "[Fix] Ship-to-App dialog no longer freezes the window during ZIP generation. `build_mesh_manager_package` and `build_mesh_ship_package` now run on a background `FunctionWorker`; progress bar updates live and the dialog surfaces errors instead of locking up.",
            "[Fix] FBX export of a PAC with a missing `.pab` skeleton no longer silently falls back to mesh-only. The exporter now searches every loaded PAMT first by sibling path, then by basename, and surfaces a confirmation dialog (with the exact reason) before producing an armature-less FBX.",
            "[Enhancement] `SubMesh.source_vertex_map` added — per-imported-vertex back-reference to the original slot. Consumed by the PAC full-rebuild path to route donor records correctly; empty when unused.",
        ],
    ),
    (
        "1.22.2", "2026-04-21", [
            "[Fix] Tab switching no longer freezes the window on first click. Materialisation is now three-phase: the loading overlay paints first, widget construction runs on the next UI tick, and game-data init runs on a background thread.",
            "[Fix] Qt currentChanged signal re-entrance during the tab swap is blocked, preventing duplicate materialisation.",
            "[Fix] User's selected tab no longer jumps when a non-focused tab is swapped — currentIndex is preserved across the swap.",
            "[Enhancement] TabInitContainer now supports lazy content installation via set_content().",
            "[Enhancement] 531 tests passing (+7 for the lazy-install path).",
        ],
    ),
    (
        "1.22.1", "2026-04-21", [
            "[Fix] Clicking a tab for the first time no longer locks the window for 5-30 seconds. Tab initialisation (PAMT indexing, paloc cross-reference, catalog builds) now runs in a background thread with a progress overlay.",
            "[Feature] New loading-overlay widget — progress bar + status label + Retry button. Flips to real tab content when init finishes.",
            "[Enhancement] Per-tab init state tracked; in-flight workers are de-duplicated; failures surface a Retry button that re-runs the same task.",
            "[Enhancement] 524 tests passing (+15 for the overlay state machine).",
        ],
    ),
    (
        "1.22.0", "2026-04-20", [
            "[Feature] Bone-mapping editor dialog — review and edit the auto-correlated PAA-track → PAB-bone mapping per rig. Saved per rig to %APPDATA%/CrimsonForge/bone_maps/<rig>.bonemap.json with colour-coded confidence.",
            "[Feature] child_idle PAA variant fully decoded — 112 tracks / 4,711 keyframes recovered (was 1 track in v1.21.1). Root bone uses stride 10; child bones use stride 8 with implicit W.",
            "[Feature] Link-variant VFS resolution verified end-to-end across primary-group, fallback-scan, and unresolvable cases.",
            "[Feature] Disconnected placeholder mesh resolved — joint cubes and limb cylinders enlarged to overlap, combined with split-weight limbs.",
            "[Feature] FBX → PAA writer now covers all three shipping variants (tagged, untagged, v3) with a unified dispatcher.",
            "[Fix] v3 parser scan window and walker bailout corrected for minimal bone blocks.",
            "[Enhancement] All five FBX-animation Known Issues tracked since v1.18.0 are now closed.",
            "[Enhancement] 509 tests passing (+21 across parser, integration, and writer).",
        ],
    ),
    (
        "1.21.1", "2026-04-20", [
            "[Feature] First-cut SRT-float / child_idle PAA variant parser — recovers 113 keyframes from the test sample where the v2 parser returned zero tracks.",
            "[Feature] parse_paa() auto-routes to v3 when v2 returns zero tracks — callers get real track data without knowing which variant a file uses.",
            "[Feature] Limb-prism vertices are now split-weighted — parent-end verts bind to the parent bone, child-end verts to the current bone. Limbs bend smoothly between joints instead of sliding past each other.",
            "[Enhancement] 499 tests passing (+13 for v3 parser and split-weight mesh).",
        ],
    ),
    (
        "1.21.0", "2026-04-19", [
            "[Feature] PAA → PAB bone mapping — auto-correlates from bind-pose angular distance with a JSON override saved per rig. After deep RE, confirmed that PAB bone names and common string hashes do not appear in PAA bytes; the mapping isn't in the file, so the auto-correlate seed + user override is the correct solution.",
            "[Feature] Link-variant PAA resolver — follows embedded %character/... paths across the VFS with a loop-guard. Covers the ~19% of shipping PAAs that point at other files instead of carrying their own animation.",
            "[Feature] parse_paa_with_resolution() — single entry point that follows link-variant references through a passed VFS.",
            "[Feature] FBX → PAA inverse writer — round-trips via the parser with bit-exact frame indices and fp16-precision quaternions.",
            "[Feature] export_animation_fbx() accepts a bone_map so PAA track i can drive PAB bone bone_map[i]; tracks mapped to -1 are excluded.",
            "[Known Issue] child_idle variant and disconnected placeholder mesh remain open this release (both closed in v1.21.1 / v1.22.0).",
            "[Enhancement] 486 tests passing (+35 across bone-mapping, link-resolver, and writer).",
        ],
    ),
    (
        "1.20.3", "2026-04-18", [
            "[Fix] Face-Part Browser 'Open Matching Prefab' now uses a reverse-reference index instead of a basename heuristic. Real corpus showed only 1 in 6 prefabs matched their PAC by basename; the new index scans every prefab once and answers queries in O(1).",
            "[Feature] prefab_reference_index module — case-insensitive PAC → prefab map with basename fallback and duplicate-add idempotency. When multiple prefabs point at the same PAC, the Explorer pops a selection dialog.",
            "[Enhancement] End-to-end flow tests exercise the prefab edit, state-machine browse, and face-parts pipelines against the real temp cache.",
            "[Enhancement] Prefab edit/patch path fuzzed — identity round-trip byte-exact, same-length edits preserve file size, length changes update size by exact delta, random 50-cycle edits always re-parse cleanly.",
            "[Enhancement] 451 tests passing (+20 across reverse-index, E2E flows, and fuzz tests).",
        ],
    ),
    (
        "1.20.2", "2026-04-17", [
            "[Fix] Face-Part Browser walks the VFS via the public list_package_groups + load_pamt API instead of a private cache — covers every shipping package group.",
            "[Fix] Face-part classifier regex expanded from 3 to 9 prefixes (ptm/phm/phw/pfm/pfw/ppdm/ppdw/pgm/pgw) so eye-detail and face-template PACs are no longer dropped from the catalog.",
            "[Feature] 'Show Sub-Parts' button reads the granular sub-parts bundled inside head_sub PACs (e.g. EyeLeft_0001, Tooth_0001, Eyebrow_0004).",
            "[Feature] 'Open Matching Prefab' button routes through the Explorer's existing edit flow in one click.",
            "[Enhancement] 431 tests passing (+3 real-corpus tests).",
        ],
    ),
    (
        "1.20.1", "2026-04-16", [
            "[Feature] New Face-Part Browser — catalogs every face-part PAC across loaded archives (Head, HeadSub, Eye, Brow, Lash, Tooth, Tongue, Nose, Lip, Mouth, Beard, Mustache, Hair, Ear, Face) with variant IDs extracted from the filename.",
            "[Feature] face_parts module — enumerated classifier with longest-prefix disambiguation, variant-ID extractor, and a granular sub-part scanner for head_sub PACs.",
            "[Feature] Category list with part/variant counts + filterable variant table; Copy Archive Path + Export Catalog CSV.",
            "[Feature] Explorer Quick Mods now includes 'Face-Part Browser...'.",
            "[Enhancement] Investigation confirmed the game's face-customisation paradigm is submesh swapping (enumerated variant PACs), not blendshapes or dedicated facial bones.",
            "[Enhancement] 428 tests passing (+14 face-part tests).",
        ],
    ),
    (
        "1.20.0", "2026-04-15", [
            "[Feature] New State-Machine Browser — cross-references every condition expression across 9 state-relevant pabgb tables and surfaces the underlying state tokens (ActionAttributes, Missions, Stages, CharacterKeys, Macros, Levels, Gimmicks).",
            "[Feature] state_machine module — byte-level tokeniser for the condition-expression grammar (FCALL allowlist, argument-identifier extraction, bare-identifier enum pass).",
            "[Feature] Token list sorted by occurrence frequency with category filter, text search, and min-occurrences threshold; CSV export per token.",
            "[Feature] Explorer Quick Mods now includes 'State-Machine Browser...'.",
            "[Feature] Known-enum catalogues exposed (ActionAttributes, CharacterKeys, MacroStates).",
            "[WIP] Face-morph investigation deferred. Hex-dumped head / eye / beard PACs: every 'shape' hit is Havok physics, not vertex deltas. The feature is bone-driven (facial rig bones + a per-character appearance blob) — scanner + blob parser tracked for a future release.",
            "[Enhancement] 414 tests passing (+14 state-machine tests).",
        ],
    ),
    (
        "1.19.1", "2026-04-14", [
            "[Fix] Prefab editor no longer crashes on open — replaced the QTableView call that only exists on QTreeView with the correct performance pattern (fixed header section-size-mode + per-pixel scroll mode).",
            "[Fix] Verified headless against a 76-row cloak prefab; 400 regression tests still pass.",
        ],
    ),
    (
        "1.19.0", "2026-04-14", [
            "[Feature] New .prefab editor — byte-level reverse-engineered parser for Pearl Abyss prefab assets (magic header + two 32-bit hashes, then a linear stream of length-prefixed UTF-8 strings classified by role).",
            "[Feature] Editor dialog with category filter, text search, live edit preview with length delta, per-string byte context, revert / save-as / patch-to-game.",
            "[Feature] Safe-mode 'Same-length edits only' (default ON) preserves binary layout; toggle off for length-changing edits with automatic length-prefix updates.",
            "[Feature] Five string categories colour-coded: File References, Tag/Enum Values, Property Names (read-only), Type Names (read-only), Other.",
            "[Feature] Tag values are paired with the nearest preceding tag-typed property (e.g. '_shrinkTag = Cloak') for clearer context.",
            "[Feature] Right-click .prefab in Explorer → 'Edit Prefab'. Patch-to-Game writes through the repack pipeline with automatic backup.",
            "[Feature] apply_edits() supports atomic multi-string rewrite with length deltas accumulated in order.",
            "[Enhancement] PAA link-variant detection scans offsets 0x14..0x100 for the '%' marker with prefix validation, exposing the detected offset to downstream consumers.",
            "[Enhancement] PAA bind-pose walker gains an offset+0/+4 probe for flag variants that insert a 4-byte hash before the first SRT record.",
            "[Enhancement] 400 tests passing (+16 prefab tests).",
        ],
    ),
    (
        "1.18.0", "2026-04-12", [
            "[Feature] New .pabgb / .pabgh game-data table editor — handles both the simple (5-byte entries) and hashed (8-byte entries) flavours discovered via byte-level inspection.",
            "[Feature] Editor dialog with searchable row list, filterable field table with auto-labels and colour coding, hex-dump pane with per-field highlighting, row comparison, duplicate/delete row, and patch-to-game.",
            "[Feature] .pabgb files now open in the editor automatically — previously only showed a hex preview, which blocked edits to iteminfo / stageinfo / conditioninfo / gimmickgroupinfo.",
            "[WIP] PAA → FBX animation export is under active reverse engineering in this release and is NOT production-ready. Use OBJ export for reliable mesh-only round-trips. Full working FBX animation export is tracked for a future release.",
            "[Enhancement] PAA 10-byte keyframe record format documented: [W:fp16][frame:uint16][xyz:3×fp16] per keyframe, sparse frame indices, per-bone implicit-W bind at the top of each block.",
            "[Enhancement] PAA bone-block separator reversed: '3c 00 3c 00 3c' + uint32 count + 6-byte bind + N × 10-byte records. Parser validates each record against |q|² ∈ [0.90, 1.10].",
            "[Enhancement] FBX export composes bind with PAA rotation (fbx_local_rot(t) = PAB_bind × PAA_rot(t)) so bind-pose angles match the expected values.",
            "[Enhancement] FBX export emits a skinned humanoid placeholder mesh so Blender's Armature modifier attaches on import.",
            "[Fix] PAB skeleton parser no longer emits phantom bones past the real count — phm_01.pab returns 56 real bones instead of 178 garbage-trailed ones that crashed Blender's FBX importer.",
            "[Fix] FBX bone positions multiplied by 100 (cm→m) so Blender doesn't collapse the skeleton into a sub-centimetre cluster at origin.",
            "[Fix] FBX bone count clamped to the PAB skeleton size — extra PAA tracks no longer emit origin-placed placeholder bones.",
            "[Known Issue] PAA tracks do not map 1:1 to PAB bone names — explicit mapping table not yet decoded. (Resolved in v1.21.0.)",
            "[Known Issue] child_idle / SRT-float variant decodes to zero tracks. (Resolved in v1.21.1 / v1.22.0.)",
            "[Known Issue] Link-variant PAAs (~19% of shipping corpus) not followed through the VFS. (Resolved in v1.21.0.)",
            "[Known Issue] Placeholder mesh is disconnected cubes + prisms. (Resolved in v1.21.1 / v1.22.0.)",
            "[Known Issue] FBX → PAA reimport not implemented. (Resolved in v1.21.0.)",
            "[Fix] PyInstaller bundle now includes numpy.",
            "[Fix] UPX compression disabled in PyInstaller spec — was corrupting the splash PNG on Windows 11.",
            "[Fix] Splash screen version text position corrected so the version string lands inside the brand banner.",
            "[Enhancement] 384 tests passing (+8 across PAA parser, placeholder mesh, bone-count clamp, and scale).",
        ],
    ),
    (
        "1.17.0", "2026-04-11", [
            "[Performance] Tabs are now lazily instantiated — only constructed when first clicked, cutting app startup time dramatically",
            "[Performance] Game loading moved to a background thread with a live progress bar so the UI stays fully responsive during initialization",
            "[Performance] PAMT scanning across all package groups now runs in parallel using up to 8 I/O threads via concurrent.futures",
            "[Performance] Explorer 'All Packages' loading moved to a background thread — no more UI freeze when browsing 1.45M+ files",
            "[Performance] All QTreeWidget instances now use setUniformRowHeights for instant height calculation instead of per-row measurement",
            "[Performance] All QTableView instances now use fixed row heights and per-pixel scrolling for smoother scroll performance",
            "[Performance] Dialogue Catalog and Item Catalog tree population wrapped with setUpdatesEnabled(False) to eliminate mass repaints during bulk insertion",
            "[Fix] 'Import WAV + Patch to Game' now correctly invalidates the audio player cache after patching so the new audio plays back immediately instead of the stale original",
            "[Fix] Mod Manager ZIP generation no longer crashes with a KeyError on asset_count — the manifest now includes the missing field",
            "[Fix] Added numpy to requirements.txt — resolves 'No module named numpy' error for 3D mesh preview",
            "[Community] OmniVoice TTS: added advanced parameters UI with individual toggles for Gender, Age, Pitch, Style, and Accent (contributed by imedox)",
            "[Community] OmniVoice TTS: added full PAZ location column, renamed JA to CH language code, made language dropdown searchable, and improved TTS UI styling (contributed by imedox)",
            "[Community] OmniVoice TTS: improved ref text auto-fill behavior and enabled text column resizing (contributed by imedox)",
            "[Community] OmniVoice TTS: updated and expanded supported language list (contributed by imedox)",
        ],
    ),
    (
        "1.16.1", "2026-04-08", [
            "[Fix] Standalone Windows builds now bundle ffmpeg and vgmstream helper tools directly inside the packaged app so audio workflows run on clean machines without first-run downloads",
            "[Fix] Bundled runtime now resolves packaged helper executables before user-space tool installs, making shipped builds more reliable on fresh systems",
            "[Fix] Release packaging now includes the helper tool trees alongside core/pa_checksum.dll and the packaged data directory in a single self-contained executable",
        ],
    ),
    (
        "1.16.0", "2026-04-07", [
            "[Feature] Explorer Navigator added as a dedicated popup workbench with live Characters, Items, and Families views built directly from installed game data",
            "[Feature] Navigator selections now scope the normal Explorer file table to exact related archive paths so preview, export, import, patch, ship, extract, and editor workflows continue to use the same Explorer rows",
            "[Enhancement] Navigator now preloads from the active game session and reuses the already loaded game path and PAMT cache instead of rebuilding a separate cold index every time the popup opens",
            "[Enhancement] Navigator UI/UX upgraded with clearer popup flow, active scope labeling, one-click clear scope, resizable split layouts, and zoomable image panels with Fit and 100% controls",
            "[Fix] Navigator DDS image preview now uses the correct decode path for live UI portraits and item icons, matching Explorer behavior instead of failing to load valid DDS files",
            "[Fix] DDS preview support expanded for additional type-1 compressed layouts, including prefixed-LZ4 and first-mip-LZ4-plus-tail families, so more portrait, impostor, and atlas textures open correctly instead of showing dots or noise",
            "[Fix] Unsupported short DDS payloads now fail cleanly with a real preview limitation message instead of fake dot/noise renders, reducing false corruption reports on edge-case textures",
            "[Fix] PAC preview parser now supports additional descriptor variants such as the Kliff/Macduff head layout, restoring full head mesh parsing instead of partial eyecover-only previews",
        ],
    ),
    (
        "1.15.0", "2026-04-06", [
            "[Fix] OBJ reimport now preserves the real face-level UV and normal index mapping from Blender exports instead of assuming position, UV, and normal indices always match",
            "[Fix] Mesh import now correctly splits reused vertices when one position is referenced with multiple UV or normal combinations, preventing mixed, floating, or scrambled textures after reimport",
            "[Fix] The Blender OBJ texture/material binding issue applies across PAC, PAM, and PAMLOD OBJ reimport workflows because the core importer now rebuilds vertices from the actual vi/ti/ni tuples",
            "[Hotfix] Full PAM topology rebuild now remaps hidden static-mesh donor payload by aligned spatial vertex matching instead of raw vertex index, reducing black shading and material corruption on edited static meshes with added geometry",
            "[Feature] Item Catalog tab added: browse raw live-game item data with deep category, subcategory, and subtype taxonomy, searchable tables, path filters, and detailed record inspection",
            "[Feature] Item Catalog exports added: generate enriched CSV/JSON catalogs from iteminfo, multichange, equip-type, slot, and related raw game tables directly from the installed game packages",
            "[Feature] Dialogue Catalog was rebuilt into an enterprise browser with Story, Speakers, and Families views, ordered conversation transcripts, search, filtering, and speaker-confidence reporting",
            "[Enhancement] Dialogue export pipeline now catalogs broad live-game dialogue coverage from localization families such as intro, epilogue, quest, AI ambient, memory, node, and scene-family keys",
            "[Enhancement] Raw game-data browsing improved with structured table indexing so non-item systems like factions, quests, NPCs, roads, and world tables can be discovered from package data faster",
            "[Enhancement] Live-package UI tracing and RTL investigation tooling was expanded for Arabic, font-swap, and English/number runtime debugging directly against Steam-installed game files",
        ],
    ),
    (
        "1.14.0", "2026-04-05", [
            # Audio / OmniVoice Enterprise TTS
            "[Feature] OmniVoice Local TTS provider added with native integration for localhost servers, live model discovery, voice catalog loading, health/status checks, and optional bearer-token auth",
            "[Feature] OmniVoice one-shot cloning now uses original game voice audio as a reference directly from the selected row, with automatic WEM/BNK decode to WAV for local AI synthesis",
            "[Feature] OmniVoice saved-profile workflow added: save or refresh voice profiles from the Audio tab, then synthesize with clone:<profile> voice mode for repeatable character dubbing",
            "[Feature] Audio tab now exposes advanced OmniVoice controls for inference steps, guidance scale, denoise, fixed duration, t_shift, position temperature, and class temperature",
            "[Feature] Batch Generate and Generate All + Patch added to the Audio tab for large-scale NPC redubbing workflows across selected or filtered voice rows",
            "[Enhancement] Audio generation now normalizes provider output formats like MP3 back to WAV automatically before playback, history storage, and WEM patch conversion",
            "[Enhancement] Audio settings changes now refresh the Audio tab immediately so provider availability, OmniVoice URL/token/model, and provider UI state update as soon as settings are saved",
            "[Enhancement] OmniVoice defaults now auto-suggest clone profile names, use selected-row reference audio, prefer unique NPC profile IDs where possible, and bias adult male/female design voices intelligently",

            # Dialogue Coverage / Audio Linking
            "[Enhancement] Audio text linking now scans all .paloc files instead of only localizationstring*.paloc, allowing wider dialogue coverage from broader game text datasets",
            "[Enhancement] Audio filename parsing and paloc linking now recognize more dialogue key families such as faction, npcvoice, npcdialog, textdialog, memory, and general-style voice keys",
            "[Enhancement] Audio linker now tries safe paloc-key aliases for common naming-variant families, improving linkage when audio and localization keys use slightly different prefixes",
            "[Enhancement] Audio index logging now reports text-link coverage percentage directly for easier enterprise QA of dubbing readiness",

            # Stability / Patch Flow
            "[Fix] Audio patch and TTS patch flows now report RepackEngine error lists correctly instead of reading a non-existent single error field",
            "[Fix] Audio tab no longer had legacy TTS patch handlers overriding the newer enterprise workflows, ensuring OmniVoice, batch operations, and normalized audio handling are actually used at runtime",
        ],
    ),
    (
        "1.13.0", "2026-04-05", [
            # Ship to App / Mod Manager Packaging
            "[Feature] Explorer Ship to App now supports a new Mod Manager ZIP (small) mode that exports rebuilt loose mesh files plus manifest.json, modinfo.json, and README.txt for manager-based installs",
            "[Feature] Translate Ship to App now supports the same small Mod Manager ZIP workflow, exporting loose translated .paloc files and optional loose font files instead of full patched archives",
            "[Enhancement] Ship dialogs now let you choose between Mod Manager ZIP (small) and Standalone ZIP (full patched archives), keeping both distribution workflows available in one place",
            "[Enhancement] Manager ZIP packaging now targets current Crimson Desert loose-file manager workflows with files/ payloads, manifest metadata, game-build tagging, and reusable package metadata",
            "[Enhancement] Explorer mesh manager packages now include paired .pamlod loose files automatically when a .pam edit needs its matching LOD asset",

            # Mesh Preview / PAC Viewing
            "[Fix] OpenGL mesh preview upload now uses full buffer byte sizes for positions, normals, and indices, fixing cut or missing body parts caused by truncated GPU buffers",
            "[Fix] Explorer PAC preview now preserves and uses parsed file normals in both the OpenGL and fallback preview paths instead of rebuilding lighting normals incorrectly",
            "[Fix] PAC preview flattening now uses a safer selective preview path with valid fallback behavior, preventing broken partial renders on edge-case character meshes",
            "[Fix] PreviewPane initialization and mesh preview backend handling were stabilized so Explorer mesh preview starts reliably without renderer setup regressions",

            # PAM / Patch-to-Game Stability
            "[Fix] Full PAM rebuild now updates additional local geometry-size headers and hidden mirrored index-count/bounds blocks required by topology-changing static meshes",
            "[Fix] Import OBJ + Patch to Game for PAM meshes now imports the paired PAMLOD transfer helper correctly instead of skipping the LOD patch with a missing-name error",
            "[Fix] Repack state now refreshes in-memory PAMT entry offsets and sizes after patching so same-session preview reads the rebuilt file instead of stale archive offsets",
        ],
    ),
    (
        "1.12.0", "2026-04-04", [
            # Explorer / Mesh Ship to App
            "[Feature] Explorer Ship to App: selected .pac, .pam, and .pamlod meshes can now be packaged as standalone ZIP installers for end users",
            "[Feature] Mesh Ship builder: edited OBJ files now rebuild mesh binaries, patch PAZ/PAMT/PAPGT fully in memory, and generate install.bat, uninstall.bat, README.txt, and manifest.json",
            "[Feature] Explorer mesh context menu now includes 'Import OBJ + Ship to App' for direct one-asset packaging from the file browser",
            "[Enhancement] Explorer mesh shipping dialog now supports multi-asset packaging with per-asset OBJ assignment, reusable metadata fields, and paired .pamlod auto-generation for edited .pam meshes",
            "[Enhancement] Explorer now remembers the last imported OBJ per mesh during the session so packaging workflows can prefill the edited source path automatically",
            "[Enhancement] Explorer mesh Ship to App now resolves real in-game item names for default mod titles when an item mapping exists, such as weapon and armor names from game data",
            "[Fix] Explorer mesh Ship to App metadata fields are now fully editable so mod name, author, and version can be customized before packaging",
            "[Fix] Mesh distribution packages now always include the full enterprise-safe patched set: PAZ payload, package PAMT index, and meta PAPGT checksum root",

            # Translate / Version Tracking
            "[Feature] Translate tab now tracks text updates by exact game build using meta/0.paver + PAPGT CRC instead of only a coarse session fingerprint",
            "[Feature] Per-entry game history is now stored for baseline, added, changed, and removed text events, enabling version-aware filtering inside the translation table",
            "[Feature] Translate table now supports enterprise version filtering: filter entries by tracked game build and by change type (Added, Changed, Removed, Baseline)",
            "[Feature] Translate status bar now shows the latest text-sync build and update summary so new strings and source-text changes are visible immediately after game updates",
            "[Enhancement] Restore, project load, and source-language load now all sync against fresh live game text using the same version-aware merge pipeline",
            "[Enhancement] Game-update sync popups now show previous build, current build, changed text samples, and sample new/removed keys for faster review triage",
            "[Enhancement] Legacy autosave projects are now migrated into enterprise version tracking automatically: existing entries become the original baseline and current pending entries are grouped into the latest update bucket",
            "[Fix] Translation baselines now preserve the original first-seen text and only extend with newly discovered keys on later updates instead of overwriting the baseline snapshot each time",

            # Stability / Static Mesh / Preview
            "[Fix] DDS preview now safely rejects truncated bogus uncompressed decodes and falls back cleanly instead of crashing when browsing problematic files like 03_cube_sp.dds",
            "[Fix] Full PAM topology rebuild now also synchronizes mirrored header metadata blocks, preventing stale static-mesh counts that could cause in-game crashes after adding geometry",
        ],
    ),
    (
        "1.11.0", "2026-04-04", [
            # ── Explorer / Mesh Editing / Search ──
            "[Feature] Full PAC round-trip editing workflow now supports export, edit, add or delete geometry, re-import, and patch back to game for topology-changing meshes",
            "[Fix] PAC OBJ import now triangulates Blender quads and n-gons automatically instead of rejecting non-triangle exports",
            "[Fix] PAC import can now map renamed Blender objects back onto the original game submesh slots using geometry matching heuristics",
            "[Fix] Exact weapon rebuild path now supports topology-changing PAC edits and partial-submesh deletion while preserving archive integrity and checksum validation",
            "[Fix] Explorer item-name search now indexes live game item data so searching by in-game names like 'Vow of the Dead King' shows the correct related files immediately",
            "[Feature] Search history added across Explorer, Audio, and Translate: latest 10 searches persist across restarts, can be clicked to reuse, and each entry can be removed individually",
            "[Enhancement] Explorer 3D preview now uses the fast hardware-accelerated OpenGL viewer path for much smoother large-mesh rendering",
            "[Fix] OpenGL preview compatibility improved: uniform uploads now use PyOpenGL-safe ctypes buffers, fixing preview failures on rebuilt high-vertex PAC meshes",

            # ── Translate / Settings / Runtime ──
            "[Fix] Translate tab AI Provider dropdown now shows the full provider catalog, not only currently enabled providers, with disabled providers clearly labeled for enterprise visibility and control",
            "[Fix] Translate tab now blocks disabled providers with explicit guidance instead of failing silently, while still reading the latest saved model configuration",
            "[Fix] Settings changes now refresh the Translate tab immediately: provider list, selected model display, translation prompt state, and autosave behavior update as soon as settings are saved",
            "[Fix] Settings tab dark-theme white background bug resolved by giving settings pages, stacked panels, and scroll content explicit themed backgrounds",
            "[Enhancement] Standalone build now bundles the entire data directory for portable runtime configuration, language definitions, and future packaged resources",
            "[Fix] Bundled executable now resolves data resources through a dedicated runtime path layer, ensuring languages.json and default settings load correctly in both source and packaged builds",
            "[Fix] Legacy or partial configs now initialize the full AI provider registry consistently, preventing missing-provider states in enterprise settings and translation workflows",
            "[Fix] Clearing custom translation prompts now properly falls back to the built-in enterprise translation prompt instead of keeping stale simplified prompt state",
        ],
    ),
    (
        "1.10.0", "2026-04-02", [
            # ── Enterprise Audio Tab ──
            "[Feature] Enterprise Audio tab: browse, play, export, import, and TTS-generate 107K+ game voice files",
            "[Feature] Audio index engine: 94.9% of voice files auto-linked to paloc dialogue text in 14 languages",
            "[Feature] Voice language auto-detection: Korean (pkg 0005), English (pkg 0006), Japanese (pkg 0035)",
            "[Feature] Audio category filter: Quest Greeting, Quest Main, AI Friendly, AI Ambient, etc.",
            "[Feature] Click any audio file to see dialogue text in all 14 game languages",
            "[Feature] Search across all languages: find audio by English, Korean, Arabic, or any translated text",
            "[Feature] Auto-load translated text into TTS input based on selected language",
            "[Feature] Generated audio history with click-to-play, save, and clear",
            "[Feature] Audio export as WAV or OGG with WEM auto-decode via vgmstream",
            "[Feature] Audio import + Patch to Game with WAV-to-WEM Vorbis conversion via Wwise",
            "[Feature] Wwise auto-detection from WWISEROOT, Program Files, or PATH",
            "[Feature] ffmpeg auto-installer: downloads and installs on first use (~80MB)",

            # ── TTS (Text-to-Speech) ──
            "[Feature] Multi-provider TTS engine: OpenAI, ElevenLabs, Edge TTS (free), Google Cloud, Azure Speech, Mistral Voxtral",
            "[Feature] All TTS models and voices fetched dynamically from provider APIs (nothing hardcoded)",
            "[Feature] TTS providers share API keys with translation providers (OpenAI, Gemini, Mistral)",
            "[Feature] Edge TTS: free, 400+ voices, no API key needed (default provider)",
            "[Feature] Generate + Patch to Game: TTS generate, convert to WEM, write to archives in one click",
            "[Feature] Only enabled providers shown in Audio tab TTS dropdown",

            # ── DeepL Translation ──
            "[Feature] DeepL translation provider (10th provider): superior quality for European languages",
            "[Feature] DeepL free tier (500K chars/month) and Pro ($25/1M chars) support",
            "[Feature] DeepL formality control, context parameter, and glossary support",

            # ── Settings ──
            "[Feature] New Audio/TTS settings page with ElevenLabs and Azure Speech API keys",
            "[Feature] Per-provider Translation Model + TTS Model dropdowns (proper dropdown, not text box)",
            "[Feature] Load Models button fetches and populates Translation + TTS model lists with auto-select",

            # ── Translation Tab ──
            "[Feature] 7 new dialogue sub-categories: Quest Greeting, Quest Main, Quest Side Content, Quest Lines, AI Friendly, AI Ambient, AI Ambient (Group)",

            # ── Mesh Import/Export Fixes ──
            "[Fix] OBJ importer: vertices kept in sequential order (was scrambled by face-visit order)",
            "[Fix] OBJ importer: all vertices preserved including face-unreferenced ones",
            "[Fix] PAM builder: vertex positions patched in-place by pattern matching (100% pass rate)",
            "[Fix] PAC round-trip: 97% pass rate (28/29 tested files)",
            "[Fix] FBX binary writer: node end_offset now absolute — Blender opens exports correctly",

            # ── Stability ──
            "[Fix] App no longer crashes on modded or corrupt game files — decompression failures caught gracefully",
            "[Fix] Browse and preview works on patched game installs where other mod tools modified PAZ archives",
        ],
    ),
    (
        "1.9.0", "2026-04-01", [
            # ── Audio Tab (initial) ──
            "[Feature] Audio tab: browse, play, and export all game audio files (WEM, BNK, WAV, OGG)",
            "[Feature] Audio player with full transport controls in Audio tab",
            "[Feature] Export audio as WAV or OGG from Explorer and Audio tab context menus",
            "[Feature] Import WAV to replace game audio with one-click Patch to Game",
            "[Feature] WEM/BNK to WAV conversion via vgmstream-cli (auto-installed)",

            # ── TTS (initial) ──
            "[Feature] TTS providers: Edge TTS (free), OpenAI TTS, ElevenLabs, Google Cloud TTS, Azure Speech",
            "[Feature] TTS Generator panel: select provider, voice, language, speed",
            "[Feature] Replace + Patch to Game: generate TTS and write directly to game archives",

            # ── DeepL Translation ──
            "[Feature] DeepL translation provider with free tier (500K chars/month) and Pro support",
            "[Feature] DeepL formality control and context parameter for improved accuracy",

            # ── Stability ──
            "[Fix] Decompression failures on modded game files caught gracefully instead of crashing",
            "[Fix] Extract handles corrupt entries by writing raw data instead of crashing",
        ],
    ),
    (
        "1.8.0", "2026-04-01", [
            # ── Round-Trip Mesh Modding ──
            "[Feature] OBJ Import: load modified OBJ files back into the app for preview and patching",
            "[Feature] PAC Builder: rebuild PAC binary from modified mesh — quantizes positions, builds vertex records and index buffer",
            "[Feature] PAM Builder: rebuild PAM binary from modified mesh — preserves header, submesh table, and geometry layout",
            "[Feature] Import OBJ (replace mesh): right-click any .pac/.pam/.pamlod in Explorer to import a modified OBJ",
            "[Feature] Import OBJ + Patch to Game: one-click import, rebuild, compress, encrypt, and write to game archives",
            "[Feature] Full round-trip pipeline: Export OBJ \u2192 edit in Blender \u2192 Import OBJ \u2192 Patch to Game",
            "[Feature] OBJ export now embeds source_path and source_format comments for re-import identification",
            "[Fix] FBX binary writer: child node end_offset was relative to 0 instead of absolute file position — Blender now opens FBX files correctly",

            # ── PAC Mesh Parser (complete rewrite) ──
            "[Feature] PAC mesh parser fully reverse-engineered from binary analysis — correct geometry for all character meshes",
            "[Feature] PAC section layout auto-detected from section offset table inside section 0 — works for all format variants",
            "[Feature] PAC vertex data: uint16 quantized positions dequantized with per-submesh bounding box",
            "[Feature] PAC index buffer: triangle list format with per-submesh index counts per LOD level",
            "[Feature] PAC multi-LOD support: LOD0 (highest quality) automatically selected for preview and export",
            "[Feature] PAC multi-submesh support: sword blades, guards, handles, accessories parsed as separate objects",
            "[Feature] PAC bone index padding: odd bone counts padded to even byte boundary (fixes facial/head meshes)",
            "[Feature] PAC auto-detect vertex stride from section size — handles 36, 38, 40, 42+ byte strides",
            "[Feature] PAC idx_count validation: stops reading at garbage values to prevent buffer overruns",
            "[Feature] UV coordinates extracted from float16 values in vertex records",

            # ── Explorer Export Fixes ──
            "[Fix] Export context menu now uses right-clicked row instead of selected row — no more exporting wrong file",
            "[Fix] Export output filenames include full path (e.g. character_warrior_body.obj) — no more overwrites",
            "[Fix] Lambda closure in export menu binds entry by value — prevents stale reference issues",

            # ── Format Compatibility ──
            "[Feature] 3-LOD PAC files (cd_pgw_* heads, eyebrows) now parse correctly alongside 4-LOD files",
            "[Feature] Variable section size encoding handled: u64 pairs, consecutive u32s, and mixed layouts",
            "[Feature] Unsupported PAC variants (skinnedmesh_box v4.3) gracefully skip instead of showing errors",
        ],
    ),
    (
        "1.7.0", "2026-03-31", [
            # ── Localization Tracer ──
            "[Feature] Localization Tracer: standalone tool — type any text, instantly see every screen it appears on in-game",
            "[Feature] Tracer shows the full chain for each hit: which UI screen, which element, what CSS styling, what font and color",
            "[Feature] 182 game screens mapped to readable names (Character Select, Skill Tree, World Map, Alert Popup, etc.)",
            "[Feature] Three search modes: search by displayed text, by paloc key ID, or by UI binding name",
            "[Feature] When a string appears on multiple screens, all locations are listed with descriptions",
            "[Feature] All 170 CSS, 153 HTML, and 29 template files decrypted and indexed on startup",

            # ── Game UI System ──
            "[Feature] Full game UI system reverse-engineered: HTML/CSS-based with custom localstring binding to paloc entries",
            "[Feature] Per-language CSS files identified — each language has its own font rules and line-breaking behavior",
            "[Feature] Widget template system mapped: reusable KeyGuide, Modal, ItemTooltip components with text overrides",
            "[Feature] 115 UI text bindings cataloged (Save/Load, Exit, Confirm, Cancel, menu labels, skill names, shop titles, etc.)",
            "[Feature] Runtime template variables documented: keybind display, currency icons, clickable game-term links",

            # ── 3D Mesh ──
            "[Feature] Extract and preview all 12,724 skinned character meshes (.pac) from game archives",
            "[Feature] Extract and preview 50,388 static meshes (.pam) including props, terrain, and breakable objects",
            "[Feature] Extract and preview 32,188 LOD mesh variants (.pamlod) with multiple quality levels",
            "[Feature] Export any mesh to OBJ (Wavefront) or FBX (binary 7.4) from Explorer right-click menu",
            "[Feature] FBX export auto-finds and embeds the matching skeleton with full bone hierarchy",
            "[Feature] Mesh preview shows 3D render, vertex/face counts, submesh list, materials, and textures",
            "[Feature] Breakable and destructible object meshes now extract correctly",

            # ── Textures ──
            "[Feature] Preview all 279,515 DDS textures directly in Explorer — no external tools needed",
            "[Feature] Supports all game texture formats: color, normal maps, roughness, heightmaps, distance fields",
            "[Feature] Grayscale and terrain textures render as preview instead of showing an error",

            # ── Skeleton / Animation / Havok ──
            "[Feature] Extract skeleton data (.pab): bone names, parent hierarchy, bind poses, transforms",
            "[Feature] Extract animation data (.paa): keyframes, bone rotations, frame count, duration",
            "[Feature] Extract Havok data (.hkx): bone names, skeleton hierarchy, content type (skeleton/animation/physics/ragdoll)",
            "[Feature] Preview all skeleton, animation, and Havok files directly in Explorer",

            # ── File Support ──
            "[Feature] 108 game file extensions recognized with category, description, and preview/edit support",
        ],
    ),
    (
        "1.6.0", "2026-03-30", [
            "[Feature] OBJ export with materials, UVs, normals, and multi-submesh support",
            "[Feature] FBX binary 7.4 export compatible with Blender, Maya, 3ds Max, Unity, Unreal Engine",
            "[Feature] Right-click Export as OBJ / Export as FBX on any mesh file in Explorer",
            "[Feature] DDS texture header info: format name, resolution, mipmap count, alpha channel",
            "[Feature] Mesh preview in Explorer with static 3D render and geometry statistics",
            "[Feature] Split export option: save each submesh as a separate OBJ file",
            "[Feature] Custom scale factor for mesh export",
        ],
    ),
    (
        "1.5.0", "2026-03-30", [
            "[Feature] Ship to App: generate ZIP+BAT packages for end-user mod distribution",
            "[Feature] Ship to App: auto-discovers Steam game, copies pre-patched files, one-click install",
            "[Feature] Ship to App: built-in font donor system — select donor font, auto-adds missing glyphs for target language",
            "[Feature] Ship to App: uninstall via Steam Verify Integrity — clean and reliable restoration",
            "[Feature] Paloc parser now extracts 172K+ entries (both numeric and symbolic keys like questdialog_*, textdialog_*)",
            "[Feature] Dialogue and Documents categories now populated from symbolic keys (was empty before)",
            "[Feature] Auto-lock untranslatable entries (empty, PHM_, placeholder) — marked Approved and protected from editing",
            "[Feature] Locked status filter in translation table — view all auto-locked entries",
            "[Feature] Wildcard search: key:quest*, *dragon*, {*} for brace tokens, locked:yes, empty:yes",
            "[Feature] Game version read from meta/0.paver — shows real version (e.g. v1.01.02) in About tab",
            "[Feature] Status bar auto-reflects on app startup — badges populate immediately after restore",
            "[Feature] Always merge with fresh game data on startup — catches new entries, parser improvements, patches",
            "[Feature] Detailed game update popup with new/changed/removed counts and text samples",
            "[Feature] Arrow key navigation in Explorer tab now triggers preview (was mouse-only)",
            "[Enhancement] Comprehensive tooltips on every widget across all tabs (Translate, Explorer, Font, Settings, About)",
            "[Enhancement] Search supports field:value syntax, quoted phrases, glob wildcards, boolean operators",
            "[Enhancement] LZ4/ChaCha20 checksum computed via native DLL (754x faster than pure Python on large PAZ files)",
            "[Enhancement] Font Builder: GSUB/GPOS merge filters out lookups referencing missing glyphs — no more KeyError crashes",
            "[Enhancement] Font Builder: handles CJK fonts with coordinates > 16-bit by clamping bounding boxes",
            "[Enhancement] Ship to App BAT scripts use delayed expansion for paths with (x86) parentheses",
            "[Fix] Usage filter categories (Dialogue, Documents) were empty on Windows — now auto-discovered from all game groups",
            "[Fix] paloc_parser was discarding 55K+ symbolic key entries (questdialog_*, textdialog_*) — now extracted",
            "[Fix] Patch to Game duplicate popup and 1-3 minute freeze — O(n) duplicate apply, single confirmation dialog",
            "[Fix] QComboBox and QTextBrowser text invisible on Windows — explicit color in QSS for item pseudo-elements",
            "[Fix] checksum_file() was bypassing native DLL, falling back to slow pure Python — now routes through pa_checksum()",
        ],
    ),
    (
        "1.4.0", "2026-03-30", [
            "[Feature] Complete UI overhaul: modern Catppuccin-inspired theme with rounded corners, gradient progress bars, smooth hover states",
            "[Feature] New button variants: primary (blue), danger (red), success (green), warning (yellow) with proper hover/press/disabled states",
            "[Feature] Styled tool buttons with checked state for toggles (loop, mute)",
            "[Enhancement] Buttons now have 6px border-radius, 500 font-weight, proper focus rings",
            "[Enhancement] Tab bar redesigned: no borders, bottom-accent style, cleaner spacing",
            "[Enhancement] Table view: removed grid lines, increased row height (30px), cleaner cell padding",
            "[Enhancement] Context menus: rounded corners (8px), proper padding, separators",
            "[Enhancement] Scrollbars: transparent track, rounded handles, pressed state",
            "[Enhancement] Combobox dropdowns: rounded items, hover highlights, proper padding",
            "[Enhancement] Group boxes: 8px radius, blue title color, more padding",
            "[Enhancement] Search input: clear button enabled, better placeholder text",
            "[Enhancement] Slider controls: styled groove, rounded handle with hover-grow effect, filled sub-page",
            "[Enhancement] Progress bar: gradient fill (teal to blue), rounded shape",
            "[Enhancement] Translate tab: danger-styled Clear/Clear All buttons, success-styled Patch to Game, warning-styled Revert",
            "[Enhancement] Translate tab: proper vertical line separators replacing ugly '|' text labels",
            "[Enhancement] Translate tab: Stop button danger-styled, AI Selected primary-styled, Approve All success-styled",
            "[Enhancement] Filter bar: styled labels, fixed-width status combo, count label highlighted in blue",
            "[Enhancement] Light theme fully redesigned to match dark theme quality",
            "[Enhancement] Tooltips: rounded corners (6px), proper padding",
            "[Enhancement] Text browser (About/Changelog): styled with proper borders and selection colors",
        ],
    ),
    (
        "1.3.0", "2026-03-30", [
            "[Feature] Auto-install vgmstream: one-click download and install for Wwise audio playback (no manual setup needed)",
            "[Feature] Enhanced audio player: volume slider with mute toggle, loop playback, format info display",
            "[Feature] Audio player keyboard shortcuts: Space (play/pause), S (stop), M (mute), L (loop)",
            "[Feature] Video player now has full transport controls: play/pause/stop, seek, volume, mute, loop",
            "[Feature] Video controls properly connected to video player (was using separate audio-only player before)",
            "[Feature] Added Bink2 (.bk2/.bik) video format support - common in Crimson Desert cinematics",
            "[Feature] Added CriWare USM (.usm) video format support - used in game cutscenes",
            "[Feature] Added MKV, FLAC, AAC format detection and preview support",
            "[Feature] Magic byte detection for FLAC, Bink2, and CriWare USM formats",
            "[Enhancement] Audio preview shows centered format icon with file type label",
            "[Enhancement] Time display supports hours for long audio/video (H:MM:SS format)",
            "[Enhancement] vgmstream auto-installer shows download progress and retry on failure",
            "[Enhancement] Explorer file type filters updated with all new audio/video formats",
            "[Fix] Fixed preview_pane.py clear() method was incorrectly nested inside _html_escape function",
            "[Fix] Video player audio was not controllable - now properly wired to volume/mute controls",
        ],
    ),
    (
        "1.2.0", "2026-03-30", [
            "[Feature] Auto-load game on first run - no manual 'Load Game' click needed when Steam install is found",
            "[Feature] Game version display in status bar, translate tab stats row, and paloc info label (CRC fingerprint + modification date)",
            "[Feature] Auto-check for new game files, new package groups, and new language entries on every launch",
            "[Feature] Game update detection with notification banner showing what changed since last session",
            "[Feature] Centralized version system with full changelog in About tab",
            "[Enhancement] Enterprise context menu: shows selection count, Revert to Pending option, Clear Selected, Select All",
            "[Enhancement] Keyboard shortcut: Ctrl+A to select all visible rows in translation table",
            "[Enhancement] Keyboard shortcut: Delete key to clear selected translations and revert to Pending",
            "[Enhancement] Paste feedback: status bar shows 'Pasted to N entries' after paste operation",
            "[Enhancement] Batch status operations now emit proper signals for real-time stats updates",
            "[Fix] Entry editor (double-click) save now correctly persists translation text",
            "[Fix] Auto-transition Pending -> Translated when entering text in entry editor",
            "[Fix] Auto-revert to Pending when clearing translation text (both inline and editor dialog)",
            "[Fix] Real-time status combo auto-update as you type in entry editor",
            "[Fix] Paste to multiple selected rows: single copied line now applies to ALL selected rows",
            "[Fix] Stats bar updates immediately after save/paste/status change operations",
        ],
    ),
    (
        "1.1.0", "2026-03-28", [
            "[Feature] Full 'Patch to Game' pipeline: export, compress, encrypt, write PAZ, update PAMT+PAPGT checksum chain",
            "[Feature] Backup manager creates automatic backups before patching game files",
            "[Feature] Duplicate detection: finds identical original text across entries and offers batch-apply",
            "[Feature] Glossary manager for proper nouns - ensures consistent translation of names, places, factions",
            "[Feature] AI glossary injection: glossary terms are injected into every AI translation prompt",
            "[Feature] Baseline manager: immutable reference of original game text, survives game updates",
            "[Feature] Game update merge: detects new/removed/changed strings and preserves translations",
            "[Feature] Import/Export JSON for external editing with merge-by-key support",
            "[Feature] Export to TXT with tab-separated format for spreadsheet compatibility",
            "[Feature] Autosave manager with configurable interval (default 30s)",
            "[Feature] Session state recovery: restores project, UI selections, and scroll position on restart",
            "[Feature] Translation batch processor with pause/resume/stop controls",
            "[Feature] Localization usage index: tags strings by game context (dialogue, quest, UI, skills, etc.)",
            "[Enhancement] Usage category filter in translation table",
            "[Enhancement] Advanced search: field-specific queries (key:, original:, translation:, usage:, status:)",
            "[Enhancement] Ranked search results with weighted scoring across all fields",
            "[Enhancement] Review All / Approve All bulk operations with progress",
            "[Enhancement] Token and cost tracking per translation batch",
        ],
    ),
    (
        "1.0.0", "2026-03-19", [
            "[Feature] Initial release - Crimson Desert Modding Studio",
            "[Feature] Game auto-discovery: scans Steam libraries for Crimson Desert installation",
            "[Feature] VFS (Virtual File System) for reading game package archives",
            "[Feature] PAPGT root index parser with full checksum chain support",
            "[Feature] PAMT metadata parser for file entries within package groups",
            "[Feature] PAZ archive reader with ChaCha20 decryption and LZ4 decompression",
            "[Feature] Paloc localization file parser and builder",
            "[Feature] Explorer tab: browse, unpack, and inspect game resources",
            "[Feature] Repack tab: rebuild modified resources back into game archives",
            "[Feature] Translate tab: AI-powered and manual translation workspace",
            "[Feature] Font Builder tab: custom font generation for game text rendering",
            "[Feature] Settings tab: configure AI providers, models, and preferences",
            "[Feature] Multi-provider AI translation: OpenAI, Anthropic, Google, DeepSeek, local models",
            "[Feature] Translation table with virtual scrolling (100K+ entries)",
            "[Feature] Column sorting, copy/paste, and status management",
            "[Feature] Dark and Light theme support",
            "[Feature] 17 game languages auto-detected from paloc files",
            "[Feature] 70+ world languages available as translation targets",
        ],
    ),
]


def get_changelog_html() -> str:
    """Render the full changelog as styled HTML for the About tab."""
    tag_colors = {
        "Feature": "#a6e3a1",
        "Enhancement": "#89b4fa",
        "Fix": "#f9e2af",
        "Breaking": "#f38ba8",
        "Security": "#cba6f7",
        "Deprecated": "#fab387",
        "Removed": "#eba0ac",
        "Performance": "#94e2d5",
        # Work-in-progress + known-issue tags — call out partial
        # features and unresolved gaps directly in the changelog so
        # users don't assume a feature is production-ready when it
        # still has known blockers.
        "WIP": "#fab387",
        "Known Issue": "#f38ba8",
        "Community": "#94e2d5",
    }

    html_parts = []
    for version, date, changes in CHANGELOG:
        html_parts.append(
            f'<h3 style="margin-top:18px; margin-bottom:4px;">'
            f'v{version} &mdash; {date}</h3>'
        )
        html_parts.append('<ul style="margin-top:2px;">')
        for change in changes:
            # Parse [Tag] prefix for coloring
            display = change
            for tag, color in tag_colors.items():
                prefix = f"[{tag}]"
                if change.startswith(prefix):
                    rest = change[len(prefix):].strip()
                    display = (
                        f'<span style="color:{color}; font-weight:bold;">[{tag}]</span> '
                        f'{rest}'
                    )
                    break
            html_parts.append(f"<li>{display}</li>")
        html_parts.append("</ul>")

    return "\n".join(html_parts)
