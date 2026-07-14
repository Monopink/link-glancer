from __future__ import annotations

import json
import queue
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from creator_collector.models import CreatorCollectionConfig
from creator_collector.service import CreatorCollectorSession
from link_glancer.application import TaskApplicationService

STATUS_PUSH_INTERVAL_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.2


class _CollectorWorkerRuntime:
    def __init__(self) -> None:
        self._app_service = TaskApplicationService.create_default()
        self._session: CreatorCollectorSession | None = None
        self._collector_session_id: int | None = None
        self._browser_config_id: str | None = None
        self._page_url = ""
        self._command_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._stop_event = threading.Event()
        self._stdin_thread = threading.Thread(target=self._read_commands, daemon=True)
        self._last_status_payload: dict[str, object] | None = None
        self._last_status_pushed_at = 0.0
        self._saving = False

    def run(self) -> None:
        self._stdin_thread.start()
        try:
            while not self._stop_event.is_set():
                self._process_pending_commands()
                if self._session is not None:
                    self._session.poll()
                    self._persist_session_progress()
                self._push_status_if_needed(force=False)
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            if self._session is not None:
                self._persist_session_progress(force=True)
                self._session.shutdown()
            self._push_message({"type": "worker_stopped"})

    def _read_commands(self) -> None:
        while not self._stop_event.is_set():
            raw = sys.stdin.readline()
            if not raw:
                self._stop_event.set()
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self._push_message({"type": "error", "message": "无效的采集指令。"})
                continue
            if isinstance(payload, dict):
                self._command_queue.put(payload)

    def _process_pending_commands(self) -> None:
        while True:
            try:
                command = self._command_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_command(command)

    def _handle_command(self, command: dict[str, object]) -> None:
        action = str(command.get("cmd") or "").strip()
        if action == "start":
            self._handle_start(command)
            return
        if action == "resume":
            self._call_session(lambda session: session.resume_auto_scroll())
            return
        if action == "pause":
            self._call_session(lambda session: session.pause_auto_scroll())
            return
        if action == "stop":
            self._call_session(lambda session: session.stop())
            return
        if action == "update_limit":
            value = int(command.get("value") or 0)
            self._call_session(lambda session: session.update_safety_limit(value))
            return
        if action == "update_interval":
            value = float(command.get("value") or 0)
            self._call_session(lambda session: session.update_auto_advance_interval(value))
            return
        if action == "checkpoint_save":
            self._handle_checkpoint_save(command)
            return
        if action == "finalize_save_and_shutdown":
            self._handle_finalize_save_and_shutdown(command)
            return
        if action == "clear_batch":
            self._call_session(lambda session: session.clear_collected_batch())
            return
        if action == "status":
            self._push_status_if_needed(force=True)
            return
        if action == "shutdown":
            self._stop_event.set()
            return
        self._push_message({"type": "error", "message": f"未知采集指令：{action}"})

    def _handle_start(self, command: dict[str, object]) -> None:
        browser_config_id = str(command.get("browser_config_id") or "").strip()
        if not browser_config_id:
            self._push_message({"type": "error", "message": "缺少浏览器配置。"})
            return
        self._shutdown_session()
        try:
            browser_config = self._app_service.load_browser_config(browser_config_id)
        except Exception as exc:  # noqa: BLE001
            self._push_message({"type": "error", "message": str(exc)})
            return
        config = CreatorCollectionConfig(
            browser_config_id=browser_config_id,
            page_url=str(command.get("page_url") or "").strip(),
            safety_limit=max(int(command.get("safety_limit") or 1), 1),
            auto_advance_interval_seconds=float(
                command.get("auto_advance_interval_seconds") or 1.5
            ),
        )
        paused = bool(command.get("paused", False))
        self._page_url = config.page_url
        self._collector_session_id = self._app_service.create_creator_collection_session(
            browser_config_id=browser_config_id,
            page_url=config.page_url,
            safety_limit=config.safety_limit,
            auto_advance_interval_seconds=config.auto_advance_interval_seconds,
            last_message="采集会话已创建。",
        )
        self._session = CreatorCollectorSession(browser_config)
        self._browser_config_id = browser_config_id
        status = self._session.start(config, paused=paused)
        self._persist_session_progress(force=True)
        self._push_status_if_needed(force=True)
        if not status.running:
            self._push_message({"type": "error", "message": status.last_message})

    def _handle_checkpoint_save(self, command: dict[str, object]) -> None:
        if self._session is None:
            self._push_message({"type": "error", "message": "采集会话未启动。"})
            return
        export_path_raw = str(command.get("export_path") or "").strip()
        if not export_path_raw:
            self._push_message({"type": "error", "message": "缺少导出路径。"})
            return
        if not self._browser_config_id:
            self._push_message({"type": "error", "message": "缺少浏览器配置。"})
            return
        if self._collector_session_id is None:
            self._push_message({"type": "error", "message": "缺少采集会话。"})
            return
        self._persist_session_progress(force=True)
        status = self._session.status()
        if status.collected_count <= 0:
            self._push_message({"type": "error", "message": "当前没有可保存的采集数据。"})
            return
        _require_safe_text(export_path_raw, field_name="导出路径")
        export_path = Path(export_path_raw)
        self._saving = True
        self._push_status_if_needed(force=True)
        try:
            task_id = self._app_service.create_task_from_creator_collection_session(
                session_id=self._collector_session_id,
                export_path=export_path,
            )
            saved_path = export_path
            session_status = self._session.status()
            self._session.clear_collected_batch()
            self._collector_session_id = self._app_service.create_creator_collection_session(
                browser_config_id=self._browser_config_id,
                page_url=self._page_url,
                safety_limit=session_status.safety_limit,
                auto_advance_interval_seconds=session_status.auto_advance_interval_seconds,
                last_message="已保存并创建任务，可继续采集。",
            )
            self._persist_session_progress(force=True)
        except Exception as exc:  # noqa: BLE001
            self._saving = False
            self._push_message({"type": "error", "message": str(exc)})
            self._push_status_if_needed(force=True)
            return
        self._saving = False
        self._push_message(
            {
                "type": "checkpoint_saved",
                "task_id": task_id,
                "saved_path": str(saved_path),
            }
        )
        self._push_status_if_needed(force=True)

    def _handle_finalize_save_and_shutdown(self, command: dict[str, object]) -> None:
        if self._session is None:
            self._push_message({"type": "error", "message": "采集会话未启动。"})
            return
        export_path_raw = str(command.get("export_path") or "").strip()
        if not export_path_raw:
            self._push_message({"type": "error", "message": "缺少导出路径。"})
            return
        if not self._browser_config_id:
            self._push_message({"type": "error", "message": "缺少浏览器配置。"})
            return
        if self._collector_session_id is None:
            self._push_message({"type": "error", "message": "缺少采集会话。"})
            return
        self._persist_session_progress(force=True)
        status = self._session.status()
        if status.collected_count <= 0:
            self._push_message({"type": "error", "message": "当前没有可保存的采集数据。"})
            return
        _require_safe_text(export_path_raw, field_name="导出路径")
        export_path = Path(export_path_raw)
        self._saving = True
        self._push_status_if_needed(force=True)
        try:
            task_id = self._app_service.create_task_from_creator_collection_session(
                session_id=self._collector_session_id,
                export_path=export_path,
            )
            saved_path = export_path
            self._session.shutdown()
            self._session = None
            self._collector_session_id = None
            self._browser_config_id = None
        except Exception as exc:  # noqa: BLE001
            self._saving = False
            self._push_message({"type": "error", "message": str(exc)})
            self._push_status_if_needed(force=True)
            return
        self._saving = False
        self._push_message(
            {
                "type": "finalized_and_saved",
                "task_id": task_id,
                "saved_path": str(saved_path),
            }
        )
        self._push_status_if_needed(force=True)
        self._stop_event.set()

    def _call_session(self, callback) -> None:
        if self._session is None:
            self._push_message({"type": "error", "message": "采集会话未启动。"})
            return
        callback(self._session)
        self._persist_session_progress(force=True)
        self._push_status_if_needed(force=True)

    def _shutdown_session(self) -> None:
        self._persist_session_progress(force=True)
        if self._session is not None:
            self._session.shutdown()
        self._session = None
        self._collector_session_id = None
        self._browser_config_id = None
        self._page_url = ""
        self._saving = False

    def _persist_session_progress(self, *, force: bool = False) -> None:
        if self._session is None or self._collector_session_id is None:
            return
        pending_rows = self._session.drain_pending_persist_rows()
        if pending_rows:
            self._app_service.append_creator_collection_session_rows(
                session_id=self._collector_session_id,
                rows=pending_rows,
            )
        if pending_rows or force:
            status = self._session.status()
            self._app_service.update_creator_collection_session(
                session_id=self._collector_session_id,
                status=self._session.collection_state(),
                collected_count=status.collected_count,
                pages_fetched=status.pages_fetched,
                safety_limit=status.safety_limit,
                auto_advance_interval_seconds=status.auto_advance_interval_seconds,
                last_message=status.last_message,
            )

    def _push_status_if_needed(self, *, force: bool) -> None:
        payload = self._build_status_payload()
        now = time.monotonic()
        if not force and payload == self._last_status_payload:
            if now - self._last_status_pushed_at < STATUS_PUSH_INTERVAL_SECONDS:
                return
        self._last_status_payload = payload
        self._last_status_pushed_at = now
        self._push_message({"type": "status", "status": payload})

    def _build_status_payload(self) -> dict[str, object]:
        if self._session is None:
            return {
                "running": False,
                "auto_scroll_enabled": False,
                "interrupted": False,
                "completed": False,
                "collected_count": 0,
                "pages_fetched": 0,
                "safety_limit": 0,
                "auto_advance_interval_seconds": 0.0,
                "last_message": "采集会话未启动。",
                "started_at": None,
                "ended_at": None,
                "estimated_total_count": None,
                "estimated_end_at": None,
                "saving": self._saving,
            }
        status = self._session.status()
        return {
            "running": status.running,
            "auto_scroll_enabled": status.auto_scroll_enabled,
            "interrupted": status.interrupted,
            "completed": status.completed,
            "collected_count": status.collected_count,
            "pages_fetched": status.pages_fetched,
            "safety_limit": status.safety_limit,
            "auto_advance_interval_seconds": status.auto_advance_interval_seconds,
            "last_message": status.last_message,
            "started_at": _serialize_datetime(status.started_at),
            "ended_at": _serialize_datetime(status.ended_at),
            "estimated_total_count": status.estimated_total_count,
            "estimated_end_at": _serialize_datetime(status.estimated_end_at),
            "saving": self._saving,
        }

    def _push_message(self, payload: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(_sanitize_payload(payload), ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _configure_stdio_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            errors = "strict" if stream_name == "stdin" else "backslashreplace"
            reconfigure(encoding="utf-8", errors=errors)


def _contains_surrogates(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _require_safe_text(value: str, *, field_name: str) -> None:
    if _contains_surrogates(value):
        raise ValueError(f"{field_name}包含无法编码的字符，请更换导出位置或文件名后重试。")


def _sanitize_text(value: str) -> str:
    if not _contains_surrogates(value):
        return value
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _sanitize_payload(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def run_worker() -> None:
    _configure_stdio_encoding()
    runtime = _CollectorWorkerRuntime()
    runtime.run()
