from __future__ import annotations

from link_glancer.tasks.models import (
    BrowserConfig,
    ReviewField,
    ReviewOption,
    ReviewShortcutConfig,
    TaskSnapshot,
)


def task_snapshot_to_dict(snapshot: TaskSnapshot) -> dict[str, object]:
    return {
        "sheet_name": snapshot.sheet_name,
        "header_row": snapshot.header_row,
        "browser_config_id": snapshot.browser_config_id,
        "open_tab_count": snapshot.open_tab_count,
        "confirm_url": snapshot.confirm_url or "",
        "url_field": snapshot.url_field,
        "display_fields": snapshot.display_fields,
        "review_fields": [review_field_to_dict(field) for field in snapshot.review_fields],
        "shortcuts": {
            "submit": snapshot.shortcuts.submit,
            "previous": snapshot.shortcuts.previous,
            "exit": snapshot.shortcuts.exit,
        },
        "export_fields": snapshot.export_fields,
    }


def task_snapshot_from_dict(data: dict[str, object]) -> TaskSnapshot:
    shortcuts = data.get("shortcuts", {})
    if not isinstance(shortcuts, dict):
        raise ValueError("Task snapshot shortcuts must be an object.")
    return TaskSnapshot(
        sheet_name=str(data["sheet_name"]),
        header_row=int(data["header_row"]),
        browser_config_id=str(data["browser_config_id"]),
        open_tab_count=int(data["open_tab_count"]),
        confirm_url=str(data.get("confirm_url") or "") or None,
        url_field=str(data["url_field"]),
        display_fields=[str(item) for item in data.get("display_fields", [])],
        review_fields=[
            review_field_from_dict(item) for item in _dict_list(data.get("review_fields", []))
        ],
        shortcuts=ReviewShortcutConfig(
            submit=str(shortcuts.get("submit", "Enter")),
            previous=str(shortcuts.get("previous", "Backspace")),
            exit=str(shortcuts.get("exit", "Esc")),
        ),
        export_fields=[str(item) for item in data.get("export_fields", [])],
    )


def browser_config_to_dict(config: BrowserConfig) -> dict[str, object]:
    return {
        "id": config.config_id,
        "name": config.name,
        "executable_path": config.executable_path,
        "launch_args": config.launch_args,
        "test_url": config.test_url,
        "last_tested_at": config.last_tested_at or "",
        "last_test_status": config.last_test_status,
    }


def browser_config_from_dict(data: dict[str, object]) -> BrowserConfig:
    launch_args = data.get("launch_args", [])
    return BrowserConfig(
        config_id=str(data["id"]),
        name=str(data["name"]),
        executable_path=str(data.get("executable_path", "")),
        launch_args=[str(item) for item in launch_args] if isinstance(launch_args, list) else [],
        test_url=str(data.get("test_url", "about:blank")),
        last_tested_at=str(data.get("last_tested_at") or "") or None,
        last_test_status=str(data.get("last_test_status", "untested")),
    )


def review_field_to_dict(field: ReviewField) -> dict[str, object]:
    return {
        "field": field.field_id,
        "label": field.label,
        "type": field.field_type,
        "required": field.required,
        "options": [
            {
                "value": option.value,
                "label": option.label,
                "shortcut": option.shortcut,
            }
            for option in field.options
        ],
    }


def review_field_from_dict(data: dict[str, object]) -> ReviewField:
    return ReviewField(
        field_id=str(data.get("field", data.get("id", ""))),
        label=str(data["label"]),
        field_type=str(data["type"]),  # type: ignore[arg-type]
        required=bool(data.get("required", False)),
        options=[
            ReviewOption(
                value=str(option["value"]),
                label=str(option["label"]),
                shortcut=str(option["shortcut"]) if option.get("shortcut") else None,
            )
            for option in _dict_list(data.get("options", []))
        ],
    )


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
