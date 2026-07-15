from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, sync_playwright

from link_glancer.browser.base import BrowserController, BrowserLaunchRequest, BrowserStatus
from link_glancer.browser.detector import BrowserCandidate, detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import TaskItem

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
        try:
            if len(self._context.pages) == 0:
                self._context.new_page()
        except PlaywrightError as exc:
            self._set_runtime_failure(f"浏览器已不可用：{exc}")

    def open_confirmation_page(self, url: str) -> None:
        if self._context is None:
            return
        self.close_confirmation_page()
        page = self._create_page()
        if page is None:
            return
        self._confirmation_page = page
        self._navigate_page(page, url)
        self._bring_page_to_front(page)

    def close_confirmation_page(self) -> None:
        if self._confirmation_page is None:
            return
        self._close_page(self._confirmation_page)
        self._confirmation_page = None

    def sync_buffer(self, tasks: list[TaskItem], url_field: str) -> None:
        if self._context is None:
            return
        if not self._status.running:
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
        self._ensure_current_page_loaded(target_current, promoted_current_page)

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
            self._open_prefetch_page(pending)
            opened_count += 1

        self._bring_page_to_front(self._current_page)

    def status(self) -> BrowserStatus:
        return self._status

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

    def _ensure_current_page_loaded(self, pending: _PendingPage, promoted_page=None) -> None:
        if self._current_page is None:
            if promoted_page is not None:
                self._current_page = promoted_page
                self._current_task_index = pending.task.task_index
                self._bring_page_to_front(self._current_page)
                return
            self._current_page = self._create_page()
            if self._current_page is None:
                return
            self._current_task_index = pending.task.task_index
            self._navigate_page(self._current_page, pending.url)
            self._bring_page_to_front(self._current_page)
            return

        if self._current_task_index == pending.task.task_index:
            if promoted_page is not None and promoted_page is not self._current_page:
                self._close_page(promoted_page)
            return

        if promoted_page is not None:
            previous_page = self._current_page
            self._current_page = promoted_page
            self._current_task_index = pending.task.task_index
            self._bring_page_to_front(self._current_page)
            if previous_page is not None and previous_page is not self._current_page:
                self._close_page(previous_page)
            return

        self._current_task_index = pending.task.task_index
        self._navigate_page(self._current_page, pending.url)
        self._bring_page_to_front(self._current_page)

    def _open_prefetch_page(self, pending: _PendingPage) -> None:
        page = self._create_page()
        if page is None:
            return
        self._bring_page_to_front(self._current_page)
        self._navigate_page(page, pending.url)
        self._ready_pages_by_task_index[pending.task.task_index] = page
        self._bring_page_to_front(self._current_page)

    def _pending_page(self, task: TaskItem, url_field: str) -> _PendingPage | None:
        url = task.task_data.get(url_field)
        if not isinstance(url, str) or not url.strip():
            return None
        return _PendingPage(task=task, url=url.strip())

    def _navigate_page(self, page, url: str) -> None:
        try:
            page.evaluate("(targetUrl) => { window.location.replace(targetUrl); }", url)
        except PlaywrightError as exc:
            self._set_runtime_failure(f"标签跳转失败：{exc}")

    def _prune_ready_pages(self, desired_indexes: set[int]) -> None:
        for task_index, page in list(self._ready_pages_by_task_index.items()):
            if task_index in desired_indexes:
                continue
            self._close_page(page)
            self._ready_pages_by_task_index.pop(task_index, None)

    def _bring_page_to_front(self, page) -> None:
        if page is None:
            return
        try:
            page.bring_to_front()
        except PlaywrightError as exc:
            self._set_runtime_failure(f"标签切换失败：{exc}")

    def _open_page_count(self) -> int:
        return len(self._ready_pages_by_task_index) + (1 if self._current_page is not None else 0)

    def _clear_runtime_pages(self) -> None:
        for page in self._ready_pages_by_task_index.values():
            self._close_page(page)
        self._ready_pages_by_task_index.clear()
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
        try:
            page.close()
        except PlaywrightError:
            pass

    def _create_page(self):
        if self._context is None:
            return None
        try:
            return self._context.new_page()
        except PlaywrightError as exc:
            self._set_runtime_failure(f"打开标签失败：{exc}")
            return None

    def _set_runtime_failure(self, message: str) -> None:
        self._status = BrowserStatus(
            active_browser=self._candidate.name if self._candidate else None,
            executable_path=self._candidate.executable_path if self._candidate else None,
            running=False,
            message=message,
        )


def create_browser_controller() -> BrowserController:
    return PlaywrightBrowserController()
