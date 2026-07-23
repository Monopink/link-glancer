from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CreatorEnrichmentFailureAttempt:
    index: int
    summary: str
    diagnostic_text: str
    issue_code: str = "manual_action"


@dataclass(slots=True)
class CreatorEnrichmentStatus:
    running: bool
    paused: bool
    completed: bool
    startup_phase: str
    total_count: int
    completed_count: int
    success_count: int
    no_contact_count: int
    auto_skipped_count: int
    skipped_count: int
    failed_count: int
    current_task_index: int | None
    current_region: str | None
    remaining_regions: list[str]
    last_message: str
    pause_reason: str | None = None
    attention_event_id: int = 0
    diagnostic_summary: str | None = None
    diagnostic_text: str | None = None
    failure_attempts: list[CreatorEnrichmentFailureAttempt] | None = None
    attention_required: bool = False
    started_at: datetime | None = None
    estimated_end_at: datetime | None = None
    auto_skip_on_failure: bool = False
    issue_code: str | None = None
