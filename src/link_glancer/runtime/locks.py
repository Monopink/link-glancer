from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from link_glancer.runtime.paths import ensure_runtime_locks_root


@dataclass(slots=True)
class LockOwner:
    resource_type: str
    resource_id: str
    pid: int
    instance_id: int | None
    owner_label: str | None
    created_at: str

    def describe(self) -> str:
        parts = []
        if self.instance_id is not None:
            parts.append(f"实例 {self.instance_id}")
        parts.append(f"PID {self.pid}")
        if self.owner_label:
            parts.append(self.owner_label)
        return " / ".join(parts)


class RuntimeLockConflictError(RuntimeError):
    def __init__(self, *, resource_label: str, owner: LockOwner) -> None:
        self.resource_label = resource_label
        self.owner = owner
        super().__init__(f"{resource_label} 正被占用：{owner.describe()}")


@dataclass(slots=True)
class RuntimeLockHandle:
    path: Path
    resource_type: str
    resource_id: str

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return


@dataclass(slots=True)
class InstanceRegistration:
    path: Path
    instance_id: int
    pid: int

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return


def register_instance() -> InstanceRegistration:
    directory = ensure_runtime_locks_root() / "instances"
    directory.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_instance_registrations(directory)
    used_ids = _live_instance_ids(directory)
    instance_id = _smallest_available_positive_int(used_ids)
    pid = os.getpid()
    path = directory / f"instance-{pid}.json"
    metadata = {
        "resource_type": "instance",
        "resource_id": str(instance_id),
        "pid": pid,
        "instance_id": instance_id,
        "owner_label": "主程序实例",
        "created_at": _now_iso(),
    }
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.environ["LINK_GLANCER_INSTANCE_ID"] = str(instance_id)
    return InstanceRegistration(path=path, instance_id=instance_id, pid=pid)


def acquire_profile_lock(profile_id: str, *, owner_label: str) -> RuntimeLockHandle:
    return _acquire_resource_lock(
        resource_type="profile",
        resource_id=profile_id,
        resource_label=f"浏览器 Profile `{profile_id}`",
        owner_label=owner_label,
    )


def acquire_task_lock(task_id: int, *, owner_label: str) -> RuntimeLockHandle:
    return _acquire_resource_lock(
        resource_type="task",
        resource_id=str(task_id),
        resource_label=f"任务 #{task_id}",
        owner_label=owner_label,
    )


def _acquire_resource_lock(
    *,
    resource_type: str,
    resource_id: str,
    resource_label: str,
    owner_label: str,
) -> RuntimeLockHandle:
    directory = ensure_runtime_locks_root() / resource_type
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_name(resource_id)}.json"
    metadata = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "pid": os.getpid(),
        "instance_id": _current_instance_id(),
        "owner_label": owner_label,
        "created_at": _now_iso(),
    }
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _read_owner(path, resource_type=resource_type, resource_id=resource_id)
            if owner is not None and not _process_exists(owner.pid):
                path.unlink(missing_ok=True)
                continue
            if owner is None:
                path.unlink(missing_ok=True)
                continue
            raise RuntimeLockConflictError(
                resource_label=resource_label,
                owner=owner,
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False, indent=2)
        return RuntimeLockHandle(
            path=path,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    owner = _read_owner(path, resource_type=resource_type, resource_id=resource_id)
    if owner is None:
        owner = LockOwner(
            resource_type=resource_type,
            resource_id=resource_id,
            pid=-1,
            instance_id=None,
            owner_label=None,
            created_at="",
        )
    raise RuntimeLockConflictError(resource_label=resource_label, owner=owner)


def _cleanup_stale_instance_registrations(directory: Path) -> None:
    for path in directory.glob("instance-*.json"):
        owner = _read_owner(path, resource_type="instance", resource_id="")
        if owner is None or not _process_exists(owner.pid):
            path.unlink(missing_ok=True)


def _live_instance_ids(directory: Path) -> set[int]:
    instance_ids: set[int] = set()
    for path in directory.glob("instance-*.json"):
        owner = _read_owner(path, resource_type="instance", resource_id="")
        if owner is None:
            continue
        if not _process_exists(owner.pid):
            path.unlink(missing_ok=True)
            continue
        if owner.instance_id is not None:
            instance_ids.add(owner.instance_id)
    return instance_ids


def _read_owner(path: Path, *, resource_type: str, resource_id: str) -> LockOwner | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return None
    instance_id = payload.get("instance_id")
    return LockOwner(
        resource_type=str(payload.get("resource_type") or resource_type),
        resource_id=str(payload.get("resource_id") or resource_id),
        pid=pid,
        instance_id=instance_id if isinstance(instance_id, int) else None,
        owner_label=(
            str(payload["owner_label"]) if isinstance(payload.get("owner_label"), str) else None
        ),
        created_at=str(payload.get("created_at") or ""),
    )


def _smallest_available_positive_int(values: set[int]) -> int:
    candidate = 1
    while candidate in values:
        candidate += 1
    return candidate


def _current_instance_id() -> int | None:
    raw = os.environ.get("LINK_GLANCER_INSTANCE_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_process_exists(pid: int) -> bool:
    import ctypes

    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    ctypes.windll.kernel32.CloseHandle(process)
    return True


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in value).strip()
    return safe or "lock"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
