from __future__ import annotations

from link_glancer.tasks.models import ReviewField, TaskRangeScope, TaskRangeSelection, TaskSnapshot

SCREEN_PASSED = "screen_passed"
SCREEN_FAILED = "screen_failed"
SCREEN_UNRESOLVED = "screen_unresolved"


def screening_fields(task_snapshot: TaskSnapshot) -> list[ReviewField]:
    enabled_ids = set(task_snapshot.enabled_review_field_ids or [])
    return [
        field
        for field in task_snapshot.review_fields
        if field.field_type == "screen" and (not enabled_ids or field.field_id in enabled_ids)
    ]


def normalize_screen_value(field: ReviewField, value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text == field.screen_pass_value:
        return SCREEN_PASSED
    if text == field.screen_fail_value:
        return SCREEN_FAILED
    return None


def screening_summary(task_snapshot: TaskSnapshot, review_data: dict[str, object]) -> str:
    saw_pass = False
    for field in screening_fields(task_snapshot):
        normalized = normalize_screen_value(field, review_data.get(field.field_id))
        if normalized == SCREEN_FAILED:
            return SCREEN_FAILED
        if normalized == SCREEN_PASSED:
            saw_pass = True
    if saw_pass:
        return SCREEN_PASSED
    return SCREEN_UNRESOLVED


def matches_screening_scope(
    task_snapshot: TaskSnapshot,
    review_data: dict[str, object],
    scope: TaskRangeSelection,
) -> bool:
    if not scope:
        return True
    return screening_summary(task_snapshot, review_data) in scope


def screening_scope_label(scope: TaskRangeScope) -> str:
    labels: dict[TaskRangeScope, str] = {
        "screen_passed": "整体通过",
        "screen_failed": "整体不通过",
        "screen_unresolved": "未筛选",
    }
    return labels[scope]


def screening_scope_labels(scopes: TaskRangeSelection) -> str:
    normalized = normalize_task_range_selection(scopes)
    if len(normalized) == 3:
        return "全部"
    return "、".join(screening_scope_label(scope) for scope in normalized)


def normalize_task_range_selection(value: object) -> TaskRangeSelection:
    valid_scopes: tuple[TaskRangeScope, ...] = (
        "screen_passed",
        "screen_failed",
        "screen_unresolved",
    )
    if isinstance(value, str):
        if value == "all":
            return list(valid_scopes)
        if value in valid_scopes:
            return [value]
        return list(valid_scopes)
    if not isinstance(value, list):
        return list(valid_scopes)
    normalized: TaskRangeSelection = []
    for item in value:
        if item in valid_scopes and item not in normalized:
            normalized.append(item)
    return normalized or list(valid_scopes)
