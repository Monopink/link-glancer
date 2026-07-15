from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from link_glancer.tasks.models import TaskItem


@dataclass(slots=True)
class BrowserLaunchRequest:
    browser_config_id: str
    browser_name: str
    executable_path: str | None = None
    launch_args: list[str] | None = None


@dataclass(slots=True)
class BrowserStatus:
    active_browser: str | None
    executable_path: Path | None
    running: bool
    message: str


@dataclass(slots=True)
class BufferBlock:
    reason: str
    message: str
    task_index: int | None = None
    url: str | None = None


class BrowserController(Protocol):
    def launch(self, request: BrowserLaunchRequest) -> None: ...

    def ensure_running(self) -> None: ...

    def open_confirmation_page(self, url: str) -> None: ...

    def close_confirmation_page(self) -> None: ...

    def sync_buffer(self, tasks: list[TaskItem], url_field: str) -> None: ...

    def buffer_block(self) -> BufferBlock | None: ...

    def resume_buffer(self) -> None: ...

    def current_review_page_matches_active_tab(self) -> bool: ...

    def status(self) -> BrowserStatus: ...

    def shutdown(self) -> None: ...
