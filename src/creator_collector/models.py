from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class CreatorCollectionConfig:
    browser_config_id: str
    page_url: str
    safety_limit: int = 1000


@dataclass(slots=True)
class CreatorCollectionStatus:
    running: bool
    paused: bool
    completed: bool
    collected_count: int
    pages_fetched: int
    safety_limit: int
    last_message: str
    rows: list[dict[str, object]]
    started_at: datetime | None = None
    estimated_total_count: int | None = None
    estimated_end_at: datetime | None = None
    backup_path: Path | None = None
