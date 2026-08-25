"""Behavioral regressions for truthful WAL snapshots and canonical DB paths."""

from __future__ import annotations

import mmap
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_export_contract import (
    _WORKTREE,
    _assert_inventory_unchanged_except_preexisting_shm,
    _assert_no_sensitive_text,
    _assert_valid_snapshot,
    _env,
    _export,
    _inventory,
)


def _checkpoint(writer: sqlite3.Connection) -> None:
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _writer_rows(writer: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(row) for row in writer.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("tasks", "task_events")
    }


@pytest.mark.requires_wal
def test_export_observes_committed_wal_task_and_status_without_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    try:
        baseline_id = kb.create_task(writer, title="checkpointed baseline")
        _checkpoint(writer)

        wal_id = kb.create_task(writer, title="committed WAL task")
        writer.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (baseline_id,))
        writer.commit()
        expected_rows = _writer_rows(writer)
        before = _inventory(home)
        assert before.get("kanban.db-shm", (None,))[0] == "file"

        result = _export(home)

        after = _inventory(home)
        actual_rows = _writer_rows(writer)
        _assert_no_sensitive_text(result)
        assert actual_rows == expected_rows
        _assert_inventory_unchanged_except_preexisting_shm(before, after)
        payload = _assert_valid_snapshot(result)
        exported = {task["id"]: task for task in payload["tasks"]}
        assert exported[baseline_id]["status"] == "done"
        assert exported[wal_id]["title"] == "committed WAL task"
    finally:
        writer.close()


def test_export_observes_schema_and_task_created_only_in_wal(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    db_path = home / "kanban.db"
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE tasks ("
            "id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
            "project_id TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        writer.execute(
            "INSERT INTO tasks VALUES (?, ?, NULL, 'ready', 7, NULL, 101, NULL, NULL)",
            ("wal-schema-task", "schema and row live in WAL"),
        )
        writer.commit()
        assert (home / "kanban.db-wal").is_file()
        before = _inventory(home)
        assert before.get("kanban.db-shm", (None,))[0] == "file"

        result = _export(home)

        _assert_no_sensitive_text(result)
        _assert_inventory_unchanged_except_preexisting_shm(before, _inventory(home))
        payload = _assert_valid_snapshot(result)
        assert [(task["id"], task["title"]) for task in payload["tasks"]] == [
            ("wal-schema-task", "schema and row live in WAL")
        ]
    finally:
        writer.close()


@pytest.mark.requires_wal
def test_export_with_wal_but_missing_shm_fails_closed_without_creating_shm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_home = tmp_path / "source"
    source_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    try:
        baseline_id = kb.create_task(writer, title="copied checkpointed baseline")
        _checkpoint(writer)
        kb.create_task(writer, title="copied committed WAL task")
        writer.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (baseline_id,))
        writer.commit()

        fixture_home = tmp_path / "copied-without-shm"
        fixture_home.mkdir()
        shutil.copyfile(source_home / "kanban.db", fixture_home / "kanban.db")
        shutil.copyfile(source_home / "kanban.db-wal", fixture_home / "kanban.db-wal")
        assert not (fixture_home / "kanban.db-shm").exists()
        before = _inventory(fixture_home)

        result = _export(fixture_home)

        _assert_no_sensitive_text(result)
        assert _inventory(fixture_home) == before
        assert not (fixture_home / "kanban.db-shm").exists()
        assert result.returncode == 4
        assert result.stdout == ""
        assert result.stderr == "error: kanban board 'default' is unavailable\n"
    finally:
        writer.close()


@pytest.mark.requires_wal
def test_export_rejects_unknown_status_committed_only_in_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    try:
        task_id = kb.create_task(writer, title="checkpointed safe task")
        _checkpoint(writer)
        writer.execute(
            "UPDATE tasks SET status = 'SENTINEL_UNKNOWN_STATUS' WHERE id = ?", (task_id,)
        )
        writer.commit()
        expected_rows = _writer_rows(writer)
        before = _inventory(home)
        assert before.get("kanban.db-shm", (None,))[0] == "file"

        result = _export(home)

        _assert_no_sensitive_text(result)
        assert _writer_rows(writer) == expected_rows
        _assert_inventory_unchanged_except_preexisting_shm(before, _inventory(home))
        assert result.returncode == 5
        assert result.stdout == ""
        assert result.stderr == "error: kanban export contains invalid task data\n"
    finally:
        writer.close()


@pytest.mark.parametrize(
    "case",
    ["db-override", "kanban-home", "custom-hermes-home", "native-profile"],
)
def test_export_resolves_default_db_exactly_like_canonical_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    fake_user_home = tmp_path / "user"
    native_root = fake_user_home / ".hermes"
    custom_root = tmp_path / "custom-root"
    env: dict[str, str] = {"HOME": str(fake_user_home)}
    if case == "db-override":
        env.update(
            HERMES_HOME=str(tmp_path / "wrong-hermes-home"),
            HERMES_KANBAN_HOME=str(tmp_path / "wrong-kanban-home"),
            HERMES_KANBAN_DB=str(tmp_path / "pinned" / "canonical.db"),
        )
        expected_path = Path(env["HERMES_KANBAN_DB"])
    elif case == "kanban-home":
        env.update(
            HERMES_HOME=str(tmp_path / "wrong-hermes-home"),
            HERMES_KANBAN_HOME=str(tmp_path / "kanban-root"),
        )
        expected_path = Path(env["HERMES_KANBAN_HOME"]) / "kanban.db"
    elif case == "custom-hermes-home":
        env["HERMES_HOME"] = str(custom_root)
        expected_path = custom_root / "kanban.db"
    else:
        env["HERMES_HOME"] = str(native_root / "profiles" / "reviewer")
        expected_path = native_root / "kanban.db"

    for name in ("HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HOME"):
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    assert kb.kanban_db_path(board="default") == expected_path
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    task_id = kb.create_task(writer, title=f"canonical path {case}")
    _checkpoint(writer)
    writer.close()
    before = _inventory(tmp_path)

    result = _export(Path(env["HERMES_HOME"]), **env)

    _assert_no_sensitive_text(result)
    assert _inventory(tmp_path) == before
    payload = _assert_valid_snapshot(result)
    assert [(task["id"], task["title"]) for task in payload["tasks"]] == [
        (task_id, f"canonical path {case}")
    ]


def test_export_db_override_fifo_fails_closed_promptly_without_opening_reader(
    tmp_path: Path,
) -> None:
    home = tmp_path / "absent-hermes-home"
    fifo = tmp_path / "kanban.db.fifo"
    os.mkfifo(fifo)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "--board",
            "default",
            "export",
            "--json",
        ],
        cwd=_WORKTREE,
        env=_env(home, HERMES_KANBAN_DB=str(fifo)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate(timeout=3)

    assert not timed_out, "export blocked while opening HERMES_KANBAN_DB FIFO"
    assert not home.exists()
    assert (process.returncode, stdout, stderr) == (
        4,
        "",
        "error: kanban board 'default' is unavailable\n",
    )


def test_export_fails_closed_when_database_path_is_swapped_during_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hermes_cli import kanban_export

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    trusted_id = kb.create_task(writer, title="trusted task")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.close()

    db_path = home / "kanban.db"
    evil_path = tmp_path / "evil.db"
    parked_path = tmp_path / "trusted.db"
    shutil.copy2(db_path, evil_path)
    evil = sqlite3.connect(evil_path)
    evil.execute(
        "UPDATE tasks SET title = ? WHERE id = ?",
        ("EVIL_RACE_WON", trusted_id),
    )
    evil.commit()
    evil.close()

    real_connect = sqlite3.connect

    def swap_around_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        os.replace(db_path, parked_path)
        os.replace(evil_path, db_path)
        try:
            return real_connect(*args, **kwargs)
        finally:
            os.replace(db_path, evil_path)
            os.replace(parked_path, db_path)

    monkeypatch.setattr(kanban_export.sqlite3, "connect", swap_around_connect)

    result = kanban_export.export_board_json("default")
    captured = capsys.readouterr()

    assert result == 4
    assert captured.out == ""
    assert captured.err == "error: kanban board 'default' is unavailable\n"
    assert "EVIL_RACE_WON" not in captured.out + captured.err


def test_connection_binding_ignores_preexisting_database_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hermes_cli import kanban_export

    db_path = tmp_path / "trusted.db"
    evil_path = tmp_path / "evil.db"
    parked_path = tmp_path / "parked.db"
    writer = sqlite3.connect(db_path)
    writer.execute(
        "CREATE TABLE tasks ("
        "id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
        "project_id TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
    )
    writer.execute(
        "INSERT INTO tasks VALUES (?, ?, NULL, ?, ?, NULL, ?, NULL, NULL)",
        ("trusted-id", "trusted task", "todo", 1, 1),
    )
    writer.commit()
    shutil.copy2(db_path, evil_path)
    evil = sqlite3.connect(evil_path)
    evil.execute("UPDATE tasks SET title = 'EVIL_PREEXISTING_FD'")
    evil.commit()
    evil.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    real_connect = sqlite3.connect

    def swap_around_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        os.replace(db_path, parked_path)
        os.replace(evil_path, db_path)
        try:
            return real_connect(*args, **kwargs)
        finally:
            os.replace(db_path, evil_path)
            os.replace(parked_path, db_path)

    monkeypatch.setattr(kanban_export.sqlite3, "connect", swap_around_connect)
    try:
        result = kanban_export.export_board_json("default")
        captured = capsys.readouterr()
    finally:
        writer.close()

    assert result == 4
    assert captured.out == ""
    assert captured.err == "error: kanban board 'default' is unavailable\n"
    assert "EVIL_PREEXISTING_FD" not in captured.out + captured.err


def test_canonicalization_symlink_loop_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hermes_cli import kanban_export

    db_path = tmp_path / "trusted.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE tasks ("
        "id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
        "project_id TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    real_open = kanban_export._open_bounded_source

    def open_then_loop(path: Path, maximum: int) -> tuple[int, os.stat_result]:
        descriptor, info = real_open(path, maximum)
        if path == db_path:
            db_path.unlink()
            db_path.symlink_to(db_path.name)
        return descriptor, info

    monkeypatch.setattr(kanban_export, "_open_bounded_source", open_then_loop)
    result = kanban_export.export_board_json("default")
    captured = capsys.readouterr()

    assert result == 4
    assert captured.out == ""
    assert captured.err == "error: kanban board 'default' is unavailable\n"


def test_symlink_looped_hermes_home_is_sanitized_in_exact_cli(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name)

    result = _export(loop)

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "error: kanban board 'default' is unavailable\n"
    assert str(loop) not in result.stderr
    assert "Traceback" not in result.stderr


def test_database_path_swap_to_fifo_is_bounded_and_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "trusted.db"
    fifo_path = tmp_path / "replacement.fifo"
    parked_path = tmp_path / "parked.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE tasks ("
        "id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
        "project_id TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
    )
    connection.commit()
    connection.close()
    os.mkfifo(fifo_path)

    script = (
        "import os,sqlite3,sys; from pathlib import Path; "
        "from hermes_cli import kanban_export as e; real=sqlite3.connect; "
        "db=Path(os.environ['RACE_DB']); fifo=Path(os.environ['RACE_FIFO']); "
        "parked=Path(os.environ['RACE_PARKED']); "
        "exec(\"def race(*args, **kwargs):\\n"
        " os.replace(db, parked)\\n os.replace(fifo, db)\\n"
        " return real(*args, **kwargs)\"); "
        "e.sqlite3.connect=race; sys.exit(e.export_board_json('default'))"
    )
    environment = _env(
        tmp_path,
        HERMES_KANBAN_DB=str(db_path),
        RACE_DB=str(db_path),
        RACE_FIFO=str(fifo_path),
        RACE_PARKED=str(parked_path),
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=_WORKTREE,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate(timeout=3)

    assert not timed_out, "connect-time FIFO swap wedged the export process"
    assert (process.returncode, stdout, stderr) == (
        4,
        "",
        "error: kanban board 'default' is unavailable\n",
    )


@pytest.mark.parametrize(
    "failure_mode", ["timeout", "eof_stall", "crash", "malformed"]
)
def test_helper_failures_are_bounded_sanitized_and_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_mode: str,
) -> None:
    from hermes_cli import kanban_export

    db_path = tmp_path / "trusted.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE tasks ("
        "id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
        "project_id TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    child_pids: list[int] = []
    real_fork = os.fork

    def recording_fork() -> int:
        pid = real_fork()
        if pid > 0:
            child_pids.append(pid)
        return pid

    monkeypatch.setattr(kanban_export.os, "fork", recording_fork)
    if failure_mode == "eof_stall":
        monkeypatch.setattr(kanban_export, "_HELPER_TIMEOUT_SECONDS", 0.2)

        def broken_write(descriptor: int, _payload: dict[str, object]) -> None:
            os.close(descriptor)
            time.sleep(5)
            os._exit(0)

        monkeypatch.setattr(kanban_export, "_write_helper_payload", broken_write)
    else:
        if failure_mode == "timeout":
            monkeypatch.setattr(kanban_export, "_HELPER_TIMEOUT_SECONDS", 0.2)

            def broken_payload(*_args: object) -> dict[str, object]:
                time.sleep(5)
                return {"status": "unsafe"}

        elif failure_mode == "crash":

            def broken_payload(*_args: object) -> dict[str, object]:
                os._exit(17)

        else:

            def broken_payload(*_args: object) -> dict[str, object]:
                return {"status": "ok", "rows": "not-a-list"}

        monkeypatch.setattr(kanban_export, "_child_snapshot_payload", broken_payload)
    started = time.monotonic()
    result = kanban_export.export_board_json("default")
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert result == 4
    assert elapsed < 2
    assert captured.out == ""
    assert captured.err == "error: kanban board 'default' is unavailable\n"
    assert len(child_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pids[0], os.WNOHANG)


@pytest.mark.requires_wal
@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_export_fails_closed_when_wal_source_identity_changes_during_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sidecar_suffix: str,
) -> None:
    from hermes_cli import kanban_export

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    try:
        kb.create_task(writer, title="trusted WAL task")
        writer.commit()
        db_path = home / "kanban.db"
        target_path = Path(f"{db_path}{sidecar_suffix}")
        alternate_path = tmp_path / f"alternate{sidecar_suffix}"
        parked_path = tmp_path / f"trusted{sidecar_suffix}"
        assert target_path.is_file()
        shutil.copy2(target_path, alternate_path)

        real_connect = sqlite3.connect
        restored = False

        def restore_sources() -> None:
            nonlocal restored
            if restored:
                return
            os.replace(target_path, alternate_path)
            os.replace(parked_path, target_path)
            restored = True

        class CursorProxy:
            def __init__(self, cursor: sqlite3.Cursor) -> None:
                self._cursor = cursor

            def fetchall(self) -> list[sqlite3.Row]:
                try:
                    return self._cursor.fetchall()
                finally:
                    restore_sources()

        class ConnectionProxy:
            def __init__(self, connection: sqlite3.Connection) -> None:
                object.__setattr__(self, "_connection", connection)

            def __setattr__(self, name: str, value: object) -> None:
                setattr(self._connection, name, value)

            def execute(self, sql: str, *args: object) -> sqlite3.Cursor | CursorProxy:
                cursor = self._connection.execute(sql, *args)
                return CursorProxy(cursor) if sql.startswith("SELECT ") else cursor

            def close(self) -> None:
                restore_sources()
                self._connection.close()

        def swap_around_connect(*args: object, **kwargs: object) -> ConnectionProxy:
            os.replace(target_path, parked_path)
            os.replace(alternate_path, target_path)
            try:
                return ConnectionProxy(real_connect(*args, **kwargs))
            except Exception:
                restore_sources()
                raise

        monkeypatch.setattr(kanban_export.sqlite3, "connect", swap_around_connect)

        result = kanban_export.export_board_json("default")
        captured = capsys.readouterr()

        assert result == 4
        assert captured.out == ""
        assert captured.err == "error: kanban board 'default' is unavailable\n"
    finally:
        writer.close()


@pytest.mark.requires_wal
def test_export_never_places_raw_snapshot_in_caller_tmpdir_even_if_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    caller_tmp = home / "caller-tmp"
    caller_tmp.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    writer = kb.connect(board="default")
    private_sentinel = b"PRIVATE_TABLE_SENTINEL_MUST_NOT_ESCAPE_7f34c9"
    lock_path = home / "kanban-export.lock"
    lock_path.write_bytes(b"LOCK_SENTINEL_UNCHANGED")
    try:
        writer.execute("CREATE TABLE private_export_guard(secret BLOB NOT NULL)")
        writer.execute(
            "INSERT INTO private_export_guard VALUES (zeroblob(?))", (64 * 1024 * 1024,)
        )
        writer.execute("INSERT INTO private_export_guard VALUES (?)", (private_sentinel,))
        writer.execute(
            "INSERT INTO private_export_guard VALUES (zeroblob(?))", (128 * 1024 * 1024,)
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        task_id = kb.create_task(writer, title="committed WAL task after private table")
        writer.commit()

        db_path = home / "kanban.db"
        wal_path = home / "kanban.db-wal"
        shm_path = home / "kanban.db-shm"
        assert wal_path.is_file() and shm_path.is_file()
        with db_path.open("rb") as source, mmap.mmap(
            source.fileno(), 0, access=mmap.ACCESS_READ
        ) as raw:
            sentinel_offset = raw.find(private_sentinel)
        assert sentinel_offset >= 0
        assert private_sentinel not in wal_path.read_bytes()
        expected_rows = _writer_rows(writer)
        before = _inventory(home)

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "kanban",
                "--board",
                "default",
                "export",
                "--json",
            ],
            cwd=_WORKTREE,
            env=_env(home, TMPDIR=str(caller_tmp)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        copied_private_data = False
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            for snapshot_db in caller_tmp.glob("hermes-kanban-export-*/kanban.db"):
                try:
                    if snapshot_db.stat().st_size >= sentinel_offset + len(private_sentinel):
                        with snapshot_db.open("rb") as copied:
                            copied.seek(sentinel_offset)
                            copied_private_data = (
                                copied.read(len(private_sentinel)) == private_sentinel
                            )
                except FileNotFoundError:
                    continue
                if copied_private_data:
                    break
            if copied_private_data:
                process.terminate()
                break
            time.sleep(0.002)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)

        after = _inventory(home)
        assert _writer_rows(writer) == expected_rows
        for path, state in before.items():
            if path != "kanban.db-shm":
                assert after.get(path) == state
        assert after.get("kanban.db-shm", (None,))[0] == "file"
        escaped = []
        for path in home.rglob("*"):
            if path.is_file() and path != db_path and private_sentinel in path.read_bytes():
                escaped.append(path.relative_to(home).as_posix())
        residues = sorted(
            path.relative_to(home).as_posix()
            for path in caller_tmp.glob("hermes-kanban-export-*")
        )
        observed = {
            "raw_private_bytes_copied": copied_private_data,
            "private_sentinel_outside_source_db": escaped,
            "temporary_snapshot_residue": residues,
        }
        assert observed == {
            "raw_private_bytes_copied": False,
            "private_sentinel_outside_source_db": [],
            "temporary_snapshot_residue": [],
        }
        assert task_id in {row[0] for row in expected_rows["tasks"]}
    finally:
        writer.close()
