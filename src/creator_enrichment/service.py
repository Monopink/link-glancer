from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, Response, sync_playwright

from creator_enrichment.constants import (
    CONTACT_API_PATH,
    CONTACT_BADGE_CLICK_TIMEOUT_MS,
    CONTACT_BADGE_SCROLL_TIMEOUT_MS,
    CONTACT_BADGE_WAIT_SECONDS,
    CONTACT_ICON_CLASS_KEYWORDS,
    CONTACT_WAIT_SECONDS,
    DETAIL_URL_TEMPLATE,
    FAILURE_RETRY_LIMIT,
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
from creator_enrichment.diagnostics import build_diagnostic_text
from creator_enrichment.models import CreatorEnrichmentFailureAttempt, CreatorEnrichmentStatus
from creator_enrichment.page_script import network_capture_init_script
from creator_enrichment.parsers import (
    contact_info_available,
    contact_patch,
    nested_value,
    normalized_creator_id,
    normalized_region,
    parse_datetime,
    profile_request_metadata,
    query_param,
    remaining_regions_from_items,
    should_capture_profile,
    sorted_items_by_region,
)
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
    profile_types: tuple[int, ...]


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
        self._last_profile_response_url = ""
        self._last_profile_payload: dict[str, object] | None = None
        self._last_profile_types: tuple[int, ...] = ()
        self._last_profile_seen_at: datetime | None = None
        self._last_contact_response_url = ""
        self._last_contact_payload: dict[str, object] | None = None
        self._pause_step = "idle"
        self._last_contact_available: bool | None = None
        self._last_contact_available_raw: object = None
        self._last_contact_badge_detected = False
        self._last_contact_badge_clicked = False
        self._last_contact_badge_strategy = ""
        self._last_contact_badge_clicked_at: datetime | None = None
        self._contact_positive_signal = False
        self._profile_patch_applied = False
        self._failure_attempts: list[CreatorEnrichmentFailureAttempt] = []

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
            self._context.add_init_script(network_capture_init_script())
            self._page = self._context.new_page()
            if self._state.get("started_at") is None:
                self._state["started_at"] = now_iso()
                self._persist_state()
            self._started_at = parse_datetime(self._state.get("started_at"))
            self._completed = False
            self._paused = True
            self._pause_reason = None
            self._last_message = "请确认浏览器状态后开始采集。"
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
        self._pull_page_captures()
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
                if self._recent_profile_activity():
                    self._last_message = self._with_subject("补充采集中。")
                    return self.status()
                self._handle_retryable_manual_failure(
                    "当前页面未获取到达人资料接口响应，请处理页面后继续，或跳过当前达人。"
                )
            return self.status()

        if self._current_step == "waiting_contact":
            if self._captured_profile is not None:
                self._apply_profile()
                return self.status()
            if self._captured_contact is not None and self._profile_patch_applied:
                self._apply_contact()
                return self.status()
            if not self._profile_patch_applied and self._timed_out(PROFILE_WAIT_SECONDS):
                if self._recent_profile_activity():
                    self._last_message = self._with_subject("补充采集中。")
                    return self.status()
                self._handle_retryable_manual_failure(
                    "已确认存在联系方式，但达人资料接口未在限定时间内返回，请处理页面后继续，或跳过当前达人。"
                )
                return self.status()
            if self._profile_patch_applied and self._timed_out(CONTACT_WAIT_SECONDS):
                self._handle_retryable_manual_failure(
                    "已触发联系方式采集，但联系方式接口未在限定时间内返回，请处理后继续，或跳过当前达人。"
                )
            return self.status()

        if self._current_step == "waiting_contact_badge":
            if self._captured_contact is not None:
                self._current_step = "waiting_contact"
                self._contact_positive_signal = True
                return self.status()
            if self._last_contact_badge_clicked:
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                return self.status()
            if self._timed_out(CONTACT_BADGE_WAIT_SECONDS):
                self._handle_retryable_manual_failure(
                    self._with_subject(
                        "资料显示存在联系方式，但页面未出现可点击的联系方式图标，请处理后继续。"
                    )
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
        self._last_message = self._with_subject("补充采集中。")
        return self.status()

    def skip_current(self) -> CreatorEnrichmentStatus:
        if self._current_task_index is None:
            return self.status()
        subject = self._current_subject()
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_SKIPPED,
        )
        self._persist_state()
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._message_for_subject(subject, "已跳过。")
        return self.status()

    def retry_current(self) -> CreatorEnrichmentStatus:
        if self._context is None or self._page is None:
            self._last_message = "补充采集会话未启动。"
            return self.status()
        if self._current_task_index is None:
            self._last_message = "当前没有可重试的达人。"
            return self.status()
        self._paused = False
        self._pause_reason = None
        self._start_current_attempt(reload_page=True, clear_failure_history=True)
        self._last_message = self._with_subject("正在重试。")
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
            diagnostic_summary=self._diagnostic_summary(),
            diagnostic_text=self._diagnostic_text(),
            failure_attempts=self._failure_attempts_for_status(),
            attention_required=self._attention_required(),
            started_at=self._started_at,
            estimated_end_at=estimated_end_at,
        )

    def _eligible_items(self) -> list[TaskItem]:
        items = self._app_service.list_all_items(self._task_id)
        return [
            item for item in items if normalized_creator_id(item.task_data.get("creator_oecuid"))
        ]

    def _pending_items(self) -> list[TaskItem]:
        pending = []
        for item in self._eligible_items():
            status = str(self._item_state(item.task_index).get("status") or "")
            if not is_terminal_status(status):
                pending.append(item)
        return sorted_items_by_region(pending)

    def _advance_to_next_pending(self, *, open_page: bool) -> None:
        pending = self._pending_items()
        self._remaining_regions = remaining_regions_from_items(pending)
        if not pending:
            self._current_task_index = None
            self._current_region = None
            self._failure_attempts = []
            self._complete("补充采集完成。")
            return
        item = pending[0]
        if item.task_index != self._current_task_index:
            self._failure_attempts = []
        self._current_task_index = item.task_index
        self._current_region = normalized_region(item.task_data.get("selection_region"))
        if open_page:
            self._load_current_detail_page()

    def _load_current_detail_page(self) -> None:
        self._start_current_attempt(reload_page=False, clear_failure_history=True)

    def _start_current_attempt(self, *, reload_page: bool, clear_failure_history: bool) -> None:
        if self._page is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        if not creator_id:
            self.skip_current()
            return
        if clear_failure_history:
            self._failure_attempts = []
        self._reset_attempt_state()
        self._captured_profile = None
        self._captured_contact = None
        self._current_step = "waiting_profile"
        self._waiting_started_at = datetime.now(UTC)
        try:
            if reload_page:
                self._page.reload(wait_until="commit")
            else:
                self._page.goto(
                    DETAIL_URL_TEMPLATE.format(creator_oecuid=creator_id),
                    wait_until="commit",
                )
            self._page.bring_to_front()
        except PlaywrightError:
            self._handle_retryable_manual_failure(
                "无法打开达人详情页，请处理后继续，或跳过当前达人。"
            )

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
            creator_id, profile_types = profile_request_metadata(response)
            shop_region = query_param(response.url, "shop_region")
            self._last_profile_response_url = response.url
            self._last_profile_payload = payload
            self._last_profile_types = profile_types
            self._last_profile_seen_at = datetime.now(UTC)
            profile = payload.get("creator_profile")
            if not isinstance(profile, dict):
                profile = {}
            response_creator_id = normalized_creator_id(
                nested_value(profile.get("creator_oecuid")) or query_param(self._page.url, "cid")
            )
            should_capture = should_capture_profile(
                profile=profile,
                request_creator_id=creator_id,
                response_creator_id=response_creator_id,
                profile_types=profile_types,
            )
            if should_capture:
                self._store_captured_profile(
                    creator_id=response_creator_id or creator_id,
                    payload=payload,
                    shop_region=shop_region,
                    profile_types=profile_types,
                )
            return
        if path.endswith(CONTACT_API_PATH):
            creator_id = query_param(response.url, "creator_oecuid")
            self._last_contact_response_url = response.url
            self._last_contact_payload = payload
            if creator_id:
                self._contact_positive_signal = True
                self._captured_contact = _CapturedContact(creator_id=creator_id, payload=payload)

    def _pull_page_captures(self) -> None:
        if self._page is None:
            return
        try:
            captures = self._page.evaluate(
                """
                () => {
                    const root = window.__linkGlancerCapture;
                    const invalidRoot = !root
                        || !Array.isArray(root.profileResponses)
                        || !Array.isArray(root.contactResponses)
                        || !Array.isArray(root.badgeEvents);
                    if (invalidRoot) {
                        return { profileResponses: [], contactResponses: [], badgeEvents: [] };
                    }
                    const profileResponses = root.profileResponses.splice(
                        0,
                        root.profileResponses.length,
                    );
                    const contactResponses = root.contactResponses.splice(
                        0,
                        root.contactResponses.length,
                    );
                    const badgeEvents = root.badgeEvents.splice(
                        0,
                        root.badgeEvents.length,
                    );
                    return { profileResponses, contactResponses, badgeEvents };
                }
                """
            )
        except PlaywrightError:
            return
        if not isinstance(captures, dict):
            return
        profile_responses = captures.get("profileResponses")
        if isinstance(profile_responses, list):
            for entry in profile_responses:
                self._handle_page_profile_capture(entry)
        contact_responses = captures.get("contactResponses")
        if isinstance(contact_responses, list):
            for entry in contact_responses:
                self._handle_page_contact_capture(entry)
        badge_events = captures.get("badgeEvents")
        if isinstance(badge_events, list):
            for entry in badge_events:
                self._handle_page_badge_capture(entry)

    def _handle_page_profile_capture(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        profile = payload.get("creator_profile")
        if not isinstance(profile, dict):
            profile = {}
        creator_id = normalized_creator_id(nested_value(profile.get("creator_oecuid")))
        if not creator_id:
            creator_id = query_param(str(entry.get("pageUrl") or ""), "cid")
        if not creator_id:
            return
        shop_region = query_param(str(entry.get("url") or ""), "shop_region") or query_param(
            str(entry.get("pageUrl") or ""),
            "shop_region",
        )
        raw_profile_types = entry.get("profileTypes")
        profile_types: tuple[int, ...] = ()
        if isinstance(raw_profile_types, list):
            parsed_types: list[int] = []
            for item in raw_profile_types:
                try:
                    parsed_types.append(int(item))
                except (TypeError, ValueError):
                    continue
            profile_types = tuple(parsed_types)
        self._last_profile_response_url = str(entry.get("url") or "")
        self._last_profile_payload = payload
        self._last_profile_types = profile_types
        self._last_profile_seen_at = datetime.now(UTC)
        if should_capture_profile(
            profile=profile,
            request_creator_id=creator_id,
            response_creator_id=creator_id,
            profile_types=profile_types,
        ):
            self._store_captured_profile(
                creator_id=creator_id,
                payload=payload,
                shop_region=shop_region,
                profile_types=profile_types,
            )

    def _handle_page_contact_capture(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        url = str(entry.get("url") or "")
        creator_id = query_param(url, "creator_oecuid") or normalized_creator_id(
            entry.get("creatorId")
        )
        if not creator_id:
            return
        self._last_contact_response_url = url
        self._last_contact_payload = payload
        self._contact_positive_signal = True
        self._captured_contact = _CapturedContact(creator_id=creator_id, payload=payload)

    def _handle_page_badge_capture(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        detected = bool(entry.get("detected"))
        clicked = bool(entry.get("clicked"))
        strategy = str(entry.get("strategy") or "")
        if detected:
            self._last_contact_badge_detected = True
            self._contact_positive_signal = True
        if strategy:
            self._last_contact_badge_strategy = strategy
        if clicked and not self._last_contact_badge_clicked:
            self._last_contact_badge_clicked = True
            self._last_contact_badge_clicked_at = datetime.now(UTC)
        if not detected:
            return
        if not self._last_contact_badge_clicked:
            self._click_contact_badge()
        if self._last_contact_badge_clicked and self._current_step in {
            "waiting_profile",
            "waiting_contact_badge",
        }:
            self._current_step = "waiting_contact"
            self._waiting_started_at = datetime.now(UTC)
            self._last_message = self._with_subject("有联系方式，正在采集。")

    def _store_captured_profile(
        self,
        *,
        creator_id: str,
        payload: dict[str, object],
        shop_region: str,
        profile_types: tuple[int, ...],
    ) -> None:
        self._captured_profile = _CapturedProfile(
            creator_id=creator_id,
            payload=payload,
            shop_region=shop_region,
            profile_types=profile_types,
        )

    def _apply_profile(self) -> None:
        if self._captured_profile is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
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
            item_region = normalized_region(item.task_data.get("selection_region"))
            request_region = normalized_region(
                captured.shop_region or query_param(self._page.url, "shop_region")
            )
            if item_region and request_region and item_region != request_region:
                self._pause(
                    PAUSE_REASON_REGION_MISMATCH,
                    self._with_subject("当前店铺区域与达人区域不匹配，请处理后继续。"),
                )
                self._mark_current_paused(STATE_STATUS_PAUSED_REGION_MISMATCH)
                return
            self._handle_retryable_manual_failure(
                "达人资料接口返回异常，请处理页面后继续，或跳过当前达人。"
            )
            return

        item_region = normalized_region(item.task_data.get("selection_region"))
        request_region = normalized_region(
            captured.shop_region or query_param(self._page.url, "shop_region")
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
        bio = nested_value(profile.get("bio"))
        patch["bio"] = bio or "-"
        self._last_contact_available_raw = profile.get("contact_info_available")
        contact_available = contact_info_available(profile.get("contact_info_available"))
        self._last_contact_available = contact_available
        self._profile_patch_applied = True
        if contact_available is not None:
            patch["contact_info_available"] = "true" if contact_available else "false"
        if patch:
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=patch,
            )
            self._refresh_items()

        has_contact = (
            contact_available is True
            or self._contact_positive_signal
            or self._captured_contact is not None
            or self._last_contact_badge_clicked
        )

        if contact_available is False and not has_contact:
            self._mark_no_contact_and_advance()
            return
        if not has_contact and contact_available is not True:
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "达人资料已返回，但 contact_info_available 不是明确的 true/false，请人工处理。"
                )
            )
            return

        self._current_step = "waiting_contact_badge"
        self._waiting_started_at = datetime.now(UTC)
        self._last_message = self._with_subject("有联系方式，正在采集。")
        if self._captured_contact is not None:
            self._current_step = "waiting_contact"
            self._contact_positive_signal = True
            return
        if self._last_contact_badge_clicked:
            self._current_step = "waiting_contact"
            self._waiting_started_at = datetime.now(UTC)
            self._last_message = self._with_subject("有联系方式，正在采集。")
            return
        if self._click_contact_badge():
            self._contact_positive_signal = True
            self._current_step = "waiting_contact"
            self._waiting_started_at = datetime.now(UTC)
            self._last_message = self._with_subject("有联系方式，正在采集。")
        return

    def _apply_contact(self) -> None:
        if self._captured_contact is None or self._current_task_index is None:
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_contact
        self._captured_contact = None
        if captured.creator_id != creator_id:
            return
        payload = captured.payload
        if int(payload.get("code") or 0) != 0:
            self._handle_retryable_manual_failure(
                "联系方式接口返回异常，请处理后继续，或跳过当前达人。"
            )
            return
        parsed = contact_patch(payload)
        if parsed.patch:
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=parsed.patch,
            )
            self._refresh_items()
            new_status = STATE_STATUS_SUCCESS
            update_item_state(
                self._state,
                task_index=self._current_task_index,
                status=new_status,
                region=self._current_region or "",
            )
            self._persist_state()
            subject = self._current_subject()
            self._advance_to_next_pending(open_page=True)
            self._last_message = self._message_for_subject(subject, "已保存补充资料。")
            return
        if parsed.total_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "资料显示存在联系方式，但联系方式接口未返回任何联系方式数据，请人工处理。"
                )
            )
            return
        if parsed.valued_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject("联系方式接口已返回，但所有联系方式值均为空，请人工处理。")
            )
            return
        if parsed.recognized_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "联系方式接口已返回，但未解析到可识别的联系方式字段，请人工处理。"
                )
            )
            return

    def _click_contact_badge(self) -> bool:
        if self._page is None:
            return False
        if self._last_contact_badge_clicked:
            return True
        result = self._click_contact_badge_via_dom()
        if result["clicked"]:
            self._last_contact_badge_detected = bool(result["detected"])
            self._last_contact_badge_clicked = True
            self._last_contact_badge_strategy = str(result["strategy"] or "")
            self._last_contact_badge_clicked_at = datetime.now(UTC)
            self._contact_positive_signal = True
            return True
        self._last_contact_badge_detected = bool(result["detected"])
        if result["strategy"]:
            self._last_contact_badge_strategy = str(result["strategy"])
        try:
            container = self._page.locator("#creator-detail-profile-container").first
            if container.count() <= 0:
                self._last_contact_badge_strategy = "playwright:container_missing"
                return False
        except PlaywrightError:
            self._last_contact_badge_strategy = "playwright:container_error"
            return False
        selectors = [
            f"div.cursor-pointer svg[class*='alliance-icon-{keyword}']"
            for keyword in CONTACT_ICON_CLASS_KEYWORDS
        ]
        for selector in selectors:
            try:
                locator = container.locator(selector).first
                if locator.count() <= 0:
                    continue
                self._last_contact_badge_detected = True
                try:
                    locator.scroll_into_view_if_needed(timeout=CONTACT_BADGE_SCROLL_TIMEOUT_MS)
                except PlaywrightError:
                    pass
                target = locator.locator(
                    "xpath=ancestor::div[contains(@class, 'cursor-pointer')][1]"
                )
                clickable = target.first if target.count() > 0 else locator
                clickable.click(timeout=CONTACT_BADGE_CLICK_TIMEOUT_MS, force=True)
                self._last_contact_badge_clicked = True
                self._last_contact_badge_strategy = f"playwright:{selector}"
                self._last_contact_badge_clicked_at = datetime.now(UTC)
                self._contact_positive_signal = True
                return True
            except PlaywrightError:
                continue
        if not self._last_contact_badge_strategy:
            self._last_contact_badge_strategy = "playwright:not_found"
        return False

    def _click_contact_badge_via_dom(self, *, click: bool = True) -> dict[str, object]:
        if self._page is None:
            return {"detected": False, "clicked": False, "strategy": "dom:no_page"}
        try:
            result = self._page.evaluate(
                """
                ({ keywords, click }) => {
                    const container = document.querySelector("#creator-detail-profile-container");
                    if (!(container instanceof HTMLElement)) {
                        return {
                            detected: false,
                            clicked: false,
                            strategy: "dom:container_missing",
                        };
                    }
                    const isVisible = (element) => {
                        if (!(element instanceof HTMLElement)) {
                            return false;
                        }
                        const style = window.getComputedStyle(element);
                        if (style.display === "none" || style.visibility === "hidden") {
                            return false;
                        }
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const resolveClickable = (element) => {
                        let current = element;
                        while (current instanceof HTMLElement && current !== container) {
                            const tagName = current.tagName.toLowerCase();
                            if (
                                tagName === "button"
                                || tagName === "a"
                                || current.classList.contains("cursor-pointer")
                                || current.getAttribute("role") === "button"
                                || typeof current.onclick === "function"
                            ) {
                                return current;
                            }
                            current = current.parentElement;
                        }
                        return element instanceof HTMLElement ? element : null;
                    };
                    const icons = Array.from(
                        container.querySelectorAll(
                            "div.cursor-pointer svg[class*='alliance-icon-']",
                        ),
                    );
                    for (const icon of icons) {
                        if (!(icon instanceof SVGElement)) {
                            continue;
                        }
                        const className = icon.getAttribute("class") || "";
                        const matchedKeyword = keywords.find(
                            (keyword) => className.includes(keyword),
                        );
                        if (!matchedKeyword) {
                            continue;
                        }
                        const target = resolveClickable(icon.parentElement || icon);
                        if (!(target instanceof HTMLElement) || !isVisible(target)) {
                            return {
                                detected: true,
                                clicked: false,
                                strategy: `dom:${matchedKeyword}:not_visible`,
                            };
                        }
                        if (!click) {
                            return {
                                detected: true,
                                clicked: false,
                                strategy: `dom:${matchedKeyword}:visible`,
                            };
                        }
                        target.click();
                        return {
                            detected: true,
                            clicked: true,
                            strategy: `dom:${matchedKeyword}`,
                        };
                    }
                    return { detected: false, clicked: false, strategy: "dom:not_found" };
                }
                """,
                {"keywords": list(CONTACT_ICON_CLASS_KEYWORDS), "click": click},
            )
        except PlaywrightError:
            return {"detected": False, "clicked": False, "strategy": "dom:error"}
        if not isinstance(result, dict):
            return {"detected": False, "clicked": False, "strategy": "dom:invalid"}
        return {
            "detected": bool(result.get("detected")),
            "clicked": bool(result.get("clicked")),
            "strategy": str(result.get("strategy") or ""),
        }

    def _timed_out(self, seconds: int) -> bool:
        if self._waiting_started_at is None:
            return False
        return (datetime.now(UTC) - self._waiting_started_at).total_seconds() >= seconds

    def _pause(self, reason: str, message: str) -> None:
        self._paused = True
        self._pause_reason = reason
        self._last_message = message
        self._pause_step = self._current_step or "idle"
        self._current_step = "idle"
        self._waiting_started_at = None

    def _pause_manual_for_current(self, message: str) -> None:
        self._pause(PAUSE_REASON_MANUAL_ACTION, message)
        self._mark_current_paused(STATE_STATUS_PAUSED_MANUAL_ACTION)

    def _handle_retryable_manual_failure(self, message: str) -> None:
        attempt_index = len(self._failure_attempts) + 1
        self._failure_attempts.append(
            CreatorEnrichmentFailureAttempt(
                index=attempt_index,
                summary=message,
                diagnostic_text=self._build_diagnostic_text(
                    message=message,
                    pause_reason=PAUSE_REASON_MANUAL_ACTION,
                    pause_step=self._current_step or "idle",
                ),
            )
        )
        if attempt_index < FAILURE_RETRY_LIMIT:
            self._paused = False
            self._pause_reason = None
            self._pause_step = self._current_step or "idle"
            self._last_message = self._with_subject(
                f"补充采集异常，正在进行第 {attempt_index + 1} 次重试。"
            )
            self._start_current_attempt(reload_page=True, clear_failure_history=False)
            return
        self._pause_manual_for_current(message)

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

    def _reset_attempt_state(self) -> None:
        self._last_profile_response_url = ""
        self._last_profile_payload = None
        self._last_contact_response_url = ""
        self._last_contact_payload = None
        self._last_profile_types = ()
        self._last_profile_seen_at = None
        self._pause_step = "idle"
        self._last_contact_available = None
        self._last_contact_available_raw = None
        self._last_contact_badge_detected = False
        self._last_contact_badge_clicked = False
        self._last_contact_badge_strategy = ""
        self._last_contact_badge_clicked_at = None
        self._contact_positive_signal = False
        self._profile_patch_applied = False
        if self._page is not None:
            try:
                self._page.evaluate(
                    """
                    () => {
                        const root = window.__linkGlancerCapture;
                        if (!root) {
                            return;
                        }
                        root.profileResponses = [];
                        root.contactResponses = [];
                        root.badgeEvents = [];
                        root.badgeClickPageUrl = "";
                    }
                    """
                )
            except PlaywrightError:
                pass

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
        for key in ("handle", "nickname", "creator_oecuid"):
            value = str(item.task_data.get(key) or "").strip()
            if value:
                return value
        return None

    def _with_subject(self, message: str) -> str:
        subject = self._current_subject()
        return self._message_for_subject(subject, message)

    def _message_for_subject(self, subject: str | None, message: str) -> str:
        if not subject:
            return message
        return f"{subject}：{message}"

    def _mark_no_contact_and_advance(self) -> None:
        if self._current_task_index is None:
            return
        subject = self._current_subject()
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_NO_CONTACT,
            region=self._current_region or "",
        )
        self._persist_state()
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._message_for_subject(
            subject,
            "当前达人没有联系方式，已继续下一条。",
        )

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

    def _recent_profile_activity(self) -> bool:
        if self._last_profile_seen_at is None:
            return False
        return (datetime.now(UTC) - self._last_profile_seen_at).total_seconds() < 3

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

    def _diagnostic_summary(self) -> str | None:
        if not self._attention_required():
            return None
        parts = [self._last_message]
        if self._pause_step:
            parts.append(f"阶段：{self._pause_step}")
        if self._current_region:
            parts.append(f"区域：{self._current_region}")
        return " | ".join(part for part in parts if part)

    def _diagnostic_text(self) -> str | None:
        if not self._attention_required():
            return None
        if self._failure_attempts:
            return self._failure_attempts[-1].diagnostic_text
        return self._build_diagnostic_text(
            message=self._last_message,
            pause_reason=self._pause_reason,
            pause_step=self._pause_step,
        )

    def _failure_attempts_for_status(self) -> list[CreatorEnrichmentFailureAttempt] | None:
        if not self._attention_required() or not self._failure_attempts:
            return None
        return list(self._failure_attempts)

    def _build_diagnostic_text(
        self,
        *,
        message: str,
        pause_reason: str | None,
        pause_step: str | None,
    ) -> str:
        page_url = "-"
        if self._page is not None:
            try:
                page_url = self._page.url
            except PlaywrightError:
                page_url = "-"
        return build_diagnostic_text(
            task_id=self._task_id,
            current_task_index=self._current_task_index,
            current_subject=self._current_subject(),
            current_region=self._current_region,
            pause_reason=pause_reason,
            pause_step=pause_step,
            page_url=page_url,
            message=message,
            profile_types=self._last_profile_types,
            contact_available=self._last_contact_available,
            contact_available_raw=self._last_contact_available_raw,
            contact_badge_detected=self._last_contact_badge_detected,
            contact_badge_clicked=self._last_contact_badge_clicked,
            contact_badge_strategy=self._last_contact_badge_strategy,
            last_profile_response_url=self._last_profile_response_url,
            last_profile_payload=self._last_profile_payload,
            last_contact_response_url=self._last_contact_response_url,
            last_contact_payload=self._last_contact_payload,
        )

    def _attention_required(self) -> bool:
        if not self._paused or self._completed:
            return False
        return (
            self._pause_reason
            in {
                PAUSE_REASON_CAPTCHA,
                PAUSE_REASON_REGION_MISMATCH,
                PAUSE_REASON_MANUAL_ACTION,
            }
            and self._last_message != "补充采集已暂停。"
        )
