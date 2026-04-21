$ErrorActionPreference = "Stop"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }
$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot
$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD crimson_winmm_fullswitch.c /link /MACHINE:X64 /OUT:winmm_fullswitch.dll kernel32.lib user32.lib"
) -join ' && '
Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    $gameBin = "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64"
    Copy-Item winmm_fullswitch.dll "$gameBin\winmm.dll" -Force
    Write-Host "`n=== BUILD + DEPLOY OK ==="
    Write-Host "  winmm.dll replaced with fullswitch proxy"
} finally { Pop-Location }
