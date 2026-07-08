from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from creator_collector.exporter import export_collection_to_path
from creator_collector.models import CreatorCollectionConfig
from creator_collector.service import DEFAULT_SAFETY_LIMIT, CreatorCollectorSession
from link_glancer.application import TaskApplicationService
from link_glancer.tasks.models import BrowserConfig

DEFAULT_CREATOR_PAGE_URL = ""
DEFAULT_COLLECTION_EXPORT_NAME = "creator_collection_{timestamp}.xlsx"
COLLECTION_POLL_INTERVAL_MS = 1000
TIME_LABEL_REFRESH_INTERVAL_SECONDS = 5


class _SaveTaskWorker(QObject):
    finished = Signal(int, str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        rows: list[dict[str, object]],
        export_path: Path,
        browser_config_id: str,
        app_service: TaskApplicationService,
    ) -> None:
        super().__init__()
        self._rows = rows
        self._export_path = export_path
        self._browser_config_id = browser_config_id
        self._app_service = app_service

    def run(self) -> None:
        try:
            saved_path = export_collection_to_path(self._rows, self._export_path)
            task_id = self._app_service.create_task_from_creator_collection(
                source_path=saved_path,
                browser_config_id=self._browser_config_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished.emit(task_id, str(saved_path))


class CreatorCollectorDialog(QDialog):
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        browser_configs: list[BrowserConfig],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_service = app_service
        self._browser_configs = browser_configs
        self.created_task_id: int | None = None
        self._default_safety_limit = DEFAULT_SAFETY_LIMIT
        self._default_auto_advance_interval_seconds = 1.5

        self.setWindowTitle("新建采集任务")
        self.resize(560, 180)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._browser_combo = QComboBox()
        for config in self._browser_configs:
            self._browser_combo.addItem(config.name, config.config_id)
        self._page_url_edit = QLineEdit(DEFAULT_CREATOR_PAGE_URL)

        form.addRow("浏览器配置", self._browser_combo)
        form.addRow("页面 URL", self._page_url_edit)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        start_button = QPushButton("开始采集")
        buttons.addButton(start_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        start_button.clicked.connect(self._start_collection)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._restore_defaults()

    def _start_collection(self) -> None:
        browser_config = self._selected_browser_config()
        if browser_config is None:
            QMessageBox.warning(self, "缺少浏览器配置", "请先选择浏览器配置。")
            return
        self._save_defaults()
        session = CreatorCollectorSession(browser_config)
        status = session.start(self._build_config(), paused=True)
        if not status.running:
            QMessageBox.warning(self, "启动失败", status.last_message)
            session.shutdown()
            return

        result = QMessageBox.question(
            self,
            "确认开始采集",
            "请确认浏览器已正常打开，并已完成登录、国家切换、筛选调整等准备。\n\n是否开始采集？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if result != QMessageBox.StandardButton.Yes:
            session.shutdown()
            return

        progress_dialog = CreatorCollectorProgressDialog(
            app_service=self._app_service,
            browser_config=browser_config,
            session=session,
            auto_start=True,
            parent=self,
        )
        progress_dialog.exec()
        self.created_task_id = progress_dialog.created_task_id
        self._default_safety_limit = progress_dialog.last_safety_limit
        self._default_auto_advance_interval_seconds = (
            progress_dialog.last_auto_advance_interval_seconds
        )
        self._save_defaults()
        if self.created_task_id is not None:
            self.accept()

    def _selected_browser_config(self) -> BrowserConfig | None:
        selected_id = self._browser_combo.currentData()
        for config in self._browser_configs:
            if config.config_id == selected_id:
                return config
        return None

    def _build_config(self) -> CreatorCollectionConfig:
        return CreatorCollectionConfig(
            browser_config_id=str(self._browser_combo.currentData() or ""),
            page_url=self._page_url_edit.text().strip(),
            safety_limit=self._default_safety_limit,
            auto_advance_interval_seconds=self._default_auto_advance_interval_seconds,
        )

    def _restore_defaults(self) -> None:
        raw = self._app_service.load_app_setting("creator_collection_defaults")
        if not isinstance(raw, dict):
            return
        browser_config_id = raw.get("browser_config_id")
        if isinstance(browser_config_id, str):
            for index in range(self._browser_combo.count()):
                if self._browser_combo.itemData(index) == browser_config_id:
                    self._browser_combo.setCurrentIndex(index)
                    break
        page_url = raw.get("page_url")
        if isinstance(page_url, str) and page_url.strip():
            self._page_url_edit.setText(page_url)
        safety_limit = raw.get("safety_limit")
        if isinstance(safety_limit, int) and safety_limit > 0:
            self._default_safety_limit = safety_limit
        interval = raw.get("auto_advance_interval_seconds")
        if isinstance(interval, (int, float)) and interval > 0:
            self._default_auto_advance_interval_seconds = float(interval)

    def _save_defaults(self) -> None:
        self._app_service.save_app_setting(
            "creator_collection_defaults",
            {
                "browser_config_id": str(self._browser_combo.currentData() or ""),
                "page_url": self._page_url_edit.text().strip(),
                "safety_limit": self._default_safety_limit,
                "auto_advance_interval_seconds": self._default_auto_advance_interval_seconds,
            },
        )


class CreatorCollectorProgressDialog(QDialog):
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        browser_config: BrowserConfig,
        session: CreatorCollectorSession,
        auto_start: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_service = app_service
        self._browser_config = browser_config
        self._session = session
        self._auto_start = auto_start
        self._collection_started = False
        self._startup_pending = auto_start
        self._handling_interrupt = False
        self._last_interrupt_message: str | None = None
        self._save_thread: QThread | None = None
        self._save_worker: _SaveTaskWorker | None = None
        self._save_target_path: Path | None = None
        self._closing_after_save = False
        self._force_close = False
        self._last_time_label_refresh_at: datetime | None = None
        self.created_task_id: int | None = None
        self.last_safety_limit = session.status().safety_limit
        self._last_auto_advance_interval_seconds = session.status().auto_advance_interval_seconds
        self.last_auto_advance_interval_seconds = self._last_auto_advance_interval_seconds

        self.setWindowTitle("采集中")
        self.resize(700, 240)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(24)
        summary_grid.setVerticalSpacing(10)
        self._status_label = QLabel("启动中" if auto_start else "暂停滚动中")
        self._count_label = QLabel("0")
        self._page_label = QLabel("0")
        self._elapsed_label = QLabel("-")
        self._eta_label = QLabel("-")
        _add_summary_row(summary_grid, 0, 0, "状态", self._status_label, stretch=True)
        _add_summary_row(summary_grid, 0, 1, "已采集", self._count_label)
        _add_summary_row(summary_grid, 0, 2, "已抓取", self._page_label)
        _add_summary_row(summary_grid, 1, 0, "结束", self._elapsed_label)
        _add_summary_row(summary_grid, 1, 1, "剩余", self._eta_label)
        summary_grid.setColumnStretch(1, 2)
        summary_grid.setColumnStretch(3, 1)
        summary_grid.setColumnStretch(5, 1)
        root.addLayout(summary_grid)

        root.addStretch(1)

        controls_box = QVBoxLayout()
        controls_box.setContentsMargins(0, 0, 0, 0)
        controls_box.setSpacing(10)

        summary_bottom = QHBoxLayout()
        summary_bottom.setContentsMargins(0, 0, 0, 0)
        summary_bottom.setSpacing(12)
        self._safety_limit_spin = QSpinBox()
        self._safety_limit_spin.setRange(1, 100000)
        self._safety_limit_spin.setValue(self.last_safety_limit)
        self._safety_limit_spin.valueChanged.connect(self._update_safety_limit)
        self._auto_scroll_interval_spin = QDoubleSpinBox()
        self._auto_scroll_interval_spin.setRange(0.3, 5.0)
        self._auto_scroll_interval_spin.setSingleStep(0.2)
        self._auto_scroll_interval_spin.setDecimals(1)
        self._auto_scroll_interval_spin.setSuffix(" 秒")
        self._auto_scroll_interval_spin.setValue(self._last_auto_advance_interval_seconds)
        self._auto_scroll_interval_spin.valueChanged.connect(self._update_auto_scroll_interval)
        summary_bottom.addWidget(QLabel("上限"))
        summary_bottom.addWidget(self._safety_limit_spin)
        summary_bottom.addSpacing(16)
        summary_bottom.addWidget(QLabel("自动滚动间隔"))
        summary_bottom.addWidget(self._auto_scroll_interval_spin)
        summary_bottom.addStretch(1)
        controls_box.addLayout(summary_bottom)

        self._auto_scroll_button = _create_action_button("暂停自动滚动")
        self._stop_button = _create_action_button("结束")
        self._save_create_button = _create_action_button("保存并创建任务")
        self._auto_scroll_button.clicked.connect(self._toggle_auto_scroll)
        self._stop_button.clicked.connect(self._stop_collection)
        self._save_create_button.clicked.connect(self._save_and_create_task)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        actions.addWidget(self._auto_scroll_button)
        actions.addWidget(self._stop_button)
        actions.addStretch(1)
        actions.addWidget(self._save_create_button)
        controls_box.addLayout(actions)
        root.addLayout(controls_box)

        self._timer = QTimer(self)
        self._timer.setInterval(COLLECTION_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()
        if self._auto_start:
            QTimer.singleShot(150, self._begin_collection)

    def _begin_collection(self) -> None:
        if self._collection_started:
            return
        self._collection_started = True
        self._startup_pending = False
        self._session.resume()
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._session.poll()
        status = self._session.status()
        self.last_safety_limit = status.safety_limit
        self._last_auto_advance_interval_seconds = status.auto_advance_interval_seconds
        self.last_auto_advance_interval_seconds = status.auto_advance_interval_seconds
        if self._safety_limit_spin.value() != status.safety_limit:
            self._safety_limit_spin.blockSignals(True)
            self._safety_limit_spin.setValue(status.safety_limit)
            self._safety_limit_spin.blockSignals(False)
        if self._auto_scroll_interval_spin.value() != status.auto_advance_interval_seconds:
            self._auto_scroll_interval_spin.blockSignals(True)
            self._auto_scroll_interval_spin.setValue(status.auto_advance_interval_seconds)
            self._auto_scroll_interval_spin.blockSignals(False)
        self._count_label.setText(f"{status.collected_count} 条")
        self._page_label.setText(f"{status.pages_fetched} 页")
        self._refresh_time_labels_if_needed(status)
        self._status_label.setText(self._display_status_text(status))
        auto_scroll_active = status.running and not status.completed
        controls_ready = not self._startup_pending
        self._auto_scroll_button.setEnabled(
            controls_ready and auto_scroll_active and not self._is_saving()
        )
        self._auto_scroll_button.setText(
            "暂停自动滚动" if status.auto_scroll_enabled else "继续自动滚动"
        )
        self._stop_button.setEnabled(controls_ready and status.running and not self._is_saving())
        has_rows = status.collected_count > 0
        self._save_create_button.setEnabled(controls_ready and has_rows and not self._is_saving())
        self._safety_limit_spin.setEnabled(controls_ready and not self._is_saving())
        self._auto_scroll_interval_spin.setEnabled(controls_ready and not self._is_saving())
        self._maybe_handle_interrupt(status)

    def _refresh_time_labels_if_needed(self, status) -> None:
        now = datetime.now(UTC)
        should_refresh = self._last_time_label_refresh_at is None
        if not should_refresh:
            should_refresh = (now - self._last_time_label_refresh_at) >= timedelta(
                seconds=TIME_LABEL_REFRESH_INTERVAL_SECONDS
            )
        if status.completed or (not status.running and status.ended_at is not None):
            should_refresh = True
        if self._startup_pending:
            should_refresh = True
        if not should_refresh:
            return
        self._elapsed_label.setText(self._format_finish_time(status.estimated_end_at, status))
        self._eta_label.setText(self._format_remaining(status))
        self._last_time_label_refresh_at = now

    def _update_safety_limit(self, value: int) -> None:
        self._session.update_safety_limit(value)
        self.last_safety_limit = value
        self._refresh_status()

    def _update_auto_scroll_interval(self, value: float) -> None:
        self._session.update_auto_advance_interval(value)
        self._last_auto_advance_interval_seconds = value
        self.last_auto_advance_interval_seconds = value
        self._refresh_status()

    def _toggle_auto_scroll(self) -> None:
        status = self._session.status()
        if status.auto_scroll_enabled:
            self._session.pause_auto_scroll()
        else:
            self._session.resume_auto_scroll()
        self._refresh_status()

    def _stop_collection(self) -> None:
        result = QMessageBox.question(
            self,
            "确认结束采集",
            "结束后将关闭当前浏览器采集会话，但已采集数据仍可导出或创建任务。是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self._session.stop()
        self._refresh_status()

    def _save_and_create_task(self, *, close_when_cancelled: bool = False) -> None:
        if self._is_saving():
            return
        default_path = self._default_export_path()
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存采集结果",
            str(default_path),
            "Excel Workbook (*.xlsx)",
        )
        if not selected:
            if close_when_cancelled:
                self._force_close = True
                self.close()
            return
        export_path = Path(selected)
        if export_path.suffix.lower() != ".xlsx":
            export_path = export_path.with_suffix(".xlsx")
        rows = self._session.status().rows
        if not rows:
            QMessageBox.warning(self, "无法保存", "当前没有可保存的采集数据。")
            if close_when_cancelled:
                self._force_close = True
                self.close()
            return
        self._save_export_dir(export_path.parent)
        self._save_target_path = export_path
        self._closing_after_save = close_when_cancelled
        self._start_save_task(rows, export_path)

    def _maybe_handle_interrupt(self, status) -> None:
        if (
            self._handling_interrupt
            or not self._collection_started
            or not status.running
            or status.completed
            or not status.interrupted
            or not status.last_message
        ):
            return
        if status.last_message == self._last_interrupt_message:
            return
        self._handling_interrupt = True
        self._last_interrupt_message = status.last_message
        result = QMessageBox.question(
            self,
            "采集中断",
            f"{status.last_message}\n\n点击“继续”后程序将自动恢复连续采集；点击“结束”将保留当前已采集数据。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        self._handling_interrupt = False
        if result == QMessageBox.StandardButton.Yes:
            self._session.resume_auto_scroll()
        else:
            self._session.stop()
        self._refresh_status()

    def closeEvent(self, event) -> None:
        if self._force_close:
            self._force_close = False
            super().closeEvent(event)
            return
        if self._is_saving():
            QMessageBox.information(self, "正在保存", "保存进行中，请等待完成后再关闭窗口。")
            event.ignore()
            return
        status = self._session.status()
        self.last_safety_limit = status.safety_limit
        self.last_auto_advance_interval_seconds = status.auto_advance_interval_seconds
        if not status.auto_scroll_enabled or not status.running or status.completed:
            event.ignore()
            self._save_and_create_task(close_when_cancelled=True)
            return
        if status.running:
            result = QMessageBox.question(
                self,
                "确认关闭",
                "关闭窗口将停止当前采集。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._session.stop()
        super().closeEvent(event)

    def _format_finish_time(self, estimated_end_at: datetime | None, status) -> str:
        if status.completed and status.ended_at is not None:
            return status.ended_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if estimated_end_at is None:
            return "-"
        return estimated_end_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _format_remaining(self, status) -> str:
        if status.completed or (not status.running and status.ended_at is not None):
            return "00:00:00"
        if status.estimated_end_at is None:
            return "-"
        remaining = max(
            int((status.estimated_end_at - datetime.now(UTC)).total_seconds()),
            0,
        )
        hours, remainder = divmod(remaining, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _display_status_text(self, status) -> str:
        if self._is_saving():
            return "保存中"
        if self._startup_pending:
            return "启动中"
        if status.completed or not status.running:
            return "已结束"
        if status.auto_scroll_enabled:
            return "采集中"
        return "暂停滚动中"

    def _default_export_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = DEFAULT_COLLECTION_EXPORT_NAME.format(timestamp=timestamp)
        saved_dir = self._load_saved_export_dir()
        return (saved_dir or Path.cwd()) / filename

    def _load_saved_export_dir(self) -> Path | None:
        raw = self._app_service.load_app_setting("creator_collection_export_dir")
        if isinstance(raw, str) and raw.strip():
            return Path(raw)
        return None

    def _save_export_dir(self, directory: Path) -> None:
        self._app_service.save_app_setting("creator_collection_export_dir", str(directory))

    def _start_save_task(self, rows: list[dict[str, object]], export_path: Path) -> None:
        self._save_thread = QThread(self)
        self._save_worker = _SaveTaskWorker(
            rows=rows,
            export_path=export_path,
            browser_config_id=self._browser_config.config_id,
            app_service=self._app_service,
        )
        self._save_worker.moveToThread(self._save_thread)
        self._save_thread.started.connect(self._save_worker.run)
        self._save_worker.finished.connect(self._handle_save_success)
        self._save_worker.failed.connect(self._handle_save_failure)
        self._save_worker.finished.connect(self._save_thread.quit)
        self._save_worker.failed.connect(self._save_thread.quit)
        self._save_thread.finished.connect(self._cleanup_save_thread)
        self._refresh_status()
        self._save_thread.start()

    def _handle_save_success(self, task_id: int, _saved_path: str) -> None:
        self.created_task_id = task_id
        self._session.stop()
        QMessageBox.information(self, "已创建任务", f"已保存并创建检查任务 #{task_id}")
        self.accept()

    def _handle_save_failure(self, message: str) -> None:
        QMessageBox.warning(self, "采集任务创建失败", message)
        self._refresh_status()

    def _cleanup_save_thread(self) -> None:
        if self._save_worker is not None:
            self._save_worker.deleteLater()
        if self._save_thread is not None:
            self._save_thread.deleteLater()
        self._save_worker = None
        self._save_thread = None
        self._save_target_path = None
        self._closing_after_save = False
        self._refresh_status()

    def _is_saving(self) -> bool:
        return self._save_thread is not None and self._save_thread.isRunning()


def _create_action_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def _add_summary_row(
    layout: QGridLayout,
    row: int,
    column_group: int,
    title: str,
    value_label: QLabel,
    *,
    stretch: bool = False,
) -> None:
    title_label = QLabel(title)
    title_label.setStyleSheet("color: #666666;")
    layout.addWidget(title_label, row, column_group * 2)
    layout.addWidget(value_label, row, column_group * 2 + 1)
    if stretch:
        value_label.setWordWrap(True)
