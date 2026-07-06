from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory

from link_glancer.ui.fonts import apply_application_font
from link_glancer.ui.main_window import MainWindow


def create_application() -> QApplication:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName("Link Glancer")
    app.setOrganizationName("Link Glancer")
    app.setStyle(QStyleFactory.create("Fusion"))
    apply_application_font(app)
    _apply_color_scheme(app)
    app.setStyleSheet(
        """
        QWidget[card="true"] {
            border: 1px solid palette(mid);
            border-radius: 12px;
            background: palette(base);
        }
        QLabel[sectionTitle="true"] {
            font-size: 16px;
            font-weight: 600;
        }
        QLabel[muted="true"] {
            color: palette(mid);
        }
        QToolBar {
            border: 0;
            spacing: 8px;
            padding: 6px 8px;
        }
        QPushButton:disabled {
            color: palette(mid);
            background: palette(window);
            border: 1px solid palette(mid);
        }
        QHeaderView::section {
            border: 0;
            border-bottom: 1px solid palette(mid);
            padding: 8px;
            background: palette(window);
        }
        QTableWidget {
            border: 1px solid palette(mid);
            border-radius: 10px;
            background: palette(base);
            alternate-background-color: palette(window);
            gridline-color: palette(mid);
        }
        QTableWidget::item:selected {
            background: transparent;
            color: palette(text);
        }
        QTableWidget::item:focus {
            border: 1px solid #9a9a9a;
        }
        QListWidget {
            border: 1px solid palette(mid);
            border-radius: 10px;
            background: palette(base);
        }
        """
    )

    try:
        window = MainWindow()
    except RuntimeError as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        raise SystemExit(1) from exc
    app.main_window = window  # type: ignore[attr-defined]
    window.show()

    return app


def _apply_color_scheme(app: QApplication) -> None:
    if app.styleHints().colorScheme() != Qt.ColorScheme.Dark:
        return

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#17191d"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f3f4f6"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#20242a"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#181c21"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#20242a"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f3f4f6"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f3f4f6"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#232830"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f3f4f6"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#3b82f6"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#98a2b3"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#3a4048"))
    app.setPalette(palette)
