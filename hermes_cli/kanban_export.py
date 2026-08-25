"""Dependency-light, read-only Kanban JSON export edge.

This module intentionally imports only the standard library.  The CLI invokes it
before configuration, logging, or the normal Kanban command dispatcher so an
export cannot initialize or otherwise mutate a board.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import sqlite3
import stat
import sys
import time
from pathlib import Path

from hermes_constants import get_default_hermes_root

_EXPORT_LIMIT = 200
_MAX_DB_BYTES = 512 * 1024 * 1024
_MAX_WAL_BYTES = 128 * 1024 * 1024
_MAX_SHM_BYTES = 128 * 1024 * 1024
_SQLITE_TIMEOUT_SECONDS = 1.0
_HELPER_TIMEOUT_SECONDS = 2.0
_MAX_HELPER_PAYLOAD_BYTES = 2 * 1024 * 1024
_VALID_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"}
)
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
_COLUMNS = (
    "id",
    "title",
    "assignee",
    "status",
    "priority",
    "project_id",
    "created_at",
    "started_at",
    "completed_at",
)


class _InvalidTaskData(Exception):
    """A native row cannot be represented by the public export schema."""


class _UnsafeDatabase(Exception):
    """The canonical database files do not satisfy the read-only boundary."""


def _kanban_root() -> Path:
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        return get_default_hermes_root()
    except (OSError, RuntimeError):
        raise _UnsafeDatabase from None


def _board_db_path(board: str) -> Path:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    root = _kanban_root()
    if board == "default":
        return root / "kanban.db"
    return root / "kanban" / "boards" / board / "kanban.db"


def _file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode)


def _open_bounded_source(path: Path, maximum: int) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if not hasattr(os, "O_NOFOLLOW") and stat.S_ISLNK(os.lstat(path).st_mode):
        raise _UnsafeDatabase
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= maximum:
            raise _UnsafeDatabase
        if _file_identity(os.stat(path, follow_symlinks=False)) != _file_identity(info):
            raise _UnsafeDatabase
        return descriptor, info
    except Exception:
        os.close(descriptor)
        raise


def _source_still_matches(path: Path, descriptor: int, maximum: int) -> bool:
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(descriptor_info.st_mode)
        and 0 <= descriptor_info.st_size <= maximum
        and _file_identity(descriptor_info) == _file_identity(path_info)
    )


def _path_is_absent(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _validate_sources(
    db_path: Path,
    db_fd: int,
    wal_path: Path,
    wal_fd: int | None,
    shm_path: Path,
    shm_fd: int | None,
) -> bool:
    if not _source_still_matches(db_path, db_fd, _MAX_DB_BYTES):
        return False
    if wal_fd is None:
        return _path_is_absent(wal_path) and (
            _path_is_absent(shm_path)
            if shm_fd is None
            else _source_still_matches(shm_path, shm_fd, _MAX_SHM_BYTES)
        )
    return (
        shm_fd is not None
        and _source_still_matches(wal_path, wal_fd, _MAX_WAL_BYTES)
        and _source_still_matches(shm_path, shm_fd, _MAX_SHM_BYTES)
    )


def _descriptor_root() -> Path | None:
    return next(
        (root for root in (Path("/proc/self/fd"), Path("/dev/fd")) if root.is_dir()),
        None,
    )


def _process_descriptors() -> frozenset[int] | None:
    descriptor_root = _descriptor_root()
    if descriptor_root is None:
        return None
    try:
        names = os.listdir(descriptor_root)
    except OSError:
        return None
    descriptors: set[int] = set()
    for name in names:
        try:
            descriptor = int(name)
            os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        descriptors.add(descriptor)
    return frozenset(descriptors)


def _connection_file_identities(excluded: frozenset[int]) -> set[tuple[int, int, int]] | None:
    """Return identities held by SQLite, excluding pre-existing descriptors."""
    descriptors = _process_descriptors()
    if descriptors is None:
        return None
    identities: set[tuple[int, int, int]] = set()
    for descriptor in descriptors - excluded:
        try:
            identities.add(_file_identity(os.fstat(descriptor)))
        except OSError:
            continue
    return identities


def _linux_mapping_count(descriptor: int) -> int | None:
    """Count mappings backed by the validated SHM inode."""
    maps_path = Path("/proc/self/maps")
    try:
        target = os.fstat(descriptor)
        lines = maps_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    matches = 0
    for line in lines:
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            continue
        try:
            major_text, minor_text = fields[3].split(":", 1)
            device = os.makedev(int(major_text, 16), int(minor_text, 16))
            inode = int(fields[4])
        except (ValueError, OSError):
            continue
        if (device, inode) == (target.st_dev, target.st_ino):
            matches += 1
    return matches


def _connection_uses_validated_sources(
    db_fd: int,
    wal_fd: int | None,
    shm_fd: int | None,
    descriptors_before: frozenset[int] | None,
    shm_mappings_before: int | None,
) -> bool:
    if descriptors_before is None:
        return False
    validation_descriptors = frozenset(
        descriptor
        for descriptor in (db_fd, wal_fd, shm_fd)
        if descriptor is not None
    )
    held = _connection_file_identities(descriptors_before | validation_descriptors)
    if held is None or _file_identity(os.fstat(db_fd)) not in held:
        return False
    if wal_fd is None:
        return True
    mappings_after = _linux_mapping_count(shm_fd) if shm_fd is not None else None
    return (
        shm_fd is not None
        and _file_identity(os.fstat(wal_fd)) in held
        and shm_mappings_before is not None
        and mappings_after is not None
        and mappings_after > shm_mappings_before
    )


def _connection_holds_validated_db(
    db_fd: int,
    wal_fd: int | None,
    shm_fd: int | None,
    descriptors_before: frozenset[int] | None,
) -> bool:
    if descriptors_before is None:
        return False
    validation_descriptors = frozenset(
        descriptor
        for descriptor in (db_fd, wal_fd, shm_fd)
        if descriptor is not None
    )
    held = _connection_file_identities(descriptors_before | validation_descriptors)
    return held is not None and _file_identity(os.fstat(db_fd)) in held


def _child_snapshot_payload(
    canonical_db: Path,
    db_fd: int,
    wal_path: Path,
    wal_fd: int | None,
    shm_path: Path,
    shm_fd: int | None,
) -> dict[str, object]:
    selected = ", ".join(_COLUMNS)
    connection = None
    try:
        descriptors_before = _process_descriptors()
        shm_mappings_before = (
            _linux_mapping_count(shm_fd) if shm_fd is not None else None
        )
        uri_options = "mode=ro" if wal_fd is not None else "mode=ro&immutable=1"
        connection = sqlite3.connect(
            f"{canonical_db.as_uri()}?{uri_options}",
            uri=True,
            timeout=_SQLITE_TIMEOUT_SECONDS,
        )
        if not _connection_holds_validated_db(
            db_fd, wal_fd, shm_fd, descriptors_before
        ):
            raise _UnsafeDatabase
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        if not _validate_sources(
            canonical_db, db_fd, wal_path, wal_fd, shm_path, shm_fd
        ):
            raise _UnsafeDatabase
        rows = connection.execute(
            f"SELECT {selected} FROM tasks WHERE status != 'archived' "
            "ORDER BY priority DESC, created_at ASC, id ASC LIMIT ?",
            (_EXPORT_LIMIT + 1,),
        ).fetchall()
        if not _connection_uses_validated_sources(
            db_fd,
            wal_fd,
            shm_fd,
            descriptors_before,
            shm_mappings_before,
        ):
            raise _UnsafeDatabase
        if not _validate_sources(
            canonical_db, db_fd, wal_path, wal_fd, shm_path, shm_fd
        ):
            raise _UnsafeDatabase
        for row in rows:
            _task_from_row(row)
        protocol_rows = [
            {column: row[column] for column in _COLUMNS}
            for row in rows
        ]
        connection.close()
        connection = None
        if not _validate_sources(
            canonical_db, db_fd, wal_path, wal_fd, shm_path, shm_fd
        ):
            raise _UnsafeDatabase
        return {"status": "ok", "rows": protocol_rows}
    except _InvalidTaskData:
        return {"status": "invalid"}
    except BaseException:
        return {"status": "unsafe"}
    finally:
        if connection is not None:
            connection.close()


def _write_helper_payload(descriptor: int, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_HELPER_PAYLOAD_BYTES:
        encoded = b'{"status":"invalid"}'
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("helper pipe closed")
        view = view[written:]


def _receive_helper_payload(pid: int, descriptor: int) -> dict[str, object]:
    deadline = time.monotonic() + _HELPER_TIMEOUT_SECONDS
    data = bytearray()
    reaped = False
    try:
        os.set_blocking(descriptor, False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _UnsafeDatabase
            readable, _, _ = select.select([descriptor], [], [], remaining)
            if not readable:
                raise _UnsafeDatabase
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_HELPER_PAYLOAD_BYTES:
                raise _UnsafeDatabase
        while True:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                reaped = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _UnsafeDatabase
            time.sleep(min(0.01, remaining))
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise _UnsafeDatabase
        payload = json.loads(data.decode("utf-8"))
        if type(payload) is not dict:
            raise _UnsafeDatabase
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _UnsafeDatabase from None
    finally:
        os.close(descriptor)
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def _read_snapshot_rows(db_path: Path) -> list[dict[str, object]]:
    if not hasattr(os, "fork"):
        raise _UnsafeDatabase
    db_fd = wal_fd = shm_fd = None
    try:
        db_fd, db_info = _open_bounded_source(db_path, _MAX_DB_BYTES)
        try:
            canonical_db = db_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise _UnsafeDatabase from None
        if _file_identity(
            os.stat(canonical_db, follow_symlinks=False)
        ) != _file_identity(db_info):
            raise _UnsafeDatabase

        wal_path = Path(f"{canonical_db}-wal")
        shm_path = Path(f"{canonical_db}-shm")
        try:
            wal_fd, _ = _open_bounded_source(wal_path, _MAX_WAL_BYTES)
        except FileNotFoundError:
            wal_fd = None
        try:
            shm_fd, _ = _open_bounded_source(shm_path, _MAX_SHM_BYTES)
        except FileNotFoundError:
            shm_fd = None
        if wal_fd is not None and shm_fd is None:
            raise _UnsafeDatabase
        if not _validate_sources(
            canonical_db, db_fd, wal_path, wal_fd, shm_path, shm_fd
        ):
            raise _UnsafeDatabase

        read_fd, write_fd = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if pid == 0:
            os.close(read_fd)
            exit_code = 0
            try:
                payload = _child_snapshot_payload(
                    canonical_db,
                    db_fd,
                    wal_path,
                    wal_fd,
                    shm_path,
                    shm_fd,
                )
                _write_helper_payload(write_fd, payload)
            except BaseException:
                exit_code = 1
            finally:
                os.close(write_fd)
            os._exit(exit_code)

        os.close(write_fd)
        payload = _receive_helper_payload(pid, read_fd)
        if not _validate_sources(
            canonical_db, db_fd, wal_path, wal_fd, shm_path, shm_fd
        ):
            raise _UnsafeDatabase
        if payload == {"status": "invalid"}:
            raise _InvalidTaskData
        if set(payload) != {"status", "rows"} or payload.get("status") != "ok":
            raise _UnsafeDatabase
        rows = payload.get("rows")
        if type(rows) is not list or len(rows) > _EXPORT_LIMIT + 1:
            raise _UnsafeDatabase
        expected_keys = set(_COLUMNS)
        for row in rows:
            if type(row) is not dict or set(row) != expected_keys:
                raise _UnsafeDatabase
            _task_from_row(row)
        return rows
    finally:
        for descriptor in (shm_fd, wal_fd, db_fd):
            if descriptor is not None:
                os.close(descriptor)


def _valid_optional_text(value: object, maximum: int) -> bool:
    return value is None or (type(value) is str and len(value) <= maximum)


def _valid_optional_timestamp(value: object) -> bool:
    return value is None or (type(value) is int and 0 <= value <= 2**63 - 1)


def _task_from_row(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    task_id = row["id"]
    title = row["title"]
    status = row["status"]
    priority = row["priority"]
    if not (type(task_id) is str and 1 <= len(task_id) <= 128):
        raise _InvalidTaskData
    if not (type(title) is str and 1 <= len(title) <= 500):
        raise _InvalidTaskData
    if not _valid_optional_text(row["assignee"], 128):
        raise _InvalidTaskData
    if type(status) is not str or status not in _VALID_STATUSES:
        raise _InvalidTaskData
    if not (type(priority) is int and -1_000_000 <= priority <= 1_000_000):
        raise _InvalidTaskData
    if not _valid_optional_text(row["project_id"], 128):
        raise _InvalidTaskData
    if not all(
        _valid_optional_timestamp(row[field])
        for field in ("created_at", "started_at", "completed_at")
    ):
        raise _InvalidTaskData

    return {
        "id": task_id,
        "title": title,
        "assignee": row["assignee"],
        "status": status,
        "priority_native": priority,
        "project_id": row["project_id"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def export_board_json(board: str) -> int:
    """Write one sanitized board snapshot and return its process exit code."""
    if not _BOARD_SLUG_RE.fullmatch(board):
        print(f"error: kanban board {board!r} is unavailable", file=sys.stderr)
        return 4

    try:
        rows = _read_snapshot_rows(_board_db_path(board))
    except _InvalidTaskData:
        print("error: kanban export contains invalid task data", file=sys.stderr)
        return 5
    except (OSError, sqlite3.Error, _UnsafeDatabase):
        print(f"error: kanban board {board!r} is unavailable", file=sys.stderr)
        return 4

    try:
        tasks = [_task_from_row(row) for row in rows[:_EXPORT_LIMIT]]
    except _InvalidTaskData:
        print("error: kanban export contains invalid task data", file=sys.stderr)
        return 5

    payload = {
        "schema_version": 1,
        "board": board,
        "tasks": tasks,
        "truncated": len(rows) > _EXPORT_LIMIT,
    }
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


def try_early_export(argv: list[str]) -> int | None:
    """Recognize only the two documented side-effect-free JSON export shapes."""
    if (
        len(argv) == 5
        and argv[0] == "kanban"
        and argv[1] == "--board"
        and argv[3:] == ["export", "--json"]
    ):
        return export_board_json(argv[2])
    if (
        len(argv) == 4
        and argv[0] == "kanban"
        and argv[1].startswith("--board=")
        and len(argv[1]) > len("--board=")
        and argv[2:] == ["export", "--json"]
    ):
        return export_board_json(argv[1][len("--board=") :])
    return None
