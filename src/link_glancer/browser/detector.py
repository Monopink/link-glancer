from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BrowserCandidate:
    name: str
    executable_path: Path
    launch_mode: str


def detect_browser(
    browser_name: str, executable_path: str | None = None
) -> BrowserCandidate | None:
    configured = _configured_candidate(executable_path)
    if configured is not None:
        return configured

    return _detect_by_name(browser_name)


def browser_path_presets() -> dict[str, list[str]]:
    return {
        "thorium": [str(path) for path in _thorium_candidate_paths()],
        "chrome": [str(path) for path in _chrome_candidate_paths()],
        "edge": [str(path) for path in _edge_candidate_paths()],
    }


def list_detected_browsers() -> list[BrowserCandidate]:
    candidates: list[BrowserCandidate] = []
    seen_paths: set[str] = set()
    for browser_name, paths in (
        ("thorium", _thorium_candidate_paths()),
        ("chrome", _chrome_candidate_paths()),
        ("edge", _edge_candidate_paths()),
    ):
        for path in _existing_paths(paths):
            normalized_path = str(path)
            if normalized_path in seen_paths:
                continue
            candidates.append(BrowserCandidate(browser_name, path, "chromium_executable"))
            seen_paths.add(normalized_path)
    return candidates


def _detect_by_name(browser_name: str) -> BrowserCandidate | None:
    detectors = {
        "thorium": _detect_thorium,
        "chrome": _detect_chrome,
        "edge": _detect_edge,
    }
    detector = detectors.get(browser_name)
    if detector is None:
        return None
    return detector()


def _detect_thorium() -> BrowserCandidate | None:
    for path in _existing_paths(_thorium_candidate_paths()):
        return BrowserCandidate("thorium", path, "chromium_executable")
    return None


def _detect_chrome() -> BrowserCandidate | None:
    for path in _existing_paths(_chrome_candidate_paths()):
        return BrowserCandidate("chrome", path, "chromium_executable")
    return None


def _detect_edge() -> BrowserCandidate | None:
    for path in _existing_paths(_edge_candidate_paths()):
        return BrowserCandidate("edge", path, "chromium_executable")
    return None


def _existing_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.is_file()]


def _configured_candidate(executable_path: str | None) -> BrowserCandidate | None:
    if not executable_path:
        return None

    path = _resolve_configured_path(Path(executable_path).expanduser())
    if not path.is_file():
        return None

    browser_name = _infer_browser_name(path)
    return BrowserCandidate(browser_name, path, "chromium_executable")


def _infer_browser_name(path: Path) -> str:
    lowered = str(path).lower()
    if "thorium" in lowered:
        return "thorium"
    if "msedge" in lowered or "edge" in lowered:
        return "edge"
    if "chrome" in lowered:
        return "chrome"
    return path.stem.lower()


def _resolve_configured_path(path: Path) -> Path:
    if path.is_file():
        return path
    if path.is_dir() and path.suffix.lower() == ".app":
        macos_dir = path / "Contents" / "MacOS"
        if macos_dir.is_dir():
            executables = [child for child in macos_dir.iterdir() if child.is_file()]
            if executables:
                return executables[0]
    return path


def _thorium_candidate_paths() -> list[Path]:
    if sys.platform == "darwin":
        return [
            _mac_app_executable("Thorium.app", "Thorium"),
            _mac_app_executable("Thorium.app", "Chromium"),
        ]
    return [
        _local_app("Thorium", "Application", "thorium.exe"),
        _local_app("Thorium", "Application", "chrome.exe"),
        _program_files("Thorium", "Application", "thorium.exe"),
        _program_files("Thorium", "Application", "chrome.exe"),
        _program_files_x86("Thorium", "Application", "thorium.exe"),
        _program_files_x86("Thorium", "Application", "chrome.exe"),
    ]


def _chrome_candidate_paths() -> list[Path]:
    if sys.platform == "darwin":
        return [
            _mac_app_executable("Google Chrome.app", "Google Chrome"),
            _mac_app_executable("Google Chrome Canary.app", "Google Chrome Canary"),
        ]
    return [
        _local_app("Google", "Chrome", "Application", "chrome.exe"),
        _program_files("Google", "Chrome", "Application", "chrome.exe"),
        _program_files_x86("Google", "Chrome", "Application", "chrome.exe"),
    ]


def _edge_candidate_paths() -> list[Path]:
    if sys.platform == "darwin":
        return [
            _mac_app_executable("Microsoft Edge.app", "Microsoft Edge"),
            _mac_app_executable("Microsoft Edge Beta.app", "Microsoft Edge Beta"),
        ]
    return [
        _program_files("Microsoft", "Edge", "Application", "msedge.exe"),
        _program_files_x86("Microsoft", "Edge", "Application", "msedge.exe"),
    ]


def _local_app(*parts: str) -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), *parts)


def _program_files(*parts: str) -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files"), *parts)


def _program_files_x86(*parts: str) -> Path:
    return Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), *parts)


def _mac_app_executable(app_name: str, executable_name: str) -> Path:
    return Path("/Applications", app_name, "Contents", "MacOS", executable_name)
