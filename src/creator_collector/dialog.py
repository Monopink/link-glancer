from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_collector.exporter import EXPORT_HEADERS, flatten_creator_row
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

        session.resume()
        progress_dialog = CreatorCollectorProgressDialog(
            app_service=self._app_service,
            browser_config=browser_config,
            session=session,
            parent=self,
        )
        progress_dialog.exec()
        self.created_task_id = progress_dialog.created_task_id
        self._default_safety_limit = progress_dialog.last_safety_limit
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

    def _save_defaults(self) -> None:
        self._app_service.save_app_setting(
            "creator_collection_defaults",
            {
                "browser_config_id": str(self._browser_combo.currentData() or ""),
                "page_url": self._page_url_edit.text().strip(),
                "safety_limit": self._default_safety_limit,
            },
        )


class CreatorCollectorProgressDialog(QDialog):
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        browser_config: BrowserConfig,
        session: CreatorCollectorSession,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_service = app_service
        self._browser_config = browser_config
        self._session = session
        self.created_task_id: int | None = None
        self.last_safety_limit = session.status().safety_limit

        self.setWindowTitle("采集中")
        self.resize(1180, 760)

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

        summary_bottom = QHBoxLayout()
        self._status_label = QLabel("状态 -")
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(True)
        self._safety_limit_spin = QSpinBox()
        self._safety_limit_spin.setRange(1, 100000)
        self._safety_limit_spin.setValue(self.last_safety_limit)
        self._safety_limit_spin.valueChanged.connect(self._update_safety_limit)
        summary_bottom.addWidget(self._status_label, stretch=1)
        summary_bottom.addWidget(QLabel("安全上限"))
        summary_bottom.addWidget(self._safety_limit_spin)
        root.addLayout(summary_bottom)

        self._table = QTableWidget(0, len(EXPORT_HEADERS))
        self._table.setHorizontalHeaderLabels(EXPORT_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        for column in range(len(EXPORT_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        if EXPORT_HEADERS:
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._table, stretch=1)

        self._pause_button = _create_action_button("暂停")
        self._stop_button = _create_action_button("停止")
        self._resume_button = _create_action_button("继续")
        self._export_button = _create_action_button("导出到...")
        self._save_create_button = _create_action_button("保存并创建任务")
        self._pause_button.clicked.connect(self._pause_collection)
        self._stop_button.clicked.connect(self._stop_collection)
        self._resume_button.clicked.connect(self._resume_collection)
        self._export_button.clicked.connect(self._export_to_directory)
        self._save_create_button.clicked.connect(self._save_and_create_task)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self._pause_button)
        actions.addWidget(self._stop_button)
        actions.addWidget(self._resume_button)
        actions.addWidget(self._export_button)
        actions.addWidget(self._save_create_button)
        root.addLayout(actions)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._session.poll()
        status = self._session.status()
        self.last_safety_limit = status.safety_limit
        if self._safety_limit_spin.value() != status.safety_limit:
            self._safety_limit_spin.blockSignals(True)
            self._safety_limit_spin.setValue(status.safety_limit)
            self._safety_limit_spin.blockSignals(False)
        self._count_label.setText(f"已采集 {status.collected_count} 条")
        self._page_label.setText(f"已抓取 {status.pages_fetched} 页")
        self._elapsed_label.setText(f"时长 {self._format_elapsed(status.started_at)}")
        self._eta_label.setText(f"预计结束 {self._format_eta(status.estimated_end_at)}")
        self._status_label.setText(f"状态 {status.last_message or '-'}")
        self._fill_table(status.rows)
        self._pause_button.setEnabled(status.running and not status.paused and not status.completed)
        self._resume_button.setEnabled(status.running and status.paused and not status.completed)
        self._stop_button.setEnabled(status.running)
        has_rows = status.collected_count > 0
        self._export_button.setEnabled(has_rows)
        self._save_create_button.setEnabled(has_rows)

    def _fill_table(self, rows: list[dict[str, object]]) -> None:
        self._table.setRowCount(len(rows))
        for row_index, source_row in enumerate(rows):
            flattened = flatten_creator_row(source_row)
            for column, header in enumerate(EXPORT_HEADERS):
                value = flattened.get(header, "")
                self._table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def _update_safety_limit(self, value: int) -> None:
        self._session.update_safety_limit(value)
        self.last_safety_limit = value
        self._refresh_status()

    def _pause_collection(self) -> None:
        self._session.pause()
        self._refresh_status()

    def _resume_collection(self) -> None:
        self._session.resume()
        self._refresh_status()

    def _stop_collection(self) -> None:
        result = QMessageBox.question(
            self,
            "确认停止采集",
            "停止后将关闭当前浏览器采集会话，但已采集数据仍可导出或创建任务。是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self._session.stop()
        self._refresh_status()

    def _export_to_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择导出目录", str(Path.cwd()))
        if not selected:
            return
        try:
            export_path = self._session.finish(Path(selected))
        except ValueError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"已导出到：{export_path}")
        self._refresh_status()

    def _save_and_create_task(self) -> None:
        try:
            export_path = self._session.finish()
            task_id = self._app_service.create_task_from_creator_collection(
                source_path=export_path,
                browser_config_id=self._browser_config.config_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "采集任务创建失败", str(exc))
            return
        self.created_task_id = task_id
        self._session.stop()
        QMessageBox.information(self, "已创建任务", f"已导出并创建审核任务 #{task_id}")
        self.accept()

    def closeEvent(self, event) -> None:
        status = self._session.status()
        self.last_safety_limit = status.safety_limit
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
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return button
