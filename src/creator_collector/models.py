from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CreatorCollectionConfig:
    browser_config_id: str
    page_url: str
    safety_limit: int = 1000
    auto_advance_interval_seconds: float = 1.5


@dataclass(slots=True)
class CreatorCollectionStatus:
    running: bool
    auto_scroll_enabled: bool
    interrupted: bool
    completed: bool
    collected_count: int
    pages_fetched: int
    safety_limit: int
    auto_advance_interval_seconds: float
    last_message: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    estimated_total_count: int | None = None
    estimated_end_at: datetime | None = None
