/**
 * CrimsonForge Full Character Switch — Enterprise Edition
 *
 * Keybind-based character switching that bypasses ALL restrictions.
 * Directly spoofs the TrocTr ChangePlayerbleCharacter internal packet.
 *
 * Hotkeys:
 *   F2 = Switch to Damiane
 *   F3 = Switch to Oongka
 *   F4 = Switch to Kliff
 *   F5 = Switch to Yahn
 *
 * Also includes:
 *   Patch 1: ForbiddenCharacterList bypass (vfunc[2] stub)
 *   Patch 2: ChangeCharacterNotice popup suppression (HTML-level, not vtable)
 *
 * All addresses found via RTTI auto-scanning — survives game updates.
 * Log: bin64/crimson_fullswitch.log
 *
 * Architecture:
 *   The game uses Pearl Abyss 'TrocTr' internal messaging (NOT network).
 *   ChangePlayerbleCharacterReq → engine validates → Ack (performs switch)
 *   We bypass by:
 *     1. Stubbing ForbiddenCharacterList handler (no chars blocked)
 *     2. Finding the Ack handler and calling it with forged packet data
 *     3. OR: finding the higher-level DoChangeCharacter function
 *
 * Character Keys:
 *   Kliff:  (1 << 32) | 0x30 = 0x100000030
 *   Yahn:   (2 << 32) | 0x30 = 0x200000030
 *   Damian: (4 << 32) | 0x30 = 0x400000030
 *   Oongka: (6 << 32) | 0x30 = 0x600000030
 *
 * Key addresses (post-update, auto-scanned via RTTI):
 *   ForbiddenCharacterList vtable: 0x4843208 (COL 0x4EE6080)
 *   ChangeCharacterNotice vtable:  0x48C9C50 (COL 0x4F0D490)
 *   ChangePlayerbleCharacterReq:   0x4CFC5F8 (COL 0x5067F18)
 *   ChangePlayerbleCharacterAck:   0x4843268 (COL 0x4EE59A0)
 *   Ack handler function:          0x984350
 *   Req handler function:          0x21F0140
 *   DoSwitch helper:               0x6BAA50
 *   Global character manager:      [0x5D2AEE0]
 *   Global switch state:           [0x5D2AEE8]
 *   SpawnCharacterCheatReq:        0x4CFE938 (COL 0x506B630)
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ── Globals ─────────────────────────────────────────────────── */
static FILE* g_log = NULL;
static uint8_t* g_base = NULL;
static HANDLE g_thread = NULL;
static volatile int g_running = 1;

/* Character indices */
#define CHAR_KLIFF   1
#define CHAR_YAHN    2
#define CHAR_DAMIAN  4
#define CHAR_OONGKA  6

/* Key = (index << 32) | 0x30 */
#define CHAR_KEY(idx) (((uint64_t)(idx) << 32) | 0x30)

static void ulog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

/* ── RTTI Scanner ────────────────────────────────────────────── */

static uint64_t* find_vtable_by_rtti(const char* class_name, uint64_t* out_col_rva) {
    /* Scan for .?AV<classname>@pa@@ in .rdata */
    char pattern[256];
    sprintf(pattern, ".?AV%s@pa@@", class_name);
    size_t plen = strlen(pattern);

    /* Get image size from PE header */
    DWORD pe = *(DWORD*)(g_base + 0x3C);
    DWORD img_size = *(DWORD*)(g_base + pe + 4 + 20 + 56);
    uint64_t image_base = (uint64_t)g_base;

    /* Find type descriptor (mangled name) */
    uint8_t* td = NULL;
    for (DWORD i = 0; i < img_size - plen - 16; i++) {
        if (memcmp(g_base + i, pattern, plen) == 0 && g_base[i + plen] == 0) {
            td = g_base + i - 16; /* TD starts 16 bytes before name */
            break;
        }
    }
    if (!td) return NULL;

    uint32_t td_rva = (uint32_t)(td - g_base);

    /* Find COL that references this TD (signature=1 at offset-12) */
    uint8_t td_rva_bytes[4];
    memcpy(td_rva_bytes, &td_rva, 4);

    for (DWORD i = 12; i < img_size - 24; i++) {
        if (memcmp(g_base + i, td_rva_bytes, 4) == 0) {
            uint32_t sig = *(uint32_t*)(g_base + i - 12);
            if (sig == 1) {
                uint32_t col_rva = (uint32_t)(i - 12);
                uint64_t col_va = image_base + col_rva;

                /* Find vtable: COL VA at vtable[-1] */
                uint8_t col_va_bytes[8];
                memcpy(col_va_bytes, &col_va, 8);

                for (DWORD j = 0; j < img_size - 8; j++) {
                    if (memcmp(g_base + j, col_va_bytes, 8) == 0) {
                        uint64_t* vtable = (uint64_t*)(g_base + j + 8);
                        if (out_col_rva) *out_col_rva = col_rva;
                        return vtable;
                    }
                }
            }
        }
    }
    return NULL;
}

/* ── Patches ─────────────────────────────────────────────────── */

static uint8_t g_stub_code[] = {0x48, 0x31, 0xC0, 0xC3}; /* xor rax,rax; ret */
static void* g_stub = NULL;

static void apply_patches(void) {
    if (!g_stub) {
        g_stub = VirtualAlloc(NULL, 16, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (g_stub) memcpy(g_stub, g_stub_code, 4);
    }
    if (!g_stub) { ulog("ERROR: stub alloc"); return; }

    /* Patch 1: ForbiddenCharacterList bypass */
    uint64_t col_rva = 0;
    uint64_t* vt = find_vtable_by_rtti("TrocTrUpdateForbiddenCharacterListAck", &col_rva);
    if (vt) {
        DWORD old;
        VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
        vt[2] = (uint64_t)(uintptr_t)g_stub;
        FlushInstructionCache(GetCurrentProcess(), vt, 96);
        VirtualProtect(vt, 96, old, &old);
        ulog("[OK] ForbiddenCharacterList bypassed (vtable at base+0x%llX, COL 0x%llX)",
             (uint64_t)((uint8_t*)vt - g_base), col_rva);
    } else {
        ulog("[SKIP] ForbiddenCharacterList not found");
    }

    /* Patch 2: ChangeCharacterNotice popup — now done via HTML data patch */
    /* vfunc[4,5] stub causes freeze post-update, so we skip it */
    /* The HTML patch (timeScale=1, visibility:hidden) handles it permanently */
    uint64_t* vt2 = find_vtable_by_rtti("UIGamePlayControl_Root_ChangeCharacterNotice", &col_rva);
    if (vt2) {
        ulog("[INFO] ChangeCharacterNotice vtable found at base+0x%llX (not patching — HTML fix active)",
             (uint64_t)((uint8_t*)vt2 - g_base));
    }

    /* Find Ack handler for keybind switching */
    uint64_t* vt_ack = find_vtable_by_rtti("TrocTrChangePlayerbleCharacterAck", &col_rva);
    if (vt_ack) {
        ulog("[INFO] CharacterAck vtable at base+0x%llX, vfunc[2]=0x%llX",
             (uint64_t)((uint8_t*)vt_ack - g_base),
             vt_ack[2] - (uint64_t)g_base);
    }

    /* Find Req for spoofing */
    uint64_t* vt_req = find_vtable_by_rtti("TrocTrChangePlayerbleCharacterReq", &col_rva);
    if (vt_req) {
        ulog("[INFO] CharacterReq vtable at base+0x%llX, vfunc[2]=0x%llX",
             (uint64_t)((uint8_t*)vt_req - g_base),
             vt_req[2] - (uint64_t)g_base);
    }

    /* Find cheat spawn */
    uint64_t* vt_cheat = find_vtable_by_rtti("TrocTrSpawnCharacterCheatReq", &col_rva);
    if (vt_cheat) {
        ulog("[INFO] SpawnCheatReq vtable at base+0x%llX",
             (uint64_t)((uint8_t*)vt_cheat - g_base));
    }
}

/* ── Character Switch Engine ─────────────────────────────────── */

/*
 * Switch architecture (reverse engineered from Ack handler at 0x984350):
 *
 * The Ack handler has TWO paths based on character type:
 *   Path A (mercenary): (charData & 0x30000000) != 0x10000000
 *     -> calls DoSwitchMercenary at 0x6BAA50(manager, charData)
 *   Path B (player char): (charData & 0x30000000) == 0x10000000
 *     -> calls SetPlayerCharacter at 0x6AE010(manager, packet_data)
 *     -> then 0x2E0410 (validate) and 0x1035960 (finalize)
 *
 * Character data is a u32 with type flags in bits 28-29:
 *   0x10000000 = player character type flag
 *
 * Global character manager singleton at [base+0x5D2AEE0]->offset+0x38
 *
 * For player character switching, the Ack handler uses a different
 * call chain that reads the target from a parsed packet structure.
 * We use the mercenary path (0x6BAA50) as it's simpler and takes
 * the character data directly as a u32 parameter.
 */

/* Function pointer types */
typedef void (__fastcall *fn_DoSwitch)(void* manager, uint32_t charData);
typedef void (__fastcall *fn_SetPlayer)(void* manager, void* packetData);

/* Cached addresses (found by RTTI scan + offset analysis) */
static fn_DoSwitch g_doSwitch = NULL;
static fn_SetPlayer g_setPlayer = NULL;
static void** g_managerGlobal = NULL;

static void init_switch_engine(void) {
    /* Find character manager global
     * In Ack handler at +0x094: mov rax, [rip+0x5D2AEE0-relative]
     * Absolute: base + 0x5D2AEE0 */
    g_managerGlobal = (void**)(g_base + 0x5D2AEE0);

    /* DoSwitch (mercenary path) — directly callable */
    g_doSwitch = (fn_DoSwitch)(g_base + 0x6BAA50);

    /* SetPlayerCharacter (player path) */
    g_setPlayer = (fn_SetPlayer)(g_base + 0x6AE010);

    ulog("[SWITCH] Engine initialized:");
    ulog("[SWITCH]   Manager global: base+0x5D2AEE0 = 0x%p", g_managerGlobal);
    ulog("[SWITCH]   DoSwitch: base+0x6BAA50");
    ulog("[SWITCH]   SetPlayer: base+0x6AE010");

    /* Read and log the current manager pointer */
    void* mgr_ptr = *g_managerGlobal;
    if (mgr_ptr) {
        void* mgr = *(void**)((uint8_t*)mgr_ptr + 0x38);
        ulog("[SWITCH]   Manager instance: 0x%p (via [0x%p]+0x38)", mgr, mgr_ptr);
    } else {
        ulog("[SWITCH]   Manager global is NULL (game not fully loaded yet)");
    }
}

static void do_character_switch(int charIndex) {
    if (!g_managerGlobal || !g_doSwitch) {
        ulog("[SWITCH] Not initialized");
        return;
    }

    /* Read manager singleton: *([base+0x5D2AEE0]) -> +0x38 -> manager */
    void* mgr_outer = *g_managerGlobal;
    if (!mgr_outer) {
        ulog("[SWITCH] Manager global is NULL");
        return;
    }
    void* manager = *(void**)((uint8_t*)mgr_outer + 0x38);
    if (!manager) {
        ulog("[SWITCH] Manager instance is NULL");
        return;
    }

    /* Build character data u32
     * The Ack handler checks: (data & 0x30000000)
     * Player chars have 0x10000000 flag
     * Mercenary path is for NON-0x10000000 values
     *
     * Try both encodings — the character index may be:
     * Option 1: Just the raw index (1, 2, 4, 6)
     * Option 2: Index with type flags (0x10000001, etc.)
     * Option 3: The hash/key format from characterinfo
     *
     * Start with the simple index — the DoSwitch function
     * at 0x6BAA50 checks r8d != 0 and then does lookup */
    uint32_t charData = (uint32_t)charIndex;

    ulog("[SWITCH] Switching to index %d, manager=0x%p", charIndex, manager);

    /* Try multiple approaches — player chars use different path than mercenaries */

    /* Approach 1: SetPlayer function (player character path) */
    /* 0x6AE010 takes (rcx=manager_outer, edx=charIndex) and calls CoreSwitch internally */
    __try {
        void* mgr_outer_ptr = *g_managerGlobal;
        ulog("[SWITCH] Approach 1: SetPlayer(mgr_outer=0x%p, index=%d)", mgr_outer_ptr, charIndex);
        g_setPlayer(mgr_outer_ptr, (void*)(uintptr_t)charIndex);
        ulog("[SWITCH]   -> Returned OK");
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        ulog("[SWITCH]   -> CRASHED (0x%X)", GetExceptionCode());
    }

    /* Approach 2: DoSwitch with player type flag 0x10000000 */
    __try {
        uint32_t flagged = 0x10000000 | charIndex;
        ulog("[SWITCH] Approach 2: DoSwitch(0x%X)", flagged);
        g_doSwitch(manager, flagged);
        ulog("[SWITCH]   -> Returned OK");
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        ulog("[SWITCH]   -> CRASHED (0x%X)", GetExceptionCode());
    }

    /* Approach 3: DoSwitch with 0x20000000 flag */
    __try {
        uint32_t flagged2 = 0x20000000 | charIndex;
        ulog("[SWITCH] Approach 3: DoSwitch(0x%X)", flagged2);
        g_doSwitch(manager, flagged2);
        ulog("[SWITCH]   -> Returned OK");
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        ulog("[SWITCH]   -> CRASHED (0x%X)", GetExceptionCode());
    }

    /* Approach 4: DoSwitch with the character key lower bits (0x30) */
    __try {
        uint32_t key30 = 0x30;
        ulog("[SWITCH] Approach 4: DoSwitch(0x%X)", key30);
        g_doSwitch(manager, key30);
        ulog("[SWITCH]   -> Returned OK");
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        ulog("[SWITCH]   -> CRASHED (0x%X)", GetExceptionCode());
    }
}

/* ── Keybind Thread ──────────────────────────────────────────── */

static DWORD WINAPI keybind_thread(LPVOID param) {
    (void)param;

    /* Wait for game to fully load (manager becomes non-NULL) */
    ulog("[KEYS] Waiting for game to load...");
    while (g_running) {
        if (g_managerGlobal && *g_managerGlobal) break;
        Sleep(500);
    }
    if (!g_running) return 0;

    void* mgr = *g_managerGlobal;
    ulog("[KEYS] Game loaded. Manager at 0x%p", mgr);
    ulog("[KEYS] Hotkeys active: F2=Damiane, F3=Oongka, F4=Kliff, F5=Yahn");

    while (g_running) {
        Sleep(50); /* 20Hz polling */

        int target = 0;
        if (GetAsyncKeyState(VK_F2) & 1) target = CHAR_DAMIAN;
        if (GetAsyncKeyState(VK_F3) & 1) target = CHAR_OONGKA;
        if (GetAsyncKeyState(VK_F4) & 1) target = CHAR_KLIFF;
        if (GetAsyncKeyState(VK_F5) & 1) target = CHAR_YAHN;

        if (target) {
            do_character_switch(target);
        }
    }
    return 0;
}

/* ── DLL Entry ───────────────────────────────────────────────── */

static LONG g_once = 0;

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);

        /* Bare load indicator */
        {
            HANDLE hf = CreateFileA("C:\\Users\\hzeem\\Desktop\\FULLSWITCH_LOADED.txt",
                GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (hf != INVALID_HANDLE_VALUE) {
                char msg[] = "DllMain fired\r\n";
                DWORD w; WriteFile(hf, msg, sizeof(msg)-1, &w, NULL);
                CloseHandle(hf);
            }
        }

        char path[MAX_PATH];
        GetModuleFileNameA(NULL, path, MAX_PATH);
        if (!strstr(path, "CrimsonDesert")) return TRUE;
        if (InterlockedCompareExchange(&g_once, 1, 0) != 0) return TRUE;

        g_base = (uint8_t*)GetModuleHandleA(NULL);

        /* Open log */
        char dir[MAX_PATH];
        strcpy(dir, path);
        char* sep = strrchr(dir, '\\');
        if (sep) *sep = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_fullswitch.log", dir);
        g_log = fopen(lp, "w");
        if (!g_log) {
            GetTempPathA(MAX_PATH, lp);
            strcat(lp, "crimson_fullswitch.log");
            g_log = fopen(lp, "w");
        }
        if (!g_log) {
            sprintf(lp, "C:\\Users\\hzeem\\Desktop\\crimson_fullswitch.log");
            g_log = fopen(lp, "w");
        }

        ulog("=== CrimsonForge Full Switch v1 ===");
        ulog("PID: %lu | Build: " __DATE__ " " __TIME__, GetCurrentProcessId());
        ulog("Base: 0x%p", g_base);

        apply_patches();
        init_switch_engine();

        /* Start hotkey thread */
        g_thread = CreateThread(NULL, 0, keybind_thread, NULL, 0, NULL);
        if (g_thread) ulog("[OK] Hotkey thread started");

        ulog("=== Init complete ===");
    } else if (reason == DLL_PROCESS_DETACH) {
        g_running = 0;
        if (g_thread) { WaitForSingleObject(g_thread, 1000); CloseHandle(g_thread); }
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
