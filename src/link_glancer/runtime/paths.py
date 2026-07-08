from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "LinkGlancer"


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def bundled_asset_path(*relative_parts: str) -> Path:
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        return Path(base_path).resolve().joinpath(*relative_parts)
    return application_root().joinpath(*relative_parts)


def ensure_app_data_root() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            root = Path(appdata) / APP_DIR_NAME
        else:
            root = Path.home() / "AppData" / "Roaming" / APP_DIR_NAME
    return _ensure_writable_root(root)


def ensure_browser_environments_root() -> Path:
    root = ensure_app_data_root() / "browser-environments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_creator_collector_dir() -> Path:
    root = ensure_app_data_root() / "creator-collector"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_runtime_locks_root() -> Path:
    root = ensure_app_data_root() / "runtime-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def app_database_path() -> Path:
    return ensure_app_data_root() / "app.db"


def ensure_browser_environment_dir(browser_config_id: str) -> Path:
    environment_dir = ensure_browser_environments_root() / _safe_dir_name(browser_config_id)
    environment_dir.mkdir(parents=True, exist_ok=True)
    return environment_dir


def _safe_dir_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in value).strip()
    return safe or "browser"


def _ensure_writable_root(primary_root: Path) -> Path:
    try:
        primary_root.mkdir(parents=True, exist_ok=True)
        probe = primary_root / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return primary_root
    except OSError:
        fallback_root = Path.cwd() / ".runtime" / APP_DIR_NAME
        fallback_root.mkdir(parents=True, exist_ok=True)
        return fallback_root
