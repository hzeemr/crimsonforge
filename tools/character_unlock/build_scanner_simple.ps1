$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot

# Build WITHOUT MinHook — scanner v3 uses VEH breakpoints only
$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD /D_CRT_SECURE_NO_WARNINGS crimson_charswitch_scanner.c /link /MACHINE:X64 /OUT:CrimsonForgeCharScan.dll user32.lib psapi.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item CrimsonForgeCharScan.dll CrimsonForgeCharScan.asi -Force
    Write-Host "`n=== BUILD OK (no MinHook) ==="
} finally {
    Pop-Location
}
