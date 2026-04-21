# CrimsonForge DLL Injector — Run as Administrator
# Usage: powershell -ExecutionPolicy Bypass -File inject.ps1

$proc = Get-Process CrimsonDesert -ErrorAction SilentlyContinue
if (-not $proc) { Write-Host "ERROR: Game not running!"; exit 1 }

$dllPath = "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonForgeFullSwitch.dll"
if (-not (Test-Path $dllPath)) { Write-Host "ERROR: DLL not found!"; exit 1 }

$processId = $proc.Id
Write-Host "Found CrimsonDesert.exe (PID: $processId)"
Write-Host "Injecting: $dllPath"

$code = @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public class DllInjector {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(int access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr VirtualAllocEx(IntPtr proc, IntPtr addr, uint size, uint type, uint protect);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool WriteProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, uint size, out int written);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetProcAddress(IntPtr mod, string name);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetModuleHandle(string name);

    [DllImport("kernel32.dll")]
    public static extern IntPtr CreateRemoteThread(IntPtr proc, IntPtr attr, uint stack, IntPtr start, IntPtr param, uint flags, out int tid);

    [DllImport("kernel32.dll")]
    public static extern int WaitForSingleObject(IntPtr handle, int ms);

    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr handle);

    public static bool Inject(int pid, string dllPath) {
        IntPtr hProc = OpenProcess(0x1F0FFF, false, pid);
        if (hProc == IntPtr.Zero) return false;

        byte[] dllBytes = Encoding.ASCII.GetBytes(dllPath + "\0");
        IntPtr alloc = VirtualAllocEx(hProc, IntPtr.Zero, (uint)dllBytes.Length, 0x3000, 0x40);
        if (alloc == IntPtr.Zero) { CloseHandle(hProc); return false; }

        int written = 0;
        WriteProcessMemory(hProc, alloc, dllBytes, (uint)dllBytes.Length, out written);

        IntPtr kernel32 = GetModuleHandle("kernel32.dll");
        IntPtr loadLib = GetProcAddress(kernel32, "LoadLibraryA");

        int tid = 0;
        IntPtr hThread = CreateRemoteThread(hProc, IntPtr.Zero, 0, loadLib, alloc, 0, out tid);

        if (hThread != IntPtr.Zero) {
            WaitForSingleObject(hThread, 10000);
            CloseHandle(hThread);
            CloseHandle(hProc);
            return true;
        }

        CloseHandle(hProc);
        return false;
    }
}
'@

Add-Type -TypeDefinition $code -Language CSharp

Write-Host "Injecting..."
$result = [DllInjector]::Inject($processId, $dllPath)

if ($result) {
    Write-Host ""
    Write-Host "SUCCESS! DLL injected into CrimsonDesert.exe"
    Write-Host "Check for log:"
    Write-Host "  bin64\crimson_fullswitch.log"
    Write-Host "  Desktop\crimson_fullswitch.log"
} else {
    Write-Host "FAILED! Run PowerShell as Administrator."
}
