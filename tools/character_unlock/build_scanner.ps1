$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found" }

$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installPath) { throw "Visual Studio Build Tools not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$source = Join-Path $PSScriptRoot "crimson_charswitch_scanner.c"
$defFile = Join-Path $PSScriptRoot "xinput1_4.def"
$outDll = Join-Path $PSScriptRoot "xinput1_4.dll"

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$PSScriptRoot`"",
    "cl /nologo /W3 /O2 /LD /DWIN32 /D_WINDOWS `"$source`" /link /MACHINE:X64 /DEF:`"$defFile`" /OUT:`"$outDll`" user32.lib psapi.lib xinput.lib"
) -join ' && '

Push-Location $PSScriptRoot
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Write-Host ""
    Write-Host "=== BUILD OK ==="
    Write-Host "Output: $outDll"
    Write-Host ""
    Write-Host "Deploy:"
    Write-Host "  1. Copy xinput1_4.dll from game bin64/ to xinput1_4_orig.dll"
    Write-Host "  2. Copy this xinput1_4.dll to game bin64/"
    Write-Host "  3. Launch game, switch to Damian, go to a Kliff quest"
    Write-Host "  4. When popup appears, press F8 in-game"
    Write-Host "  5. Close game"
    Write-Host "  6. Check bin64/crimson_charswitch_scan.log"
    Write-Host ""
} finally {
    Pop-Location
}
