/**
 * CrimsonForge Character Unlock — winmm.dll Proxy
 *
 * Separate from the xinput ASI loader — no conflicts.
 * Hooks the quest dialog populate function at 0x0EE3D20
 * to make quest dialogs available for all characters.
 *
 * Also includes vtable patches for popup + forbidden.
 *
 * Deploy: copy winmm.dll to bin64/ (game imports winmm)
 * Log: bin64/crimson_winmm_hook.log
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ── winmm proxy forwards ── */
static HMODULE g_orig_winmm = NULL;

typedef DWORD (WINAPI *timeGetTime_t)(void);
typedef UINT (WINAPI *timeBeginPeriod_t)(UINT);
typedef UINT (WINAPI *timeEndPeriod_t)(UINT);

static timeGetTime_t orig_timeGetTime = NULL;
static timeBeginPeriod_t orig_timeBeginPeriod = NULL;
static timeEndPeriod_t orig_timeEndPeriod = NULL;

/* Generic forwarder type */
typedef DWORD (WINAPI *generic_func_t)();
static generic_func_t orig_funcs[20] = {0};

/* Forward all winmm exports to the real winmm.dll */
DWORD WINAPI timeGetTime(void) {
    return orig_timeGetTime ? orig_timeGetTime() : 0;
}
UINT WINAPI timeBeginPeriod(UINT p) {
    return orig_timeBeginPeriod ? orig_timeBeginPeriod(p) : 0;
}
UINT WINAPI timeEndPeriod(UINT p) {
    return orig_timeEndPeriod ? orig_timeEndPeriod(p) : 0;
}

/* These are forwarded via GetProcAddress at runtime */
typedef UINT (WINAPI *timeGetDevCaps_t)(void*, UINT);
typedef UINT (WINAPI *waveOut_generic_t)();
static timeGetDevCaps_t orig_timeGetDevCaps = NULL;
static waveOut_generic_t orig_waveOutGetNumDevs = NULL;
static waveOut_generic_t orig_waveOutOpen = NULL;
static waveOut_generic_t orig_waveOutClose = NULL;
static waveOut_generic_t orig_waveOutWrite = NULL;
static waveOut_generic_t orig_waveOutPrepareHeader = NULL;
static waveOut_generic_t orig_waveOutUnprepareHeader = NULL;
static waveOut_generic_t orig_waveOutReset = NULL;
static waveOut_generic_t orig_waveOutGetPosition = NULL;

UINT WINAPI timeGetDevCaps(void* ptc, UINT cbtc) {
    return orig_timeGetDevCaps ? orig_timeGetDevCaps(ptc, cbtc) : 0;
}
UINT WINAPI waveOutGetNumDevs(void) {
    return orig_waveOutGetNumDevs ? orig_waveOutGetNumDevs() : 0;
}
/* Use variadic forwarding for complex functions */
UINT WINAPI waveOutOpen(void* a, UINT b, void* c, DWORD_PTR d, DWORD_PTR e, DWORD f) {
    typedef UINT (WINAPI *fn_t)(void*, UINT, void*, DWORD_PTR, DWORD_PTR, DWORD);
    return orig_waveOutOpen ? ((fn_t)orig_waveOutOpen)(a,b,c,d,e,f) : 0;
}
UINT WINAPI waveOutClose(void* a) {
    typedef UINT (WINAPI *fn_t)(void*);
    return orig_waveOutClose ? ((fn_t)orig_waveOutClose)(a) : 0;
}
UINT WINAPI waveOutWrite(void* a, void* b, UINT c) {
    typedef UINT (WINAPI *fn_t)(void*, void*, UINT);
    return orig_waveOutWrite ? ((fn_t)orig_waveOutWrite)(a,b,c) : 0;
}
UINT WINAPI waveOutPrepareHeader(void* a, void* b, UINT c) {
    typedef UINT (WINAPI *fn_t)(void*, void*, UINT);
    return orig_waveOutPrepareHeader ? ((fn_t)orig_waveOutPrepareHeader)(a,b,c) : 0;
}
UINT WINAPI waveOutUnprepareHeader(void* a, void* b, UINT c) {
    typedef UINT (WINAPI *fn_t)(void*, void*, UINT);
    return orig_waveOutUnprepareHeader ? ((fn_t)orig_waveOutUnprepareHeader)(a,b,c) : 0;
}
UINT WINAPI waveOutReset(void* a) {
    typedef UINT (WINAPI *fn_t)(void*);
    return orig_waveOutReset ? ((fn_t)orig_waveOutReset)(a) : 0;
}
UINT WINAPI waveOutGetPosition(void* a, void* b, UINT c) {
    typedef UINT (WINAPI *fn_t)(void*, void*, UINT);
    return orig_waveOutGetPosition ? ((fn_t)orig_waveOutGetPosition)(a,b,c) : 0;
}

/* ── Logging ── */
static FILE* g_log = NULL;
static LONG  g_init_state = 0;
static uint8_t* g_exe_base = NULL;

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

/* ── Vtable patches (popup + forbidden) ── */
static void patch_vtables(void) {
    static uint8_t sc[] = {0x48,0x31,0xC0,0xC3};
    static void* stub = NULL;
    if (!stub) {
        stub = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (stub) memcpy(stub, sc, 4);
    }
    if (!stub) return;

    uint64_t* v1 = (uint64_t*)(g_exe_base + 0x48C9C30);
    if (*(v1-1) == (uint64_t)g_exe_base + 0x4F0D220) {
        DWORD o; VirtualProtect(v1, 96, PAGE_EXECUTE_READWRITE, &o);
        v1[4]=(uint64_t)(uintptr_t)stub; v1[5]=(uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), v1, 96);
        VirtualProtect(v1, 96, o, &o);
        ulog("[VT] Popup suppressed");
    }

    uint64_t* v2 = (uint64_t*)(g_exe_base + 0x4843160);
    if (*(v2-1) == (uint64_t)g_exe_base + 0x4EE6098) {
        DWORD o; VirtualProtect(v2, 96, PAGE_EXECUTE_READWRITE, &o);
        v2[2]=(uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), v2, 96);
        VirtualProtect(v2, 96, o, &o);
        ulog("[VT] Forbidden bypassed");
    }
}

/* ── Quest dialog hook via VEH + hardware breakpoint ── */

/*
 * Strategy: Use hardware breakpoint on the virtual call inside
 * the populate function at 0x0EE3D50 (call [rax+18]).
 *
 * When the breakpoint fires:
 * 1. The function is about to call [rax+18] to get quest dialog data
 * 2. We let it execute
 * 3. After it returns, if RAX is NULL (no data for current character),
 *    we try to find the data from any character that has it
 *
 * Actually simpler approach: patch the populate function to skip
 * the NULL check — if the virtual call returns NULL, we call a
 * fallback that searches all quest dialog entries.
 *
 * SIMPLEST approach: patch the conditional jump at 0x0EE3D4E
 * (test rcx, rcx / jz skip) to never jump — force it to always
 * try to populate. If [rcx] is NULL, it will crash, so we need
 * to add a NULL guard.
 *
 * SAFEST approach for now: just NOP the character check in the
 * caller function. From our analysis:
 *
 * At RVA 0x12C2070:
 *   C6 01 03              mov byte ptr [rcx], 3  (set type=3)
 *   33 ED                 xor ebp, ebp
 *   48 89 69 08           mov [rcx+8], rbp = 0
 *   48 89 69 10           mov [rcx+10], rbp = 0   ← ZEROS QUEST DATA
 *
 * We can't NOP the zeroing because the object might have stale data.
 * The real fix requires hooking the populate function.
 *
 * Let's try the VEH approach: set HW BP on HasQuestDialog,
 * and when [rcx+10]==0, scan nearby memory for a valid quest
 * dialog pointer from another condition object and copy it.
 */

/* Captured quest dialog pointers from working objects */
static volatile uint64_t g_last_valid_quest_data = 0;
static volatile LONG g_hook_active = 0;
static volatile LONG g_hit_count = 0;

static LONG CALLBACK quest_veh(PEXCEPTION_POINTERS ex) {
    if (ex->ExceptionRecord->ExceptionCode != EXCEPTION_SINGLE_STEP)
        return EXCEPTION_CONTINUE_SEARCH;

    uint64_t rip = ex->ContextRecord->Rip;
    uint64_t target = (uint64_t)(g_exe_base + 0x1BAB120);

    if (rip != target)
        return EXCEPTION_CONTINUE_SEARCH;

    LONG count = InterlockedIncrement(&g_hit_count);
    uint64_t rcx = ex->ContextRecord->Rcx;

    /* Read [rcx+0x10] — quest dialog data pointer */
    uint64_t quest_data = 0;
    __try {
        quest_data = *(uint64_t*)(rcx + 0x10);
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        quest_data = 0;
    }

    if (quest_data != 0) {
        /* This object HAS quest dialog data — save it */
        InterlockedExchange64((LONG64*)&g_last_valid_quest_data, (LONG64)quest_data);
    } else {
        /* This object has NO data — inject the saved data if we have one */
        uint64_t saved = (uint64_t)InterlockedCompareExchange64(
            (LONG64*)&g_last_valid_quest_data, 0, 0);
        if (saved != 0) {
            __try {
                *(uint64_t*)(rcx + 0x10) = saved;
                if (count <= 5) {
                    ulog("[HOOK] Injected quest data 0x%llX into object 0x%llX",
                         saved, rcx);
                }
            } __except(EXCEPTION_EXECUTE_HANDLER) {
                /* ignore */
            }
        }
    }

    /* Resume: clear DR6, set resume flag */
    ex->ContextRecord->Dr6 = 0;
    ex->ContextRecord->EFlags |= 0x10000;
    return EXCEPTION_CONTINUE_EXECUTION;
}

/* Set HW breakpoint on all threads */
typedef struct { DWORD dwSize; DWORD cntUsage; DWORD th32ThreadID;
                 DWORD th32OwnerProcessID; LONG tpBasePri;
                 LONG tpDeltaPri; DWORD dwFlags; } TE32;
typedef HANDLE (WINAPI *CST_t)(DWORD, DWORD);
typedef BOOL (WINAPI *TF_t)(HANDLE, TE32*);

static void set_hwbp_all_threads(uint64_t addr) {
    HMODULE k = GetModuleHandleA("kernel32.dll");
    CST_t cst = (CST_t)GetProcAddress(k, "CreateToolhelp32Snapshot");
    TF_t tf = (TF_t)GetProcAddress(k, "Thread32First");
    TF_t tn = (TF_t)GetProcAddress(k, "Thread32Next");

    HANDLE snap = cst(0x4, 0);
    if (snap == INVALID_HANDLE_VALUE) return;

    TE32 te; te.dwSize = sizeof(te);
    DWORD pid = GetCurrentProcessId();
    DWORD my_tid = GetCurrentThreadId();

    if (tf(snap, &te)) {
        do {
            if (te.th32OwnerProcessID == pid && te.th32ThreadID != my_tid) {
                HANDLE ht = OpenThread(0x1A, FALSE, te.th32ThreadID);
                if (ht) {
                    SuspendThread(ht);
                    CONTEXT ctx; ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
                    if (GetThreadContext(ht, &ctx)) {
                        ctx.Dr0 = addr;
                        ctx.Dr7 = 1; /* Local enable DR0, execute, 1-byte */
                        ctx.Dr6 = 0;
                        SetThreadContext(ht, &ctx);
                    }
                    ResumeThread(ht);
                    CloseHandle(ht);
                }
            }
        } while (tn(snap, &te));
    }
    CloseHandle(snap);
}

/* ── Delayed init (wait for game to fully load) ── */
static DWORD WINAPI delayed_init(LPVOID param) {
    (void)param;
    /* Wait for the game to load */
    Sleep(10000);

    ulog("[INIT] Applying patches...");
    patch_vtables();

    /* Set up VEH and hardware breakpoint for quest dialog */
    AddVectoredExceptionHandler(1, quest_veh);
    uint64_t bp_addr = (uint64_t)(g_exe_base + 0x1BAB120);
    set_hwbp_all_threads(bp_addr);
    InterlockedExchange(&g_hook_active, 1);
    ulog("[INIT] Quest dialog hook active (VEH + HW BP at 0x1BAB120)");
    ulog("[INIT] When Kliff approaches a quest NPC, the dialog data is saved.");
    ulog("[INIT] When Damiane approaches, the saved data is injected.");

    /* Monitor */
    while (1) {
        Sleep(5000);
        if (g_hit_count > 0) {
            ulog("[MON] Hits: %ld, saved data: 0x%llX",
                 g_hit_count, g_last_valid_quest_data);
        }
    }
    return 0;
}

/* ── DLL entry ── */
BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDll);
        if (!is_game()) {
            /* Still need to load real winmm for non-game processes */
            char sys[MAX_PATH];
            GetSystemDirectoryA(sys, MAX_PATH);
            strcat(sys, "\\winmm.dll");
            g_orig_winmm = LoadLibraryA(sys);
            if (g_orig_winmm) {
                orig_timeGetTime = (timeGetTime_t)GetProcAddress(g_orig_winmm, "timeGetTime");
                orig_timeBeginPeriod = (timeBeginPeriod_t)GetProcAddress(g_orig_winmm, "timeBeginPeriod");
                orig_timeEndPeriod = (timeEndPeriod_t)GetProcAddress(g_orig_winmm, "timeEndPeriod");
                orig_timeGetDevCaps = (timeGetDevCaps_t)GetProcAddress(g_orig_winmm, "timeGetDevCaps");
                orig_waveOutGetNumDevs = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutGetNumDevs");
                orig_waveOutOpen = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutOpen");
                orig_waveOutClose = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutClose");
                orig_waveOutWrite = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutWrite");
                orig_waveOutPrepareHeader = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutPrepareHeader");
                orig_waveOutUnprepareHeader = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutUnprepareHeader");
                orig_waveOutReset = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutReset");
                orig_waveOutGetPosition = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutGetPosition");
            }
            return TRUE;
        }

        LONG prior = InterlockedCompareExchange(&g_init_state, 1, 0);
        if (prior != 0) return TRUE;

        /* Load real winmm */
        char sys[MAX_PATH];
        GetSystemDirectoryA(sys, MAX_PATH);
        strcat(sys, "\\winmm.dll");
        g_orig_winmm = LoadLibraryA(sys);
        if (g_orig_winmm) {
            orig_timeGetTime = (timeGetTime_t)GetProcAddress(g_orig_winmm, "timeGetTime");
            orig_timeBeginPeriod = (timeBeginPeriod_t)GetProcAddress(g_orig_winmm, "timeBeginPeriod");
            orig_timeEndPeriod = (timeEndPeriod_t)GetProcAddress(g_orig_winmm, "timeEndPeriod");
            orig_timeGetDevCaps = (timeGetDevCaps_t)GetProcAddress(g_orig_winmm, "timeGetDevCaps");
            orig_waveOutGetNumDevs = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutGetNumDevs");
            orig_waveOutOpen = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutOpen");
            orig_waveOutClose = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutClose");
            orig_waveOutWrite = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutWrite");
            orig_waveOutPrepareHeader = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutPrepareHeader");
            orig_waveOutUnprepareHeader = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutUnprepareHeader");
            orig_waveOutReset = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutReset");
            orig_waveOutGetPosition = (waveOut_generic_t)GetProcAddress(g_orig_winmm, "waveOutGetPosition");
        }

        g_exe_base = (uint8_t*)GetModuleHandleA(NULL);

        char dir[MAX_PATH];
        GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\'); if (sep) *sep = 0;
        char lp[MAX_PATH];
        sprintf(lp, "%s\\crimson_winmm_hook.log", dir);
        g_log = fopen(lp, "w");

        ulog("=== CrimsonForge winmm Quest Dialog Hook ===");
        ulog("PID: %lu", GetCurrentProcessId());

        /* Delayed init — wait for game to load */
        CreateThread(NULL, 0, delayed_init, NULL, 0, NULL);

        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_orig_winmm) FreeLibrary(g_orig_winmm);
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
