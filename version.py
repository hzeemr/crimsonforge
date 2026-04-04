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
APP_VERSION = "1.12.0"

# Each entry: (version, date, list_of_changes)
# Newest first. `date` is YYYY-MM-DD.
CHANGELOG: list[tuple[str, str, list[str]]] = [
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
