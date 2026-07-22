from __future__ import annotations

import re
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "src" / "creator_enrichment" / "version.py"
VERSION_PATTERN = re.compile(
    r'^(CREATOR_ENRICHMENT_IMPL_VERSION\s*=\s*")v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)\.(?P<build>\d+)(")$',
    re.MULTILINE,
)


def main() -> int:
    original = VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(original)
    if match is None:
        raise SystemExit(f"invalid version format in {VERSION_FILE}")
    build = int(match.group("build")) + 1
    updated = VERSION_PATTERN.sub(
        rf"\1v{match.group('major')}.{match.group('minor')}.{match.group('patch')}.{build}\6",
        original,
        count=1,
    )
    if updated != original:
        VERSION_FILE.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
