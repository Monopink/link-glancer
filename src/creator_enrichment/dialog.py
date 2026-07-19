from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QEventLoop, QProcess, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from link_glancer.application import TaskApplicationService
from link_glancer.runtime.dev import dev_mode_title_suffix, is_dev_mode
from link_glancer.runtime.locks import (
    RuntimeLockConflictError,
    RuntimeLockHandle,
    acquire_profile_lock,
    acquire_task_lock,
)
from link_glancer.tasks.models import TaskDetail

COLLECTION_POLL_INTERVAL_MS = 1000
TIME_LABEL_REFRESH_INTERVAL_SECONDS = 5


@dataclass(slots=True)
class _EnrichmentWorkerStatus:
    running: bool
    paused: bool
    completed: bool
    startup_phase: str
    total_count: int
    completed_count: int
    success_count: int
    no_contact_count: int
    auto_skipped_count: int
    skipped_count: int
    failed_count: int
    current_task_index: int | None
    current_region: str | None
    remaining_regions: list[str]
    last_message: str
    pause_reason: str | None
    diagnostic_summary: str | None
    diagnostic_text: str | None
    failure_attempts: list[dict[str, object]]
    attention_required: bool
    started_at: datetime | None
    estimated_end_at: datetime | None

    @classmethod
    def idle(cls) -> _EnrichmentWorkerStatus:
        return cls(
            running=False,
            paused=True,
            completed=False,
            startup_phase="idle",
            total_count=0,
            completed_count=0,
            success_count=0,
            no_contact_count=0,
            auto_skipped_count=0,
            skipped_count=0,
            failed_count=0,
            current_task_index=None,
            current_region=None,
            remaining_regions=[],
            last_message="补充采集会话未启动。",
            pause_reason=None,
            diagnostic_summary=None,
            diagnostic_text=None,
            failure_attempts=[],
            attention_required=False,
            started_at=None,
            estimated_end_at=None,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> _EnrichmentWorkerStatus:
        remaining_regions = payload.get("remaining_regions")
        raw_failure_attempts = payload.get("failure_attempts")
        return cls(
            running=bool(payload.get("running")),
            paused=bool(payload.get("paused")),
            completed=bool(payload.get("completed")),
            startup_phase=str(payload.get("startup_phase") or "idle"),
            total_count=int(payload.get("total_count") or 0),
            completed_count=int(payload.get("completed_count") or 0),
            success_count=int(payload.get("success_count") or 0),
            no_contact_count=int(payload.get("no_contact_count") or 0),
            auto_skipped_count=int(payload.get("auto_skipped_count") or 0),
            skipped_count=int(payload.get("skipped_count") or 0),
            failed_count=int(payload.get("failed_count") or 0),
            current_task_index=(
                int(payload["current_task_index"])
                if isinstance(payload.get("current_task_index"), int)
                else None
            ),
            current_region=(
                str(payload.get("current_region")) if payload.get("current_region") else None
            ),
            remaining_regions=(
                [str(item) for item in remaining_regions if item]
                if isinstance(remaining_regions, list)
                else []
            ),
            last_message=str(payload.get("last_message") or ""),
            pause_reason=str(payload.get("pause_reason") or "") or None,
            diagnostic_summary=(
                str(payload.get("diagnostic_summary"))
                if payload.get("diagnostic_summary")
                else None
            ),
            diagnostic_text=(
                str(payload.get("diagnostic_text")) if payload.get("diagnostic_text") else None
            ),
            failure_attempts=(
                [item for item in raw_failure_attempts if isinstance(item, dict)]
                if isinstance(raw_failure_attempts, list)
                else []
            ),
            attention_required=bool(payload.get("attention_required")),
            started_at=_parse_datetime(payload.get("started_at")),
            estimated_end_at=_parse_datetime(payload.get("estimated_end_at")),
        )


class CreatorEnrichmentDialog(QDialog):
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        task: TaskDetail,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_service = app_service
        self._task = task
        self._status = _EnrichmentWorkerStatus.idle()
        self._startup_pending = True
        self._startup_phase = "browser_confirm"
        self._worker_started = False
        self._worker_buffer = ""
        self._expected_process_exit = False
        self._force_close = False
        self._last_time_label_refresh_at: datetime | None = None
        self._last_notified_message = ""
        self._last_issue_dialog_key: tuple[object, ...] | None = None
        self._startup_loop: QEventLoop | None = None
        self._startup_result: bool | None = None
        self._process = QProcess(self)
        self._profile_lock: RuntimeLockHandle | None = None
        self._task_lock: RuntimeLockHandle | None = None

        self.setWindowTitle(f"补充采集 · 任务 #{task.task_id}{dev_mode_title_suffix()}")
        self.resize(420, 260)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(16)

        left_widget = QWidget()
        left_grid = QGridLayout(left_widget)
        left_grid.setContentsMargins(0, 0, 0, 0)
        left_grid.setHorizontalSpacing(12)
        left_grid.setVerticalSpacing(10)
        left_grid.setColumnStretch(1, 1)
        self._auto_skip_checkbox = QCheckBox("自动跳过")
        self._auto_skip_checkbox.setChecked(False)
        self._count_label = QLabel("0 / 0")
        self._success_label = QLabel("0")
        self._no_contact_label = QLabel("0")
        self._skipped_label = QLabel("0")
        _add_summary_row(left_grid, 0, "进度", self._count_label)
        _add_summary_row(left_grid, 1, "有联系方式", self._success_label)
        _add_summary_row(left_grid, 2, "无联系方式", self._no_contact_label)
        _add_summary_row(left_grid, 3, "跳过", self._skipped_label)
        summary_row.addWidget(left_widget, 3)

        right_widget = QWidget()
        right_grid = QGridLayout(right_widget)
        right_grid.setContentsMargins(0, 0, 0, 0)
        right_grid.setHorizontalSpacing(12)
        right_grid.setVerticalSpacing(10)
        self._region_label = QLabel("-")
        self._remaining_region_label = QLabel("-")
        self._elapsed_label = QLabel("-")
        self._eta_label = QLabel("-")
        _add_summary_row(right_grid, 0, "当前区域", self._region_label)
        _add_summary_row(right_grid, 1, "剩余区域", self._remaining_region_label, stretch=True)
        _add_summary_row(right_grid, 2, "预计结束", self._elapsed_label)
        _add_summary_row(right_grid, 3, "剩余时间", self._eta_label)
        summary_row.addWidget(right_widget, 4)
        root.addLayout(summary_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        self._message_label = QLabel("启动中")
        self._message_label.setWordWrap(True)
        root.addWidget(self._message_label)

        options_row = QHBoxLayout()
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setSpacing(12)
        options_row.addWidget(self._auto_skip_checkbox)
        options_row.addStretch(1)
        self._pause_button = QPushButton("暂停")
        self._pause_button.clicked.connect(self._pause)
        self._continue_button = QPushButton("继续")
        self._continue_button.clicked.connect(self._resume)
        options_row.addWidget(self._pause_button)
        options_row.addWidget(self._continue_button)

        root.addStretch(1)
        root.addLayout(options_row)

        self._timer = QTimer(self)
        self._timer.setInterval(COLLECTION_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_time_labels)
        self._timer.start()
        self._configure_process()
        self._refresh_from_status()

    def _configure_process(self) -> None:
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.started.connect(self._send_start_command)
        self._process.readyReadStandardOutput.connect(self._read_worker_output)
        self._process.finished.connect(self._handle_process_finished)
        self._process.errorOccurred.connect(self._handle_process_error)

    def start_with_confirmation(self) -> bool:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return False
        self._startup_result = None
        self._start_worker_process()
        if self._force_close:
            return False
        loop = QEventLoop(self)
        self._startup_loop = loop
        loop.exec()
        self._startup_loop = None
        return bool(self._startup_result)

    def _start_worker_process(self) -> None:
        try:
            self._task_lock = acquire_task_lock(
                self._task.task_id,
                owner_label=f"补充采集任务 - {self._task.browser_config.name}",
            )
            self._profile_lock = acquire_profile_lock(
                self._task.browser_config.profile_id,
                owner_label=f"补充采集任务 - {self._task.browser_config.name}",
            )
        except RuntimeLockConflictError as exc:
            QMessageBox.warning(self, "无法开始补充采集", str(exc))
            self._force_close = True
            self.close()
            return
        if getattr(sys, "frozen", False):
            program = sys.executable
            arguments = ["--creator-enrichment-worker"]
        else:
            program = sys.executable
            arguments = ["-m", "link_glancer.main", "--creator-enrichment-worker"]
        if is_dev_mode():
            arguments.append("--dev-mode")
        self._process.start(program, arguments)

    def _send_start_command(self) -> None:
        self._send_command({"cmd": "start", "task_id": self._task.task_id})

    def _read_worker_output(self) -> None:
        self._worker_buffer += bytes(self._process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        while "\n" in self._worker_buffer:
            line, self._worker_buffer = self._worker_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._handle_worker_message(payload)

    def _handle_worker_message(self, payload: dict[str, object]) -> None:
        message_type = str(payload.get("type") or "")
        if message_type == "status":
            raw_status = payload.get("status")
            if isinstance(raw_status, dict):
                self._status = _EnrichmentWorkerStatus.from_payload(raw_status)
                if not self._worker_started and self._status.running:
                    self._worker_started = True
                self._maybe_handle_startup_phase()
                self._refresh_from_status()
                if self._should_close_after_browser_shutdown():
                    self._force_close = True
                    self.close()
                    return
                self._notify_status_change()
                self._show_issue_dialog_if_needed()
            return
        if message_type == "error":
            message = str(payload.get("message") or "补充采集进程执行失败。")
            details = str(payload.get("details") or "")
            if _is_expected_browser_close_message(message, details):
                return
            self._finish_startup(result=False)
            QMessageBox.warning(self, "补充采集失败", message)

    def _maybe_handle_startup_phase(self) -> None:
        if not self._startup_pending:
            return
        phase = self._status.startup_phase or "idle"
        if phase == "browser_confirm" and self._startup_phase == "browser_confirm":
            self._startup_phase = "browser_confirm_shown"
            self._confirm_browser_start()
            return
        if phase == "collecting" and self._startup_phase == "browser_confirm_done":
            self._startup_phase = "collecting"
            self._startup_pending = False
            self._finish_startup(result=True)

    def _finish_startup(self, *, result: bool) -> None:
        if self._startup_result is None:
            self._startup_result = result
        if self._startup_loop is not None and self._startup_loop.isRunning():
            self._startup_loop.quit()

    def _confirm_browser_start(self) -> None:
        region_text = self._status.current_region or "未知区域"
        result = QMessageBox.question(
            self,
            "确认开始补充采集",
            "请确认浏览器已正常打开，并已完成采集准备。\n"
            f"即将采集的商店区域：{region_text}\n\n"
            "是否开始补充采集？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._startup_phase = "browser_confirm_done"
            self._send_command({"cmd": "prepare_pages"})
            return
        self._startup_phase = "browser_confirm"
        self._finish_startup(result=False)
        self._expected_process_exit = True
        self._send_command({"cmd": "shutdown"})
        self._force_close = True
        self.close()

    def _refresh_from_status(self) -> None:
        status = self._status
        self._count_label.setText(f"{status.completed_count} / {status.total_count}")
        self._region_label.setText(status.current_region or "-")
        self._remaining_region_label.setText(", ".join(status.remaining_regions) or "-")
        self._success_label.setText(str(status.success_count))
        self._no_contact_label.setText(str(status.no_contact_count))
        self._skipped_label.setText(str(status.skipped_count + status.auto_skipped_count))
        self._message_label.setText(self._display_message_text(status))
        progress = 0
        if status.total_count > 0:
            progress = int(status.completed_count * 100 / status.total_count)
        self._progress.setValue(progress)
        self._refresh_time_labels_if_needed(force=True)
        controls_ready = not self._startup_pending
        can_continue = controls_ready and status.running and status.paused and not status.completed
        self._continue_button.setEnabled(can_continue)
        self._pause_button.setEnabled(
            controls_ready and status.running and not status.completed and not status.paused
        )

    def _refresh_time_labels(self) -> None:
        self._refresh_time_labels_if_needed(force=False)

    def _refresh_time_labels_if_needed(self, *, force: bool) -> None:
        now = datetime.now(UTC)
        should_refresh = force or self._last_time_label_refresh_at is None
        if not should_refresh:
            should_refresh = (now - self._last_time_label_refresh_at) >= timedelta(
                seconds=TIME_LABEL_REFRESH_INTERVAL_SECONDS
            )
        if not should_refresh:
            return
        self._elapsed_label.setText(self._format_finish_time())
        self._eta_label.setText(self._format_remaining())
        self._last_time_label_refresh_at = now

    def _format_finish_time(self) -> str:
        estimated_end_at = self._status.estimated_end_at
        if estimated_end_at is None:
            return "-"
        return estimated_end_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _format_remaining(self) -> str:
        estimated_end_at = self._status.estimated_end_at
        if estimated_end_at is None:
            return "-"
        remaining = max(int((estimated_end_at - datetime.now(UTC)).total_seconds()), 0)
        hours, remainder = divmod(remaining, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _display_status_text(self, status: _EnrichmentWorkerStatus) -> str:
        if self._startup_pending:
            return "启动中"
        if status.completed:
            return "采集完成"
        if status.paused:
            return "已暂停"
        return "采集中"

    def _display_message_text(self, status: _EnrichmentWorkerStatus) -> str:
        if self._startup_pending:
            if self._startup_phase == "browser_confirm_done":
                return "正在准备补充采集。"
            return "正在启动浏览器。"
        return status.last_message or "-"

    def _notify_status_change(self) -> None:
        status = self._status
        if not status.last_message or status.last_message == self._last_notified_message:
            return
        self._last_notified_message = status.last_message
        if status.completed:
            self._show_system_notification("补充采集完成", status.last_message)
            return
        if status.paused:
            title = "补充采集已暂停"
            if status.pause_reason == "captcha":
                title = "需要验证码"
            elif status.pause_reason == "region_mismatch":
                title = "需要切换区域"
            self._show_system_notification(title, status.last_message)

    def _show_system_notification(self, title: str, message: str) -> None:
        app = QApplication.instance()
        if app is None or app.applicationState() == Qt.ApplicationState.ApplicationActive:
            return
        tray_icon = getattr(app, "tray_icon", None)
        if tray_icon is None:
            return
        tray_icon.showMessage(title, message, tray_icon.MessageIcon.Information, 8000)

    def _should_close_after_browser_shutdown(self) -> bool:
        return (
            self._worker_started
            and not self._status.running
            and self._status.last_message == "浏览器已关闭。"
        )

    def _resume(self) -> None:
        self._send_command(
            {
                "cmd": "resume",
                "auto_skip_on_failure": self._auto_skip_checkbox.isChecked(),
            }
        )

    def _pause(self) -> None:
        self._send_command({"cmd": "pause"})

    def _show_issue_dialog_if_needed(self) -> None:
        status = self._status
        if status.last_message == "浏览器已关闭。":
            return
        if not status.attention_required:
            return
        dialog_key = (
            status.current_task_index,
            status.pause_reason,
            status.last_message,
            status.diagnostic_summary,
        )
        if dialog_key == self._last_issue_dialog_key:
            return
        self._last_issue_dialog_key = dialog_key
        if not status.diagnostic_text:
            return
        dialog = _IssueDialog(
            title=self._issue_dialog_title(status),
            user_message=self._issue_dialog_message(status),
            diagnostic_text=status.diagnostic_text,
            failure_attempts=status.failure_attempts,
            task_name=self._task.name,
            parent=self,
        )
        action = dialog.exec_action()
        if action == "retry":
            self._send_command({"cmd": "retry"})
        elif action == "skip":
            self._send_command({"cmd": "skip"})

    def _issue_dialog_title(self, status: _EnrichmentWorkerStatus) -> str:
        if status.pause_reason == "captcha":
            return "需要完成验证码"
        if status.pause_reason == "region_mismatch":
            return "需要切换店铺区域"
        return "补充采集需要人工处理"

    def _issue_dialog_message(self, status: _EnrichmentWorkerStatus) -> str:
        if status.pause_reason == "captcha":
            return "请在浏览器中完成验证。\n是否准备好重试？"
        if status.pause_reason == "region_mismatch":
            target_region = status.current_region or "目标区域"
            return (
                f"当前页面的店铺区域与达人数据区域不一致，请切换区域：{target_region}。\n"
                "是否准备好重试？"
            )
        return "当前达人的补充采集出现异常已暂停，请进行检查并重试或跳过。"

    def _send_command(self, payload: dict[str, object]) -> None:
        if self._process.state() != QProcess.ProcessState.Running:
            return
        self._process.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self._process.waitForBytesWritten(1000)

    def _handle_process_finished(self) -> None:
        self._release_locks()
        self._finish_startup(result=False)
        if self._force_close:
            self._force_close = False
            self.close()

    def _handle_process_error(self, _error: QProcess.ProcessError) -> None:
        if self._force_close or self._expected_process_exit:
            return
        if self._status.last_message == "浏览器已关闭。":
            self._finish_startup(result=False)
            self._force_close = True
            self.close()
            return
        self._finish_startup(result=False)
        QMessageBox.warning(self, "补充采集进程失败", self._process.errorString())

    def _shutdown_worker_process(self) -> None:
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        self._expected_process_exit = True
        self._send_command({"cmd": "shutdown"})
        if not self._process.waitForFinished(3000):
            self._process.kill()
            self._process.waitForFinished(2000)

    def _release_locks(self) -> None:
        if self._task_lock is not None:
            self._task_lock.release()
            self._task_lock = None
        if self._profile_lock is not None:
            self._profile_lock.release()
            self._profile_lock = None

    def closeEvent(self, event) -> None:
        self._shutdown_worker_process()
        self._release_locks()
        super().closeEvent(event)


def _add_summary_row(
    grid: QGridLayout,
    row: int,
    label_text: str,
    value_label: QLabel,
    *,
    stretch: bool = False,
) -> None:
    label = QLabel(label_text)
    grid.addWidget(label, row, 0)
    grid.addWidget(value_label, row, 1)
    if stretch:
        grid.setColumnStretch(1, 1)


def _parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _is_expected_browser_close_message(message: str, details: str) -> bool:
    content = f"{message}\n{details}"
    lowered = content.casefold()
    return (
        "浏览器已关闭" in content
        or "target page, context or browser has been closed" in lowered
        or "browser has been closed" in lowered
    )


class _IssueDialog(QDialog):
    def __init__(
        self,
        *,
        title: str,
        user_message: str,
        diagnostic_text: str,
        failure_attempts: list[dict[str, object]],
        task_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._diagnostic_text = diagnostic_text
        self._failure_attempts = failure_attempts
        self._task_name = task_name
        self._selected_action = "close"
        self.setWindowTitle(title)
        self.resize(860, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary_label = QLabel(user_message)
        summary_label.setWordWrap(True)
        summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(summary_label)

        details_label = QLabel("开发者详细排查信息")
        root.addWidget(details_label)

        self._tabs = QTabWidget()
        self._text_edits: list[QPlainTextEdit] = []
        if failure_attempts:
            for index, attempt in enumerate(failure_attempts, start=1):
                text_edit = QPlainTextEdit()
                text_edit.setPlainText(str(attempt.get("diagnostic_text") or ""))
                text_edit.setReadOnly(True)
                self._tabs.addTab(text_edit, f"第{index}次")
                self._text_edits.append(text_edit)
        else:
            text_edit = QPlainTextEdit()
            text_edit.setPlainText(diagnostic_text)
            text_edit.setReadOnly(True)
            self._tabs.addTab(text_edit, "详情")
            self._text_edits.append(text_edit)
        root.addWidget(self._tabs)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        retry_button = QPushButton("重试")
        retry_button.clicked.connect(self._accept_retry)
        skip_button = QPushButton("跳过")
        skip_button.clicked.connect(self._accept_skip)
        copy_button = QPushButton("复制全部")
        copy_button.clicked.connect(self._copy_text)
        select_button = QPushButton("全选")
        select_button.clicked.connect(self._select_current_text)
        export_button = QPushButton("导出")
        export_button.clicked.connect(self._export_text)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        actions.addWidget(retry_button)
        actions.addWidget(skip_button)
        actions.addWidget(copy_button)
        actions.addWidget(select_button)
        actions.addWidget(export_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        root.addLayout(actions)

    def exec_action(self) -> str:
        self.exec()
        return self._selected_action

    def _accept_retry(self) -> None:
        self._selected_action = "retry"
        self.accept()

    def _accept_skip(self) -> None:
        self._selected_action = "skip"
        self.accept()

    def _copy_text(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._export_content())

    def _select_current_text(self) -> None:
        index = self._tabs.currentIndex()
        if 0 <= index < len(self._text_edits):
            self._text_edits[index].selectAll()

    def _export_text(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(char if char not in '<>:"/\\|?*' else "_" for char in self._task_name)
        default_name = f"{safe_name or 'task'}_enrichment_error_{timestamp}.txt"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "导出错误详情",
            default_name,
            "Text File (*.txt)",
        )
        if not selected:
            return
        path = selected if selected.lower().endswith(".txt") else f"{selected}.txt"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self._export_content())
        QMessageBox.information(self, "已导出", f"错误详情已导出到\n{path}")

    def _export_content(self) -> str:
        if not self._failure_attempts:
            return self._diagnostic_text
        sections: list[str] = []
        for index, attempt in enumerate(self._failure_attempts, start=1):
            sections.append(f"===== 第{index}次失败 =====")
            sections.append(str(attempt.get("diagnostic_text") or ""))
            sections.append("")
        return "\n".join(sections).rstrip()
