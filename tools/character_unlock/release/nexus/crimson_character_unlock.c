/**
 * CrimsonForge Character Unlock — Final Release
 *
 * Two vtable patches:
 * 1. ChangeCharacterNotice → suppresses "This is Kliff's story" popup
 * 2. ForbiddenCharacterList → bypasses "Comrade is on an important mission"
 *
 * Both patches are RTTI-verified for safety.
 * Log: bin64/crimson_charunlock.log
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static FILE* g_log = NULL;
static LONG  g_init_state = 0;

static void ulog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
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

static void do_patch(void) {
    uint8_t* base = (uint8_t*)GetModuleHandleA(NULL);
    if (!base) { ulog("ERROR: no module"); return; }
    DWORD pe = *(DWORD*)(base + 0x3C);
    size_t size = *(DWORD*)(base + pe + 4 + 20 + 56);
    ulog("Exe: 0x%p size 0x%llX", base, (uint64_t)size);

    static uint8_t code[] = {0x48,0x31,0xC0,0xC3};
    static void* stub = NULL;
    if (!stub) {
        stub = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (stub) memcpy(stub, code, 4);
    }
    if (!stub) { ulog("ERROR: stub alloc"); return; }

    uint64_t* vt1 = (uint64_t*)(base + 0x48C9C50);
    if (*(vt1-1) == (uint64_t)base + 0x4F0D490) {
        DWORD old; VirtualProtect(vt1, 96, PAGE_EXECUTE_READWRITE, &old);
        vt1[4] = (uint64_t)(uintptr_t)stub;
        vt1[5] = (uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), vt1, 96);
        VirtualProtect(vt1, 96, old, &old);
        ulog("[OK] Popup suppressed (ChangeCharacterNotice vfunc[4,5])");
    } else {
        ulog("[SKIP] ChangeCharacterNotice RTTI mismatch");
    }

    uint64_t* vt2 = (uint64_t*)(base + 0x4843208);
    if (*(vt2-1) == (uint64_t)base + 0x4EE6080) {
        DWORD old; VirtualProtect(vt2, 96, PAGE_EXECUTE_READWRITE, &old);
        vt2[2] = (uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), vt2, 96);
        VirtualProtect(vt2, 96, old, &old);
        ulog("[OK] Forbidden character list bypassed (vfunc[2])");
    } else {
        ulog("[SKIP] ForbiddenCharacterList RTTI mismatch");
    }
}

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)hDll; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);
        if (!is_game()) return TRUE;

        LONG prior = InterlockedCompareExchange(&g_init_state, 1, 0);
        if (prior != 0) return TRUE;

        char dir[MAX_PATH];
        GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\'); if (sep) *sep = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_charunlock.log", dir);
        g_log = fopen(lp, "w");

        ulog("=== CrimsonForge Character Unlock ===");
        ulog("PID: %lu", GetCurrentProcessId());
        do_patch();
        ulog("=== Done ===");

        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
