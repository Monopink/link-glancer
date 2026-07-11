from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    svg_path = project_root / "packaging" / "icon.svg"
    ico_path = project_root / "packaging" / "icon.ico"
    if not svg_path.exists():
        raise FileNotFoundError(f"Missing source icon: {svg_path}")

    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG icon: {svg_path}")

    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(ico_path), "ICO"):
        raise OSError(f"Failed to save ICO icon: {ico_path}")
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
