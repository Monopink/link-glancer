from __future__ import annotations

from datetime import UTC, datetime

from creator_enrichment.constants import STATE_STATUS_PENDING, TERMINAL_ENRICHMENT_STATUSES


def setting_key(task_id: int) -> str:
    return f"creator_enrichment_state:{task_id}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_state(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {"version": 1, "statuses": {}, "started_at": None}
    statuses_raw = raw.get("statuses")
    statuses: dict[str, dict[str, object]] = {}
    if isinstance(statuses_raw, dict):
        for key, value in statuses_raw.items():
            if not isinstance(value, dict):
                continue
            statuses[str(key)] = {
                "status": str(value.get("status") or STATE_STATUS_PENDING),
                "reason": str(value.get("reason") or ""),
                "updated_at": str(value.get("updated_at") or ""),
                "region": str(value.get("region") or ""),
            }
    return {
        "version": 1,
        "statuses": statuses,
        "started_at": raw.get("started_at") if isinstance(raw.get("started_at"), str) else None,
    }


def item_state(state: dict[str, object], task_index: int) -> dict[str, object]:
    statuses = state.setdefault("statuses", {})
    assert isinstance(statuses, dict)
    normalized_key = str(task_index)
    value = statuses.get(normalized_key)
    if not isinstance(value, dict):
        value = {
            "status": STATE_STATUS_PENDING,
            "reason": "",
            "updated_at": "",
            "region": "",
        }
        statuses[normalized_key] = value
    return value


def update_item_state(
    state: dict[str, object],
    *,
    task_index: int,
    status: str,
    reason: str = "",
    region: str = "",
) -> None:
    value = item_state(state, task_index)
    value["status"] = status
    value["reason"] = reason
    value["region"] = region
    value["updated_at"] = now_iso()


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_ENRICHMENT_STATUSES
