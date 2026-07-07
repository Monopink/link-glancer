$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Missing .venv. Create the project virtual environment first."
}

if (-not (Test-Path "dist\LinkGlancer\LinkGlancer.exe")) {
    Write-Host "Windows app build not found. Building application first..."
    & powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
}

$isccCommand = Get-Command iscc -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }

if (-not $isccPath) {
    $defaultIscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (Test-Path $defaultIscc) {
        $isccPath = $defaultIscc
    }
}

if (-not $isccPath) {
    throw "Missing Inno Setup 6. Install it first, then rerun scripts\build_windows_installer.ps1."
}

& $isccPath packaging\link_glancer_windows.iss

Write-Host ""
Write-Host "Installer build complete:"
Write-Host "  dist\installer\LinkGlancer-Setup.exe"
