#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This release script must be run on macOS." >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This release script currently supports Apple Silicon only." >&2
  exit 1
fi

if [[ -z "${CODESIGN_IDENTITY:-}" ]]; then
  echo "Missing CODESIGN_IDENTITY." >&2
  echo "Example: export CODESIGN_IDENTITY='Developer ID Application: Example Corp (TEAMID)'" >&2
  exit 1
fi

if [[ -z "${NOTARY_PROFILE:-}" ]]; then
  echo "Missing NOTARY_PROFILE." >&2
  echo "Create one with notarytool store-credentials first." >&2
  exit 1
fi

VERSION="$(python - <<'PY'
from pathlib import Path
namespace = {}
exec(Path("src/link_glancer/__init__.py").read_text(encoding="utf-8"), namespace)
print(namespace["__version__"])
PY
)"
APP_PATH="dist/LinkGlancer.app"
DMG_DIR="dist/release"
DMG_NAME="LinkGlancer-${VERSION}-macos-arm64.dmg"
DMG_PATH="${DMG_DIR}/${DMG_NAME}"
ZIP_PATH="dist/LinkGlancer-${VERSION}-macos-arm64.zip"
ENTITLEMENTS="packaging/link_glancer_macos.entitlements"

bash "$ROOT_DIR/scripts/build_macos.sh"

mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH" "$ZIP_PATH"

codesign --force --deep --options runtime --timestamp \
  --entitlements "$ENTITLEMENTS" \
  --sign "$CODESIGN_IDENTITY" \
  "$APP_PATH"

codesign --verify --deep --strict --verbose=2 "$APP_PATH"

ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ZIP_PATH"
xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP_PATH"

hdiutil create \
  -volname "Link Glancer" \
  -srcfolder "$APP_PATH" \
  -ov -format UDZO \
  "$DMG_PATH"

codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG_PATH"
xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG_PATH"

echo
echo "Release build complete:"
echo "  $DMG_PATH"
echo "  $APP_PATH"
