from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from link_glancer.infrastructure.repositories import BrowserConfigRepository, TaskRepository
from link_glancer.infrastructure.workbooks import WorkbookImporter
from link_glancer.tasks.models import (
    BrowserConfig,
    ReviewField,
    ReviewRecord,
    ReviewShortcutConfig,
    TaskDetail,
    TaskItem,
    TaskSnapshot,
    TaskSummary,
)


@dataclass(slots=True)
class TaskCreationWarnings:
    missing_review_export_fields: list[str]
    overlapping_export_review_fields: list[str]
    uses_first_task_url_as_confirmation: bool


class TaskApplicationService:
    """Application use cases for task-driven review workflows."""

    def __init__(
        self,
        task_repository: TaskRepository,
        browser_config_repository: BrowserConfigRepository,
        workbook_importer: WorkbookImporter,
    ) -> None:
        self._tasks = task_repository
        self._browser_configs = browser_config_repository
        self._workbook_importer = workbook_importer

    @property
    def database_path(self) -> Path:
        return self._tasks.database_path

    @classmethod
    def create_default(cls) -> TaskApplicationService:
        task_repository = TaskRepository.create_default()
        browser_config_repository = BrowserConfigRepository(task_repository.database_path)
        return cls(task_repository, browser_config_repository, WorkbookImporter())

    def list_sheet_names(self, source_path: Path) -> list[str]:
        return self._workbook_importer.list_sheet_names(source_path)

    def list_headers(self, source_path: Path, *, sheet_name: str, header_row: int) -> list[str]:
        return self._workbook_importer.list_headers(
            source_path, sheet_name=sheet_name, header_row=header_row
        )

    def build_task_name(self, source_path: Path, *, created_at: datetime | None = None) -> str:
        timestamp = (created_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        return f"{source_path.stem}_{timestamp}"

    def validate_task_snapshot(
        self,
        *,
        source_path: Path,
        task_snapshot: TaskSnapshot,
    ) -> TaskCreationWarnings:
        self.validate_shortcuts(
            task_snapshot.shortcuts,
            task_snapshot.review_fields,
        )
        headers = self._workbook_importer.list_headers(
            source_path,
            sheet_name=task_snapshot.sheet_name,
            header_row=task_snapshot.header_row,
        )
        normalized_headers = {header.casefold(): header for header in headers}

        required_excel_headers = [task_snapshot.url_field, *task_snapshot.display_fields]
        missing_required = [
            header
            for header in required_excel_headers
            if header.casefold() not in normalized_headers
        ]
        if missing_required:
            raise ValueError(f"Excel 缺少必要列：{', '.join(missing_required)}")

        review_field_names = [field.field_id for field in task_snapshot.review_fields]
        export_missing = [
            field
            for field in task_snapshot.export_fields
            if field.casefold() not in normalized_headers and field not in review_field_names
        ]
        if export_missing:
            raise ValueError(f"导出字段不存在：{', '.join(export_missing)}")

        overlap = [
            field.field_id
            for field in task_snapshot.review_fields
            if field.field_id.casefold() in normalized_headers
        ]
        missing_review_export_fields = [
            field.field_id
            for field in task_snapshot.review_fields
            if field.field_id not in task_snapshot.export_fields
        ]
        return TaskCreationWarnings(
            missing_review_export_fields=missing_review_export_fields,
            overlapping_export_review_fields=overlap,
            uses_first_task_url_as_confirmation=not bool(task_snapshot.confirm_url),
        )

    def create_task(
        self,
        *,
        source_path: Path,
        task_snapshot: TaskSnapshot,
    ) -> int:
        browser_config = self._browser_configs.load_browser_config(task_snapshot.browser_config_id)
        rows = self._workbook_importer.import_rows(source_path, task_snapshot)
        if not rows:
            raise ValueError("未找到可导入的任务行。")
        name = self.build_task_name(source_path)
        task_id = self._tasks.create_task(
            name=name,
            source_file_path=source_path,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
            rows=rows,
        )
        self._tasks.save_last_task_creation_defaults(
            source_path=source_path,
            task_snapshot=task_snapshot,
        )
        return task_id

    def load_task(self, task_id: int) -> TaskDetail:
        return self._tasks.load_task(task_id)

    def list_tasks(self) -> list[TaskSummary]:
        return self._tasks.list_tasks()

    def delete_task(self, task_id: int) -> None:
        self._tasks.delete_task(task_id)

    def update_task_configuration(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
    ) -> TaskDetail:
        task = self.load_task(task_id)
        self.validate_review_fields(task_snapshot.review_fields)
        self.validate_task_snapshot(
            source_path=task.source_file_path,
            task_snapshot=task_snapshot,
        )
        browser_config = self._browser_configs.load_browser_config(task_snapshot.browser_config_id)

        source_structure_changed = self._source_structure_changed(
            task.task_snapshot,
            task_snapshot,
        )
        review_structure_changed = task.task_snapshot.review_fields != task_snapshot.review_fields

        rows: list[tuple[int, dict[str, object]]] | None = None
        if source_structure_changed:
            rows = self._workbook_importer.import_rows(task.source_file_path, task_snapshot)
            if not rows:
                raise ValueError("按新配置重新导入后没有可用任务行。")

        self._tasks.update_task_configuration(
            task_id=task_id,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
            rows=rows,
            reset_reviews=review_structure_changed and rows is None,
        )
        return self.load_task(task_id)

    def update_task_snapshot(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
    ) -> TaskDetail:
        self.validate_review_fields(task_snapshot.review_fields)
        self.validate_shortcuts(
            task_snapshot.shortcuts,
            task_snapshot.review_fields,
        )
        browser_config = self._browser_configs.load_browser_config(task_snapshot.browser_config_id)
        self._tasks.update_task_snapshot(
            task_id=task_id,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
        )
        return self.load_task(task_id)

    def load_last_task_creation_defaults(self) -> tuple[Path | None, TaskSnapshot | None]:
        return self._tasks.load_last_task_creation_defaults()

    def list_buffer_items(self, task: TaskDetail) -> list[TaskItem]:
        return self._tasks.list_items_in_range(
            task_id=task.task_id,
            start_index=task.current_task_index,
            limit=task.task_snapshot.open_tab_count,
        )

    def list_all_items(self, task_id: int) -> list[TaskItem]:
        return self._tasks.list_all_items(task_id)

    def load_item_at(self, *, task_id: int, task_index: int) -> TaskItem | None:
        items = self._tasks.list_items_in_range(task_id=task_id, start_index=task_index, limit=1)
        return items[0] if items else None

    def list_reviews(self, task_id: int) -> dict[int, ReviewRecord]:
        return self._tasks.list_reviews(task_id)

    def find_previous_reviewed_index(self, *, task_id: int, before_task_index: int) -> int | None:
        return self._tasks.find_previous_reviewed_index(
            task_id=task_id,
            before_task_index=before_task_index,
        )

    def load_review(self, *, task_id: int, task_index: int) -> ReviewRecord | None:
        return self._tasks.load_review(task_id=task_id, task_index=task_index)

    def save_review(
        self,
        *,
        task_id: int,
        task_index: int,
        review_data: dict[str, object],
        advance_pointer: bool,
    ) -> TaskDetail:
        self._tasks.save_review(
            task_id=task_id,
            task_index=task_index,
            review_data=review_data,
            advance_pointer=advance_pointer,
        )
        return self.load_task(task_id)

    def jump_to_task_index(self, *, task_id: int, task_index: int) -> TaskDetail:
        self._tasks.jump_to_task_index(task_id=task_id, task_index=task_index)
        return self.load_task(task_id)

    def mark_task_in_progress(self, task_id: int) -> TaskDetail:
        self._tasks.mark_task_in_progress(task_id)
        return self.load_task(task_id)

    def export_task(self, *, task_id: int, destination_dir: Path | None = None) -> Path:
        return self._tasks.export_task(task_id=task_id, destination_dir=destination_dir)

    def load_app_setting(self, key: str) -> object | None:
        return self._tasks.load_app_setting(key)

    def save_app_setting(self, key: str, value: object) -> None:
        self._tasks.save_app_setting(key, value)

    def list_browser_configs(self) -> list[BrowserConfig]:
        return self._browser_configs.list_browser_configs()

    def save_browser_config(self, config: BrowserConfig) -> None:
        self._browser_configs.save_browser_config(config)

    def delete_browser_config(self, config_id: str) -> None:
        self._browser_configs.delete_browser_config(config_id)

    def validate_review_fields(self, review_fields: list[ReviewField]) -> None:
        if not review_fields:
            raise ValueError("至少需要一个检查项。")
        seen: set[str] = set()
        for field in review_fields:
            if not field.field_id.strip():
                raise ValueError("检查项结果列名不能为空。")
            if field.field_id in seen:
                raise ValueError(f"检查项结果列名重复：{field.field_id}")
            if not field.label.strip():
                raise ValueError("检查项问题标题不能为空。")
            if field.field_type in {"single_select", "multi_select"} and not field.options:
                raise ValueError(f"选择类型的检查项必须配置选项：{field.label}")
            seen.add(field.field_id)

    def validate_shortcuts(
        self,
        shortcuts: ReviewShortcutConfig,
        review_fields: list[ReviewField],
    ) -> None:
        seen: dict[str, str] = {}

        def register(value: str, label: str) -> None:
            normalized = value.strip().casefold()
            if not normalized:
                raise ValueError(f"{label}不能为空。")
            if normalized in seen:
                raise ValueError(f"快捷键冲突：{label} 与 {seen[normalized]} 重复。")
            seen[normalized] = label

        register(shortcuts.submit, "提交快捷键")
        register(shortcuts.previous, "上一条快捷键")
        register(shortcuts.exit, "退出快捷键")

        for field in review_fields:
            for option in field.options:
                if option.shortcut:
                    register(option.shortcut, f"{field.label} / {option.label}")

    def _source_structure_changed(
        self,
        previous: TaskSnapshot,
        current: TaskSnapshot,
    ) -> bool:
        return (
            previous.sheet_name != current.sheet_name or previous.header_row != current.header_row
        )
