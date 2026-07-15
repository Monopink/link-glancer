from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

FieldType = Literal["single_select", "multi_select", "text", "boolean"]
TaskStatus = Literal["ready", "in_progress", "completed"]
CollectorSessionStatus = Literal["active", "interrupted", "finalizing", "finalized", "discarded"]


@dataclass(slots=True)
class ReviewOption:
    value: str
    shortcut: str | None = None


@dataclass(slots=True)
class ReviewField:
    field_id: str
    label: str
    field_type: FieldType
    required: bool = False
    options: list[ReviewOption] = field(default_factory=list)


@dataclass(slots=True)
class ReviewShortcutConfig:
    submit: str = "Enter"
    previous: str = "Left"
    next: str = "Right"
    skip: str = "+"


@dataclass(slots=True)
class BrowserProfile:
    profile_id: str
    name: str


@dataclass(slots=True)
class BrowserConfig:
    config_id: str
    name: str
    profile_id: str
    executable_path: str = ""
    launch_args: list[str] = field(default_factory=list)
    test_url: str = "about:blank"
    last_tested_at: str | None = None
    last_test_status: str = "untested"


@dataclass(slots=True)
class TaskSnapshot:
    sheet_name: str
    header_row: int
    browser_config_id: str
    open_tab_count: int
    confirm_url: str | None
    url_field: str
    display_fields: list[str] = field(default_factory=list)
    review_fields: list[ReviewField] = field(default_factory=list)
    enabled_review_field_ids: list[str] = field(default_factory=list)
    shortcuts: ReviewShortcutConfig = field(default_factory=ReviewShortcutConfig)
    export_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskSummary:
    task_id: int
    name: str
    source_file_path: Path
    source_file_name: str
    status: TaskStatus
    current_task_index: int
    total_items: int
    completed_items: int
    updated_at: str


@dataclass(slots=True)
class TaskItem:
    task_item_id: int
    task_index: int
    source_row: int
    task_data: dict[str, object]


@dataclass(slots=True)
class ReviewRecord:
    task_item_id: int
    review_data: dict[str, object]
    review_status: str
    reviewed_at: str | None
    updated_at: str


@dataclass(slots=True)
class ReviewDraft:
    task_item_id: int
    draft_data: dict[str, object]
    updated_at: str


@dataclass(slots=True)
class TaskDetail:
    task_id: int
    name: str
    source_file_path: Path
    source_file_name: str
    source_file_size: int | None
    source_file_mtime: str | None
    source_file_hash: str | None
    task_snapshot: TaskSnapshot
    browser_config: BrowserConfig
    status: TaskStatus
    current_task_index: int
    viewing_task_index: int
    total_items: int
    completed_items: int
    current_item: TaskItem | None
    current_review: ReviewRecord | None
    current_draft: ReviewDraft | None
    viewing_item: TaskItem | None
    viewing_review: ReviewRecord | None
    viewing_draft: ReviewDraft | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class CreatorCollectionSessionSummary:
    session_id: int
    browser_config_id: str
    page_url: str
    status: CollectorSessionStatus
    collected_count: int
    pages_fetched: int
    safety_limit: int
    auto_advance_interval_seconds: float
    last_message: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class CreatorCollectionRecovery:
    session_id: int
    browser_config_id: str
    browser_config_name: str
    page_url: str
    status: CollectorSessionStatus
    collected_count: int
    pages_fetched: int
    safety_limit: int
    auto_advance_interval_seconds: float
    last_message: str
    created_at: str
    updated_at: str
