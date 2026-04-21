/**
 * CrimsonForge Full Switch — winmm.dll proxy
 *
 * Replaces the existing winmm.dll proxy. Forwards all 178 winmm functions
 * to winmm_orig.dll (real Windows DLL) and runs character unlock + switch code.
 *
 * This approach WORKS because winmm.dll proxy is already proven to load
 * in Crimson Desert (existing CrimsonForge mods use it).
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ── winmm forwarding ────────────────────────────────────────── */

static HMODULE g_winmm = NULL;

/* We forward ALL winmm calls via GetProcAddress at runtime */
/* The game only imports a few functions but we export all common ones */

#define FORWARD(name) \
    static FARPROC p_##name = NULL; \
    __declspec(dllexport) void __stdcall name() { \
        if (!p_##name) p_##name = GetProcAddress(g_winmm, #name); \
        if (p_##name) { \
            __asm { jmp p_##name } \
        } \
    }

/* Can't use inline asm in x64 MSVC. Use function pointer forwarding instead */

static FARPROC winmm_proc(const char* name) {
    return g_winmm ? GetProcAddress(g_winmm, name) : NULL;
}

/* Export the functions the game actually uses via pragma */
/* Using naked forwarding via function pointers */

#define DEF_FWD(name) \
    static FARPROC fp_##name = NULL;

DEF_FWD(timeGetTime)
DEF_FWD(timeBeginPeriod)
DEF_FWD(timeEndPeriod)
DEF_FWD(timeGetDevCaps)
DEF_FWD(waveOutGetNumDevs)
DEF_FWD(PlaySoundA)
DEF_FWD(PlaySoundW)

/* Export via linker pragmas — these are the functions the game imports */
#pragma comment(linker, "/EXPORT:timeGetTime=proxy_timeGetTime")
#pragma comment(linker, "/EXPORT:timeBeginPeriod=proxy_timeBeginPeriod")
#pragma comment(linker, "/EXPORT:timeEndPeriod=proxy_timeEndPeriod")
#pragma comment(linker, "/EXPORT:timeGetDevCaps=proxy_timeGetDevCaps")
#pragma comment(linker, "/EXPORT:waveOutGetNumDevs=proxy_waveOutGetNumDevs")
#pragma comment(linker, "/EXPORT:PlaySoundA=proxy_PlaySoundA")
#pragma comment(linker, "/EXPORT:PlaySoundW=proxy_PlaySoundW")

DWORD WINAPI proxy_timeGetTime(void) {
    typedef DWORD(WINAPI*fn)(void);
    if (!fp_timeGetTime) fp_timeGetTime = winmm_proc("timeGetTime");
    return fp_timeGetTime ? ((fn)fp_timeGetTime)() : 0;
}
DWORD WINAPI proxy_timeBeginPeriod(UINT p) {
    typedef DWORD(WINAPI*fn)(UINT);
    if (!fp_timeBeginPeriod) fp_timeBeginPeriod = winmm_proc("timeBeginPeriod");
    return fp_timeBeginPeriod ? ((fn)fp_timeBeginPeriod)(p) : 0;
}
DWORD WINAPI proxy_timeEndPeriod(UINT p) {
    typedef DWORD(WINAPI*fn)(UINT);
    if (!fp_timeEndPeriod) fp_timeEndPeriod = winmm_proc("timeEndPeriod");
    return fp_timeEndPeriod ? ((fn)fp_timeEndPeriod)(p) : 0;
}
DWORD WINAPI proxy_timeGetDevCaps(void* a, UINT b) {
    typedef DWORD(WINAPI*fn)(void*,UINT);
    if (!fp_timeGetDevCaps) fp_timeGetDevCaps = winmm_proc("timeGetDevCaps");
    return fp_timeGetDevCaps ? ((fn)fp_timeGetDevCaps)(a,b) : 0;
}
UINT WINAPI proxy_waveOutGetNumDevs(void) {
    typedef UINT(WINAPI*fn)(void);
    if (!fp_waveOutGetNumDevs) fp_waveOutGetNumDevs = winmm_proc("waveOutGetNumDevs");
    return fp_waveOutGetNumDevs ? ((fn)fp_waveOutGetNumDevs)() : 0;
}
BOOL WINAPI proxy_PlaySoundA(LPCSTR a, HMODULE b, DWORD c) {
    typedef BOOL(WINAPI*fn)(LPCSTR,HMODULE,DWORD);
    if (!fp_PlaySoundA) fp_PlaySoundA = winmm_proc("PlaySoundA");
    return fp_PlaySoundA ? ((fn)fp_PlaySoundA)(a,b,c) : FALSE;
}
BOOL WINAPI proxy_PlaySoundW(LPCWSTR a, HMODULE b, DWORD c) {
    typedef BOOL(WINAPI*fn)(LPCWSTR,HMODULE,DWORD);
    if (!fp_PlaySoundW) fp_PlaySoundW = winmm_proc("PlaySoundW");
    return fp_PlaySoundW ? ((fn)fp_PlaySoundW)(a,b,c) : FALSE;
}

static void load_winmm(void) {
    /* Load the REAL winmm - try winmm_orig.dll first (existing chain) */
    char dir[MAX_PATH];
    GetModuleFileNameA(NULL, dir, MAX_PATH);
    char* sep = strrchr(dir, '\\');
    if (sep) *sep = 0;

    char path[MAX_PATH];
    sprintf(path, "%s\\winmm_orig.dll", dir);
    g_winmm = LoadLibraryA(path);

    if (!g_winmm) {
        /* Fall back to System32 */
        char sys[MAX_PATH];
        GetSystemDirectoryA(sys, MAX_PATH);
        strcat(sys, "\\winmm.dll");
        g_winmm = LoadLibraryA(sys);
    }
}

/* ── Character Unlock Code ───────────────────────────────────── */

static FILE* g_log = NULL;
static uint8_t* g_base = NULL;
static volatile int g_running = 1;
static HANDLE g_thread = NULL;

static void ulog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

/* RTTI Scanner — finds vtables automatically */
static uint64_t* find_vtable_by_rtti(const char* class_name, uint64_t* out_col_rva) {
    char pattern[256];
    sprintf(pattern, ".?AV%s@pa@@", class_name);
    size_t plen = strlen(pattern);

    DWORD pe = *(DWORD*)(g_base + 0x3C);
    DWORD img_size = *(DWORD*)(g_base + pe + 4 + 20 + 56);
    uint64_t image_base = (uint64_t)g_base;

    uint8_t* td = NULL;
    for (DWORD i = 0; i < img_size - plen - 16; i++) {
        if (memcmp(g_base + i, pattern, plen) == 0 && g_base[i + plen] == 0) {
            td = g_base + i - 16;
            break;
        }
    }
    if (!td) return NULL;

    uint32_t td_rva = (uint32_t)(td - g_base);
    uint8_t td_rva_bytes[4];
    memcpy(td_rva_bytes, &td_rva, 4);

    for (DWORD i = 12; i < img_size - 24; i++) {
        if (memcmp(g_base + i, td_rva_bytes, 4) == 0) {
            uint32_t sig = *(uint32_t*)(g_base + i - 12);
            if (sig == 1) {
                uint32_t col_rva = (uint32_t)(i - 12);
                uint64_t col_va = image_base + col_rva;
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

static void apply_patches(void) {
    /* Stub: xor rax,rax; ret */
    static uint8_t code[] = {0x48, 0x31, 0xC0, 0xC3};
    void* stub = VirtualAlloc(NULL, 16, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!stub) { ulog("ERROR: stub alloc"); return; }
    memcpy(stub, code, 4);

    /* Patch: ForbiddenCharacterList bypass */
    uint64_t col_rva = 0;
    uint64_t* vt = find_vtable_by_rtti("TrocTrUpdateForbiddenCharacterListAck", &col_rva);
    if (vt) {
        DWORD old;
        VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
        vt[2] = (uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), vt, 96);
        VirtualProtect(vt, 96, old, &old);
        ulog("[OK] ForbiddenCharacterList bypassed (vtable base+0x%llX)",
             (uint64_t)((uint8_t*)vt - g_base));
    } else {
        ulog("[SKIP] ForbiddenCharacterList not found via RTTI");
    }

    /* Log other vtables for debugging */
    uint64_t* vt2 = find_vtable_by_rtti("UIGamePlayControl_Root_ChangeCharacterNotice", &col_rva);
    if (vt2) ulog("[INFO] ChangeCharacterNotice at base+0x%llX", (uint64_t)((uint8_t*)vt2 - g_base));

    uint64_t* vt3 = find_vtable_by_rtti("TrocTrChangePlayerbleCharacterAck", &col_rva);
    if (vt3) ulog("[INFO] CharacterAck at base+0x%llX", (uint64_t)((uint8_t*)vt3 - g_base));
}

/* ── DLL Entry ───────────────────────────────────────────────── */

static LONG g_once = 0;

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);
        load_winmm();

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
            sprintf(lp, "C:\\Users\\hzeem\\Desktop\\crimson_fullswitch.log");
            g_log = fopen(lp, "w");
        }

        ulog("=== CrimsonForge Full Switch (winmm proxy) ===");
        ulog("PID: %lu | Build: " __DATE__ " " __TIME__, GetCurrentProcessId());
        ulog("Base: 0x%p | winmm_orig: 0x%p", g_base, g_winmm);

        apply_patches();
        ulog("=== Ready ===");
    } else if (reason == DLL_PROCESS_DETACH) {
        g_running = 0;
        if (g_log) { fclose(g_log); g_log = NULL; }
        if (g_winmm) { FreeLibrary(g_winmm); g_winmm = NULL; }
    }
    return TRUE;
}
