/**
 * CrimsonForge Character Unlock — WINHTTP.dll proxy
 * Game imports 12 WinHttp functions. We forward all to real System32\winhttp.dll.
 * Patches applied on DLL_PROCESS_ATTACH.
 */
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ── Real winhttp.dll ────────────────────────────────────────── */
static HMODULE g_real = NULL;
static FARPROC p[12];

static const char* names[] = {
    "WinHttpOpen","WinHttpConnect","WinHttpOpenRequest","WinHttpSendRequest",
    "WinHttpReceiveResponse","WinHttpQueryDataAvailable","WinHttpReadData",
    "WinHttpQueryHeaders","WinHttpSetOption","WinHttpSetTimeouts",
    "WinHttpSetStatusCallback","WinHttpCloseHandle"
};

static LONG g_real_loaded = 0;
static void ensure_real(void) {
    if (InterlockedCompareExchange(&g_real_loaded, 1, 0) == 0) {
        char sys[MAX_PATH];
        GetSystemDirectoryA(sys, MAX_PATH);
        strcat(sys, "\\winhttp.dll");
        g_real = LoadLibraryA(sys);
        if (g_real) {
            for (int i = 0; i < 12; i++)
                p[i] = GetProcAddress(g_real, names[i]);
        }
        InterlockedExchange(&g_real_loaded, 2);
    }
    while (g_real_loaded == 1) Sleep(0); /* wait if another thread is loading */
}

/* ── Forwarded exports ───────────────────────────────────────── */

#pragma comment(linker, "/EXPORT:WinHttpOpen=proxy_WinHttpOpen")
#pragma comment(linker, "/EXPORT:WinHttpConnect=proxy_WinHttpConnect")
#pragma comment(linker, "/EXPORT:WinHttpOpenRequest=proxy_WinHttpOpenRequest")
#pragma comment(linker, "/EXPORT:WinHttpSendRequest=proxy_WinHttpSendRequest")
#pragma comment(linker, "/EXPORT:WinHttpReceiveResponse=proxy_WinHttpReceiveResponse")
#pragma comment(linker, "/EXPORT:WinHttpQueryDataAvailable=proxy_WinHttpQueryDataAvailable")
#pragma comment(linker, "/EXPORT:WinHttpReadData=proxy_WinHttpReadData")
#pragma comment(linker, "/EXPORT:WinHttpQueryHeaders=proxy_WinHttpQueryHeaders")
#pragma comment(linker, "/EXPORT:WinHttpSetOption=proxy_WinHttpSetOption")
#pragma comment(linker, "/EXPORT:WinHttpSetTimeouts=proxy_WinHttpSetTimeouts")
#pragma comment(linker, "/EXPORT:WinHttpSetStatusCallback=proxy_WinHttpSetStatusCallback")
#pragma comment(linker, "/EXPORT:WinHttpCloseHandle=proxy_WinHttpCloseHandle")

/* Each export: call real function via stored pointer, variable args via stack passthrough */
/* Using simple wrappers — WinHttp functions use stdcall with known signatures */

void* __stdcall proxy_WinHttpOpen(void* a, DWORD b, void* c, void* d, DWORD e) {
    ensure_real();
    typedef void*(__stdcall*fn)(void*,DWORD,void*,void*,DWORD);
    return p[0] ? ((fn)p[0])(a,b,c,d,e) : NULL;
}
void* __stdcall proxy_WinHttpConnect(void* a, void* b, DWORD c, DWORD d) {
    ensure_real();
    typedef void*(__stdcall*fn)(void*,void*,DWORD,DWORD);
    return p[1] ? ((fn)p[1])(a,b,c,d) : NULL;
}
void* __stdcall proxy_WinHttpOpenRequest(void* a,void* b,void* c,void* d,void* e,void** f,DWORD g) {
    ensure_real();
    typedef void*(__stdcall*fn)(void*,void*,void*,void*,void*,void**,DWORD);
    return p[2] ? ((fn)p[2])(a,b,c,d,e,f,g) : NULL;
}
BOOL __stdcall proxy_WinHttpSendRequest(void* a,void* b,DWORD c,void* d,DWORD e,DWORD f,DWORD_PTR g) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,void*,DWORD,void*,DWORD,DWORD,DWORD_PTR);
    return p[3] ? ((fn)p[3])(a,b,c,d,e,f,g) : FALSE;
}
BOOL __stdcall proxy_WinHttpReceiveResponse(void* a, void* b) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,void*);
    return p[4] ? ((fn)p[4])(a,b) : FALSE;
}
BOOL __stdcall proxy_WinHttpQueryDataAvailable(void* a, DWORD* b) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,DWORD*);
    return p[5] ? ((fn)p[5])(a,b) : FALSE;
}
BOOL __stdcall proxy_WinHttpReadData(void* a, void* b, DWORD c, DWORD* d) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,void*,DWORD,DWORD*);
    return p[6] ? ((fn)p[6])(a,b,c,d) : FALSE;
}
BOOL __stdcall proxy_WinHttpQueryHeaders(void* a,DWORD b,void* c,void* d,DWORD* e,DWORD* f) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,DWORD,void*,void*,DWORD*,DWORD*);
    return p[7] ? ((fn)p[7])(a,b,c,d,e,f) : FALSE;
}
BOOL __stdcall proxy_WinHttpSetOption(void* a, DWORD b, void* c, DWORD d) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,DWORD,void*,DWORD);
    return p[8] ? ((fn)p[8])(a,b,c,d) : FALSE;
}
BOOL __stdcall proxy_WinHttpSetTimeouts(void* a, int b, int c, int d, int e) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*,int,int,int,int);
    return p[9] ? ((fn)p[9])(a,b,c,d,e) : FALSE;
}
void* __stdcall proxy_WinHttpSetStatusCallback(void* a, void* b, DWORD c, DWORD_PTR d) {
    ensure_real();
    typedef void*(__stdcall*fn)(void*,void*,DWORD,DWORD_PTR);
    return p[10] ? ((fn)p[10])(a,b,c,d) : NULL;
}
BOOL __stdcall proxy_WinHttpCloseHandle(void* a) {
    ensure_real();
    typedef BOOL(__stdcall*fn)(void*);
    return p[11] ? ((fn)p[11])(a) : FALSE;
}

/* ── Character unlock patches ────────────────────────────────── */
static FILE* g_log = NULL;
static void ulog(const char* fmt, ...) {
    va_list ap; if (!g_log) return;
    va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
    fprintf(g_log, "\n"); fflush(g_log);
}

static void do_patch(void) {
    uint8_t* base = (uint8_t*)GetModuleHandleA(NULL);
    if (!base) { ulog("ERROR: no base"); return; }
    ulog("Base: 0x%p", base);

    static uint8_t code[] = {0x48,0x31,0xC0,0xC3};
    void* stub = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!stub) { ulog("ERROR: alloc"); return; }
    memcpy(stub, code, 4);

    /* Patch 1: ChangeCharacterNotice popup — vtable 0x48C9C30, COL 0x4F0D220 */
    {
        uint64_t* vt = (uint64_t*)(base + 0x48C9C30);
        uint64_t ex = (uint64_t)(base + 0x4F0D220);
        if (*(vt-1) == ex) {
            DWORD old; VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
            vt[4] = vt[5] = (uint64_t)(uintptr_t)stub;
            FlushInstructionCache(GetCurrentProcess(), vt, 96);
            VirtualProtect(vt, 96, old, &old);
            ulog("[OK] Patch 1: Popup suppressed");
        } else {
            ulog("[SKIP] Patch 1: RTTI mismatch (got 0x%llX want 0x%llX)", *(vt-1), ex);
        }
    }
    /* Patch 2: ForbiddenCharacterList — vtable 0x4843160, COL 0x4EE6098 */
    {
        uint64_t* vt = (uint64_t*)(base + 0x4843160);
        uint64_t ex = (uint64_t)(base + 0x4EE6098);
        if (*(vt-1) == ex) {
            DWORD old; VirtualProtect(vt, 96, PAGE_EXECUTE_READWRITE, &old);
            vt[2] = (uint64_t)(uintptr_t)stub;
            FlushInstructionCache(GetCurrentProcess(), vt, 96);
            VirtualProtect(vt, 96, old, &old);
            ulog("[OK] Patch 2: Forbidden list bypassed");
        } else {
            ulog("[SKIP] Patch 2: RTTI mismatch (got 0x%llX want 0x%llX)", *(vt-1), ex);
        }
    }
}

/* ── DLL entry ───────────────────────────────────────────────── */
static LONG g_once = 0;
BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);

        /* Bare-bones load test */
        {
            HANDLE hf = CreateFileA("C:\\Users\\hzeem\\Desktop\\WINHTTP_LOADED.txt",
                GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (hf != INVALID_HANDLE_VALUE) {
                char msg[] = "winhttp proxy loaded\r\n";
                DWORD w; WriteFile(hf, msg, sizeof(msg)-1, &w, NULL);
                CloseHandle(hf);
            }
        }

        /* Real winhttp loaded lazily on first function call (avoids loader lock) */

        char path[MAX_PATH];
        GetModuleFileNameA(NULL, path, MAX_PATH);
        if (!strstr(path, "CrimsonDesert")) return TRUE;
        if (InterlockedCompareExchange(&g_once, 1, 0) != 0) return TRUE;

        char dir[MAX_PATH]; strcpy(dir, path);
        char* s = strrchr(dir, '\\'); if (s) *s = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_charunlock.log", dir);
        g_log = fopen(lp, "w");
        if (!g_log) { GetTempPathA(MAX_PATH, lp); strcat(lp, "crimson_charunlock.log"); g_log = fopen(lp, "w"); }
        if (!g_log) { sprintf(lp, "C:\\Users\\hzeem\\Desktop\\crimson_charunlock.log"); g_log = fopen(lp, "w"); }

        ulog("=== CrimsonForge Character Unlock v11 (winhttp proxy) ===");
        ulog("PID: %lu | Build: " __DATE__ " " __TIME__, GetCurrentProcessId());
        ulog("Exe: %s", path);
        do_patch();
        ulog("=== Ready ===");
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
        if (g_real) { FreeLibrary(g_real); g_real = NULL; }
    }
    return TRUE;
}
