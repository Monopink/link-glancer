from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
from link_glancer.browser.base import BrowserController
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
        self._next_shortcut_edit = QKeySequenceEdit()
        self._skip_shortcut_edit = QKeySequenceEdit()

        shortcuts = task.task_snapshot.shortcuts
        self._submit_shortcut_edit.setKeySequence(QKeySequence(shortcuts.submit))
        self._previous_shortcut_edit.setKeySequence(QKeySequence(shortcuts.previous))
        self._next_shortcut_edit.setKeySequence(QKeySequence(shortcuts.next))
        self._skip_shortcut_edit.setKeySequence(QKeySequence(shortcuts.skip))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("提交/保存", self._submit_shortcut_edit)
        form.addRow("上一条", self._previous_shortcut_edit)
        form.addRow("下一条", self._next_shortcut_edit)
        form.addRow("跳过", self._skip_shortcut_edit)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> tuple[str, str, str, str]:
        return (
            self._key_sequence_text(self._submit_shortcut_edit),
            self._key_sequence_text(self._previous_shortcut_edit),
            self._key_sequence_text(self._next_shortcut_edit),
            self._key_sequence_text(self._skip_shortcut_edit),
        )

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
        self._review_started_completed = self._display_completed_items_for(self.task)
        self._submit_locked = False
        self._switching_current_item = False
        self._browser_unavailable_notified = False

        self.setWindowTitle("检查")
        self.resize(660, 660)
        self.setMinimumWidth(560)

        self._build_ui()
        self._load_window_preferences()
        self._render_review_fields()
        self._bind_shortcuts()
        self._refresh_view()

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
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
        top_row.setSpacing(12)
        top_row.addWidget(self._summary("进度", self._progress_label), stretch=1)
        top_row.addWidget(self._summary("完成", self._completion_label), stretch=1)
        top_row.addWidget(self._summary("剩余", self._remaining_label), stretch=1)
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

        top_controls = QHBoxLayout()
        self._always_on_top_checkbox = QCheckBox("窗口置顶")
        self._always_on_top_checkbox.clicked.connect(self._toggle_always_on_top)
        top_controls.addStretch(1)
        top_controls.addWidget(self._always_on_top_checkbox)
        layout.addLayout(top_controls)

        actions = QHBoxLayout()
        self._shortcut_button = QPushButton("快捷键")
        self._shortcut_button.clicked.connect(self._edit_public_shortcuts)
        self._skip_button = QPushButton("跳过")
        self._skip_button.clicked.connect(self._skip_review)
        self._previous_button = QPushButton("上一条")
        self._previous_button.clicked.connect(self._go_to_previous)
        self._next_button = QPushButton("下一条")
        self._next_button.clicked.connect(self._go_to_next)
        self._submit_button = QPushButton("提交")
        self._submit_button.clicked.connect(self._submit_review)
        actions.addWidget(self._shortcut_button)
        actions.addStretch(1)
        actions.addWidget(self._skip_button)
        actions.addWidget(self._previous_button)
        actions.addWidget(self._next_button)
        actions.addWidget(self._submit_button)
        layout.addLayout(actions)

        self.setCentralWidget(root)

    def _summary(self, title: str, value: QLabel) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title_label = QLabel(f"{title}:")
        value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(title_label)
        layout.addWidget(value)
        layout.addStretch(1)
        return widget

    def _render_review_fields(self, values: dict[str, object] | None = None) -> None:
        while self._fields_layout.count():
            item = self._fields_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._field_widgets.clear()
        for field in self._app_service.enabled_review_fields(self.task.task_snapshot):
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
        submit_shortcut.setProperty("action_id", "submit")
        self._dynamic_shortcuts.append(submit_shortcut)

        previous_shortcut = QShortcut(QKeySequence(shortcuts.previous), self)
        previous_shortcut.activated.connect(self._go_to_previous)
        previous_shortcut.setProperty("action_id", "previous")
        self._dynamic_shortcuts.append(previous_shortcut)

        next_shortcut = QShortcut(QKeySequence(shortcuts.next), self)
        next_shortcut.activated.connect(self._go_to_next)
        next_shortcut.setProperty("action_id", "next")
        self._dynamic_shortcuts.append(next_shortcut)

        skip_shortcut = QShortcut(QKeySequence(shortcuts.skip), self)
        skip_shortcut.activated.connect(self._skip_review)
        skip_shortcut.setProperty("action_id", "skip")
        self._dynamic_shortcuts.append(skip_shortcut)

        for field in self._app_service.enabled_review_fields(self.task.task_snapshot):
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
        self._apply_interaction_state()

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
        self._refresh_browser_availability(notify=False)
        viewing_index = self.task.viewing_task_index
        completed = self._display_completed_items()
        percentage = int((completed / self.task.total_items) * 100) if self.task.total_items else 0
        self._progress_label.setText(f"{completed}/{self.task.total_items}")
        self._progress_bar.setMaximum(max(self.task.total_items, 1))
        self._progress_bar.setValue(completed)
        self._progress_bar.setFormat(f"{percentage}%")
        self._refresh_time_labels()
        self._item_details_label.setText(self._build_item_details(viewing_index))
        self._sync_form_with_viewing_item()
        self._apply_interaction_state()

    def _apply_interaction_state(self) -> None:
        viewing_current_item = self._is_viewing_current_item()
        if self._switching_current_item and viewing_current_item:
            submit_text = "切换中"
        else:
            submit_text = self._button_text(
                "提交" if viewing_current_item else "保存修改",
                self.task.task_snapshot.shortcuts.submit,
            )
        self._submit_button.setText(submit_text)
        self._submit_button.setEnabled(
            self.task.viewing_item is not None
            and not self._submit_locked
            and self._can_submit_current_view()
        )
        navigation_enabled = not self._switching_current_item
        self._skip_button.setText(self._button_text("跳过", self.task.task_snapshot.shortcuts.skip))
        self._previous_button.setText(
            self._button_text("上一条", self.task.task_snapshot.shortcuts.previous)
        )
        self._next_button.setText(
            self._button_text("下一条", self.task.task_snapshot.shortcuts.next)
        )
        self._shortcut_button.setText("快捷键")
        self._skip_button.setEnabled(
            navigation_enabled
            and self._is_viewing_current_item()
            and self.task.viewing_item is not None
            and self._browser.status().running
        )
        self._previous_button.setEnabled(navigation_enabled and self._can_view_previous())
        self._next_button.setEnabled(navigation_enabled and self._can_view_next())
        self._fields_container.setEnabled(not self._switching_current_item)
        for shortcut in self._dynamic_shortcuts:
            action_id = shortcut.property("action_id")
            if action_id == "submit":
                shortcut.setEnabled(not self._submit_locked)
            elif action_id == "previous":
                shortcut.setEnabled(navigation_enabled)
            elif action_id == "next":
                shortcut.setEnabled(navigation_enabled)
            elif action_id == "skip":
                shortcut.setEnabled(
                    navigation_enabled
                    and self._is_viewing_current_item()
                    and self.task.viewing_item is not None
                )

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
        details: list[str] = [f"序号：{task_index}"]
        for field_name in self._app_service.task_display_field_names(self.task, [item]):
            if field_name in item.task_data:
                details.append(f"{field_name}：{item.task_data[field_name]}")
        return "\n".join(details)

    def _review_time_metrics(self) -> int | None:
        elapsed = max(int((datetime.now(UTC) - self._review_started_at).total_seconds()), 0)
        completed_in_session = self._display_completed_items() - self._review_started_completed
        remaining = max(self.task.total_items - self._display_completed_items(), 0)
        if completed_in_session <= 0:
            return None
        return int(elapsed / completed_in_session * remaining)

    def _format_completion_time(self, remaining_seconds: int | None) -> str:
        if remaining_seconds is None:
            return "计算中"
        estimated_at = datetime.now().astimezone() + timedelta(seconds=remaining_seconds)
        return estimated_at.strftime("%Y-%m-%d %H:%M")

    def _format_duration(self, total_seconds: int) -> str:
        hours, remainder = divmod(max(total_seconds, 0), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}小时 {minutes}分"
        if minutes > 0:
            return f"{minutes}分 {seconds}秒"
        return f"{seconds}秒"

    def _display_completed_items(self) -> int:
        return self._display_completed_items_for(self.task)

    def _display_completed_items_for(self, task: TaskDetail) -> int:
        if task.total_items <= 0:
            return 0
        pointer_completed = max(0, min(task.current_task_index - 1, task.total_items))
        return min(task.completed_items, pointer_completed)

    def _collect_review_values(self) -> tuple[dict[str, object], list[str]]:
        values: dict[str, object] = {}
        errors: list[str] = []
        for field in self._app_service.enabled_review_fields(self.task.task_snapshot):
            widget = self._field_widgets.get(field.field_id)
            if widget is None:
                continue
            values[field.field_id] = widget.value()
            if not widget.is_complete():
                errors.append(field.label)
        return values, errors

    def _collect_form_values(self) -> dict[str, object]:
        values = {field_id: widget.value() for field_id, widget in self._field_widgets.items()}
        return {key: value for key, value in values.items() if not _is_empty_value(value)}

    def _submit_review(self) -> None:
        if self._submit_locked:
            return
        if not self._can_submit_current_view():
            self._refresh_browser_availability(notify=True)
            return
        target_index = self.task.viewing_task_index
        submitting_current_item = self._is_viewing_current_item()
        values, errors = self._collect_review_values()
        if errors:
            QMessageBox.warning(self, "缺少必填项", "请先完成：\n" + "\n".join(errors))
            return
        if submitting_current_item:
            self._submit_locked = True
            self._switching_current_item = True
            self._apply_interaction_state()
        self.task = self._app_service.save_review(
            task_id=self.task.task_id,
            task_index=target_index,
            review_data=values,
            advance_pointer=submitting_current_item,
        )
        self._refresh_view()
        if submitting_current_item:
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            QTimer.singleShot(0, self._sync_browser_buffer_after_submit)
            return
        self._submit_locked = False
        self._apply_interaction_state()

    def _sync_browser_buffer_after_submit(self) -> None:
        try:
            self._sync_browser_buffer()
        finally:
            self._switching_current_item = False
            self._submit_locked = False
            self._refresh_view()

    def _skip_review(self) -> None:
        if (
            self._submit_locked
            or self._switching_current_item
            or not self._is_viewing_current_item()
        ):
            return
        if self.task.viewing_item is None:
            return
        if not self._browser.status().running:
            self._refresh_browser_availability(notify=True)
            return
        self._submit_locked = True
        self._switching_current_item = True
        self._apply_interaction_state()
        self.task = self._app_service.skip_review(
            task_id=self.task.task_id,
            task_index=self.task.current_task_index,
        )
        self._refresh_view()
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        QTimer.singleShot(0, self._sync_browser_buffer_after_submit)

    def _go_to_previous(self) -> None:
        if self._switching_current_item:
            return
        if not self._can_view_previous():
            return
        self._navigate_to(self.task.viewing_task_index - 1)

    def _go_to_next(self) -> None:
        if self._switching_current_item:
            return
        if not self._can_view_next():
            return
        self._navigate_to(self.task.viewing_task_index + 1)

    def _navigate_to(self, task_index: int) -> None:
        if self._switching_current_item:
            return
        if self._is_viewing_current_item():
            self._save_current_draft()
        self.task = self._app_service.set_viewing_task_index(
            task_id=self.task.task_id,
            task_index=task_index,
        )
        self._refresh_view()

    def _can_view_previous(self) -> bool:
        return self.task.viewing_task_index > 1

    def _can_view_next(self) -> bool:
        return self.task.viewing_task_index < self._max_viewing_index()

    def _max_viewing_index(self) -> int:
        if self.task.total_items <= 0:
            return 1
        return min(self.task.current_task_index, self.task.total_items)

    def _is_viewing_current_item(self) -> bool:
        return self.task.viewing_task_index == self.task.current_task_index

    def _save_current_draft(self) -> None:
        if not self._is_viewing_current_item():
            return
        self.task = self._app_service.save_review_draft(
            task_id=self.task.task_id,
            task_index=self.task.current_task_index,
            draft_data=self._collect_form_values(),
        )

    def _sync_browser_buffer(self) -> None:
        items = self._app_service.list_buffer_items(self.task)
        self._browser.sync_buffer(
            tasks=items,
            url_field=self.task.task_snapshot.url_field,
        )
        self._refresh_browser_availability(notify=True)

    def _edit_public_shortcuts(self) -> None:
        dialog = ShortcutConfigDialog(self.task, self)
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        try:
            submit_shortcut, previous_shortcut, next_shortcut, skip_shortcut = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "快捷键无效", str(exc))
            return
        snapshot = deepcopy(self.task.task_snapshot)
        snapshot.shortcuts.submit = submit_shortcut
        snapshot.shortcuts.previous = previous_shortcut
        snapshot.shortcuts.next = next_shortcut
        snapshot.shortcuts.skip = skip_shortcut
        self._update_snapshot(snapshot)

    def _edit_option_shortcut(self, field_id: str, option_value: str) -> None:
        snapshot = deepcopy(self.task.task_snapshot)
        target_option = None
        for field in snapshot.review_fields:
            if field.field_id != field_id:
                continue
            for option in field.options:
                if option.value == option_value:
                    target_option = option
                    break
        if target_option is None:
            return

        editor = QKeySequenceEdit()
        if target_option.shortcut:
            editor.setKeySequence(QKeySequence(target_option.shortcut))

        dialog = QDialog(self)
        dialog.setWindowTitle(f"设置快捷键 - {option_value}")
        dialog.setModal(True)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(16, 16, 16, 16)
        dialog_layout.setSpacing(10)
        dialog_layout.addWidget(QLabel(option_value))
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

    def _sync_form_with_viewing_item(self) -> None:
        values: dict[str, object] = {}
        if self._is_viewing_current_item():
            if self.task.current_draft is not None:
                values = self.task.current_draft.draft_data
            elif self.task.current_review is not None:
                values = self.task.current_review.review_data
        elif self.task.viewing_review is not None:
            values = self.task.viewing_review.review_data
        self._populate_review_form(values)

    def _toggle_always_on_top(self, checked: bool) -> None:
        self._set_always_on_top(checked, persist=True)

    def _set_always_on_top(self, enabled: bool, *, persist: bool) -> None:
        self._always_on_top_checkbox.blockSignals(True)
        self._always_on_top_checkbox.setChecked(enabled)
        self._always_on_top_checkbox.blockSignals(False)
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

    def _button_text(self, label: str, shortcut: str) -> str:
        return f"{label} ({shortcut})" if shortcut else label

    def _can_submit_current_view(self) -> bool:
        if not self._is_viewing_current_item():
            return True
        return self._browser.status().running

    def _refresh_browser_availability(self, *, notify: bool) -> None:
        status = self._browser.status()
        if status.running:
            self._browser_unavailable_notified = False
            return
        if notify and not self._browser_unavailable_notified:
            QMessageBox.warning(
                self,
                "检查已停止",
                status.message or "浏览器已不可用，无法继续提交或跳过当前检查。",
            )
            self._browser_unavailable_notified = True

    def closeEvent(self, event) -> None:
        result = QMessageBox.question(
            self,
            "确认退出检查",
            "确认退出检查？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self._timer.stop()
        self._save_current_draft()
        self._browser.shutdown()
        if self._on_close is not None:
            self._on_close()
        super().closeEvent(event)


def _is_empty_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False
