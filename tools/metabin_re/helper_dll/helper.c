/*
 * AnimationMetaData runtime inspector DLL (v2 — deep deserializer trace).
 *
 * Changes from v1:
 *   * Hooks the FIRST 12 vfuncs of each vtable (up from 4). The
 *     deserializer tends to live at vfunc[4..10], so the earlier
 *     version missed it.
 *   * Logs RDX in addition to RCX. For deserializer functions RDX
 *     typically holds a BinaryReader or source-buffer pointer; we
 *     dereference one level to capture the raw `.paa_metabin` bytes
 *     being parsed.
 *   * Skips logging of clearly-repeating calls (destructor is called
 *     on every frame for some objects and drowns the log).
 *
 * Build (MinGW x64):
 *   gcc -O2 -shared helper.c -o helper.dll -luser32
 */

#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdarg.h>


/* Vtable VAs from tools/metabin_re/output/rtti_report.json. */
#define NUM_VTABLES   3
#define HOOKS_PER_VTABLE  12

static const uintptr_t g_vtable_vas[NUM_VTABLES] = {
    0x144C87298,    /* vtable 0 (main) */
    0x144C87810,    /* vtable 1 (secondary) */
    0x144C87288,    /* vtable 2 (interface) */
};


static HANDLE g_log_file = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_log_lock;
static void* g_originals[NUM_VTABLES][HOOKS_PER_VTABLE] = {0};

/* Deduplication: count consecutive identical (vtable, vfunc, this) hits
 * and collapse them into a single log line with a count. */
static int g_last_vt = -1;
static int g_last_vf = -1;
static void* g_last_this = NULL;
static int g_dup_count = 0;


static void log_raw(const char* buf, size_t len) {
    if (g_log_file == INVALID_HANDLE_VALUE) return;
    DWORD written;
    WriteFile(g_log_file, buf, (DWORD)len, &written, NULL);
    WriteFile(g_log_file, "\r\n", 2, &written, NULL);
    FlushFileBuffers(g_log_file);
}


static void log_line(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    if ((size_t)n >= sizeof(buf)) n = sizeof(buf) - 1;

    EnterCriticalSection(&g_log_lock);
    log_raw(buf, n);
    LeaveCriticalSection(&g_log_lock);
}


static int is_memory_readable(const void* addr, size_t len) {
    if (!addr) return 0;
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(addr, &mbi, sizeof(mbi)) == 0) return 0;
    if (mbi.State != MEM_COMMIT) return 0;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return 0;
    DWORD readable_mask = PAGE_READONLY | PAGE_READWRITE |
                          PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                          PAGE_WRITECOPY | PAGE_EXECUTE_WRITECOPY;
    if (!(mbi.Protect & readable_mask)) return 0;
    uintptr_t region_end = (uintptr_t)mbi.BaseAddress + mbi.RegionSize;
    if ((uintptr_t)addr + len > region_end) return 0;
    return 1;
}


static void dump_memory(const char* label, const void* addr, size_t len) {
    if (!is_memory_readable(addr, len)) {
        log_line("  %s: <unreadable @ 0x%p>", label, addr);
        return;
    }
    char line[256];
    for (size_t off = 0; off < len; off += 16) {
        size_t chunk = (len - off) < 16 ? (len - off) : 16;
        int p = snprintf(line, sizeof(line), "  %s+0x%02zx:", label, off);
        for (size_t k = 0; k < chunk; k++) {
            int remaining = (int)sizeof(line) - p;
            if (remaining <= 4) break;
            p += snprintf(line + p, remaining, " %02x",
                          ((const unsigned char*)addr)[off + k]);
        }
        log_line("%s", line);
    }
}


/* Log a hook hit with full context. Called by every trampoline.
 * Captures rcx (this), rdx (arg2 — often source-buffer ptr), and
 * dumps 0x80 bytes at each. Deduplicates back-to-back identical
 * (vt, vf, this) calls. */
static void log_hook(int vt, int vf, void* this_ptr, void* rdx_val) {
    EnterCriticalSection(&g_log_lock);

    if (vt == g_last_vt && vf == g_last_vf && this_ptr == g_last_this) {
        g_dup_count++;
        LeaveCriticalSection(&g_log_lock);
        return;
    }

    if (g_dup_count > 0) {
        char buf[128];
        int n = snprintf(buf, sizeof(buf),
                          "  (previous event repeated %d more times)",
                          g_dup_count);
        log_raw(buf, n);
        g_dup_count = 0;
    }
    g_last_vt = vt;
    g_last_vf = vf;
    g_last_this = this_ptr;

    char buf[256];
    int n = snprintf(buf, sizeof(buf),
                      "---- vt%d.vf%d hit   this=0x%p   rdx=0x%p ----",
                      vt, vf, this_ptr, rdx_val);
    log_raw(buf, n);

    LeaveCriticalSection(&g_log_lock);

    dump_memory("this", this_ptr, 0x80);

    /* rdx might point to a BinaryReader with internal pointers;
     * dump both the reader struct itself AND the buffer its first
     * pointer field points to (if readable). */
    if (rdx_val && is_memory_readable(rdx_val, 0x40)) {
        dump_memory("rdx", rdx_val, 0x40);
        /* Common BinaryReader layout: first field is a char* to the
         * source bytes. Dereference and dump. */
        void* buf_ptr = *(void**)rdx_val;
        if (is_memory_readable(buf_ptr, 0x80)) {
            dump_memory("*rdx (buf?)", buf_ptr, 0x80);
        }
    }
}


/* Trampolines — one per (vtable, vfunc). Each trampoline captures
 * rdx via inline asm (GCC) / pseudo-intrinsic (MSVC), then tail-calls
 * the original. */

#ifdef __GNUC__
# define GET_RDX(dst) do { __asm__ __volatile__("movq %%rdx, %0" : "=r"(dst)); } while(0)
#else
# include <intrin.h>
/* MSVC: __readgsqword etc. don't give us rdx, but we can use a
 * trick: rdx is the 2nd fastcall arg, so the trampoline signature
 * (void*, void*) captures it as its 2nd parameter. */
# define GET_RDX(dst) /* handled via function signature on MSVC */
#endif


#ifdef __GNUC__
/* GCC: use attribute ms_abi to match Windows x64 calling convention. */
#define HOOK_IMPL(VT, VF)                                                    \
__attribute__((ms_abi))                                                      \
static void hook_vt##VT##_vf##VF(void* this_ptr, void* rdx_val) {            \
    log_hook((VT), (VF), this_ptr, rdx_val);                                 \
    typedef void (__attribute__((ms_abi)) *orig_t)(void*, void*);            \
    orig_t orig = (orig_t)g_originals[VT][VF];                               \
    if (orig) orig(this_ptr, rdx_val);                                        \
}
#else
#define HOOK_IMPL(VT, VF)                                                    \
static void __fastcall hook_vt##VT##_vf##VF(void* this_ptr, void* rdx_val) { \
    log_hook((VT), (VF), this_ptr, rdx_val);                                 \
    typedef void (__fastcall *orig_t)(void*, void*);                         \
    orig_t orig = (orig_t)g_originals[VT][VF];                               \
    if (orig) orig(this_ptr, rdx_val);                                        \
}
#endif


/* Expand 12 vfuncs × 3 vtables = 36 trampolines. */
#define HOOK_VTABLE(VT) \
    HOOK_IMPL(VT, 0)  HOOK_IMPL(VT, 1)  HOOK_IMPL(VT, 2)  HOOK_IMPL(VT, 3)   \
    HOOK_IMPL(VT, 4)  HOOK_IMPL(VT, 5)  HOOK_IMPL(VT, 6)  HOOK_IMPL(VT, 7)   \
    HOOK_IMPL(VT, 8)  HOOK_IMPL(VT, 9)  HOOK_IMPL(VT, 10) HOOK_IMPL(VT, 11)

HOOK_VTABLE(0)
HOOK_VTABLE(1)
HOOK_VTABLE(2)


static void* const g_hooks[NUM_VTABLES][HOOKS_PER_VTABLE] = {
    { hook_vt0_vf0, hook_vt0_vf1, hook_vt0_vf2, hook_vt0_vf3,
      hook_vt0_vf4, hook_vt0_vf5, hook_vt0_vf6, hook_vt0_vf7,
      hook_vt0_vf8, hook_vt0_vf9, hook_vt0_vf10, hook_vt0_vf11 },
    { hook_vt1_vf0, hook_vt1_vf1, hook_vt1_vf2, hook_vt1_vf3,
      hook_vt1_vf4, hook_vt1_vf5, hook_vt1_vf6, hook_vt1_vf7,
      hook_vt1_vf8, hook_vt1_vf9, hook_vt1_vf10, hook_vt1_vf11 },
    { hook_vt2_vf0, hook_vt2_vf1, hook_vt2_vf2, hook_vt2_vf3,
      hook_vt2_vf4, hook_vt2_vf5, hook_vt2_vf6, hook_vt2_vf7,
      hook_vt2_vf8, hook_vt2_vf9, hook_vt2_vf10, hook_vt2_vf11 },
};


static int install_hooks(void) {
    int total_installed = 0;
    for (int vt_idx = 0; vt_idx < NUM_VTABLES; vt_idx++) {
        void** vtable = (void**)g_vtable_vas[vt_idx];
        DWORD old_prot;
        SIZE_T region_size = HOOKS_PER_VTABLE * sizeof(void*);
        if (!VirtualProtect(vtable, region_size, PAGE_READWRITE, &old_prot)) {
            log_line("FAIL: VirtualProtect on vtable %d (0x%p) err=%lu",
                     vt_idx, vtable, GetLastError());
            continue;
        }
        for (int vf_idx = 0; vf_idx < HOOKS_PER_VTABLE; vf_idx++) {
            g_originals[vt_idx][vf_idx] = vtable[vf_idx];
            vtable[vf_idx] = g_hooks[vt_idx][vf_idx];
            total_installed++;
        }
        DWORD dummy;
        VirtualProtect(vtable, region_size, old_prot, &dummy);
        log_line("hooked vtable %d at 0x%p (%d vfuncs)", vt_idx, vtable,
                 HOOKS_PER_VTABLE);
    }
    log_line("hook installation complete: %d vfuncs hooked total",
             total_installed);
    return total_installed;
}


static DWORD WINAPI worker(LPVOID arg) {
    (void)arg;
    Sleep(5000);

    InitializeCriticalSection(&g_log_lock);

    char log_path[MAX_PATH];
    if (ExpandEnvironmentStringsA("%USERPROFILE%\\Desktop\\metabin_trace.log",
                                   log_path, sizeof(log_path)) == 0) {
        strncpy(log_path, "C:\\metabin_trace.log", sizeof(log_path) - 1);
        log_path[sizeof(log_path) - 1] = '\0';
    }
    g_log_file = CreateFileA(log_path, GENERIC_WRITE, FILE_SHARE_READ,
                              NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_log_file == INVALID_HANDLE_VALUE) {
        MessageBoxA(NULL, "helper.dll: could not open log file", "error", MB_OK);
        return 1;
    }

    log_line("=== AnimationMetaData helper DLL v2 (deep trace) ===");
    log_line("log path: %s", log_path);
    log_line("image base: 0x%p", GetModuleHandleA(NULL));
    log_line("hooking 12 vfuncs per vtable (36 total); logs rcx + rdx");
    log_line("duplicates are collapsed; watch for '(repeated N times)'");

    int n = install_hooks();
    log_line("installation finished: %d hooks active", n);
    log_line("--- trigger animations in-game to populate the trace ---");
    return 0;
}


BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved) {
    (void)h; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        HANDLE t = CreateThread(NULL, 0, worker, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
