"""CrimsonForge DLL Injector — Run as Administrator"""
import ctypes
import sys
import os
import subprocess

# Proper 64-bit types
HANDLE = ctypes.c_uint64
LPVOID = ctypes.c_uint64
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
SIZE_T = ctypes.c_uint64

k32 = ctypes.WinDLL('kernel32')

# Set proper argtypes/restypes for 64-bit
k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
k32.OpenProcess.restype = HANDLE

k32.VirtualAllocEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, DWORD]
k32.VirtualAllocEx.restype = LPVOID

k32.WriteProcessMemory.argtypes = [HANDLE, LPVOID, ctypes.c_char_p, SIZE_T, ctypes.POINTER(SIZE_T)]
k32.WriteProcessMemory.restype = BOOL

k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
k32.GetModuleHandleW.restype = HANDLE

k32.GetProcAddress.argtypes = [HANDLE, ctypes.c_char_p]
k32.GetProcAddress.restype = LPVOID

k32.CreateRemoteThread.argtypes = [HANDLE, LPVOID, SIZE_T, LPVOID, LPVOID, DWORD, ctypes.POINTER(DWORD)]
k32.CreateRemoteThread.restype = HANDLE

k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
k32.WaitForSingleObject.restype = DWORD

k32.CloseHandle.argtypes = [HANDLE]
k32.CloseHandle.restype = BOOL

def find_pid():
    try:
        out = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq CrimsonDesert.exe" /FO CSV /NH',
            shell=True).decode()
        for line in out.strip().split('\n'):
            if 'CrimsonDesert' in line:
                parts = line.split(',')
                return int(parts[1].strip('"'))
    except:
        pass
    return None

def inject(pid, dll_path):
    print(f"Opening process {pid}...")
    hProc = k32.OpenProcess(0x1F0FFF, 0, pid)
    if not hProc:
        print("ERROR: OpenProcess failed. Run as Administrator!")
        return False

    dll_bytes = dll_path.encode('ascii') + b'\x00'

    print("Allocating memory...")
    alloc = k32.VirtualAllocEx(hProc, 0, len(dll_bytes), 0x3000, 0x04)
    if not alloc:
        print("ERROR: VirtualAllocEx failed")
        k32.CloseHandle(hProc)
        return False
    print(f"  Allocated at: 0x{alloc:X}")

    print("Writing DLL path...")
    written = SIZE_T(0)
    k32.WriteProcessMemory(hProc, alloc, dll_bytes, len(dll_bytes), ctypes.byref(written))
    print(f"  Written: {written.value} bytes")

    print("Getting LoadLibraryA...")
    hKernel = k32.GetModuleHandleW("kernel32.dll")
    loadLib = k32.GetProcAddress(hKernel, b"LoadLibraryA")
    print(f"  LoadLibraryA at: 0x{loadLib:X}")

    print("Creating remote thread...")
    tid = DWORD(0)
    hThread = k32.CreateRemoteThread(hProc, 0, 0, loadLib, alloc, 0, ctypes.byref(tid))

    if hThread:
        print(f"  Thread TID: {tid.value}")
        k32.WaitForSingleObject(hThread, 10000)
        k32.CloseHandle(hThread)
        k32.CloseHandle(hProc)
        return True
    else:
        print("ERROR: CreateRemoteThread failed")
        k32.CloseHandle(hProc)
        return False

if __name__ == '__main__':
    dll = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonForgeFullSwitch.dll"

    if not os.path.exists(dll):
        print(f"ERROR: DLL not found: {dll}")
        sys.exit(1)

    pid = find_pid()
    if not pid:
        print("ERROR: CrimsonDesert.exe not running!")
        sys.exit(1)

    print(f"PID: {pid}")
    print(f"DLL: {dll}")
    print()

    if inject(pid, dll):
        print()
        print("SUCCESS! DLL injected.")
        print("Check: bin64\\crimson_fullswitch.log or Desktop\\crimson_fullswitch.log")
    else:
        print()
        print("FAILED!")
