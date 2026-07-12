$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Missing .venv. Create the project virtual environment first."
}

& .venv\Scripts\python.exe -c "import PyInstaller, PySide6, playwright, openpyxl"
if ($LASTEXITCODE -ne 0) {
    throw "Missing build/runtime dependencies in .venv. Install the project environment first."
}

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
& .venv\Scripts\python.exe scripts\trim_packaged_distribution.py dist\LinkGlancer

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\LinkGlancer"
