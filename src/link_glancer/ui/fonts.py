from __future__ import annotations

import sys

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog

PREFERRED_FONT_FAMILIES = (
    "Segoe UI",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "Tahoma",
    "Arial",
)

PREFERRED_TABLE_FONT_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
)


def apply_application_font(app: QApplication) -> str:
    if sys.platform != "win32":
        return app.font().family()
    family = _choose_font_family(PREFERRED_FONT_FAMILIES)
    font = QFont(family) if family else QFont()
    font.setPointSize(10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    return family or app.font().family()


def create_table_font(*, point_size: int = 10) -> QFont:
    if sys.platform != "win32":
        return QFont()
    family = _choose_font_family(PREFERRED_TABLE_FONT_FAMILIES) or _choose_font_family(
        PREFERRED_FONT_FAMILIES
    )
    font = QFont(family) if family else QFont()
    font.setPointSize(point_size)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def table_header_stylesheet() -> str:
    if sys.platform != "win32":
        return ""
    family = _choose_font_family(PREFERRED_TABLE_FONT_FAMILIES) or _choose_font_family(
        PREFERRED_FONT_FAMILIES
    )
    if not family:
        return ""
    return (
        "QHeaderView {"
        f' font-family: "{family}";'
        "}"
        "QHeaderView::section {"
        f' font-family: "{family}";'
        "}"
    )


def dialog_stylesheet() -> str:
    if sys.platform != "win32":
        return ""
    family = _choose_font_family(PREFERRED_TABLE_FONT_FAMILIES) or _choose_font_family(
        PREFERRED_FONT_FAMILIES
    )
    if not family:
        return ""
    return (
        "QDialog, QMessageBox, QFileDialog, QLabel, QPushButton, QLineEdit, QTextEdit, "
        "QPlainTextEdit, QCheckBox, QRadioButton, QComboBox, QSpinBox, QKeySequenceEdit, "
        "QGroupBox, QMenu {"
        f' font-family: "{family}";'
        "}"
        "QComboBox QAbstractItemView, QListView, QTreeView, QTableView, QAbstractItemView {"
        f' font-family: "{family}";'
        "}"
    )


def non_native_file_dialog_options() -> QFileDialog.Option:
    options = QFileDialog.Option(0)
    if sys.platform == "win32":
        options |= QFileDialog.Option.DontUseNativeDialog
    return options


def _choose_font_family(candidates: tuple[str, ...]) -> str | None:
    available = set(QFontDatabase.families())
    for family in candidates:
        if family in available:
            return family
    return None
