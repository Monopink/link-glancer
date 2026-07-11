#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script must be run on macOS." >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This build script currently supports Apple Silicon only." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv. Create the project virtual environment first." >&2
  exit 1
fi

source .venv/bin/activate

python -m pip install -e ".[build]"
python -m playwright install
python -m ruff check .
python -m compileall -q src
python scripts/generate_macos_icon.py
mkdir -p packaging
iconutil -c icns build/macos/icon.iconset -o packaging/icon.icns

rm -rf build dist

pyinstaller --noconfirm packaging/link_glancer_macos.spec

echo
echo "Build complete:"
echo "  dist/LinkGlancer.app"
