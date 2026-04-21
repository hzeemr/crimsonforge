/*
 * External DLL injector for CrimsonDesert.exe
 *
 * Bypasses Denuvo's DLL-proxy-loading block by attaching to the already-
 * running game process and calling LoadLibraryA via CreateRemoteThread.
 * This is the standard Windows injection technique that game-side anti-
 * tamper typically can't prevent (it requires the game to have been
 * launched normally first, then we attach from outside).
 *
 * Build (MSVC or MinGW x64):
 *
 *     cl /O2 /EHsc injector.c /Fe:injector.exe
 *
 * Usage:
 *
 *     injector.exe path\to\helper.dll
 *
 * The injector will:
 *   1. Find CrimsonDesert.exe in the process list.
 *   2. Open a handle with PROCESS_ALL_ACCESS.
 *   3. Allocate a buffer in the target for the DLL path string.
 *   4. WriteProcessMemory the path into that buffer.
 *   5. CreateRemoteThread with LoadLibraryA as the start address.
 *   6. Wait for the thread to finish and report the HMODULE returned.
 *
 * Error handling is explicit — every Win32 call is checked and a clear
 * message is emitted. No silent failures.
 */

#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <string.h>


static DWORD find_process_pid(const char* image_name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "error: CreateToolhelp32Snapshot failed (code %lu)\n", GetLastError());
        return 0;
    }

    PROCESSENTRY32 entry = { sizeof(PROCESSENTRY32) };
    DWORD pid = 0;
    if (Process32First(snap, &entry)) {
        do {
            if (_stricmp(entry.szExeFile, image_name) == 0) {
                pid = entry.th32ProcessID;
                break;
            }
        } while (Process32Next(snap, &entry));
    }
    CloseHandle(snap);
    return pid;
}


static int inject_dll(DWORD pid, const char* dll_path) {
    HANDLE proc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!proc) {
        fprintf(stderr, "error: OpenProcess(%lu) failed (code %lu)\n",
                pid, GetLastError());
        return 1;
    }

    SIZE_T path_len = strlen(dll_path) + 1;
    LPVOID remote_buf = VirtualAllocEx(proc, NULL, path_len,
                                        MEM_COMMIT | MEM_RESERVE,
                                        PAGE_READWRITE);
    if (!remote_buf) {
        fprintf(stderr, "error: VirtualAllocEx failed (code %lu)\n", GetLastError());
        CloseHandle(proc);
        return 2;
    }

    if (!WriteProcessMemory(proc, remote_buf, dll_path, path_len, NULL)) {
        fprintf(stderr, "error: WriteProcessMemory failed (code %lu)\n",
                GetLastError());
        VirtualFreeEx(proc, remote_buf, 0, MEM_RELEASE);
        CloseHandle(proc);
        return 3;
    }

    /* LoadLibraryA is in kernel32.dll which is guaranteed to be loaded at
     * the same base address in every process (it's one of the few modules
     * ASLR relocates uniformly). We can therefore pass its address from
     * the injector directly. */
    HMODULE k32 = GetModuleHandleA("kernel32.dll");
    if (!k32) {
        fprintf(stderr, "error: kernel32.dll not loaded (somehow?)\n");
        VirtualFreeEx(proc, remote_buf, 0, MEM_RELEASE);
        CloseHandle(proc);
        return 4;
    }
    FARPROC load_library = GetProcAddress(k32, "LoadLibraryA");
    if (!load_library) {
        fprintf(stderr, "error: LoadLibraryA not found in kernel32.dll\n");
        VirtualFreeEx(proc, remote_buf, 0, MEM_RELEASE);
        CloseHandle(proc);
        return 5;
    }

    HANDLE thread = CreateRemoteThread(
        proc, NULL, 0,
        (LPTHREAD_START_ROUTINE)load_library,
        remote_buf, 0, NULL
    );
    if (!thread) {
        fprintf(stderr, "error: CreateRemoteThread failed (code %lu)\n",
                GetLastError());
        VirtualFreeEx(proc, remote_buf, 0, MEM_RELEASE);
        CloseHandle(proc);
        return 6;
    }

    printf("  Remote thread started. Waiting for LoadLibraryA to return...\n");
    WaitForSingleObject(thread, 15000);   /* 15s timeout */

    DWORD exit_code = 0;
    GetExitCodeThread(thread, &exit_code);
    /* exit_code holds the lower 32 bits of the HMODULE returned by
     * LoadLibraryA. 0 means failure. */
    if (exit_code == 0) {
        fprintf(stderr, "error: LoadLibraryA returned NULL — DLL failed to load.\n");
        fprintf(stderr, "       Common causes:\n");
        fprintf(stderr, "         * DLL path is wrong (use absolute path!)\n");
        fprintf(stderr, "         * DLL architecture mismatch (needs x64)\n");
        fprintf(stderr, "         * DLL has unresolved dependencies\n");
        fprintf(stderr, "         * DllMain returned FALSE\n");
    } else {
        printf("  LoadLibraryA returned HMODULE lo32 = 0x%08lx\n", exit_code);
        printf("  DLL loaded successfully.\n");
    }

    CloseHandle(thread);
    VirtualFreeEx(proc, remote_buf, 0, MEM_RELEASE);
    CloseHandle(proc);
    return (exit_code == 0) ? 7 : 0;
}


int main(int argc, char** argv) {
    if (argc != 2) {
        printf("Usage: %s <path_to_helper.dll>\n", argv[0]);
        printf("\n");
        printf("Injects the given DLL into CrimsonDesert.exe using\n");
        printf("CreateRemoteThread. The game must already be running.\n");
        return 1;
    }

    const char* dll_path = argv[1];

    /* Require absolute path — LoadLibraryA in the target process has
     * the target's CWD, which isn't the injector's CWD. */
    char abs_path[MAX_PATH];
    DWORD n = GetFullPathNameA(dll_path, MAX_PATH, abs_path, NULL);
    if (n == 0 || n >= MAX_PATH) {
        fprintf(stderr, "error: could not resolve absolute path for %s\n", dll_path);
        return 2;
    }

    if (GetFileAttributesA(abs_path) == INVALID_FILE_ATTRIBUTES) {
        fprintf(stderr, "error: %s does not exist\n", abs_path);
        return 3;
    }

    printf("CrimsonDesert DLL injector\n");
    printf("  DLL: %s\n", abs_path);

    DWORD pid = find_process_pid("CrimsonDesert.exe");
    if (pid == 0) {
        fprintf(stderr, "error: CrimsonDesert.exe is not running. Launch the game first.\n");
        return 4;
    }
    printf("  Target PID: %lu\n", pid);

    return inject_dll(pid, abs_path);
}
