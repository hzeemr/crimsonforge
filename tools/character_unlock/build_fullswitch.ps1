$ErrorActionPreference = "Stop"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }
$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot
$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD crimson_fullswitch.c /link /MACHINE:X64 /OUT:CrimsonForgeFullSwitch.dll kernel32.lib user32.lib"
) -join ' && '
Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item CrimsonForgeFullSwitch.dll CrimsonForgeFullSwitch.asi -Force
    Write-Host "`n=== BUILD OK ==="
    Write-Host "  CrimsonForgeFullSwitch.asi ready"
    Write-Host "  Features: RTTI auto-scanner + forbidden list bypass + keybind switching"
    Write-Host "  Hotkeys: F2=Damiane, F3=Oongka, F4=Kliff, F5=Yahn"
} finally { Pop-Location }
