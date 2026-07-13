from __future__ import annotations

import re

from link_glancer.tasks.models import ReviewField, TaskItem

KNOWN_CONTACT_EXPORT_FIELDS = [
    "whatsapp",
    "email",
    "line",
    "zalo",
    "viber",
    "facebook",
]
KNOWN_ENRICHMENT_EXPORT_FIELDS = ["bio", "contact_info_available", *KNOWN_CONTACT_EXPORT_FIELDS]
_UNKNOWN_CONTACT_PATTERN = re.compile(r"^contact_(\d+)$", re.IGNORECASE)


def resolve_export_fields(
    review_fields: list[ReviewField],
    enabled_review_field_ids: list[str],
    items: list[TaskItem],
    export_fields: list[str],
) -> list[str]:
    available_columns = {
        key.casefold()
        for item in items
        for key in item.task_data
        if isinstance(key, str) and key.strip()
    }
    enabled_ids = enabled_review_ids(review_fields, enabled_review_field_ids)
    enabled_set = {field_id.casefold() for field_id in enabled_ids}
    resolved: list[str] = []
    seen: set[str] = set()

    for field_name in export_fields:
        normalized = field_name.casefold()
        if normalized in seen:
            continue
        if normalized in available_columns or normalized in enabled_set:
            resolved.append(field_name)
            seen.add(normalized)

    for field_name in KNOWN_ENRICHMENT_EXPORT_FIELDS:
        normalized = field_name.casefold()
        if normalized in seen or normalized not in available_columns:
            continue
        resolved.append(field_name)
        seen.add(normalized)

    for field_name in unknown_contact_export_fields(items):
        normalized = field_name.casefold()
        if normalized in seen or normalized not in available_columns:
            continue
        resolved.append(field_name)
        seen.add(normalized)

    for field_id in enabled_ids:
        normalized = field_id.casefold()
        if normalized in seen:
            continue
        resolved.append(field_id)
        seen.add(normalized)
    return resolved


def enabled_review_ids(
    review_fields: list[ReviewField],
    enabled_review_field_ids: list[str],
) -> list[str]:
    known_ids = [field.field_id for field in review_fields]
    if not enabled_review_field_ids:
        return known_ids
    enabled_set = {field_id for field_id in enabled_review_field_ids}
    return [field_id for field_id in known_ids if field_id in enabled_set]


def unknown_contact_export_fields(items: list[TaskItem]) -> list[str]:
    indexed: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item in items:
        for key in item.task_data:
            if not isinstance(key, str):
                continue
            match = _UNKNOWN_CONTACT_PATTERN.match(key.strip())
            if match is None:
                continue
            normalized = key.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            indexed.append((int(match.group(1)), key))
    indexed.sort(key=lambda item: item[0])
    return [field_name for _index, field_name in indexed]
