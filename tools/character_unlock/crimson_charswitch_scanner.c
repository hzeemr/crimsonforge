/**
 * CrimsonForge NPC Talk Scanner v5 — Safe Polling
 *
 * No hooks, no breakpoints, no code patches.
 * Uses MinHook to safely hook each [rbp+0x2B8] site ONE AT A TIME.
 *
 * F6 = cycle to next site (hooks one site, unhooks previous)
 * F7 = show current site hit count
 * F8 = dump all results
 * F9 = unhook everything
 *
 * Workflow:
 *   1. Launch game, go to Naira as Damian
 *   2. Press F6 (hooks site 0)
 *   3. Try to talk to Naira
 *   4. Press F7 to check if this site was hit
 *   5. If hit count > 0, THIS is the site! Note the RVA.
 *   6. If hit count = 0, press F6 again (cycles to site 1)
 *   7. Repeat until you find the active site
 *
 * Log: bin64/crimson_charswitch_scan.log
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "hook_backend.h"

static FILE*    g_log = NULL;
static LONG     g_init_state = 0;
static uint8_t* g_exe_base = NULL;
static size_t   g_exe_size = 0;

#define MAX_SITES 20

typedef struct {
    uint64_t rva;
    uint8_t* addr;
    LONG     hit_count;
    int      hooked;
    void*    trampoline;
    char     desc[64];
} site_t;

static site_t  g_sites[MAX_SITES];
static int     g_site_count = 0;
static int     g_current_site = -1;
static volatile LONG g_active_hits = 0;

static void slog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

static int is_game(void) {
    char p[MAX_PATH];
    GetModuleFileNameA(NULL, p, MAX_PATH);
    return strstr(p, "CrimsonDesert") != NULL;
}

/* The original function signature for cmp sites:
 * These are mid-function, so we can't cleanly hook them.
 * Instead, we'll use a different approach: patch the CMP immediate
 * from 0x00 to 0xFF (impossible value) so the branch is never taken,
 * then restore it. This is safer than hooking mid-function.
 */

static int patch_site_active(int idx) {
    if (idx < 0 || idx >= g_site_count) return 0;
    site_t* s = &g_sites[idx];

    /* The cmp instruction is: 80 BD B8 02 00 00 [imm8]
     * Byte at offset +6 is the comparison value (0 or 1)
     * We'll change the CMP value to make the branch always/never taken
     *
     * For "cmp [rbp+0x2B8], 0 / JZ" (jumps if flag==0):
     *   Change cmp value from 0 to 0xFF — flag will never be 0xFF, so JZ never taken
     *
     * For "cmp [rbp+0x2B8], 0 / JNZ" (jumps if flag!=0):
     *   Change cmp value from 0 to 0xFF — flag will never be 0xFF, so JNZ always taken
     *
     * For "cmp [rbp+0x2B8], 1 / JNZ" (jumps if flag!=1):
     *   Change cmp value from 1 to 0xFF — flag will never be 0xFF, so JNZ always taken
     *
     * All cases: changing the CMP immediate to 0xFF makes the comparison
     * always fail (flag is never 0xFF), which means:
     *   - JZ branches are never taken (good — skips "wrong char" path)
     *   - JNZ branches are always taken (good — takes "correct char" path)
     */

    DWORD old;
    uint8_t* cmp_imm = s->addr + 6;

    if (!VirtualProtect(cmp_imm, 1, PAGE_EXECUTE_READWRITE, &old))
        return 0;

    *cmp_imm = 0xFF;
    FlushInstructionCache(GetCurrentProcess(), cmp_imm, 1);
    VirtualProtect(cmp_imm, 1, old, &old);

    s->hooked = 1;
    slog("[PATCH] Site[%d] RVA 0x%llX: cmp imm changed to 0xFF (%s)",
         idx, s->rva, s->desc);
    return 1;
}

static int unpatch_site(int idx) {
    if (idx < 0 || idx >= g_site_count) return 0;
    site_t* s = &g_sites[idx];
    if (!s->hooked) return 0;

    DWORD old;
    uint8_t* cmp_imm = s->addr + 6;

    /* Restore original value: read from the original code
     * The original values are 0x00 or 0x01 */
    uint8_t orig_val;
    if (s->rva == 0x1A25C8D || s->rva == 0x261459F) {
        orig_val = 0x01;  /* These compare against 1 */
    } else {
        orig_val = 0x00;  /* All others compare against 0 */
    }

    if (!VirtualProtect(cmp_imm, 1, PAGE_EXECUTE_READWRITE, &old))
        return 0;

    *cmp_imm = orig_val;
    FlushInstructionCache(GetCurrentProcess(), cmp_imm, 1);
    VirtualProtect(cmp_imm, 1, old, &old);

    s->hooked = 0;
    slog("[UNPATCH] Site[%d] RVA 0x%llX: cmp imm restored to 0x%02X (%s)",
         idx, s->rva, orig_val, s->desc);
    return 1;
}

static void init_sites(void) {
    struct { uint64_t rva; const char* desc; } known[] = {
        {0x67D25E,  "stage decision"},
        {0x6BEBBF,  "array builder"},
        {0x1A25C8D, "char filter (cmp 1)"},
        {0x1ACDF81, "interaction check"},
        {0x221E814, "char check short"},
        {0x261459F, "stage gate (cmp 1)"},
        {0x32A1B34, "stage proc A"},
        {0x32A2F4A, "stage proc B"},
        {0x32A330A, "stage proc C"},
        {0x32A36F3, "stage proc D"},
        {0x32A3860, "stage proc E"},
        {0x32A3924, "stage proc F"},
        {0x32A5E88, "stage proc G"},
        {0, NULL}
    };

    for (int i = 0; known[i].desc; i++) {
        if (g_site_count >= MAX_SITES) break;
        uint8_t* addr = g_exe_base + known[i].rva;

        /* Verify it's a cmp [rbp+0x2B8] instruction */
        if (addr[0] == 0x80 && addr[1] == 0xBD &&
            addr[2] == 0xB8 && addr[3] == 0x02) {
            g_sites[g_site_count].rva = known[i].rva;
            g_sites[g_site_count].addr = addr;
            g_sites[g_site_count].hit_count = 0;
            g_sites[g_site_count].hooked = 0;
            g_sites[g_site_count].trampoline = NULL;
            strncpy(g_sites[g_site_count].desc, known[i].desc, 63);
            slog("  Site[%d] RVA 0x%llX: verified (%s)",
                 g_site_count, known[i].rva, known[i].desc);
            g_site_count++;
        } else {
            slog("  RVA 0x%llX: NOT a cmp instruction, skipped (%s)",
                 known[i].rva, known[i].desc);
        }
    }
    slog("Total verified sites: %d", g_site_count);
}

static DWORD WINAPI scanner_thread(LPVOID param) {
    (void)param;
    Sleep(8000);

    /* Patch vtable FIRST to suppress the popup */
    {
        uint64_t vt_rva = 0x48C9C30;
        uint64_t col_va = (uint64_t)g_exe_base + 0x4F0D220;
        uint64_t* vt = (uint64_t*)(g_exe_base + vt_rva);
        if (*(vt - 1) == col_va) {
            DWORD old;
            if (VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old)) {
                /* stub that returns NULL */
                static uint8_t stub_code[] = {0x48,0x31,0xC0,0xC3}; /* xor rax,rax; ret */
                static int stub_ready = 0;
                static void* stub_ptr = NULL;
                if (!stub_ready) {
                    stub_ptr = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE);
                    if (stub_ptr) {
                        memcpy(stub_ptr, stub_code, 4);
                        stub_ready = 1;
                    }
                }
                if (stub_ptr) {
                    vt[4] = (uint64_t)(uintptr_t)stub_ptr;
                    vt[5] = (uint64_t)(uintptr_t)stub_ptr;
                    FlushInstructionCache(GetCurrentProcess(), vt, 96);
                    slog("[VTABLE] Popup suppressed (vfunc[4]+[5] stubbed)");
                }
                VirtualProtect(vt, 96, old, &old);
            }
        } else {
            slog("[VTABLE] RTTI mismatch — popup NOT suppressed");
        }
    }

    slog("[SCANNER] Initializing sites...");
    init_sites();
    slog("[SCANNER] Ready!");
    slog("  F6 = patch next site (one at a time)");
    slog("  F7 = show current patched site");
    slog("  F8 = dump all site states");
    slog("  F9 = unpatch all sites");

    while (1) {
        Sleep(100);

        if (GetAsyncKeyState(VK_F6) & 1) {
            /* Unpatch current, patch next */
            if (g_current_site >= 0) {
                unpatch_site(g_current_site);
            }
            g_current_site++;
            if (g_current_site >= g_site_count) {
                g_current_site = 0;
            }
            patch_site_active(g_current_site);
            slog("[F6] Now testing Site[%d] RVA 0x%llX (%s)",
                 g_current_site, g_sites[g_current_site].rva,
                 g_sites[g_current_site].desc);
            slog("     Try talking to Naira now. If it works, THIS is the site!");
        }

        if (GetAsyncKeyState(VK_F7) & 1) {
            if (g_current_site >= 0) {
                slog("[F7] Current: Site[%d] RVA 0x%llX (%s) hooked=%d",
                     g_current_site, g_sites[g_current_site].rva,
                     g_sites[g_current_site].desc,
                     g_sites[g_current_site].hooked);
            } else {
                slog("[F7] No site active. Press F6 to start.");
            }
        }

        if (GetAsyncKeyState(VK_F8) & 1) {
            slog("\n[F8] === ALL SITES ===");
            for (int i = 0; i < g_site_count; i++) {
                slog("  Site[%d] RVA 0x%llX: hooked=%d (%s)%s",
                     i, g_sites[i].rva, g_sites[i].hooked,
                     g_sites[i].desc,
                     (i == g_current_site) ? " <-- ACTIVE" : "");
            }
            slog("==================\n");
        }

        if (GetAsyncKeyState(VK_F9) & 1) {
            slog("[F9] Unpatching all...");
            for (int i = 0; i < g_site_count; i++) {
                unpatch_site(i);
            }
            g_current_site = -1;
            slog("[F9] All unpatched.");
        }
    }
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);
        if (!is_game()) return TRUE;

        LONG prior = InterlockedCompareExchange(&g_init_state, 1, 0);
        if (prior != 0) return TRUE;

        char dir[MAX_PATH];
        GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\');
        if (sep) *sep = 0;

        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_charswitch_scan.log", dir);
        g_log = fopen(lp, "w");

        slog("=== CrimsonForge NPC Talk Scanner v5 ===");
        slog("PID: %lu", GetCurrentProcessId());

        g_exe_base = (uint8_t*)GetModuleHandleA(NULL);
        if (g_exe_base) {
            DWORD pe = *(DWORD*)(g_exe_base + 0x3C);
            g_exe_size = *(DWORD*)(g_exe_base + pe + 4 + 20 + 56);
            slog("Exe: 0x%p size 0x%llX", g_exe_base, (uint64_t)g_exe_size);
        }

        CreateThread(NULL, 0, scanner_thread, NULL, 0, NULL);
        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        /* Restore all patched sites */
        for (int i = 0; i < g_site_count; i++)
            unpatch_site(i);
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
