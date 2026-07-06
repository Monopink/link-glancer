from __future__ import annotations

from PySide6.QtCore import QLocale
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

PREFERRED_UI_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "DengXian",
    "SimHei",
)


def apply_application_font(app: QApplication) -> str:
    QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    family = choose_ui_font_family()
    font = QFont(family)
    font.setPointSize(10)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    return family


def choose_ui_font_family() -> str:
    available = set(QFontDatabase.families())
    for family in PREFERRED_UI_FONTS:
        if family in available:
            return family
    return QApplication.font().family()
