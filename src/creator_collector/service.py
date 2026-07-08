from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, Response, sync_playwright

from creator_collector.exporter import (
    dedupe_creator_rows,
    export_collection_to_path,
    export_collection_to_xlsx,
    save_backup_json,
)
from creator_collector.models import CreatorCollectionConfig, CreatorCollectionStatus
from link_glancer.browser.detector import detect_browser
from link_glancer.runtime.paths import ensure_browser_environment_dir, ensure_creator_collector_dir
from link_glancer.tasks.models import BrowserConfig

REQUEST_API_PATH = "/api/v1/oec/affiliate/creator/marketplace/find"
DEFAULT_SAFETY_LIMIT = 1000
MAX_REPEAT_PAGES = 2
BACKUP_INTERVAL_SECONDS = 5
RESPONSE_WAIT_SECONDS = 8
AUTO_ADVANCE_INTERVAL_SECONDS = 1.5
MIN_AUTO_ADVANCE_INTERVAL_SECONDS = 0.3
MAX_AUTO_ADVANCE_INTERVAL_SECONDS = 5.0
PLAYWRIGHT_ALLOWED_DEFAULT_ARGS = [
    "--no-sandbox",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
]


@dataclass(slots=True)
class _CapturedResponse:
    capture_id: int
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
        self._auto_scroll_enabled = False
        self._interrupted = False
        self._completed = False
        self._last_message = "未启动"
        self._backup_path: Path | None = None
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None
        self._estimated_total_count: int | None = None
        self._last_backup_saved_at: datetime | None = None
        self._rows_dirty = False
        self._row_keys: set[str] = set()
        self._captured_responses: list[_CapturedResponse] = []
        self._next_capture_id = 0
        self._last_processed_capture_id = 0
        self._waiting_for_response = False
        self._waiting_started_at: datetime | None = None
        self._next_action_at: datetime | None = None
        self._pages_fetched = 0
        self._repeat_page_hits = 0
        self._safety_limit = DEFAULT_SAFETY_LIMIT
        self._auto_advance_interval_seconds = AUTO_ADVANCE_INTERVAL_SECONDS

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
            self._next_capture_id = 0
            self._last_processed_capture_id = 0
            self._waiting_for_response = False
            self._waiting_started_at = None
            self._next_action_at = None
            self._pages_fetched = 0
            self._repeat_page_hits = 0
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
            self._page.goto(config.page_url, wait_until="domcontentloaded")
            self._page.bring_to_front()
            self._config = config
            self._auto_scroll_enabled = not paused
            self._interrupted = False
            self._completed = False
            self._started_at = datetime.now(UTC)
            self._ended_at = None
            self._estimated_total_count = None
            self._last_backup_saved_at = None
            self._rows_dirty = False
            self._safety_limit = max(config.safety_limit, 1)
            self._auto_advance_interval_seconds = _normalize_auto_advance_interval(
                config.auto_advance_interval_seconds
            )
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
        if len(self._rows) >= self._safety_limit and not self._interrupted:
            self._interrupt_with_message(
                f"已达到上限 {self._safety_limit} 条，可调整上限后继续采集。"
            )
            return self.status()

        if self._consume_pending_responses():
            self._flush_backup(force=False)
            return self.status()

        if self._completed or self._interrupted or not self._auto_scroll_enabled:
            return self.status()

        now = datetime.now(UTC)
        if self._waiting_for_response:
            if self._waiting_started_at is None:
                self._waiting_started_at = now
            elif (now - self._waiting_started_at).total_seconds() >= RESPONSE_WAIT_SECONDS:
                self._interrupt_with_message(
                    "未捕获到新的列表响应，请在页面中滚动或切换筛选后重试。"
                )
            elif self._next_action_at is not None and now >= self._next_action_at:
                self._trigger_page_progress()
            return self.status()

        if self._next_action_at is None or now >= self._next_action_at:
            self._trigger_page_progress()
        return self.status()

    def resume(self) -> CreatorCollectionStatus:
        return self.resume_auto_scroll()

    def resume_auto_scroll(self) -> CreatorCollectionStatus:
        if self._context is None:
            self._last_message = "采集会话未启动。"
            return self.status()
        if len(self._rows) >= self._safety_limit:
            self._interrupt_with_message(
                f"当前已采集 {len(self._rows)} 条，已达到上限 {self._safety_limit} 条。"
                "请先调高上限后再继续。"
            )
            return self.status()
        self._auto_scroll_enabled = True
        self._interrupted = False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = datetime.now(UTC)
        if self._completed:
            self._last_message = "采集已完成，可导出或创建任务。"
            return self.status()
        if self._consume_pending_responses():
            return self.status()
        self._last_message = "正在继续采集。"
        return self.status()

    def pause_auto_scroll(self) -> CreatorCollectionStatus:
        if self._context is None:
            self._last_message = "采集会话未启动。"
            return self.status()
        if self._completed:
            self._last_message = "采集已完成，可导出或创建任务。"
            return self.status()
        self._auto_scroll_enabled = False
        self._interrupted = False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = None
        self._last_message = "已暂停自动滚动，仍会继续监听页面响应。"
        return self.status()

    def stop(self) -> CreatorCollectionStatus:
        self._flush_backup(force=True)
        if self._started_at is not None and self._ended_at is None:
            self._ended_at = datetime.now(UTC)
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

    def finish_to_path(self, export_path: Path) -> Path:
        if not self._rows:
            raise ValueError("当前没有可导出的采集数据。")
        self._rows = dedupe_creator_rows(self._rows)
        self._row_keys = {_row_identity(row) for row in self._rows}
        self._flush_backup(force=True)
        saved_path = export_collection_to_path(self._rows, export_path)
        self._last_message = f"已导出到 {saved_path}"
        return saved_path

    def clear_collected_batch(self) -> CreatorCollectionStatus:
        self._rows.clear()
        self._completed = False
        self._interrupted = False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = None
        self._pages_fetched = 0
        self._repeat_page_hits = 0
        self._estimated_total_count = None
        self._backup_path = None
        self._last_backup_saved_at = None
        self._rows_dirty = False
        self._started_at = datetime.now(UTC) if self._context is not None else None
        self._ended_at = None
        self._auto_scroll_enabled = False
        self._last_message = "已保存并创建任务，可继续采集。"
        return self.status()

    def update_safety_limit(self, value: int) -> CreatorCollectionStatus:
        self._safety_limit = max(value, 1)
        if self._config is not None:
            self._config.safety_limit = self._safety_limit
        if len(self._rows) >= self._safety_limit and not self._completed:
            self._interrupt_with_message(
                f"已达到上限 {self._safety_limit} 条，可调整上限后继续采集。"
            )
        return self.status()

    def update_auto_advance_interval(self, value: float) -> CreatorCollectionStatus:
        self._auto_advance_interval_seconds = _normalize_auto_advance_interval(value)
        if self._config is not None:
            self._config.auto_advance_interval_seconds = self._auto_advance_interval_seconds
        if self._auto_scroll_enabled and not self._completed and self._context is not None:
            self._next_action_at = datetime.now(UTC)
        self._last_message = f"已调整自动滚动间隔为 {self._auto_advance_interval_seconds:.1f} 秒。"
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
            reference_time = self._ended_at or datetime.now(UTC)
            elapsed_seconds = max(
                int((reference_time - self._started_at).total_seconds()),
                1,
            )
            total_seconds = int(elapsed_seconds * estimated_total_count / len(self._rows))
            estimated_end_at = self._started_at + timedelta(seconds=total_seconds)
        return CreatorCollectionStatus(
            running=self._context is not None,
            auto_scroll_enabled=self._auto_scroll_enabled,
            interrupted=self._interrupted,
            completed=self._completed,
            collected_count=len(self._rows),
            pages_fetched=self._pages_fetched,
            safety_limit=self._safety_limit,
            auto_advance_interval_seconds=self._auto_advance_interval_seconds,
            last_message=self._last_message,
            rows=list(self._rows),
            started_at=self._started_at,
            ended_at=self._ended_at,
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
        self._auto_scroll_enabled = False
        self._interrupted = False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = None
        if clear_collection_state:
            self._completed = False
            self._started_at = None
            self._ended_at = None
            self._estimated_total_count = None
            self._last_backup_saved_at = None
            self._rows_dirty = False
            self._row_keys.clear()
            self._rows.clear()
            self._captured_responses.clear()
            self._next_capture_id = 0
            self._last_processed_capture_id = 0
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
        except (PlaywrightError, ValueError):
            return
        self._next_capture_id += 1
        self._captured_responses.append(
            _CapturedResponse(
                capture_id=self._next_capture_id,
                payload=payload,
            )
        )
        if len(self._captured_responses) > 5:
            self._captured_responses = self._captured_responses[-5:]

    def _consume_pending_responses(self) -> bool:
        pending = [
            item
            for item in self._captured_responses
            if item.capture_id > self._last_processed_capture_id
        ]
        if not pending:
            return False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = datetime.now(UTC) + timedelta(
            seconds=self._auto_advance_interval_seconds
        )
        for captured in pending:
            self._last_processed_capture_id = captured.capture_id
            self._apply_payload(captured.payload, source="页面响应")
            if self._completed:
                break
        return True

    def _trigger_page_progress(self) -> None:
        if self._page is None:
            self._interrupt_with_message("浏览器页面不可用，请重新打开采集。")
            return
        try:
            self._page.evaluate(
                """
                () => {
                    const candidates = Array.from(document.querySelectorAll("*"));
                    let target = null;
                    let bestScore = -1;
                    for (const element of candidates) {
                        if (!(element instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(element);
                        const overflowY = style.overflowY;
                        const scrollable =
                            (overflowY === "auto" || overflowY === "scroll") &&
                            element.scrollHeight > element.clientHeight + 40;
                        if (!scrollable) continue;
                        const rect = element.getBoundingClientRect();
                        const score = rect.width * rect.height;
                        if (score > bestScore) {
                            bestScore = score;
                            target = element;
                        }
                    }
                    if (target) {
                        target.scrollTop = target.scrollHeight;
                        target.dispatchEvent(new Event("scroll", { bubbles: true }));
                        return "container";
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                    return "window";
                }
                """
            )
            self._page.keyboard.press("End")
            self._page.keyboard.press("PageDown")
            self._page.mouse.wheel(0, 2400)
        except PlaywrightError:
            self._interrupt_with_message("无法触发页面加载，请处理页面后重试。")
            return
        self._waiting_for_response = True
        if self._waiting_started_at is None:
            self._waiting_started_at = datetime.now(UTC)
        self._next_action_at = datetime.now(UTC) + timedelta(
            seconds=self._auto_advance_interval_seconds
        )
        self._last_message = "正在采集。"

    def _apply_payload(self, payload: dict[str, object], *, source: str) -> None:
        rows = payload.get("creator_profile_list")
        if not isinstance(rows, list):
            self._interrupt_with_message(f"{source}缺少列表数据，请处理后点继续。")
            return

        added_count = 0
        for item in rows:
            if not isinstance(item, dict):
                continue
            row_key = _row_identity(item)
            if row_key in self._row_keys:
                continue
            self._row_keys.add(row_key)
            self._rows.append(item)
            added_count += 1

        self._pages_fetched += 1
        next_pagination = payload.get("next_pagination")
        self._estimated_total_count = self._estimate_total_count(
            payload,
            len(rows),
            next_pagination,
        )

        if rows and added_count == 0:
            self._repeat_page_hits += 1
        else:
            self._repeat_page_hits = 0

        if added_count > 0:
            self._rows_dirty = True

        if not rows:
            self._interrupt_with_message("分页返回空数据，请处理后点继续。")
            return
        if self._repeat_page_hits >= MAX_REPEAT_PAGES:
            self._mark_completed("检测到重复页，已停止继续翻页。")
            return
        if len(self._rows) >= self._safety_limit:
            self._interrupt_with_message(
                f"已达到上限 {self._safety_limit} 条，当前页数据已完整保留。可调整上限后继续采集。"
            )
            return
        if not _pagination_has_more(next_pagination):
            self._mark_completed("采集完成，可导出或创建任务。")
            return

        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = datetime.now(UTC) + timedelta(
            seconds=self._auto_advance_interval_seconds
        )
        self._last_message = f"正在采集，已采集 {len(self._rows)} 条。"

    def _estimate_total_count(
        self,
        payload: dict[str, object],
        page_size: int,
        pagination: dict[str, object] | None,
    ) -> int | None:
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
        if _pagination_has_more(pagination):
            return self._safety_limit
        return min(len(self._rows), self._safety_limit)

    def _interrupt_with_message(self, message: str) -> None:
        self._auto_scroll_enabled = False
        self._interrupted = True
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = None
        self._ended_at = None
        self._last_message = message

    def _mark_completed(self, message: str) -> None:
        self._completed = True
        self._auto_scroll_enabled = False
        self._interrupted = False
        self._waiting_for_response = False
        self._waiting_started_at = None
        self._next_action_at = None
        if self._ended_at is None:
            self._ended_at = datetime.now(UTC)
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


def _matches_request_api(url: str) -> bool:
    return urlsplit(url).path.endswith(REQUEST_API_PATH)


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


def _normalize_auto_advance_interval(value: float) -> float:
    return min(
        max(float(value), MIN_AUTO_ADVANCE_INTERVAL_SECONDS),
        MAX_AUTO_ADVANCE_INTERVAL_SECONDS,
    )
