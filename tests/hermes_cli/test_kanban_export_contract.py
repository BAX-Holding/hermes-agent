"""Immutable RED contract for the sanitized, read-only Kanban JSON edge."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_export


_WORKTREE = Path(__file__).resolve().parents[2]
_EXPORT_ARGV = ["kanban", "--board", "default", "export", "--json"]
_TASK_KEYS = {
    "id",
    "title",
    "assignee",
    "status",
    "priority_native",
    "project_id",
    "created_at",
    "started_at",
    "completed_at",
}
_EXPORT_LIMIT = 200


@pytest.fixture(autouse=True)
def _restore_process_kanban_state():
    previous_home = os.environ.get("HERMES_HOME")
    yield
    if previous_home is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = previous_home
    kb._INITIALIZED_PATHS.clear()


_SENSITIVE = (
    "SENTINEL_BODY",
    "SENTINEL_RESULT",
    "SENTINEL_SESSION",
    "SENTINEL_WORKSPACE",
    "SENTINEL_BRANCH",
    "SENTINEL_TENANT",
    "SENTINEL_CREATOR",
    "SENTINEL_SKILL",
    "SENTINEL_WORKFLOW",
    "SENTINEL_EVENT",
    "SENTINEL_COMMENT",
    "SENTINEL_OTHER_BOARD",
    "SENTINEL_UNKNOWN_STATUS",
    "SENTINEL_MALFORMED_TITLE",
)


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(home),
            "PYTHONPATH": str(_WORKTREE),
            # An export is a local DB read. Credentials are deliberately unusable.
            "OPENAI_API_KEY": "export-must-not-contact-a-model",
            "ANTHROPIC_API_KEY": "export-must-not-contact-a-model",
        }
    )
    env.update(extra)
    return env


def _export(
    home: Path, argv: list[str] | None = None, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *(argv or _EXPORT_ARGV)],
        cwd=_WORKTREE,
        env=_env(home, **extra_env),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _inventory(root: Path) -> dict[str, tuple[str, int, str]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, int, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("dir", 0, "")
        elif path.is_file():
            raw = path.read_bytes()
            result[relative] = ("file", len(raw), hashlib.sha256(raw).hexdigest())
        else:
            result[relative] = ("other", 0, "")
    return result


def _assert_inventory_unchanged_except_preexisting_shm(
    before: dict[str, tuple[str, int, str]],
    after: dict[str, tuple[str, int, str]],
    shm_relative_path: str = "kanban.db-shm",
) -> None:
    """Permit SQLite read-mark byte changes only in an existing canonical SHM."""
    assert before.get(shm_relative_path, (None,))[0] == "file"
    assert set(after) == set(before)
    assert after[shm_relative_path][0] == "file"
    assert {path: state for path, state in after.items() if path != shm_relative_path} == {
        path: state for path, state in before.items() if path != shm_relative_path
    }


def _db_state(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "task_events")
        }
    finally:
        conn.close()


def _assert_no_sensitive_text(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert all(sentinel not in combined for sentinel in _SENSITIVE)


def _assert_valid_snapshot(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {"schema_version", "board", "tasks", "truncated"}
    assert payload["schema_version"] == 1
    assert payload["board"] == "default"
    assert type(payload["truncated"]) is bool
    assert type(payload["tasks"]) is list
    for task in payload["tasks"]:
        assert set(task) == _TASK_KEYS
        assert type(task["id"]) is str and 1 <= len(task["id"]) <= 128
        assert type(task["title"]) is str and 1 <= len(task["title"]) <= 500
        assert task["assignee"] is None or (
            type(task["assignee"]) is str and len(task["assignee"]) <= 128
        )
        assert task["status"] in kb.VALID_STATUSES - {"archived"}
        assert type(task["priority_native"]) is int
        assert -1_000_000 <= task["priority_native"] <= 1_000_000
        assert task["project_id"] is None or (
            type(task["project_id"]) is str and len(task["project_id"]) <= 128
        )
        for field in ("created_at", "started_at", "completed_at"):
            assert task[field] is None or (
                type(task[field]) is int and 0 <= task[field] <= 2**63 - 1
            )
    return payload


def _close_setup_connection(conn: sqlite3.Connection) -> None:
    conn.commit()
    # Fold setup WAL bytes into the main DB and remove setup-owned sidecars.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_export_is_exact_sanitized_native_snapshot_without_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")

    conn = kb.connect(board="default")
    high = kb.create_task(
        conn,
        title="high priority",
        body="SENTINEL_BODY",
        assignee="alice",
        created_by="SENTINEL_CREATOR",
        workspace_kind="worktree",
        workspace_path="/tmp/SENTINEL_WORKSPACE",
        branch_name="SENTINEL_BRANCH",
        tenant="SENTINEL_TENANT",
        priority=9,
        skills=["SENTINEL_SKILL"],
        session_id="SENTINEL_SESSION",
    )
    parent = kb.create_task(conn, title="completed dependency", priority=5)
    child = kb.create_task(conn, title="blocked dependent", priority=5, parents=[parent])
    low = kb.create_task(conn, title="low priority", priority=1)
    archived = kb.create_task(conn, title="must not be exported", priority=99)
    assert kb.complete_task(conn, parent, result="SENTINEL_RESULT")
    kb.add_comment(conn, high, "operator", "SENTINEL_COMMENT")
    conn.execute(
        "UPDATE tasks SET created_at = CASE id "
        "WHEN ? THEN 300 WHEN ? THEN 200 WHEN ? THEN 250 WHEN ? THEN 100 ELSE 50 END, "
        "workflow_template_id = CASE WHEN id = ? THEN 'SENTINEL_WORKFLOW' ELSE workflow_template_id END "
        "WHERE id IN (?, ?, ?, ?, ?)",
        (high, parent, child, low, high, high, parent, child, low, archived),
    )
    # This is intentionally non-sticky: recompute_ready() would promote it.
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (child,))
    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (archived,))
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (high, "fixture", '{"detail":"SENTINEL_EVENT"}', 123),
    )
    _close_setup_connection(conn)

    kb.create_board("other")
    other = kb.connect(board="other")
    kb.create_task(other, title="SENTINEL_OTHER_BOARD", priority=999)
    _close_setup_connection(other)
    kb.set_current_board("other")

    db_path = home / "kanban.db"
    before_rows = _db_state(db_path)
    before_files = _inventory(home)
    result = _export(home, HERMES_KANBAN_BOARD="other")
    after_rows = _db_state(db_path)
    after_files = _inventory(home)

    # Safety is checked before the missing-feature assertion.
    _assert_no_sensitive_text(result)
    assert after_rows == before_rows
    assert after_files == before_files
    payload = _assert_valid_snapshot(result)
    assert payload["truncated"] is False
    assert [task["id"] for task in payload["tasks"]] == [high, parent, child, low]
    assert [task["status"] for task in payload["tasks"]] == [
        "ready",
        "done",
        "blocked",
        "ready",
    ]
    assert len(payload["tasks"]) == 4


def test_export_has_fixed_hard_cap_and_deterministic_truncation_marker(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    conn = kb.connect(board="default")
    task_ids = [
        kb.create_task(conn, title=f"task {index:03d}", priority=index % 7)
        for index in range(_EXPORT_LIMIT + 1)
    ]
    # Freeze unique creation times so the native order has no ties.
    conn.executemany(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        [(10_000 + index, task_id) for index, task_id in enumerate(task_ids)],
    )
    expected = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE status != 'archived' "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (_EXPORT_LIMIT,),
        )
    ]
    _close_setup_connection(conn)

    before = _inventory(home)
    first = _export(home)
    second = _export(home)
    assert _inventory(home) == before
    _assert_no_sensitive_text(first)
    _assert_no_sensitive_text(second)
    first_payload = _assert_valid_snapshot(first)
    second_payload = _assert_valid_snapshot(second)
    assert first.stdout == second.stdout
    assert first_payload["truncated"] is True
    assert len(first_payload["tasks"]) == _EXPORT_LIMIT
    assert [task["id"] for task in first_payload["tasks"]] == expected


def test_export_accepts_maximum_valid_escaped_wire_payload(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    conn = kb.connect(board="default")
    for index in range(_EXPORT_LIMIT + 1):
        original_id = kb.create_task(conn, title="placeholder", priority=index)
        task_id = f"{index:03d}" + "\x00" * 125
        conn.execute(
            "UPDATE tasks SET id = ?, title = ?, assignee = ?, project_id = ?, "
            "created_at = ?, started_at = ?, completed_at = ? WHERE id = ?",
            (
                task_id,
                "\x00" * 500,
                "\x00" * 128,
                "\x00" * 128,
                2**63 - 1,
                2**63 - 1,
                2**63 - 1,
                original_id,
            ),
        )
    _close_setup_connection(conn)

    result = _export(home)
    payload = _assert_valid_snapshot(result)

    assert payload["truncated"] is True
    assert len(payload["tasks"]) == _EXPORT_LIMIT
    assert len(result.stdout.encode("utf-8")) > 1_000_000


def test_export_uses_id_as_final_ascending_order_tiebreak(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    conn = kb.connect(board="default")
    later_id = kb.create_task(conn, title="sorts second", priority=7)
    earlier_id = kb.create_task(conn, title="sorts first", priority=7)
    conn.execute("UPDATE tasks SET id = 'tie-b', created_at = 123 WHERE id = ?", (later_id,))
    conn.execute("UPDATE tasks SET id = 'tie-a', created_at = 123 WHERE id = ?", (earlier_id,))
    _close_setup_connection(conn)

    first = _assert_valid_snapshot(_export(home))
    second = _assert_valid_snapshot(_export(home))

    assert [task["id"] for task in first["tasks"]] == ["tie-a", "tie-b"]
    assert second == first


@pytest.mark.parametrize(
    "argv",
    [
        ["kanban", "--board", "default", "export", "--json"],
        ["kanban", "--board=default", "export", "--json"],
    ],
    ids=["split-board", "equals-board"],
)
def test_try_early_export_accepts_only_documented_exact_shapes(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(kanban_export, "export_board_json", lambda board: calls.append(board) or 17)

    assert kanban_export.try_early_export(argv) == 17
    assert calls == ["default"]


@pytest.mark.parametrize(
    "argv",
    [
        ["kanban", "--board=", "export", "--json"],
        ["kanban", "export", "--board=default", "--json"],
        ["kanban", "--board=default", "--json", "export"],
        ["kanban", "--board", "default", "export", "--json", "--profile", "x"],
        ["kanban", "--board", "default", "export"],
    ],
    ids=[
        "empty-equals",
        "reordered-board",
        "reordered-json",
        "profile-permutation",
        "missing-json",
    ],
)
def test_try_early_export_rejects_malformed_or_unsupported_shapes(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(
        kanban_export,
        "export_board_json",
        lambda _board: pytest.fail("unsupported argv reached early export"),
    )

    assert kanban_export.try_early_export(argv) is None


def test_try_early_export_preserves_split_shape_empty_board_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(kanban_export, "export_board_json", lambda board: calls.append(board) or 4)

    assert kanban_export.try_early_export(["kanban", "--board", "", "export", "--json"]) == 4
    assert calls == [""]


@pytest.mark.parametrize(
    "argv",
    [
        ["kanban", "--board", "default", "export", "--json"],
        ["kanban", "--board=default", "export", "--json"],
    ],
    ids=["split-board", "equals-board"],
)
def test_export_shapes_run_before_startup_without_creating_home(
    tmp_path: Path, argv: list[str]
) -> None:
    home = tmp_path / "absent" / "hermes-home"
    assert not home.exists()

    result = _export(home, argv=argv, HERMES_KANBAN_BOARD="other")

    _assert_no_sensitive_text(result)
    assert not home.exists()
    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "error: kanban board 'default' is unavailable\n"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "SENTINEL_UNKNOWN_STATUS"),
        ("title", sqlite3.Binary(b"SENTINEL_MALFORMED_TITLE")),
    ],
    ids=["unknown-status", "malformed-title"],
)
def test_export_malformed_native_row_fails_closed(
    tmp_path: Path, column: str, value: object
) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    conn = kb.connect(board="default")
    task_id = kb.create_task(conn, title="safe title")
    conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))
    _close_setup_connection(conn)
    before_rows = _db_state(home / "kanban.db")
    before_files = _inventory(home)

    result = _export(home)

    _assert_no_sensitive_text(result)
    assert _db_state(home / "kanban.db") == before_rows
    assert _inventory(home) == before_files
    assert result.returncode == 5
    assert result.stdout == ""
    assert result.stderr == "error: kanban export contains invalid task data\n"
