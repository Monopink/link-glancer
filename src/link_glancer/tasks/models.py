from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

FieldType = Literal["single_select", "multi_select", "text", "boolean"]
TaskStatus = Literal["ready", "in_progress", "completed"]


@dataclass(slots=True)
class ReviewOption:
    value: str
    label: str
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
    previous: str = "Backspace"
    exit: str = "Esc"


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
    total_items: int
    completed_items: int
    current_item: TaskItem | None
    current_review: ReviewRecord | None
    created_at: str
    updated_at: str
