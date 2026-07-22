from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from link_glancer.runtime.paths import app_database_path, ensure_database_backups_dir
from link_glancer.tasks.models import (
    BrowserConfig,
    BrowserProfile,
    CreatorCollectionRecovery,
    CreatorCollectionSessionSummary,
    ReviewDraft,
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

CURRENT_SCHEMA_VERSION = 8


@dataclass(frozen=True)
class DatabaseResetSummary:
    reason: str
    backup_path: Path | None = None


_LAST_DATABASE_RESET_SUMMARY: DatabaseResetSummary | None = None


class DatabaseResetRequiredError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def ensure_app_database() -> Path:
    database_path = app_database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _ensure_schema_objects(connection)
        _migrate_schema(connection)
        reset_reason = _database_reset_reason(connection)
        if reset_reason is not None:
            raise DatabaseResetRequiredError(reset_reason)
        _set_app_setting(connection, "schema_version", CURRENT_SCHEMA_VERSION)
    return database_path


def reset_app_database() -> Path:
    global _LAST_DATABASE_RESET_SUMMARY

    database_path = app_database_path()
    backup_path = _backup_existing_database(database_path)
    try:
        database_path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "无法重建数据库文件。请关闭正在运行的 LinkGlancer 或占用 app.db 的程序后重试。"
        ) from exc

    _LAST_DATABASE_RESET_SUMMARY = DatabaseResetSummary(
        reason="用户确认重建数据库。",
        backup_path=backup_path,
    )
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _ensure_schema_objects(connection)
        _set_app_setting(connection, "schema_version", CURRENT_SCHEMA_VERSION)
    return database_path


def consume_database_reset_summary() -> DatabaseResetSummary | None:
    global _LAST_DATABASE_RESET_SUMMARY

    summary = _LAST_DATABASE_RESET_SUMMARY
    _LAST_DATABASE_RESET_SUMMARY = None
    return summary


def _backup_existing_database(database_path: Path) -> Path | None:
    if not database_path.exists():
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = ensure_database_backups_dir() / f"app_{timestamp}.db"
    try:
        shutil.copy2(database_path, backup_path)
    except OSError as exc:
        raise RuntimeError(
            "无法备份现有数据库文件，已取消重建。请关闭占用 app.db 的程序或检查目录权限后重试。"
        ) from exc
    return backup_path


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
                browser_config_snapshot_json, status, current_task_index, viewing_task_index,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        viewing_index = int(row["viewing_task_index"])
        current_item = load_task_item_by_index(connection, task_id, current_index)
        current_review = (
            load_review_for_item(connection, current_item.task_item_id) if current_item else None
        )
        current_draft = (
            load_draft_for_item(connection, current_item.task_item_id) if current_item else None
        )
        viewing_item = load_task_item_by_index(connection, task_id, viewing_index)
        viewing_review = (
            load_review_for_item(connection, viewing_item.task_item_id) if viewing_item else None
        )
        viewing_draft = (
            load_draft_for_item(connection, viewing_item.task_item_id) if viewing_item else None
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
            viewing_task_index=viewing_index,
            total_items=int(row["total_items"]),
            completed_items=int(row["completed_items"]),
            current_item=current_item,
            current_review=current_review,
            current_draft=current_draft,
            viewing_item=viewing_item,
            viewing_review=viewing_review,
            viewing_draft=viewing_draft,
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
            _delete_task_review_state(connection, task_id)
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
            viewing_task_index = 1
        elif reset_reviews:
            _delete_task_review_state(connection, task_id)
            status = "ready"
            current_task_index = 1
            viewing_task_index = 1
        else:
            current = connection.execute(
                "SELECT current_task_index, viewing_task_index, status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"Task not found: {task_id}")
            current_task_index = int(current[0])
            viewing_task_index = int(current[1])
            status = str(current[2])  # type: ignore[assignment]

        connection.execute(
            """
            UPDATE tasks
            SET browser_config_id = ?,
                task_snapshot_json = ?,
                browser_config_snapshot_json = ?,
                current_task_index = ?,
                viewing_task_index = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                browser_config.config_id,
                _json(task_snapshot_to_dict(task_snapshot)),
                _json(browser_config_to_dict(browser_config)),
                current_task_index,
                viewing_task_index,
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


def create_creator_collection_session(
    database_path: Path,
    *,
    browser_config_id: str,
    page_url: str,
    safety_limit: int,
    auto_advance_interval_seconds: float,
    last_message: str,
) -> int:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO creator_collection_sessions (
                browser_config_id, page_url, status, collected_count, pages_fetched,
                safety_limit, auto_advance_interval_seconds, last_message, created_at, updated_at
            )
            VALUES (?, ?, 'active', 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                browser_config_id,
                page_url,
                safety_limit,
                auto_advance_interval_seconds,
                last_message,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def append_creator_collection_session_rows(
    database_path: Path,
    *,
    session_id: int,
    rows: list[tuple[str, dict[str, object]]],
) -> int:
    if not rows:
        return 0
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        existing = connection.execute(
            """
            SELECT row_key
            FROM creator_collection_session_rows
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
        existing_keys = {str(row[0]) for row in existing}
        pending = [
            (row_key, row_data) for row_key, row_data in rows if row_key not in existing_keys
        ]
        if not pending:
            return 0
        current_index = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(item_index), 0)
                FROM creator_collection_session_rows
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        connection.executemany(
            """
            INSERT INTO creator_collection_session_rows (
                session_id, item_index, row_key, task_data_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (session_id, current_index + offset, row_key, _json(row_data), now)
                for offset, (row_key, row_data) in enumerate(pending, start=1)
            ],
        )
        connection.execute(
            """
            UPDATE creator_collection_sessions
            SET collected_count = collected_count + ?, updated_at = ?
            WHERE id = ?
            """,
            (len(pending), now, session_id),
        )
        return len(pending)


def update_creator_collection_session(
    database_path: Path,
    *,
    session_id: int,
    status: str,
    collected_count: int,
    pages_fetched: int,
    safety_limit: int,
    auto_advance_interval_seconds: float,
    last_message: str,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE creator_collection_sessions
            SET status = ?,
                collected_count = ?,
                pages_fetched = ?,
                safety_limit = ?,
                auto_advance_interval_seconds = ?,
                last_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                collected_count,
                pages_fetched,
                safety_limit,
                auto_advance_interval_seconds,
                last_message,
                _now_iso(),
                session_id,
            ),
        )


def load_pending_creator_collection_recovery(
    database_path: Path,
) -> CreatorCollectionRecovery | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                s.id,
                s.browser_config_id,
                s.page_url,
                s.status,
                s.collected_count,
                s.pages_fetched,
                s.safety_limit,
                s.auto_advance_interval_seconds,
                s.last_message,
                s.created_at,
                s.updated_at,
                c.name AS browser_config_name
            FROM creator_collection_sessions s
            JOIN browser_configs c ON c.id = s.browser_config_id
            WHERE s.status IN ('active', 'interrupted', 'finalizing')
              AND s.collected_count > 0
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return CreatorCollectionRecovery(
            session_id=int(row["id"]),
            browser_config_id=str(row["browser_config_id"]),
            browser_config_name=str(row["browser_config_name"]),
            page_url=str(row["page_url"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            collected_count=int(row["collected_count"]),
            pages_fetched=int(row["pages_fetched"]),
            safety_limit=int(row["safety_limit"]),
            auto_advance_interval_seconds=float(row["auto_advance_interval_seconds"]),
            last_message=str(row["last_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def load_creator_collection_session_rows(
    database_path: Path,
    *,
    session_id: int,
) -> list[dict[str, object]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT task_data_json
            FROM creator_collection_session_rows
            WHERE session_id = ?
            ORDER BY item_index
            """,
            (session_id,),
        ).fetchall()
        return [json.loads(str(row["task_data_json"])) for row in rows]


def load_creator_collection_session_summary(
    database_path: Path,
    *,
    session_id: int,
) -> CreatorCollectionSessionSummary:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                id,
                browser_config_id,
                page_url,
                status,
                collected_count,
                pages_fetched,
                safety_limit,
                auto_advance_interval_seconds,
                last_message,
                created_at,
                updated_at
            FROM creator_collection_sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Creator collection session not found: {session_id}")
        return CreatorCollectionSessionSummary(
            session_id=int(row["id"]),
            browser_config_id=str(row["browser_config_id"]),
            page_url=str(row["page_url"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            collected_count=int(row["collected_count"]),
            pages_fetched=int(row["pages_fetched"]),
            safety_limit=int(row["safety_limit"]),
            auto_advance_interval_seconds=float(row["auto_advance_interval_seconds"]),
            last_message=str(row["last_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def finalize_creator_collection_session(database_path: Path, *, session_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            UPDATE creator_collection_sessions
            SET status = 'finalized', updated_at = ?
            WHERE id = ?
            """,
            (_now_iso(), session_id),
        )
        connection.execute(
            "DELETE FROM creator_collection_session_rows WHERE session_id = ?",
            (session_id,),
        )


def discard_creator_collection_session(database_path: Path, *, session_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            UPDATE creator_collection_sessions
            SET status = 'discarded', updated_at = ?
            WHERE id = ?
            """,
            (_now_iso(), session_id),
        )
        connection.execute(
            "DELETE FROM creator_collection_session_rows WHERE session_id = ?",
            (session_id,),
        )


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


def find_previous_reviewed_index(
    database_path: Path, *, task_id: int, before_task_index: int
) -> int | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT i.task_index
            FROM reviews r
            JOIN task_items i ON i.id = r.task_item_id
            WHERE i.task_id = ? AND i.task_index < ? AND r.review_status = 'completed'
            ORDER BY i.task_index DESC
            LIMIT 1
            """,
            (task_id, before_task_index),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])


def find_next_reviewed_index(
    database_path: Path, *, task_id: int, after_task_index: int, max_task_index: int
) -> int | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT i.task_index
            FROM reviews r
            JOIN task_items i ON i.id = r.task_item_id
            WHERE i.task_id = ?
              AND i.task_index > ?
              AND i.task_index <= ?
              AND r.review_status = 'completed'
            ORDER BY i.task_index
            LIMIT 1
            """,
            (task_id, after_task_index, max_task_index),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])


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
        connection.execute("DELETE FROM review_drafts WHERE task_item_id = ?", (item.task_item_id,))
        if advance_pointer:
            total_items = _count_task_items(connection, task_id)
            next_index = min(task_index + 1, total_items + 1)
            status: TaskStatus = (
                "completed"
                if _first_unreviewed_task_index(connection, task_id) is None
                else "in_progress"
            )
            _update_task_pointer(
                connection,
                task_id,
                current_task_index=next_index,
                viewing_task_index=next_index,
                status=status,
            )
        else:
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))


def skip_review(database_path: Path, *, task_id: int, task_index: int) -> None:
    with sqlite3.connect(database_path) as connection:
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            raise ValueError(f"Task item not found: task_id={task_id}, task_index={task_index}")
        connection.execute("DELETE FROM review_drafts WHERE task_item_id = ?", (item.task_item_id,))
        total_items = _count_task_items(connection, task_id)
        next_index = min(task_index + 1, total_items + 1)
        status: TaskStatus = (
            "completed"
            if _first_unreviewed_task_index(connection, task_id) is None
            else "in_progress"
        )
        _update_task_pointer(
            connection,
            task_id,
            current_task_index=next_index,
            viewing_task_index=next_index,
            status=status,
        )


def save_review_draft(
    database_path: Path,
    *,
    task_id: int,
    task_index: int,
    draft_data: dict[str, object],
) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            raise ValueError(f"Task item not found: task_id={task_id}, task_index={task_index}")
        if not draft_data:
            connection.execute(
                "DELETE FROM review_drafts WHERE task_item_id = ?", (item.task_item_id,)
            )
        else:
            connection.execute(
                """
                INSERT INTO review_drafts (task_item_id, draft_data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_item_id) DO UPDATE SET
                    draft_data_json = excluded.draft_data_json,
                    updated_at = excluded.updated_at
                """,
                (item.task_item_id, _json(draft_data), now),
            )
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))


def load_review_draft_by_task_index(
    database_path: Path, *, task_id: int, task_index: int
) -> ReviewDraft | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            return None
        return load_draft_for_item(connection, item.task_item_id)


def update_task_item_data(
    database_path: Path,
    *,
    task_id: int,
    task_index: int,
    task_data_patch: dict[str, object],
) -> None:
    if not task_data_patch:
        return
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        item = load_task_item_by_index(connection, task_id, task_index)
        if item is None:
            raise ValueError(f"Task item not found: task_id={task_id}, task_index={task_index}")
        updated = dict(item.task_data)
        updated.update(task_data_patch)
        connection.execute(
            "UPDATE task_items SET task_data_json = ? WHERE id = ?",
            (_json(updated), item.task_item_id),
        )
        connection.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (now, task_id),
        )


def jump_to_task_index(database_path: Path, task_id: int, task_index: int) -> None:
    with sqlite3.connect(database_path) as connection:
        total_items = _count_task_items(connection, task_id)
        normalized = 1 if total_items == 0 else max(1, min(task_index, total_items))
        status: TaskStatus = "ready" if total_items == 0 else "in_progress"
        _update_task_pointer(
            connection,
            task_id,
            current_task_index=normalized,
            viewing_task_index=normalized,
            status=status,
        )


def set_viewing_task_index(database_path: Path, task_id: int, task_index: int) -> None:
    with sqlite3.connect(database_path) as connection:
        total_items = _count_task_items(connection, task_id)
        if total_items <= 0:
            normalized = 1
        else:
            row = connection.execute(
                "SELECT current_task_index FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Task not found: {task_id}")
            max_viewing_index = min(int(row[0]), total_items)
            normalized = max(1, min(task_index, max_viewing_index))
        connection.execute(
            "UPDATE tasks SET viewing_task_index = ?, updated_at = ? WHERE id = ?",
            (normalized, _now_iso(), task_id),
        )


def mark_task_in_progress(database_path: Path, task_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        total_items = _count_task_items(connection, task_id)
        first_unreviewed = _first_unreviewed_task_index(connection, task_id)
        status: TaskStatus = (
            "completed" if first_unreviewed is None and total_items > 0 else "in_progress"
        )
        current_index = first_unreviewed if first_unreviewed is not None else total_items + 1
        _update_task_pointer(
            connection,
            task_id,
            current_task_index=current_index,
            viewing_task_index=current_index,
            status=status,
        )


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
            SELECT id, name, profile_id, executable_path, launch_args_json, test_url,
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
            SELECT id, name, profile_id, executable_path, launch_args_json, test_url,
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
                id, name, profile_id, executable_path, launch_args_json, test_url,
                last_tested_at, last_test_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                profile_id = excluded.profile_id,
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
                config.profile_id,
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


def list_browser_profiles(database_path: Path) -> list[BrowserProfile]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, name
            FROM browser_profiles
            ORDER BY updated_at DESC, id
            """
        ).fetchall()
        return [
            BrowserProfile(
                profile_id=str(row["id"]),
                name=str(row["name"]),
            )
            for row in rows
        ]


def save_browser_profile(database_path: Path, profile: BrowserProfile) -> None:
    now = _now_iso()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO browser_profiles (id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (profile.profile_id, profile.name, now, now),
        )


def delete_browser_profile(database_path: Path, profile_id: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM browser_profiles WHERE id = ?", (profile_id,))


def _database_reset_reason(connection: sqlite3.Connection) -> str | None:
    try:
        table_names = _table_names(connection)
        if not table_names:
            return None

        schema_version = _schema_version(connection)
        if schema_version is not None and schema_version > CURRENT_SCHEMA_VERSION:
            return (
                f"数据库 schema 版本不匹配：当前需要 {CURRENT_SCHEMA_VERSION}，"
                f"实际为 {schema_version or '未设置'}"
            )
    except sqlite3.DatabaseError as exc:
        return f"数据库文件无法读取：{exc}"

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


def _ensure_schema_objects(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS browser_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS browser_configs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            executable_path TEXT NOT NULL,
            launch_args_json TEXT NOT NULL,
            test_url TEXT NOT NULL,
            last_tested_at TEXT,
            last_test_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id) REFERENCES browser_profiles(id)
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
            viewing_task_index INTEGER NOT NULL,
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

        CREATE TABLE IF NOT EXISTS review_drafts (
            task_item_id INTEGER PRIMARY KEY,
            draft_data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_item_id) REFERENCES task_items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS creator_collection_sessions (
            id INTEGER PRIMARY KEY,
            browser_config_id TEXT NOT NULL,
            page_url TEXT NOT NULL,
            status TEXT NOT NULL,
            collected_count INTEGER NOT NULL,
            pages_fetched INTEGER NOT NULL,
            safety_limit INTEGER NOT NULL,
            auto_advance_interval_seconds REAL NOT NULL,
            last_message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(browser_config_id) REFERENCES browser_configs(id)
        );

        CREATE TABLE IF NOT EXISTS creator_collection_session_rows (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            item_index INTEGER NOT NULL,
            row_key TEXT NOT NULL,
            task_data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, item_index),
            UNIQUE(session_id, row_key),
            FOREIGN KEY(session_id) REFERENCES creator_collection_sessions(id) ON DELETE CASCADE
        );
        """
    )


def _migrate_schema(connection: sqlite3.Connection) -> None:
    schema_version = _schema_version(connection)
    if schema_version is None:
        return
    if schema_version >= CURRENT_SCHEMA_VERSION:
        return
    if schema_version == 5:
        _migrate_schema_v5_to_v6(connection)
        schema_version = 6
    if schema_version == 6:
        _migrate_schema_v6_to_v7(connection)
        schema_version = 7
    if schema_version == 7:
        _migrate_schema_v7_to_v8(connection)
        return
    raise RuntimeError(
        f"不支持从 schema 版本 {schema_version} 自动迁移到 {CURRENT_SCHEMA_VERSION}。"
    )


def _migrate_schema_v5_to_v6(connection: sqlite3.Connection) -> None:
    _ensure_schema_objects(connection)
    _set_app_setting(connection, "schema_version", 6)


def _migrate_schema_v6_to_v7(connection: sqlite3.Connection) -> None:
    existing_task_columns = _table_columns(connection, "tasks")
    if "viewing_task_index" not in existing_task_columns:
        connection.execute(
            "ALTER TABLE tasks ADD COLUMN viewing_task_index INTEGER NOT NULL DEFAULT 1"
        )
        connection.execute(
            """
            UPDATE tasks
            SET viewing_task_index = current_task_index
            WHERE viewing_task_index IS NULL OR viewing_task_index < 1
            """
        )
    connection.execute("DROP TABLE IF EXISTS review_history")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_drafts (
            task_item_id INTEGER PRIMARY KEY,
            draft_data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_item_id) REFERENCES task_items(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_schema_objects(connection)
    _set_app_setting(connection, "schema_version", 7)


def _migrate_schema_v7_to_v8(connection: sqlite3.Connection) -> None:
    _ensure_schema_objects(connection)
    _migrate_task_snapshot_payloads(connection)
    _migrate_app_settings_payloads(connection)
    _set_app_setting(connection, "schema_version", 8)


def _migrate_task_snapshot_payloads(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT id, task_snapshot_json FROM tasks").fetchall()
    for task_id, raw_payload in rows:
        payload = json.loads(str(raw_payload))
        if not isinstance(payload, dict):
            continue
        normalized = _normalize_task_snapshot_payload(payload)
        connection.execute(
            "UPDATE tasks SET task_snapshot_json = ?, updated_at = ? WHERE id = ?",
            (_json(normalized), _now_iso(), int(task_id)),
        )


def _migrate_app_settings_payloads(connection: sqlite3.Connection) -> None:
    review_field_library = load_app_setting_from_connection(connection, "review_field_library")
    if isinstance(review_field_library, list):
        normalized_library = [
            _normalize_review_field_payload(item)
            for item in review_field_library
            if isinstance(item, dict)
        ]
        _set_app_setting(connection, "review_field_library", normalized_library)

    last_defaults = load_app_setting_from_connection(connection, "last_task_creation_defaults")
    if isinstance(last_defaults, dict):
        snapshot = last_defaults.get("task_snapshot")
        if isinstance(snapshot, dict):
            last_defaults["task_snapshot"] = _normalize_task_snapshot_payload(snapshot)
            _set_app_setting(connection, "last_task_creation_defaults", last_defaults)


def _normalize_task_snapshot_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    review_fields = normalized.get("review_fields")
    if isinstance(review_fields, list):
        normalized["review_fields"] = [
            _normalize_review_field_payload(item)
            for item in review_fields
            if isinstance(item, dict)
        ]
    normalized.setdefault(
        "manual_review_scope", ["screen_passed", "screen_failed", "screen_unresolved"]
    )
    normalized.setdefault("export_scope", ["screen_passed", "screen_failed", "screen_unresolved"])
    normalized.setdefault(
        "enrichment_scope", ["screen_passed", "screen_failed", "screen_unresolved"]
    )
    return normalized


def _normalize_review_field_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized.setdefault("field", str(normalized.get("id", "")))
    normalized.setdefault("screen_pass_value", "通过")
    normalized.setdefault("screen_fail_value", "不通过")
    normalized.setdefault("source", "manual")
    return normalized


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def load_app_setting_from_connection(connection: sqlite3.Connection, key: str) -> object | None:
    row = connection.execute(
        "SELECT value_json FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


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


def load_draft_for_item(connection: sqlite3.Connection, task_item_id: int) -> ReviewDraft | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT task_item_id, draft_data_json, updated_at
        FROM review_drafts
        WHERE task_item_id = ?
        """,
        (task_item_id,),
    ).fetchone()
    if row is None:
        return None
    return _review_draft_from_row(row)


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


def _review_draft_from_row(row: sqlite3.Row) -> ReviewDraft:
    return ReviewDraft(
        task_item_id=int(row["task_item_id"]),
        draft_data=json.loads(row["draft_data_json"]),
        updated_at=str(row["updated_at"]),
    )


def _browser_config_from_row(row: sqlite3.Row) -> BrowserConfig:
    return BrowserConfig(
        config_id=str(row["id"]),
        name=str(row["name"]),
        profile_id=str(row["profile_id"]),
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


def _first_unreviewed_task_index(connection: sqlite3.Connection, task_id: int) -> int | None:
    row = connection.execute(
        """
        SELECT MIN(i.task_index)
        FROM task_items i
        LEFT JOIN reviews r ON r.task_item_id = i.id AND r.review_status = 'completed'
        WHERE i.task_id = ? AND r.task_item_id IS NULL
        """,
        (task_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _delete_task_review_state(connection: sqlite3.Connection, task_id: int) -> None:
    connection.execute(
        """
        DELETE FROM review_drafts
        WHERE task_item_id IN (
            SELECT id FROM task_items WHERE task_id = ?
        )
        """,
        (task_id,),
    )
    connection.execute(
        """
        DELETE FROM reviews
        WHERE task_item_id IN (
            SELECT id FROM task_items WHERE task_id = ?
        )
        """,
        (task_id,),
    )


def _update_task_pointer(
    connection: sqlite3.Connection,
    task_id: int,
    *,
    current_task_index: int,
    viewing_task_index: int,
    status: TaskStatus,
) -> None:
    now = _now_iso()
    connection.execute(
        """
        UPDATE tasks
        SET current_task_index = ?, viewing_task_index = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (current_task_index, viewing_task_index, status, now, task_id),
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
