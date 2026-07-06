from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from link_glancer.runtime.paths import app_database_path
from link_glancer.tasks.defaults import default_browser_config
from link_glancer.tasks.models import (
    BrowserConfig,
    ReviewRecord,
    TaskDetail,
    TaskItem,
    TaskSnapshot,
    TaskStatus,
    TaskSummary,
)
from link_glancer.tasks.serialization import (
    browser_config_from_dict,
    browser_config_to_dict,
    task_snapshot_from_dict,
    task_snapshot_to_dict,
)

CURRENT_SCHEMA_VERSION = 4
_LAST_DATABASE_RESET_REASON: str | None = None

_REQUIRED_SCHEMA_COLUMNS = {
    "browser_configs": {
        "id",
        "name",
        "executable_path",
        "launch_args_json",
        "test_url",
        "last_tested_at",
        "last_test_status",
        "created_at",
        "updated_at",
    },
    "tasks": {
        "id",
        "name",
        "source_file_path",
        "source_file_name",
        "source_file_size",
        "source_file_mtime",
        "source_file_hash",
        "browser_config_id",
        "task_snapshot_json",
        "browser_config_snapshot_json",
        "status",
        "current_task_index",
        "created_at",
        "updated_at",
    },
    "task_items": {"id", "task_id", "task_index", "source_row", "task_data_json", "created_at"},
    "reviews": {"task_item_id", "review_data_json", "review_status", "reviewed_at", "updated_at"},
    "review_history": {"id", "task_item_id", "action", "payload_json", "created_at"},
    "app_settings": {"key", "value_json", "updated_at"},
}


def ensure_app_database() -> Path:
    global _LAST_DATABASE_RESET_REASON

    database_path = app_database_path()
    reset_reason = _database_reset_reason(database_path)
    if reset_reason is not None:
        try:
            database_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                "检测到不兼容的旧数据库，但无法重建数据库文件。"
                "请关闭正在运行的 Link Glancer 或占用 app.db 的程序后重试。"
            ) from exc
        _LAST_DATABASE_RESET_REASON = reset_reason

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS browser_configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                executable_path TEXT NOT NULL,
                launch_args_json TEXT NOT NULL,
                test_url TEXT NOT NULL,
                last_tested_at TEXT,
                last_test_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                source_file_path TEXT NOT NULL,
                source_file_name TEXT NOT NULL,
                source_file_size INTEGER,
                source_file_mtime TEXT,
                source_file_hash TEXT,
                browser_config_id TEXT NOT NULL,
                task_snapshot_json TEXT NOT NULL,
                browser_config_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                current_task_index INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_items (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                task_index INTEGER NOT NULL,
                source_row INTEGER NOT NULL,
                task_data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, task_index),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reviews (
                task_item_id INTEGER PRIMARY KEY,
                review_data_json TEXT NOT NULL,
                review_status TEXT NOT NULL,
                reviewed_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_item_id) REFERENCES task_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY,
                task_item_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _seed_defaults(connection)
        _set_app_setting(connection, "schema_version", CURRENT_SCHEMA_VERSION)
    return database_path


def consume_database_reset_reason() -> str | None:
    global _LAST_DATABASE_RESET_REASON

    reason = _LAST_DATABASE_RESET_REASON
    _LAST_DATABASE_RESET_REASON = None
    return reason


def create_task(
    database_path: Path,
    *,
    name: str,
    source_file_path: Path,
    task_snapshot: TaskSnapshot,
    browser_config: BrowserConfig,
    rows: list[tuple[int, dict[str, object]]],
    source_file_hash: str | None = None,
) -> int:
    now = _now_iso()
    stat = source_file_path.stat()
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO tasks (
                name, source_file_path, source_file_name, source_file_size,
                source_file_mtime, source_file_hash, browser_config_id, task_snapshot_json,
                browser_config_snapshot_json, status, current_task_index, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(source_file_path),
                source_file_path.name,
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                source_file_hash,
                browser_config.config_id,
                _json(task_snapshot_to_dict(task_snapshot)),
                _json(browser_config_to_dict(browser_config)),
                "ready",
                1,
                now,
                now,
            ),
        )
        task_id = int(cursor.lastrowid)
        connection.executemany(
            """
            INSERT INTO task_items (task_id, task_index, source_row, task_data_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (task_id, index, source_row, _json(task_data), now)
                for index, (source_row, task_data) in enumerate(rows, start=1)
            ],
        )
        _set_app_setting(connection, "last_task_id", task_id)
        return task_id


def list_task_summaries(database_path: Path) -> list[TaskSummary]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                t.id, t.name, t.source_file_path, t.source_file_name, t.status,
                t.current_task_index, t.updated_at,
                COUNT(i.id) AS total_items,
                COUNT(CASE WHEN r.review_status = 'completed' THEN 1 END) AS completed_items
            FROM tasks t
            LEFT JOIN task_items i ON i.task_id = t.id
            LEFT JOIN reviews r ON r.task_item_id = i.id
            GROUP BY t.id
            ORDER BY t.updated_at DESC, t.id DESC
            """
        ).fetchall()
        return [
            TaskSummary(
                task_id=int(row["id"]),
                name=str(row["name"]),
                source_file_path=Path(str(row["source_file_path"])),
                source_file_name=str(row["source_file_name"]),
                status=str(row["status"]),  # type: ignore[arg-type]
                current_task_index=int(row["current_task_index"]),
                total_items=int(row["total_items"]),
                completed_items=int(row["completed_items"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]


def load_task_detail(database_path: Path, task_id: int) -> TaskDetail:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                t.*,
                COUNT(i.id) AS total_items,
                COUNT(CASE WHEN r.review_status = 'completed' THEN 1 END) AS completed_items
            FROM tasks t
            LEFT JOIN task_items i ON i.task_id = t.id
            LEFT JOIN reviews r ON r.task_item_id = i.id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Task not found: {task_id}")

        current_index = int(row["current_task_index"])
        current_item = load_task_item_by_index(connection, task_id, current_index)
        current_review = (
            load_review_for_item(connection, current_item.task_item_id) if current_item else None
        )
        return TaskDetail(
            task_id=int(row["id"]),
            name=str(row["name"]),
            source_file_path=Path(str(row["source_file_path"])),
            source_file_name=str(row["source_file_name"]),
            source_file_size=(
                int(row["source_file_size"]) if row["source_file_size"] is not None else None
            ),
            source_file_mtime=(str(row["source_file_mtime"]) if row["source_file_mtime"] else None),
            source_file_hash=str(row["source_file_hash"]) if row["source_file_hash"] else None,
            task_snapshot=task_snapshot_from_dict(json.loads(row["task_snapshot_json"])),
            browser_config=browser_config_from_dict(
                json.loads(row["browser_config_snapshot_json"])
            ),
            status=str(row["status"]),  # type: ignore[arg-type]
            current_task_index=current_index,
            total_items=int(row["total_items"]),
            completed_items=int(row["completed_items"]),
            current_item=current_item,
            current_review=current_review,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def delete_task(database_path: Path, task_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def update_task_configuration(
    database_path: Path,
    *,
    task_id: int,
    task_snapshot: TaskSnapshot,
    browser_config: BrowserConfig,
    rows: list[tuple[int, dict[str, object]]] | None = None,
    reset_reviews: bool = False,
) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        task_row = connection.execute(
            "SELECT id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            raise ValueError(f"Task not found: {task_id}")

        if rows is not None:
            _delete_task_history(connection, task_id)
            connection.execute("DELETE FROM task_items WHERE task_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO task_items (task_id, task_index, source_row, task_data_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (task_id, index, source_row, _json(task_data), now)
                    for index, (source_row, task_data) in enumerate(rows, start=1)
                ],
            )
            status: TaskStatus = "ready"
            current_task_index = 1
        elif reset_reviews:
            _delete_task_history(connection, task_id)
            connection.execute(
                """
                DELETE FROM reviews
                WHERE task_item_id IN (
                    SELECT id FROM task_items WHERE task_id = ?
                )
                """,
                (task_id,),
            )
            status = "ready"
            current_task_index = 1
        else:
            current = connection.execute(
                "SELECT current_task_index, status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"Task not found: {task_id}")
            current_task_index = int(current[0])
            status = str(current[1])  # type: ignore[assignment]

        connection.execute(
            """
            UPDATE tasks
            SET browser_config_id = ?,
                task_snapshot_json = ?,
                browser_config_snapshot_json = ?,
                current_task_index = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                browser_config.config_id,
                _json(task_snapshot_to_dict(task_snapshot)),
                _json(browser_config_to_dict(browser_config)),
                current_task_index,
                status,
                now,
                task_id,
            ),
        )
        _set_app_setting(connection, "last_task_id", task_id)


def update_task_snapshot(
    database_path: Path,
    *,
    task_id: int,
    task_snapshot: TaskSnapshot,
    browser_config: BrowserConfig,
) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        task_row = connection.execute(
            "SELECT id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            raise ValueError(f"Task not found: {task_id}")
        connection.execute(
            """
            UPDATE tasks
            SET browser_config_id = ?,
                task_snapshot_json = ?,
                browser_config_snapshot_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                browser_config.config_id,
                _json(task_snapshot_to_dict(task_snapshot)),
                _json(browser_config_to_dict(browser_config)),
                now,
                task_id,
            ),
        )
        _set_app_setting(connection, "last_task_id", task_id)


def load_app_setting(database_path: Path, key: str) -> object | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])


def save_app_setting(database_path: Path, key: str, value: object) -> None:
    with sqlite3.connect(database_path) as connection:
        _set_app_setting(connection, key, value)


def list_items_in_range(
    database_path: Path, *, task_id: int, start_index: int, limit: int
) -> list[TaskItem]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, task_index, source_row, task_data_json
            FROM task_items
            WHERE task_id = ? AND task_index >= ?
            ORDER BY task_index
            LIMIT ?
            """,
            (task_id, start_index, limit),
        ).fetchall()
        return [_task_item_from_row(row) for row in rows]


def list_all_items(database_path: Path, task_id: int) -> list[TaskItem]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, task_index, source_row, task_data_json
            FROM task_items
            WHERE task_id = ?
            ORDER BY task_index
            """,
            (task_id,),
        ).fetchall()
        return [_task_item_from_row(row) for row in rows]


def list_reviews(database_path: Path, task_id: int) -> dict[int, ReviewRecord]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT r.task_item_id, r.review_data_json, r.review_status, r.reviewed_at, r.updated_at
            FROM reviews r
            JOIN task_items i ON i.id = r.task_item_id
            WHERE i.task_id = ?
            ORDER BY i.task_index
            """,
            (task_id,),
        ).fetchall()
        return {int(row["task_item_id"]): _review_from_row(row) for row in rows}


def save_review(
    database_path: Path,
    *,
    task_id: int,
    task_index: int,
    review_data: dict[str, object],
    advance_pointer: bool = True,
) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            raise ValueError(f"Task item not found: task_id={task_id}, task_index={task_index}")
        connection.execute(
            """
            INSERT INTO reviews (
                task_item_id, review_data_json, review_status, reviewed_at, updated_at
            )
            VALUES (?, ?, 'completed', ?, ?)
            ON CONFLICT(task_item_id) DO UPDATE SET
                review_data_json = excluded.review_data_json,
                review_status = excluded.review_status,
                reviewed_at = excluded.reviewed_at,
                updated_at = excluded.updated_at
            """,
            (item.task_item_id, _json(review_data), now, now),
        )
        connection.execute(
            """
            INSERT INTO review_history (task_item_id, action, payload_json, created_at)
            VALUES (?, 'updated', ?, ?)
            """,
            (item.task_item_id, _json(review_data), now),
        )
        if advance_pointer:
            total_items = _count_task_items(connection, task_id)
            next_index = min(task_index + 1, total_items + 1)
            status = "completed" if task_index >= total_items else "in_progress"
            _update_task_pointer(connection, task_id, next_index, status)


def revoke_review(
    database_path: Path, *, task_id: int, task_index: int, reset_pointer: bool = True
) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            raise ValueError(f"Task item not found: task_id={task_id}, task_index={task_index}")
        connection.execute("DELETE FROM reviews WHERE task_item_id = ?", (item.task_item_id,))
        connection.execute(
            """
            INSERT INTO review_history (task_item_id, action, payload_json, created_at)
            VALUES (?, 'revoked', '{}', ?)
            """,
            (item.task_item_id, now),
        )
        if reset_pointer:
            _update_task_pointer(connection, task_id, task_index, "in_progress")


def jump_to_task_index(database_path: Path, task_id: int, task_index: int) -> None:
    with sqlite3.connect(database_path) as connection:
        total_items = _count_task_items(connection, task_id)
        normalized = 1 if total_items == 0 else max(1, min(task_index, total_items))
        current_completed = _count_completed_items(connection, task_id)
        status: TaskStatus = (
            "completed" if current_completed >= total_items and total_items > 0 else "in_progress"
        )
        _update_task_pointer(connection, task_id, normalized, status)


def mark_task_in_progress(database_path: Path, task_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        total_items = _count_task_items(connection, task_id)
        current_completed = _count_completed_items(connection, task_id)
        status: TaskStatus = (
            "completed" if current_completed >= total_items and total_items > 0 else "in_progress"
        )
        row = connection.execute(
            "SELECT current_task_index FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Task not found: {task_id}")
        _update_task_pointer(connection, task_id, int(row[0]), status)


def load_review_by_task_index(
    database_path: Path, *, task_id: int, task_index: int
) -> ReviewRecord | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            return None
        return load_review_for_item(connection, item.task_item_id)


def list_browser_configs(database_path: Path) -> list[BrowserConfig]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, name, executable_path, launch_args_json, test_url,
                   last_tested_at, last_test_status
            FROM browser_configs
            ORDER BY updated_at DESC, id
            """
        ).fetchall()
        return [_browser_config_from_row(row) for row in rows]


def load_browser_config(database_path: Path, config_id: str) -> BrowserConfig:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, name, executable_path, launch_args_json, test_url,
                   last_tested_at, last_test_status
            FROM browser_configs
            WHERE id = ?
            """,
            (config_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Browser configuration not found: {config_id}")
        return _browser_config_from_row(row)


def save_browser_config(database_path: Path, config: BrowserConfig) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO browser_configs (
                id, name, executable_path, launch_args_json, test_url,
                last_tested_at, last_test_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                executable_path = excluded.executable_path,
                launch_args_json = excluded.launch_args_json,
                test_url = excluded.test_url,
                last_tested_at = excluded.last_tested_at,
                last_test_status = excluded.last_test_status,
                updated_at = excluded.updated_at
            """,
            (
                config.config_id,
                config.name,
                config.executable_path,
                _json(config.launch_args),
                config.test_url,
                config.last_tested_at,
                config.last_test_status,
                now,
                now,
            ),
        )


def delete_browser_config(database_path: Path, config_id: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM browser_configs WHERE id = ?", (config_id,))


def _database_reset_reason(database_path: Path) -> str | None:
    if not database_path.exists():
        return None

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path)
        table_names = _table_names(connection)
        if not table_names:
            return None
        if "app_settings" not in table_names:
            return "数据库缺少 schema 版本信息"

        schema_version = _schema_version(connection)
        if schema_version != CURRENT_SCHEMA_VERSION:
            return (
                f"数据库 schema 版本不匹配：当前需要 {CURRENT_SCHEMA_VERSION}，"
                f"实际为 {schema_version or '未设置'}"
            )

        for table_name, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
            if table_name not in table_names:
                return f"数据库缺少表：{table_name}"
            existing_columns = _table_columns(connection, table_name)
            missing_columns = sorted(required_columns - existing_columns)
            if missing_columns:
                return f"数据库表 {table_name} 缺少字段：{', '.join(missing_columns)}"
    except sqlite3.DatabaseError as exc:
        return f"数据库文件无法读取：{exc}"
    finally:
        if connection is not None:
            connection.close()

    return None


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        return int(json.loads(str(row[0])))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _seed_defaults(connection: sqlite3.Connection) -> None:
    now = _now_iso()
    _ensure_default_browser_config(connection, default_browser_config(), now)


def _ensure_default_browser_config(
    connection: sqlite3.Connection, config: BrowserConfig, now: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO browser_configs (
            id, name, executable_path, launch_args_json, test_url,
            last_tested_at, last_test_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            config.config_id,
            config.name,
            config.executable_path,
            _json(config.launch_args),
            config.test_url,
            config.last_tested_at,
            config.last_test_status,
            now,
            now,
        ),
    )


def load_task_item_by_index(
    connection: sqlite3.Connection, task_id: int, task_index: int
) -> TaskItem | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT id, task_index, source_row, task_data_json
        FROM task_items
        WHERE task_id = ? AND task_index = ?
        """,
        (task_id, task_index),
    ).fetchone()
    if row is None:
        return None
    return _task_item_from_row(row)


def load_review_for_item(connection: sqlite3.Connection, task_item_id: int) -> ReviewRecord | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT task_item_id, review_data_json, review_status, reviewed_at, updated_at
        FROM reviews
        WHERE task_item_id = ?
        """,
        (task_item_id,),
    ).fetchone()
    if row is None:
        return None
    return _review_from_row(row)


def _task_item_from_row(row: sqlite3.Row) -> TaskItem:
    return TaskItem(
        task_item_id=int(row["id"]),
        task_index=int(row["task_index"]),
        source_row=int(row["source_row"]),
        task_data=json.loads(row["task_data_json"]),
    )


def _review_from_row(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        task_item_id=int(row["task_item_id"]),
        review_data=json.loads(row["review_data_json"]),
        review_status=str(row["review_status"]),
        reviewed_at=str(row["reviewed_at"]) if row["reviewed_at"] else None,
        updated_at=str(row["updated_at"]),
    )


def _browser_config_from_row(row: sqlite3.Row) -> BrowserConfig:
    return BrowserConfig(
        config_id=str(row["id"]),
        name=str(row["name"]),
        executable_path=str(row["executable_path"]),
        launch_args=json.loads(row["launch_args_json"]),
        test_url=str(row["test_url"]),
        last_tested_at=str(row["last_tested_at"]) if row["last_tested_at"] else None,
        last_test_status=str(row["last_test_status"]),
    )


def _count_task_items(connection: sqlite3.Connection, task_id: int) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM task_items WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    )


def _count_completed_items(connection: sqlite3.Connection, task_id: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM reviews r
            JOIN task_items i ON i.id = r.task_item_id
            WHERE i.task_id = ? AND r.review_status = 'completed'
            """,
            (task_id,),
        ).fetchone()[0]
    )


def _delete_task_history(connection: sqlite3.Connection, task_id: int) -> None:
    connection.execute(
        """
        DELETE FROM review_history
        WHERE task_item_id IN (
            SELECT id FROM task_items WHERE task_id = ?
        )
        """,
        (task_id,),
    )


def _update_task_pointer(
    connection: sqlite3.Connection, task_id: int, task_index: int, status: TaskStatus
) -> None:
    now = _now_iso()
    connection.execute(
        """
        UPDATE tasks
        SET current_task_index = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (task_index, status, now, task_id),
    )
    _set_app_setting(connection, "last_task_id", task_id)


def _set_app_setting(connection: sqlite3.Connection, key: str, value: object) -> None:
    connection.execute(
        """
        INSERT INTO app_settings (key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (key, _json(value), _now_iso()),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
