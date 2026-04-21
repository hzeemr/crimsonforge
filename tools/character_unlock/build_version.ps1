$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "Visual Studio Build Tools not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD crimson_version_proxy.c /link /MACHINE:X64 /DEF:version.def /OUT:version.dll kernel32.lib user32.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }

    $gameBin = "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64"
    if (Test-Path $gameBin) {
        Copy-Item version.dll "$gameBin\version.dll" -Force
        Write-Host "`n=== BUILD + DEPLOY OK ==="
        Write-Host "  version.dll copied to: $gameBin"
    } else {
        Write-Host "`n=== BUILD OK ==="
        Write-Host "  version.dll ready"
        Write-Host "  Deploy manually: copy version.dll to bin64/"
    }
} finally {
    Pop-Location
}
