from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
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
from link_glancer.browser.service import create_browser_controller
from link_glancer.tasks.database import consume_database_reset_reason
from link_glancer.tasks.models import BrowserConfig, TaskDetail, TaskSnapshot, TaskStatus
from link_glancer.ui.config_manager_dialog import ConfigManagerDialog
from link_glancer.ui.review_window import ReviewWindow
from link_glancer.ui.task_creation_dialog import TaskCreationDialog


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.app_service = TaskApplicationService.create_default()
        self.browser: BrowserController = create_browser_controller()
        self.task: TaskDetail | None = None
        self._task_table_ids: list[int] = []
        self._editing_task_index: int | None = None
        self._review_window: ReviewWindow | None = None
        self._confirmation_task_id: int | None = None
        self._handling_browser_block = False

        self.setWindowTitle("Link Glancer")
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

        new_task = QPushButton("新建任务")
        new_task.clicked.connect(self._create_task)
        self._toolbar.addWidget(new_task)

        new_collection_task = QPushButton("新建采集任务")
        new_collection_task.clicked.connect(self._create_collection_task)
        self._toolbar.addWidget(new_collection_task)

        browser_configs = QPushButton("浏览器配置")
        browser_configs.clicked.connect(self._show_browser_configs)
        self._toolbar.addWidget(browser_configs)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)

        self._open_task_button = QPushButton("打开任务")
        self._open_task_button.clicked.connect(self._open_selected_task_from_table)
        self._toolbar.addWidget(self._open_task_button)

        self._delete_task_button = QPushButton("删除任务")
        self._delete_task_button.clicked.connect(self._delete_selected_task_from_table)
        self._toolbar.addWidget(self._delete_task_button)

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
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        layout.addWidget(QLabel("任务列表"))

        self._task_table = QTableWidget(0, 6)
        self._task_table.setHorizontalHeaderLabels(
            ["ID", "任务", "进度", "状态", "源文件", "更新时间"]
        )
        self._task_table.verticalHeader().setVisible(False)
        self._task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._task_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._task_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._task_table.setStyleSheet(
            "QTableView { outline: 0; } "
            "QTableView::item:selected { "
            "border: 0px; "
            "background-color: #2563eb; "
            "color: #ffffff; "
            "}"
        )
        self._task_table.setAlternatingRowColors(True)
        self._task_table.doubleClicked.connect(self._open_selected_task_from_table)
        self._task_table.itemSelectionChanged.connect(self._update_start_page_actions)
        header_view = self._task_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._task_table, stretch=1)
        return page

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        self._task_title_label = QLabel("任务")
        self._task_title_label.setProperty("sectionTitle", True)
        self._task_meta_label = QLabel("-")
        self._task_meta_label.setWordWrap(True)
        title_box.addWidget(self._task_title_label)
        title_box.addWidget(self._task_meta_label)
        layout.addLayout(title_box)

        actions = QHBoxLayout()
        back_button = QPushButton("返回任务列表")
        back_button.clicked.connect(self._show_start_page)
        config_button = QPushButton("任务配置")
        config_button.clicked.connect(self._edit_task_configuration)
        export_button = QPushButton("导出")
        export_button.clicked.connect(self._export_task_with_dialog)
        delete_button = QPushButton("删除任务")
        delete_button.clicked.connect(self._delete_current_task)
        start_button = QPushButton("开始检查")
        start_button.clicked.connect(self._start_review_flow)
        actions.addWidget(back_button)
        actions.addWidget(config_button)
        actions.addWidget(export_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        actions.addWidget(start_button)
        layout.addLayout(actions)

        self._task_data_table = QTableWidget(0, 0)
        self._task_data_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._task_data_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._task_data_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._task_data_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._task_data_table.setStyleSheet(
            "QTableView { outline: 0; } QTableView::item:selected { border: 0px; }"
        )
        self._task_data_table.setAlternatingRowColors(True)
        self._task_data_table.horizontalHeader().setSectionsMovable(False)
        layout.addWidget(self._task_data_table, stretch=1)
        return page

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
        try:
            task_id = self.app_service.create_task(
                source_path=dialog.source_path,
                task_snapshot=dialog.task_snapshot,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "创建任务失败", str(exc))
            return
        self._load_task(task_id)
        self.statusBar().showMessage(f"任务已创建：#{task_id}", 4000)

    def _show_browser_configs(self) -> None:
        dialog = ConfigManagerDialog(
            browser_configs=self.app_service.list_browser_configs(),
            browser_test_callback=self._test_browser_config,
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
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        if dialog.created_task_id is None:
            return
        self._load_task(dialog.created_task_id)
        self.statusBar().showMessage(f"采集完成并创建任务：#{dialog.created_task_id}", 4000)

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
        reset_reviews_required = (
            self.task.task_snapshot.review_fields != dialog.task_snapshot.review_fields
        )
        if reimport_required or reset_reviews_required:
            message_lines = ["保存后将重置当前任务中的已保存检查结果。"]
            if reimport_required:
                message_lines.append("由于修改了 Sheet 或标题行，任务数据将按新配置重新导入。")
            else:
                message_lines.append("由于修改了检查项，已有结果将被清空。")
            result = QMessageBox.question(
                self,
                "确认保存任务配置",
                "\n".join(message_lines) + "\n\n是否继续？",
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        try:
            self.task = self.app_service.update_task_configuration(
                task_id=self.task.task_id,
                task_snapshot=dialog.task_snapshot,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "任务配置无效", str(exc))
            return
        self._shutdown_confirmation_browser()
        self._editing_task_index = None
        self._refresh_task_page()
        self.statusBar().showMessage("任务配置已保存。", 4000)

    def _load_task(self, task_id: int) -> None:
        self._shutdown_confirmation_browser()
        try:
            self.task = self.app_service.load_task(task_id)
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "任务错误", str(exc))
            return
        self._editing_task_index = None
        self._show_task_page()

    def _show_start_page(self) -> None:
        self._shutdown_confirmation_browser()
        self.task = None
        self._editing_task_index = None
        self._toolbar.show()
        self._stack.setCurrentWidget(self._start_page)
        self._refresh_start_page()

    def _show_task_page(self) -> None:
        self.task = self.app_service.load_task(self.task.task_id) if self.task else None
        self._toolbar.hide()
        self._stack.setCurrentWidget(self._task_page)
        self._refresh_task_page()

    def _refresh_start_page(self) -> None:
        summaries = self.app_service.list_tasks()
        self._task_table_ids = [summary.task_id for summary in summaries]
        self._task_table.clearSelection()
        self._task_table.setRowCount(len(summaries))
        for row, summary in enumerate(summaries):
            values = [
                str(summary.task_id),
                summary.name,
                f"{summary.completed_items}/{summary.total_items}",
                self._task_status_label(summary.status),
                summary.source_file_name,
                summary.updated_at,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._task_table.setItem(row, column, item)
        self._update_start_page_actions()

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
        self._task_title_label.setText(self.task.name)
        self._task_meta_label.setText(
            "  ·  ".join(
                [
                    self._format_task_created_at(self.task.created_at),
                    f"已完成 {self.task.completed_items}/{self.task.total_items}",
                    f"状态 {self._task_status_label(self.task.status)}",
                ]
            )
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
        request = BrowserLaunchRequest(
            browser_config_id=self.task.browser_config.profile_id,
            browser_name="configured",
            executable_path=self.task.browser_config.executable_path or None,
            launch_args=self.task.browser_config.launch_args,
        )
        self.browser.launch(request)
        self.browser.ensure_running()
        status = self.browser.status()
        if not status.running:
            self._confirmation_task_id = None
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
        if self.task is not None:
            self.task = self.app_service.load_task(self.task.task_id)
            self._refresh_task_page()

    def _refresh_task_data_table(self) -> None:
        if not self.task:
            return
        export_fields = self.task.task_snapshot.export_fields
        items = self.app_service.list_all_items(self.task.task_id)
        reviews = self.app_service.list_reviews(self.task.task_id)
        self._task_data_table.clear()
        self._task_data_table.setColumnCount(len(export_fields))
        self._task_data_table.setHorizontalHeaderLabels(export_fields)
        self._task_data_table.setRowCount(len(items))
        self._task_data_table.setVerticalHeaderLabels([str(item.task_index) for item in items])

        header = self._task_data_table.horizontalHeader()
        for column in range(len(export_fields)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, 160)
        if export_fields:
            header.resizeSection(0, 220)

        palette = self.palette()
        completed_background = palette.alternateBase().color()
        current_background = QColor("#1d4ed8" if self.is_dark_mode() else "#dbeafe")
        current_foreground = QColor("#ffffff") if self.is_dark_mode() else palette.text().color()

        for row, item in enumerate(items):
            review = reviews.get(item.task_item_id)
            is_current = item.task_index == self.task.current_task_index
            is_completed = review is not None
            for column, field_name in enumerate(export_fields):
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

        current_row = min(max(self.task.current_task_index - 1, 0), max(len(items) - 1, 0))
        if 0 <= current_row < self._task_data_table.rowCount():
            self._task_data_table.clearSelection()
            self._task_data_table.setCurrentCell(-1, -1)

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

    def _format_task_created_at(self, value: str) -> str:
        try:
            created_at = datetime.fromisoformat(value)
        except ValueError:
            return value
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

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
            current_task_index=self.task.current_task_index,
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
                    current_task_index=self.task.current_task_index,
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
        self.browser.shutdown()
        super().closeEvent(event)

    def _test_browser_config(self, config: BrowserConfig) -> tuple[bool, str]:
        request = BrowserLaunchRequest(
            browser_config_id=config.profile_id,
            browser_name="configured",
            executable_path=config.executable_path or None,
            launch_args=config.launch_args,
        )
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
        self.browser.shutdown()
        return True, f"启动成功：{test_url}"

    def _shutdown_confirmation_browser(self) -> None:
        if self._review_window is not None and self._review_window.isVisible():
            return
        self._confirmation_task_id = None
        self.browser.shutdown()
