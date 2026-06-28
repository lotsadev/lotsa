"""Tests for the SQLite forward-only migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lotsa.migrations import apply_migrations, get_user_version


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _apply_through_m002(conn: sqlite3.Connection) -> None:
    """Run migrations only up to and including _m002.

    The ADR-029 _m004 clean break recreates ``tasks`` and drops every
    pre-multi-project row, so _m002's backfill is no longer observable after
    the full chain. Truncating the migration list lets these tests verify
    _m002's logic in isolation, exactly as before _m004 landed.
    """
    import lotsa.migrations as m

    original = list(m.MIGRATIONS)
    m.MIGRATIONS[:] = original[:2]
    try:
        apply_migrations(conn)
    finally:
        m.MIGRATIONS[:] = original


def test_get_user_version_starts_at_zero(tmp_path):
    conn = _open(tmp_path / "t.db")
    assert get_user_version(conn) == 0
    conn.close()


def test_migration_001_adds_status_and_current_step_columns(tmp_path):
    db = tmp_path / "t.db"
    conn = _open(db)
    # Pre-create the original schema (without the new columns).
    conn.execute(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '',"
        "  state TEXT NOT NULL DEFAULT 'backlog', priority INTEGER DEFAULT 0,"
        "  flow_name TEXT DEFAULT '', metadata TEXT DEFAULT '{}',"
        "  created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE messages ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
        "  role TEXT NOT NULL, step_name TEXT DEFAULT '', content TEXT DEFAULT '',"
        "  type TEXT NOT NULL, metadata TEXT DEFAULT '{}', created_at TEXT NOT NULL"
        ")"
    )
    apply_migrations(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "status" in cols
    assert "current_step" in cols
    assert get_user_version(conn) >= 1
    conn.close()


def test_migration_002_backfills_complete_tasks(tmp_path):
    db = tmp_path / "t.db"
    conn = _open(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
        "  state TEXT NOT NULL, priority INTEGER, flow_name TEXT, metadata TEXT,"
        "  created_at TEXT, updated_at TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
        "  role TEXT, step_name TEXT, content TEXT, type TEXT, metadata TEXT,"
        "  created_at TEXT);"
    )
    conn.execute("INSERT INTO tasks VALUES ('t1','Done','','complete',0,'simple','{}','2026-01-01','2026-01-01')")
    # Observe _m002's backfill in isolation: the ADR-029 _m004 clean break
    # recreates ``tasks`` and drops these rows, so the backfill is only
    # observable when the chain stops at _m002.
    _apply_through_m002(conn)

    row = conn.execute("SELECT status, current_step FROM tasks WHERE id='t1'").fetchone()
    assert row["status"] == "complete"
    assert row["current_step"] is None
    conn.close()


def test_migration_002_backfills_active_tasks_to_blocked_with_message(tmp_path):
    db = tmp_path / "t.db"
    conn = _open(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
        "  state TEXT NOT NULL, priority INTEGER, flow_name TEXT, metadata TEXT,"
        "  created_at TEXT, updated_at TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
        "  role TEXT, step_name TEXT, content TEXT, type TEXT, metadata TEXT,"
        "  created_at TEXT);"
    )
    conn.execute("INSERT INTO tasks VALUES ('t2','Active','','coding',0,'simple','{}','2026-01-01','2026-01-01')")
    # See _m002-in-isolation note above (ADR-029 _m004 drops tasks rows).
    _apply_through_m002(conn)

    row = conn.execute("SELECT status, current_step FROM tasks WHERE id='t2'").fetchone()
    assert row["status"] == "blocked"
    assert row["current_step"] == "coding"

    msg = conn.execute("SELECT content, type, role FROM messages WHERE task_id='t2'").fetchone()
    assert msg is not None
    assert msg["type"] == "status_change"
    assert msg["role"] == "system"
    assert "migrated" in msg["content"].lower()
    conn.close()


def test_apply_migrations_rolls_back_on_failure(tmp_path):
    """If a migration raises, no partial state is committed and user_version stays put."""
    from lotsa.migrations import MIGRATIONS, get_user_version, register

    db = tmp_path / "t.db"
    conn = _open(db)
    # Set up schema for the existing migrations to apply cleanly.
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
        "  state TEXT NOT NULL, priority INTEGER, flow_name TEXT, metadata TEXT,"
        "  created_at TEXT, updated_at TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
        "  role TEXT, step_name TEXT, content TEXT, type TEXT, metadata TEXT,"
        "  created_at TEXT);"
    )
    conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, n INTEGER)")
    conn.execute("INSERT INTO marker VALUES (1, 0)")

    # Bring user_version up to current so the failing one is the next applied.
    apply_migrations(conn)
    baseline_version = get_user_version(conn)
    baseline_n = conn.execute("SELECT n FROM marker WHERE id=1").fetchone()[0]

    # Register a migration that mutates and then raises. Restore on teardown.
    @register
    def _bad_migration(c):
        c.execute("UPDATE marker SET n = 99 WHERE id = 1")
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            apply_migrations(conn)
        assert get_user_version(conn) == baseline_version
        n_after = conn.execute("SELECT n FROM marker WHERE id=1").fetchone()[0]
        assert n_after == baseline_n
    finally:
        MIGRATIONS.pop()
        conn.close()


def test_migration_003_creates_status_index(tmp_path):
    """idx_tasks_status should exist after migrations run, so list_tasks
    queries with status= filters use an index scan instead of a table scan.
    """
    db = tmp_path / "t.db"
    conn = _open(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
        "  state TEXT NOT NULL, priority INTEGER, flow_name TEXT, metadata TEXT,"
        "  created_at TEXT, updated_at TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
        "  role TEXT, step_name TEXT, content TEXT, type TEXT, metadata TEXT,"
        "  created_at TEXT);"
    )
    apply_migrations(conn)

    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(tasks)")}
    assert "idx_tasks_status" in indexes
    # And the planner uses it for status= queries.
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE status = ?", ("waiting",)).fetchall()
    plan_text = " ".join(r["detail"] for r in plan)
    assert "idx_tasks_status" in plan_text, f"planner didn't use idx_tasks_status: {plan_text}"
    conn.close()
