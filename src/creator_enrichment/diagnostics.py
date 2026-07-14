from __future__ import annotations

from datetime import UTC, datetime
from pprint import pformat


def format_contact_available(value: bool | None) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def build_diagnostic_text(
    *,
    task_id: int,
    current_task_index: int | None,
    current_subject: str | None,
    current_region: str | None,
    pause_reason: str | None,
    pause_step: str | None,
    page_url: str,
    message: str,
    profile_types: tuple[int, ...],
    contact_available: bool | None,
    contact_available_raw: object,
    contact_badge_detected: bool,
    contact_badge_clicked: bool,
    contact_badge_strategy: str,
    last_profile_response_url: str,
    last_profile_payload: dict[str, object] | None,
    last_contact_response_url: str,
    last_contact_payload: dict[str, object] | None,
) -> str:
    lines = [
        f"时间: {datetime.now(UTC).isoformat()}",
        f"任务ID: {task_id}",
        f"当前条目: {current_task_index}",
        f"当前对象: {current_subject or '-'}",
        f"当前区域: {current_region or '-'}",
        f"暂停原因: {pause_reason or '-'}",
        f"暂停阶段: {pause_step or '-'}",
        f"页面URL: {page_url}",
        f"消息: {message}",
        f"profile_types: {list(profile_types)}",
        f"contact_info_available 判定: {format_contact_available(contact_available)}",
        f"contact_info_available 原始值: {pformat(contact_available_raw, width=100)}",
        f"联系方式图标是否发现: {'是' if contact_badge_detected else '否'}",
        f"联系方式图标是否点击: {'是' if contact_badge_clicked else '否'}",
        f"联系方式图标点击策略: {contact_badge_strategy or '-'}",
        "",
        "最近一次 profile 请求:",
        last_profile_response_url or "-",
        "最近一次 profile 响应:",
        pformat(last_profile_payload, width=100) if last_profile_payload else "-",
        "",
        "最近一次 contact 请求:",
        last_contact_response_url or "-",
        "最近一次 contact 响应:",
        pformat(last_contact_payload, width=100) if last_contact_payload else "-",
    ]
    return "\n".join(lines)
