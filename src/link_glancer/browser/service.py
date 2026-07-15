from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, sync_playwright

from link_glancer.browser.base import (
    BrowserController,
    BrowserLaunchRequest,
    BrowserStatus,
    BufferBlock,
)
from link_glancer.browser.detector import BrowserCandidate, detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import TaskItem

TIKTOK_HOST = "www.tiktok.com"
BUFFER_PAGE_READY_TIMEOUT_MS = 1000
BUFFER_PAGE_READY_POLL_MS = 100
MAX_NEW_PREFETCH_PAGES_PER_SYNC = 2
PLAYWRIGHT_ALLOWED_DEFAULT_ARGS = [
    "--no-sandbox",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
]


@dataclass(slots=True)
class _PendingPage:
    task: TaskItem
    url: str


class PlaywrightBrowserController(BrowserController):
    def __init__(self) -> None:
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._candidate: BrowserCandidate | None = None
        self._confirmation_page = None
        self._current_page = None
        self._current_task_index: int | None = None
        self._ready_pages_by_task_index: dict[int, object] = {}
        self._controlled_page_ids: dict[object, str] = {}
        self._last_active_controlled_page_id: str | None = None
        self._buffer_block: BufferBlock | None = None
        self._blocked_page = None
        self._blocked_pending: _PendingPage | None = None
        self._status = BrowserStatus(
            active_browser=None,
            executable_path=None,
            running=False,
            message="No browser started",
        )

    def launch(self, request: BrowserLaunchRequest) -> None:
        self.shutdown()

        candidate = detect_browser(request.browser_name, request.executable_path)
        if candidate is None:
            self._candidate = None
            self._status = BrowserStatus(
                active_browser=None,
                executable_path=None,
                running=False,
                message="No supported browser detected",
            )
            return

        self._candidate = candidate
        environment_dir = ensure_browser_environment_dir(request.browser_config_id)
        try:
            self._playwright_manager = sync_playwright().start()
            self._playwright = cast(Playwright, self._playwright_manager)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(environment_dir),
                headless=False,
                executable_path=str(candidate.executable_path),
                args=request.launch_args or [],
                ignore_default_args=PLAYWRIGHT_ALLOWED_DEFAULT_ARGS,
            )
            self._context.expose_binding(
                "__linkGlancerReviewStateChanged",
                self._handle_review_page_activity_signal,
            )
            self._status = BrowserStatus(
                active_browser=candidate.name,
                executable_path=candidate.executable_path,
                running=True,
                message=f"Running with {candidate.name}",
            )
        except PlaywrightError as exc:
            self._status = BrowserStatus(
                active_browser=candidate.name,
                executable_path=candidate.executable_path,
                running=False,
                message=f"Launch failed: {exc}",
            )
            self.shutdown()

    def ensure_running(self) -> None:
        if self._context is None:
            return
        if len(self._context.pages) == 0:
            self._context.new_page()

    def open_confirmation_page(self, url: str) -> None:
        if self._context is None:
            return
        self.close_confirmation_page()
        page = self._context.new_page()
        self._confirmation_page = page
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.bring_to_front()
        except PlaywrightError as exc:
            self._status = BrowserStatus(
                active_browser=self._candidate.name if self._candidate else None,
                executable_path=self._candidate.executable_path if self._candidate else None,
                running=self._context is not None,
                message=f"Confirmation page failed: {exc}",
            )

    def close_confirmation_page(self) -> None:
        if self._confirmation_page is None:
            return
        self._close_page(self._confirmation_page)
        self._confirmation_page = None

    def sync_buffer(self, tasks: list[TaskItem], url_field: str) -> None:
        if self._context is None:
            return
        if not tasks:
            self._clear_runtime_pages()
            return

        target_pages = [
            pending for task in tasks if (pending := self._pending_page(task, url_field))
        ]
        if not target_pages:
            self._clear_runtime_pages()
            return

        target_current = target_pages[0]
        desired_prefetch = target_pages[1:]
        desired_ready_indexes = {pending.task.task_index for pending in desired_prefetch}

        promoted_current_page = self._ready_pages_by_task_index.pop(
            target_current.task.task_index, None
        )
        self._prune_ready_pages(desired_ready_indexes)
        if not self._ensure_current_page_loaded(target_current, promoted_current_page):
            return

        if self._buffer_block is not None and self._blocked_pending is not None:
            return

        missing_prefetch = [
            pending
            for pending in desired_prefetch
            if pending.task.task_index not in self._ready_pages_by_task_index
        ]

        opened_count = 0
        while (
            missing_prefetch
            and opened_count < MAX_NEW_PREFETCH_PAGES_PER_SYNC
            and self._open_page_count() < len(tasks)
        ):
            pending = missing_prefetch.pop(0)
            if not self._open_prefetch_page(pending):
                break
            opened_count += 1

        self._restore_current_page_front()

    def status(self) -> BrowserStatus:
        return self._status

    def buffer_block(self) -> BufferBlock | None:
        return self._buffer_block

    def resume_buffer(self) -> None:
        if self._blocked_page is None or self._blocked_pending is None:
            self._buffer_block = None
            return

        self._buffer_block = None
        retry_block = self._reload_page(self._blocked_page, self._blocked_pending.url)
        if retry_block is None:
            if self._blocked_pending.task.task_index == self._current_task_index:
                self._current_page = self._blocked_page
            else:
                self._ready_pages_by_task_index[self._blocked_pending.task.task_index] = (
                    self._blocked_page
                )
            self._blocked_page = None
            self._blocked_pending = None
            self._restore_current_page_front()
            return

        self._buffer_block = BufferBlock(
            reason=retry_block.reason,
            message=retry_block.message,
            task_index=self._blocked_pending.task.task_index,
            url=self._blocked_pending.url,
        )
        self._restore_current_page_front()

    def current_review_page_matches_active_tab(self) -> bool:
        if self._current_page is None:
            return True
        current_page_id = self._controlled_page_ids.get(self._current_page)
        if current_page_id is None:
            return False
        return current_page_id == self._last_active_controlled_page_id

    def shutdown(self) -> None:
        self._clear_runtime_pages()
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
            self._context = None
        if self._playwright_manager is not None:
            try:
                self._playwright_manager.stop()
            except PlaywrightError:
                pass
            self._playwright_manager = None
        self._playwright = None

    def _ensure_current_page_loaded(self, pending: _PendingPage, promoted_page=None) -> bool:
        if self._current_page is None:
            if promoted_page is not None:
                self._current_page = promoted_page
                self._current_task_index = pending.task.task_index
                self._bring_page_to_front(self._current_page)
                return True
            self._current_page = self._context.new_page()
            self._register_controlled_page(self._current_page)
            self._current_task_index = pending.task.task_index
            load_block = self._load_page(self._current_page, pending.url)
            if load_block is not None:
                self._blocked_page = self._current_page
                self._blocked_pending = pending
                self._buffer_block = BufferBlock(
                    reason=load_block.reason,
                    message=load_block.message,
                    task_index=pending.task.task_index,
                    url=pending.url,
                )
                return False
            self._bring_page_to_front(self._current_page)
            return True

        if self._current_task_index == pending.task.task_index:
            if promoted_page is not None and promoted_page is not self._current_page:
                self._close_page(promoted_page)
            return True

        if promoted_page is not None:
            previous_page = self._current_page
            self._current_page = promoted_page
            self._current_task_index = pending.task.task_index
            self._bring_page_to_front(self._current_page)
            if previous_page is not None and previous_page is not self._current_page:
                self._close_page(previous_page)
            return True

        self._current_task_index = pending.task.task_index
        load_block = self._load_page(self._current_page, pending.url)
        if load_block is not None:
            self._blocked_page = self._current_page
            self._blocked_pending = pending
            self._buffer_block = BufferBlock(
                reason=load_block.reason,
                message=load_block.message,
                task_index=pending.task.task_index,
                url=pending.url,
            )
            return False
        self._bring_page_to_front(self._current_page)
        return True

    def _open_prefetch_page(self, pending: _PendingPage) -> bool:
        page = self._context.new_page()
        self._register_controlled_page(page)
        self._restore_current_page_front()
        load_block = self._load_page(page, pending.url)
        if load_block is None:
            self._ready_pages_by_task_index[pending.task.task_index] = page
            self._restore_current_page_front()
            return True

        self._bring_page_to_front(page)
        retry_block = self._reload_page(page, pending.url)
        if retry_block is None:
            self._ready_pages_by_task_index[pending.task.task_index] = page
            self._restore_current_page_front()
            return True

        self._blocked_page = page
        self._blocked_pending = pending
        self._buffer_block = BufferBlock(
            reason=retry_block.reason,
            message=retry_block.message,
            task_index=pending.task.task_index,
            url=pending.url,
        )
        self._status = BrowserStatus(
            active_browser=self._candidate.name if self._candidate else None,
            executable_path=self._candidate.executable_path if self._candidate else None,
            running=self._context is not None,
            message=retry_block.message,
        )
        return False

    def _pending_page(self, task: TaskItem, url_field: str) -> _PendingPage | None:
        url = task.task_data.get(url_field)
        if not isinstance(url, str) or not url.strip():
            return None
        return _PendingPage(task=task, url=url.strip())

    def _load_page(self, page, url: str) -> BufferBlock | None:
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                return BufferBlock(
                    reason="navigation_error",
                    message=f"页面加载失败（HTTP {response.status}），请处理后点击继续。",
                )
            navigation_error = self._detect_page_navigation_error(page)
            if navigation_error is not None:
                return BufferBlock(reason="navigation_error", message=navigation_error)
            if self._requires_ready_probe(url):
                probe_block = self._probe_tiktok_page(page)
                if probe_block is not None:
                    return probe_block
            self._attach_review_page_activity_tracker(page)
            return None
        except PlaywrightError as exc:
            return BufferBlock(reason="navigation_error", message=f"页面打开失败：{exc}")

    def _reload_page(self, page, url: str) -> BufferBlock | None:
        try:
            page.reload(wait_until="domcontentloaded")
        except PlaywrightError:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                return BufferBlock(reason="navigation_error", message=f"页面打开失败：{exc}")
        return self._load_page(page, url)

    def _prune_ready_pages(self, desired_indexes: set[int]) -> None:
        for task_index, page in list(self._ready_pages_by_task_index.items()):
            if task_index in desired_indexes:
                continue
            self._close_page(page)
            self._ready_pages_by_task_index.pop(task_index, None)

    def _restore_current_page_front(self) -> None:
        if self._blocked_page is not None and self._buffer_block is not None:
            return
        self._bring_page_to_front(self._current_page)

    def _bring_page_to_front(self, page) -> None:
        if page is None:
            return
        try:
            page.bring_to_front()
            page_id = self._controlled_page_ids.get(page)
            if page_id is not None:
                self._last_active_controlled_page_id = page_id
        except PlaywrightError:
            pass

    def _open_page_count(self) -> int:
        return len(self._ready_pages_by_task_index) + (1 if self._current_page is not None else 0)

    def _clear_runtime_pages(self) -> None:
        for page in self._ready_pages_by_task_index.values():
            self._close_page(page)
        self._ready_pages_by_task_index.clear()
        self._close_page(self._blocked_page)
        self._blocked_page = None
        self._blocked_pending = None
        self._buffer_block = None
        self._last_active_controlled_page_id = None
        if self._current_page is not None:
            self._close_page(self._current_page)
            self._current_page = None
        self._current_task_index = None
        if self._confirmation_page is not None:
            self._close_page(self._confirmation_page)
            self._confirmation_page = None

    def _close_page(self, page) -> None:
        if page is None:
            return
        page_id = self._controlled_page_ids.pop(page, None)
        if page_id is not None and self._last_active_controlled_page_id == page_id:
            self._last_active_controlled_page_id = None
        try:
            page.close()
        except PlaywrightError:
            pass

    def _register_controlled_page(self, page) -> None:
        self._controlled_page_ids[page] = uuid4().hex

    def _attach_review_page_activity_tracker(self, page) -> None:
        page_id = self._controlled_page_ids.get(page)
        if page_id is None:
            return
        try:
            page.evaluate(
                """
                (pageId) => {
                    const notify = (state) => {
                        try {
                            window.__linkGlancerReviewStateChanged(pageId, state);
                        } catch (error) {
                        }
                    };
                    if (window.__linkGlancerReviewTrackerAttached === pageId) {
                        notify(document.visibilityState === "visible" ? "active" : "inactive");
                        return;
                    }
                    window.__linkGlancerReviewTrackerAttached = pageId;
                    window.addEventListener("focus", () => notify("active"), true);
                    window.addEventListener("blur", () => notify("inactive"), true);
                    document.addEventListener("visibilitychange", () => {
                        notify(document.visibilityState === "visible" ? "active" : "inactive");
                    });
                    notify(document.visibilityState === "visible" ? "active" : "inactive");
                }
                """,
                page_id,
            )
        except PlaywrightError:
            pass

    def _handle_review_page_activity_signal(self, source, page_id: str, state: str) -> None:
        if state == "active":
            self._last_active_controlled_page_id = page_id
            return
        if self._last_active_controlled_page_id == page_id:
            self._last_active_controlled_page_id = None

    def _requires_ready_probe(self, url: str) -> bool:
        return urlsplit(url).netloc.casefold() == TIKTOK_HOST

    def _probe_tiktok_page(self, page) -> BufferBlock | None:
        poll_count = max(BUFFER_PAGE_READY_TIMEOUT_MS // BUFFER_PAGE_READY_POLL_MS, 1)
        for _ in range(poll_count):
            captcha_detected = page.locator(
                "#captcha-verify-container-main-page, "
                ".captcha-verify-container, "
                "#captcha_slide_button"
            ).count()
            if captcha_detected:
                return BufferBlock(
                    reason="captcha_required",
                    message="检测到验证码，请先在浏览器中完成验证，再点击继续。",
                )
            captcha_text = page.get_by_text("Drag the slider to fit the puzzle")
            if captcha_text.count():
                return BufferBlock(
                    reason="captcha_required",
                    message="检测到验证码，请先在浏览器中完成验证，再点击继续。",
                )
            ready_detected = page.locator(
                '[data-e2e="user-page"], [data-e2e="user-title"], '
                '[data-e2e="followers-count"], [data-e2e="user-post-item"]'
            ).count()
            if ready_detected:
                return None
            page.wait_for_timeout(BUFFER_PAGE_READY_POLL_MS)
        return BufferBlock(
            reason="page_not_ready",
            message="页面未在预期时间内完成加载，请处理后点击继续。",
        )

    def _detect_page_navigation_error(self, page) -> str | None:
        try:
            page_url = page.url.casefold()
            title = page.title().casefold()
            body_text = page.locator("body").inner_text(timeout=200).casefold()
        except PlaywrightError:
            return None
        if page_url.startswith("chrome-error://"):
            return "页面打开失败，请处理浏览器错误页后点击继续。"
        error_markers = (
            "this site can't be reached",
            "err_connection_refused",
            "err_name_not_resolved",
            "404",
            "not found",
            "connection refused",
        )
        if any(marker in title or marker in body_text for marker in error_markers):
            return "页面加载失败，请处理错误页面后点击继续。"
        return None


def create_browser_controller() -> BrowserController:
    return PlaywrightBrowserController()
