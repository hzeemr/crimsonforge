/**
 * CrimsonForge Character Unlock — xinput1_4.dll proxy v2
 *
 * EXACT match of real xinput1_4.dll export table:
 *   Named: @1 DllMain, @2 XInputGetState, @3 XInputSetState,
 *          @4 XInputGetCapabilities, @5 XInputEnable,
 *          @7 XInputGetBatteryInformation, @8 XInputGetKeystroke,
 *          @10 XInputGetAudioDeviceIds
 *   Hidden: @100-@104, @108, @109 (ordinal-only, used by many games)
 *
 * Patches applied on game load:
 * 1. ChangeCharacterNotice vtable → suppresses "This is Kliff's story" popup
 * 2. ForbiddenCharacterList vtable → bypasses "Comrade is on an important mission"
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ── Real xinput1_4.dll ──────────────────────────────────────── */

static HMODULE g_real = NULL;

typedef DWORD(WINAPI *fn_get)(DWORD, void*);
typedef DWORD(WINAPI *fn_set)(DWORD, void*);
typedef DWORD(WINAPI *fn_cap)(DWORD, DWORD, void*);
typedef void (WINAPI *fn_ena)(BOOL);
typedef DWORD(WINAPI *fn_aud)(DWORD, void*, void*, void*, void*);
typedef DWORD(WINAPI *fn_bat)(DWORD, BYTE, void*);
typedef DWORD(WINAPI *fn_key)(DWORD, DWORD, void*);

static fn_get r_GetState;
static fn_set r_SetState;
static fn_cap r_GetCaps;
static fn_ena r_Enable;
static fn_aud r_GetAudio;
static fn_bat r_GetBatt;
static fn_key r_GetKey;

/* Hidden ordinal function pointers */
static FARPROC r_ord100, r_ord101, r_ord102, r_ord103, r_ord104, r_ord108, r_ord109;

static void load_real(void) {
    char sys[MAX_PATH];
    GetSystemDirectoryA(sys, MAX_PATH);
    strcat(sys, "\\xinput1_4.dll");
    g_real = LoadLibraryA(sys);
    if (!g_real) return;

    r_GetState = (fn_get)GetProcAddress(g_real, "XInputGetState");
    r_SetState = (fn_set)GetProcAddress(g_real, "XInputSetState");
    r_GetCaps  = (fn_cap)GetProcAddress(g_real, "XInputGetCapabilities");
    r_Enable   = (fn_ena)GetProcAddress(g_real, "XInputEnable");
    r_GetAudio = (fn_aud)GetProcAddress(g_real, "XInputGetAudioDeviceIds");
    r_GetBatt  = (fn_bat)GetProcAddress(g_real, "XInputGetBatteryInformation");
    r_GetKey   = (fn_key)GetProcAddress(g_real, "XInputGetKeystroke");

    r_ord100 = GetProcAddress(g_real, (LPCSTR)100);
    r_ord101 = GetProcAddress(g_real, (LPCSTR)101);
    r_ord102 = GetProcAddress(g_real, (LPCSTR)102);
    r_ord103 = GetProcAddress(g_real, (LPCSTR)103);
    r_ord104 = GetProcAddress(g_real, (LPCSTR)104);
    r_ord108 = GetProcAddress(g_real, (LPCSTR)108);
    r_ord109 = GetProcAddress(g_real, (LPCSTR)109);
}

/* ── Exports: named (@2-@10) ─────────────────────────────────── */

#pragma comment(linker, "/EXPORT:XInputGetState,@2")
#pragma comment(linker, "/EXPORT:XInputSetState,@3")
#pragma comment(linker, "/EXPORT:XInputGetCapabilities,@4")
#pragma comment(linker, "/EXPORT:XInputEnable,@5")
#pragma comment(linker, "/EXPORT:XInputGetBatteryInformation,@7")
#pragma comment(linker, "/EXPORT:XInputGetKeystroke,@8")
#pragma comment(linker, "/EXPORT:XInputGetAudioDeviceIds,@10")

DWORD WINAPI XInputGetState(DWORD i, void* s) {
    return r_GetState ? r_GetState(i, s) : 0x48F;
}
DWORD WINAPI XInputSetState(DWORD i, void* v) {
    return r_SetState ? r_SetState(i, v) : 0x48F;
}
DWORD WINAPI XInputGetCapabilities(DWORD i, DWORD f, void* c) {
    return r_GetCaps ? r_GetCaps(i, f, c) : 0x48F;
}
void WINAPI XInputEnable(BOOL e) {
    if (r_Enable) r_Enable(e);
}
DWORD WINAPI XInputGetBatteryInformation(DWORD i, BYTE t, void* b) {
    return r_GetBatt ? r_GetBatt(i, t, b) : 0x48F;
}
DWORD WINAPI XInputGetKeystroke(DWORD i, DWORD r, void* k) {
    return r_GetKey ? r_GetKey(i, r, k) : 0x48F;
}
DWORD WINAPI XInputGetAudioDeviceIds(DWORD a, void* b, void* c, void* d, void* e) {
    return r_GetAudio ? r_GetAudio(a, b, c, d, e) : 0x48F;
}

/* ── Exports: hidden ordinal-only (@100-@104, @108, @109) ──── */

#pragma comment(linker, "/EXPORT:HiddenGetState=HiddenGetState,@100,NONAME")
#pragma comment(linker, "/EXPORT:HiddenSetState=HiddenSetState,@101,NONAME")
#pragma comment(linker, "/EXPORT:HiddenGetCaps=HiddenGetCaps,@102,NONAME")
#pragma comment(linker, "/EXPORT:HiddenEnable=HiddenEnable,@103,NONAME")
#pragma comment(linker, "/EXPORT:HiddenGetAudio=HiddenGetAudio,@104,NONAME")
#pragma comment(linker, "/EXPORT:HiddenGetBusInfo=HiddenGetBusInfo,@108,NONAME")
#pragma comment(linker, "/EXPORT:HiddenGetCapsEx=HiddenGetCapsEx,@109,NONAME")

DWORD WINAPI HiddenGetState(DWORD i, void* s) {
    return r_ord100 ? ((fn_get)r_ord100)(i, s) : (r_GetState ? r_GetState(i, s) : 0x48F);
}
DWORD WINAPI HiddenSetState(DWORD i, void* v) {
    return r_ord101 ? ((fn_set)r_ord101)(i, v) : (r_SetState ? r_SetState(i, v) : 0x48F);
}
DWORD WINAPI HiddenGetCaps(DWORD i, DWORD f, void* c) {
    return r_ord102 ? ((fn_cap)r_ord102)(i, f, c) : (r_GetCaps ? r_GetCaps(i, f, c) : 0x48F);
}
void WINAPI HiddenEnable(BOOL e) {
    if (r_ord103) ((fn_ena)r_ord103)(e);
    else if (r_Enable) r_Enable(e);
}
DWORD WINAPI HiddenGetAudio(DWORD a, void* b, void* c, void* d, void* e) {
    return r_ord104 ? ((fn_aud)r_ord104)(a, b, c, d, e) : 0x48F;
}
DWORD WINAPI HiddenGetBusInfo(DWORD i, void* b) {
    if (r_ord108) return ((DWORD(WINAPI*)(DWORD,void*))r_ord108)(i, b);
    return 0x48F;
}
DWORD WINAPI HiddenGetCapsEx(DWORD u, DWORD i, void* c) {
    if (r_ord109) return ((DWORD(WINAPI*)(DWORD,DWORD,void*))r_ord109)(u, i, c);
    return 0x48F;
}


/* ── Character Unlock Patches ────────────────────────────────── */

static FILE* g_log = NULL;

static void ulog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

static void do_patch(void) {
    uint8_t* base = (uint8_t*)GetModuleHandleA(NULL);
    if (!base) { ulog("ERROR: no base module"); return; }

    ulog("Base: 0x%p", base);

    /* Stub: xor rax,rax; ret — returns 0/NULL/false */
    static uint8_t code[] = {0x48, 0x31, 0xC0, 0xC3};
    void* stub = VirtualAlloc(NULL, 16, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!stub) { ulog("ERROR: VirtualAlloc failed"); return; }
    memcpy(stub, code, 4);
    ulog("Stub: 0x%p", stub);

    /* Patch 1: ChangeCharacterNotice popup suppression
     * Vtable RVA: 0x48C9C50  |  RTTI COL RVA: 0x4F0D490  (updated game) */
    {
        uint64_t* vt = (uint64_t*)(base + 0x48C9C50);
        uint64_t expect = (uint64_t)(base + 0x4F0D490);
        if (*(vt - 1) == expect) {
            DWORD old;
            VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
            vt[4] = (uint64_t)(uintptr_t)stub;
            vt[5] = (uint64_t)(uintptr_t)stub;
            FlushInstructionCache(GetCurrentProcess(), vt, 96);
            VirtualProtect(vt, 96, old, &old);
            ulog("[OK] Patch 1: Popup suppressed (ChangeCharacterNotice vfunc[4,5])");
        } else {
            ulog("[SKIP] Patch 1: RTTI mismatch (expected 0x%llX got 0x%llX)",
                 (unsigned long long)expect, (unsigned long long)*(vt - 1));
        }
    }

    /* Patch 2: ForbiddenCharacterList bypass
     * Vtable RVA: 0x4843208  |  RTTI COL RVA: 0x4EE6080  (updated game) */
    {
        uint64_t* vt = (uint64_t*)(base + 0x4843208);
        uint64_t expect = (uint64_t)(base + 0x4EE6080);
        if (*(vt - 1) == expect) {
            DWORD old;
            VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
            vt[2] = (uint64_t)(uintptr_t)stub;
            FlushInstructionCache(GetCurrentProcess(), vt, 96);
            VirtualProtect(vt, 96, old, &old);
            ulog("[OK] Patch 2: Forbidden list bypassed (vfunc[2])");
        } else {
            ulog("[SKIP] Patch 2: RTTI mismatch (expected 0x%llX got 0x%llX)",
                 (unsigned long long)expect, (unsigned long long)*(vt - 1));
        }
    }
}

/* ── DLL entry ───────────────────────────────────────────────── */

static LONG g_once = 0;

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);

        /* Bare-bones load indicator — no dependencies, just Win32 API */
        {
            HANDLE hf = CreateFileA("C:\\Users\\hzeem\\Desktop\\XINPUT_LOADED.txt",
                GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (hf != INVALID_HANDLE_VALUE) {
                char msg[] = "xinput1_4 proxy DllMain fired\r\n";
                DWORD w; WriteFile(hf, msg, sizeof(msg)-1, &w, NULL);
                CloseHandle(hf);
            }
        }

        load_real();

        /* Only patch CrimsonDesert.exe */
        char path[MAX_PATH];
        GetModuleFileNameA(NULL, path, MAX_PATH);
        if (!strstr(path, "CrimsonDesert")) return TRUE;
        if (InterlockedCompareExchange(&g_once, 1, 0) != 0) return TRUE;

        /* Log — try game dir, then TEMP, then Desktop */
        char dir[MAX_PATH];
        strcpy(dir, path);
        char* sep = strrchr(dir, '\\');
        if (sep) *sep = 0;

        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_charunlock.log", dir);
        g_log = fopen(lp, "w");
        if (!g_log) {
            GetTempPathA(MAX_PATH, lp);
            strcat(lp, "crimson_charunlock.log");
            g_log = fopen(lp, "w");
        }
        if (!g_log) {
            sprintf(lp, "C:\\Users\\hzeem\\Desktop\\crimson_charunlock.log");
            g_log = fopen(lp, "w");
        }

        ulog("=== CrimsonForge Character Unlock v10 ===");
        ulog("PID: %lu | Build: " __DATE__ " " __TIME__, GetCurrentProcessId());
        ulog("Exe: %s", path);
        ulog("Real xinput: 0x%p", g_real);
        do_patch();
        ulog("=== Ready ===");
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
        if (g_real) { FreeLibrary(g_real); g_real = NULL; }
    }
    return TRUE;
}
