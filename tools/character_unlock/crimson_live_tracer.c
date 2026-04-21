/**
 * CrimsonForge Availability Tracer v4 — Fast Key Search
 *
 * Searches for the localization key 4640198652743123120
 * ("Comrade is on an important mission") as a uint64 in memory.
 * This is much faster than searching for wide strings.
 *
 * Also searches for the "NonSwitchable" UI key as ASCII in heap.
 *
 * F7 = scan for loc key in heap
 * F8 = mark
 *
 * Includes vtable popup patch.
 * Log: bin64/crimson_live_trace.log
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static FILE*    g_log = NULL;
static LONG     g_init_state = 0;
static uint8_t* g_exe_base = NULL;
static size_t   g_exe_size = 0;

static void tlog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_log, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

static int is_game(void) {
    char p[MAX_PATH]; GetModuleFileNameA(NULL, p, MAX_PATH);
    return strstr(p, "CrimsonDesert") != NULL;
}

static void patch_vtable(void) {
    uint64_t vt_rva = 0x48C9C30;
    uint64_t col_va = (uint64_t)g_exe_base + 0x4F0D220;
    uint64_t* vt = (uint64_t*)(g_exe_base + vt_rva);
    if (*(vt - 1) != col_va) return;
    static uint8_t stub[] = {0x48,0x31,0xC0,0xC3};
    static void* sm = NULL;
    if (!sm) { sm = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE); if(sm) memcpy(sm, stub, 4); }
    if (!sm) return;
    DWORD old;
    if (VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old)) {
        vt[4] = (uint64_t)(uintptr_t)sm; vt[5] = (uint64_t)(uintptr_t)sm;
        FlushInstructionCache(GetCurrentProcess(), vt, 96);
        VirtualProtect(vt, 96, old, &old);
        tlog("[VT] popup suppressed");
    }
}

static void scan_for_key(void) {
    /* Search for "NonSwitchable_ImportantQuest" as ASCII in heap
     * This is the UI key that the code uses BEFORE resolving to localized text.
     * It's shorter and faster to find than the wide string.
     */
    const char* needle = "NonSwitchable_ImportantQuest";
    size_t needle_len = strlen(needle);

    MEMORY_BASIC_INFORMATION mbi;
    uint8_t* addr = NULL;
    int found = 0;

    tlog("  Scanning for \"%s\" in heap...", needle);

    while (VirtualQuery(addr, &mbi, sizeof(mbi))) {
        if (mbi.State == MEM_COMMIT &&
            (mbi.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE | PAGE_READONLY | PAGE_EXECUTE_READ)) &&
            mbi.RegionSize > 0x100 && mbi.RegionSize < 0x10000000) {

            uint8_t* base = (uint8_t*)mbi.BaseAddress;
            size_t sz = mbi.RegionSize;

            /* Skip the exe module itself (we already know it's there) */
            if (base >= g_exe_base && base < g_exe_base + g_exe_size) {
                addr = g_exe_base + g_exe_size;
                continue;
            }

            for (size_t i = 0; i + needle_len < sz; i++) {
                __try {
                    if (memcmp(base + i, needle, needle_len) == 0) {
                        tlog("    FOUND at 0x%p (region 0x%p+0x%llX, prot=0x%X)",
                             base + i, base, (uint64_t)i, mbi.Protect);

                        /* Dump 128 bytes before and after */
                        int pre = 128;
                        uint8_t* dstart = (i >= (size_t)pre) ? base + i - pre : base;
                        for (int d = 0; d < 256; d += 32) {
                            char hex[200] = {0};
                            char asc[40] = {0};
                            int hp = 0, ap = 0;
                            for (int b = 0; b < 32; b++) {
                                uint8_t byte;
                                __try { byte = dstart[d + b]; }
                                __except(EXCEPTION_EXECUTE_HANDLER) { byte = 0; }
                                hp += sprintf(hex + hp, "%02X ", byte);
                                asc[ap++] = (byte >= 32 && byte < 127) ? (char)byte : '.';
                            }
                            asc[ap] = 0;
                            int64_t rel = (int64_t)(dstart + d) - (int64_t)(base + i);
                            tlog("      %+5lld: %-96s %s", (long long)rel, hex, asc);
                        }

                        found++;
                        if (found >= 10) goto done;
                    }
                } __except(EXCEPTION_EXECUTE_HANDLER) {
                    break;
                }
            }
        }
        addr = (uint8_t*)mbi.BaseAddress + mbi.RegionSize;
        if ((uint64_t)addr < (uint64_t)mbi.BaseAddress) break;
    }
done:
    tlog("  Found %d heap instances (excludes exe image)", found);
}

/* Also search for the "ChangeCharacter" UI key as ASCII */
static void scan_for_change_char(void) {
    const char* needle = "UI_MercenaryQuickSlot_NonSwitchable";
    size_t needle_len = strlen(needle);

    MEMORY_BASIC_INFORMATION mbi;
    uint8_t* addr = NULL;
    int found = 0;

    tlog("  Scanning for \"%s\" in heap...", needle);

    while (VirtualQuery(addr, &mbi, sizeof(mbi))) {
        if (mbi.State == MEM_COMMIT &&
            (mbi.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE)) &&
            mbi.RegionSize > 0x100 && mbi.RegionSize < 0x10000000) {

            uint8_t* base = (uint8_t*)mbi.BaseAddress;
            size_t sz = mbi.RegionSize;
            if (base >= g_exe_base && base < g_exe_base + g_exe_size) {
                addr = g_exe_base + g_exe_size;
                continue;
            }

            for (size_t i = 0; i + needle_len < sz; i++) {
                __try {
                    if (memcmp(base + i, needle, needle_len) == 0) {
                        /* Read full string */
                        char full[256] = {0};
                        size_t j;
                        for (j = 0; j < 255 && i + j < sz; j++) {
                            full[j] = base[i + j];
                            if (full[j] == 0) break;
                        }
                        tlog("    FOUND at 0x%p: \"%s\"", base + i, full);

                        /* Dump struct around it */
                        int pre = 64;
                        uint8_t* dstart = (i >= (size_t)pre) ? base + i - pre : base;
                        for (int d = 0; d < 192; d += 32) {
                            char hex[200] = {0};
                            int hp = 0;
                            for (int b = 0; b < 32; b++) {
                                uint8_t byte;
                                __try { byte = dstart[d + b]; }
                                __except(EXCEPTION_EXECUTE_HANDLER) { byte = 0; }
                                hp += sprintf(hex + hp, "%02X ", byte);
                            }
                            int64_t rel = (int64_t)(dstart + d) - (int64_t)(base + i);
                            tlog("      %+5lld: %s", (long long)rel, hex);
                        }

                        found++;
                        if (found >= 5) goto done2;
                    }
                } __except(EXCEPTION_EXECUTE_HANDLER) {
                    break;
                }
            }
        }
        addr = (uint8_t*)mbi.BaseAddress + mbi.RegionSize;
        if ((uint64_t)addr < (uint64_t)mbi.BaseAddress) break;
    }
done2:
    tlog("  Found %d heap instances", found);
}

static DWORD WINAPI monitor_thread(LPVOID param) {
    (void)param;
    Sleep(8000);
    tlog("[MON] Ready. F7=scan for NonSwitchable key, F5=scan UI key, F8=mark");

    while (1) {
        Sleep(100);

        if (GetAsyncKeyState(VK_F7) & 1) {
            tlog("\n[F7] === NONSWITCHABLE KEY SCAN ===");
            scan_for_key();
            tlog("[F7] === DONE ===\n");
        }

        if (GetAsyncKeyState(VK_F5) & 1) {
            tlog("\n[F5] === UI KEY SCAN ===");
            scan_for_change_char();
            tlog("[F5] === DONE ===\n");
        }

        if (GetAsyncKeyState(VK_F8) & 1) {
            tlog("\n======== MARK ========\n");
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

        g_exe_base = (uint8_t*)GetModuleHandleA(NULL);
        if (g_exe_base) {
            DWORD pe = *(DWORD*)(g_exe_base + 0x3C);
            g_exe_size = *(DWORD*)(g_exe_base + pe + 4 + 20 + 56);
        }

        char dir[MAX_PATH];
        GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\'); if (sep) *sep = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_live_trace.log", dir);
        g_log = fopen(lp, "w");

        tlog("=== CrimsonForge Availability Tracer v4 ===");
        patch_vtable();
        CreateThread(NULL, 0, monitor_thread, NULL, 0, NULL);
        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
