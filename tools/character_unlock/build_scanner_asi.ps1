$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot
$hookSrc = "C:\Users\hzeem\Desktop\hzeem app\hzeem app\tools\rtl_hook\hook_backend.c"
$mhRoot = "C:\Users\hzeem\Desktop\hzeem app\hzeem app\tools\rtl_hook\vendor\MinHook"
$mhSources = @(
    (Join-Path $mhRoot "src\buffer.c"),
    (Join-Path $mhRoot "src\hook.c"),
    (Join-Path $mhRoot "src\trampoline.c"),
    (Join-Path $mhRoot "src\hde\hde64.c")
)

$srcFiles = "`"$workDir\crimson_charswitch_scanner.c`" `"$hookSrc`""
foreach ($s in $mhSources) { $srcFiles += " `"$s`"" }

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD /D_CRT_SECURE_NO_WARNINGS /DCRIMSON_USE_MINHOOK=1 /I`"$(Join-Path $mhRoot 'include')`" /I`"$(Join-Path $mhRoot 'src')`" $srcFiles /link /MACHINE:X64 /OUT:CrimsonForgeCharScan.dll user32.lib psapi.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item CrimsonForgeCharScan.dll CrimsonForgeCharScan.asi -Force
    Write-Host "`n=== BUILD OK ==="
    Write-Host "  CrimsonForgeCharScan.asi (with MinHook)"
} finally {
    Pop-Location
}
