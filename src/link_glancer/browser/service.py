from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

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
        self._pages_by_task_index: dict[int, object] = {}
        self._confirmation_page = None
        self._buffer_block: BufferBlock | None = None
        self._pending_pages: list[_PendingPage] = []
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
        try:
            self._confirmation_page.close()
        except PlaywrightError:
            pass
        self._confirmation_page = None

    def sync_buffer(self, tasks: list[TaskItem], url_field: str) -> None:
        if self._context is None:
            return

        target_indexes = {task.task_index for task in tasks}
        for task_index, page in list(self._pages_by_task_index.items()):
            if task_index in target_indexes:
                continue
            try:
                page.close()
            except PlaywrightError:
                pass
            self._pages_by_task_index.pop(task_index, None)

        pending_pages: list[_PendingPage] = []
        for task in tasks:
            if task.task_index in self._pages_by_task_index:
                continue
            url = task.task_data.get(url_field)
            if not isinstance(url, str) or not url.strip():
                continue
            pending_pages.append(_PendingPage(task=task, url=url.strip()))

        if self._buffer_block is not None and self._pending_pages:
            pending_pages = list(self._pending_pages)

        self._pending_pages = []
        for pending in pending_pages:
            if self._buffer_block is not None:
                self._pending_pages.append(pending)
                continue
            page = self._context.new_page()
            self._pages_by_task_index[pending.task.task_index] = page
            try:
                response = page.goto(pending.url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    self._block_buffer(
                        reason="navigation_error",
                        message=f"页面加载失败（HTTP {response.status}），请处理后点击继续。",
                        task_index=pending.task.task_index,
                        url=pending.url,
                    )
                    self._pending_pages.append(pending)
                    break
                navigation_error = self._detect_page_navigation_error(page)
                if navigation_error is not None:
                    self._block_buffer(
                        reason="navigation_error",
                        message=navigation_error,
                        task_index=pending.task.task_index,
                        url=pending.url,
                    )
                    self._pending_pages.append(pending)
                    break
                if self._requires_ready_probe(pending.url):
                    probe_result = self._probe_tiktok_page(page)
                    if probe_result is not None:
                        self._block_buffer(
                            reason=probe_result.reason,
                            message=probe_result.message,
                            task_index=pending.task.task_index,
                            url=pending.url,
                        )
                        self._pending_pages.append(pending)
                        break
            except PlaywrightError as exc:
                self._block_buffer(
                    reason="navigation_error",
                    message=f"页面打开失败：{exc}",
                    task_index=pending.task.task_index,
                    url=pending.url,
                )
                self._pending_pages.append(pending)
                break

    def status(self) -> BrowserStatus:
        return self._status

    def buffer_block(self) -> BufferBlock | None:
        return self._buffer_block

    def resume_buffer(self) -> None:
        self._buffer_block = None

    def shutdown(self) -> None:
        self._pages_by_task_index.clear()
        self._confirmation_page = None
        self._buffer_block = None
        self._pending_pages.clear()
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

    def _block_buffer(
        self,
        *,
        reason: str,
        message: str,
        task_index: int | None,
        url: str | None,
    ) -> None:
        self._buffer_block = BufferBlock(
            reason=reason,
            message=message,
            task_index=task_index,
            url=url,
        )
        self._status = BrowserStatus(
            active_browser=self._candidate.name if self._candidate else None,
            executable_path=self._candidate.executable_path if self._candidate else None,
            running=self._context is not None,
            message=message,
        )

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
