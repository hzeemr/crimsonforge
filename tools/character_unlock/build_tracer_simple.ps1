$ErrorActionPreference = "Stop"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot
$hookSrc = "C:\Users\hzeem\Desktop\hzeem app\hzeem app\tools\rtl_hook\hook_backend.c"

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD /D_CRT_SECURE_NO_WARNINGS /I`"$workDir`" `"$workDir\crimson_live_tracer.c`" `"$hookSrc`" /link /MACHINE:X64 /OUT:CrimsonForgeLiveTracer.dll kernel32.lib user32.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item CrimsonForgeLiveTracer.dll CrimsonForgeLiveTracer.asi -Force
    Write-Host "`n=== BUILD OK (InlineAbsJump, no MinHook) ==="
} finally {
    Pop-Location
}
