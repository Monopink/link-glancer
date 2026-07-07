from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from creator_collector.models import CreatorCollectionConfig
from creator_collector.service import DEFAULT_SAFETY_LIMIT, CreatorCollectorSession
from link_glancer.application import TaskApplicationService
from link_glancer.tasks.models import BrowserConfig

DEFAULT_CREATOR_PAGE_URL = ""


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
        self._handling_interrupt = False
        self._last_interrupt_message: str | None = None
        self.created_task_id: int | None = None
        self.last_safety_limit = session.status().safety_limit
        self._last_auto_advance_interval_seconds = session.status().auto_advance_interval_seconds
        self.last_auto_advance_interval_seconds = self._last_auto_advance_interval_seconds

        self.setWindowTitle("采集中")
        self.resize(620, 280)

        root = QVBoxLayout(self)

        summary_top = QHBoxLayout()
        self._count_label = QLabel("已采集 0 条")
        self._page_label = QLabel("已抓取 0 页")
        self._elapsed_label = QLabel("时长 -")
        self._eta_label = QLabel("预计结束 -")
        summary_top.addWidget(self._count_label)
        summary_top.addWidget(self._page_label)
        summary_top.addWidget(self._elapsed_label)
        summary_top.addWidget(self._eta_label)
        summary_top.addStretch(1)
        root.addLayout(summary_top)

        summary_mid = QHBoxLayout()
        self._status_label = QLabel("状态：等待开始")
        self._status_label.setWordWrap(True)
        summary_mid.addWidget(self._status_label, stretch=1)
        root.addLayout(summary_mid)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(divider)

        summary_bottom = QHBoxLayout()
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
        summary_bottom.addWidget(QLabel("安全上限"))
        summary_bottom.addWidget(self._safety_limit_spin)
        summary_bottom.addSpacing(16)
        summary_bottom.addWidget(QLabel("自动滚动间隔"))
        summary_bottom.addWidget(self._auto_scroll_interval_spin)
        summary_bottom.addStretch(1)
        root.addLayout(summary_bottom)

        hint_label = QLabel(
            "采集中窗口只负责进度显示和自动滚动控制。发生中断时会弹窗提示，"
            "点击“继续”后程序会恢复自动连续采集。"
        )
        hint_label.setWordWrap(True)
        hint_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root.addWidget(hint_label)

        self._pause_button = _create_action_button("暂停自动滚动")
        self._resume_button = _create_action_button("继续自动滚动")
        self._stop_button = _create_action_button("结束")
        self._save_create_button = _create_action_button("保存并创建任务")
        self._pause_button.clicked.connect(self._pause_auto_scroll)
        self._resume_button.clicked.connect(self._resume_auto_scroll)
        self._stop_button.clicked.connect(self._stop_collection)
        self._save_create_button.clicked.connect(self._save_and_create_task)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        actions.addWidget(self._pause_button)
        actions.addWidget(self._resume_button)
        actions.addWidget(self._stop_button)
        actions.addStretch(1)
        actions.addWidget(self._save_create_button)
        root.addLayout(actions)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()
        if self._auto_start:
            QTimer.singleShot(0, self._begin_collection)

    def _begin_collection(self) -> None:
        if self._collection_started:
            return
        self._collection_started = True
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
        self._count_label.setText(f"已采集 {status.collected_count} 条")
        self._page_label.setText(f"已抓取 {status.pages_fetched} 页")
        self._elapsed_label.setText(f"时长 {self._format_elapsed(status.started_at)}")
        self._eta_label.setText(f"预计结束 {self._format_eta(status.estimated_end_at)}")
        self._status_label.setText(f"状态：{status.last_message}")
        self._pause_button.setEnabled(
            status.running and status.auto_scroll_enabled and not status.completed
        )
        self._resume_button.setEnabled(
            status.running and not status.auto_scroll_enabled and not status.completed
        )
        self._stop_button.setEnabled(status.running)
        has_rows = status.collected_count > 0
        self._save_create_button.setEnabled(has_rows)
        self._maybe_handle_interrupt(status)

    def _update_safety_limit(self, value: int) -> None:
        self._session.update_safety_limit(value)
        self.last_safety_limit = value
        self._refresh_status()

    def _update_auto_scroll_interval(self, value: float) -> None:
        self._session.update_auto_advance_interval(value)
        self._last_auto_advance_interval_seconds = value
        self.last_auto_advance_interval_seconds = value
        self._refresh_status()

    def _pause_auto_scroll(self) -> None:
        self._session.pause_auto_scroll()
        self._refresh_status()

    def _resume_auto_scroll(self) -> None:
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

    def _save_and_create_task(self) -> None:
        default_name = datetime.now().strftime("creator_collection_%Y%m%d_%H%M%S.xlsx")
        QMessageBox.information(
            self,
            "保存采集结果",
            "请先选择采集结果的保存位置，随后程序会基于该文件直接创建检查任务。",
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存采集结果",
            str(Path.cwd() / default_name),
            "Excel Workbook (*.xlsx)",
        )
        if not selected:
            return
        try:
            export_path = self._session.finish_to_path(Path(selected))
            task_id = self._app_service.create_task_from_creator_collection(
                source_path=export_path,
                browser_config_id=self._browser_config.config_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "采集任务创建失败", str(exc))
            return
        self.created_task_id = task_id
        self._session.stop()
        QMessageBox.information(self, "已创建任务", f"已保存并创建检查任务 #{task_id}")
        self.accept()

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
        status = self._session.status()
        self.last_safety_limit = status.safety_limit
        self.last_auto_advance_interval_seconds = status.auto_advance_interval_seconds
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

    def _format_elapsed(self, started_at: datetime | None) -> str:
        if started_at is None:
            return "-"
        elapsed = max(int((datetime.now(UTC) - started_at).total_seconds()), 0)
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _format_eta(self, estimated_end_at: datetime | None) -> str:
        if estimated_end_at is None:
            return "-"
        return estimated_end_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _create_action_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button
