from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from link_glancer.runtime.paths import ensure_logs_dir

_DEV_MODE_ENABLED = False
_DEV_MODE_ENV_KEY = "LINK_GLANCER_DEV_MODE"


def set_dev_mode(enabled: bool) -> None:
    global _DEV_MODE_ENABLED

    _DEV_MODE_ENABLED = enabled
    if enabled:
        os.environ[_DEV_MODE_ENV_KEY] = "1"
    else:
        os.environ.pop(_DEV_MODE_ENV_KEY, None)


def initialize_dev_mode_from_environment() -> None:
    set_dev_mode(os.environ.get(_DEV_MODE_ENV_KEY) == "1")


def is_dev_mode() -> bool:
    return _DEV_MODE_ENABLED


def dev_mode_title_suffix() -> str:
    return " [开发者模式]" if is_dev_mode() else ""


class JsonlDevLogger:
    def __init__(self, *, module: str, file_stem: str) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        self._base_stem = f"{file_stem}_{timestamp}"
        self._path = ensure_logs_dir() / f"{self._base_stem}.jsonl"
        self._module = module
        self._artifact_dir = ensure_logs_dir() / f"{self._base_stem}_artifacts"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def artifact_dir(self) -> Path:
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._artifact_dir

    def log(self, event: str, **fields: object) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "module": self._module,
            "event": event,
            **fields,
        }
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            return

    def write_text_artifact(
        self,
        *,
        name: str,
        content: str,
        extension: str = "txt",
    ) -> Path | None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_file_component(name)
        safe_extension = _safe_file_component(extension).lstrip(".") or "txt"
        path = self.artifact_dir / f"{timestamp}_{safe_name}.{safe_extension}"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            return None
        return path


def _safe_file_component(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in value).strip("._")
    return safe or "artifact"
