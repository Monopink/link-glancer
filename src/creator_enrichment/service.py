from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Playwright, Response, sync_playwright

from creator_enrichment.constants import (
    BLOCKED_RESOURCE_HOSTS,
    BLOCKED_RESOURCE_PATH_MARKERS,
    BLOCKED_RESOURCE_TYPES,
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
    PROFILE_TYPES_ALLOWLIST,
    PROFILE_WAIT_SECONDS,
    STATE_STATUS_AUTO_SKIPPED,
    STATE_STATUS_NO_CONTACT,
    STATE_STATUS_PAUSED_CAPTCHA,
    STATE_STATUS_PAUSED_MANUAL_ACTION,
    STATE_STATUS_PAUSED_REGION_MISMATCH,
    STATE_STATUS_SKIPPED,
    STATE_STATUS_SUCCESS,
)
from creator_enrichment.diagnostics import build_diagnostic_text
from creator_enrichment.models import CreatorEnrichmentFailureAttempt, CreatorEnrichmentStatus
from creator_enrichment.page_script import (
    enrichment_collection_mode_script,
    network_capture_init_script,
)
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
from link_glancer.runtime.dev import JsonlDevLogger, is_dev_mode
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import BrowserConfig, TaskItem

AUTO_START_PROFILE_WAIT_SECONDS = 8
PROFILE_WAIT_GRACE_SECONDS = 4


@dataclass(slots=True)
class _CapturedProfile:
    creator_id: str
    payload: dict[str, object]
    shop_region: str
    profile_types: tuple[int, ...]
    page_url: str


@dataclass(slots=True)
class _CapturedContact:
    creator_id: str
    payload: dict[str, object]
    page_url: str


@dataclass(slots=True)
class _BufferPage:
    page: Page
    task_index: int | None
    profile_cache: _CapturedProfile | None = None
    contact_cache: _CapturedContact | None = None
    collection_mode_installed: bool = False


class CreatorEnrichmentSession:
    def __init__(
        self,
        *,
        app_service: TaskApplicationService,
        task_id: int,
        browser_config: BrowserConfig,
        open_tab_count: int,
        confirm_url: str | None,
        state_key: str,
    ) -> None:
        self._app_service = app_service
        self._task_id = task_id
        self._browser_config = browser_config
        self._open_tab_count = max(1, open_tab_count)
        self._confirm_url = (confirm_url or "").strip() or None
        self._state_key = state_key
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._page = None
        self._current_page_entry: _BufferPage | None = None
        self._page_entries: dict[int, _BufferPage] = {}
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

    def start(self) -> CreatorEnrichmentStatus:
        self.shutdown()
        if is_dev_mode():
            self._dev_logger = JsonlDevLogger(
                module="creator_enrichment",
                file_stem=f"creator_enrichment_task_{self._task_id}",
            )
            self._log_event("session_start")
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
            self._context.add_init_script(network_capture_init_script())
            self._page = self._context.new_page()
            self._current_page_entry = self._register_page(self._page, None)
            if self._state.get("started_at") is None:
                self._state["started_at"] = now_iso()
                self._persist_state()
            self._started_at = parse_datetime(self._state.get("started_at"))
            self._run_started_at = datetime.now(UTC)
            self._run_completed_baseline = self._status_counts()["completed"]
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
                    source="service_retry",
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
        self._sync_current_page()
        self._start_current_attempt(reload_page=True, clear_failure_history=True)
        self._last_message = self._with_subject("正在重试。")
        self._log_event("retry", task_index=self._current_task_index, reason="manual")
        return self.status()

    def stop(self) -> CreatorEnrichmentStatus:
        self._paused = True
        self._pause_reason = PAUSE_REASON_MANUAL_ACTION
        self._last_message = "补充采集已暂停。"
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
        self._current_page_entry = None
        self._page_entries = {}
        self._captured_profile = None
        self._captured_contact = None
        self._attempt_page_url = ""
        self._attempt_creator_id = ""
        self._attempt_in_progress = False
        self._finalizing_task_indexes = set()
        self._run_started_at = None
        self._run_completed_baseline = 0
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
        completed_delta = max(counts["completed"] - self._run_completed_baseline, 0)
        if started_at is not None and completed_delta > 0 and total_count >= counts["completed"]:
            elapsed_seconds = max(int((datetime.now(UTC) - started_at).total_seconds()), 1)
            remaining_count = max(total_count - counts["completed"], 0)
            projected_remaining = int(elapsed_seconds * remaining_count / completed_delta)
            estimated_end_at = datetime.now(UTC) + timedelta(seconds=projected_remaining)
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
            self._sync_visible_buffer()

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

    def _sync_visible_buffer(self) -> None:
        self._sync_current_page()

    def _sync_current_page(self) -> None:
        if self._context is None:
            return
        pending = self._pending_items()
        if not pending:
            return
        current_item = pending[0]
        self._ensure_current_page(current_item)

    def _sync_prefetch_pages(self) -> None:
        return

    def _prepare_page_for_item(self, page: Page, item: TaskItem) -> bool:
        return False

    def _ensure_current_page(self, item: TaskItem) -> None:
        if self._page is None:
            self._page = self._create_page()
            if self._page is None:
                return
            self._current_page_entry = self._register_page(self._page, None)
        target_task_index = item.task_index
        if self._assigned_task_index(self._page) == target_task_index:
            self._current_task_index = target_task_index
            self._current_region = normalized_region(item.task_data.get("selection_region"))
            self._ensure_collection_mode_for_page(self._page)
            self._bring_page_to_front(self._page)
            return
        target_url = self._detail_url_for_item(item)
        self._current_task_index = target_task_index
        self._current_region = normalized_region(item.task_data.get("selection_region"))
        self._navigate_page(self._page, target_url)
        self._assign_page_task_index(self._page, target_task_index)
        self._ensure_collection_mode_for_page(self._page)
        self._log_event(
            "current_committed",
            task_index=item.task_index,
            creator_oecuid=normalized_creator_id(item.task_data.get("creator_oecuid")),
            page_url=target_url,
        )
        self._bring_page_to_front(self._page)

    def _close_prepared_page(self, task_index: int) -> None:
        return

    def _retire_current_page(self) -> None:
        self._captured_profile = None
        self._captured_contact = None
        self._attempt_creator_id = ""
        self._attempt_page_url = ""
        self._attempt_in_progress = False

    def _close_confirmation_page_if_needed(self) -> None:
        return

    def _open_prefetch_page(self, item: TaskItem) -> None:
        return

    def _current_page_matches_current_item(self) -> bool:
        if self._page is None or self._current_task_index is None:
            return False
        return self._assigned_task_index(self._page) == self._current_task_index

    def _safe_page_url(self) -> str:
        return self._page_url(self._page)

    def _page_url(self, page: Page | None) -> str:
        if page is None:
            return ""
        try:
            return page.url
        except PlaywrightError:
            return ""

    def _response_page(self, response: Response) -> Page | None:
        try:
            return response.frame.page
        except PlaywrightError:
            return None

    def _register_page(self, page: Page, task_index: int | None) -> _BufferPage:
        page_key = id(page)
        entry = self._page_entries.get(page_key)
        if entry is None:
            entry = _BufferPage(page=page, task_index=task_index)
            self._page_entries[page_key] = entry
        return entry

    def _entry_for_page(self, page: Page | None) -> _BufferPage | None:
        if page is None:
            return None
        return self._page_entries.get(id(page))

    def _entry_for_task(self, task_index: int | None) -> _BufferPage | None:
        if task_index is None:
            return None
        if (
            self._current_page_entry is not None
            and self._current_page_entry.task_index == task_index
        ):
            return self._current_page_entry
        return None

    def _active_page_entries(self) -> list[_BufferPage]:
        if self._current_page_entry is None:
            return []
        return [self._current_page_entry]

    def _unregister_page(self, page: Page) -> None:
        page_key = id(page)
        entry = self._page_entries.pop(page_key, None)
        if entry is None:
            return
        if self._current_page_entry is entry:
            self._current_page_entry = None

    def _mark_collection_mode_stale(self, page: Page | None) -> None:
        entry = self._entry_for_page(page)
        if entry is not None:
            entry.collection_mode_installed = False

    def _assign_page_task_index(self, page: Page | None, task_index: int | None) -> None:
        if page is None:
            return
        entry = self._register_page(page, None)
        entry.task_index = task_index

    def _assigned_task_index(self, page: Page | None) -> int | None:
        if page is None:
            return None
        entry = self._entry_for_page(page)
        return None if entry is None else entry.task_index

    def _detail_url_for_task(self, task_index: int | None) -> str | None:
        if task_index is None:
            return None
        item = self._items_by_index.get(task_index)
        if item is None:
            return None
        return self._detail_url_for_item(item)

    def _detail_url_for_item(self, item: TaskItem) -> str | None:
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        if not creator_id:
            return None
        shop_region = normalized_region(item.task_data.get("selection_region"))
        return DETAIL_URL_TEMPLATE.format(
            creator_oecuid=creator_id,
            shop_region=shop_region or "",
        )

    def _create_page(self) -> Page | None:
        if self._context is None:
            return None
        try:
            page = self._context.new_page()
            self._register_page(page, None)
            return page
        except PlaywrightError as exc:
            self._last_message = f"打开标签失败：{exc}"
            self._log_event("page_create_failed", details=str(exc))
            return None

    def _navigate_page(self, page: Page | None, target_url: str) -> None:
        if page is None:
            return
        self._mark_collection_mode_stale(page)
        current_url = self._page_url(page)
        try:
            if not current_url or current_url == "about:blank":
                page.goto(target_url, wait_until="commit")
                return
            page.evaluate("(url) => { window.location.replace(url); }", target_url)
        except PlaywrightError:
            page.goto(target_url, wait_until="commit")

    def _bring_page_to_front(self, page: Page | None) -> None:
        if page is None:
            return
        try:
            page.bring_to_front()
        except PlaywrightError as exc:
            self._last_message = f"标签切换失败：{exc}"
            self._log_event("page_focus_failed", details=str(exc))

    def _close_page(self, page: Page | None) -> None:
        if page is None:
            return
        self._unregister_page(page)
        try:
            page.close()
        except PlaywrightError:
            pass

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
            if reload_page:
                self._clear_cached_capture_for_task(self._current_task_index)
                self._mark_collection_mode_stale(self._page)
                self._page.reload(wait_until="commit")
                self._log_event(
                    "open_committed",
                    task_index=self._current_task_index,
                    navigation_mode="reload",
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

    def _handle_response(self, response: Response) -> None:
        managed_page = self._response_page(response)
        if managed_page is None:
            return
        task_index = self._assigned_task_index(managed_page)
        if task_index is None:
            return
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
            profile = payload.get("creator_profile")
            if not isinstance(profile, dict):
                profile = {}
            response_creator_id = normalized_creator_id(
                nested_value(profile.get("creator_oecuid"))
                or query_param(self._page_url(managed_page), "cid")
            )
            should_capture = should_capture_profile(
                profile=profile,
                request_creator_id=creator_id,
                response_creator_id=response_creator_id,
                profile_types=profile_types,
            )
            if should_capture:
                self._log_event(
                    "profile_seen",
                    task_index=task_index,
                    source="response",
                )
                self._store_profile_capture(
                    task_index=task_index,
                    creator_id=response_creator_id or creator_id,
                    payload=payload,
                    shop_region=shop_region,
                    profile_types=profile_types,
                    page_url=self._page_url(managed_page),
                )
            return
        if path.endswith(CONTACT_API_PATH):
            creator_id = query_param(response.url, "creator_oecuid")
            if creator_id:
                self._log_event(
                    "contact_seen",
                    task_index=task_index,
                    source="response",
                )
                self._store_contact_capture(
                    task_index=task_index,
                    creator_id=creator_id,
                    payload=payload,
                    page_url=self._page_url(managed_page),
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
        entry = self._entry_for_page(page)
        if entry is not None and entry.collection_mode_installed:
            return
        try:
            page.evaluate(enrichment_collection_mode_script())
        except PlaywrightError:
            return
        if entry is not None:
            entry.collection_mode_installed = True

    def _pull_page_captures(self) -> None:
        for entry in self._active_page_entries():
            try:
                captures = entry.page.evaluate(
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
                continue
            if not isinstance(captures, dict):
                continue
            profile_responses = captures.get("profileResponses")
            if isinstance(profile_responses, list):
                for payload in profile_responses:
                    self._handle_page_profile_capture(entry.task_index, payload)
            contact_responses = captures.get("contactResponses")
            if isinstance(contact_responses, list):
                for payload in contact_responses:
                    self._handle_page_contact_capture(entry.task_index, payload)
            badge_events = captures.get("badgeEvents")
            if isinstance(badge_events, list):
                for payload in badge_events:
                    self._handle_page_badge_capture(entry.task_index, payload)
        self._process_captured_events(trigger="poll")

    def _handle_page_profile_capture(self, task_index: int | None, entry: object) -> None:
        if task_index is None or not isinstance(entry, dict):
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
            self._log_event(
                "profile_seen",
                task_index=task_index,
                source="page_capture",
            )
            self._store_profile_capture(
                task_index=task_index,
                creator_id=creator_id,
                payload=payload,
                shop_region=shop_region,
                profile_types=profile_types,
                page_url=str(entry.get("pageUrl") or ""),
            )

    def _handle_page_contact_capture(self, task_index: int | None, entry: object) -> None:
        if task_index is None or not isinstance(entry, dict):
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
        self._log_event(
            "contact_seen",
            task_index=task_index,
            source="page_capture",
        )
        self._store_contact_capture(
            task_index=task_index,
            creator_id=creator_id,
            payload=payload,
            page_url=str(entry.get("pageUrl") or ""),
        )

    def _handle_page_badge_capture(self, task_index: int | None, entry: object) -> None:
        if (
            task_index is None
            or task_index != self._current_task_index
            or not isinstance(entry, dict)
        ):
            return
        detected = bool(entry.get("detected"))
        clicked = bool(entry.get("clicked"))
        strategy = str(entry.get("strategy") or "")
        if detected:
            self._last_contact_badge_detected = True
            self._contact_positive_signal = True
            self._log_event("badge_detected", task_index=self._current_task_index)
        if strategy:
            self._last_contact_badge_strategy = strategy
        if clicked and not self._last_contact_badge_clicked:
            self._last_contact_badge_clicked = True
            self._last_contact_badge_clicked_at = datetime.now(UTC)
            self._log_event(
                "badge_clicked",
                task_index=self._current_task_index,
                source="page_capture",
            )
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
            self._process_captured_events(trigger="page_capture:badge")

    def _process_captured_events(self, *, trigger: str) -> None:
        if self._paused or self._completed or self._current_task_index is None:
            return
        if not self._attempt_in_progress or self._is_task_finalizing(self._current_task_index):
            return
        self._discard_stale_captures()
        self._log_event(
            "process_captured_events",
            trigger=trigger,
            has_profile=self._captured_profile is not None,
            has_contact=self._captured_contact is not None,
            step=self._current_step,
        )
        if self._current_step == "waiting_profile" and self._captured_profile is not None:
            self._apply_profile()
            return
        if self._current_step == "waiting_contact_badge":
            if self._captured_contact is not None:
                self._current_step = "waiting_contact"
                self._contact_positive_signal = True
            elif self._last_contact_badge_clicked:
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                self._last_message = self._with_subject("有联系方式，正在采集。")
        if self._current_step == "waiting_contact":
            if self._captured_profile is not None and not self._profile_patch_applied:
                self._apply_profile()
                if self._paused or self._completed:
                    return
            if self._captured_contact is not None and self._profile_patch_applied:
                self._apply_contact()

    def _store_profile_capture(
        self,
        *,
        task_index: int,
        creator_id: str,
        payload: dict[str, object],
        shop_region: str,
        profile_types: tuple[int, ...],
        page_url: str,
    ) -> None:
        if self._is_task_finalizing(task_index):
            return
        captured = _CapturedProfile(
            creator_id=creator_id,
            payload=payload,
            shop_region=shop_region,
            profile_types=profile_types,
            page_url=page_url,
        )
        if task_index != self._current_task_index:
            return
        self._captured_profile = captured
        self._last_profile_response_url = page_url
        self._last_profile_payload = payload
        self._last_profile_types = profile_types
        self._last_profile_seen_at = datetime.now(UTC)
        self._process_captured_events(trigger="capture:profile")

    def _store_contact_capture(
        self,
        *,
        task_index: int,
        creator_id: str,
        payload: dict[str, object],
        page_url: str,
    ) -> None:
        if self._is_task_finalizing(task_index):
            return
        captured = _CapturedContact(
            creator_id=creator_id,
            payload=payload,
            page_url=page_url,
        )
        if task_index != self._current_task_index:
            return
        self._contact_positive_signal = True
        self._captured_contact = captured
        self._last_contact_response_url = page_url
        self._last_contact_payload = payload
        self._process_captured_events(trigger="capture:contact")

    def _load_cached_captures_for_current_task(self) -> None:
        return

    def _clear_cached_capture_for_task(self, task_index: int) -> None:
        return

    def _is_task_finalizing(self, task_index: int | None) -> bool:
        return task_index is not None and task_index in self._finalizing_task_indexes

    def _begin_task_finalization(self, task_index: int | None) -> None:
        if task_index is None:
            return
        self._finalizing_task_indexes.add(task_index)

    def _finish_task_finalization(self, task_index: int | None) -> None:
        if task_index is None:
            return
        self._finalizing_task_indexes.discard(task_index)

    def _discard_stale_captures(self) -> None:
        if self._captured_profile is not None and not self._is_profile_capture_current(
            self._captured_profile
        ):
            self._log_event(
                "stale_profile_discarded",
                captured_creator_id=self._captured_profile.creator_id,
                captured_page_url=self._captured_profile.page_url,
                attempt_creator_id=self._attempt_creator_id,
                attempt_page_url=self._attempt_page_url,
            )
            self._captured_profile = None
        if self._captured_contact is not None and not self._is_contact_capture_current(
            self._captured_contact
        ):
            self._log_event(
                "stale_contact_discarded",
                captured_creator_id=self._captured_contact.creator_id,
                captured_page_url=self._captured_contact.page_url,
                attempt_creator_id=self._attempt_creator_id,
                attempt_page_url=self._attempt_page_url,
            )
            self._captured_contact = None

    def _is_profile_capture_current(self, captured: _CapturedProfile) -> bool:
        if not self._attempt_creator_id or captured.creator_id != self._attempt_creator_id:
            return False
        return self._captured_page_matches_attempt(captured.page_url)

    def _is_contact_capture_current(self, captured: _CapturedContact) -> bool:
        if not self._attempt_creator_id or captured.creator_id != self._attempt_creator_id:
            return False
        return self._captured_page_matches_attempt(captured.page_url)

    def _captured_page_matches_attempt(self, captured_page_url: str) -> bool:
        captured_page_url = captured_page_url.strip()
        if not captured_page_url or not self._attempt_page_url:
            return True
        return query_param(captured_page_url, "cid") == query_param(self._attempt_page_url, "cid")

    def _apply_profile(self) -> None:
        if self._captured_profile is None or self._current_task_index is None:
            return
        if self._is_task_finalizing(self._current_task_index):
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_profile
        self._captured_profile = None
        if captured.creator_id != creator_id:
            self._log_event(
                "profile_ignored",
                task_index=self._current_task_index,
                expected_creator_id=creator_id,
                captured_creator_id=captured.creator_id,
            )
            return
        payload = captured.payload
        response_code = int(payload.get("code") or 0)
        item_region = normalized_region(item.task_data.get("selection_region"))
        request_region = normalized_region(
            captured.shop_region or query_param(self._safe_page_url(), "shop_region")
        )
        self._log_event(
            "profile_apply",
            task_index=self._current_task_index,
            response_code=response_code,
            item_region=item_region,
            request_region=request_region,
            captured_creator_id=captured.creator_id,
        )
        if response_code != 0:
            if self._is_captcha_present():
                self._pause(PAUSE_REASON_CAPTCHA, "检测到人机验证，请先完成验证后继续。")
                self._mark_current_paused(STATE_STATUS_PAUSED_CAPTCHA)
                return
            if item_region and request_region and item_region != request_region:
                self._log_event(
                    "profile_region_mismatch",
                    task_index=self._current_task_index,
                    response_code=response_code,
                    item_region=item_region,
                    request_region=request_region,
                )
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

        if item_region and request_region and item_region != request_region:
            self._log_event(
                "profile_region_mismatch",
                task_index=self._current_task_index,
                response_code=response_code,
                item_region=item_region,
                request_region=request_region,
            )
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
        self._log_event(
            "profile_contact_state",
            task_index=self._current_task_index,
            contact_available=contact_available,
            contact_available_raw=self._last_contact_available_raw,
            has_contact=has_contact,
            contact_positive_signal=self._contact_positive_signal,
            captured_contact=self._captured_contact is not None,
            badge_clicked=self._last_contact_badge_clicked,
            patch_keys=sorted(patch.keys()),
        )

        if contact_available is False and not has_contact:
            self._log_event("no_contact", task_index=self._current_task_index)
            self._mark_no_contact_and_advance()
            return
        if not has_contact and contact_available is not True:
            self._log_event(
                "profile_contact_state_invalid",
                task_index=self._current_task_index,
                contact_available=contact_available,
                contact_available_raw=self._last_contact_available_raw,
            )
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
            self._log_event("badge_clicked", task_index=self._current_task_index, source="service")
        return

    def _apply_contact(self) -> None:
        if self._captured_contact is None or self._current_task_index is None:
            return
        if self._is_task_finalizing(self._current_task_index):
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
        response_code = int(payload.get("code") or 0)
        if response_code != 0:
            self._log_event(
                "contact_apply_error",
                task_index=self._current_task_index,
                response_code=response_code,
            )
            self._handle_retryable_manual_failure(
                "联系方式接口返回异常，请处理后继续，或跳过当前达人。"
            )
            return
        parsed = contact_patch(payload, self._current_region or "")
        self._log_event(
            "contact_apply",
            task_index=self._current_task_index,
            total_entries=parsed.total_entries,
            valued_entries=parsed.valued_entries,
            recognized_entries=parsed.recognized_entries,
            patch_keys=sorted(parsed.patch.keys()),
        )
        if parsed.patch:
            finalized_task_index = self._current_task_index
            self._begin_task_finalization(finalized_task_index)
            subject = self._current_subject()
            region = self._current_region or ""
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=parsed.patch,
            )
            self._refresh_items()
            new_status = STATE_STATUS_SUCCESS
            update_item_state(
                self._state,
                task_index=finalized_task_index,
                status=new_status,
                region=region,
            )
            self._persist_state()
            self._advance_to_next_pending(open_page=True)
            self._last_message = self._message_for_subject(subject, "已保存补充资料。")
            self._finish_task_finalization(finalized_task_index)
            self._continue_after_terminal_transition()
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
        self._log_event(
            "badge_click_attempt",
            task_index=self._current_task_index,
            detected=self._last_contact_badge_detected,
            clicked=False,
            strategy=self._last_contact_badge_strategy,
            candidate_count=int(result.get("candidate_count") or 0),
            candidate_summary=str(result.get("candidate_summary") or ""),
        )
        roots = [
            "#creator-detail-profile-container",
            "#content-container > main",
            "#content-container",
            "main",
            "body",
        ]
        try:
            root_locator = None
            root_name = ""
            for root_selector in roots:
                candidate = self._page.locator(root_selector).first
                if candidate.count() <= 0:
                    continue
                root_locator = candidate
                root_name = root_selector
                break
            if root_locator is None:
                self._last_contact_badge_strategy = "playwright:root_missing"
                return False
        except PlaywrightError:
            self._last_contact_badge_strategy = "playwright:root_error"
            return False
        selectors = [
            f"div.cursor-pointer svg[class*='alliance-icon-{keyword}']"
            for keyword in CONTACT_ICON_CLASS_KEYWORDS
        ] + [
            f"button.core-btn-icon-only svg[class*='alliance-icon-{keyword}']"
            for keyword in CONTACT_ICON_CLASS_KEYWORDS
        ]
        for selector in selectors:
            try:
                locator = root_locator.locator(selector).first
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
                self._last_contact_badge_strategy = f"playwright:{root_name}:{selector}"
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
                    const roots = [
                        document.querySelector("#creator-detail-profile-container"),
                        document.querySelector("#content-container > main"),
                        document.querySelector("#content-container"),
                        document.querySelector("main"),
                        document.body,
                    ].filter((node) => node instanceof HTMLElement);
                    if (roots.length <= 0) {
                        return {
                            detected: false,
                            clicked: false,
                            strategy: "dom:root_missing",
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
                    const resolveClickable = (element, root) => {
                        let current = element;
                        while (current instanceof HTMLElement && current !== root) {
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
                    const normalizeText = (value) =>
                        typeof value === "string" ? value.trim().toLowerCase() : "";
                    const keywordVariants = keywords.map((keyword) => normalizeText(keyword));
                    const describeTarget = (target) => {
                        if (!(target instanceof HTMLElement)) {
                            return "";
                        }
                        const className =
                            typeof target.className === "string" ? target.className : "";
                        const svgClassName =
                            target.querySelector("svg")?.getAttribute("class") || "";
                        return [
                            target.tagName.toLowerCase(),
                            target.getAttribute("data-e2e") || "",
                            target.getAttribute("data-tid") || "",
                            target.getAttribute("aria-label") || "",
                            target.getAttribute("title") || "",
                            className,
                            svgClassName,
                        ]
                            .map((part) => normalizeText(part))
                            .filter(Boolean)
                            .join("|");
                    };
                    const triggerClick = (target) => {
                        target.dispatchEvent(
                            new MouseEvent("mousedown", { bubbles: true, cancelable: true }),
                        );
                        target.dispatchEvent(
                            new MouseEvent("mouseup", { bubbles: true, cancelable: true }),
                        );
                        target.dispatchEvent(
                            new MouseEvent("click", { bubbles: true, cancelable: true }),
                        );
                        target.click();
                    };
                    const candidates = [];
                    const seen = new Set();
                    const pushCandidate = (node, root, rootName) => {
                        const target = resolveClickable(
                            node instanceof HTMLElement ? node : node?.parentElement,
                            root,
                        );
                        if (!(target instanceof HTMLElement)) {
                            return;
                        }
                        if (seen.has(target)) {
                            return;
                        }
                        seen.add(target);
                        const description = describeTarget(target);
                        const matchedKeywordIndex = keywordVariants.findIndex(
                            (keyword) => keyword && description.includes(keyword),
                        );
                        if (matchedKeywordIndex < 0) {
                            return;
                        }
                        candidates.push({
                            target,
                            matchedKeyword: keywords[matchedKeywordIndex],
                            description,
                            rootName,
                        });
                    };
                    for (const root of roots) {
                        const rootName = root.id ? `#${root.id}` : root.tagName.toLowerCase();
                        for (const node of root.querySelectorAll(
                            "svg, button, a, [role='button'], div.cursor-pointer",
                        )) {
                            pushCandidate(node, root, rootName);
                        }
                    }
                    const candidateSummary = candidates
                        .slice(0, 6)
                        .map(
                            (candidate) =>
                                `${candidate.rootName}:${candidate.matchedKeyword}:${candidate.description}`,
                        )
                        .join(" || ");
                    for (const candidate of candidates) {
                        const target = candidate.target;
                        if (!(target instanceof HTMLElement) || !isVisible(target)) {
                            return {
                                detected: true,
                                clicked: false,
                                strategy:
                                    `dom:${candidate.rootName}:${candidate.matchedKeyword}`
                                    + ":not_visible",
                                candidate_count: candidates.length,
                                candidate_summary: candidateSummary,
                            };
                        }
                        if (!click) {
                            return {
                                detected: true,
                                clicked: false,
                                strategy:
                                    `dom:${candidate.rootName}:${candidate.matchedKeyword}`
                                    + ":visible",
                                candidate_count: candidates.length,
                                candidate_summary: candidateSummary,
                            };
                        }
                        triggerClick(target);
                        return {
                            detected: true,
                            clicked: true,
                            strategy: `dom:${candidate.rootName}:${candidate.matchedKeyword}`,
                            candidate_count: candidates.length,
                            candidate_summary: candidateSummary,
                        };
                    }
                    return {
                        detected: false,
                        clicked: false,
                        strategy: "dom:not_found",
                        candidate_count: 0,
                        candidate_summary: "",
                    };
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
            "candidate_count": int(result.get("candidate_count") or 0),
            "candidate_summary": str(result.get("candidate_summary") or ""),
        }

    def _maybe_capture_profile_from_dom(self) -> bool:
        if self._page is None or self._current_task_index is None:
            return False
        if not self._current_page_matches_current_item():
            return False
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            return False
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        if not creator_id:
            return False
        dom_profile = self._extract_profile_dom_snapshot()
        if not dom_profile.get("ready"):
            return False
        bio = str(dom_profile.get("bio") or "").strip()
        if not bio:
            return False
        has_badge = bool(dom_profile.get("has_badge"))
        if not has_badge:
            return False
        badge_strategy = str(dom_profile.get("badge_strategy") or "")
        if badge_strategy:
            self._last_contact_badge_strategy = badge_strategy
        self._last_contact_badge_detected = True
        self._contact_positive_signal = True
        self._log_event(
            "profile_dom_ready",
            task_index=self._current_task_index,
            bio_length=len(bio),
            badge_strategy=badge_strategy,
        )
        self._store_profile_capture(
            task_index=self._current_task_index,
            creator_id=creator_id,
            payload={
                "code": 0,
                "creator_profile": {
                    "creator_oecuid": creator_id,
                    "bio": bio,
                    "contact_info_available": {
                        "value": True,
                        "is_authorized": True,
                        "status": 0,
                    },
                },
            },
            shop_region=self._current_region or "",
            profile_types=(1,),
            page_url=self._safe_page_url(),
        )
        return True

    def _extract_profile_dom_snapshot(self) -> dict[str, object]:
        if self._page is None:
            return {"ready": False, "bio": "", "has_badge": False, "badge_strategy": ""}
        try:
            result = self._page.evaluate(
                """
                ({ keywords }) => {
                    const roots = [
                        document.querySelector("#creator-detail-profile-container"),
                        document.querySelector("#content-container > main"),
                        document.querySelector("#content-container"),
                        document.querySelector("main"),
                        document.body,
                    ].filter((node) => node instanceof HTMLElement);
                    if (roots.length <= 0) {
                        return {
                            ready: false,
                            bio: "",
                            hasBadge: false,
                            badgeStrategy: "profile_dom:no_root",
                        };
                    }
                    const normalizeText = (value) =>
                        typeof value === "string" ? value.trim().toLowerCase() : "";
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
                    const keywordVariants = keywords.map((keyword) => normalizeText(keyword));
                    const describeTarget = (target) => {
                        if (!(target instanceof HTMLElement)) {
                            return "";
                        }
                        const className =
                            typeof target.className === "string" ? target.className : "";
                        const svgClassName =
                            target.querySelector("svg")?.getAttribute("class") || "";
                        return [
                            target.tagName.toLowerCase(),
                            target.getAttribute("data-e2e") || "",
                            target.getAttribute("data-tid") || "",
                            target.getAttribute("aria-label") || "",
                            target.getAttribute("title") || "",
                            className,
                            svgClassName,
                        ]
                            .map((part) => normalizeText(part))
                            .filter(Boolean)
                            .join("|");
                    };
                    const bioSelectors = [
                        "#creator-detail-profile-container [data-e2e='2e9732e6-4d06-458d']",
                        "#creator-detail-profile-container .whitespace-pre-wrap",
                        "#creator-detail-profile-container [class*='break-words']",
                    ];
                    let bio = "";
                    for (const selector of bioSelectors) {
                        const node = document.querySelector(selector);
                        if (!(node instanceof HTMLElement) || !isVisible(node)) {
                            continue;
                        }
                        const text = (node.innerText || node.textContent || "").trim();
                        if (text.length > bio.length) {
                            bio = text;
                        }
                    }
                    let hasBadge = false;
                    let badgeStrategy = "";
                    const seen = new Set();
                    for (const root of roots) {
                        const rootName = root.id ? `#${root.id}` : root.tagName.toLowerCase();
                        for (const node of root.querySelectorAll(
                            "svg, button, a, [role='button'], div.cursor-pointer",
                        )) {
                            const target =
                                node instanceof HTMLElement ? node : node?.parentElement;
                            if (!(target instanceof HTMLElement) || seen.has(target)) {
                                continue;
                            }
                            seen.add(target);
                            if (!isVisible(target)) {
                                continue;
                            }
                            const description = describeTarget(target);
                            const matchedKeyword = keywordVariants.find(
                                (keyword) => keyword && description.includes(keyword),
                            );
                            if (!matchedKeyword) {
                                continue;
                            }
                            hasBadge = true;
                            badgeStrategy = `profile_dom:${rootName}:${matchedKeyword}`;
                            break;
                        }
                        if (hasBadge) {
                            break;
                        }
                    }
                    return {
                        ready: Boolean(bio) || hasBadge,
                        bio,
                        hasBadge,
                        badgeStrategy,
                    };
                }
                """,
                {"keywords": list(CONTACT_ICON_CLASS_KEYWORDS)},
            )
        except PlaywrightError:
            return {"ready": False, "bio": "", "has_badge": False, "badge_strategy": ""}
        if not isinstance(result, dict):
            return {"ready": False, "bio": "", "has_badge": False, "badge_strategy": ""}
        return {
            "ready": bool(result.get("ready")),
            "bio": str(result.get("bio") or ""),
            "has_badge": bool(result.get("hasBadge")),
            "badge_strategy": str(result.get("badgeStrategy") or ""),
        }

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
        self._paused = True
        self._pause_reason = reason
        self._last_message = message
        self._pause_step = self._current_step or "idle"
        self._attempt_in_progress = False
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
        self._contact_positive_signal = False
        self._profile_patch_applied = False
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
