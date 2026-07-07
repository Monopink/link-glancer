$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Missing .venv. Create the project virtual environment first."
}

& .venv\Scripts\python.exe -m pip install -e ".[build]"
& .venv\Scripts\python.exe -m playwright install
& .venv\Scripts\python.exe -m ruff check .
& .venv\Scripts\python.exe -m compileall -q src
& .venv\Scripts\python.exe scripts/generate_windows_icon.py

if (Test-Path build) {
    Remove-Item -LiteralPath build -Recurse -Force
}
if (Test-Path dist) {
    Remove-Item -LiteralPath dist -Recurse -Force
}

& .venv\Scripts\pyinstaller.exe --noconfirm packaging/link_glancer_windows.spec

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\LinkGlancer"
