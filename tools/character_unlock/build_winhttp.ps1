$ErrorActionPreference = "Stop"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "VS not found" }
$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot
$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD crimson_winhttp_proxy.c /link /MACHINE:X64 /OUT:winhttp.dll kernel32.lib user32.lib"
) -join ' && '
Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    $gameBin = "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64"
    if (Test-Path $gameBin) {
        Copy-Item winhttp.dll "$gameBin\winhttp.dll" -Force
        Write-Host "`n=== BUILD + DEPLOY OK ==="
        Write-Host "  winhttp.dll → $gameBin"
    } else {
        Write-Host "`n=== BUILD OK === (deploy manually)"
    }
} finally { Pop-Location }
