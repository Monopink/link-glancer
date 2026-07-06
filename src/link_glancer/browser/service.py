from __future__ import annotations

from typing import cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, sync_playwright

from link_glancer.browser.base import BrowserController, BrowserLaunchRequest, BrowserStatus
from link_glancer.browser.detector import BrowserCandidate, detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir
from link_glancer.tasks.models import TaskItem


class PlaywrightBrowserController(BrowserController):
    def __init__(self) -> None:
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._candidate: BrowserCandidate | None = None
        self._pages_by_task_index: dict[int, object] = {}
        self._confirmation_page = None
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
                ignore_default_args=["--no-sandbox"],
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

    def sync_buffer(self, tasks: list[TaskItem], url_field: str, current_task_index: int) -> None:
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

        for task in tasks:
            if task.task_index in self._pages_by_task_index:
                continue
            url = task.task_data.get(url_field)
            if not isinstance(url, str) or not url.strip():
                continue
            page = self._context.new_page()
            self._pages_by_task_index[task.task_index] = page
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                self._status = BrowserStatus(
                    active_browser=self._candidate.name if self._candidate else None,
                    executable_path=self._candidate.executable_path if self._candidate else None,
                    running=self._context is not None,
                    message=f"Navigation failed: {exc}",
                )

        current_page = self._pages_by_task_index.get(current_task_index)
        if current_page is not None:
            try:
                current_page.bring_to_front()
            except PlaywrightError:
                pass

    def status(self) -> BrowserStatus:
        return self._status

    def shutdown(self) -> None:
        self._pages_by_task_index.clear()
        self._confirmation_page = None
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright_manager is not None:
            self._playwright_manager.stop()
            self._playwright_manager = None
        self._playwright = None


def create_browser_controller() -> BrowserController:
    return PlaywrightBrowserController()
