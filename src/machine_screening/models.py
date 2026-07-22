from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MachineScreeningSummary:
    field_labels: list[str]
    url_field: str
    total_items: int
    target_items: int
    completed_items: int

    @property
    def pending_items(self) -> int:
        return max(self.target_items - self.completed_items, 0)
