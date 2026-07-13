from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence
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
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
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

ROW_ORIGIN_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_TASK_OWNED_ROLE = Qt.ItemDataRole.UserRole + 2
ROW_IS_UNIQUE_ROLE = Qt.ItemDataRole.UserRole + 3
FORM_FIELD_FULL_WIDTH = 420
FORM_FIELD_COMPACT_WIDTH = 140
MAX_AUTO_EXPORT_FIELDS = 50


@dataclass(slots=True)
class ReviewFieldRowState:
    field: ReviewField
    enabled: bool
    origin: str
    task_owned: bool


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
        review_field_id_editable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.source_path: Path | None = source_path
        self.task_snapshot: TaskSnapshot | None = None
        self.review_field_library: list[ReviewField] = []
        self._browser_configs = browser_configs
        self._app_service = app_service
        self._available_headers: list[str] = []
        self._accept_button_text = accept_button_text
        self._source_editable = source_editable
        self._review_field_id_editable = review_field_id_editable
        self._global_review_fields = self._app_service.load_review_field_library()
        self._suspend_global_sync = False
        self._suspend_export_field_tracking = False
        self._export_fields_auto_mode = initial_snapshot is None and source_editable
        self._initial_task_field_ids: set[str] = (
            set(field.field_id for field in initial_snapshot.review_fields)
            if initial_snapshot is not None and not source_editable
            else set()
        )

        self.setWindowTitle(dialog_title)
        self.resize(980, 760)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._source_edit = QLineEdit()
        self._source_edit.setReadOnly(True)
        source_button = QPushButton("浏览...")
        source_button.clicked.connect(self._browse_source)
        source_button.setEnabled(source_editable)
        source_widget = self._build_compound_field(
            self._source_edit,
            source_button,
        )

        self._sheet_combo = QComboBox()
        self._sheet_combo.currentIndexChanged.connect(self._reload_headers)
        self._header_row_spin = QSpinBox()
        self._header_row_spin.setRange(1, 10000)
        self._header_row_spin.setValue(1)
        self._header_row_spin.valueChanged.connect(self._reload_headers)
        self._browser_combo = QComboBox()
        for config in self._browser_configs:
            self._browser_combo.addItem(config.name, config.config_id)
        self._open_tab_spin = QSpinBox()
        self._open_tab_spin.setRange(1, 10)
        self._open_tab_spin.setValue(3)
        self._confirm_url_edit = QLineEdit()
        self._url_field_combo = QComboBox()
        self._url_field_combo.setEditable(True)
        self._display_fields_edit = QTextEdit()
        self._display_fields_edit.setAcceptRichText(False)
        self._display_fields_edit.setFixedHeight(72)
        self._export_fields_edit = QTextEdit()
        self._export_fields_edit.setAcceptRichText(False)
        self._export_fields_edit.setFixedHeight(72)
        self._export_fields_edit.textChanged.connect(self._handle_export_fields_changed)

        form.addRow("Excel 文件", source_widget)
        form.addRow("确认页 URL", self._expanding_form_field(self._confirm_url_edit))
        form.addRow(
            "浏览器配置",
            self._bounded_form_field(self._browser_combo, FORM_FIELD_COMPACT_WIDTH),
        )
        form.addRow(
            "同时打开标签",
            self._bounded_form_field(self._open_tab_spin, FORM_FIELD_COMPACT_WIDTH),
        )
        form.addRow(
            "Sheet",
            self._bounded_form_field(self._sheet_combo, FORM_FIELD_COMPACT_WIDTH),
        )
        form.addRow(
            "标题行",
            self._bounded_form_field(self._header_row_spin, FORM_FIELD_COMPACT_WIDTH),
        )
        form.addRow(
            "URL 列名",
            self._bounded_form_field(self._url_field_combo, FORM_FIELD_COMPACT_WIDTH),
        )
        form.addRow("展示字段", self._expanding_form_field(self._display_fields_edit))
        form.addRow("导出字段", self._expanding_form_field(self._export_fields_edit))
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
        self._review_fields_table.setColumnCount(6)
        self._review_fields_table.setHorizontalHeaderLabels(
            ["启用", "结果列名", "问题标题", "类型", "必填", "选项"]
        )
        _setup_table(self._review_fields_table)
        self._review_fields_table.cellClicked.connect(self._handle_review_field_cell_clicked)
        self._review_fields_table.itemChanged.connect(self._handle_review_field_item_changed)
        root.addWidget(self._review_fields_table, stretch=1)

        table_actions = QHBoxLayout()
        table_actions.addStretch(1)
        add_review_button = QPushButton("添加检查项")
        remove_review_button = QPushButton("删除检查项")
        up_review_button = QPushButton("上移")
        down_review_button = QPushButton("下移")
        add_review_button.clicked.connect(self._add_review_row)
        remove_review_button.clicked.connect(self._remove_review_row)
        up_review_button.clicked.connect(lambda: self._move_review_row(-1))
        down_review_button.clicked.connect(lambda: self._move_review_row(1))
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

    def _build_compound_field(self, primary: QWidget, secondary: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(primary, stretch=1)
        layout.addWidget(secondary)
        return container

    def _bounded_form_field(self, widget: QWidget, width: int) -> QWidget:
        widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        widget.setFixedWidth(width)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _expanding_form_field(self, widget: QWidget) -> QWidget:
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return widget

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
            rows = [
                ReviewFieldRowState(
                    field=deepcopy(field),
                    enabled=True,
                    origin="global",
                    task_owned=False,
                )
                for field in self._global_review_fields
            ]
            self._render_review_field_rows(rows)
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
        self._export_fields_auto_mode = False
        self._set_combo_text(self._sheet_combo, snapshot.sheet_name)
        self._header_row_spin.setValue(snapshot.header_row)
        self._set_browser_config(snapshot.browser_config_id)
        self._open_tab_spin.setValue(snapshot.open_tab_count)
        self._confirm_url_edit.setText(snapshot.confirm_url or "")
        self._reload_headers()
        self._url_field_combo.setCurrentText(snapshot.url_field)
        self._display_fields_edit.setPlainText(", ".join(snapshot.display_fields))
        self._export_fields_edit.setPlainText(", ".join(snapshot.export_fields))
        self._submit_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.submit))
        self._previous_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.previous))
        self._exit_shortcut_edit.setKeySequence(QKeySequence(snapshot.shortcuts.exit))
        if self._source_editable:
            self._render_review_field_rows(self._build_new_task_review_field_rows(snapshot))
        else:
            self._render_review_field_rows(self._build_review_field_rows(snapshot))

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
        self._apply_auto_export_fields(headers)

    def _handle_export_fields_changed(self) -> None:
        if self._suspend_export_field_tracking:
            return
        self._export_fields_auto_mode = False

    def _apply_auto_export_fields(self, headers: list[str]) -> None:
        if not self._export_fields_auto_mode:
            return
        export_fields = headers[:MAX_AUTO_EXPORT_FIELDS]
        self._suspend_export_field_tracking = True
        try:
            self._export_fields_edit.setPlainText(", ".join(export_fields))
        finally:
            self._suspend_export_field_tracking = False

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
        self.review_field_library = self._collect_global_review_fields()
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
            display_fields=_split_csv(self._display_fields_edit.toPlainText()),
            review_fields=_collect_review_fields(self._review_fields_table),
            enabled_review_field_ids=_collect_enabled_review_field_ids(self._review_fields_table),
            shortcuts=ReviewShortcutConfig(
                submit=_key_sequence_text(self._submit_shortcut_edit),
                previous=_key_sequence_text(self._previous_shortcut_edit),
                exit=_key_sequence_text(self._exit_shortcut_edit),
            ),
            export_fields=_split_csv(self._export_fields_edit.toPlainText()),
        )

    def _update_warnings(self, warnings: list[str]) -> None:
        self._warnings_label.setText("\n".join(warnings))

    def _handle_review_field_item_changed(self, _item: QTableWidgetItem) -> None:
        self._sync_global_review_field_library()

    def _handle_review_field_cell_clicked(self, row: int, column: int) -> None:
        if self._row_origin(row) == "unique" and column != 0:
            if self._prompt_add_unique_field_to_global(row):
                self._configure_review_fields_table()
            return
        if column != 5:
            return
        try:
            _edit_options_for_row(self._review_fields_table, row)
        except ValueError as exc:
            QMessageBox.warning(self, "选项无效", str(exc))
            return
        self._sync_global_review_field_library()

    def _add_review_row(self) -> None:
        _add_empty_review_row(self._review_fields_table)
        row = self._review_fields_table.rowCount() - 1
        self._set_row_metadata(row, origin="global", task_owned=True)
        self._bind_review_row_signals(row)
        self._configure_review_fields_table()

    def _remove_review_row(self) -> None:
        row = self._review_fields_table.currentRow()
        if row < 0:
            return
        origin = self._row_origin(row)
        if origin == "unique":
            QMessageBox.information(
                self,
                "无法删除",
                "独特检查项不能直接删除。可取消勾选，或点击后添加回全局配置。",
            )
            return
        field_id = _table_text(self._review_fields_table, row, 1)
        if not field_id:
            self._review_fields_table.removeRow(row)
            self._configure_review_fields_table()
            return
        result = QMessageBox.question(
            self,
            "确认删除",
            f"将从全局配置删除检查项 `{field_id}`。\n"
            "已存在于任务中的同名检查项会保留为独特检查项。\n\n是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        updated_library = [
            field for field in self._collect_global_review_fields() if field.field_id != field_id
        ]
        self._app_service.save_review_field_library(updated_library)
        self._global_review_fields = self._app_service.load_review_field_library()
        if self._row_task_owned(row) and field_id in self._initial_task_field_ids:
            self._set_row_metadata(row, origin="unique", task_owned=True)
            self._configure_review_fields_table()
            return
        self._review_fields_table.removeRow(row)
        self._configure_review_fields_table()

    def _move_review_row(self, direction: int) -> None:
        _move_selected_row(self._review_fields_table, direction)
        self._configure_review_fields_table()
        self._sync_global_review_field_library()

    def _build_review_field_rows(self, snapshot: TaskSnapshot) -> list[ReviewFieldRowState]:
        global_map = {field.field_id: deepcopy(field) for field in self._global_review_fields}
        rows: list[ReviewFieldRowState] = []
        seen_ids: set[str] = set()
        enabled_ids = set(snapshot.enabled_review_field_ids)
        for field in snapshot.review_fields:
            global_field = global_map.get(field.field_id)
            if global_field is not None:
                rows.append(
                    ReviewFieldRowState(
                        field=global_field,
                        enabled=field.field_id in enabled_ids,
                        origin="global",
                        task_owned=not self._source_editable,
                    )
                )
            else:
                rows.append(
                    ReviewFieldRowState(
                        field=deepcopy(field),
                        enabled=field.field_id in enabled_ids,
                        origin="unique",
                        task_owned=True,
                    )
                )
            seen_ids.add(field.field_id)
        for field in self._global_review_fields:
            if field.field_id in seen_ids:
                continue
            rows.append(
                ReviewFieldRowState(
                    field=deepcopy(field),
                    enabled=False,
                    origin="global",
                    task_owned=False,
                )
            )
        return rows

    def _build_new_task_review_field_rows(
        self,
        snapshot: TaskSnapshot,
    ) -> list[ReviewFieldRowState]:
        enabled_ids = set(snapshot.enabled_review_field_ids)
        return [
            ReviewFieldRowState(
                field=deepcopy(field),
                enabled=field.field_id in enabled_ids if enabled_ids else True,
                origin="global",
                task_owned=False,
            )
            for field in self._global_review_fields
        ]

    def _render_review_field_rows(self, rows: list[ReviewFieldRowState]) -> None:
        self._review_fields_table.setRowCount(0)
        if not rows:
            return
        for row_state in rows:
            _add_empty_review_row(self._review_fields_table)
            row = self._review_fields_table.rowCount() - 1
            enabled_checkbox = self._review_fields_table.cellWidget(row, 0)
            if isinstance(enabled_checkbox, QCheckBox):
                enabled_checkbox.setChecked(row_state.enabled)
            self._review_fields_table.item(row, 1).setText(row_state.field.field_id)
            self._review_fields_table.item(row, 2).setText(row_state.field.label)
            type_combo = self._review_fields_table.cellWidget(row, 3)
            if isinstance(type_combo, QComboBox):
                combo_index = type_combo.findData(row_state.field.field_type)
                if combo_index >= 0:
                    type_combo.setCurrentIndex(combo_index)
            required_checkbox = self._review_fields_table.cellWidget(row, 4)
            if isinstance(required_checkbox, QCheckBox):
                required_checkbox.setChecked(row_state.field.required)
            _set_options_item(self._review_fields_table, row, row_state.field.options)
            self._set_row_metadata(
                row,
                origin=row_state.origin,
                task_owned=row_state.task_owned,
            )
            self._bind_review_row_signals(row)
        self._configure_review_fields_table()

    def _collect_global_review_fields(self) -> list[ReviewField]:
        global_fields: list[ReviewField] = []
        for row in range(self._review_fields_table.rowCount()):
            if self._row_origin(row) != "global":
                continue
            field = _collect_review_field_at_row(self._review_fields_table, row)
            if field is not None:
                global_fields.append(field)
        return global_fields

    def _set_row_metadata(self, row: int, *, origin: str, task_owned: bool) -> None:
        for column in (1, 2, 5):
            item = self._review_fields_table.item(row, column)
            if item is None:
                item = QTableWidgetItem("")
                self._review_fields_table.setItem(row, column, item)
            item.setData(ROW_ORIGIN_ROLE, origin)
            item.setData(ROW_TASK_OWNED_ROLE, task_owned)
            item.setData(ROW_IS_UNIQUE_ROLE, origin == "unique")

    def _row_origin(self, row: int) -> str:
        item = self._review_fields_table.item(row, 1)
        if item is None:
            return "global"
        value = item.data(ROW_ORIGIN_ROLE)
        return str(value) if value else "global"

    def _row_task_owned(self, row: int) -> bool:
        item = self._review_fields_table.item(row, 1)
        if item is None:
            return True
        return bool(item.data(ROW_TASK_OWNED_ROLE))

    def _prompt_add_unique_field_to_global(self, row: int) -> bool:
        field = _collect_review_field_at_row(self._review_fields_table, row)
        if field is None:
            return False
        existing_ids = {
            existing_field.field_id for existing_field in self._collect_global_review_fields()
        }
        if field.field_id in existing_ids:
            QMessageBox.warning(
                self,
                "无法添加",
                f"全局配置中已存在同名结果列：{field.field_id}",
            )
            return False
        result = QMessageBox.question(
            self,
            "添加检查项",
            "此检查项当前仅存在于该任务中。\n\n是否添加此检查项到全局配置？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return False
        updated_library = [*self._collect_global_review_fields(), field]
        try:
            self._app_service.save_review_field_library(updated_library)
        except ValueError as exc:
            QMessageBox.warning(self, "无法添加", str(exc))
            return False
        self._global_review_fields = self._app_service.load_review_field_library()
        self._set_row_metadata(row, origin="global", task_owned=True)
        return True

    def _configure_review_fields_table(self) -> None:
        self._suspend_global_sync = True
        _configure_review_fields_table(
            self._review_fields_table,
            field_id_editable=True,
        )
        self._suspend_global_sync = False

    def _bind_review_row_signals(self, row: int) -> None:
        type_combo = self._review_fields_table.cellWidget(row, 3)
        required_checkbox = self._review_fields_table.cellWidget(row, 4)
        if isinstance(type_combo, QComboBox):
            try:
                type_combo.currentIndexChanged.disconnect(self._sync_global_review_field_library)
            except (RuntimeError, TypeError):
                pass
            type_combo.currentIndexChanged.connect(self._sync_global_review_field_library)
        if isinstance(required_checkbox, QCheckBox):
            try:
                required_checkbox.checkStateChanged.disconnect(
                    self._sync_global_review_field_library
                )
            except (RuntimeError, TypeError):
                pass
            required_checkbox.checkStateChanged.connect(self._sync_global_review_field_library)

    def _sync_global_review_field_library(self) -> None:
        if self._suspend_global_sync:
            return
        try:
            global_fields = self._collect_global_review_fields()
        except ValueError:
            return
        current_ids = [field.field_id for field in self._global_review_fields]
        next_ids = [field.field_id for field in global_fields]
        if current_ids == next_ids and self._global_review_fields == global_fields:
            return
        self._app_service.save_review_field_library(global_fields)
        self._global_review_fields = self._app_service.load_review_field_library()


def _warning_lines(warnings) -> list[str]:
    lines: list[str] = []
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
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setStyleSheet(
        "QTableView { outline: 0; } "
        "QTableView::item:selected { "
        "border: 0px; "
        "background-color: #2563eb; "
        "color: #ffffff; "
        "}"
    )
    table.horizontalHeader().setStretchLastSection(False)


def _add_empty_review_row(table: QTableWidget) -> None:
    row = table.rowCount()
    table.insertRow(row)
    enabled_checkbox = QCheckBox()
    enabled_checkbox.setChecked(True)
    table.setCellWidget(row, 0, enabled_checkbox)
    for column in (1, 2):
        table.setItem(row, column, QTableWidgetItem(""))
    type_combo = QComboBox()
    for label, value in REVIEW_FIELD_TYPE_OPTIONS:
        type_combo.addItem(label, value)
    table.setCellWidget(row, 3, type_combo)
    required_checkbox = QCheckBox()
    table.setCellWidget(row, 4, required_checkbox)
    _set_options_item(table, row, [])
    _configure_review_fields_table(table)


def _set_options_item(table: QTableWidget, row: int, options: list[ReviewOption]) -> None:
    summary_item = table.item(row, 5)
    if summary_item is None:
        summary_item = QTableWidgetItem()
        table.setItem(row, 5, summary_item)
    summary_item.setText(_options_preview(options))
    summary_item.setToolTip(_options_tooltip(options))
    summary_item.setData(Qt.ItemDataRole.UserRole, list(options))
    table.setRowHeight(row, max(table.rowHeight(row), 34))


def _edit_options_for_row(table: QTableWidget, row: int) -> None:
    field_type = _combo_data(table, row, 3) or "single_select"
    if field_type not in {"single_select", "multi_select"}:
        return
    existing_options = _options_value(table, row, 5)
    dialog = ReviewOptionsDialog(existing_options, table)
    if dialog.exec() != int(QDialog.DialogCode.Accepted):
        return
    _set_options_item(table, row, dialog.options)


def _options_preview(options: list[ReviewOption]) -> str:
    if not options:
        return "未配置选项"
    parts: list[str] = []
    for option in options:
        label = option.label or option.value
        parts.append(f"{label}({option.shortcut})" if option.shortcut else label)
    return " / ".join(parts)


def _options_tooltip(options: list[ReviewOption]) -> str:
    if not options:
        return "未配置选项"
    return "\n".join(
        f"{option.value} | {option.label}" + (f" | {option.shortcut}" if option.shortcut else "")
        for option in options
    )


def _collect_review_fields(table: QTableWidget) -> list[ReviewField]:
    fields: list[ReviewField] = []
    shortcuts: set[str] = set()
    for row in range(table.rowCount()):
        field = _collect_review_field_at_row(table, row)
        if field is None:
            continue
        for option in field.options:
            if option.shortcut:
                normalized = option.shortcut.casefold()
                if normalized in shortcuts:
                    raise ValueError(f"选项快捷键冲突：{option.shortcut}")
                shortcuts.add(normalized)
        fields.append(field)
    return fields


def _collect_review_field_at_row(table: QTableWidget, row: int) -> ReviewField | None:
    field_id = _table_text(table, row, 1)
    label = _table_text(table, row, 2)
    field_type = _combo_data(table, row, 3) or "single_select"
    required = _checkbox_value(table, row, 4)
    options = _options_value(table, row, 5)
    if not field_id and not label:
        return None
    if not field_id or not label:
        raise ValueError(f"检查项第 {row + 1} 行需要结果列名和问题标题。")
    if field_type in {"single_select", "multi_select"} and not options:
        raise ValueError(f"检查项 {label} 需要至少一个选项。")
    if field_type in {"text", "boolean"}:
        options = []
    return ReviewField(
        field_id=field_id,
        label=label,
        field_type=str(field_type),  # type: ignore[arg-type]
        required=required,
        options=options,
    )


def _collect_enabled_review_field_ids(table: QTableWidget) -> list[str]:
    enabled_ids: list[str] = []
    for row in range(table.rowCount()):
        field_id = _table_text(table, row, 1)
        label = _table_text(table, row, 2)
        if not field_id and not label:
            continue
        if _checkbox_value(table, row, 0):
            enabled_ids.append(field_id)
    return enabled_ids


def _fill_options_table(table: QTableWidget, options: list[ReviewOption]) -> None:
    table.setRowCount(len(options))
    for row, option in enumerate(options):
        _add_empty_option_row(table, row=row)
        table.setItem(row, 0, QTableWidgetItem(option.value))
        table.setItem(row, 1, QTableWidgetItem(option.label))
        editor = table.cellWidget(row, 2)
        if isinstance(editor, QKeySequenceEdit) and option.shortcut:
            editor.setKeySequence(QKeySequence(option.shortcut))


def _fill_review_fields_table(
    table: QTableWidget,
    fields: list[ReviewField],
    *,
    enabled_review_field_ids: list[str],
    field_id_editable: bool,
) -> None:
    table.setRowCount(0)
    if not fields:
        _add_empty_review_row(table)
        _configure_review_fields_table(table, field_id_editable=field_id_editable)
        return
    enabled_ids = set(enabled_review_field_ids)
    for field in fields:
        _add_empty_review_row(table)
        row = table.rowCount() - 1
        enabled_checkbox = table.cellWidget(row, 0)
        if isinstance(enabled_checkbox, QCheckBox):
            enabled_checkbox.setChecked(field.field_id in enabled_ids)
        table.item(row, 1).setText(field.field_id)
        table.item(row, 2).setText(field.label)
        type_combo = table.cellWidget(row, 3)
        if isinstance(type_combo, QComboBox):
            combo_index = type_combo.findData(field.field_type)
            if combo_index >= 0:
                type_combo.setCurrentIndex(combo_index)
        required_checkbox = table.cellWidget(row, 4)
        if isinstance(required_checkbox, QCheckBox):
            required_checkbox.setChecked(field.required)
        summary_item = table.item(row, 5)
        if summary_item is None:
            summary_item = QTableWidgetItem()
            table.setItem(row, 5, summary_item)
        summary_item.setText(_options_preview(field.options))
        summary_item.setToolTip(_options_tooltip(field.options))
        _set_options_item(table, row, field.options)
    _configure_review_fields_table(table, field_id_editable=field_id_editable)


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
    item = table.item(row, column)
    if item is None:
        return []
    raw_options = item.data(Qt.ItemDataRole.UserRole)
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
    if column == 1:
        item = table.item(row, column)
        return {
            "text": _table_text(table, row, column),
            "origin": item.data(ROW_ORIGIN_ROLE) if item is not None else "global",
            "task_owned": bool(item.data(ROW_TASK_OWNED_ROLE)) if item is not None else True,
        }
    if column == 5:
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
    if column == 1 and isinstance(value, dict):
        item = QTableWidgetItem(str(value.get("text") or ""))
        table.setItem(row, column, item)
        origin = str(value.get("origin") or "global")
        item.setData(ROW_ORIGIN_ROLE, origin)
        item.setData(ROW_TASK_OWNED_ROLE, bool(value.get("task_owned")))
        item.setData(ROW_IS_UNIQUE_ROLE, origin == "unique")
        return
    if column == 5:
        options = value if isinstance(value, list) else []
        _set_options_item(
            table,
            row,
            [item for item in options if isinstance(item, ReviewOption)],
        )
        return
    if isinstance(widget, QKeySequenceEdit):
        widget.setKeySequence(QKeySequence(str(value or "")))
        return
    table.setItem(row, column, QTableWidgetItem(str(value or "")))


def _configure_review_fields_table(table: QTableWidget, *, field_id_editable: bool = True) -> None:
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
    table.setColumnWidth(0, 58)
    table.setColumnWidth(1, 150)
    table.setColumnWidth(3, 78)
    table.setColumnWidth(4, 62)
    table.setColumnWidth(5, 360)
    for row in range(table.rowCount()):
        origin_item = table.item(row, 1)
        is_unique = bool(origin_item.data(ROW_IS_UNIQUE_ROLE)) if origin_item is not None else False
        field_item = table.item(row, 1)
        if field_item is not None:
            flags = field_item.flags()
            if (field_id_editable or not field_item.text().strip()) and not is_unique:
                field_item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
            else:
                field_item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
        label_item = table.item(row, 2)
        if label_item is not None:
            flags = label_item.flags()
            if is_unique:
                label_item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
            else:
                label_item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
        option_item = table.item(row, 5)
        if option_item is not None:
            unique_color = QColor("#9ca3af")
            default_color = table.palette().text().color()
            color = unique_color if is_unique else default_color
            for item in (field_item, label_item, option_item):
                if item is not None:
                    item.setForeground(color)
        type_combo = table.cellWidget(row, 3)
        required_checkbox = table.cellWidget(row, 4)
        if isinstance(type_combo, QComboBox):
            type_combo.setEnabled(not is_unique)
        if isinstance(required_checkbox, QCheckBox):
            required_checkbox.setEnabled(not is_unique)
