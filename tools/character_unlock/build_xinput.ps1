$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $installPath) { throw "Visual Studio Build Tools not found" }

$vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
$workDir = $PSScriptRoot

$cmd = @(
    "call `"$vcvars`" >nul",
    "cd /d `"$workDir`"",
    "cl /nologo /W3 /O2 /LD crimson_xinput_proxy.c /link /MACHINE:X64 /OUT:xinput1_4.dll kernel32.lib user32.lib"
) -join ' && '

Push-Location $workDir
try {
    cmd.exe /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }

    $gameBin = "C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64"
    if (Test-Path $gameBin) {
        # Backup original if it exists and isn't ours
        $target = Join-Path $gameBin "xinput1_4.dll"
        if (Test-Path $target) {
            $backup = Join-Path $gameBin "xinput1_4_original.dll"
            if (-not (Test-Path $backup)) {
                Copy-Item $target $backup -Force
                Write-Host "  Backed up original xinput1_4.dll -> xinput1_4_original.dll"
            }
        }
        Copy-Item xinput1_4.dll $target -Force
        # Clean up failed version.dll proxy
        $oldVersion = Join-Path $gameBin "version.dll"
        if (Test-Path $oldVersion) { Remove-Item $oldVersion -Force; Write-Host "  Removed unused version.dll proxy" }
        Write-Host "`n=== BUILD + DEPLOY OK ==="
        Write-Host "  xinput1_4.dll copied to: $gameBin"
        Write-Host "  Restart the game for patches to take effect."
    } else {
        Write-Host "`n=== BUILD OK ==="
        Write-Host "  xinput1_4.dll ready"
        Write-Host "  Deploy: copy to game bin64/ folder"
    }
} finally {
    Pop-Location
}
