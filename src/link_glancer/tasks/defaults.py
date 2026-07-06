from __future__ import annotations

from pathlib import Path

from link_glancer.browser.detector import browser_path_presets
from link_glancer.tasks.models import BrowserConfig


def default_browser_config() -> BrowserConfig:
    path = _first_existing_browser_path()
    return BrowserConfig(
        config_id="default-browser",
        name="Default Browser",
        executable_path=str(path) if path else "",
        test_url="about:blank",
        last_test_status="untested",
    )


def _first_existing_browser_path() -> Path | None:
    for paths in browser_path_presets().values():
        for path in paths:
            candidate = Path(path)
            if candidate.is_file():
                return candidate
    return None
