from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _remove_file(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    size = path.stat().st_size
    path.unlink()
    return size


def _remove_tree(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    size = sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    shutil.rmtree(path)
    return size


def _candidate_roots(dist_path: Path) -> list[Path]:
    roots = [dist_path]
    macos_internal = dist_path / "Contents" / "MacOS" / "_internal"
    if macos_internal.exists():
        roots.append(macos_internal)
    internal = dist_path / "_internal"
    if internal.exists():
        roots.append(internal)
    return roots


def trim_distribution(dist_path: Path) -> int:
    removed_bytes = 0
    for root in _candidate_roots(dist_path):
        translations_dir = root / "PySide6" / "translations"
        removed_bytes += _remove_tree(translations_dir)

        playwright_driver = root / "playwright" / "driver"
        removed_bytes += _remove_file(playwright_driver / "README.md")
        removed_bytes += _remove_file(playwright_driver / "package" / "README.md")
        removed_bytes += _remove_tree(playwright_driver / "package" / "types")
    return removed_bytes


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit("usage: python scripts/trim_packaged_distribution.py <dist-path>")

    dist_path = Path(args[0]).resolve()
    if not dist_path.exists():
        raise SystemExit(f"distribution path not found: {dist_path}")

    removed_bytes = trim_distribution(dist_path)
    print(
        f"Trimmed packaged distribution: {dist_path} "
        f"(removed {removed_bytes / (1024 * 1024):.2f} MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
