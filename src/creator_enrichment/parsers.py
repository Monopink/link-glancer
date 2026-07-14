from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response

from creator_enrichment.constants import KNOWN_CONTACT_FIELD_MAP
from link_glancer.tasks.models import TaskItem

PHONE_PLACEHOLDER = "INVALID"
_PHONE_PREFIX_PATTERN = re.compile(
    r"^(whatsapp|whats?app|wa|line|viber|zalo|tel|phone)[:\s-]*", re.IGNORECASE
)
_DIGIT_TRANSLATION = str.maketrans("０１２３４５６７８９＋", "0123456789+")


@dataclass(frozen=True, slots=True)
class _PhoneRegionRule:
    country_code: str
    valid_prefixes: tuple[str, ...]
    national_lengths: tuple[int, ...]


_PHONE_REGION_RULES: dict[str, _PhoneRegionRule] = {
    "PH": _PhoneRegionRule(country_code="63", valid_prefixes=("9",), national_lengths=(10,)),
    "MY": _PhoneRegionRule(
        country_code="60",
        valid_prefixes=("10", "11", "12", "13", "14", "16", "17", "18", "19"),
        national_lengths=(9, 10),
    ),
    "SG": _PhoneRegionRule(country_code="65", valid_prefixes=("8", "9"), national_lengths=(8,)),
    "TH": _PhoneRegionRule(
        country_code="66",
        valid_prefixes=("6", "8", "9"),
        national_lengths=(9,),
    ),
    "VN": _PhoneRegionRule(
        country_code="84",
        valid_prefixes=("3", "5", "7", "8", "9"),
        national_lengths=(9,),
    ),
    "ID": _PhoneRegionRule(
        country_code="62", valid_prefixes=("8",), national_lengths=(9, 10, 11, 12, 13)
    ),
}


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


def contact_patch(payload: dict[str, object], region: str) -> ParsedContactPayload:
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
    patch = {
        field_name: values[0] if len(values) == 1 else "; ".join(values)
        for field_name, values in grouped.items()
    }
    phone_value = normalized_phone_value(grouped, region)
    if phone_value is not None:
        patch["phone"] = phone_value
    return ParsedContactPayload(
        patch=patch,
        total_entries=len(contact_info),
        valued_entries=valued_entries,
        recognized_entries=recognized_entries,
    )


def normalized_phone_value(grouped_contacts: dict[str, list[str]], region: str) -> str | None:
    rule = _PHONE_REGION_RULES.get(normalized_region(region))
    if rule is None:
        return None
    raw_candidates = [
        value
        for field_name, values in grouped_contacts.items()
        if field_name.casefold() not in {"email", "phone"}
        for value in values
        if isinstance(value, str) and value.strip()
    ]
    if not raw_candidates:
        return None
    normalized_values: list[str] = []
    saw_invalid = False
    for raw_value in raw_candidates:
        for part in _split_contact_values(raw_value):
            normalized = _normalize_phone_candidate(part, rule)
            if normalized is None:
                if _looks_like_phone_candidate(part):
                    saw_invalid = True
                continue
            if normalized not in normalized_values:
                normalized_values.append(normalized)
    if normalized_values:
        return "; ".join(normalized_values)
    if saw_invalid:
        return PHONE_PLACEHOLDER
    return None


def _split_contact_values(raw_value: str) -> list[str]:
    parts = [part.strip() for part in raw_value.split(";")]
    return [part for part in parts if part]


def _normalize_phone_candidate(raw_value: str, rule: _PhoneRegionRule) -> str | None:
    normalized_text = raw_value.translate(_DIGIT_TRANSLATION).strip()
    normalized_text = _PHONE_PREFIX_PATTERN.sub("", normalized_text).strip()
    normalized_text = normalized_text.replace("\u00a0", " ")
    normalized_text = re.sub(r"[\s()./_-]+", "", normalized_text)
    if not normalized_text:
        return None
    if normalized_text.startswith("00"):
        normalized_text = "+" + normalized_text[2:]
    if normalized_text.count("+") > 1 or (
        "+" in normalized_text and not normalized_text.startswith("+")
    ):
        return None
    if normalized_text.startswith("+"):
        digits = normalized_text[1:]
        if not digits.isdigit() or not digits.startswith(rule.country_code):
            return None
        national_number = digits[len(rule.country_code) :]
    else:
        if not normalized_text.isdigit():
            return None
        digits = normalized_text
        if digits.startswith(rule.country_code):
            national_number = digits[len(rule.country_code) :]
        else:
            national_number = digits[1:] if digits.startswith("0") else digits
    if not _is_valid_national_number(national_number, rule):
        return None
    return f"+{rule.country_code}{national_number}"


def _looks_like_phone_candidate(raw_value: str) -> bool:
    normalized_text = raw_value.translate(_DIGIT_TRANSLATION).strip()
    normalized_text = _PHONE_PREFIX_PATTERN.sub("", normalized_text).strip()
    digit_count = sum(character.isdigit() for character in normalized_text)
    if digit_count < 6:
        return False
    return bool(re.fullmatch(r"[+\d\s()./_-]+", normalized_text))


def _is_valid_national_number(national_number: str, rule: _PhoneRegionRule) -> bool:
    if not national_number.isdigit():
        return False
    if len(national_number) not in rule.national_lengths:
        return False
    return any(national_number.startswith(prefix) for prefix in rule.valid_prefixes)


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
