from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from link_glancer.tasks.models import ReviewField


class ReviewFieldWidget(QFrame):
    def __init__(
        self,
        field: ReviewField,
        *,
        option_shortcut_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.field = field
        self._option_shortcut_handler = option_shortcut_handler
        self.setFrameShape(QFrame.Shape.StyledPanel)

    def value(self) -> object:
        raise NotImplementedError

    def set_value(self, value: object) -> None:
        raise NotImplementedError

    def clear_value(self) -> None:
        raise NotImplementedError

    def is_complete(self) -> bool:
        if not self.field.required:
            return True

        value = self.value()
        if self.field.field_type == "multi_select":
            return isinstance(value, list) and len(value) > 0
        if self.field.field_type == "boolean":
            return value is not None
        if self.field.field_type == "text":
            return isinstance(value, str) and bool(value.strip())
        return value not in (None, "")

    def activate_shortcut(self, shortcut: str) -> bool:
        return False


class SingleSelectFieldWidget(ReviewFieldWidget):
    def __init__(
        self,
        field: ReviewField,
        *,
        option_shortcut_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(field, option_shortcut_handler=option_shortcut_handler)
        layout = QVBoxLayout(self)
        layout.addWidget(_field_title(field))

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        for option in field.options:
            option_widget = QWidget()
            option_layout = QHBoxLayout(option_widget)
            option_layout.setContentsMargins(0, 0, 0, 0)
            option_layout.setSpacing(6)
            button = QRadioButton(option.label)
            button.setProperty("option_value", option.value)
            self._buttons.addButton(button)
            option_layout.addWidget(button)
            option_layout.addStretch(1)
            option_layout.addWidget(
                _option_shortcut_button(
                    field_id=field.field_id,
                    option_value=option.value,
                    shortcut=option.shortcut,
                    handler=self._option_shortcut_handler,
                )
            )
            layout.addWidget(option_widget)

    def value(self) -> str | None:
        checked = self._buttons.checkedButton()
        if checked is None:
            return None
        return str(checked.property("option_value"))

    def set_value(self, value: object) -> None:
        found = False
        for button in self._buttons.buttons():
            is_selected = button.property("option_value") == value
            button.setChecked(is_selected)
            found = found or is_selected
        if not found:
            self._buttons.setExclusive(False)
            for button in self._buttons.buttons():
                button.setChecked(False)
            self._buttons.setExclusive(True)

    def clear_value(self) -> None:
        self.set_value(None)

    def activate_shortcut(self, shortcut: str) -> bool:
        for option, button in zip(self.field.options, self._buttons.buttons(), strict=False):
            if option.shortcut and option.shortcut.lower() == shortcut.lower():
                button.setChecked(True)
                return True
        return False


class MultiSelectFieldWidget(ReviewFieldWidget):
    def __init__(
        self,
        field: ReviewField,
        *,
        option_shortcut_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(field, option_shortcut_handler=option_shortcut_handler)
        layout = QVBoxLayout(self)
        layout.addWidget(_field_title(field))

        self._checkboxes: list[QCheckBox] = []
        for option in field.options:
            option_widget = QWidget()
            option_layout = QHBoxLayout(option_widget)
            option_layout.setContentsMargins(0, 0, 0, 0)
            option_layout.setSpacing(6)
            checkbox = QCheckBox(option.label)
            checkbox.setProperty("option_value", option.value)
            self._checkboxes.append(checkbox)
            option_layout.addWidget(checkbox)
            option_layout.addStretch(1)
            option_layout.addWidget(
                _option_shortcut_button(
                    field_id=field.field_id,
                    option_value=option.value,
                    shortcut=option.shortcut,
                    handler=self._option_shortcut_handler,
                )
            )
            layout.addWidget(option_widget)

    def value(self) -> list[str]:
        return [
            str(checkbox.property("option_value"))
            for checkbox in self._checkboxes
            if checkbox.isChecked()
        ]

    def set_value(self, value: object) -> None:
        selected = {str(item) for item in value} if isinstance(value, list) else set()
        for checkbox in self._checkboxes:
            checkbox.setChecked(str(checkbox.property("option_value")) in selected)

    def clear_value(self) -> None:
        for checkbox in self._checkboxes:
            checkbox.setChecked(False)

    def activate_shortcut(self, shortcut: str) -> bool:
        for option, checkbox in zip(self.field.options, self._checkboxes, strict=False):
            if option.shortcut and option.shortcut.lower() == shortcut.lower():
                checkbox.setChecked(not checkbox.isChecked())
                return True
        return False


class BooleanFieldWidget(ReviewFieldWidget):
    def __init__(
        self,
        field: ReviewField,
        *,
        option_shortcut_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(field, option_shortcut_handler=option_shortcut_handler)
        self._current: bool | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(_field_title(field))

        row = QHBoxLayout()
        self._yes = QPushButton("Yes")
        self._no = QPushButton("No")
        self._yes.setCheckable(True)
        self._no.setCheckable(True)
        self._yes.clicked.connect(lambda checked: self._toggle(True, checked))
        self._no.clicked.connect(lambda checked: self._toggle(False, checked))
        row.addWidget(self._yes)
        row.addWidget(self._no)
        row.addStretch(1)
        layout.addLayout(row)

    def _toggle(self, value: bool, checked: bool) -> None:
        if not checked:
            self._current = None
            self._yes.setChecked(False)
            self._no.setChecked(False)
            return

        self._current = value
        self._yes.setChecked(value is True)
        self._no.setChecked(value is False)

    def value(self) -> bool | None:
        return self._current

    def set_value(self, value: object) -> None:
        if value is True:
            self._toggle(True, True)
        elif value is False:
            self._toggle(False, True)
        else:
            self._current = None
            self._yes.setChecked(False)
            self._no.setChecked(False)

    def clear_value(self) -> None:
        self.set_value(None)


class TextFieldWidget(ReviewFieldWidget):
    def __init__(
        self,
        field: ReviewField,
        *,
        option_shortcut_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(field, option_shortcut_handler=option_shortcut_handler)
        layout = QVBoxLayout(self)
        layout.addWidget(_field_title(field))

        if field.required:
            editor: QWidget = QPlainTextEdit()
            editor.setMinimumHeight(100)
            self._editor = editor
        else:
            self._editor = QLineEdit()

        layout.addWidget(self._editor)

    def value(self) -> str:
        if isinstance(self._editor, QPlainTextEdit):
            return self._editor.toPlainText()
        return self._editor.text()

    def set_value(self, value: object) -> None:
        text = "" if value is None else str(value)
        if isinstance(self._editor, QPlainTextEdit):
            self._editor.setPlainText(text)
        else:
            self._editor.setText(text)

    def clear_value(self) -> None:
        self.set_value("")


def create_field_widget(
    field: ReviewField,
    *,
    option_shortcut_handler: Callable[[str, str], None] | None = None,
) -> ReviewFieldWidget:
    widget_types = {
        "single_select": SingleSelectFieldWidget,
        "multi_select": MultiSelectFieldWidget,
        "boolean": BooleanFieldWidget,
        "text": TextFieldWidget,
    }
    return widget_types[field.field_type](
        field,
        option_shortcut_handler=option_shortcut_handler,
    )


def _field_title(field: ReviewField) -> QLabel:
    suffix = "（必填）" if field.required else "（选填）"
    label = QLabel(f"{field.label}{suffix}")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _option_shortcut_button(
    *,
    field_id: str,
    option_value: str,
    shortcut: str | None,
    handler: Callable[[str, str], None] | None,
) -> QPushButton:
    button = QPushButton(shortcut or "未设置")
    button.setProperty("field_id", field_id)
    button.setProperty("option_value", option_value)
    button.setMinimumWidth(60)
    button.setMaximumWidth(92)
    if handler is not None:
        button.clicked.connect(lambda: handler(field_id, option_value))
    else:
        button.setEnabled(False)
    return button
