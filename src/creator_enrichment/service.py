from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, Response, sync_playwright

from creator_enrichment.constants import (
    CONTACT_API_PATH,
    CONTACT_BADGE_WAIT_SECONDS,
    CONTACT_WAIT_SECONDS,
    DETAIL_URL_TEMPLATE,
    KNOWN_CONTACT_FIELD_MAP,
    PAUSE_REASON_CAPTCHA,
    PAUSE_REASON_MANUAL_ACTION,
    PAUSE_REASON_REGION_MISMATCH,
    PLAYWRIGHT_ALLOWED_DEFAULT_ARGS,
    PROFILE_API_PATH,
    PROFILE_WAIT_SECONDS,
    STATE_STATUS_NO_CONTACT,
    STATE_STATUS_PAUSED_CAPTCHA,
    STATE_STATUS_PAUSED_MANUAL_ACTION,
    STATE_STATUS_PAUSED_REGION_MISMATCH,
    STATE_STATUS_SKIPPED,
    STATE_STATUS_SUCCESS,
)
from creator_enrichment.models import CreatorEnrichmentStatus
from creator_enrichment.state import is_terminal_status, normalize_state, now_iso, update_item_state
from link_glancer.application import TaskApplicationService
from link_glancer.browser.detector import detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import BrowserConfig, TaskItem


@dataclass(slots=True)
class _CapturedProfile:
    creator_id: str
    payload: dict[str, object]
    shop_region: str


@dataclass(slots=True)
class _CapturedContact:
    creator_id: str
    payload: dict[str, object]


class CreatorEnrichmentSession:
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        task_id: int,
        browser_config: BrowserConfig,
        state_key: str,
    ) -> None:
        self._app_service = app_service
        self._task_id = task_id
        self._browser_config = browser_config
        self._state_key = state_key
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._page = None
        self._paused = True
        self._completed = False
        self._last_message = "未启动"
        self._pause_reason: str | None = None
        self._current_task_index: int | None = None
        self._current_region: str | None = None
        self._remaining_regions: list[str] = []
        self._current_step = "idle"
        self._waiting_started_at: datetime | None = None
        self._started_at: datetime | None = None
        self._captured_profile: _CapturedProfile | None = None
        self._captured_contact: _CapturedContact | None = None
        self._items: list[TaskItem] = []
        self._items_by_index: dict[int, TaskItem] = {}
        self._state = normalize_state(self._app_service.load_app_setting(self._state_key))

    def start(self) -> CreatorEnrichmentStatus:
        self.shutdown()
        candidate = detect_browser("configured", self._browser_config.executable_path or None)
        if candidate is None:
            self._last_message = "未找到可用浏览器。"
            return self.status()

        environment_dir = ensure_browser_environment_dir(self._browser_config.profile_id)
        try:
            self._items = self._eligible_items()
            self._items_by_index = {item.task_index: item for item in self._items}
            self._playwright_manager = sync_playwright().start()
            self._playwright = cast(Playwright, self._playwright_manager)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(environment_dir),
                headless=False,
                executable_path=str(candidate.executable_path),
                args=self._browser_config.launch_args or [],
                ignore_default_args=PLAYWRIGHT_ALLOWED_DEFAULT_ARGS,
            )
            self._context.on("response", self._handle_response)
            self._page = self._context.new_page()
            if self._state.get("started_at") is None:
                self._state["started_at"] = now_iso()
                self._persist_state()
            self._started_at = _parse_datetime(self._state.get("started_at"))
            self._completed = False
            self._paused = True
            self._pause_reason = None
            self._last_message = "浏览器已启动，请确认登录、店铺区域与页面状态。"
            self._advance_to_next_pending(open_page=True)
        except PlaywrightError as exc:
            self._last_message = f"浏览器启动失败：{exc}"
            self.shutdown()
        return self.status()

    def poll(self) -> CreatorEnrichmentStatus:
        if self._context is None or self._page is None:
            return self.status()
        if not self._ensure_runtime_alive():
            return self.status()
        if self._completed or self._paused:
            return self.status()
        if self._current_task_index is None:
            self._complete("补充采集完成。")
            return self.status()
        if self._is_captcha_present():
            self._pause(
                PAUSE_REASON_CAPTCHA,
                self._with_subject("检测到人机验证，请先完成验证后继续。"),
            )
            self._mark_current_paused(STATE_STATUS_PAUSED_CAPTCHA)
            return self.status()

        if self._current_step == "waiting_profile":
            if self._captured_profile is not None:
                self._apply_profile()
                return self.status()
            if self._timed_out(PROFILE_WAIT_SECONDS):
                self._pause_manual_for_current(
                    "当前页面未获取到有效资料，请处理后继续，或跳过当前达人。"
                )
            return self.status()

        if self._current_step == "waiting_contact":
            if self._captured_contact is not None:
                self._apply_contact()
                return self.status()
            if self._timed_out(CONTACT_WAIT_SECONDS):
                self._pause_manual_for_current(
                    "联系方式请求未返回有效数据，请处理后继续，或跳过当前达人。"
                )
            return self.status()

        if self._current_step == "waiting_contact_badge":
            if self._click_contact_badge():
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                self._last_message = self._with_subject("正在获取联系方式。")
                return self.status()
            if self._timed_out(CONTACT_BADGE_WAIT_SECONDS):
                self._pause_manual_for_current(
                    self._with_subject("存在联系方式，但未能点击联系方式徽章，请处理后继续。")
                )
            return self.status()

        return self.status()

    def resume(self) -> CreatorEnrichmentStatus:
        if self._context is None or self._page is None:
            self._last_message = "补充采集会话未启动。"
            return self.status()
        if self._completed:
            self._last_message = "补充采集已完成。"
            return self.status()
        if self._current_task_index is None:
            self._advance_to_next_pending(open_page=True)
        else:
            self._load_current_detail_page()
        self._paused = False
        self._pause_reason = None
        self._last_message = self._with_subject("正在采集。")
        return self.status()

    def skip_current(self) -> CreatorEnrichmentStatus:
        if self._current_task_index is None:
            return self.status()
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_SKIPPED,
        )
        self._persist_state()
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._with_subject("已跳过。")
        return self.status()

    def stop(self) -> CreatorEnrichmentStatus:
        self._paused = True
        self._pause_reason = PAUSE_REASON_MANUAL_ACTION
        self._last_message = "补充采集已暂停。"
        return self.status()

    def shutdown(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
        if self._playwright_manager is not None:
            try:
                self._playwright_manager.stop()
            except PlaywrightError:
                pass
        self._playwright_manager = None
        self._playwright = None
        self._context = None
        self._page = None
        self._captured_profile = None
        self._captured_contact = None
        self._current_step = "idle"

    def status(self) -> CreatorEnrichmentStatus:
        total_count = len(self._eligible_items())
        counts = self._status_counts()
        estimated_end_at = None
        started_at = self._started_at
        if (
            started_at is not None
            and counts["completed"] > 0
            and total_count >= counts["completed"]
        ):
            elapsed_seconds = max(int((datetime.now(UTC) - started_at).total_seconds()), 1)
            projected_total = int(elapsed_seconds * total_count / counts["completed"])
            estimated_end_at = started_at + timedelta(seconds=projected_total)
        return CreatorEnrichmentStatus(
            running=self._context is not None,
            paused=self._paused,
            completed=self._completed,
            total_count=total_count,
            completed_count=counts["completed"],
            success_count=counts["success"],
            no_contact_count=counts["no_contact"],
            skipped_count=counts["skipped"],
            failed_count=counts["paused"],
            current_task_index=self._current_task_index,
            current_region=self._current_region,
            remaining_regions=list(self._remaining_regions),
            last_message=self._last_message,
            pause_reason=self._pause_reason,
            started_at=self._started_at,
            estimated_end_at=estimated_end_at,
        )

    def _eligible_items(self) -> list[TaskItem]:
        items = self._app_service.list_all_items(self._task_id)
        return [
            item for item in items if _normalized_creator_id(item.task_data.get("creator_oecuid"))
        ]

    def _pending_items(self) -> list[TaskItem]:
        pending = []
        for item in self._eligible_items():
            status = str(self._item_state(item.task_index).get("status") or "")
            if not is_terminal_status(status):
                pending.append(item)
        return _sorted_items_by_region(pending)

    def _advance_to_next_pending(self, *, open_page: bool) -> None:
        pending = self._pending_items()
        self._remaining_regions = _remaining_regions_from_items(pending)
        if not pending:
            self._current_task_index = None
            self._current_region = None
            self._complete("补充采集完成。")
            return
        item = pending[0]
        self._current_task_index = item.task_index
        self._current_region = _normalized_region(item.task_data.get("selection_region"))
        if open_page:
            self._load_current_detail_page()

    def _load_current_detail_page(self) -> None:
        if self._page is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = _normalized_creator_id(item.task_data.get("creator_oecuid"))
        if not creator_id:
            self.skip_current()
            return
        self._captured_profile = None
        self._captured_contact = None
        self._current_step = "waiting_profile"
        self._waiting_started_at = datetime.now(UTC)
        try:
            self._page.goto(
                DETAIL_URL_TEMPLATE.format(creator_oecuid=creator_id),
                wait_until="commit",
            )
            self._page.bring_to_front()
        except PlaywrightError:
            self._pause_manual_for_current("无法打开达人详情页，请处理后继续，或跳过当前达人。")

    def _handle_response(self, response: Response) -> None:
        try:
            path = urlsplit(response.url).path
        except ValueError:
            return
        try:
            payload = response.json()
        except (PlaywrightError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if path.endswith(PROFILE_API_PATH):
            creator_id = _profile_creator_id(response)
            shop_region = _query_param(response.url, "shop_region")
            if creator_id:
                self._captured_profile = _CapturedProfile(
                    creator_id=creator_id,
                    payload=payload,
                    shop_region=shop_region,
                )
            return
        if path.endswith(CONTACT_API_PATH):
            creator_id = _query_param(response.url, "creator_oecuid")
            if creator_id:
                self._captured_contact = _CapturedContact(creator_id=creator_id, payload=payload)

    def _apply_profile(self) -> None:
        if self._captured_profile is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = _normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_profile
        self._captured_profile = None
        if captured.creator_id != creator_id:
            return
        payload = captured.payload
        if int(payload.get("code") or 0) != 0:
            if self._is_captcha_present():
                self._pause(PAUSE_REASON_CAPTCHA, "检测到人机验证，请先完成验证后继续。")
                self._mark_current_paused(STATE_STATUS_PAUSED_CAPTCHA)
                return
            item_region = _normalized_region(item.task_data.get("selection_region"))
            request_region = _normalized_region(
                captured.shop_region or _query_param(self._page.url, "shop_region")
            )
            if item_region and request_region and item_region != request_region:
                self._pause(
                    PAUSE_REASON_REGION_MISMATCH,
                    self._with_subject("当前店铺区域与达人区域不匹配，请处理后继续。"),
                )
                self._mark_current_paused(STATE_STATUS_PAUSED_REGION_MISMATCH)
                return
            self._pause_manual_for_current("达人详情未返回有效数据，请处理后继续，或跳过当前达人。")
            return

        item_region = _normalized_region(item.task_data.get("selection_region"))
        request_region = _normalized_region(
            captured.shop_region or _query_param(self._page.url, "shop_region")
        )
        if item_region and request_region and item_region != request_region:
            self._pause(
                PAUSE_REASON_REGION_MISMATCH,
                self._with_subject("当前店铺区域与达人区域不匹配，请处理后继续。"),
            )
            self._mark_current_paused(STATE_STATUS_PAUSED_REGION_MISMATCH)
            return

        profile = payload.get("creator_profile")
        if not isinstance(profile, dict):
            profile = {}
        patch: dict[str, object] = {}
        bio = _nested_value(profile.get("bio"))
        patch["bio"] = bio or "-"
        contact_available = _contact_info_available(profile.get("contact_info_available"))
        if contact_available is not None:
            patch["contact_info_available"] = "true" if contact_available else "false"
        if patch:
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=patch,
            )
            self._refresh_items()

        if contact_available is False:
            self._mark_no_contact_and_advance()
            return

        self._current_step = "waiting_contact_badge"
        self._waiting_started_at = datetime.now(UTC)
        self._last_message = self._with_subject("正在等待联系方式徽章。")
        if self._click_contact_badge():
            self._current_step = "waiting_contact"
            self._waiting_started_at = datetime.now(UTC)
            self._last_message = self._with_subject("正在获取联系方式。")
            return
        return

    def _apply_contact(self) -> None:
        if self._captured_contact is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = _normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_contact
        self._captured_contact = None
        if captured.creator_id != creator_id:
            return
        payload = captured.payload
        if int(payload.get("code") or 0) != 0:
            self._pause_manual_for_current("联系方式请求失败，请处理后继续，或跳过当前达人。")
            return
        patch = _contact_patch(payload)
        if patch:
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=patch,
            )
            self._refresh_items()
            new_status = STATE_STATUS_SUCCESS
        else:
            new_status = STATE_STATUS_NO_CONTACT
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=new_status,
            region=self._current_region or "",
        )
        self._persist_state()
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._with_subject("已保存补充资料。")

    def _click_contact_badge(self) -> bool:
        if self._page is None:
            return False
        try:
            return bool(
                self._page.evaluate(
                    """
                    () => {
                        const icon = document.querySelector("div.cursor-pointer svg.alliance-icon");
                        if (!icon) {
                            return false;
                        }
                        const target = icon.closest("div.cursor-pointer");
                        if (!(target instanceof HTMLElement)) {
                            return false;
                        }
                        target.click();
                        return true;
                    }
                    """
                )
            )
        except PlaywrightError:
            return False

    def _timed_out(self, seconds: int) -> bool:
        if self._waiting_started_at is None:
            return False
        return (datetime.now(UTC) - self._waiting_started_at).total_seconds() >= seconds

    def _pause(self, reason: str, message: str) -> None:
        self._paused = True
        self._pause_reason = reason
        self._last_message = message
        self._current_step = "idle"
        self._waiting_started_at = None

    def _pause_manual_for_current(self, message: str) -> None:
        self._pause(PAUSE_REASON_MANUAL_ACTION, message)
        self._mark_current_paused(STATE_STATUS_PAUSED_MANUAL_ACTION)

    def _mark_current_paused(self, paused_status: str) -> None:
        if self._current_task_index is None:
            return
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=paused_status,
            reason=self._last_message,
            region=self._current_region or "",
        )
        self._persist_state()

    def _complete(self, message: str) -> None:
        self._completed = True
        self._paused = True
        self._pause_reason = None
        self._current_step = "idle"
        self._waiting_started_at = None
        self._last_message = message

    def _persist_state(self) -> None:
        self._app_service.save_app_setting(self._state_key, self._state)

    def _item_state(self, task_index: int) -> dict[str, object]:
        statuses = self._state.setdefault("statuses", {})
        assert isinstance(statuses, dict)
        raw = statuses.get(str(task_index))
        if not isinstance(raw, dict):
            raw = {"status": "pending", "reason": "", "updated_at": "", "region": ""}
            statuses[str(task_index)] = raw
        return raw

    def _refresh_items(self) -> None:
        self._items = self._eligible_items()
        self._items_by_index = {item.task_index: item for item in self._items}

    def _current_subject(self) -> str | None:
        if self._current_task_index is None:
            return None
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            return None
        for key in ("nickname", "handle", "creator_oecuid"):
            value = str(item.task_data.get(key) or "").strip()
            if value:
                return value
        return None

    def _with_subject(self, message: str) -> str:
        subject = self._current_subject()
        if not subject:
            return message
        return f"{subject}：{message}"

    def _mark_no_contact_and_advance(self) -> None:
        if self._current_task_index is None:
            return
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_NO_CONTACT,
            region=self._current_region or "",
        )
        self._persist_state()
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._with_subject("没有联系方式，已继续下一个。")

    def _status_counts(self) -> dict[str, int]:
        counts = {"completed": 0, "success": 0, "no_contact": 0, "skipped": 0, "paused": 0}
        for item in self._eligible_items():
            status = str(self._item_state(item.task_index).get("status") or "")
            if is_terminal_status(status):
                counts["completed"] += 1
            if status == STATE_STATUS_SUCCESS:
                counts["success"] += 1
            elif status == STATE_STATUS_NO_CONTACT:
                counts["no_contact"] += 1
            elif status == STATE_STATUS_SKIPPED:
                counts["skipped"] += 1
            elif status.startswith("paused_"):
                counts["paused"] += 1
        return counts

    def _ensure_runtime_alive(self) -> bool:
        if self._context is None:
            return False
        try:
            if not self._context.pages:
                self._last_message = "浏览器已关闭。"
                self.shutdown()
                return False
        except PlaywrightError:
            self._last_message = "浏览器已关闭。"
            self.shutdown()
            return False
        return True

    def _is_captcha_present(self) -> bool:
        if self._page is None:
            return False
        try:
            return bool(
                self._page.evaluate(
                    """
                    () => {
                        const selectors = [
                            ".captcha_verify_container",
                            "#captcha-verify-image",
                            "#secsdk-captcha-drag-wrapper",
                        ];
                        for (const selector of selectors) {
                            if (document.querySelector(selector)) {
                                return true;
                            }
                        }
                        const text = document.body?.innerText || "";
                        return text.includes("请完成下列验证后继续")
                            || text.includes("按住左边按钮拖动完成上方拼图");
                    }
                    """
                )
            )
        except PlaywrightError:
            return False


def _nested_value(raw: object) -> str:
    if isinstance(raw, dict):
        value = raw.get("value")
        if value is None:
            return ""
        return str(value)
    if raw is None:
        return ""
    return str(raw)


def _contact_info_available(raw: object) -> bool | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return None


def _profile_creator_id(response: Response) -> str:
    request = response.request
    try:
        post_data = request.post_data or ""
    except PlaywrightError:
        return ""
    try:
        payload = json.loads(post_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    creator_id = payload.get("creator_oec_id")
    return str(creator_id or "").strip()


def _query_param(url: str, key: str) -> str:
    try:
        values = parse_qs(urlsplit(url).query).get(key)
    except ValueError:
        return ""
    if not values:
        return ""
    return str(values[0] or "").strip()


def _contact_patch(payload: dict[str, object]) -> dict[str, object]:
    contact_info = payload.get("contact_info")
    if not isinstance(contact_info, list):
        return {}
    grouped: dict[str, list[str]] = {}
    for item in contact_info:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        try:
            field_number = int(field)
        except (TypeError, ValueError):
            continue
        field_name = KNOWN_CONTACT_FIELD_MAP.get(field_number, f"contact_{field_number}")
        grouped.setdefault(field_name, [])
        if value not in grouped[field_name]:
            grouped[field_name].append(value)
    return {
        field_name: values[0] if len(values) == 1 else "; ".join(values)
        for field_name, values in grouped.items()
    }


def _normalized_region(raw: object) -> str:
    return str(raw or "").strip().upper()


def _normalized_creator_id(raw: object) -> str:
    value = str(raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _sorted_items_by_region(items: list[TaskItem]) -> list[TaskItem]:
    def sort_key(item: TaskItem) -> tuple[int, str, int]:
        region = _normalized_region(item.task_data.get("selection_region"))
        if not region:
            return (1, "ZZZ", item.task_index)
        return (0, region, item.task_index)

    return sorted(items, key=sort_key)


def _remaining_regions_from_items(items: list[TaskItem]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        region = _normalized_region(item.task_data.get("selection_region")) or "UNKNOWN"
        if region in seen:
            continue
        seen.add(region)
        result.append(region)
    return result


def _parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
