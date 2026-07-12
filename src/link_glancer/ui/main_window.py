from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from creator_collector import CreatorCollectorDialog
from link_glancer.application import TaskApplicationService
from link_glancer.browser.base import BrowserController, BrowserLaunchRequest, BufferBlock
from link_glancer.browser.detector import detect_browser
from link_glancer.browser.service import create_browser_controller
from link_glancer.runtime.locks import (
    RuntimeLockConflictError,
    RuntimeLockHandle,
    acquire_profile_lock,
    acquire_task_lock,
)
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.database import consume_database_reset_reason
from link_glancer.tasks.models import (
    BrowserConfig,
    ReviewField,
    TaskDetail,
    TaskSnapshot,
    TaskStatus,
)
from link_glancer.ui.config_manager_dialog import ConfigManagerDialog
from link_glancer.ui.review_window import ReviewWindow
from link_glancer.ui.task_creation_dialog import TaskCreationDialog


class _TaskMutationWorker(QObject):
    succeeded = Signal(int, str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        operation: str,
        source_path: Path | None = None,
        task_snapshot: TaskSnapshot | None = None,
        task_id: int | None = None,
        review_field_library: list[ReviewField] | None = None,
    ) -> None:
        super().__init__()
        self._app_service = app_service
        self._operation = operation
        self._source_path = source_path
        self._task_snapshot = task_snapshot
        self._task_id = task_id
        self._review_field_library = review_field_library

    def run(self) -> None:
        try:
            if self._operation == "create":
                if self._source_path is None or self._task_snapshot is None:
                    raise ValueError("缺少创建任务所需参数。")
                result_task_id = self._app_service.create_task(
                    source_path=self._source_path,
                    task_snapshot=self._task_snapshot,
                    review_field_library=self._review_field_library,
                )
                self.succeeded.emit(result_task_id, "任务已创建")
                return
            if self._operation == "update_config":
                if self._task_id is None or self._task_snapshot is None:
                    raise ValueError("缺少更新任务配置所需参数。")
                self._app_service.update_task_configuration(
                    task_id=self._task_id,
                    task_snapshot=self._task_snapshot,
                    review_field_library=self._review_field_library,
                )
                self.succeeded.emit(self._task_id, "任务配置已保存")
                return
            raise ValueError(f"不支持的任务操作：{self._operation}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    _PAGE_LAYOUT_MARGINS = (9, 9, 9, 9)
    _PAGE_LAYOUT_SPACING = 6

    def __init__(self, *, instance_id: int) -> None:
        super().__init__()
        self._instance_id = instance_id
        self.app_service = TaskApplicationService.create_default()
        self.browser: BrowserController = create_browser_controller()
        self.task: TaskDetail | None = None
        self._task_table_ids: list[int] = []
        self._editing_task_index: int | None = None
        self._review_window: ReviewWindow | None = None
        self._confirmation_task_id: int | None = None
        self._handling_browser_block = False
        self._task_worker_thread: QThread | None = None
        self._task_worker: _TaskMutationWorker | None = None
        self._task_progress_dialog: QProgressDialog | None = None
        self._pending_task_success_message: str | None = None
        self._review_task_lock: RuntimeLockHandle | None = None
        self._review_profile_lock: RuntimeLockHandle | None = None
        self._browser_launch_processes: dict[str, tuple[QProcess, RuntimeLockHandle]] = {}

        self._update_window_title()
        self.resize(1120, 720)
        self.setMinimumWidth(980)

        self._build_toolbar()
        self._build_pages()
        self.setStatusBar(QStatusBar(self))
        self._show_start_page()
        QTimer.singleShot(0, self._show_database_reset_notice)

    def _show_database_reset_notice(self) -> None:
        reason = consume_database_reset_reason()
        if reason is None:
            return
        QMessageBox.information(
            self,
            "数据库已重建",
            "检测到不兼容的旧数据库，已按当前版本重建。\n"
            "旧任务数据已清空，浏览器配置的独立环境目录未删除。\n\n"
            f"原因：{reason}",
        )

    def _build_toolbar(self) -> None:
        self._toolbar = QToolBar("任务")
        self._toolbar.setMovable(False)
        self.addToolBar(self._toolbar)
        self._toolbar.hide()

    def _build_pages(self) -> None:
        self._stack = QStackedWidget()
        self._start_page = self._build_start_page()
        self._task_page = self._build_task_page()
        self._stack.addWidget(self._start_page)
        self._stack.addWidget(self._task_page)
        self.setCentralWidget(self._stack)

    def _build_start_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._configure_page_layout(layout)

        layout.addWidget(QLabel("任务列表"))

        self._task_table = QTableWidget(0, 6)
        self._task_table.setHorizontalHeaderLabels(
            ["ID", "状态", "进度", "任务", "更新时间", "源文件"]
        )
        self._task_table.verticalHeader().setVisible(False)
        self._configure_table_widget(self._task_table, focus_policy=Qt.FocusPolicy.NoFocus)
        self._task_table.doubleClicked.connect(self._open_selected_task_from_table)
        self._task_table.itemSelectionChanged.connect(self._update_start_page_actions)
        header_view = self._task_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._task_table, stretch=1)

        self._task_empty_state_label = QLabel("暂无任务，请先新建任务或新建采集任务。")
        self._task_empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._task_empty_state_label.setWordWrap(True)
        layout.addWidget(self._task_empty_state_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self._new_task_button = QPushButton("新建任务")
        self._new_task_button.clicked.connect(self._create_task)
        actions.addWidget(self._new_task_button)
        self._new_collection_task_button = QPushButton("开始采集")
        self._new_collection_task_button.clicked.connect(self._create_collection_task)
        actions.addWidget(self._new_collection_task_button)
        self._browser_configs_button = QPushButton("浏览器配置")
        self._browser_configs_button.clicked.connect(self._show_browser_configs)
        actions.addWidget(self._browser_configs_button)
        actions.addStretch(1)
        self._delete_task_button = QPushButton("删除任务")
        self._delete_task_button.clicked.connect(self._delete_selected_task_from_table)
        actions.addWidget(self._delete_task_button)
        self._open_task_button = QPushButton("打开任务")
        self._open_task_button.clicked.connect(self._open_selected_task_from_table)
        self._open_task_button.setAutoDefault(True)
        self._open_task_button.setDefault(True)
        actions.addWidget(self._open_task_button)
        layout.addLayout(actions)
        return page

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._configure_page_layout(layout)

        self._task_title_label = QLabel("任务")
        self._task_title_label.setTextFormat(Qt.TextFormat.PlainText)
        self._task_title_label.setWordWrap(True)
        layout.addWidget(self._task_title_label)

        self._task_data_table = QTableWidget(0, 0)
        self._configure_table_widget(
            self._task_data_table,
            focus_policy=Qt.FocusPolicy.StrongFocus,
        )
        self._task_data_table.horizontalHeader().setSectionsMovable(False)
        self._task_data_table.doubleClicked.connect(self._confirm_jump_task_progress_from_table)
        layout.addWidget(self._task_data_table, stretch=1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self._back_to_list_button = QPushButton("返回任务列表")
        self._back_to_list_button.clicked.connect(self._show_start_page)
        config_button = QPushButton("任务配置")
        config_button.clicked.connect(self._edit_task_configuration)
        export_button = QPushButton("导出表格")
        export_button.clicked.connect(self._export_task_with_dialog)
        delete_button = QPushButton("删除任务")
        delete_button.clicked.connect(self._delete_current_task)
        start_button = QPushButton("开始检查")
        start_button.clicked.connect(self._start_review_flow)
        start_button.setAutoDefault(True)
        start_button.setDefault(True)
        actions.addWidget(self._back_to_list_button)
        actions.addWidget(config_button)
        actions.addStretch(1)
        actions.addWidget(export_button)
        actions.addWidget(delete_button)
        actions.addWidget(start_button)
        layout.addLayout(actions)
        return page

    def _configure_table_widget(
        self,
        table: QTableWidget,
        *,
        focus_policy: Qt.FocusPolicy,
    ) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setFocusPolicy(focus_policy)
        table.setStyleSheet(
            "QTableView { outline: 0; } "
            "QTableView::item:selected { "
            "border: 0px; "
            "background-color: #2563eb; "
            "color: #ffffff; "
            "}"
        )
        table.setAlternatingRowColors(True)

    def _configure_page_layout(self, layout: QVBoxLayout) -> None:
        layout.setContentsMargins(*self._PAGE_LAYOUT_MARGINS)
        layout.setSpacing(self._PAGE_LAYOUT_SPACING)

    def _summary_row(self, title: str, value_widget: QLabel) -> QWidget:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(f"{title}:"))
        row.addWidget(value_widget, stretch=1)
        return row_widget

    def _create_task(self) -> None:
        last_source_path, last_snapshot = self.app_service.load_last_task_creation_defaults()
        dialog = TaskCreationDialog(
            browser_configs=self.app_service.list_browser_configs(),
            app_service=self.app_service,
            source_path=last_source_path,
            initial_snapshot=last_snapshot,
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        if dialog.source_path is None or dialog.task_snapshot is None:
            return
        self._start_task_mutation(
            operation="create",
            progress_title="创建任务中",
            progress_label="正在读取 Excel 并写入任务数据，请稍候...",
            source_path=dialog.source_path,
            task_snapshot=dialog.task_snapshot,
            review_field_library=dialog.review_field_library,
        )

    def _show_browser_configs(self) -> None:
        dialog = ConfigManagerDialog(
            browser_configs=self.app_service.list_browser_configs(),
            browser_test_callback=self._test_browser_config,
            browser_launch_callback=self._launch_browser_for_config,
            save_browser_config_callback=self.app_service.save_browser_config,
            save_browser_profile_callback=self.app_service.save_browser_profile,
            delete_browser_config_callback=self.app_service.delete_browser_config,
            delete_browser_profile_callback=self.app_service.delete_browser_profile,
            parent=self,
        )
        dialog.exec()

    def _create_collection_task(self) -> None:
        dialog = CreatorCollectorDialog(
            app_service=self.app_service,
            browser_configs=self.app_service.list_browser_configs(),
            instance_id=self._instance_id,
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        if dialog.last_created_task_id is None:
            return
        self._show_start_page(selected_task_id=dialog.last_created_task_id)
        self.statusBar().showMessage(
            f"采集完成并创建任务：#{dialog.last_created_task_id}",
            4000,
        )

    def _edit_task_configuration(self) -> None:
        if not self.task:
            return
        dialog = TaskCreationDialog(
            browser_configs=self.app_service.list_browser_configs(),
            app_service=self.app_service,
            source_path=self.task.source_file_path,
            initial_snapshot=self.task.task_snapshot,
            dialog_title="任务配置",
            accept_button_text="保存",
            source_editable=False,
            review_field_id_editable=False,
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        if dialog.task_snapshot is None:
            return

        reimport_required = self._task_source_structure_changed(
            self.task.task_snapshot,
            dialog.task_snapshot,
        )
        if reimport_required:
            message_lines = ["保存后将重置当前任务中的已保存检查结果。"]
            message_lines.append("由于修改了 Sheet 或标题行，任务数据将按新配置重新导入。")
            result = QMessageBox.question(
                self,
                "确认保存任务配置",
                "\n".join(message_lines) + "\n\n是否继续？",
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        self._shutdown_confirmation_browser()
        self._start_task_mutation(
            operation="update_config",
            progress_title="保存任务配置中",
            progress_label="正在按新配置处理任务数据，请稍候...",
            task_id=self.task.task_id,
            task_snapshot=dialog.task_snapshot,
            review_field_library=dialog.review_field_library,
        )

    def _load_task(self, task_id: int) -> None:
        self._shutdown_confirmation_browser()
        try:
            self.task = self.app_service.load_task(task_id)
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "任务错误", str(exc))
            return
        self._editing_task_index = None
        self._show_task_page()

    def _show_start_page(self, *, selected_task_id: int | None = None) -> None:
        self._shutdown_confirmation_browser()
        self.task = None
        self._editing_task_index = None
        self._toolbar.hide()
        self._stack.setCurrentWidget(self._start_page)
        self._refresh_start_page(selected_task_id=selected_task_id)

    def _show_task_page(self) -> None:
        self.task = self.app_service.load_task(self.task.task_id) if self.task else None
        self._toolbar.hide()
        self._stack.setCurrentWidget(self._task_page)
        self._refresh_task_page()

    def _refresh_start_page(self, *, selected_task_id: int | None = None) -> None:
        selected_task_id = (
            selected_task_id if selected_task_id is not None else self._selected_task_id()
        )
        summaries = self.app_service.list_tasks()
        self._task_empty_state_label.setVisible(not summaries)
        self._task_table_ids = [summary.task_id for summary in summaries]
        self._task_table.setRowCount(len(summaries))
        for row, summary in enumerate(summaries):
            progress_completed = self._display_completed_items(
                completed_items=summary.completed_items,
                current_task_index=summary.current_task_index,
                total_items=summary.total_items,
            )
            values = [
                str(summary.task_id),
                self._task_status_badge(summary.status),
                f"{progress_completed}/{summary.total_items}",
                summary.name,
                self._format_task_timestamp(summary.updated_at),
                summary.source_file_name,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 1, 2):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._task_table.setItem(row, column, item)
        self._select_task_in_table(selected_task_id)
        self._update_start_page_actions()

    def _select_task_in_table(self, task_id: int | None) -> None:
        if task_id is None:
            task_id = self._selected_task_id()
        if task_id is None:
            self._task_table.clearSelection()
            return
        try:
            row = self._task_table_ids.index(task_id)
        except ValueError:
            self._task_table.clearSelection()
            return
        self._task_table.selectRow(row)
        item = self._task_table.item(row, 0)
        if item is not None:
            self._task_table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _open_selected_task_from_table(self, *_args: object) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        self._load_task(task_id)

    def _delete_selected_task_from_table(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        result = QMessageBox.question(
            self,
            "确认删除任务",
            f"将删除任务 #{task_id} 及其所有检查结果，是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.app_service.delete_task(task_id)
        self._refresh_start_page()

    def _delete_current_task(self) -> None:
        if not self.task:
            return
        result = QMessageBox.question(
            self,
            "确认删除任务",
            f"将删除任务 #{self.task.task_id} 及其所有检查结果，是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        task_id = self.task.task_id
        self._shutdown_confirmation_browser()
        self.app_service.delete_task(task_id)
        self._show_start_page()

    def _selected_task_id(self) -> int | None:
        selected_ranges = self._task_table.selectedRanges()
        if not selected_ranges:
            return None
        row = selected_ranges[0].topRow()
        if row < 0 or row >= len(self._task_table_ids):
            return None
        return self._task_table_ids[row]

    def _update_start_page_actions(self) -> None:
        if not hasattr(self, "_task_table"):
            self._open_task_button.setEnabled(False)
            self._delete_task_button.setEnabled(False)
            return
        has_selection = self._selected_task_id() is not None
        self._open_task_button.setEnabled(has_selection)
        self._delete_task_button.setEnabled(has_selection)

    def _refresh_task_page(self) -> None:
        if not self.task:
            return
        self.task = self.app_service.load_task(self.task.task_id)
        self._task_title_label.setText(
            f"#{self.task.task_id}  ·  {self.task.name}  ·  {self._task_summary_text(self.task)}"
        )
        self._refresh_task_data_table()

    def _start_review_flow(self) -> None:
        if not self.task:
            return
        if self.task.current_item is None:
            QMessageBox.information(
                self,
                "任务已完成",
                "当前任务已完成。如需复核，请先跳转到目标条目。",
            )
            return
        if not self._has_usable_buffer_urls():
            QMessageBox.warning(
                self,
                "无法开始",
                "当前检查缓冲区没有可用的 URL。\n请确认采集结果中包含有效的 url 列。",
            )
            return
        confirm_url = self._resolve_confirmation_url()
        if not confirm_url:
            QMessageBox.warning(self, "无法开始", "没有可用于浏览器确认的 URL。")
            return
        if self._review_window is not None and self._review_window.isVisible():
            self._review_window.activateWindow()
            self._review_window.raise_()
            return
        self._release_review_locks()
        try:
            self._review_task_lock = acquire_task_lock(
                self.task.task_id,
                owner_label=f"检查任务 - Profile: {self.task.browser_config.name}",
            )
            self._review_profile_lock = acquire_profile_lock(
                self.task.browser_config.profile_id,
                owner_label=f"检查任务 - Profile: {self.task.browser_config.name}",
            )
        except RuntimeLockConflictError as exc:
            self._release_review_locks()
            QMessageBox.warning(self, "无法开始检查", str(exc))
            return
        request = BrowserLaunchRequest(
            browser_config_id=self.task.browser_config.profile_id,
            browser_name="configured",
            executable_path=self.task.browser_config.executable_path or None,
            launch_args=self.task.browser_config.launch_args,
        )
        self._update_window_title(profile_name=self.task.browser_config.name)
        self.browser.launch(request)
        self.browser.ensure_running()
        status = self.browser.status()
        if not status.running:
            self._confirmation_task_id = None
            self._release_review_locks()
            self._update_window_title()
            self._refresh_task_page()
            QMessageBox.warning(self, "浏览器启动失败", status.message)
            return
        self.browser.open_confirmation_page(confirm_url)
        result = QMessageBox.question(
            self,
            "确认开始检查",
            "请确认网页已正常打开，并完成登录或验证码等准备。\n\n是否开始检查？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if result != QMessageBox.StandardButton.Yes:
            self.browser.shutdown()
            self._confirmation_task_id = None
            self._release_review_locks()
            self._update_window_title()
            self.statusBar().showMessage("已取消开始检查。", 3000)
            return
        self._confirmation_task_id = self.task.task_id
        self.task = self.app_service.mark_task_in_progress(self.task.task_id)
        self._enter_review_mode()

    def _enter_review_mode(self) -> None:
        if not self.task:
            return
        self.task = self.app_service.load_task(self.task.task_id)
        if self.task.current_item is None:
            QMessageBox.information(self, "无任务", "当前没有可检查的条目。")
            return
        self.browser.close_confirmation_page()
        self._confirmation_task_id = None
        if not self._sync_browser_buffer():
            return
        self._review_window = ReviewWindow(
            task_id=self.task.task_id,
            app_service=self.app_service,
            browser=self.browser,
            on_close=self._handle_review_window_closed,
        )
        self._review_window.show()

    def _handle_review_window_closed(self) -> None:
        self._review_window = None
        self._confirmation_task_id = None
        self._release_review_locks()
        self._update_window_title()
        if self.task is not None:
            self.task = self.app_service.load_task(self.task.task_id)
            self._refresh_task_page()

    def _refresh_task_data_table(self) -> None:
        if not self.task:
            return
        items = self.app_service.list_all_items(self.task.task_id)
        reviews = self.app_service.list_reviews(self.task.task_id)
        table_fields = self.app_service.task_table_field_names(self.task, items)
        self._task_data_table.clear()
        self._task_data_table.setColumnCount(len(table_fields))
        self._task_data_table.setHorizontalHeaderLabels(table_fields)
        self._task_data_table.setRowCount(len(items))
        self._task_data_table.setVerticalHeaderLabels([str(item.task_index) for item in items])

        palette = self.palette()
        completed_background = palette.alternateBase().color()
        current_background = QColor("#1d4ed8" if self.is_dark_mode() else "#dbeafe")
        current_foreground = QColor("#ffffff") if self.is_dark_mode() else palette.text().color()

        for row, item in enumerate(items):
            review = reviews.get(item.task_item_id)
            is_current = item.task_index == self.task.current_task_index
            is_completed = review is not None
            for column, field_name in enumerate(table_fields):
                value = self._resolve_table_value(
                    field_name,
                    item.task_data,
                    review.review_data if review else {},
                )
                if isinstance(value, list):
                    display_value = ", ".join(str(part) for part in value)
                elif value in ("", None):
                    display_value = ""
                else:
                    display_value = str(value)
                table_item = QTableWidgetItem(display_value)
                if is_current:
                    table_item.setBackground(current_background)
                    table_item.setForeground(current_foreground)
                elif is_completed:
                    table_item.setBackground(completed_background)
                self._task_data_table.setItem(row, column, table_item)

        self._apply_task_table_column_widths(table_fields)

        current_row = min(max(self.task.current_task_index - 1, 0), max(len(items) - 1, 0))
        if 0 <= current_row < self._task_data_table.rowCount():
            self._task_data_table.selectRow(current_row)

    def _apply_task_table_column_widths(self, table_fields: list[str]) -> None:
        header = self._task_data_table.horizontalHeader()
        header.setStretchLastSection(False)
        self._task_data_table.resizeColumnsToContents()
        metrics = QFontMetrics(self._task_data_table.font())
        padding = max(24, metrics.horizontalAdvance("000"))
        min_width = max(72, metrics.horizontalAdvance("0000"))
        max_width = max(220, metrics.horizontalAdvance("0" * 28))

        for column, field_name in enumerate(table_fields):
            content_width = self._task_data_table.columnWidth(column)
            sample_width = self._task_table_sample_width(column)
            target_width = min(
                max(min_width, content_width, sample_width + padding),
                max_width,
            )
            if self._is_compact_task_table_column(field_name, column):
                target_width = min(target_width, max(96, metrics.horizontalAdvance("0" * 10)))
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, target_width)

    def _task_table_sample_width(self, column: int) -> int:
        header_item = self._task_data_table.horizontalHeaderItem(column)
        header_text = header_item.text() if header_item is not None else ""
        sample_texts = [header_text]
        row_count = min(self._task_data_table.rowCount(), 24)
        for row in range(row_count):
            item = self._task_data_table.item(row, column)
            if item is not None:
                sample_texts.append(item.text())
        metrics = QFontMetrics(self._task_data_table.font())
        return max((metrics.horizontalAdvance(text) for text in sample_texts), default=0)

    def _is_compact_task_table_column(self, field_name: str, column: int) -> bool:
        values: list[str] = []
        row_count = self._task_data_table.rowCount()
        for row in range(row_count):
            item = self._task_data_table.item(row, column)
            if item is None:
                continue
            text = item.text().strip()
            if text:
                values.append(text)
        if not values:
            return False
        if all(len(value) <= 8 for value in values):
            return True
        if all(self._is_numeric_like(value) for value in values):
            return True
        if all(value.casefold() in {"true", "false", "yes", "no", "0", "1"} for value in values):
            return True
        normalized = field_name.casefold()
        return normalized.endswith("_id") and all(len(value) <= 12 for value in values)

    def _is_numeric_like(self, value: str) -> bool:
        normalized = value.replace(",", "").replace(".", "").replace("-", "").strip()
        return bool(normalized) and normalized.isdigit()

    def _confirm_jump_task_progress_from_table(self) -> None:
        if not self.task:
            return
        row = self._task_data_table.currentRow()
        if row < 0:
            return
        task_index_label = self._task_data_table.verticalHeaderItem(row)
        if task_index_label is None:
            return
        try:
            task_index = int(task_index_label.text())
        except ValueError:
            return
        if task_index == self.task.current_task_index:
            return
        result = QMessageBox.question(
            self,
            "确认调整进度",
            f"将检查进度调整到第 {task_index} 条，并从该条开始继续检查。\n\n是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.task = self.app_service.jump_to_task_index(
            task_id=self.task.task_id,
            task_index=task_index,
        )
        self._refresh_task_page()
        self.statusBar().showMessage(f"检查进度已调整到第 {task_index} 条", 4000)

    def _task_progress_completed(self, task: TaskDetail) -> int:
        return self._display_completed_items(
            completed_items=task.completed_items,
            current_task_index=task.current_task_index,
            total_items=task.total_items,
        )

    def _display_completed_items(
        self,
        *,
        completed_items: int,
        current_task_index: int,
        total_items: int,
    ) -> int:
        if total_items <= 0:
            return 0
        pointer_completed = max(0, min(current_task_index - 1, total_items))
        return min(completed_items, pointer_completed)

    def _resolve_table_value(
        self,
        field_name: str,
        task_data: dict[str, object],
        review_data: dict[str, object],
    ) -> object:
        if field_name in review_data:
            return review_data[field_name]
        if field_name in task_data:
            return task_data[field_name]
        return ""

    def is_dark_mode(self) -> bool:
        return self.palette().window().color().lightness() < 128

    def _task_source_structure_changed(
        self,
        previous: TaskSnapshot,
        current: TaskSnapshot,
    ) -> bool:
        return (
            previous.sheet_name != current.sheet_name or previous.header_row != current.header_row
        )

    def _task_summary_text(self, task: TaskDetail) -> str:
        return "  ·  ".join(
            [
                self._task_status_badge(task.status),
                f"{self._task_progress_completed(task)}/{task.total_items}",
                self._format_task_timestamp(task.updated_at),
            ]
        )

    def _task_status_badge(self, status: TaskStatus) -> str:
        return f"{self._task_status_emoji(status)} {self._task_status_label(status)}"

    def _task_status_emoji(self, status: TaskStatus) -> str:
        labels: dict[TaskStatus, str] = {
            "ready": "📥",
            "in_progress": "👀",
            "completed": "✅",
        }
        return labels.get(status, "•")

    def _format_task_timestamp(self, value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        local_time = timestamp.astimezone()
        today = datetime.now().astimezone().date()
        if local_time.date() == today:
            return local_time.strftime("今天 %H:%M")
        if local_time.date().toordinal() == today.toordinal() - 1:
            return local_time.strftime("昨天 %H:%M")
        return local_time.strftime("%Y-%m-%d %H:%M")

    def _resolve_confirmation_url(self) -> str | None:
        if not self.task:
            return None
        if self.task.task_snapshot.confirm_url:
            return self.task.task_snapshot.confirm_url
        candidate_indexes = [self.task.current_task_index, 1]
        for task_index in candidate_indexes:
            item = self.app_service.load_item_at(task_id=self.task.task_id, task_index=task_index)
            if item is None:
                continue
            value = item.task_data.get(self.task.task_snapshot.url_field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _has_usable_buffer_urls(self) -> bool:
        if not self.task:
            return False
        items = self.app_service.list_buffer_items(self.task)
        url_field = self.task.task_snapshot.url_field
        for item in items:
            value = item.task_data.get(url_field)
            if isinstance(value, str) and value.strip():
                return True
        return False

    def _sync_browser_buffer(self) -> bool:
        if not self.task:
            return True
        items = self.app_service.list_buffer_items(self.task)
        self.browser.sync_buffer(
            tasks=items,
            url_field=self.task.task_snapshot.url_field,
        )
        return self._handle_browser_buffer_block()

    def _handle_browser_buffer_block(self) -> bool:
        block = self.browser.buffer_block()
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
                self.browser.resume_buffer()
                if self.task is None:
                    return False
                items = self.app_service.list_buffer_items(self.task)
                self.browser.sync_buffer(
                    tasks=items,
                    url_field=self.task.task_snapshot.url_field,
                )
                block = self.browser.buffer_block()
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

    def _export_task_with_dialog(self) -> None:
        if not self.task:
            return
        default_path = self._default_export_path()
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "导出",
            str(default_path),
            "Excel Workbook (*.xlsx)",
        )
        if not selected:
            return
        export_path = Path(selected)
        if export_path.suffix.lower() != ".xlsx":
            export_path = export_path.with_suffix(".xlsx")
        self.app_service.save_app_setting("task_export_dir", str(export_path.parent))
        export_path = self.app_service.export_task_to_path(
            task_id=self.task.task_id,
            export_path=export_path,
        )
        self.statusBar().showMessage(f"已导出到 {export_path}", 6000)

    def _default_export_path(self) -> Path:
        if not self.task:
            return Path.cwd() / "task_export.xlsx"
        raw = self.app_service.load_app_setting("task_export_dir")
        export_dir = (
            Path(raw) if isinstance(raw, str) and raw.strip() else self.task.source_file_path.parent
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = self._safe_filename(self.task.name)
        return export_dir / f"{safe_name}_{timestamp}.xlsx"

    def _task_status_label(self, status: TaskStatus) -> str:
        labels: dict[TaskStatus, str] = {
            "ready": "待开始",
            "in_progress": "进行中",
            "completed": "已完成",
        }
        return labels.get(status, str(status))

    def _safe_filename(self, value: str) -> str:
        sanitized = "".join(char if char not in '<>:"/\\|?*' else "_" for char in value).strip()
        return sanitized or "task_export"

    def closeEvent(self, event) -> None:
        if self._review_window is not None:
            self._review_window.close()
        self._confirmation_task_id = None
        self._release_review_locks()
        self._shutdown_launched_browsers()
        self.browser.shutdown()
        super().closeEvent(event)

    def _test_browser_config(self, config: BrowserConfig) -> tuple[bool, str]:
        profile_lock: RuntimeLockHandle | None = None
        try:
            profile_lock = acquire_profile_lock(
                config.profile_id,
                owner_label=f"浏览器测试 - Profile: {config.name}",
            )
        except RuntimeLockConflictError as exc:
            return False, str(exc)
        request = BrowserLaunchRequest(
            browser_config_id=config.profile_id,
            browser_name="configured",
            executable_path=config.executable_path or None,
            launch_args=config.launch_args,
        )
        try:
            self.browser.launch(request)
            self.browser.ensure_running()
            status = self.browser.status()
            if not status.running:
                config.last_test_status = "failed"
                return False, status.message
            test_url = config.test_url or "about:blank"
            self.browser.open_confirmation_page(test_url)
            config.last_test_status = "passed"
            config.last_tested_at = datetime.now(UTC).isoformat()
            return True, f"启动成功：{test_url}"
        finally:
            self.browser.shutdown()
            if profile_lock is not None:
                profile_lock.release()

    def _shutdown_confirmation_browser(self) -> None:
        if self._review_window is not None and self._review_window.isVisible():
            return
        self._confirmation_task_id = None
        self.browser.shutdown()
        self._release_review_locks()
        self._update_window_title()

    def _start_task_mutation(
        self,
        *,
        operation: str,
        progress_title: str,
        progress_label: str,
        source_path: Path | None = None,
        task_snapshot: TaskSnapshot | None = None,
        task_id: int | None = None,
        review_field_library: list[ReviewField] | None = None,
    ) -> None:
        if self._task_worker_thread is not None and self._task_worker_thread.isRunning():
            QMessageBox.information(self, "处理中", "当前已有任务处理进行中，请等待完成。")
            return

        self._task_worker_thread = QThread(self)
        self._task_worker = _TaskMutationWorker(
            app_service=self.app_service,
            operation=operation,
            source_path=source_path,
            task_snapshot=task_snapshot,
            task_id=task_id,
            review_field_library=review_field_library,
        )
        self._task_worker.moveToThread(self._task_worker_thread)
        self._task_worker_thread.started.connect(self._task_worker.run)
        self._task_worker.succeeded.connect(self._handle_task_mutation_success)
        self._task_worker.failed.connect(self._handle_task_mutation_failure)
        self._task_worker.succeeded.connect(self._task_worker_thread.quit)
        self._task_worker.failed.connect(self._task_worker_thread.quit)
        self._task_worker_thread.finished.connect(self._cleanup_task_worker)

        self._task_progress_dialog = QProgressDialog(progress_label, "", 0, 0, self)
        self._task_progress_dialog.setWindowTitle(progress_title)
        self._task_progress_dialog.setCancelButton(None)
        self._task_progress_dialog.setMinimumDuration(0)
        self._task_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._task_progress_dialog.setAutoClose(False)
        self._task_progress_dialog.setAutoReset(False)
        self._task_progress_dialog.show()
        self._task_worker_thread.start()

    def _handle_task_mutation_success(self, task_id: int, message: str) -> None:
        self._pending_task_success_message = message
        self._load_task(task_id)
        self._editing_task_index = None
        self.statusBar().showMessage(f"{message}：#{task_id}", 4000)

    def _handle_task_mutation_failure(self, message: str) -> None:
        QMessageBox.warning(self, "任务处理失败", message)

    def _cleanup_task_worker(self) -> None:
        if self._task_progress_dialog is not None:
            self._task_progress_dialog.close()
            self._task_progress_dialog.deleteLater()
        if self._task_worker is not None:
            self._task_worker.deleteLater()
        if self._task_worker_thread is not None:
            self._task_worker_thread.deleteLater()
        self._task_progress_dialog = None
        self._task_worker = None
        self._task_worker_thread = None
        self._pending_task_success_message = None

    def _launch_browser_for_config(self, config: BrowserConfig) -> tuple[bool, str]:
        existing = self._browser_launch_processes.get(config.profile_id)
        if existing is not None:
            process, _lock = existing
            if process.state() != QProcess.ProcessState.NotRunning:
                return False, f"Profile `{config.profile_id}` 已在当前实例中启动。"
            self._release_browser_launch(config.profile_id)
        try:
            profile_lock = acquire_profile_lock(
                config.profile_id,
                owner_label=f"启动浏览器 - Profile: {config.name}",
            )
        except RuntimeLockConflictError as exc:
            return False, str(exc)

        candidate = detect_browser("configured", config.executable_path or None)
        if candidate is None:
            profile_lock.release()
            return False, "未找到可用浏览器程序。"
        environment_dir = ensure_browser_environment_dir(config.profile_id)
        start_url = config.test_url or "about:blank"
        arguments = [f"--user-data-dir={environment_dir}", *config.launch_args, start_url]

        process = QProcess(self)
        process.finished.connect(
            lambda _code=0, _status=QProcess.ExitStatus.NormalExit, profile_id=config.profile_id: (
                self._release_browser_launch(profile_id)
            )
        )
        process.errorOccurred.connect(
            lambda _error, profile_id=config.profile_id: self._release_browser_launch(profile_id)
        )
        process.start(str(candidate.executable_path), arguments)
        if not process.waitForStarted(5000):
            profile_lock.release()
            process.deleteLater()
            return False, f"浏览器启动失败：{process.errorString()}"

        self._browser_launch_processes[config.profile_id] = (process, profile_lock)
        return True, f"已启动 {config.name}，关闭浏览器后会自动释放 Profile 占用。"

    def _release_review_locks(self) -> None:
        if self._review_task_lock is not None:
            self._review_task_lock.release()
            self._review_task_lock = None
        if self._review_profile_lock is not None:
            self._review_profile_lock.release()
            self._review_profile_lock = None

    def _release_browser_launch(self, profile_id: str) -> None:
        pair = self._browser_launch_processes.pop(profile_id, None)
        if pair is None:
            return
        process, lock = pair
        lock.release()
        process.deleteLater()

    def _shutdown_launched_browsers(self) -> None:
        for profile_id, (process, lock) in list(self._browser_launch_processes.items()):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(3000):
                    process.kill()
                    process.waitForFinished(2000)
            lock.release()
            process.deleteLater()
            self._browser_launch_processes.pop(profile_id, None)

    def _update_window_title(self, *, profile_name: str | None = None) -> None:
        suffix = f" {self._instance_id}" if self._instance_id > 1 else ""
        title = f"Link Glancer{suffix}"
        if profile_name:
            title += f" - Profile: {profile_name}"
        self.setWindowTitle(title)
