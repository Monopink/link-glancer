from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from creator_collector.exporter import export_collection_to_path
from link_glancer.infrastructure.repositories import BrowserConfigRepository, TaskRepository
from link_glancer.infrastructure.workbooks import WorkbookImporter
from link_glancer.tasks.export_fields import resolve_export_fields
from link_glancer.tasks.models import (
    BrowserConfig,
    BrowserProfile,
    CreatorCollectionRecovery,
    ReviewDraft,
    ReviewField,
    ReviewOption,
    ReviewRecord,
    ReviewShortcutConfig,
    TaskDetail,
    TaskItem,
    TaskSnapshot,
    TaskSummary,
)
from link_glancer.tasks.screening import (
    matches_screening_scope,
    normalize_task_range_selection,
    screening_fields,
    screening_scope_labels,
)
from link_glancer.tasks.serialization import (
    review_field_from_dict,
    review_field_to_dict,
    task_snapshot_from_dict,
    task_snapshot_to_dict,
)

LAST_TASK_DEFAULTS_KEY = "last_task_creation_defaults"
REVIEW_FIELD_LIBRARY_KEY = "review_field_library"
TASK_SHORTCUT_DEFAULTS_KEY = "task_shortcut_defaults"


@dataclass(slots=True)
class TaskCreationWarnings:
    missing_review_export_fields: list[str]
    overlapping_export_review_fields: list[str]


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
        self._validate_path_text(source_path)
        timestamp = (created_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        return f"{source_path.stem}_{timestamp}"

    def validate_task_snapshot(
        self,
        *,
        source_path: Path,
        task_snapshot: TaskSnapshot,
    ) -> TaskCreationWarnings:
        enabled_review_fields = self.enabled_review_fields(task_snapshot)
        self.validate_review_fields(task_snapshot.review_fields)
        self.validate_enabled_review_fields(task_snapshot)
        self.validate_shortcuts(
            task_snapshot.shortcuts,
            enabled_review_fields,
        )
        headers = self._workbook_importer.list_headers(
            source_path,
            sheet_name=task_snapshot.sheet_name,
            header_row=task_snapshot.header_row,
        )
        normalized_headers = {header.casefold(): header for header in headers}
        if task_snapshot.url_field.casefold() not in normalized_headers:
            raise ValueError(f"Excel 缺少必要列：{task_snapshot.url_field}")

        overlap = [
            field.field_id
            for field in enabled_review_fields
            if field.field_id.casefold() in normalized_headers
        ]
        return TaskCreationWarnings(
            missing_review_export_fields=[],
            overlapping_export_review_fields=overlap,
        )

    def create_task(
        self,
        *,
        source_path: Path,
        task_snapshot: TaskSnapshot,
        persist_defaults: bool = True,
        review_field_library: list[ReviewField] | None = None,
    ) -> int:
        self._validate_path_text(source_path)
        normalized_snapshot = self.normalize_task_snapshot(task_snapshot)
        browser_config = self._browser_configs.load_browser_config(
            normalized_snapshot.browser_config_id
        )
        rows = self._workbook_importer.import_rows(source_path, normalized_snapshot)
        if not rows:
            raise ValueError("未找到可导入的任务行。")
        name = self.build_task_name(source_path)
        task_id = self._tasks.create_task(
            name=name,
            source_file_path=source_path,
            task_snapshot=normalized_snapshot,
            browser_config=browser_config,
            rows=rows,
        )
        if persist_defaults:
            self.save_task_creation_defaults(
                source_path=source_path,
                task_snapshot=normalized_snapshot,
                review_field_library=review_field_library,
            )
        return task_id

    def create_task_from_creator_collection(
        self,
        *,
        source_path: Path,
        browser_config_id: str,
    ) -> int:
        task_snapshot = self.build_creator_collection_task_snapshot(
            source_path=source_path,
            browser_config_id=browser_config_id,
        )
        task_id = self.create_task(
            source_path=source_path,
            task_snapshot=task_snapshot,
            persist_defaults=False,
        )
        self.save_task_creation_defaults_from_creator_collection(
            source_path=source_path,
            task_snapshot=task_snapshot,
        )
        return task_id

    def create_creator_collection_session(
        self,
        *,
        browser_config_id: str,
        page_url: str,
        safety_limit: int,
        auto_advance_interval_seconds: float,
        last_message: str,
    ) -> int:
        return self._tasks.create_creator_collection_session(
            browser_config_id=browser_config_id,
            page_url=page_url,
            safety_limit=safety_limit,
            auto_advance_interval_seconds=auto_advance_interval_seconds,
            last_message=last_message,
        )

    def append_creator_collection_session_rows(
        self,
        *,
        session_id: int,
        rows: list[tuple[str, dict[str, object]]],
    ) -> int:
        return self._tasks.append_creator_collection_session_rows(session_id=session_id, rows=rows)

    def update_creator_collection_session(
        self,
        *,
        session_id: int,
        status: str,
        collected_count: int,
        pages_fetched: int,
        safety_limit: int,
        auto_advance_interval_seconds: float,
        last_message: str,
    ) -> None:
        self._tasks.update_creator_collection_session(
            session_id=session_id,
            status=status,
            collected_count=collected_count,
            pages_fetched=pages_fetched,
            safety_limit=safety_limit,
            auto_advance_interval_seconds=auto_advance_interval_seconds,
            last_message=last_message,
        )

    def load_pending_creator_collection_recovery(self) -> CreatorCollectionRecovery | None:
        return self._tasks.load_pending_creator_collection_recovery()

    def discard_creator_collection_session(self, *, session_id: int) -> None:
        self._tasks.discard_creator_collection_session(session_id=session_id)

    def create_task_from_creator_collection_session(
        self,
        *,
        session_id: int,
        export_path: Path,
    ) -> int:
        summary = self._tasks.load_creator_collection_session_summary(session_id=session_id)
        rows = self._tasks.load_creator_collection_session_rows(session_id=session_id)
        if not rows:
            raise ValueError("当前没有可保存的采集数据。")
        saved_path = export_collection_to_path(rows, export_path)
        task_id = self.create_task_from_creator_collection(
            source_path=saved_path,
            browser_config_id=summary.browser_config_id,
        )
        self._tasks.finalize_creator_collection_session(session_id=session_id)
        return task_id

    def load_task(self, task_id: int) -> TaskDetail:
        task = self._tasks.load_task(task_id)
        task.task_snapshot = self.normalize_task_snapshot(task.task_snapshot)
        return task

    def list_tasks(self) -> list[TaskSummary]:
        return self._tasks.list_tasks()

    def delete_task(self, task_id: int) -> None:
        self._tasks.delete_task(task_id)

    def update_task_configuration(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
        review_field_library: list[ReviewField] | None = None,
    ) -> TaskDetail:
        task = self.load_task(task_id)
        normalized_snapshot = self.normalize_task_snapshot(task_snapshot)
        self.validate_task_snapshot(
            source_path=task.source_file_path,
            task_snapshot=normalized_snapshot,
        )
        browser_config = self._browser_configs.load_browser_config(
            normalized_snapshot.browser_config_id
        )

        source_structure_changed = self._source_structure_changed(
            task.task_snapshot,
            normalized_snapshot,
        )

        rows: list[tuple[int, dict[str, object]]] | None = None
        if source_structure_changed:
            rows = self._workbook_importer.import_rows(task.source_file_path, normalized_snapshot)
            if not rows:
                raise ValueError("按新配置重新导入后没有可用任务行。")

        self._tasks.update_task_configuration(
            task_id=task_id,
            task_snapshot=normalized_snapshot,
            browser_config=browser_config,
            rows=rows,
            reset_reviews=False,
        )
        if review_field_library is not None:
            self.save_review_field_library(review_field_library)
        return self.load_task(task_id)

    def update_task_snapshot(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
    ) -> TaskDetail:
        normalized_snapshot = self.normalize_task_snapshot(task_snapshot)
        self.validate_review_fields(normalized_snapshot.review_fields)
        self.validate_enabled_review_fields(normalized_snapshot)
        self.validate_shortcuts(
            normalized_snapshot.shortcuts,
            self.enabled_review_fields(normalized_snapshot),
        )
        browser_config = self._browser_configs.load_browser_config(
            normalized_snapshot.browser_config_id
        )
        self._tasks.update_task_snapshot(
            task_id=task_id,
            task_snapshot=normalized_snapshot,
            browser_config=browser_config,
        )
        return self.load_task(task_id)

    def load_last_task_creation_defaults(self) -> tuple[Path | None, TaskSnapshot | None]:
        raw = self._tasks.load_app_setting(LAST_TASK_DEFAULTS_KEY)
        source_path: Path | None = None
        snapshot: TaskSnapshot | None = None
        legacy_review_fields: list[ReviewField] = []
        legacy_shortcuts: ReviewShortcutConfig | None = None
        if isinstance(raw, dict):
            source_path_value = raw.get("source_path")
            snapshot_value = raw.get("task_snapshot")
            if isinstance(source_path_value, str) and source_path_value.strip():
                source_path = Path(source_path_value).resolve()
            if isinstance(snapshot_value, dict):
                snapshot = task_snapshot_from_dict(snapshot_value)
                legacy_review_fields = list(snapshot.review_fields)
                legacy_shortcuts = snapshot.shortcuts
        library = self.load_review_field_library()
        if legacy_review_fields:
            library = self.merge_review_fields(library, legacy_review_fields)
            self.save_review_field_library(library)
        shortcuts = self.load_task_shortcut_defaults()
        if (
            legacy_shortcuts is not None
            and self._tasks.load_app_setting(TASK_SHORTCUT_DEFAULTS_KEY) is None
        ):
            self.save_task_shortcut_defaults(legacy_shortcuts)
            shortcuts = legacy_shortcuts
        if snapshot is None:
            if not library:
                return source_path, None
            snapshot = TaskSnapshot(
                sheet_name="",
                header_row=1,
                browser_config_id="",
                open_tab_count=3,
                confirm_url=None,
                url_field="url",
                display_fields=[],
                review_fields=library,
                enabled_review_field_ids=[field.field_id for field in library],
                shortcuts=shortcuts,
                export_fields=[],
                manual_review_scope=["screen_passed", "screen_failed", "screen_unresolved"],
                export_scope=["screen_passed", "screen_failed", "screen_unresolved"],
                enrichment_scope=["screen_passed", "screen_failed", "screen_unresolved"],
            )
            return source_path, snapshot

        snapshot.review_fields = list(library)
        snapshot.enabled_review_field_ids = self.normalize_enabled_review_field_ids(
            snapshot.review_fields,
            snapshot.enabled_review_field_ids,
        )
        snapshot.shortcuts = shortcuts
        if legacy_review_fields:
            self.save_task_creation_defaults(
                source_path=source_path,
                task_snapshot=snapshot,
                review_field_library=library,
            )
        return source_path, snapshot

    def load_review_field_library(self) -> list[ReviewField]:
        raw = self._tasks.load_app_setting(REVIEW_FIELD_LIBRARY_KEY)
        if not isinstance(raw, list):
            return []
        fields = [review_field_from_dict(item) for item in raw if isinstance(item, dict)]
        if fields:
            self.validate_review_fields(fields)
        return fields

    def save_review_field_library(self, review_fields: list[ReviewField]) -> None:
        normalized = self.merge_review_fields([], review_fields)
        if normalized:
            self.validate_review_fields(normalized)
        payload = [review_field_to_dict(field) for field in normalized]
        self._tasks.save_app_setting(REVIEW_FIELD_LIBRARY_KEY, payload)

    def load_task_shortcut_defaults(self) -> ReviewShortcutConfig:
        raw = self._tasks.load_app_setting(TASK_SHORTCUT_DEFAULTS_KEY)
        if not isinstance(raw, dict):
            return ReviewShortcutConfig()
        return ReviewShortcutConfig(
            submit=str(raw.get("submit", "Enter")),
            previous=str(raw.get("previous", "Left")),
            next=str(raw.get("next", "Right")),
            skip=str(raw.get("skip", "+")),
        )

    def save_task_shortcut_defaults(self, shortcuts: ReviewShortcutConfig) -> None:
        self.validate_shortcuts(shortcuts, [])
        self._tasks.save_app_setting(
            TASK_SHORTCUT_DEFAULTS_KEY,
            {
                "submit": shortcuts.submit,
                "previous": shortcuts.previous,
                "next": shortcuts.next,
                "skip": shortcuts.skip,
            },
        )

    def save_task_creation_defaults(
        self,
        *,
        source_path: Path | None,
        task_snapshot: TaskSnapshot,
        review_field_library: list[ReviewField] | None = None,
    ) -> None:
        normalized_snapshot = self.normalize_task_snapshot(task_snapshot)
        if review_field_library is not None:
            self.save_review_field_library(review_field_library)
        self.save_task_shortcut_defaults(normalized_snapshot.shortcuts)
        payload_snapshot = TaskSnapshot(
            sheet_name=normalized_snapshot.sheet_name,
            header_row=normalized_snapshot.header_row,
            browser_config_id=normalized_snapshot.browser_config_id,
            open_tab_count=normalized_snapshot.open_tab_count,
            confirm_url=normalized_snapshot.confirm_url,
            url_field=normalized_snapshot.url_field,
            display_fields=list(normalized_snapshot.display_fields),
            review_fields=[],
            enabled_review_field_ids=list(normalized_snapshot.enabled_review_field_ids),
            shortcuts=ReviewShortcutConfig(),
            export_fields=list(normalized_snapshot.export_fields),
            manual_review_scope=normalized_snapshot.manual_review_scope,
            export_scope=normalized_snapshot.export_scope,
            enrichment_scope=normalized_snapshot.enrichment_scope,
        )
        self._tasks.save_app_setting(
            LAST_TASK_DEFAULTS_KEY,
            {
                "source_path": str(source_path) if source_path is not None else None,
                "task_snapshot": task_snapshot_to_dict(payload_snapshot),
            },
        )

    def save_task_creation_defaults_from_creator_collection(
        self,
        *,
        source_path: Path,
        task_snapshot: TaskSnapshot,
    ) -> None:
        current_source_path, current_snapshot = self.load_last_task_creation_defaults()
        library = self.merge_review_fields(
            self.load_review_field_library(),
            task_snapshot.review_fields,
        )
        self.save_review_field_library(library)
        if current_snapshot is None:
            current_snapshot = TaskSnapshot(
                sheet_name=task_snapshot.sheet_name,
                header_row=task_snapshot.header_row,
                browser_config_id=task_snapshot.browser_config_id,
                open_tab_count=task_snapshot.open_tab_count,
                confirm_url=task_snapshot.confirm_url,
                url_field=task_snapshot.url_field,
                display_fields=list(task_snapshot.display_fields),
                review_fields=[],
                enabled_review_field_ids=[],
                shortcuts=self.load_task_shortcut_defaults(),
                export_fields=list(task_snapshot.export_fields),
                manual_review_scope=task_snapshot.manual_review_scope,
                export_scope=task_snapshot.export_scope,
                enrichment_scope=task_snapshot.enrichment_scope,
            )
            current_source_path = source_path

        merged_enabled_ids = self._merge_id_order(
            current_snapshot.enabled_review_field_ids,
            task_snapshot.enabled_review_field_ids
            or [field.field_id for field in task_snapshot.review_fields],
        )
        updated_snapshot = TaskSnapshot(
            sheet_name=task_snapshot.sheet_name,
            header_row=task_snapshot.header_row,
            browser_config_id=task_snapshot.browser_config_id,
            open_tab_count=task_snapshot.open_tab_count,
            confirm_url=task_snapshot.confirm_url,
            url_field=task_snapshot.url_field,
            display_fields=list(task_snapshot.display_fields),
            review_fields=library,
            enabled_review_field_ids=merged_enabled_ids,
            shortcuts=self.load_task_shortcut_defaults(),
            export_fields=list(task_snapshot.export_fields),
            manual_review_scope=task_snapshot.manual_review_scope,
            export_scope=task_snapshot.export_scope,
            enrichment_scope=task_snapshot.enrichment_scope,
        )
        self.save_task_creation_defaults(
            source_path=source_path or current_source_path,
            task_snapshot=updated_snapshot,
            review_field_library=library,
        )

    def build_creator_collection_task_snapshot(
        self,
        *,
        source_path: Path,
        browser_config_id: str,
    ) -> TaskSnapshot:
        sheet_names = self.list_sheet_names(source_path)
        if not sheet_names:
            raise ValueError("采集结果中没有可用工作表。")
        sheet_name = sheet_names[0]
        collector_fields = [
            ReviewField(
                field_id="validity",
                label="是否通过筛选",
                field_type="screen",
                required=True,
                screen_pass_value="是",
                screen_fail_value="否",
            ),
            ReviewField(
                field_id="quality",
                label="达人质量",
                field_type="single_select",
                required=False,
                options=[
                    ReviewOption(value="好", shortcut="3"),
                    ReviewOption(value="中", shortcut="4"),
                    ReviewOption(value="差", shortcut="5"),
                ],
            ),
            ReviewField(
                field_id="remark",
                label="备注",
                field_type="text",
                required=False,
                options=[],
            ),
        ]
        library = self.merge_review_fields(self.load_review_field_library(), collector_fields)
        default_snapshot = self.load_last_task_creation_defaults()[1]
        default_enabled_ids = default_snapshot.enabled_review_field_ids if default_snapshot else []
        enabled_review_field_ids = self._merge_id_order(
            default_enabled_ids,
            [field.field_id for field in collector_fields],
        )
        return TaskSnapshot(
            sheet_name=sheet_name,
            header_row=1,
            browser_config_id=browser_config_id,
            open_tab_count=3,
            confirm_url=None,
            url_field="url",
            display_fields=[
                "nickname",
                "handle",
                "selection_region",
                "follower_cnt",
                "units_sold",
                "category",
            ],
            review_fields=library,
            enabled_review_field_ids=enabled_review_field_ids,
            shortcuts=self.load_task_shortcut_defaults(),
            export_fields=[
                "creator_oecuid",
                "handle",
                "nickname",
                "selection_region",
                "units_sold",
                "follower_cnt",
                "ec_video_avg_view_cnt",
                "video_engagement",
                "ec_video_engagement",
                "video_avg_view_cnt",
                "category",
                "top_follower_gender",
                "url",
            ],
            manual_review_scope=["screen_passed", "screen_failed", "screen_unresolved"],
            export_scope=["screen_passed", "screen_failed", "screen_unresolved"],
            enrichment_scope=["screen_passed"],
        )

    def list_buffer_items(self, task: TaskDetail) -> list[TaskItem]:
        eligible_items = self.manual_scope_items(task)
        return [item for item in eligible_items if item.task_index >= task.current_task_index][
            : task.task_snapshot.open_tab_count
        ]

    def list_all_items(self, task_id: int) -> list[TaskItem]:
        return self._tasks.list_all_items(task_id)

    def manual_scope_items(self, task: TaskDetail) -> list[TaskItem]:
        items = self.list_all_items(task.task_id)
        reviews = self.list_reviews(task.task_id)
        return [
            item
            for item in items
            if matches_screening_scope(
                task.task_snapshot,
                reviews.get(item.task_item_id).review_data
                if reviews.get(item.task_item_id) is not None
                else {},
                task.task_snapshot.manual_review_scope,
            )
        ]

    def enrichment_scope_items(self, task_id: int) -> list[TaskItem]:
        task = self.load_task(task_id)
        items = self.list_all_items(task_id)
        reviews = self.list_reviews(task_id)
        return [
            item
            for item in items
            if matches_screening_scope(
                task.task_snapshot,
                reviews.get(item.task_item_id).review_data
                if reviews.get(item.task_item_id) is not None
                else {},
                task.task_snapshot.enrichment_scope,
            )
        ]

    def manual_scope_indexes(self, task: TaskDetail) -> list[int]:
        return [item.task_index for item in self.manual_scope_items(task)]

    def manual_scope_total_count(self, task: TaskDetail) -> int:
        return len(self.manual_scope_items(task))

    def manual_scope_completed_count(self, task: TaskDetail) -> int:
        reviews = self.list_reviews(task.task_id)
        return sum(1 for item in self.manual_scope_items(task) if item.task_item_id in reviews)

    def has_manual_scope_items(self, task: TaskDetail) -> bool:
        return self.manual_scope_total_count(task) > 0

    def align_manual_scope_pointer(self, task_id: int) -> TaskDetail:
        task = self.load_task(task_id)
        scope_indexes = self.manual_scope_indexes(task)
        if not scope_indexes:
            return task
        if task.current_task_index in scope_indexes:
            if task.viewing_task_index in scope_indexes:
                return task
            return self.set_viewing_task_index(task_id=task_id, task_index=task.current_task_index)
        next_index = next(
            (index for index in scope_indexes if index >= task.current_task_index),
            None,
        )
        if next_index is None:
            next_index = scope_indexes[-1]
        return self.jump_to_task_index(task_id=task_id, task_index=next_index)

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

    def find_next_reviewed_index(
        self,
        *,
        task_id: int,
        after_task_index: int,
        max_task_index: int,
    ) -> int | None:
        return self._tasks.find_next_reviewed_index(
            task_id=task_id,
            after_task_index=after_task_index,
            max_task_index=max_task_index,
        )

    def load_review(self, *, task_id: int, task_index: int) -> ReviewRecord | None:
        return self._tasks.load_review(task_id=task_id, task_index=task_index)

    def load_review_draft(self, *, task_id: int, task_index: int) -> ReviewDraft | None:
        return self._tasks.load_review_draft(task_id=task_id, task_index=task_index)

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
        task = self.load_task(task_id)
        if advance_pointer:
            task = self.align_manual_scope_pointer(task_id)
        return task

    def save_review_draft(
        self,
        *,
        task_id: int,
        task_index: int,
        draft_data: dict[str, object],
    ) -> TaskDetail:
        self._tasks.save_review_draft(
            task_id=task_id,
            task_index=task_index,
            draft_data=draft_data,
        )
        return self.load_task(task_id)

    def skip_review(self, *, task_id: int, task_index: int) -> TaskDetail:
        self._tasks.skip_review(task_id=task_id, task_index=task_index)
        return self.align_manual_scope_pointer(task_id)

    def update_task_item_data(
        self,
        *,
        task_id: int,
        task_index: int,
        task_data_patch: dict[str, object],
    ) -> TaskDetail:
        self._tasks.update_task_item_data(
            task_id=task_id,
            task_index=task_index,
            task_data_patch=task_data_patch,
        )
        return self.load_task(task_id)

    def jump_to_task_index(self, *, task_id: int, task_index: int) -> TaskDetail:
        self._tasks.jump_to_task_index(task_id=task_id, task_index=task_index)
        return self.load_task(task_id)

    def set_viewing_task_index(self, *, task_id: int, task_index: int) -> TaskDetail:
        self._tasks.set_viewing_task_index(task_id=task_id, task_index=task_index)
        return self.load_task(task_id)

    def mark_task_in_progress(self, task_id: int) -> TaskDetail:
        self._tasks.mark_task_in_progress(task_id)
        return self.align_manual_scope_pointer(task_id)

    def export_task(self, *, task_id: int, destination_dir: Path | None = None) -> Path:
        return self._tasks.export_task(task_id=task_id, destination_dir=destination_dir)

    def export_task_to_path(self, *, task_id: int, export_path: Path) -> Path:
        return self._tasks.export_task_to_path(task_id=task_id, export_path=export_path)

    def load_app_setting(self, key: str) -> object | None:
        return self._tasks.load_app_setting(key)

    def save_app_setting(self, key: str, value: object) -> None:
        self._tasks.save_app_setting(key, value)

    def list_browser_configs(self) -> list[BrowserConfig]:
        return self._browser_configs.list_browser_configs()

    def load_browser_config(self, config_id: str) -> BrowserConfig:
        return self._browser_configs.load_browser_config(config_id)

    def list_browser_profiles(self) -> list[BrowserProfile]:
        return self._browser_configs.list_browser_profiles()

    def save_browser_config(self, config: BrowserConfig) -> None:
        self._browser_configs.save_browser_config(config)

    def save_browser_profile(self, profile: BrowserProfile) -> None:
        self._browser_configs.save_browser_profile(profile)

    def delete_browser_config(self, config_id: str) -> None:
        self._browser_configs.delete_browser_config(config_id)

    def delete_browser_profile(self, profile_id: str) -> None:
        self._browser_configs.delete_browser_profile(profile_id)

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
            if field.field_type == "screen":
                if field.options:
                    raise ValueError(f"筛选类型检查项不能配置普通选项：{field.label}")
                if not field.screen_pass_value.strip() or not field.screen_fail_value.strip():
                    raise ValueError(f"筛选类型检查项必须配置通过/不通过展示值：{field.label}")
                if field.screen_pass_value == field.screen_fail_value:
                    raise ValueError(f"筛选类型检查项的通过/不通过展示值不能相同：{field.label}")
            elif field.source == "machine":
                raise ValueError(f"仅筛选类型检查项可设置为机器来源：{field.label}")
            if field.field_type in {"single_select", "multi_select"} and not field.options:
                raise ValueError(f"选择类型的检查项必须配置选项：{field.label}")
            seen.add(field.field_id)
        machine_screen_fields = [
            field
            for field in review_fields
            if field.field_type == "screen" and field.source == "machine"
        ]
        if len(machine_screen_fields) > 1:
            raise ValueError("当前仅支持一个机器初筛检查项。")

    def validate_enabled_review_fields(self, task_snapshot: TaskSnapshot) -> None:
        if not task_snapshot.enabled_review_field_ids:
            raise ValueError("至少需要启用一个检查项。")
        field_ids = {field.field_id for field in task_snapshot.review_fields}
        for field_id in task_snapshot.enabled_review_field_ids:
            if field_id not in field_ids:
                raise ValueError(f"启用的检查项不存在：{field_id}")

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
        register(shortcuts.next, "下一条快捷键")
        register(shortcuts.skip, "跳过快捷键")

        for field in review_fields:
            for option in field.options:
                if option.shortcut:
                    register(option.shortcut, f"{field.label} / {option.value}")

    def enabled_review_fields(self, task_snapshot: TaskSnapshot) -> list[ReviewField]:
        enabled_ids = {
            field_id
            for field_id in self.normalize_enabled_review_field_ids(
                task_snapshot.review_fields,
                task_snapshot.enabled_review_field_ids,
            )
        }
        return [field for field in task_snapshot.review_fields if field.field_id in enabled_ids]

    def machine_screening_fields(self, task_snapshot: TaskSnapshot) -> list[ReviewField]:
        return [field for field in screening_fields(task_snapshot) if field.source == "machine"]

    def has_machine_screening_fields(self, task_snapshot: TaskSnapshot) -> bool:
        return bool(self.machine_screening_fields(task_snapshot))

    def task_display_field_names(
        self, task: TaskDetail, items: list[TaskItem] | None = None
    ) -> list[str]:
        items = items if items is not None else self.list_all_items(task.task_id)
        available_columns = self._available_task_columns(items)
        return [
            field_name
            for field_name in task.task_snapshot.display_fields
            if field_name.casefold() in available_columns
        ]

    def task_table_field_names(
        self, task: TaskDetail, items: list[TaskItem] | None = None
    ) -> list[str]:
        return self.export_field_names(task, items)

    def export_field_names(
        self, task: TaskDetail, items: list[TaskItem] | None = None
    ) -> list[str]:
        items = items if items is not None else self.list_all_items(task.task_id)
        return resolve_export_fields(
            task.task_snapshot.review_fields,
            task.task_snapshot.enabled_review_field_ids,
            items,
            task.task_snapshot.export_fields,
        )

    def range_scope_label(self, scope: object, *, mode: str = "overall") -> str:
        normalized = normalize_task_range_selection(scope)
        if mode == "manual":
            labels = {
                "screen_passed": "初筛通过",
                "screen_failed": "初筛不通过",
                "screen_unresolved": "未初筛",
            }
            if len(normalized) == 3:
                return "全部"
            return "、".join(labels[item] for item in normalized)
        return screening_scope_labels(normalized)

    def normalize_task_snapshot(self, task_snapshot: TaskSnapshot) -> TaskSnapshot:
        review_fields = self.merge_review_fields([], task_snapshot.review_fields)
        enabled_review_field_ids = self.normalize_enabled_review_field_ids(
            review_fields,
            task_snapshot.enabled_review_field_ids,
        )
        return TaskSnapshot(
            sheet_name=task_snapshot.sheet_name,
            header_row=task_snapshot.header_row,
            browser_config_id=task_snapshot.browser_config_id,
            open_tab_count=task_snapshot.open_tab_count,
            confirm_url=task_snapshot.confirm_url,
            url_field=task_snapshot.url_field,
            display_fields=list(task_snapshot.display_fields),
            review_fields=review_fields,
            enabled_review_field_ids=enabled_review_field_ids,
            shortcuts=task_snapshot.shortcuts,
            export_fields=list(task_snapshot.export_fields),
            manual_review_scope=normalize_task_range_selection(task_snapshot.manual_review_scope),
            export_scope=normalize_task_range_selection(task_snapshot.export_scope),
            enrichment_scope=normalize_task_range_selection(task_snapshot.enrichment_scope),
        )

    def merge_review_fields(
        self,
        base_fields: list[ReviewField],
        incoming_fields: list[ReviewField],
    ) -> list[ReviewField]:
        merged: list[ReviewField] = []
        field_index: dict[str, int] = {}
        for field in base_fields:
            merged.append(field)
            field_index[field.field_id] = len(merged) - 1
        for field in incoming_fields:
            existing_index = field_index.get(field.field_id)
            if existing_index is None:
                merged.append(field)
                field_index[field.field_id] = len(merged) - 1
            else:
                merged[existing_index] = field
        return merged

    def normalize_enabled_review_field_ids(
        self,
        review_fields: list[ReviewField],
        enabled_review_field_ids: list[str],
    ) -> list[str]:
        known_ids = [field.field_id for field in review_fields]
        if not enabled_review_field_ids:
            return list(known_ids)
        enabled_set = {field_id for field_id in enabled_review_field_ids}
        return [field_id for field_id in known_ids if field_id in enabled_set]

    def _available_task_columns(self, items: list[TaskItem]) -> set[str]:
        return {
            key.casefold()
            for item in items
            for key in item.task_data
            if isinstance(key, str) and key.strip()
        }

    def _merge_id_order(self, primary: list[str], secondary: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for field_name in [*primary, *secondary]:
            normalized = field_name.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(field_name)
        return result

    def _validate_path_text(self, path: Path) -> None:
        for field_name, value in (
            ("文件路径", str(path)),
            ("文件名", path.name),
            ("文件名", path.stem),
        ):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError(f"{field_name}包含无法编码的字符，请更换导出位置或文件名后重试。")

    def _source_structure_changed(
        self,
        previous: TaskSnapshot,
        current: TaskSnapshot,
    ) -> bool:
        return (
            previous.sheet_name != current.sheet_name or previous.header_row != current.header_row
        )
