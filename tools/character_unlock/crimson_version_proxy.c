/**
 * CrimsonForge Character Unlock — version.dll proxy
 *
 * Drop-in replacement for version.dll in bin64/.
 * Forwards all version.dll exports to the real System32\version.dll
 * and applies character unlock patches on game startup.
 *
 * Patches:
 * 1. ChangeCharacterNotice vtable → suppresses "This is Kliff's story" popup
 * 2. ForbiddenCharacterList vtable → bypasses "Comrade is on an important mission"
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

/* ── Real version.dll forwarding ─────────────────────────────── */

static HMODULE g_real_version = NULL;

typedef BOOL(WINAPI *t_GetFileVersionInfoA)(LPCSTR, DWORD, DWORD, LPVOID);
typedef BOOL(WINAPI *t_GetFileVersionInfoW)(LPCWSTR, DWORD, DWORD, LPVOID);
typedef DWORD(WINAPI *t_GetFileVersionInfoSizeA)(LPCSTR, LPDWORD);
typedef DWORD(WINAPI *t_GetFileVersionInfoSizeW)(LPCWSTR, LPDWORD);
typedef BOOL(WINAPI *t_GetFileVersionInfoExA)(DWORD, LPCSTR, DWORD, DWORD, LPVOID);
typedef BOOL(WINAPI *t_GetFileVersionInfoExW)(DWORD, LPCWSTR, DWORD, DWORD, LPVOID);
typedef DWORD(WINAPI *t_GetFileVersionInfoSizeExA)(DWORD, LPCSTR, LPDWORD);
typedef DWORD(WINAPI *t_GetFileVersionInfoSizeExW)(DWORD, LPCWSTR, LPDWORD);
typedef BOOL(WINAPI *t_VerQueryValueA)(LPCVOID, LPCSTR, LPVOID*, PUINT);
typedef BOOL(WINAPI *t_VerQueryValueW)(LPCVOID, LPCWSTR, LPVOID*, PUINT);
typedef DWORD(WINAPI *t_VerFindFileA)(DWORD, LPCSTR, LPCSTR, LPCSTR, LPSTR, PUINT, LPSTR, PUINT);
typedef DWORD(WINAPI *t_VerFindFileW)(DWORD, LPCWSTR, LPCWSTR, LPCWSTR, LPWSTR, PUINT, LPWSTR, PUINT);
typedef DWORD(WINAPI *t_VerInstallFileA)(DWORD, LPCSTR, LPCSTR, LPCSTR, LPCSTR, LPCSTR, LPSTR, PUINT);
typedef DWORD(WINAPI *t_VerInstallFileW)(DWORD, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, LPWSTR, PUINT);
typedef DWORD(WINAPI *t_VerLanguageNameA)(DWORD, LPSTR, DWORD);
typedef DWORD(WINAPI *t_VerLanguageNameW)(DWORD, LPWSTR, DWORD);
typedef int(WINAPI *t_GetFileVersionInfoByHandle)(DWORD, LPCWSTR, DWORD, DWORD, LPVOID);

static t_GetFileVersionInfoA       p_GetFileVersionInfoA;
static t_GetFileVersionInfoW       p_GetFileVersionInfoW;
static t_GetFileVersionInfoSizeA   p_GetFileVersionInfoSizeA;
static t_GetFileVersionInfoSizeW   p_GetFileVersionInfoSizeW;
static t_GetFileVersionInfoExA     p_GetFileVersionInfoExA;
static t_GetFileVersionInfoExW     p_GetFileVersionInfoExW;
static t_GetFileVersionInfoSizeExA p_GetFileVersionInfoSizeExA;
static t_GetFileVersionInfoSizeExW p_GetFileVersionInfoSizeExW;
static t_VerQueryValueA            p_VerQueryValueA;
static t_VerQueryValueW            p_VerQueryValueW;
static t_VerFindFileA              p_VerFindFileA;
static t_VerFindFileW              p_VerFindFileW;
static t_VerInstallFileA           p_VerInstallFileA;
static t_VerInstallFileW           p_VerInstallFileW;
static t_VerLanguageNameA          p_VerLanguageNameA;
static t_VerLanguageNameW          p_VerLanguageNameW;
static t_GetFileVersionInfoByHandle p_GetFileVersionInfoByHandle;

static void load_real_version(void) {
    char sys[MAX_PATH];
    GetSystemDirectoryA(sys, MAX_PATH);
    strcat(sys, "\\version.dll");
    g_real_version = LoadLibraryA(sys);
    if (!g_real_version) return;

    p_GetFileVersionInfoA       = (t_GetFileVersionInfoA)GetProcAddress(g_real_version, "GetFileVersionInfoA");
    p_GetFileVersionInfoW       = (t_GetFileVersionInfoW)GetProcAddress(g_real_version, "GetFileVersionInfoW");
    p_GetFileVersionInfoSizeA   = (t_GetFileVersionInfoSizeA)GetProcAddress(g_real_version, "GetFileVersionInfoSizeA");
    p_GetFileVersionInfoSizeW   = (t_GetFileVersionInfoSizeW)GetProcAddress(g_real_version, "GetFileVersionInfoSizeW");
    p_GetFileVersionInfoExA     = (t_GetFileVersionInfoExA)GetProcAddress(g_real_version, "GetFileVersionInfoExA");
    p_GetFileVersionInfoExW     = (t_GetFileVersionInfoExW)GetProcAddress(g_real_version, "GetFileVersionInfoExW");
    p_GetFileVersionInfoSizeExA = (t_GetFileVersionInfoSizeExA)GetProcAddress(g_real_version, "GetFileVersionInfoSizeExA");
    p_GetFileVersionInfoSizeExW = (t_GetFileVersionInfoSizeExW)GetProcAddress(g_real_version, "GetFileVersionInfoSizeExW");
    p_VerQueryValueA            = (t_VerQueryValueA)GetProcAddress(g_real_version, "VerQueryValueA");
    p_VerQueryValueW            = (t_VerQueryValueW)GetProcAddress(g_real_version, "VerQueryValueW");
    p_VerFindFileA              = (t_VerFindFileA)GetProcAddress(g_real_version, "VerFindFileA");
    p_VerFindFileW              = (t_VerFindFileW)GetProcAddress(g_real_version, "VerFindFileW");
    p_VerInstallFileA           = (t_VerInstallFileA)GetProcAddress(g_real_version, "VerInstallFileA");
    p_VerInstallFileW           = (t_VerInstallFileW)GetProcAddress(g_real_version, "VerInstallFileW");
    p_VerLanguageNameA          = (t_VerLanguageNameA)GetProcAddress(g_real_version, "VerLanguageNameA");
    p_VerLanguageNameW          = (t_VerLanguageNameW)GetProcAddress(g_real_version, "VerLanguageNameW");
    p_GetFileVersionInfoByHandle = (t_GetFileVersionInfoByHandle)GetProcAddress(g_real_version, "GetFileVersionInfoByHandle");
}

/* Forwarded exports */
__declspec(dllexport) BOOL WINAPI Export_GetFileVersionInfoA(LPCSTR a, DWORD b, DWORD c, LPVOID d)
    { return p_GetFileVersionInfoA ? p_GetFileVersionInfoA(a,b,c,d) : FALSE; }
__declspec(dllexport) BOOL WINAPI Export_GetFileVersionInfoW(LPCWSTR a, DWORD b, DWORD c, LPVOID d)
    { return p_GetFileVersionInfoW ? p_GetFileVersionInfoW(a,b,c,d) : FALSE; }
__declspec(dllexport) DWORD WINAPI Export_GetFileVersionInfoSizeA(LPCSTR a, LPDWORD b)
    { return p_GetFileVersionInfoSizeA ? p_GetFileVersionInfoSizeA(a,b) : 0; }
__declspec(dllexport) DWORD WINAPI Export_GetFileVersionInfoSizeW(LPCWSTR a, LPDWORD b)
    { return p_GetFileVersionInfoSizeW ? p_GetFileVersionInfoSizeW(a,b) : 0; }
__declspec(dllexport) BOOL WINAPI Export_GetFileVersionInfoExA(DWORD f, LPCSTR a, DWORD b, DWORD c, LPVOID d)
    { return p_GetFileVersionInfoExA ? p_GetFileVersionInfoExA(f,a,b,c,d) : FALSE; }
__declspec(dllexport) BOOL WINAPI Export_GetFileVersionInfoExW(DWORD f, LPCWSTR a, DWORD b, DWORD c, LPVOID d)
    { return p_GetFileVersionInfoExW ? p_GetFileVersionInfoExW(f,a,b,c,d) : FALSE; }
__declspec(dllexport) DWORD WINAPI Export_GetFileVersionInfoSizeExA(DWORD f, LPCSTR a, LPDWORD b)
    { return p_GetFileVersionInfoSizeExA ? p_GetFileVersionInfoSizeExA(f,a,b) : 0; }
__declspec(dllexport) DWORD WINAPI Export_GetFileVersionInfoSizeExW(DWORD f, LPCWSTR a, LPDWORD b)
    { return p_GetFileVersionInfoSizeExW ? p_GetFileVersionInfoSizeExW(f,a,b) : 0; }
__declspec(dllexport) BOOL WINAPI Export_VerQueryValueA(LPCVOID a, LPCSTR b, LPVOID* c, PUINT d)
    { return p_VerQueryValueA ? p_VerQueryValueA(a,b,c,d) : FALSE; }
__declspec(dllexport) BOOL WINAPI Export_VerQueryValueW(LPCVOID a, LPCWSTR b, LPVOID* c, PUINT d)
    { return p_VerQueryValueW ? p_VerQueryValueW(a,b,c,d) : FALSE; }
__declspec(dllexport) DWORD WINAPI Export_VerFindFileA(DWORD a, LPCSTR b, LPCSTR c, LPCSTR d, LPSTR e, PUINT f, LPSTR g, PUINT h)
    { return p_VerFindFileA ? p_VerFindFileA(a,b,c,d,e,f,g,h) : 0; }
__declspec(dllexport) DWORD WINAPI Export_VerFindFileW(DWORD a, LPCWSTR b, LPCWSTR c, LPCWSTR d, LPWSTR e, PUINT f, LPWSTR g, PUINT h)
    { return p_VerFindFileW ? p_VerFindFileW(a,b,c,d,e,f,g,h) : 0; }
__declspec(dllexport) DWORD WINAPI Export_VerInstallFileA(DWORD a, LPCSTR b, LPCSTR c, LPCSTR d, LPCSTR e, LPCSTR f, LPSTR g, PUINT h)
    { return p_VerInstallFileA ? p_VerInstallFileA(a,b,c,d,e,f,g,h) : 0; }
__declspec(dllexport) DWORD WINAPI Export_VerInstallFileW(DWORD a, LPCWSTR b, LPCWSTR c, LPCWSTR d, LPCWSTR e, LPCWSTR f, LPWSTR g, PUINT h)
    { return p_VerInstallFileW ? p_VerInstallFileW(a,b,c,d,e,f,g,h) : 0; }
__declspec(dllexport) DWORD WINAPI Export_VerLanguageNameA(DWORD a, LPSTR b, DWORD c)
    { return p_VerLanguageNameA ? p_VerLanguageNameA(a,b,c) : 0; }
__declspec(dllexport) DWORD WINAPI Export_VerLanguageNameW(DWORD a, LPWSTR b, DWORD c)
    { return p_VerLanguageNameW ? p_VerLanguageNameW(a,b,c) : 0; }
__declspec(dllexport) int WINAPI Export_GetFileVersionInfoByHandle(DWORD a, LPCWSTR b, DWORD c, DWORD d, LPVOID e)
    { return p_GetFileVersionInfoByHandle ? p_GetFileVersionInfoByHandle(a,b,c,d,e) : 0; }


/* ── Character unlock patches ────────────────────────────────── */

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

    /* Stub: xor rax,rax; ret — returns 0/NULL */
    static uint8_t code[] = {0x48,0x31,0xC0,0xC3};
    static void* stub = NULL;
    if (!stub) {
        stub = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (stub) memcpy(stub, code, 4);
    }
    if (!stub) { ulog("ERROR: stub alloc"); return; }

    /* Patch 1: ChangeCharacterNotice — suppress popup */
    uint64_t* vt1 = (uint64_t*)(base + 0x48C9C30);
    if (*(vt1-1) == (uint64_t)base + 0x4F0D220) {
        DWORD old; VirtualProtect(vt1, 96, PAGE_EXECUTE_READWRITE, &old);
        vt1[4] = (uint64_t)(uintptr_t)stub;
        vt1[5] = (uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), vt1, 96);
        VirtualProtect(vt1, 96, old, &old);
        ulog("[OK] Popup suppressed (ChangeCharacterNotice vfunc[4,5])");
    } else {
        ulog("[SKIP] ChangeCharacterNotice RTTI mismatch (game version changed?)");
    }

    /* Patch 2: ForbiddenCharacterList — bypass lock */
    uint64_t* vt2 = (uint64_t*)(base + 0x4843160);
    if (*(vt2-1) == (uint64_t)base + 0x4EE6098) {
        DWORD old; VirtualProtect(vt2, 96, PAGE_EXECUTE_READWRITE, &old);
        vt2[2] = (uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), vt2, 96);
        VirtualProtect(vt2, 96, old, &old);
        ulog("[OK] Forbidden character list bypassed (vfunc[2])");
    } else {
        ulog("[SKIP] ForbiddenCharacterList RTTI mismatch (game version changed?)");
    }
}


/* ── DLL entry point ─────────────────────────────────────────── */

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)hDll; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);

        /* Always load real version.dll for forwarding */
        load_real_version();

        if (!is_game()) return TRUE;

        LONG prior = InterlockedCompareExchange(&g_init_state, 1, 0);
        if (prior != 0) return TRUE;

        char dir[MAX_PATH];
        GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\'); if (sep) *sep = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_charunlock.log", dir);
        g_log = fopen(lp, "w");

        ulog("=== CrimsonForge Character Unlock (version.dll proxy) ===");
        ulog("PID: %lu", GetCurrentProcessId());
        do_patch();
        ulog("=== Done ===");

        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
        if (g_real_version) { FreeLibrary(g_real_version); g_real_version = NULL; }
    }
    return TRUE;
}
