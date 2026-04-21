$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "Visual Studio Build Tools not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD /D_CRT_SECURE_NO_WARNINGS crimson_character_unlock.c /link /MACHINE:X64 /OUT:CrimsonForgeCharUnlock.dll kernel32.lib /NODEFAULTLIB:psapi.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    Copy-Item CrimsonForgeCharUnlock.dll CrimsonForgeCharUnlock.asi -Force
    Write-Host "`n=== BUILD OK ==="
    Write-Host "  CrimsonForgeCharUnlock.asi ready"
    Write-Host "`nDeploy: copy to bin64/scripts/CrimsonForgeRTL.asi"
} finally {
    Pop-Location
}
