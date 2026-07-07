from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, Response, sync_playwright

from creator_collector.exporter import (
    dedupe_creator_rows,
    export_collection_to_xlsx,
    save_backup_json,
)
from creator_collector.models import CreatorCollectionConfig, CreatorCollectionStatus
from link_glancer.browser.detector import detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir, ensure_creator_collector_dir
from link_glancer.tasks.models import BrowserConfig

REQUEST_API_PATH = "/api/v1/oec/affiliate/creator/marketplace/find"
DEFAULT_SAFETY_LIMIT = 1000
REQUEST_TIMEOUT_MS = 20_000
BASE_DELAY_RANGE = (1.2, 2.8)
BURST_DELAY_RANGE = (5.0, 8.0)
BURST_PAGE_INTERVAL = 8
MAX_REPEAT_PAGES = 2
BACKUP_INTERVAL_SECONDS = 5
HEADER_BLOCKLIST = {
    "content-length",
    "cookie",
    "host",
}


@dataclass(slots=True)
class _CapturedResponse:
    request_url: str
    headers: dict[str, str]
    payload: dict[str, object]


class CreatorCollectorSession:
    def __init__(self, browser_config: BrowserConfig) -> None:
        self._browser_config = browser_config
        self._playwright_manager = None
        self._playwright: Playwright | None = None
        self._context = None
        self._page = None
        self._config: CreatorCollectionConfig | None = None
        self._rows: list[dict[str, object]] = []
        self._paused = False
        self._completed = False
        self._last_message = "未启动"
        self._backup_path: Path | None = None
        self._started_at: datetime | None = None
        self._estimated_total_count: int | None = None
        self._last_backup_saved_at: datetime | None = None
        self._rows_dirty = False
        self._row_keys: set[str] = set()
        self._captured_responses: list[_CapturedResponse] = []
        self._request_template_url: str | None = None
        self._request_headers: dict[str, str] = {}
        self._next_pagination: dict[str, object] | None = None
        self._pages_fetched = 0
        self._next_request_at: datetime | None = None
        self._repeat_page_hits = 0
        self._safety_limit = DEFAULT_SAFETY_LIMIT

    def start(
        self,
        config: CreatorCollectionConfig,
        *,
        paused: bool = False,
    ) -> CreatorCollectionStatus:
        self.shutdown()
        candidate = detect_browser("configured", self._browser_config.executable_path or None)
        if candidate is None:
            self._last_message = "未找到可用浏览器。"
            return self.status()

        environment_dir = ensure_browser_environment_dir(self._browser_config.profile_id)
        try:
            self._rows.clear()
            self._row_keys.clear()
            self._captured_responses.clear()
            self._request_template_url = None
            self._request_headers = {}
            self._next_pagination = None
            self._pages_fetched = 0
            self._next_request_at = None
            self._repeat_page_hits = 0
            self._playwright_manager = sync_playwright().start()
            self._playwright = cast(Playwright, self._playwright_manager)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(environment_dir),
                headless=False,
                executable_path=str(candidate.executable_path),
                args=self._browser_config.launch_args or [],
                ignore_default_args=["--no-sandbox"],
            )
            self._context.on("response", self._handle_response)
            self._page = self._context.new_page()
            self._page.goto(config.page_url, wait_until="domcontentloaded")
            self._page.bring_to_front()
            self._config = config
            self._paused = paused
            self._completed = False
            self._started_at = datetime.now(UTC)
            self._estimated_total_count = None
            self._last_backup_saved_at = None
            self._rows_dirty = False
            self._safety_limit = max(config.safety_limit, 1)
            self._last_message = (
                "浏览器已启动，请确认登录、筛选与页面状态。" if paused else "等待第一页列表请求。"
            )
        except PlaywrightError as exc:
            self._last_message = f"浏览器启动失败：{exc}"
            self.shutdown()
        return self.status()

    def poll(self) -> CreatorCollectionStatus:
        if self._context is None:
            return self.status()
        if not self._ensure_runtime_alive():
            return self.status()

        self._flush_backup(force=False)
        if self._paused or self._completed:
            return self.status()

        if self._request_template_url is None:
            if self._bootstrap_from_captured_response():
                self._flush_backup(force=False)
                return self.status()
            if self._bootstrap_from_page_resources():
                self._flush_backup(force=False)
                return self.status()
            self._last_message = "等待列表请求；如已错过第一页，请在页面上重新触发一次请求。"
            return self.status()

        if len(self._rows) >= self._safety_limit:
            self._mark_completed(f"已达到安全上限 {self._safety_limit} 条。")
            return self.status()
        if not _pagination_has_more(self._next_pagination):
            self._mark_completed("采集完成，可导出或创建任务。")
            return self.status()

        if self._next_request_at is not None and datetime.now(UTC) < self._next_request_at:
            return self.status()

        payload = self._request_next_page()
        if payload is None:
            return self.status()
        self._apply_payload(payload, source="分页请求")
        self._flush_backup(force=False)
        return self.status()

    def pause(self) -> CreatorCollectionStatus:
        self._paused = True
        if not self._completed:
            self._last_message = "采集已暂停。"
        return self.status()

    def resume(self) -> CreatorCollectionStatus:
        if self._context is None:
            self._last_message = "采集会话未启动。"
            return self.status()
        if self._captured_responses and "请处理后点继续" in self._last_message:
            self._request_template_url = None
            self._request_headers = {}
            self._next_pagination = None
        self._paused = False
        if self._completed:
            self._last_message = "采集已完成，可导出或创建任务。"
            return self.status()
        if self._request_template_url is None:
            self._last_message = "等待第一页列表请求。"
        else:
            self._last_message = "已恢复采集。"
            self._schedule_next_request(immediate=True)
        return self.status()

    def stop(self) -> CreatorCollectionStatus:
        self._flush_backup(force=True)
        self._close_runtime(clear_collection_state=False)
        self._last_message = "采集已停止。"
        return self.status()

    def finish(self, destination_dir: Path | None = None) -> Path:
        if not self._rows:
            raise ValueError("当前没有可导出的采集数据。")
        self._rows = dedupe_creator_rows(self._rows)
        self._row_keys = {_row_identity(row) for row in self._rows}
        self._flush_backup(force=True)
        output_dir = destination_dir or (ensure_creator_collector_dir() / "exports")
        export_path = export_collection_to_xlsx(self._rows, output_dir)
        self._last_message = f"已导出到 {export_path}"
        return export_path

    def update_safety_limit(self, value: int) -> CreatorCollectionStatus:
        self._safety_limit = max(value, 1)
        if self._config is not None:
            self._config.safety_limit = self._safety_limit
        if len(self._rows) >= self._safety_limit and not self._completed:
            self._mark_completed(f"已达到安全上限 {self._safety_limit} 条。")
        return self.status()

    def status(self) -> CreatorCollectionStatus:
        estimated_end_at = None
        estimated_total_count = self._estimated_total_count
        if estimated_total_count is None and self._started_at is not None and self._rows:
            estimated_total_count = self._safety_limit
        if (
            self._started_at is not None
            and estimated_total_count is not None
            and len(self._rows) > 0
            and estimated_total_count >= len(self._rows)
        ):
            elapsed_seconds = max(
                int((datetime.now(UTC) - self._started_at).total_seconds()),
                1,
            )
            total_seconds = int(elapsed_seconds * estimated_total_count / len(self._rows))
            estimated_end_at = self._started_at + timedelta(seconds=total_seconds)
        return CreatorCollectionStatus(
            running=self._context is not None,
            paused=self._paused,
            completed=self._completed,
            collected_count=len(self._rows),
            pages_fetched=self._pages_fetched,
            safety_limit=self._safety_limit,
            last_message=self._last_message,
            rows=list(self._rows),
            started_at=self._started_at,
            estimated_total_count=estimated_total_count,
            estimated_end_at=estimated_end_at,
            backup_path=self._backup_path,
        )

    def shutdown(self) -> None:
        self._close_runtime(clear_collection_state=True)

    def _close_runtime(self, *, clear_collection_state: bool) -> None:
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
        self._config = None
        self._paused = False
        self._next_request_at = None
        if clear_collection_state:
            self._completed = False
            self._started_at = None
            self._estimated_total_count = None
            self._last_backup_saved_at = None
            self._rows_dirty = False
            self._row_keys.clear()
            self._rows.clear()
            self._captured_responses.clear()
            self._request_template_url = None
            self._request_headers = {}
            self._next_pagination = None
            self._pages_fetched = 0
            self._repeat_page_hits = 0

    def _ensure_runtime_alive(self) -> bool:
        if self._context is None:
            return False
        try:
            if not self._context.pages:
                self._close_runtime(clear_collection_state=False)
                self._last_message = "浏览器已关闭，可导出已采集数据。"
                return False
        except PlaywrightError:
            self._close_runtime(clear_collection_state=False)
            self._last_message = "浏览器已关闭，可导出已采集数据。"
            return False
        return True

    def _handle_response(self, response: Response) -> None:
        if not _matches_request_api(response.url):
            return
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                return
            headers = _sanitize_headers(response.request.all_headers())
        except (PlaywrightError, ValueError):
            return
        self._captured_responses.append(
            _CapturedResponse(
                request_url=response.request.url,
                headers=headers,
                payload=payload,
            )
        )
        if len(self._captured_responses) > 5:
            self._captured_responses = self._captured_responses[-5:]

    def _bootstrap_from_captured_response(self) -> bool:
        if not self._captured_responses:
            return False
        captured = self._captured_responses[-1]
        self._captured_responses.clear()
        self._request_template_url = captured.request_url
        self._request_headers = captured.headers
        self._apply_payload(captured.payload, source="首屏响应")
        return True

    def _bootstrap_from_page_resources(self) -> bool:
        if self._page is None:
            return False
        try:
            resource_urls = self._page.evaluate(
                """
                (apiPath) => performance
                    .getEntriesByType("resource")
                    .map((entry) => entry.name)
                    .filter((name) => typeof name === "string" && name.includes(apiPath))
                    .slice(-5)
                """,
                REQUEST_API_PATH,
            )
        except PlaywrightError:
            return False
        if not isinstance(resource_urls, list) or not resource_urls:
            return False
        request_url = str(resource_urls[-1]).strip()
        if not request_url:
            return False
        self._request_template_url = request_url
        self._request_headers = self._default_request_headers()
        try:
            payload = self._request_next_page(initial_url=request_url)
        except Exception:
            self._request_template_url = None
            return False
        if payload is None:
            self._request_template_url = None
            return False
        self._apply_payload(payload, source="首屏补抓")
        return True

    def _request_next_page(self, *, initial_url: str | None = None) -> dict[str, object] | None:
        if self._context is None:
            return None
        request_url = initial_url or _build_paginated_url(
            self._request_template_url,
            self._next_pagination,
        )
        if not request_url:
            self._pause_with_message("分页参数缺失，请处理后点继续。")
            return None

        try:
            response = self._context.request.get(
                request_url,
                headers=self._request_headers or self._default_request_headers(),
                fail_on_status_code=False,
                timeout=REQUEST_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            self._pause_with_message(f"分页请求失败，请处理后点继续：{exc}")
            return None

        if response.status != 200:
            self._pause_with_message(f"分页请求返回 {response.status}，请处理后点继续。")
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            self._pause_with_message(f"接口响应解析失败，请处理后点继续：{exc}")
            return None
        if not isinstance(payload, dict):
            self._pause_with_message("接口返回结构异常，请处理后点继续。")
            return None
        return payload

    def _apply_payload(self, payload: dict[str, object], *, source: str) -> None:
        rows = payload.get("creator_profile_list")
        if not isinstance(rows, list):
            self._pause_with_message(f"{source}缺少列表数据，请处理后点继续。")
            return

        added_count = 0
        page_keys: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            row_key = _row_identity(item)
            page_keys.add(row_key)
            if row_key in self._row_keys:
                continue
            self._row_keys.add(row_key)
            self._rows.append(item)
            added_count += 1
            if len(self._rows) >= self._safety_limit:
                break

        self._pages_fetched += 1
        self._next_pagination = payload.get("next_pagination")
        self._estimated_total_count = self._estimate_total_count(payload, len(rows))

        if rows and added_count == 0:
            self._repeat_page_hits += 1
        else:
            self._repeat_page_hits = 0

        if added_count > 0:
            self._rows_dirty = True

        if not rows:
            self._pause_with_message("分页返回空数据，请处理后点继续。")
            return
        if self._repeat_page_hits >= MAX_REPEAT_PAGES:
            self._mark_completed("检测到重复页，已停止继续翻页。")
            return
        if len(self._rows) >= self._safety_limit:
            self._mark_completed(f"已达到安全上限 {self._safety_limit} 条。")
            return
        if not _pagination_has_more(self._next_pagination):
            self._mark_completed("采集完成，可导出或创建任务。")
            return

        self._schedule_next_request(immediate=False)
        self._last_message = (
            f"已采集 {len(self._rows)} 条，已抓取 {self._pages_fetched} 页，等待下一页。"
        )

    def _estimate_total_count(self, payload: dict[str, object], page_size: int) -> int | None:
        candidates: list[object] = [
            payload.get("total"),
            payload.get("total_count"),
            payload.get("count"),
        ]
        for value in candidates:
            if isinstance(value, int) and value > 0:
                return min(value, self._safety_limit)
            if isinstance(value, str) and value.isdigit():
                return min(int(value), self._safety_limit)
        if page_size <= 0:
            return None
        if _pagination_has_more(self._next_pagination):
            return self._safety_limit
        return min(len(self._rows), self._safety_limit)

    def _schedule_next_request(self, *, immediate: bool) -> None:
        if immediate:
            self._next_request_at = datetime.now(UTC)
            return
        delay = random.uniform(*BASE_DELAY_RANGE)
        if self._pages_fetched and self._pages_fetched % BURST_PAGE_INTERVAL == 0:
            delay += random.uniform(*BURST_DELAY_RANGE)
        self._next_request_at = datetime.now(UTC) + timedelta(seconds=delay)

    def _pause_with_message(self, message: str) -> None:
        self._paused = True
        self._next_request_at = None
        self._last_message = message

    def _mark_completed(self, message: str) -> None:
        self._completed = True
        self._paused = True
        self._next_request_at = None
        self._last_message = message

    def _flush_backup(self, *, force: bool) -> None:
        if not self._rows or not self._rows_dirty:
            return
        now = datetime.now(UTC)
        if (
            not force
            and self._last_backup_saved_at is not None
            and (now - self._last_backup_saved_at).total_seconds() < BACKUP_INTERVAL_SECONDS
        ):
            return
        backup_root = ensure_creator_collector_dir() / "backups"
        if self._backup_path is None:
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            self._backup_path = backup_root / f"creator_collection_{timestamp}.json"
        save_backup_json(self._rows, self._backup_path)
        self._last_backup_saved_at = now
        self._rows_dirty = False

    def _default_request_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
        }
        if self._page is None:
            return headers
        try:
            headers["referer"] = self._page.url
        except PlaywrightError:
            pass
        try:
            user_agent = self._page.evaluate("() => navigator.userAgent")
            if isinstance(user_agent, str) and user_agent.strip():
                headers["user-agent"] = user_agent
        except PlaywrightError:
            pass
        return headers


def _matches_request_api(url: str) -> bool:
    return urlsplit(url).path.endswith(REQUEST_API_PATH)


def _build_paginated_url(
    request_template_url: str | None,
    pagination: dict[str, object] | None,
) -> str | None:
    if not request_template_url:
        return None
    if not pagination:
        return None
    parsed = urlsplit(request_template_url)
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("next_page", "search_key", "next_item_cursor"):
        value = pagination.get(key)
        if value is None or value == "":
            query_items.pop(key, None)
            continue
        query_items[key] = str(value)
    updated_query = urlencode(query_items)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, updated_query, parsed.fragment))


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.casefold() not in HEADER_BLOCKLIST}


def _pagination_has_more(pagination: dict[str, object] | None) -> bool:
    if not isinstance(pagination, dict):
        return False
    return bool(pagination.get("has_more"))


def _row_identity(row: dict[str, object]) -> str:
    creator_oecuid = _nested_value(row.get("creator_oecuid"))
    handle = _nested_value(row.get("handle"))
    if creator_oecuid:
        return creator_oecuid
    return f"{creator_oecuid}|{handle}"


def _nested_value(raw: object) -> str:
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, dict):
            minimal = value.get("minimal")
            if minimal is not None:
                return str(minimal)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    if raw is None:
        return ""
    return str(raw)
