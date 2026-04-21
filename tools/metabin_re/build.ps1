<#
.SYNOPSIS
    Builds the AnimationMetaData reverse-engineering toolkit.

.DESCRIPTION
    Compiles two artifacts:

      * injector/injector.exe — external DLL injector (attaches to a
                                 running CrimsonDesert.exe and calls
                                 LoadLibraryA via CreateRemoteThread).
      * helper_dll/helper.dll — the helper DLL that, once injected,
                                 installs vtable hooks on every
                                 AnimationMetaData vfunc and writes
                                 a trace log to the Desktop.

    Uses either MSVC cl.exe (if available in the current environment)
    or MinGW-w64 x86_64-w64-mingw32-gcc (if available). The MSVC path
    requires running inside an x64 Native Tools Command Prompt; the
    MinGW path works from any shell as long as GCC is on PATH.

.EXAMPLE
    PS> cd C:\Users\hzeem\Desktop\crimsonforge
    PS> .\tools\metabin_re\build.ps1

    Builds both artifacts in-place.

.EXAMPLE
    PS> .\tools\metabin_re\build.ps1 -Compiler mingw

    Forces the MinGW path (useful when cl.exe is on PATH but you prefer GCC).
#>

param(
    [ValidateSet('auto', 'msvc', 'mingw')]
    [string]$Compiler = 'auto'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSCommandPath
$InjectorSrc = Join-Path $Root 'injector\injector.c'
$InjectorOut = Join-Path $Root 'injector\injector.exe'
$HelperSrc   = Join-Path $Root 'helper_dll\helper.c'
$HelperOut   = Join-Path $Root 'helper_dll\helper.dll'

function Test-MsvcAvailable {
    $cl = Get-Command cl.exe -ErrorAction SilentlyContinue
    return $null -ne $cl
}

function Test-MingwAvailable {
    $gcc = Get-Command x86_64-w64-mingw32-gcc -ErrorAction SilentlyContinue
    if ($null -eq $gcc) {
        $gcc = Get-Command gcc -ErrorAction SilentlyContinue
    }
    return $null -ne $gcc
}

# Resolve compiler choice.
if ($Compiler -eq 'auto') {
    if (Test-MsvcAvailable) {
        $Compiler = 'msvc'
        Write-Host "Using MSVC (cl.exe on PATH)" -ForegroundColor Cyan
    }
    elseif (Test-MingwAvailable) {
        $Compiler = 'mingw'
        Write-Host "Using MinGW (gcc on PATH)" -ForegroundColor Cyan
    }
    else {
        Write-Host "ERROR: neither cl.exe nor gcc found on PATH" -ForegroundColor Red
        Write-Host ""
        Write-Host "For MSVC: run this script from 'x64 Native Tools Command Prompt for VS 2022'"
        Write-Host "For MinGW: install MSYS2 and add its mingw64\bin to PATH"
        exit 1
    }
}

Push-Location $Root

try {
    if ($Compiler -eq 'msvc') {
        Write-Host "`nBuilding injector.exe (MSVC)..."
        & cl.exe /nologo /O2 /EHsc $InjectorSrc "/Fe:$InjectorOut" /link user32.lib advapi32.lib
        if ($LASTEXITCODE -ne 0) { throw "injector build failed" }

        Write-Host "`nBuilding helper.dll (MSVC)..."
        & cl.exe /nologo /O2 /LD $HelperSrc "/Fe:$HelperOut" /link user32.lib
        if ($LASTEXITCODE -ne 0) { throw "helper build failed" }
    }
    else {
        # MinGW
        $gcc = (Get-Command x86_64-w64-mingw32-gcc -ErrorAction SilentlyContinue)
        if ($null -eq $gcc) { $gcc = (Get-Command gcc -ErrorAction SilentlyContinue) }
        $gccPath = $gcc.Source

        Write-Host "`nBuilding injector.exe (MinGW)..."
        & $gccPath -O2 -o $InjectorOut $InjectorSrc -luser32 -ladvapi32
        if ($LASTEXITCODE -ne 0) { throw "injector build failed" }

        Write-Host "`nBuilding helper.dll (MinGW)..."
        & $gccPath -O2 -shared -o $HelperOut $HelperSrc -luser32
        if ($LASTEXITCODE -ne 0) { throw "helper build failed" }
    }

    Write-Host "`n=== Build complete ===" -ForegroundColor Green
    Write-Host "  injector: $InjectorOut"
    Write-Host "  helper:   $HelperOut"
    Write-Host ""
    Write-Host "Next step: see tools/metabin_re/README.md for how to use them." -ForegroundColor Yellow
}
finally {
    Pop-Location
}

# Clean up intermediate files (MSVC emits .obj and .exp/.lib files).
Get-ChildItem -Path $Root -Include *.obj, *.exp, *.lib -File -Recurse | Remove-Item -Force
