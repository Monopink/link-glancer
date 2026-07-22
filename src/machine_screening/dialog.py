from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from link_glancer.application import TaskApplicationService
from link_glancer.runtime.dev import dev_mode_title_suffix
from link_glancer.tasks.models import TaskDetail
from machine_screening.service import build_machine_screening_summary, machine_screening_fields


class MachineScreeningDialog(QDialog):
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
        self.setWindowTitle(f"自动初筛 · 任务 #{task.task_id}{dev_mode_title_suffix()}")
        self.resize(460, 240)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._message_label = QLabel()
        self._message_label.setWordWrap(True)
        root.addWidget(self._message_label)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self._field_label = QLabel("-")
        self._url_label = QLabel("-")
        self._count_label = QLabel("-")
        self._completed_label = QLabel("-")
        self._pending_label = QLabel("-")
        form.addRow("初筛列", self._field_label)
        form.addRow("URL 列", self._url_label)
        form.addRow("目标条数", self._count_label)
        form.addRow("已写入", self._completed_label)
        form.addRow("未筛选", self._pending_label)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._start_button = QPushButton("开始自动初筛")
        self._start_button.clicked.connect(self._show_not_ready_message)
        buttons.addButton(self._start_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_summary()

    def _refresh_summary(self) -> None:
        self._task = self._app_service.load_task(self._task.task_id)
        fields = machine_screening_fields(self._task.task_snapshot)
        items = self._app_service.list_all_items(self._task.task_id)
        reviews = self._app_service.list_reviews(self._task.task_id)
        summary = build_machine_screening_summary(self._task.task_snapshot, items, reviews)
        self._field_label.setText("、".join(summary.field_labels) if summary.field_labels else "-")
        self._url_label.setText(summary.url_field or "-")
        self._count_label.setText(f"{summary.target_items} / {summary.total_items}")
        self._completed_label.setText(str(summary.completed_items))
        self._pending_label.setText(str(summary.pending_items))
        if not fields:
            self._message_label.setText("当前任务未配置机器来源的筛选检查项，无法启动自动初筛。")
            self._start_button.setEnabled(False)
            return
        if summary.target_items <= 0:
            self._message_label.setText("当前任务没有可用于自动初筛的 URL 条目。")
            self._start_button.setEnabled(False)
            return
        self._message_label.setText(
            "自动初筛模块骨架已接入。当前仅完成任务级入口、字段绑定和进度摘要，"
            "TikTok 页面读取与规则判定逻辑将在下一步接入。"
        )
        self._start_button.setEnabled(True)

    def _show_not_ready_message(self) -> None:
        QMessageBox.information(
            self,
            "尚未接入网页逻辑",
            "自动初筛的独立模块和任务入口已就位，但当前版本还未接入 TikTok 页面读取与判定逻辑。",
        )
