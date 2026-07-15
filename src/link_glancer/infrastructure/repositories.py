from __future__ import annotations

from pathlib import Path

from link_glancer.tasks import database
from link_glancer.tasks.exporter import export_task_results, export_task_results_to_path
from link_glancer.tasks.models import (
    BrowserConfig,
    BrowserProfile,
    CreatorCollectionRecovery,
    CreatorCollectionSessionSummary,
    ReviewDraft,
    ReviewRecord,
    TaskDetail,
    TaskItem,
    TaskSnapshot,
    TaskSummary,
)
from link_glancer.tasks.serialization import task_snapshot_from_dict, task_snapshot_to_dict


class TaskRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @classmethod
    def create_default(cls) -> TaskRepository:
        return cls(database.ensure_app_database())

    def create_task(
        self,
        *,
        name: str,
        source_file_path: Path,
        task_snapshot: TaskSnapshot,
        browser_config: BrowserConfig,
        rows: list[tuple[int, dict[str, object]]],
    ) -> int:
        return database.create_task(
            self.database_path,
            name=name,
            source_file_path=source_file_path,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
            rows=rows,
        )

    def list_tasks(self) -> list[TaskSummary]:
        return database.list_task_summaries(self.database_path)

    def load_task(self, task_id: int) -> TaskDetail:
        return database.load_task_detail(self.database_path, task_id)

    def list_items_in_range(self, *, task_id: int, start_index: int, limit: int) -> list[TaskItem]:
        return database.list_items_in_range(
            self.database_path,
            task_id=task_id,
            start_index=start_index,
            limit=limit,
        )

    def list_all_items(self, task_id: int) -> list[TaskItem]:
        return database.list_all_items(self.database_path, task_id)

    def list_reviews(self, task_id: int) -> dict[int, ReviewRecord]:
        return database.list_reviews(self.database_path, task_id)

    def find_previous_reviewed_index(self, *, task_id: int, before_task_index: int) -> int | None:
        return database.find_previous_reviewed_index(
            self.database_path,
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
        return database.find_next_reviewed_index(
            self.database_path,
            task_id=task_id,
            after_task_index=after_task_index,
            max_task_index=max_task_index,
        )

    def load_review(self, *, task_id: int, task_index: int) -> ReviewRecord | None:
        return database.load_review_by_task_index(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
        )

    def load_review_draft(self, *, task_id: int, task_index: int) -> ReviewDraft | None:
        return database.load_review_draft_by_task_index(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
        )

    def save_review(
        self,
        *,
        task_id: int,
        task_index: int,
        review_data: dict[str, object],
        advance_pointer: bool,
    ) -> None:
        database.save_review(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
            review_data=review_data,
            advance_pointer=advance_pointer,
        )

    def save_review_draft(
        self,
        *,
        task_id: int,
        task_index: int,
        draft_data: dict[str, object],
    ) -> None:
        database.save_review_draft(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
            draft_data=draft_data,
        )

    def skip_review(self, *, task_id: int, task_index: int) -> None:
        database.skip_review(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
        )

    def update_task_item_data(
        self,
        *,
        task_id: int,
        task_index: int,
        task_data_patch: dict[str, object],
    ) -> None:
        database.update_task_item_data(
            self.database_path,
            task_id=task_id,
            task_index=task_index,
            task_data_patch=task_data_patch,
        )

    def jump_to_task_index(self, *, task_id: int, task_index: int) -> None:
        database.jump_to_task_index(self.database_path, task_id, task_index)

    def set_viewing_task_index(self, *, task_id: int, task_index: int) -> None:
        database.set_viewing_task_index(self.database_path, task_id, task_index)

    def mark_task_in_progress(self, task_id: int) -> None:
        database.mark_task_in_progress(self.database_path, task_id)

    def export_task(self, *, task_id: int, destination_dir: Path | None = None) -> Path:
        return export_task_results(self.database_path, task_id, destination_dir=destination_dir)

    def export_task_to_path(self, *, task_id: int, export_path: Path) -> Path:
        return export_task_results_to_path(self.database_path, task_id, export_path=export_path)

    def delete_task(self, task_id: int) -> None:
        database.delete_task(self.database_path, task_id)

    def update_task_configuration(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
        browser_config: BrowserConfig,
        rows: list[tuple[int, dict[str, object]]] | None = None,
        reset_reviews: bool = False,
    ) -> None:
        database.update_task_configuration(
            self.database_path,
            task_id=task_id,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
            rows=rows,
            reset_reviews=reset_reviews,
        )

    def update_task_snapshot(
        self,
        *,
        task_id: int,
        task_snapshot: TaskSnapshot,
        browser_config: BrowserConfig,
    ) -> None:
        database.update_task_snapshot(
            self.database_path,
            task_id=task_id,
            task_snapshot=task_snapshot,
            browser_config=browser_config,
        )

    def load_last_task_creation_defaults(self) -> tuple[Path | None, TaskSnapshot | None]:
        raw = database.load_app_setting(self.database_path, "last_task_creation_defaults")
        if not isinstance(raw, dict):
            return None, None
        source_path_value = raw.get("source_path")
        snapshot_value = raw.get("task_snapshot")
        source_path = (
            Path(str(source_path_value)).resolve()
            if isinstance(source_path_value, str) and source_path_value.strip()
            else None
        )
        snapshot = (
            task_snapshot_from_dict(snapshot_value) if isinstance(snapshot_value, dict) else None
        )
        return source_path, snapshot

    def save_last_task_creation_defaults(
        self,
        *,
        source_path: Path | None,
        task_snapshot: TaskSnapshot,
    ) -> None:
        payload = {
            "source_path": str(source_path) if source_path is not None else None,
            "task_snapshot": task_snapshot_to_dict(task_snapshot),
        }
        database.save_app_setting(
            self.database_path,
            "last_task_creation_defaults",
            payload,
        )

    def load_app_setting(self, key: str) -> object | None:
        return database.load_app_setting(self.database_path, key)

    def save_app_setting(self, key: str, value: object) -> None:
        database.save_app_setting(self.database_path, key, value)

    def create_creator_collection_session(
        self,
        *,
        browser_config_id: str,
        page_url: str,
        safety_limit: int,
        auto_advance_interval_seconds: float,
        last_message: str,
    ) -> int:
        return database.create_creator_collection_session(
            self.database_path,
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
        return database.append_creator_collection_session_rows(
            self.database_path,
            session_id=session_id,
            rows=rows,
        )

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
        database.update_creator_collection_session(
            self.database_path,
            session_id=session_id,
            status=status,
            collected_count=collected_count,
            pages_fetched=pages_fetched,
            safety_limit=safety_limit,
            auto_advance_interval_seconds=auto_advance_interval_seconds,
            last_message=last_message,
        )

    def load_pending_creator_collection_recovery(self) -> CreatorCollectionRecovery | None:
        return database.load_pending_creator_collection_recovery(self.database_path)

    def load_creator_collection_session_rows(self, *, session_id: int) -> list[dict[str, object]]:
        return database.load_creator_collection_session_rows(
            self.database_path,
            session_id=session_id,
        )

    def load_creator_collection_session_summary(
        self,
        *,
        session_id: int,
    ) -> CreatorCollectionSessionSummary:
        return database.load_creator_collection_session_summary(
            self.database_path,
            session_id=session_id,
        )

    def finalize_creator_collection_session(self, *, session_id: int) -> None:
        database.finalize_creator_collection_session(self.database_path, session_id=session_id)

    def discard_creator_collection_session(self, *, session_id: int) -> None:
        database.discard_creator_collection_session(self.database_path, session_id=session_id)


class BrowserConfigRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def list_browser_configs(self) -> list[BrowserConfig]:
        return database.list_browser_configs(self.database_path)

    def list_browser_profiles(self) -> list[BrowserProfile]:
        return database.list_browser_profiles(self.database_path)

    def load_browser_config(self, config_id: str) -> BrowserConfig:
        return database.load_browser_config(self.database_path, config_id)

    def save_browser_config(self, config: BrowserConfig) -> None:
        database.save_browser_config(self.database_path, config)

    def save_browser_profile(self, profile: BrowserProfile) -> None:
        database.save_browser_profile(self.database_path, profile)

    def delete_browser_config(self, config_id: str) -> None:
        database.delete_browser_config(self.database_path, config_id)

    def delete_browser_profile(self, profile_id: str) -> None:
        database.delete_browser_profile(self.database_path, profile_id)
