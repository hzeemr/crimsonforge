/**
 * CrimsonForge Full Quest Capture
 *
 * NO HOOKS. Uses hardware breakpoints on HasQuestDialog (0x1BAB120)
 * to capture the FULL condition object state for both Kliff and Damiane.
 *
 * When the breakpoint fires, dumps:
 * - All registers
 * - The condition object ([rcx]) — 256 bytes
 * - The quest dialog data at [rcx+0x10] — 256 bytes (if not NULL)
 * - The object at [rcx+0x18], [rcx+0x20] etc.
 * - Call stack (return addresses)
 *
 * F6 = arm breakpoint (captures up to 20 hits)
 * F7 = disarm + dump ALL captured data
 * F8 = mark (label "KLIFF" or "DAMIANE" section)
 * F9 = clear captures
 *
 * Includes vtable patches for popup + forbidden.
 * Log: bin64/crimson_quest_trace.log
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

static void patch_vtables(void) {
    static uint8_t sc[] = {0x48,0x31,0xC0,0xC3};
    static void* stub = NULL;
    if (!stub) { stub = VirtualAlloc(NULL, 16, MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE); if(stub) memcpy(stub, sc, 4); }
    if (!stub) return;
    uint64_t* v1 = (uint64_t*)(g_exe_base + 0x48C9C30);
    if (*(v1-1) == (uint64_t)g_exe_base + 0x4F0D220) {
        DWORD o; VirtualProtect(v1, 96, PAGE_EXECUTE_READWRITE, &o);
        v1[4]=(uint64_t)(uintptr_t)stub; v1[5]=(uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), v1, 96); VirtualProtect(v1, 96, o, &o);
        tlog("[VT] Popup suppressed");
    }
    uint64_t* v2 = (uint64_t*)(g_exe_base + 0x4843160);
    if (*(v2-1) == (uint64_t)g_exe_base + 0x4EE6098) {
        DWORD o; VirtualProtect(v2, 96, PAGE_EXECUTE_READWRITE, &o);
        v2[2]=(uint64_t)(uintptr_t)stub;
        FlushInstructionCache(GetCurrentProcess(), v2, 96); VirtualProtect(v2, 96, o, &o);
        tlog("[VT] Forbidden bypassed");
    }
}

/* Capture storage */
#define MAX_CAP 20
#define OBJ_DUMP_SIZE 256

typedef struct {
    uint64_t rax, rcx, rdx, r8, r9, rbp, rsp, rip;
    uint64_t ret_addr;
    uint64_t rcx_plus_10;  /* [rcx+0x10] value */
    uint64_t rcx_plus_18;
    uint64_t rcx_plus_20;
    uint64_t rcx_plus_08;
    uint8_t  obj_dump[OBJ_DUMP_SIZE];     /* [rcx] object */
    uint8_t  data_dump[OBJ_DUMP_SIZE];    /* [[rcx+0x10]] if not NULL */
    int      has_data;                     /* 1 if [rcx+0x10] was not NULL */
    DWORD    tid;
} capture_t;

static capture_t g_caps[MAX_CAP];
static volatile LONG g_cap_count = 0;
static volatile LONG g_armed = 0;

/* Safe memory read */
static int safe_read(void* dst, const void* src, size_t len) {
    __try {
        memcpy(dst, src, len);
        return 1;
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        memset(dst, 0xEE, len);
        return 0;
    }
}

/* Hardware breakpoint on HasQuestDialog */
static void set_hwbp(HANDLE hThread, uint64_t addr) {
    CONTEXT ctx; ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(hThread, &ctx)) return;
    ctx.Dr0 = addr;
    ctx.Dr7 = 1; /* Enable DR0, execute, 1-byte, local */
    ctx.Dr6 = 0;
    SetThreadContext(hThread, &ctx);
}

static void clear_hwbp(HANDLE hThread) {
    CONTEXT ctx; ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(hThread, &ctx)) return;
    ctx.Dr0 = ctx.Dr1 = ctx.Dr2 = ctx.Dr3 = 0;
    ctx.Dr6 = ctx.Dr7 = 0;
    SetThreadContext(hThread, &ctx);
}

typedef struct { DWORD dwSize; DWORD cntUsage; DWORD th32ThreadID;
                 DWORD th32OwnerProcessID; LONG tpBasePri;
                 LONG tpDeltaPri; DWORD dwFlags; } TE32;
typedef BOOL (WINAPI *TF)(HANDLE, TE32*);

static void for_each_thread(void (*fn)(HANDLE)) {
    HANDLE snap = CreateToolhelp32Snapshot(0x4, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    HMODULE k = GetModuleHandleA("kernel32.dll");
    TF tf = (TF)GetProcAddress(k, "Thread32First");
    TF tn = (TF)GetProcAddress(k, "Thread32Next");
    TE32 te; te.dwSize = sizeof(te);
    DWORD pid = GetCurrentProcessId();
    if (tf(snap, &te)) {
        do {
            if (te.th32OwnerProcessID == pid) {
                HANDLE ht = OpenThread(0x1A, FALSE, te.th32ThreadID);
                if (ht) { SuspendThread(ht); fn(ht); ResumeThread(ht); CloseHandle(ht); }
            }
        } while (tn(snap, &te));
    }
    CloseHandle(snap);
}

static uint64_t g_bp_addr = 0;
static void arm_thread(HANDLE h) { set_hwbp(h, g_bp_addr); }
static void disarm_thread(HANDLE h) { clear_hwbp(h); }

/* VEH handler */
static LONG CALLBACK veh(PEXCEPTION_POINTERS ex) {
    if (ex->ExceptionRecord->ExceptionCode != EXCEPTION_SINGLE_STEP)
        return EXCEPTION_CONTINUE_SEARCH;

    if (!g_armed || (uint64_t)ex->ContextRecord->Rip != g_bp_addr)
        return EXCEPTION_CONTINUE_SEARCH;

    LONG idx = InterlockedIncrement(&g_cap_count) - 1;
    if (idx < MAX_CAP) {
        capture_t* c = &g_caps[idx];
        c->rax = ex->ContextRecord->Rax;
        c->rcx = ex->ContextRecord->Rcx;
        c->rdx = ex->ContextRecord->Rdx;
        c->r8 = ex->ContextRecord->R8;
        c->r9 = ex->ContextRecord->R9;
        c->rbp = ex->ContextRecord->Rbp;
        c->rsp = ex->ContextRecord->Rsp;
        c->rip = ex->ContextRecord->Rip;
        c->tid = GetCurrentThreadId();

        /* Read return address from stack */
        safe_read(&c->ret_addr, (void*)c->rsp, 8);

        /* Dump the condition object at [rcx] */
        safe_read(c->obj_dump, (void*)c->rcx, OBJ_DUMP_SIZE);

        /* Read key pointers from the object */
        safe_read(&c->rcx_plus_08, (void*)(c->rcx + 0x08), 8);
        safe_read(&c->rcx_plus_10, (void*)(c->rcx + 0x10), 8);
        safe_read(&c->rcx_plus_18, (void*)(c->rcx + 0x18), 8);
        safe_read(&c->rcx_plus_20, (void*)(c->rcx + 0x20), 8);

        /* If [rcx+0x10] is not NULL, dump what it points to */
        if (c->rcx_plus_10 != 0 && c->rcx_plus_10 != 0xEEEEEEEEEEEEEEEE) {
            c->has_data = safe_read(c->data_dump, (void*)c->rcx_plus_10, OBJ_DUMP_SIZE);
        } else {
            c->has_data = 0;
            memset(c->data_dump, 0, OBJ_DUMP_SIZE);
        }
    }

    /* Resume: clear DR6, set resume flag */
    ex->ContextRecord->Dr6 = 0;
    ex->ContextRecord->EFlags |= 0x10000;
    return EXCEPTION_CONTINUE_EXECUTION;
}

static void dump_captures(void) {
    LONG count = g_cap_count;
    if (count > MAX_CAP) count = MAX_CAP;
    tlog("=== %ld CAPTURES ===", count);

    for (LONG i = 0; i < count; i++) {
        capture_t* c = &g_caps[i];
        uint64_t ret_rva = c->ret_addr - (uint64_t)g_exe_base;

        tlog("\n--- Capture[%ld] tid=%lu ---", i, c->tid);
        tlog("  RCX=0x%llX  RDX=0x%llX  R8=0x%llX  R9=0x%llX",
             c->rcx, c->rdx, c->r8, c->r9);
        tlog("  RetAddr=0x%llX (RVA 0x%llX)", c->ret_addr, ret_rva);
        tlog("  [RCX+0x08]=0x%llX", c->rcx_plus_08);
        tlog("  [RCX+0x10]=0x%llX  %s", c->rcx_plus_10,
             c->rcx_plus_10 ? "<-- HAS DATA" : "<-- NULL (no quest dialog)");
        tlog("  [RCX+0x18]=0x%llX", c->rcx_plus_18);
        tlog("  [RCX+0x20]=0x%llX", c->rcx_plus_20);

        /* Object hex dump */
        tlog("  Object [RCX] dump:");
        for (int r = 0; r < 8; r++) {
            char hex[120] = {0}; int hp = 0;
            for (int b = 0; b < 32; b++)
                hp += sprintf(hex + hp, "%02X ", c->obj_dump[r*32 + b]);
            tlog("    +0x%02X: %s", r*32, hex);
        }

        if (c->has_data) {
            tlog("  Data [[RCX+0x10]] dump:");
            for (int r = 0; r < 8; r++) {
                char hex[120] = {0}; int hp = 0;
                for (int b = 0; b < 32; b++)
                    hp += sprintf(hex + hp, "%02X ", c->data_dump[r*32 + b]);
                tlog("    +0x%02X: %s", r*32, hex);
            }
        }
    }
    tlog("\n=== END CAPTURES ===");
}

static DWORD WINAPI monitor(LPVOID p) {
    (void)p;
    Sleep(8000);
    g_bp_addr = (uint64_t)(g_exe_base + 0x1BAB120);
    tlog("[MON] BP target: HasQuestDialog at 0x%llX", g_bp_addr);
    tlog("[MON] F6=arm, F7=dump, F8=mark, F9=clear");

    while (1) {
        Sleep(100);

        if (GetAsyncKeyState(VK_F6) & 1) {
            tlog("\n[F6] ARMING...");
            InterlockedExchange(&g_armed, 1);
            for_each_thread(arm_thread);
            tlog("[F6] Armed. Walk near quest NPC.");
        }

        if (GetAsyncKeyState(VK_F7) & 1) {
            tlog("\n[F7] DISARMING...");
            InterlockedExchange(&g_armed, 0);
            for_each_thread(disarm_thread);
            dump_captures();
        }

        if (GetAsyncKeyState(VK_F8) & 1) {
            tlog("\n========== MARK (captures: %ld) ==========\n", g_cap_count);
        }

        if (GetAsyncKeyState(VK_F9) & 1) {
            InterlockedExchange(&g_cap_count, 0);
            memset(g_caps, 0, sizeof(g_caps));
            tlog("[F9] Captures cleared");
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
        if (g_exe_base) { DWORD pe = *(DWORD*)(g_exe_base + 0x3C);
            g_exe_size = *(DWORD*)(g_exe_base + pe + 4 + 20 + 56); }

        char dir[MAX_PATH]; GetModuleFileNameA(NULL, dir, MAX_PATH);
        char* sep = strrchr(dir, '\\'); if (sep) *sep = 0;
        char lp[MAX_PATH]; sprintf(lp, "%s\\crimson_quest_trace.log", dir);
        g_log = fopen(lp, "w");

        tlog("=== CrimsonForge Full Quest Capture ===");
        tlog("PID: %lu", GetCurrentProcessId());
        patch_vtables();
        AddVectoredExceptionHandler(1, veh);
        CreateThread(NULL, 0, monitor, NULL, 0, NULL);
        InterlockedExchange(&g_init_state, 2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_armed) for_each_thread(disarm_thread);
        if (g_log) { fclose(g_log); g_log = NULL; }
    }
    return TRUE;
}
