from __future__ import annotations

import json
import queue
import sys
import threading
import time
import traceback
from datetime import UTC, datetime

from creator_enrichment.constants import POLL_INTERVAL_SECONDS, STATUS_PUSH_INTERVAL_SECONDS
from creator_enrichment.service import CreatorEnrichmentSession
from creator_enrichment.state import setting_key
from link_glancer.application import TaskApplicationService


class _EnrichmentWorkerRuntime:
    def __init__(self) -> None:
        self._app_service = TaskApplicationService.create_default()
        self._session: CreatorEnrichmentSession | None = None
        self._command_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._stop_event = threading.Event()
        self._stdin_thread = threading.Thread(target=self._read_commands, daemon=True)
        self._last_status_payload: dict[str, object] | None = None
        self._last_status_pushed_at = 0.0

    def run(self) -> None:
        self._stdin_thread.start()
        try:
            while not self._stop_event.is_set():
                try:
                    self._process_pending_commands()
                    if self._session is not None:
                        self._session.poll()
                except Exception as exc:  # noqa: BLE001
                    self._push_message(
                        {
                            "type": "error",
                            "message": f"补充采集 worker 异常：{exc}",
                            "details": traceback.format_exc(),
                        }
                    )
                    self._stop_event.set()
                    break
                self._push_status_if_needed(force=False)
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            if self._session is not None:
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
                self._push_message({"type": "error", "message": "无效的补充采集指令。"})
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
            self._call_session(lambda session: session.resume())
            return
        if action == "pause":
            self._call_session(lambda session: session.stop())
            return
        if action == "retry":
            self._call_session(lambda session: session.retry_current())
            return
        if action == "skip":
            self._call_session(lambda session: session.skip_current())
            return
        if action == "status":
            self._push_status_if_needed(force=True)
            return
        if action == "shutdown":
            self._stop_event.set()
            return
        self._push_message({"type": "error", "message": f"未知补充采集指令：{action}"})

    def _handle_start(self, command: dict[str, object]) -> None:
        task_id = int(command.get("task_id") or 0)
        if task_id <= 0:
            self._push_message({"type": "error", "message": "缺少任务 ID。"})
            return
        try:
            task = self._app_service.load_task(task_id)
        except Exception as exc:  # noqa: BLE001
            self._push_message({"type": "error", "message": str(exc)})
            return
        self._shutdown_session()
        self._session = CreatorEnrichmentSession(
            app_service=self._app_service,
            task_id=task_id,
            browser_config=task.browser_config,
            state_key=setting_key(task_id),
        )
        status = self._session.start()
        self._push_status_if_needed(force=True)
        if not status.running:
            self._push_message({"type": "error", "message": status.last_message})

    def _call_session(self, callback) -> None:
        if self._session is None:
            self._push_message({"type": "error", "message": "补充采集会话未启动。"})
            return
        try:
            callback(self._session)
        except Exception as exc:  # noqa: BLE001
            self._push_message(
                {
                    "type": "error",
                    "message": f"补充采集操作失败：{exc}",
                    "details": traceback.format_exc(),
                }
            )
            self._stop_event.set()
            return
        self._push_status_if_needed(force=True)

    def _shutdown_session(self) -> None:
        if self._session is not None:
            self._session.shutdown()
        self._session = None

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
                "paused": True,
                "completed": False,
                "total_count": 0,
                "completed_count": 0,
                "success_count": 0,
                "no_contact_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "current_task_index": None,
                "current_region": None,
                "remaining_regions": [],
                "last_message": "补充采集会话未启动。",
                "pause_reason": None,
                "diagnostic_summary": None,
                "diagnostic_text": None,
                "failure_attempts": [],
                "attention_required": False,
                "started_at": None,
                "estimated_end_at": None,
            }
        status = self._session.status()
        return {
            "running": status.running,
            "paused": status.paused,
            "completed": status.completed,
            "total_count": status.total_count,
            "completed_count": status.completed_count,
            "success_count": status.success_count,
            "no_contact_count": status.no_contact_count,
            "skipped_count": status.skipped_count,
            "failed_count": status.failed_count,
            "current_task_index": status.current_task_index,
            "current_region": status.current_region,
            "remaining_regions": status.remaining_regions,
            "last_message": status.last_message,
            "pause_reason": status.pause_reason,
            "diagnostic_summary": status.diagnostic_summary,
            "diagnostic_text": status.diagnostic_text,
            "failure_attempts": [
                {
                    "index": attempt.index,
                    "summary": attempt.summary,
                    "diagnostic_text": attempt.diagnostic_text,
                }
                for attempt in (status.failure_attempts or [])
            ],
            "attention_required": status.attention_required,
            "started_at": _serialize_datetime(status.started_at),
            "estimated_end_at": _serialize_datetime(status.estimated_end_at),
        }

    def _push_message(self, payload: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
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


def run_worker() -> None:
    _configure_stdio_encoding()
    runtime = _EnrichmentWorkerRuntime()
    runtime.run()
