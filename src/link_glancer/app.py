from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory, QSystemTrayIcon

from link_glancer.runtime.dev import dev_mode_title_suffix
from link_glancer.runtime.locks import register_instance
from link_glancer.runtime.paths import bundled_asset_path
from link_glancer.tasks.database import DatabaseResetRequiredError, reset_app_database
from link_glancer.ui.fonts import apply_application_font
from link_glancer.ui.main_window import MainWindow


def create_application() -> QApplication:
    _set_windows_app_user_model_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName(f"LinkGlancer{dev_mode_title_suffix()}")
    app.setOrganizationName("LinkGlancer")
    instance_registration = register_instance()
    icon_path = bundled_asset_path("packaging", "icon.svg")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    tray_icon = QSystemTrayIcon(app.windowIcon(), app)
    tray_icon.setToolTip("LinkGlancer")
    tray_icon.show()
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
        QHeaderView::section {
            border: 0;
            border-bottom: 1px solid palette(mid);
            padding: 8px;
            background: palette(window);
        }
        QTableWidget {
            border: 1px solid palette(mid);
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
        window = _create_main_window_with_database_recovery(
            instance_id=instance_registration.instance_id
        )
    except Exception as exc:
        instance_registration.release()
        QMessageBox.critical(None, "启动失败", str(exc))
        raise SystemExit(1) from exc
    tray_icon.messageClicked.connect(lambda: _activate_main_window(window))
    app.main_window = window  # type: ignore[attr-defined]
    app.tray_icon = tray_icon  # type: ignore[attr-defined]
    app.instance_registration = instance_registration  # type: ignore[attr-defined]
    app.aboutToQuit.connect(lambda: instance_registration.release())
    window.show()
    window.raise_()
    window.activateWindow()

    return app


def _create_main_window_with_database_recovery(*, instance_id: int) -> MainWindow:
    try:
        return MainWindow(instance_id=instance_id)
    except DatabaseResetRequiredError as exc:
        result = QMessageBox.warning(
            None,
            "数据库需要重建",
            "当前数据库无法平稳迁移到新版本，继续将清空现有任务数据并重建数据库。\n\n"
            f"原因：{exc.reason}\n\n"
            "是否继续重建？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result != QMessageBox.StandardButton.Yes:
            raise RuntimeError("用户取消了数据库重建。") from exc
        reset_app_database()
        return MainWindow(instance_id=instance_id)


def _apply_color_scheme(app: QApplication) -> None:
    if app.styleHints().colorScheme() != Qt.ColorScheme.Dark:
        _apply_disabled_button_text_palette(app, app.palette().color(QPalette.ColorRole.Mid))
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
    _apply_disabled_button_text_palette(app, QColor("#8e97a5"))


def _apply_disabled_button_text_palette(app: QApplication, color: QColor) -> None:
    palette = app.palette()
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, color)
    app.setPalette(palette)


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LinkGlancer.Desktop")
    except (AttributeError, OSError):
        return


def _activate_main_window(window: MainWindow) -> None:
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
