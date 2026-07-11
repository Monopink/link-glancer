from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ICONSET_SIZES = (
    16,
    32,
    64,
    128,
    256,
    512,
    1024,
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / "packaging" / "icon.svg"
    iconset_dir = root / "build" / "macos" / "icon.iconset"

    app = QGuiApplication([])
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"Failed to load SVG icon: {source}")

    iconset_dir.mkdir(parents=True, exist_ok=True)
    for size in ICONSET_SIZES:
        _write_icon(renderer, iconset_dir / f"icon_{size}x{size}.png", size)
        if size < 512:
            _write_icon(renderer, iconset_dir / f"icon_{size}x{size}@2x.png", size * 2)

    _write_icon(renderer, iconset_dir / "icon_512x512@2x.png", 1024)
    app.quit()
    return 0


def _write_icon(renderer: QSvgRenderer, path: Path, size: int) -> None:
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(path)):
        raise RuntimeError(f"Failed to write icon PNG: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
