from __future__ import annotations

from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response

from creator_enrichment.constants import DETAIL_URL_TEMPLATE
from creator_enrichment.parsers import normalized_creator_id, normalized_region, query_param
from link_glancer.tasks.models import TaskItem


class BrowserGuardMixin:
    def _ensure_single_work_page(self, *, reason: str, force_reset: bool = False) -> bool:
        if self._context is None:
            return False
        try:
            pages = [page for page in self._context.pages if self._page_is_alive(page)]
        except PlaywrightError:
            self._handle_browser_closed()
            return False
        target_task_index = self._current_task_index
        target_url = self._detail_url_for_task(target_task_index) or self._attempt_page_url or ""
        self._log_event(
            "page_normalize_before",
            reason=reason,
            page_count=len(pages),
            target_url=target_url,
            guard_breached=self._page_guard_breached,
            guard_breach_reason=self._page_guard_breach_reason,
            force_reset=force_reset,
        )
        reset_applied = force_reset or self._page_guard_breached
        if force_reset or self._page_guard_breached:
            pages = self._reset_page_environment(
                reason=reason,
                pages=pages,
                reset_reason=(
                    "forced_retry_reset" if force_reset else self._page_guard_breach_reason
                ),
            )
        recreated = False
        work_page = self._select_work_page_candidate(
            pages,
            task_index=target_task_index,
            target_url=target_url or None,
        )
        if work_page is None:
            work_page = self._create_page()
            if work_page is None:
                self._last_message = "无法创建补充采集工作页面。"
                return False
            recreated = True
            self._log_event("work_page_recreated", reason=reason)
            pages = [work_page]
        else:
            self._log_event(
                "work_page_reused",
                reason=reason,
                page_url=self._page_url(work_page),
            )
        for extra_page in pages:
            if extra_page is work_page:
                continue
            try:
                extra_page.close()
                self._log_event(
                    "extra_page_closed",
                    reason=reason,
                    page_url=self._page_url(extra_page),
                )
            except PlaywrightError:
                pass
        if self._page is not work_page:
            self._collection_mode_installed = False
            self._work_page_task_index = None
        self._page = work_page
        self._attach_page_diagnostics(work_page, source="work_page")
        if recreated:
            self._restore_work_page_after_recreation(reason=reason)
        elif reset_applied and target_task_index is not None and target_url:
            self._reset_work_page_after_normalization(
                reason=reason,
                task_index=target_task_index,
                target_url=target_url,
            )
        elif target_task_index is not None and target_url:
            self._repair_work_page_if_needed(
                reason=reason,
                task_index=target_task_index,
                target_url=target_url,
            )
        self._log_event(
            "page_normalize_after",
            reason=reason,
            page_count=len([page for page in self._context.pages if self._page_is_alive(page)]),
        )
        self._page_guard_breached = False
        self._page_guard_breach_reason = ""
        return True

    def _reset_page_environment(
        self,
        *,
        reason: str,
        pages: list[Page],
        reset_reason: str,
    ) -> list[Page]:
        survivor = self._select_reset_survivor(pages)
        kept_pages: list[Page] = []
        for page in pages:
            if page is survivor:
                kept_pages.append(page)
                self._log_event(
                    "page_preserved_for_reset",
                    reason=reason,
                    page_url=self._page_url(page),
                    reset_reason=reset_reason,
                    was_work_page=page is self._page,
                )
                continue
            try:
                page_url = self._page_url(page)
                page.close()
                self._log_event(
                    "page_closed_for_reset",
                    reason=reason,
                    page_url=page_url,
                    reset_reason=reset_reason,
                    was_work_page=page is self._page,
                )
            except PlaywrightError:
                self._log_event(
                    "page_close_for_reset_failed",
                    reason=reason,
                    page_url=self._page_url(page),
                    reset_reason=reset_reason,
                    was_work_page=page is self._page,
                )
                if self._page_is_alive(page):
                    kept_pages.append(page)
        self._page = survivor if self._page_is_alive(survivor) else None
        self._work_page_task_index = None
        self._collection_mode_installed = False
        return [page for page in kept_pages if self._page_is_alive(page)]

    def _select_reset_survivor(self, pages: list[Page]) -> Page | None:
        alive_pages = [page for page in pages if self._page_is_alive(page)]
        if not alive_pages:
            return None
        if self._page in alive_pages:
            return self._page
        for page in alive_pages:
            if not self._is_blank_page(page):
                return page
        return alive_pages[0]

    def _select_work_page_candidate(
        self,
        pages: list[Page],
        *,
        task_index: int | None,
        target_url: str | None,
    ) -> Page | None:
        if self._page in pages and self._is_trusted_detail_page(
            self._page,
            task_index=task_index,
            target_url=target_url,
        ):
            return self._page
        if target_url:
            for page in pages:
                if self._is_trusted_detail_page(page, task_index=task_index, target_url=target_url):
                    return page
        if self._page in pages and not self._is_blank_page(self._page):
            return self._page
        for page in pages:
            if not self._is_blank_page(page):
                return page
        if self._page in pages:
            return self._page
        return pages[0] if pages else None

    def _repair_work_page_if_needed(
        self,
        *,
        reason: str,
        task_index: int,
        target_url: str,
    ) -> None:
        if self._page is None:
            return
        if self._is_trusted_detail_page(self._page, task_index=task_index, target_url=target_url):
            self._assign_page_task_index(self._page, task_index)
            return
        self._log_event(
            "work_page_repair",
            reason=reason,
            task_index=task_index,
            page_url=self._safe_page_url(),
            target_url=target_url,
        )
        self._navigate_page(self._page, target_url)
        self._assign_page_task_index(self._page, task_index)
        self._ensure_collection_mode_for_page(self._page)

    def _reset_work_page_after_normalization(
        self,
        *,
        reason: str,
        task_index: int,
        target_url: str,
    ) -> None:
        if self._page is None:
            return
        self._log_event(
            "work_page_reset_navigation",
            reason=reason,
            task_index=task_index,
            page_url=self._safe_page_url(),
            target_url=target_url,
        )
        self._navigate_page(self._page, target_url)
        self._assign_page_task_index(self._page, task_index)
        self._ensure_collection_mode_for_page(self._page)

    def _page_is_alive(self, page: Page | None) -> bool:
        if page is None:
            return False
        try:
            _ = page.url
        except PlaywrightError:
            return False
        return True

    def _sync_current_page(self) -> None:
        if not self._ensure_single_work_page(reason="sync_current_page"):
            return
        pending = self._pending_items()
        if not pending:
            return
        current_item = pending[0]
        self._ensure_current_page(current_item)

    def _ensure_current_page(self, item: TaskItem) -> None:
        if self._page is None:
            return
        target_task_index = item.task_index
        target_url = self._detail_url_for_item(item)
        if self._page_matches_task(
            page=self._page,
            task_index=target_task_index,
            target_url=target_url,
        ):
            self._current_task_index = target_task_index
            self._current_region = normalized_region(item.task_data.get("selection_region"))
            self._ensure_collection_mode_for_page(self._page)
            self._bring_page_to_front(self._page)
            return
        self._current_task_index = target_task_index
        self._current_region = normalized_region(item.task_data.get("selection_region"))
        self._navigate_page(self._page, target_url)
        self._work_page_task_index = target_task_index
        self._ensure_collection_mode_for_page(self._page)
        self._log_event(
            "current_committed",
            task_index=item.task_index,
            creator_oecuid=normalized_creator_id(item.task_data.get("creator_oecuid")),
            page_url=target_url,
        )
        self._bring_page_to_front(self._page)

    def _current_page_matches_current_item(self) -> bool:
        if self._page is None or self._current_task_index is None:
            return False
        target_url = self._detail_url_for_task(self._current_task_index)
        return self._is_trusted_detail_page(
            self._page,
            task_index=self._current_task_index,
            target_url=target_url,
        )

    def _can_reload_current_page(self) -> bool:
        if self._page is None or self._current_task_index is None:
            return False
        target_url = self._detail_url_for_task(self._current_task_index)
        if not self._is_trusted_detail_page(
            self._page,
            task_index=self._current_task_index,
            target_url=target_url,
        ):
            return False
        current_url = self._safe_page_url()
        if not current_url:
            return False
        try:
            current_path = urlsplit(current_url).path
            target_path = urlsplit(target_url or "").path
        except ValueError:
            return False
        if not target_path or current_path != target_path:
            return False
        return current_path == urlsplit(DETAIL_URL_TEMPLATE).path

    def _is_blank_page(self, page: Page | None) -> bool:
        current_url = self._page_url(page).strip()
        return not current_url or current_url == "about:blank"

    def _page_matches_detail_target(self, page: Page | None, target_url: str | None) -> bool:
        current_url = self._page_url(page)
        if not current_url or current_url == "about:blank" or not target_url:
            return False
        try:
            current_parts = urlsplit(current_url)
            target_parts = urlsplit(target_url)
        except ValueError:
            return False
        if current_parts.netloc != target_parts.netloc:
            return False
        if current_parts.path != target_parts.path:
            return False
        return query_param(current_url, "cid") == query_param(target_url, "cid") and query_param(
            current_url, "shop_region"
        ) == query_param(target_url, "shop_region")

    def _is_trusted_detail_page(
        self,
        page: Page | None,
        *,
        task_index: int | None,
        target_url: str | None,
    ) -> bool:
        if page is None or task_index is None:
            return False
        if not self._page_matches_detail_target(page, target_url):
            return False
        assigned_task_index = self._assigned_task_index(page)
        return assigned_task_index in {None, task_index}

    def _page_matches_task(
        self,
        *,
        page: Page | None,
        task_index: int | None,
        target_url: str | None,
    ) -> bool:
        if page is None or task_index is None:
            return False
        if self._assigned_task_index(page) != task_index:
            return False
        return self._page_matches_detail_target(page, target_url)

    def _restore_work_page_after_recreation(self, *, reason: str) -> None:
        if self._page is None:
            return
        target_url = self._attempt_page_url or self._detail_url_for_task(self._current_task_index)
        if not target_url:
            return
        self._navigate_page(self._page, target_url)
        if self._current_task_index is not None:
            self._work_page_task_index = self._current_task_index
        self._log_event(
            "work_page_restored",
            reason=reason,
            task_index=self._current_task_index,
            page_url=target_url,
        )

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

    def _handle_context_page(self, page: Page) -> None:
        self._attach_page_diagnostics(page, source="context_page")
        self._log_event(
            "context_page_opened",
            page_url=self._page_url(page),
            work_page_url=self._safe_page_url(),
            work_page_task_index=self._work_page_task_index,
        )
        if self._page is not None and page is not self._page:
            self._mark_page_guard_breached("context_page_opened")
            try:
                page.close()
                self._log_event(
                    "context_page_closed",
                    page_url=self._page_url(page),
                    work_page_url=self._safe_page_url(),
                    work_page_task_index=self._work_page_task_index,
                )
            except PlaywrightError:
                self._log_event(
                    "context_page_close_failed",
                    page_url=self._page_url(page),
                    work_page_url=self._safe_page_url(),
                    work_page_task_index=self._work_page_task_index,
                )

    def _attach_page_diagnostics(self, page: Page, *, source: str) -> None:
        page_key = id(page)
        if page_key in self._diagnostic_pages:
            return
        try:
            page.on(
                "popup",
                lambda popup: self._handle_popup_opened(page=page, source=source, popup=popup),
            )
            page.on(
                "framenavigated",
                lambda frame: self._handle_frame_navigated(page=page, source=source, frame=frame),
            )
            self._diagnostic_pages.add(page_key)
        except PlaywrightError:
            return

    def _handle_popup_opened(self, *, page: Page, source: str, popup: Page) -> None:
        popup_url = self._page_url(popup)
        self._mark_page_guard_breached("popup_opened")
        self._log_event(
            "page_popup_opened",
            source=source,
            page_url=self._page_url(page),
            popup_url=popup_url,
            work_page_url=self._safe_page_url(),
            work_page_task_index=self._work_page_task_index,
        )
        if self._page is None or popup is self._page:
            return
        try:
            popup.close()
            self._log_event(
                "popup_closed",
                source=source,
                popup_url=popup_url,
                work_page_url=self._safe_page_url(),
                work_page_task_index=self._work_page_task_index,
            )
        except PlaywrightError:
            self._log_event(
                "popup_close_failed",
                source=source,
                popup_url=popup_url,
                work_page_url=self._safe_page_url(),
                work_page_task_index=self._work_page_task_index,
            )

    def _mark_page_guard_breached(self, reason: str) -> None:
        self._page_guard_breached = True
        self._page_guard_breach_reason = reason

    def _handle_frame_navigated(self, *, page: Page, source: str, frame) -> None:
        try:
            is_main_frame = frame == page.main_frame
            frame_url = frame.url
        except PlaywrightError:
            return
        if not is_main_frame:
            return
        self._log_event(
            "page_navigated",
            source=source,
            page_url=frame_url,
            is_work_page=page is self._page,
            work_page_task_index=self._work_page_task_index,
        )

    def _mark_collection_mode_stale(self, page: Page | None) -> None:
        if page is self._page:
            self._collection_mode_installed = False

    def _assign_page_task_index(self, page: Page | None, task_index: int | None) -> None:
        if page is self._page:
            self._work_page_task_index = task_index

    def _assigned_task_index(self, page: Page | None) -> int | None:
        if page is None or page is not self._page:
            return None
        return self._work_page_task_index

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
            self._attach_page_diagnostics(page, source="create_page")
            self._log_event("work_page_created")
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
