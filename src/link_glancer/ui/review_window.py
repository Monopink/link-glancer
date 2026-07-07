from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from link_glancer.application import TaskApplicationService
from link_glancer.browser.base import BrowserController, BufferBlock
from link_glancer.tasks.models import TaskDetail, TaskSnapshot
from link_glancer.ui.review_form import ReviewFieldWidget, create_field_widget


class ShortcutConfigDialog(QDialog):
    def __init__(self, task: TaskDetail, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("快捷键")
        self.setModal(True)
        self.resize(420, 180)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self._submit_shortcut_edit = QKeySequenceEdit()
        self._previous_shortcut_edit = QKeySequenceEdit()
        self._exit_shortcut_edit = QKeySequenceEdit()

        shortcuts = task.task_snapshot.shortcuts
        self._submit_shortcut_edit.setKeySequence(QKeySequence(shortcuts.submit))
        self._previous_shortcut_edit.setKeySequence(QKeySequence(shortcuts.previous))
        self._exit_shortcut_edit.setKeySequence(QKeySequence(shortcuts.exit))

        root.addWidget(self._row("提交", self._submit_shortcut_edit))
        root.addWidget(self._row("上一条", self._previous_shortcut_edit))
        root.addWidget(self._row("退出", self._exit_shortcut_edit))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return (
            self._key_sequence_text(self._submit_shortcut_edit),
            self._key_sequence_text(self._previous_shortcut_edit),
            self._key_sequence_text(self._exit_shortcut_edit),
        )

    def _row(self, title: str, editor: QKeySequenceEdit) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(editor, stretch=1)
        return widget

    def _key_sequence_text(self, editor: QKeySequenceEdit) -> str:
        text = editor.keySequence().toString(QKeySequence.SequenceFormat.NativeText).strip()
        if not text:
            raise ValueError("快捷键不能为空。")
        return text


class ReviewWindow(QMainWindow):
    def __init__(
        self,
        *,
        task_id: int,
        app_service: TaskApplicationService,
        browser: BrowserController,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._app_service = app_service
        self._browser = browser
        self._on_close = on_close
        self.task: TaskDetail = self._app_service.load_task(task_id)
        self._field_widgets: dict[str, ReviewFieldWidget] = {}
        self._dynamic_shortcuts: list[QShortcut] = []
        self._review_started_at = datetime.now(UTC)
        self._review_started_completed = self.task.completed_items
        self._handling_browser_block = False

        self.setWindowTitle("检查")
        self.resize(660, 660)
        self.setMinimumWidth(560)

        self._build_ui()
        self._load_window_preferences()
        self._render_review_fields()
        self._bind_shortcuts()
        self._refresh_view()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_time_labels)
        self._timer.start()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        self._progress_label = QLabel("0 / 0")
        self._completion_label = QLabel("-")
        self._remaining_label = QLabel("-")
        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        top_row.addWidget(self._summary("进度", self._progress_label))
        top_row.addWidget(self._summary("完成", self._completion_label))
        top_row.addWidget(self._summary("剩余", self._remaining_label))
        layout.addLayout(top_row)
        layout.addWidget(self._progress_bar)

        self._item_details_label = QLabel("-")
        self._item_details_label.setWordWrap(True)
        self._item_details_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._item_details_label)

        self._fields_container = QWidget()
        self._fields_layout = QVBoxLayout(self._fields_container)
        self._fields_layout.setContentsMargins(0, 0, 0, 0)
        self._fields_layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._fields_container)
        layout.addWidget(scroll, stretch=1)

        actions = QHBoxLayout()
        self._shortcut_button = QPushButton("快捷键")
        self._shortcut_button.clicked.connect(self._edit_public_shortcuts)
        self._always_on_top_button = QPushButton("窗口置顶")
        self._always_on_top_button.setCheckable(True)
        self._always_on_top_button.clicked.connect(self._toggle_always_on_top)
        self._exit_button = QPushButton("退出检查")
        self._exit_button.clicked.connect(self.close)
        self._previous_button = QPushButton("上一条")
        self._previous_button.clicked.connect(self._go_to_previous)
        self._submit_button = QPushButton("提交")
        self._submit_button.clicked.connect(self._submit_review)
        actions.addWidget(self._shortcut_button)
        actions.addWidget(self._always_on_top_button)
        actions.addWidget(self._exit_button)
        actions.addStretch(1)
        actions.addWidget(self._previous_button)
        actions.addWidget(self._submit_button)
        layout.addLayout(actions)

        self.setCentralWidget(root)

    def _summary(self, title: str, value: QLabel) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(f"{title}:"))
        layout.addWidget(value)
        return widget

    def _render_review_fields(self, values: dict[str, object] | None = None) -> None:
        while self._fields_layout.count():
            item = self._fields_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._field_widgets.clear()
        for field in self.task.task_snapshot.review_fields:
            widget = create_field_widget(
                field,
                option_shortcut_handler=self._edit_option_shortcut,
            )
            if values is not None:
                widget.set_value(values.get(field.field_id))
            self._field_widgets[field.field_id] = widget
            self._fields_layout.addWidget(widget)
        self._fields_layout.addStretch(1)

    def _bind_shortcuts(self) -> None:
        for shortcut in self._dynamic_shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._dynamic_shortcuts.clear()

        shortcuts = self.task.task_snapshot.shortcuts
        submit_shortcut = QShortcut(QKeySequence(shortcuts.submit), self)
        submit_shortcut.activated.connect(self._submit_review)
        self._dynamic_shortcuts.append(submit_shortcut)

        previous_shortcut = QShortcut(QKeySequence(shortcuts.previous), self)
        previous_shortcut.activated.connect(self._go_to_previous)
        self._dynamic_shortcuts.append(previous_shortcut)

        exit_shortcut = QShortcut(QKeySequence(shortcuts.exit), self)
        exit_shortcut.activated.connect(self.close)
        self._dynamic_shortcuts.append(exit_shortcut)

        for field in self.task.task_snapshot.review_fields:
            for option in field.options:
                if not option.shortcut:
                    continue
                option_shortcut = QShortcut(QKeySequence(option.shortcut), self)
                option_shortcut.activated.connect(
                    lambda field_id=field.field_id, key=option.shortcut: (
                        self._activate_option_shortcut(field_id, key)
                    )
                )
                self._dynamic_shortcuts.append(option_shortcut)

    def _update_snapshot(self, task_snapshot: TaskSnapshot) -> None:
        current_values = {
            field_id: widget.value() for field_id, widget in self._field_widgets.items()
        }
        try:
            self.task = self._app_service.update_task_snapshot(
                task_id=self.task.task_id,
                task_snapshot=task_snapshot,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "快捷键无效", str(exc))
            return
        self._render_review_fields(current_values)
        self._bind_shortcuts()
        self._refresh_view()

    def _refresh_view(self) -> None:
        self.task = self._app_service.load_task(self.task.task_id)
        current_index = self.task.current_task_index
        completed = min(self.task.completed_items, self.task.total_items)
        self._progress_label.setText(f"{completed} / {self.task.total_items}")
        self._progress_bar.setMaximum(max(self.task.total_items, 1))
        self._progress_bar.setValue(completed)
        self._progress_bar.setFormat(self._progress_label.text())
        self._refresh_time_labels()
        self._item_details_label.setText(self._build_item_details(current_index))
        self._submit_button.setEnabled(self.task.current_item is not None)
        self._previous_button.setEnabled(self._has_previous_review())
        self._sync_form_with_current_item()

    def _refresh_time_labels(self) -> None:
        eta_seconds = self._review_time_metrics()
        self._completion_label.setText(self._format_completion_time(eta_seconds))
        self._remaining_label.setText(
            self._format_duration(eta_seconds) if eta_seconds is not None else "计算中"
        )

    def _build_item_details(self, task_index: int) -> str:
        item = self._app_service.load_item_at(task_id=self.task.task_id, task_index=task_index)
        if item is None:
            return "没有更多条目。"
        details: list[str] = []
        for field_name in self.task.task_snapshot.display_fields:
            if field_name in item.task_data:
                details.append(f"{field_name}：{item.task_data[field_name]}")
        return "\n".join(details) if details else "当前条目没有可展示字段。"

    def _review_time_metrics(self) -> int | None:
        elapsed = max(int((datetime.now(UTC) - self._review_started_at).total_seconds()), 0)
        completed_in_session = self.task.completed_items - self._review_started_completed
        remaining = max(self.task.total_items - self.task.completed_items, 0)
        if completed_in_session <= 0:
            return None
        eta = int(elapsed / completed_in_session * remaining)
        return eta

    def _format_completion_time(self, remaining_seconds: int | None) -> str:
        if remaining_seconds is None:
            return "计算中"
        estimated_at = datetime.now().astimezone() + timedelta(seconds=remaining_seconds)
        return estimated_at.strftime("%H:%M")

    def _format_duration(self, total_seconds: int) -> str:
        hours, remainder = divmod(max(total_seconds, 0), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}小时 {minutes}分"
        if minutes > 0:
            return f"{minutes}分 {seconds}秒"
        return f"{seconds}秒"

    def _collect_review_values(self) -> tuple[dict[str, object], list[str]]:
        values: dict[str, object] = {}
        errors: list[str] = []
        for field in self.task.task_snapshot.review_fields:
            widget = self._field_widgets.get(field.field_id)
            if widget is None:
                continue
            values[field.field_id] = widget.value()
            if not widget.is_complete():
                errors.append(field.label)
        return values, errors

    def _submit_review(self) -> None:
        target_index = self.task.current_task_index
        values, errors = self._collect_review_values()
        if errors:
            QMessageBox.warning(self, "缺少必填项", "请先完成：\n" + "\n".join(errors))
            return
        self.task = self._app_service.save_review(
            task_id=self.task.task_id,
            task_index=target_index,
            review_data=values,
            advance_pointer=True,
        )
        self._clear_review_form()
        if not self._sync_browser_buffer():
            return
        self._refresh_view()

    def _go_to_previous(self) -> None:
        target_index = self._app_service.find_previous_reviewed_index(
            task_id=self.task.task_id,
            before_task_index=self.task.current_task_index,
        )
        if target_index is None:
            return
        review = self._app_service.load_review(task_id=self.task.task_id, task_index=target_index)
        if review is None:
            return
        self.task = self._app_service.jump_to_task_index(
            task_id=self.task.task_id,
            task_index=target_index,
        )
        self._populate_review_form(review.review_data)
        if not self._sync_browser_buffer():
            return
        self._refresh_view()

    def _has_previous_review(self) -> bool:
        return (
            self._app_service.find_previous_reviewed_index(
                task_id=self.task.task_id,
                before_task_index=self.task.current_task_index,
            )
            is not None
        )

    def _sync_browser_buffer(self) -> bool:
        items = self._app_service.list_buffer_items(self.task)
        self._browser.sync_buffer(
            tasks=items,
            url_field=self.task.task_snapshot.url_field,
            current_task_index=self.task.current_task_index,
        )
        return self._handle_browser_buffer_block()

    def _handle_browser_buffer_block(self) -> bool:
        block = self._browser.buffer_block()
        if block is None or self._handling_browser_block:
            return True
        self._handling_browser_block = True
        try:
            while block is not None:
                result = QMessageBox.question(
                    self,
                    "页面处理",
                    self._format_browser_block_message(block),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if result != QMessageBox.StandardButton.Yes:
                    return False
                self._browser.resume_buffer()
                items = self._app_service.list_buffer_items(self.task)
                self._browser.sync_buffer(
                    tasks=items,
                    url_field=self.task.task_snapshot.url_field,
                    current_task_index=self.task.current_task_index,
                )
                block = self._browser.buffer_block()
            return True
        finally:
            self._handling_browser_block = False

    def _format_browser_block_message(self, block: BufferBlock) -> str:
        lines = [block.message]
        if block.task_index is not None:
            lines.append(f"条目序号：{block.task_index}")
        if block.url:
            lines.append(f"URL：{block.url}")
        lines.append("")
        lines.append("请在浏览器中处理后点击“继续”。")
        return "\n".join(lines)

    def _edit_public_shortcuts(self) -> None:
        dialog = ShortcutConfigDialog(self.task, self)
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        try:
            submit_shortcut, previous_shortcut, exit_shortcut = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "快捷键无效", str(exc))
            return
        snapshot = deepcopy(self.task.task_snapshot)
        snapshot.shortcuts.submit = submit_shortcut
        snapshot.shortcuts.previous = previous_shortcut
        snapshot.shortcuts.exit = exit_shortcut
        self._update_snapshot(snapshot)

    def _edit_option_shortcut(self, field_id: str, option_value: str) -> None:
        snapshot = deepcopy(self.task.task_snapshot)
        target_option = None
        target_label = option_value
        for field in snapshot.review_fields:
            if field.field_id != field_id:
                continue
            for option in field.options:
                if option.value == option_value:
                    target_option = option
                    target_label = option.label
                    break
        if target_option is None:
            return

        editor = QKeySequenceEdit()
        if target_option.shortcut:
            editor.setKeySequence(QKeySequence(target_option.shortcut))

        dialog = QDialog(self)
        dialog.setWindowTitle(f"设置快捷键 - {target_label}")
        dialog.setModal(True)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(16, 16, 16, 16)
        dialog_layout.setSpacing(10)
        dialog_layout.addWidget(QLabel(f"{target_label}"))
        dialog_layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear_button = buttons.addButton("清空", QDialogButtonBox.ButtonRole.ResetRole)
        clear_button.clicked.connect(editor.clear)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog_layout.addWidget(buttons)

        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        shortcut_text = self._key_sequence_text(editor, allow_empty=True)
        target_option.shortcut = shortcut_text or None
        self._update_snapshot(snapshot)

    def _activate_option_shortcut(self, field_id: str, shortcut: str) -> None:
        widget = self._field_widgets.get(field_id)
        if widget is not None:
            widget.activate_shortcut(shortcut)

    def _populate_review_form(self, values: dict[str, object]) -> None:
        for field_id, widget in self._field_widgets.items():
            widget.set_value(values.get(field_id))

    def _clear_review_form(self) -> None:
        for widget in self._field_widgets.values():
            widget.clear_value()

    def _sync_form_with_current_item(self) -> None:
        if self.task.current_review is None:
            return
        self._populate_review_form(self.task.current_review.review_data)

    def _toggle_always_on_top(self, checked: bool) -> None:
        self._set_always_on_top(checked, persist=True)

    def _set_always_on_top(self, enabled: bool, *, persist: bool) -> None:
        self._always_on_top_button.blockSignals(True)
        self._always_on_top_button.setChecked(enabled)
        self._always_on_top_button.blockSignals(False)
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        if was_visible:
            self.show()
        if persist:
            self._app_service.save_app_setting("always_on_top", enabled)

    def _load_window_preferences(self) -> None:
        raw = self._app_service.load_app_setting("always_on_top")
        self._set_always_on_top(bool(raw), persist=False)

    def _key_sequence_text(
        self,
        editor: QKeySequenceEdit,
        *,
        allow_empty: bool = False,
    ) -> str:
        text = editor.keySequence().toString(QKeySequence.SequenceFormat.NativeText).strip()
        if not allow_empty and not text:
            raise ValueError("快捷键不能为空。")
        return text

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._browser.shutdown()
        if self._on_close is not None:
            self._on_close()
        super().closeEvent(event)
