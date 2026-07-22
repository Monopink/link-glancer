from __future__ import annotations

from link_glancer.tasks.models import ReviewRecord, TaskItem, TaskSnapshot
from link_glancer.tasks.screening import normalize_screen_value
from machine_screening.models import MachineScreeningSummary


def machine_screening_fields(task_snapshot: TaskSnapshot):
    return [
        field
        for field in task_snapshot.review_fields
        if field.field_type == "screen" and field.source == "machine"
    ]


def has_machine_screening_fields(task_snapshot: TaskSnapshot) -> bool:
    return bool(machine_screening_fields(task_snapshot))


def build_machine_screening_summary(
    task_snapshot: TaskSnapshot,
    items: list[TaskItem],
    reviews: dict[int, ReviewRecord],
) -> MachineScreeningSummary:
    fields = machine_screening_fields(task_snapshot)
    target_items = [
        item for item in items if str(item.task_data.get(task_snapshot.url_field) or "").strip()
    ]
    completed_items = 0
    if fields:
        field = fields[0]
        for item in target_items:
            review = reviews.get(item.task_item_id)
            review_data = review.review_data if review is not None else {}
            if normalize_screen_value(field, review_data.get(field.field_id)) is not None:
                completed_items += 1
    return MachineScreeningSummary(
        field_labels=[field.label for field in fields],
        url_field=task_snapshot.url_field,
        total_items=len(items),
        target_items=len(target_items),
        completed_items=completed_items,
    )
