from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from link_glancer.application.services import TaskApplicationService
from link_glancer.tasks.models import (
    BrowserConfig,
    ReviewField,
    ReviewOption,
    ReviewShortcutConfig,
    TaskSnapshot,
)

REVIEW_FIELD_TYPE_OPTIONS = [
    ("单选", "single_select"),
    ("多选", "multi_select"),
    ("文本", "text"),
    ("布尔", "boolean"),
]


class ReviewOptionsDialog(QDialog):
    def __init__(self, options: list[ReviewOption], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.options = [deepcopy(option) for option in options]
        self.setWindowTitle("编辑选项")
        self.resize(640, 360)

        root = QVBoxLayout(self)
        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["值", "显示名", "快捷键"])
        _setup_table(self._table)
        _fill_options_table(self._table, self.options)
        root.addWidget(self._table, stretch=1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        add_button = QPushButton("添加")
        remove_button = QPushButton("删除")
        up_button = QPushButton("上移")
        down_button = QPushButton("下移")
        add_button.clicked.connect(lambda: _add_empty_option_row(self._table))
        remove_button.clicked.connect(lambda: _remove_selected_row(self._table))
        up_button.clicked.connect(lambda: _move_selected_row(self._table, -1))
        down_button.clicked.connect(lambda: _move_selected_row(self._table, 1))
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addWidget(up_button)
        actions.addWidget(down_button)
        root.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _accept(self) -> None:
        try:
            self.options = _collect_options(self._table)
        except ValueError as exc:
            QMessageBox.warning(self, "选项无效", str(exc))
            return
        self.accept()


class TaskCreationDialog(QDialog):
    def __init__(
        self,
        *,
        browser_configs: list[BrowserConfig],
        app_service: TaskApplicationService,
        source_path: Path | None = None,
        initial_snapshot: TaskSnapshot | None = None,
        dialog_title: str = "新建任务",
        accept_button_text: str = "创建",
        source_editable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.source_path: Path | None = source_path
        self.task_snapshot: TaskSnapshot | None = None
        self._browser_configs = browser_configs
        self._app_service = app_service
        self._available_headers: list[str] = []
        self._accept_button_text = accept_button_text
        self._source_editable = source_editable

        self.setWindowTitle(dialog_title)
        self.resize(980, 760)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._source_edit = QLineEdit()
        self._source_edit.setReadOnly(True)
        source_row = QHBoxLayout()
        source_row.addWidget(self._source_edit, stretch=1)
        source_button = QPushButton("浏览...")
        source_button.clicked.connect(self._browse_source)
        source_button.setEnabled(source_editable)
        source_row.addWidget(source_button)
        source_widget = QWidget()
        source_widget.setLayout(source_row)

        self._sheet_combo = QComboBox()
        self._sheet_combo.currentIndexChanged.connect(self._reload_headers)
        self._header_row_spin = QSpinBox()
        self._header_row_spin.setRange(1, 10000)
        self._header_row_spin.setValue(1)
        self._header_row_spin.valueChanged.connect(self._reload_headers)
        self._browser_combo = QComboBox()
        for config in self._browser_configs:
            self._browser_combo.addItem(f"{config.name} ({config.config_id})", config.config_id)
        self._open_tab_spin = QSpinBox()
        self._open_tab_spin.setRange(1, 10)
        self._open_tab_spin.setValue(3)
        self._confirm_url_edit = QLineEdit()
        self._url_field_combo = QComboBox()
        self._url_field_combo.setEditable(True)
        self._display_fields_edit = QLineEdit()
        self._export_fields_edit = QLineEdit()

        form.addRow("Excel 文件", source_widget)
        form.addRow("Sheet", self._sheet_combo)
        form.addRow("标题行", self._header_row_spin)
        form.addRow("浏览器配置", self._browser_combo)
        form.addRow("标签数", self._open_tab_spin)
        form.addRow("确认页面", self._confirm_url_edit)
        form.addRow("URL 列名", self._url_field_combo)
        form.addRow("展示列名", self._display_fields_edit)
        form.addRow("导出字段", self._export_fields_edit)
        root.addLayout(form)

        shortcut_group = QGridLayout()
        self._submit_shortcut_edit = QKeySequenceEdit()
        self._submit_shortcut_edit.setKeySequence(QKeySequence("Enter"))
        self._previous_shortcut_edit = QKeySequenceEdit()
        self._previous_shortcut_edit.setKeySequence(QKeySequence("Backspace"))
        self._exit_shortcut_edit = QKeySequenceEdit()
        self._exit_shortcut_edit.setKeySequence(QKeySequence("Esc"))
        shortcut_group.addWidget(QLabel("提交快捷键"), 0, 0)
        shortcut_group.addWidget(self._submit_shortcut_edit, 0, 1)
        shortcut_group.addWidget(QLabel("上一条快捷键"), 0, 2)
        shortcut_group.addWidget(self._previous_shortcut_edit, 0, 3)
        shortcut_group.addWidget(QLabel("退出快捷键"), 0, 4)
        shortcut_group.addWidget(self._exit_shortcut_edit, 0, 5)
        root.addLayout(shortcut_group)

        root.addWidget(QLabel("检查项"))
        self._review_fields_table = QTableWidget()
        self._review_fields_table.setColumnCount(5)
        self._review_fields_table.setHorizontalHeaderLabels(
            ["结果列名", "问题标题", "类型", "必填", "选项"]
        )
        _setup_table(self._review_fields_table)
        root.addWidget(self._review_fields_table, stretch=1)

        table_actions = QHBoxLayout()
        table_actions.addStretch(1)
        add_review_button = QPushButton("添加检查项")
        remove_review_button = QPushButton("删除检查项")
        up_review_button = QPushButton("上移")
        down_review_button = QPushButton("下移")
        add_review_button.clicked.connect(lambda: _add_empty_review_row(self._review_fields_table))
        remove_review_button.clicked.connect(
            lambda: _remove_selected_row(self._review_fields_table)
        )
        up_review_button.clicked.connect(lambda: _move_selected_row(self._review_fields_table, -1))
        down_review_button.clicked.connect(lambda: _move_selected_row(self._review_fields_table, 1))
        table_actions.addWidget(add_review_button)
        table_actions.addWidget(remove_review_button)
        table_actions.addWidget(up_review_button)
        table_actions.addWidget(down_review_button)
        root.addLayout(table_actions)

        self._warnings_label = QLabel()
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self._warnings_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(accept_button_text)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._initialize_form(initial_snapshot)

    def _initialize_form(self, initial_snapshot: TaskSnapshot | None) -> None:
        if self.source_path is not None:
            if self.source_path.exists():
                self._source_edit.setText(str(self.source_path))
                self._load_sheet_names()
            else:
                self.source_path = None
        if initial_snapshot is not None:
            self._apply_snapshot(initial_snapshot)
        elif self._review_fields_table.rowCount() == 0:
            _add_empty_review_row(self._review_fields_table)
        self._update_warnings([])

    def _load_sheet_names(self) -> None:
        if self.source_path is None:
            return
        try:
            sheet_names = self._app_service.list_sheet_names(self.source_path)
        except ValueError as exc:
            QMessageBox.warning(self, "Excel 读取失败", str(exc))
            return
        self._sheet_combo.clear()
        self._sheet_combo.addItems(sheet_names)
        if len(sheet_names) == 1:
            self._sheet_combo.setCurrentIndex(0)
        self._reload_headers()

    def _apply_snapshot(self, snapshot: TaskSnapshot) -> None:
        self._set_combo_text(self._sheet_combo, snapshot.sheet_name)
        self._header_row_spin.setValue(snapshot.header_row)
        self._set_browser_config(snapshot.browser_config_id)
        self._open_tab_spin.setValue(snapshot.open_tab_count)
        self._confirm_url_edit.setText(snapshot.confirm_url or "")
        self._reload_headers()
        self._url_field_combo.setCurrentText(snapshot.url_field)
        self._display_fields_edit.setText(", ".join(snapshot.display_fields))
        self._export_fields_edit.setText(", ".join(snapshot.export_fields))
        self._submit_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.submit))
        self._previous_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.previous))
        self._exit_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.exit))
        _fill_review_fields_table(self._review_fields_table, snapshot.review_fields)

    def _set_combo_text(self, combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.isEditable():
            combo.setCurrentText(value)

    def _set_browser_config(self, config_id: str) -> None:
        for index in range(self._browser_combo.count()):
            if self._browser_combo.itemData(index) == config_id:
                self._browser_combo.setCurrentIndex(index)
                return

    def _browse_source(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择任务源文件", str(Path.cwd()), "Excel Workbook (*.xlsx)"
        )
        if not selected:
            return
        self.source_path = Path(selected).resolve()
        self._source_edit.setText(str(self.source_path))
        self._load_sheet_names()

    def _reload_headers(self) -> None:
        if self.source_path is None:
            return
        sheet_name = self._sheet_combo.currentText().strip()
        if not sheet_name:
            return
        try:
            headers = self._app_service.list_headers(
                self.source_path,
                sheet_name=sheet_name,
                header_row=int(self._header_row_spin.value()),
            )
        except ValueError:
            headers = []
        current_url = self._url_field_combo.currentText().strip()
        self._available_headers = headers
        self._url_field_combo.clear()
        self._url_field_combo.addItems(headers)
        if current_url:
            self._url_field_combo.setCurrentText(current_url)
        elif headers:
            self._url_field_combo.setCurrentText(headers[0])

    def _accept(self) -> None:
        try:
            snapshot = self._collect_task_snapshot()
            self._app_service.validate_review_fields(snapshot.review_fields)
            warnings = self._app_service.validate_task_snapshot(
                source_path=self.source_path,
                task_snapshot=snapshot,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "任务配置无效", str(exc))
            return

        warning_lines = _warning_lines(warnings)
        if warning_lines:
            self._update_warnings(warning_lines)
            result = QMessageBox.question(
                self,
                f"确认{self._accept_button_text}",
                "检测到以下提示：\n\n"
                + "\n".join(warning_lines)
                + f"\n\n仍然{self._accept_button_text}吗？",
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        self.task_snapshot = snapshot
        self.accept()

    def _collect_task_snapshot(self) -> TaskSnapshot:
        if self.source_path is None:
            raise ValueError("请选择 Excel 文件。")
        sheet_name = self._sheet_combo.currentText().strip()
        if not sheet_name:
            raise ValueError("请选择 Sheet。")
        browser_config_id = self._browser_combo.currentData()
        if browser_config_id is None:
            raise ValueError("请选择浏览器配置。")
        url_field = self._url_field_combo.currentText().strip()
        if not url_field:
            raise ValueError("URL 列名不能为空。")

        return TaskSnapshot(
            sheet_name=sheet_name,
            header_row=int(self._header_row_spin.value()),
            browser_config_id=str(browser_config_id),
            open_tab_count=int(self._open_tab_spin.value()),
            confirm_url=self._confirm_url_edit.text().strip() or None,
            url_field=url_field,
            display_fields=_split_csv(self._display_fields_edit.text()),
            review_fields=_collect_review_fields(self._review_fields_table),
            shortcuts=ReviewShortcutConfig(
                submit=_key_sequence_text(self._submit_shortcut_edit),
                previous=_key_sequence_text(self._previous_shortcut_edit),
                exit=_key_sequence_text(self._exit_shortcut_edit),
            ),
            export_fields=_split_csv(self._export_fields_edit.text()),
        )

    def _update_warnings(self, warnings: list[str]) -> None:
        self._warnings_label.setText(
            "\n".join(warnings) if warnings else "创建前校验将显示在这里。"
        )


def _warning_lines(warnings) -> list[str]:
    lines: list[str] = []
    if warnings.uses_first_task_url_as_confirmation:
        lines.append("确认页面为空，将使用第一条任务 URL 作为确认页面。")
    if warnings.missing_review_export_fields:
        lines.append(
            "导出字段未包含以下检查结果列：" + ", ".join(warnings.missing_review_export_fields)
        )
    if warnings.overlapping_export_review_fields:
        lines.append(
            "以下检查结果列与 Excel 原列同名，导出时检查结果将覆盖原值："
            + ", ".join(warnings.overlapping_export_review_fields)
        )
    return lines


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _key_sequence_text(editor: QKeySequenceEdit) -> str:
    text = editor.keySequence().toString(QKeySequence.SequenceFormat.NativeText).strip()
    if not text:
        raise ValueError("快捷键不能为空。")
    return text


def _setup_table(table: QTableWidget) -> None:
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)


def _add_empty_review_row(table: QTableWidget) -> None:
    row = table.rowCount()
    table.insertRow(row)
    for column in (0, 1):
        table.setItem(row, column, QTableWidgetItem(""))
    type_combo = QComboBox()
    for label, value in REVIEW_FIELD_TYPE_OPTIONS:
        type_combo.addItem(label, value)
    table.setCellWidget(row, 2, type_combo)
    required_checkbox = QCheckBox()
    table.setCellWidget(row, 3, required_checkbox)
    _set_options_button(table, row, [])


def _set_options_button(table: QTableWidget, row: int, options: list[ReviewOption]) -> None:
    button = QPushButton(_options_summary(options))
    button.setProperty("options", [deepcopy(option) for option in options])
    button.clicked.connect(lambda _checked=False, current=button: _edit_options(current))
    table.setCellWidget(row, 4, button)


def _edit_options(button: QPushButton) -> None:
    raw_options = button.property("options")
    options = raw_options if isinstance(raw_options, list) else []
    dialog = ReviewOptionsDialog(
        [option for option in options if isinstance(option, ReviewOption)],
        button,
    )
    if dialog.exec() != int(QDialog.DialogCode.Accepted):
        return
    button.setProperty("options", [deepcopy(option) for option in dialog.options])
    button.setText(_options_summary(dialog.options))


def _options_summary(options: list[ReviewOption]) -> str:
    return f"编辑选项 ({len(options)})" if options else "编辑选项..."


def _collect_review_fields(table: QTableWidget) -> list[ReviewField]:
    fields: list[ReviewField] = []
    shortcuts: set[str] = set()
    for row in range(table.rowCount()):
        field_id = _table_text(table, row, 0)
        label = _table_text(table, row, 1)
        field_type = _combo_data(table, row, 2) or "single_select"
        required = _checkbox_value(table, row, 3)
        options = _options_value(table, row, 4)
        if not field_id and not label:
            continue
        if not field_id or not label:
            raise ValueError(f"检查项第 {row + 1} 行需要结果列名和问题标题。")
        if field_type in {"single_select", "multi_select"} and not options:
            raise ValueError(f"检查项 {label} 需要至少一个选项。")
        if field_type in {"text", "boolean"}:
            options = []
        for option in options:
            if option.shortcut:
                normalized = option.shortcut.casefold()
                if normalized in shortcuts:
                    raise ValueError(f"选项快捷键冲突：{option.shortcut}")
                shortcuts.add(normalized)
        fields.append(
            ReviewField(
                field_id=field_id,
                label=label,
                field_type=str(field_type),  # type: ignore[arg-type]
                required=required,
                options=options,
            )
        )
    return fields


def _fill_options_table(table: QTableWidget, options: list[ReviewOption]) -> None:
    table.setRowCount(len(options))
    for row, option in enumerate(options):
        _add_empty_option_row(table, row=row)
        table.setItem(row, 0, QTableWidgetItem(option.value))
        table.setItem(row, 1, QTableWidgetItem(option.label))
        editor = table.cellWidget(row, 2)
        if isinstance(editor, QKeySequenceEdit) and option.shortcut:
            editor.setKeySequence(QKeySequence(option.shortcut))


def _fill_review_fields_table(table: QTableWidget, fields: list[ReviewField]) -> None:
    table.setRowCount(0)
    if not fields:
        _add_empty_review_row(table)
        return
    for field in fields:
        _add_empty_review_row(table)
        row = table.rowCount() - 1
        table.item(row, 0).setText(field.field_id)
        table.item(row, 1).setText(field.label)
        type_combo = table.cellWidget(row, 2)
        if isinstance(type_combo, QComboBox):
            combo_index = type_combo.findData(field.field_type)
            if combo_index >= 0:
                type_combo.setCurrentIndex(combo_index)
        required_checkbox = table.cellWidget(row, 3)
        if isinstance(required_checkbox, QCheckBox):
            required_checkbox.setChecked(field.required)
        _set_options_button(table, row, field.options)


def _add_empty_option_row(table: QTableWidget, row: int | None = None) -> None:
    actual_row = table.rowCount() if row is None else row
    if row is None:
        table.insertRow(actual_row)
    else:
        table.setRowCount(max(table.rowCount(), actual_row + 1))
    for column in (0, 1):
        if table.item(actual_row, column) is None:
            table.setItem(actual_row, column, QTableWidgetItem(""))
    if table.cellWidget(actual_row, 2) is None:
        table.setCellWidget(actual_row, 2, QKeySequenceEdit())


def _collect_options(table: QTableWidget) -> list[ReviewOption]:
    options: list[ReviewOption] = []
    seen_values: set[str] = set()
    seen_shortcuts: set[str] = set()
    for row in range(table.rowCount()):
        value = _table_text(table, row, 0)
        label = _table_text(table, row, 1)
        shortcut = _key_editor_value(table, row, 2)
        if not value and not label and not shortcut:
            continue
        if not value or not label:
            raise ValueError(f"选项第 {row + 1} 行需要值和显示名。")
        if value in seen_values:
            raise ValueError(f"选项值重复：{value}")
        if shortcut:
            normalized = shortcut.casefold()
            if normalized in seen_shortcuts:
                raise ValueError(f"选项快捷键重复：{shortcut}")
            seen_shortcuts.add(normalized)
        seen_values.add(value)
        options.append(ReviewOption(value=value, label=label, shortcut=shortcut or None))
    return options


def _remove_selected_row(table: QTableWidget) -> None:
    row = table.currentRow()
    if row >= 0:
        table.removeRow(row)


def _move_selected_row(table: QTableWidget, direction: int) -> None:
    row = table.currentRow()
    target = row + direction
    if row < 0 or target < 0 or target >= table.rowCount():
        return
    values = [_cell_value(table, row, column) for column in range(table.columnCount())]
    target_values = [_cell_value(table, target, column) for column in range(table.columnCount())]
    for column, value in enumerate(target_values):
        _set_cell_value(table, row, column, value)
    for column, value in enumerate(values):
        _set_cell_value(table, target, column, value)
    table.setCurrentCell(target, 0)


def _table_text(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    return item.text().strip() if item else ""


def _combo_data(table: QTableWidget, row: int, column: int) -> str | None:
    widget = table.cellWidget(row, column)
    if isinstance(widget, QComboBox):
        data = widget.currentData()
        return str(data) if data is not None else None
    return None


def _checkbox_value(table: QTableWidget, row: int, column: int) -> bool:
    widget = table.cellWidget(row, column)
    return bool(widget.isChecked()) if isinstance(widget, QCheckBox) else False


def _options_value(table: QTableWidget, row: int, column: int) -> list[ReviewOption]:
    widget = table.cellWidget(row, column)
    if not isinstance(widget, QPushButton):
        return []
    raw_options = widget.property("options")
    if not isinstance(raw_options, list):
        return []
    return [deepcopy(option) for option in raw_options if isinstance(option, ReviewOption)]


def _key_editor_value(table: QTableWidget, row: int, column: int) -> str:
    widget = table.cellWidget(row, column)
    if not isinstance(widget, QKeySequenceEdit):
        return ""
    return widget.keySequence().toString(QKeySequence.SequenceFormat.NativeText).strip()


def _cell_value(table: QTableWidget, row: int, column: int) -> object:
    widget = table.cellWidget(row, column)
    if isinstance(widget, QComboBox):
        return widget.currentData()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QPushButton):
        return _options_value(table, row, column)
    if isinstance(widget, QKeySequenceEdit):
        return widget.keySequence().toString(QKeySequence.SequenceFormat.NativeText).strip()
    return _table_text(table, row, column)


def _set_cell_value(table: QTableWidget, row: int, column: int, value: object) -> None:
    widget = table.cellWidget(row, column)
    if isinstance(widget, QComboBox):
        index = widget.findData(value)
        widget.setCurrentIndex(index if index >= 0 else 0)
        return
    if isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
        return
    if isinstance(widget, QPushButton):
        options = value if isinstance(value, list) else []
        _set_options_button(
            table,
            row,
            [item for item in options if isinstance(item, ReviewOption)],
        )
        return
    if isinstance(widget, QKeySequenceEdit):
        widget.setKeySequence(QKeySequence(str(value or "")))
        return
    table.setItem(row, column, QTableWidgetItem(str(value or "")))
