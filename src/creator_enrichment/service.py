from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from creator_enrichment.browser_guard import BrowserGuardMixin
from creator_enrichment.capture_pipeline import (
    CapturePipelineMixin,
    _CapturedContact,
    _CapturedProfile,
)
from creator_enrichment.constants import (
    BLOCKED_RESOURCE_HOSTS,
    BLOCKED_RESOURCE_PATH_MARKERS,
    BLOCKED_RESOURCE_TYPES,
    CONTACT_BADGE_WAIT_SECONDS,
    CONTACT_WAIT_SECONDS,
    DETAIL_URL_TEMPLATE,
    FAILURE_RETRY_LIMIT,
    PAUSE_REASON_CAPTCHA,
    PAUSE_REASON_MANUAL_ACTION,
    PAUSE_REASON_REGION_MISMATCH,
    PLAYWRIGHT_ALLOWED_DEFAULT_ARGS,
    PROFILE_API_PATH,
    PROFILE_TYPES_ALLOWLIST,
    PROFILE_WAIT_SECONDS,
    STATE_STATUS_AUTO_SKIPPED,
    STATE_STATUS_NO_CONTACT,
    STATE_STATUS_PAUSED_CAPTCHA,
    STATE_STATUS_PAUSED_MANUAL_ACTION,
    STATE_STATUS_SKIPPED,
    STATE_STATUS_SUCCESS,
)
from creator_enrichment.contact_badge import (
    CONTACT_BADGE_LOGIC_VERSION,
    ContactBadgeMixin,
)
from creator_enrichment.diagnostics import build_diagnostic_text
from creator_enrichment.models import CreatorEnrichmentFailureAttempt, CreatorEnrichmentStatus
from creator_enrichment.page_script import (
    enrichment_collection_mode_script,
    network_capture_init_script,
)
from creator_enrichment.parsers import (
    normalized_creator_id,
    normalized_region,
    parse_datetime,
    remaining_regions_from_items,
    sorted_items_by_region,
)
from creator_enrichment.state import is_terminal_status, normalize_state, now_iso, update_item_state
from link_glancer.application import TaskApplicationService
from link_glancer.browser.detector import detect_browser
from link_glancer.runtime.dev import JsonlDevLogger, is_dev_mode
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import BrowserConfig, TaskItem

AUTO_START_PROFILE_WAIT_SECONDS = 8
PROFILE_WAIT_GRACE_SECONDS = 4
ATTENTION_PAUSE_REASONS = {
    PAUSE_REASON_CAPTCHA,
    PAUSE_REASON_REGION_MISMATCH,
    PAUSE_REASON_MANUAL_ACTION,
}


class CreatorEnrichmentSession(BrowserGuardMixin, CapturePipelineMixin, ContactBadgeMixin):
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        task_id: int,
        browser_config: BrowserConfig,
        confirm_url: str | None,
        state_key: str,
    ) -> None:
        self._app_service = app_service
        self._task_id = task_id
        self._browser_config = browser_config
        self._confirm_url = (confirm_url or "").strip() or None
        self._state_key = state_key
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._page = None
        self._work_page_task_index: int | None = None
        self._collection_mode_installed = False
        self._paused = True
        self._completed = False
        self._last_message = "未启动"
        self._pause_reason: str | None = None
        self._current_task_index: int | None = None
        self._current_region: str | None = None
        self._remaining_regions: list[str] = []
        self._startup_phase = "idle"
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
        self._contact_badge_click_inflight = False
        self._contact_positive_signal = False
        self._profile_patch_applied = False
        self._failure_attempts: list[CreatorEnrichmentFailureAttempt] = []
        self._collection_started = False
        self._route_installed = False
        self._auto_skip_on_failure = False
        self._dev_logger: JsonlDevLogger | None = None
        self._attempt_page_url = ""
        self._attempt_creator_id = ""
        self._attempt_in_progress = False
        self._profile_wait_grace_used = False
        self._finalizing_task_indexes: set[int] = set()
        self._run_started_at: datetime | None = None
        self._run_completed_baseline = 0
        self._paused_started_at: datetime | None = None
        self._diagnostic_pages: set[int] = set()
        self._last_dom_profile_signature = ""
        self._last_dom_profile_seen_at: datetime | None = None
        self._last_dom_profile_probe_at: datetime | None = None
        self._page_guard_breached = False
        self._page_guard_breach_reason = ""

    def start(self) -> CreatorEnrichmentStatus:
        self.shutdown()
        if is_dev_mode():
            self._dev_logger = JsonlDevLogger(
                module="creator_enrichment",
                file_stem=f"creator_enrichment_task_{self._task_id}",
            )
            self._log_event(
                "session_start",
                contact_badge_logic_version=CONTACT_BADGE_LOGIC_VERSION,
                jsonl_path=str(self._dev_logger.path),
                artifact_dir=str(self._dev_logger.artifact_dir),
            )
        candidate = detect_browser("configured", self._browser_config.executable_path or None)
        if candidate is None:
            self._last_message = "未找到可用浏览器。"
            self._log_event("session_error", reason="browser_not_found")
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
            self._context.on("page", self._handle_context_page)
            self._context.add_init_script(network_capture_init_script())
            if not self._ensure_single_work_page(reason="start"):
                raise PlaywrightError(self._last_message or "无法创建补充采集工作页面。")
            if self._state.get("started_at") is None:
                self._state["started_at"] = now_iso()
                self._persist_state()
            self._started_at = parse_datetime(self._state.get("started_at"))
            self._run_started_at = datetime.now(UTC)
            self._run_completed_baseline = self._status_counts()["completed"]
            self._paused_started_at = None
            self._completed = False
            self._paused = True
            self._pause_reason = None
            self._startup_phase = "browser_confirm"
            self._collection_started = False
            self._route_installed = False
            self._last_message = "请确认浏览器状态正常，确认后将自动开始补充采集。"
            self._advance_to_next_pending(open_page=False)
            self._open_browser_confirmation_page()
        except PlaywrightError as exc:
            self._last_message = f"浏览器启动失败：{exc}"
            self._log_event("session_error", reason="browser_launch_failed", details=str(exc))
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
            if self._maybe_capture_profile_from_dom():
                return self.status()
            if self._timed_out(self._current_profile_wait_seconds()):
                if self._recent_profile_activity():
                    self._last_message = self._with_subject("补充采集中。")
                    return self.status()
                if self._maybe_extend_profile_wait():
                    return self.status()
                self._handle_retryable_manual_failure(
                    "当前页面未获取到达人资料接口响应，请处理页面后继续，或跳过当前达人。"
                )
            return self.status()

        if self._current_step == "waiting_contact":
            if self._captured_profile is not None:
                self._apply_profile()
                return self.status()
            if not self._profile_patch_applied and self._maybe_capture_profile_from_dom():
                return self.status()
            if self._captured_contact is not None and self._profile_patch_applied:
                self._apply_contact()
                return self.status()
            if not self._profile_patch_applied and self._timed_out(
                self._current_profile_wait_seconds()
            ):
                if self._recent_profile_activity():
                    self._last_message = self._with_subject("补充采集中。")
                    return self.status()
                if self._maybe_extend_profile_wait():
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
            if self._contact_badge_click_inflight:
                self._last_message = self._with_subject("有联系方式，正在采集。")
                return self.status()
            if self._last_contact_badge_clicked:
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                return self.status()
            if self._click_contact_badge():
                self._contact_positive_signal = True
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                self._last_message = self._with_subject("有联系方式，正在采集。")
                self._log_event(
                    "badge_clicked",
                    task_index=self._current_task_index,
                    source="service",
                )
                return self.status()
            if self._timed_out(CONTACT_BADGE_WAIT_SECONDS):
                self._handle_retryable_manual_failure(
                    self._with_subject(
                        "资料显示存在联系方式，但页面未出现可点击的联系方式图标，请处理后继续。"
                    )
                )
            return self.status()

        return self.status()

    def resume(self, *, auto_skip_on_failure: bool = False) -> CreatorEnrichmentStatus:
        if self._context is None or self._page is None:
            self._last_message = "补充采集会话未启动。"
            return self.status()
        if self._completed:
            self._last_message = "补充采集已完成。"
            return self.status()
        self._auto_skip_on_failure = auto_skip_on_failure
        self._startup_phase = "collecting"
        if self._run_started_at is None:
            self._run_started_at = datetime.now(UTC)
            self._run_completed_baseline = self._status_counts()["completed"]
        self._resume_eta_clock()
        self._ensure_resource_blocking()
        if self._current_task_index is None:
            self._advance_to_next_pending(open_page=True)
        else:
            self._sync_current_page()
        self._collection_started = True
        self._paused = False
        self._pause_reason = None
        self._last_message = self._with_subject("补充采集中。")
        self._start_current_attempt(reload_page=False, clear_failure_history=True)
        self._log_event("resume", task_index=self._current_task_index)
        return self.status()

    def prepare_pages(self) -> CreatorEnrichmentStatus:
        if self._context is None or self._page is None:
            self._last_message = "补充采集会话未启动。"
            return self.status()
        if self._completed:
            self._last_message = "补充采集已完成。"
            return self.status()
        if self._current_task_index is None:
            self._advance_to_next_pending(open_page=False)
        self._ensure_resource_blocking()
        self._collection_started = True
        self._startup_phase = "collecting"
        self._log_event("prepare_pages", task_index=self._current_task_index)
        self._sync_current_page()
        self._resume_eta_clock()
        self._paused = False
        self._pause_reason = None
        self._last_message = self._with_subject("正在自动开始补充采集。")
        self._start_current_attempt(reload_page=False, clear_failure_history=True)
        return self.status()

    def skip_current(self) -> CreatorEnrichmentStatus:
        if self._current_task_index is None:
            return self.status()
        self._begin_task_finalization(self._current_task_index)
        subject = self._current_subject()
        self._log_event("skip", task_index=self._current_task_index, reason="manual")
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_SKIPPED,
        )
        self._persist_state()
        self._paused = False
        self._pause_reason = None
        self._paused_started_at = None
        self._pause_step = "idle"
        self._current_step = "idle"
        self._waiting_started_at = None
        self._attempt_in_progress = False
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._message_for_subject(subject, "已跳过。")
        self._continue_after_terminal_transition()
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
        self._resume_eta_clock()
        self._sync_current_page()
        self._start_current_attempt(reload_page=True, clear_failure_history=True)
        self._last_message = self._with_subject("正在重试。")
        self._log_event("retry", task_index=self._current_task_index, reason="manual")
        return self.status()

    def stop(self) -> CreatorEnrichmentStatus:
        if not self._paused:
            self._paused_started_at = datetime.now(UTC)
        self._paused = True
        self._pause_reason = PAUSE_REASON_MANUAL_ACTION
        self._last_message = "补充采集已暂停。"
        self._pause_step = "idle"
        self._attempt_in_progress = False
        self._current_step = "idle"
        self._waiting_started_at = None
        self._log_event(
            "pause",
            task_index=self._current_task_index,
            reason=PAUSE_REASON_MANUAL_ACTION,
        )
        return self.status()

    def shutdown(self) -> None:
        self._log_event("session_end", task_index=self._current_task_index)
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
        self._work_page_task_index = None
        self._collection_mode_installed = False
        self._captured_profile = None
        self._captured_contact = None
        self._attempt_page_url = ""
        self._attempt_creator_id = ""
        self._attempt_in_progress = False
        self._finalizing_task_indexes = set()
        self._run_started_at = None
        self._run_completed_baseline = 0
        self._paused_started_at = None
        self._diagnostic_pages = set()
        self._last_dom_profile_signature = ""
        self._last_dom_profile_seen_at = None
        self._last_dom_profile_probe_at = None
        self._page_guard_breached = False
        self._page_guard_breach_reason = ""
        self._startup_phase = "idle"
        self._current_step = "idle"
        self._collection_started = False
        self._route_installed = False
        self._dev_logger = None

    def status(self) -> CreatorEnrichmentStatus:
        total_count = len(self._eligible_items())
        counts = self._status_counts()
        estimated_end_at = None
        started_at = self._run_started_at
        reference_now = self._paused_started_at or datetime.now(UTC)
        completed_delta = max(counts["completed"] - self._run_completed_baseline, 0)
        if started_at is not None and completed_delta > 0 and total_count >= counts["completed"]:
            elapsed_seconds = max(int((reference_now - started_at).total_seconds()), 1)
            remaining_count = max(total_count - counts["completed"], 0)
            projected_remaining = int(elapsed_seconds * remaining_count / completed_delta)
            estimated_end_at = reference_now + timedelta(seconds=projected_remaining)
        return CreatorEnrichmentStatus(
            running=self._context is not None,
            paused=self._paused,
            completed=self._completed,
            startup_phase=self._startup_phase,
            total_count=total_count,
            completed_count=counts["completed"],
            success_count=counts["success"],
            no_contact_count=counts["no_contact"],
            auto_skipped_count=counts["auto_skipped"],
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
        self._log_event(
            "advance_next",
            task_index=self._current_task_index,
            region=self._current_region,
            open_page=open_page,
        )
        if open_page:
            self._sync_current_page()

    def _open_browser_confirmation_page(self) -> None:
        if self._page is None:
            return
        confirmation_url = self._resolve_browser_confirmation_url()
        if not confirmation_url:
            return
        self._navigate_page(self._page, confirmation_url)
        if self._current_task_index is not None and confirmation_url == self._detail_url_for_task(
            self._current_task_index
        ):
            self._assign_page_task_index(self._page, self._current_task_index)
        else:
            self._assign_page_task_index(self._page, None)
        self._bring_page_to_front(self._page)

    def _resolve_browser_confirmation_url(self) -> str | None:
        if self._confirm_url:
            return self._confirm_url
        if self._current_task_index is None:
            return None
        return self._detail_url_for_task(self._current_task_index)

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
        shop_region = normalized_region(item.task_data.get("selection_region"))
        target_url = DETAIL_URL_TEMPLATE.format(
            creator_oecuid=creator_id,
            shop_region=shop_region or "",
        )
        if (
            self._attempt_in_progress
            and not reload_page
            and self._attempt_creator_id == creator_id
            and self._current_step
            in {"waiting_profile", "waiting_contact_badge", "waiting_contact"}
        ):
            self._log_event(
                "open_start_skipped",
                task_index=self._current_task_index,
                creator_oecuid=creator_id,
                reason="attempt_already_in_progress",
            )
            return
        if clear_failure_history:
            self._failure_attempts = []
        self._reset_attempt_state()
        self._captured_profile = None
        self._captured_contact = None
        self._attempt_in_progress = True
        self._current_step = "waiting_profile"
        self._waiting_started_at = datetime.now(UTC)
        self._attempt_creator_id = creator_id
        self._attempt_page_url = target_url
        self._log_event(
            "open_start",
            task_index=self._current_task_index,
            creator_oecuid=creator_id,
            reload_page=reload_page,
        )
        try:
            normalize_reason = "attempt_retry_reset" if reload_page else "attempt_start"
            if not self._ensure_single_work_page(
                reason=normalize_reason,
                force_reset=reload_page,
            ):
                self._handle_retryable_manual_failure(
                    "补充采集工作页面不可用，请处理后继续，或跳过当前达人。"
                )
                return
            if reload_page:
                self._clear_cached_capture_for_task(self._current_task_index)
                self._navigate_page(self._page, target_url)
                self._assign_page_task_index(self._page, self._current_task_index)
                self._log_event(
                    "open_committed",
                    task_index=self._current_task_index,
                    navigation_mode="goto_after_retry_reset",
                )
            elif not self._current_page_matches_current_item():
                self._page.goto(target_url, wait_until="commit")
                self._assign_page_task_index(self._page, self._current_task_index)
                self._log_event(
                    "open_committed",
                    task_index=self._current_task_index,
                    navigation_mode="goto",
                )
            else:
                self._log_event(
                    "resume_reuse_page",
                    task_index=self._current_task_index,
                    page_url=self._safe_page_url(),
                )
            if not self._current_page_matches_current_item():
                self._repair_work_page_if_needed(
                    reason="attempt_post_open",
                    task_index=self._current_task_index,
                    target_url=target_url,
                )
            self._bring_page_to_front(self._page)
            if self._collection_started:
                self._ensure_collection_mode()
            self._load_cached_captures_for_current_task()
            self._process_captured_events(trigger="attempt_start")
        except PlaywrightError:
            self._log_event("open_failed", task_index=self._current_task_index)
            self._handle_retryable_manual_failure(
                "无法打开达人详情页，请处理后继续，或跳过当前达人。"
            )

    def _handle_route(self, route) -> None:
        try:
            request = route.request
            if self._collection_started:
                if request.resource_type in BLOCKED_RESOURCE_TYPES or _should_block_resource_url(
                    request.url
                ):
                    route.abort()
                    return
                if _should_block_profile_request(request.url, request.post_data or ""):
                    route.abort()
                    return
        except Exception:  # noqa: BLE001
            pass
        try:
            route.continue_()
        except Exception:  # noqa: BLE001
            pass

    def _ensure_resource_blocking(self) -> None:
        if self._context is None or self._route_installed:
            return
        self._context.route("**/*", self._handle_route)
        self._route_installed = True

    def _ensure_collection_mode(self) -> None:
        if self._page is None:
            return
        self._ensure_collection_mode_for_page(self._page)

    def _ensure_collection_mode_for_page(self, page: Page) -> None:
        if page is not self._page:
            return
        if self._collection_mode_installed:
            return
        try:
            page.evaluate(enrichment_collection_mode_script())
        except PlaywrightError:
            return
        self._collection_mode_installed = True

    def _timed_out(self, seconds: int) -> bool:
        if self._waiting_started_at is None:
            return False
        return (datetime.now(UTC) - self._waiting_started_at).total_seconds() >= seconds

    def _current_profile_wait_seconds(self) -> int:
        base_seconds = PROFILE_WAIT_SECONDS
        if self._run_completed_baseline == self._status_counts()["completed"]:
            base_seconds = AUTO_START_PROFILE_WAIT_SECONDS
        if self._profile_wait_grace_used:
            return base_seconds + PROFILE_WAIT_GRACE_SECONDS
        return base_seconds

    def _maybe_extend_profile_wait(self) -> bool:
        if self._profile_wait_grace_used:
            return False
        if self._failure_attempts:
            return False
        if not self._current_page_matches_current_item():
            return False
        if self._is_captcha_present():
            return False
        self._profile_wait_grace_used = True
        self._last_message = self._with_subject("页面已打开，等待资料接口返回。")
        self._log_event(
            "profile_wait_extended",
            task_index=self._current_task_index,
            grace_seconds=PROFILE_WAIT_GRACE_SECONDS,
            step=self._current_step,
            page_url=self._safe_page_url(),
        )
        return True

    def _pause(self, reason: str, message: str) -> None:
        if not self._paused:
            self._paused_started_at = datetime.now(UTC)
        self._paused = True
        self._pause_reason = reason
        self._last_message = message
        self._pause_step = self._current_step or "idle"
        self._attempt_in_progress = False
        self._current_step = "idle"
        self._waiting_started_at = None
        self._write_dev_pause_diagnostic(reason=reason, message=message)

    def _resume_eta_clock(self) -> None:
        if self._paused_started_at is None:
            return
        if self._run_started_at is not None:
            self._run_started_at += datetime.now(UTC) - self._paused_started_at
        self._paused_started_at = None

    def _pause_manual_for_current(self, message: str) -> None:
        self._pause(PAUSE_REASON_MANUAL_ACTION, message)
        self._mark_current_paused(STATE_STATUS_PAUSED_MANUAL_ACTION)

    def _handle_retryable_manual_failure(self, message: str) -> None:
        attempt_index = len(self._failure_attempts) + 1
        diagnostic_text = self._build_diagnostic_text(
            message=message,
            pause_reason=PAUSE_REASON_MANUAL_ACTION,
            pause_step=self._current_step or "idle",
        )
        self._failure_attempts.append(
            CreatorEnrichmentFailureAttempt(
                index=attempt_index,
                summary=message,
                diagnostic_text=diagnostic_text,
            )
        )
        self._write_dev_diagnostic_artifact(
            name=(
                f"creator_enrichment_task_{self._task_id}_item_{self._current_task_index or 'na'}"
                f"_attempt_{attempt_index}"
            ),
            diagnostic_text=diagnostic_text,
            summary=message,
            kind="failure_attempt",
            attempt_index=attempt_index,
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
        if self._auto_skip_on_failure:
            self._mark_current_auto_skipped(message)
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

    def _mark_current_auto_skipped(self, message: str) -> None:
        if self._current_task_index is None:
            return
        self._begin_task_finalization(self._current_task_index)
        subject = self._current_subject()
        update_item_state(
            self._state,
            task_index=self._current_task_index,
            status=STATE_STATUS_AUTO_SKIPPED,
            reason=message,
            region=self._current_region or "",
        )
        self._persist_state()
        self._paused = False
        self._pause_reason = None
        self._pause_step = "idle"
        self._current_step = "idle"
        self._waiting_started_at = None
        self._advance_to_next_pending(open_page=True)
        self._last_message = self._message_for_subject(
            subject,
            "补充采集异常，已自动跳过并继续下一条。",
        )
        self._continue_after_terminal_transition()

    def _complete(self, message: str) -> None:
        self._completed = True
        self._paused = True
        self._pause_reason = None
        self._attempt_in_progress = False
        self._current_step = "idle"
        self._waiting_started_at = None
        self._last_message = message

    def _persist_state(self) -> None:
        self._app_service.save_app_setting(self._state_key, self._state)

    def _reset_attempt_state(self, *, clear_page_capture: bool = True) -> None:
        self._attempt_in_progress = False
        self._profile_wait_grace_used = False
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
        self._contact_badge_click_inflight = False
        self._contact_positive_signal = False
        self._profile_patch_applied = False
        self._last_dom_profile_signature = ""
        self._last_dom_profile_seen_at = None
        self._last_dom_profile_probe_at = None
        if clear_page_capture and self._page is not None:
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

    def _log_event(self, event: str, **fields: object) -> None:
        if self._dev_logger is None:
            return
        self._dev_logger.log(
            event,
            task_id=self._task_id,
            current_task_index=self._current_task_index,
            current_region=self._current_region,
            current_step=self._current_step,
            paused=self._paused,
            completed=self._completed,
            message=self._last_message,
            **fields,
        )

    def _mark_no_contact_and_advance(self) -> None:
        if self._current_task_index is None:
            return
        self._begin_task_finalization(self._current_task_index)
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
        self._continue_after_terminal_transition()

    def _continue_after_terminal_transition(self) -> None:
        if self._paused or self._completed or self._current_task_index is None:
            return
        self._sync_current_page()
        self._start_current_attempt(reload_page=False, clear_failure_history=True)

    def _status_counts(self) -> dict[str, int]:
        counts = {
            "completed": 0,
            "success": 0,
            "no_contact": 0,
            "auto_skipped": 0,
            "skipped": 0,
            "paused": 0,
        }
        for item in self._eligible_items():
            status = str(self._item_state(item.task_index).get("status") or "")
            if is_terminal_status(status):
                counts["completed"] += 1
            if status == STATE_STATUS_SUCCESS:
                counts["success"] += 1
            elif status == STATE_STATUS_NO_CONTACT:
                counts["no_contact"] += 1
            elif status == STATE_STATUS_AUTO_SKIPPED:
                counts["auto_skipped"] += 1
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
        if self._ensure_single_work_page(reason="runtime_check"):
            return True
        if self._context is not None:
            self._pause_manual_for_current("工作页面不可用，请处理浏览器后重试。")
        return False

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
            self._pause_reason in ATTENTION_PAUSE_REASONS
            and self._last_message != "补充采集已暂停。"
        )

    def _write_dev_pause_diagnostic(self, *, reason: str, message: str) -> None:
        if reason not in ATTENTION_PAUSE_REASONS or message == "补充采集已暂停。":
            return
        diagnostic_text = self._build_diagnostic_text(
            message=message,
            pause_reason=reason,
            pause_step=self._pause_step or "idle",
        )
        self._write_dev_diagnostic_artifact(
            name=(
                f"creator_enrichment_task_{self._task_id}_item_{self._current_task_index or 'na'}"
                f"_pause_{reason}"
            ),
            diagnostic_text=diagnostic_text,
            summary=message,
            kind="pause_snapshot",
            attempt_index=len(self._failure_attempts) or None,
        )

    def _write_dev_diagnostic_artifact(
        self,
        *,
        name: str,
        diagnostic_text: str,
        summary: str,
        kind: str,
        attempt_index: int | None,
    ) -> None:
        if self._dev_logger is None:
            return
        header = [
            f"kind: {kind}",
            f"task_id: {self._task_id}",
            f"task_index: {self._current_task_index}",
            f"attempt_index: {attempt_index if attempt_index is not None else '-'}",
            f"summary: {summary}",
            "",
        ]
        path = self._dev_logger.write_text_artifact(
            name=name,
            content="\n".join(header) + diagnostic_text,
        )
        if path is None:
            self._log_event(
                "diagnostic_artifact_write_failed",
                task_index=self._current_task_index,
                artifact_kind=kind,
                artifact_dir=str(self._dev_logger.artifact_dir),
                attempt_index=attempt_index,
                summary=summary,
            )
            return
        self._log_event(
            "diagnostic_artifact_written",
            task_index=self._current_task_index,
            artifact_kind=kind,
            artifact_path=str(path),
            attempt_index=attempt_index,
            summary=summary,
        )


def _should_block_resource_url(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.casefold()
    path = parts.path.casefold()
    if host in BLOCKED_RESOURCE_HOSTS:
        return True
    return any(marker in path for marker in BLOCKED_RESOURCE_PATH_MARKERS)


def _should_block_profile_request(url: str, post_data: str) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    if not path.endswith(PROFILE_API_PATH):
        return False
    creator_id, profile_types = _parse_profile_request_body(post_data)
    if not creator_id or not profile_types:
        return False
    return any(profile_type not in PROFILE_TYPES_ALLOWLIST for profile_type in profile_types)


def _parse_profile_request_body(post_data: str) -> tuple[str, tuple[int, ...]]:
    try:
        payload = json.loads(post_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", ()
    creator_id = str(payload.get("creator_oec_id") or "").strip()
    raw_profile_types = payload.get("profile_types")
    profile_types: list[int] = []
    if isinstance(raw_profile_types, list):
        for item in raw_profile_types:
            try:
                profile_types.append(int(item))
            except (TypeError, ValueError):
                continue
    return creator_id, tuple(profile_types)
