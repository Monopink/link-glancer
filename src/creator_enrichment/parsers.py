from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response

from creator_enrichment.constants import KNOWN_CONTACT_FIELD_MAP
from link_glancer.tasks.models import TaskItem


@dataclass(slots=True)
class ParsedContactPayload:
    patch: dict[str, object]
    total_entries: int
    valued_entries: int
    recognized_entries: int


def nested_value(raw: object) -> str:
    if isinstance(raw, dict):
        value = raw.get("value")
        if value is None:
            return ""
        return str(value)
    if raw is None:
        return ""
    return str(raw)


def contact_info_available(raw: object) -> bool | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return None


def profile_request_metadata(response: Response) -> tuple[str, tuple[int, ...]]:
    request = response.request
    try:
        post_data = request.post_data or ""
    except PlaywrightError:
        return "", ()
    try:
        payload = json.loads(post_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", ()
    creator_id = payload.get("creator_oec_id")
    raw_profile_types = payload.get("profile_types")
    profile_types: list[int] = []
    if isinstance(raw_profile_types, list):
        for item in raw_profile_types:
            try:
                profile_types.append(int(item))
            except (TypeError, ValueError):
                continue
    return str(creator_id or "").strip(), tuple(profile_types)


def should_capture_profile(
    *,
    profile: dict[str, object],
    request_creator_id: str,
    response_creator_id: str,
    profile_types: tuple[int, ...],
) -> bool:
    if request_creator_id and 1 in profile_types:
        return True
    return "contact_info_available" in profile and bool(response_creator_id)


def query_param(url: str, key: str) -> str:
    try:
        values = parse_qs(urlsplit(url).query).get(key)
    except ValueError:
        return ""
    if not values:
        return ""
    return str(values[0] or "").strip()


def contact_patch(payload: dict[str, object]) -> ParsedContactPayload:
    contact_info = payload.get("contact_info")
    if not isinstance(contact_info, list):
        return ParsedContactPayload(
            patch={},
            total_entries=0,
            valued_entries=0,
            recognized_entries=0,
        )
    grouped: dict[str, list[str]] = {}
    valued_entries = 0
    recognized_entries = 0
    for item in contact_info:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        valued_entries += 1
        try:
            field_number = int(field)
        except (TypeError, ValueError):
            continue
        recognized_entries += 1
        field_name = KNOWN_CONTACT_FIELD_MAP.get(field_number, f"contact_{field_number}")
        grouped.setdefault(field_name, [])
        if value not in grouped[field_name]:
            grouped[field_name].append(value)
    return ParsedContactPayload(
        patch={
            field_name: values[0] if len(values) == 1 else "; ".join(values)
            for field_name, values in grouped.items()
        },
        total_entries=len(contact_info),
        valued_entries=valued_entries,
        recognized_entries=recognized_entries,
    )


def normalized_region(raw: object) -> str:
    return str(raw or "").strip().upper()


def normalized_creator_id(raw: object) -> str:
    value = str(raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def sorted_items_by_region(items: list[TaskItem]) -> list[TaskItem]:
    def sort_key(item: TaskItem) -> tuple[int, str, int]:
        region = normalized_region(item.task_data.get("selection_region"))
        if not region:
            return (1, "ZZZ", item.task_index)
        return (0, region, item.task_index)

    return sorted(items, key=sort_key)


def remaining_regions_from_items(items: list[TaskItem]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        region = normalized_region(item.task_data.get("selection_region")) or "UNKNOWN"
        if region in seen:
            continue
        seen.add(region)
        result.append(region)
    return result


def parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
