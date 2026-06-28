"""Forward-only SQLite migration runner using PRAGMA user_version.

Each migration is a function ``(conn) -> None`` that performs schema
and data changes for the increment from version N-1 to version N.
The runner is idempotent: running it again on an up-to-date DB is a
no-op. It is invoked from ``TaskDB._open()`` so application code never
needs to think about it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

MigrationFn = Callable[[sqlite3.Connection], None]


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept parameter binding, but the int is locally
    # generated so format-string concat is safe here.
    conn.execute(f"PRAGMA user_version = {version}")


# Ordered list of migrations. Index 0 is migration "1" (0 → 1).
MIGRATIONS: list[MigrationFn] = []


def register(fn: MigrationFn) -> MigrationFn:
    """Decorator: append *fn* to the migration list."""
    MIGRATIONS.append(fn)
    return fn


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring *conn* to the latest schema version."""
    current = get_user_version(conn)
    target = len(MIGRATIONS)
    if current >= target:
        return
    for i in range(current, target):
        version = i + 1
        logger.info("Applying DB migration %d", version)
        conn.execute("BEGIN")
        try:
            MIGRATIONS[i](conn)
            _set_user_version(conn, version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


@register
def _m001_add_status_columns(conn: sqlite3.Connection) -> None:
    """Add ``status`` and ``current_step`` to ``tasks``.

    Default ``status`` is ``'working'`` so that the column is non-nullable;
    the backfill in :func:`_m002_backfill_status` rewrites every legacy row
    to a sane (current_step, status) pair before the orchestrator boots.
    """
    conn.execute("ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'working'")
    conn.execute("ALTER TABLE tasks ADD COLUMN current_step TEXT")


def _now_iso() -> str:
    """Return current time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


@register
def _m002_backfill_status(conn: sqlite3.Connection) -> None:
    """Rewrite every legacy row to a (current_step, status) pair.

    - ``state == 'complete'`` → ``status='complete'``, ``current_step=NULL``.
    - everything else        → ``status='blocked'``,  ``current_step=<state>``.

    A system message is appended explaining why the task is blocked. We
    deliberately don't try to guess whether the task was mid-execution or
    waiting for review at the time of the upgrade — the user retries
    explicitly.
    """
    rows = conn.execute("SELECT id, state FROM tasks").fetchall()
    now = _now_iso()
    for row in rows:
        task_id = row["id"]
        state = row["state"]
        if state == "complete":
            conn.execute(
                "UPDATE tasks SET status='complete', current_step=NULL WHERE id = ?",
                (task_id,),
            )
            continue
        conn.execute(
            "UPDATE tasks SET status='blocked', current_step=? WHERE id = ?",
            (state, task_id),
        )
        conn.execute(
            "INSERT INTO messages (task_id, role, step_name, content, type, metadata, created_at) "
            "VALUES (?, 'system', '', ?, 'status_change', '{}', ?)",
            (task_id, "Migrated by upgrade — click Retry to resume from this step.", now),
        )


@register
def _m003_index_status(conn: sqlite3.Connection) -> None:
    """Add an index on ``tasks.status``.

    Most reads (PrMonitor's per-poll ``list_tasks(status='waiting_for_pr')``,
    sidebar filtering, ``start()``'s "find any working tasks" sweep) filter
    on ``status``. Without an index this is a full scan; the table is small
    enough on CE that no one notices today, but the cost grows linearly with
    history retention. SQLite's ``CREATE INDEX IF NOT EXISTS`` is idempotent.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")


@register
def _m004_multi_project_clean_break(conn: sqlite3.Connection) -> None:
    """ADR-029 multi-project support, shipped as a pre-alpha CLEAN BREAK.

    Adds a ``projects`` table and a ``tasks.project_id`` (NOT NULL) FK. Rather
    than backfill existing tasks and fight SQLite's
    ``ALTER TABLE … ADD COLUMN … NOT NULL`` restriction, the ``tasks`` table is
    **recreated** with the new column — dropping any pre-multi-project rows.
    Lotsa is pre-release alpha with no install base to preserve, so the break
    is automatic rather than a manual "wipe ~/.lotsa" step (ADR-029 §2).

    The DB rows are dropped here; the matching on-disk cleanup — deleting the
    old flat ``data_dir/worktrees/`` tree whose git-registered worktree dirs
    would otherwise linger as orphans — happens at orchestrator startup
    (``OrchestratorService._sync_projects`` → ``_cleanup_legacy_worktrees``),
    where ``data_dir`` is in scope. The two halves are deliberately split
    across layers; this docstring is the pointer between them.

    The recreated ``tasks`` schema is the post-``_m001`` shape (base columns +
    ``status``/``current_step``) plus ``project_id``. FK enforcement stays off
    (today's default — no ``PRAGMA foreign_keys=ON``) so dropping/recreating
    ``tasks`` while ``messages.task_id`` references it is safe.

    The append-only ``messages`` log is cleared too: every pre-break message
    references a dropped task, so leaving them would strand orphaned audit rows
    (invisible through the API but accumulating as dead data). The append-only
    invariant governs application code (``add_message`` is INSERT-only); a
    one-time pre-alpha clean break is the legitimate layer to drop rows whose
    parent task no longer exists. ``messages`` is guaranteed present here —
    ``_SCHEMA`` creates it before ``apply_migrations`` runs, and ``_m002``
    already inserts into it — so the ``DELETE`` needs no existence guard.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projects ("
        "  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,"
        "  created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
        ")"
    )
    # Clean break: drop the old tasks table (and its indexes) and recreate it
    # with project_id. Pre-multi-project rows are intentionally not carried over.
    conn.execute("DROP TABLE IF EXISTS tasks")
    # Clear the now-orphaned messages (each referenced a just-dropped task).
    conn.execute("DELETE FROM messages")
    conn.execute(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  body TEXT DEFAULT '',"
        "  state TEXT NOT NULL DEFAULT 'backlog',"
        "  priority INTEGER DEFAULT 0,"
        "  flow_name TEXT DEFAULT '',"
        "  project_id TEXT NOT NULL REFERENCES projects(id),"
        "  metadata TEXT DEFAULT '{}',"
        "  created_at TEXT NOT NULL,"
        "  updated_at TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'working',"
        "  current_step TEXT"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
    # Index the project FK too: tasks are filtered by project (the dashboard
    # project filter, and ``_relocate_project``'s per-project task sweep), so
    # ``project_id`` joins ``status``/``state`` as an indexed dimension.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)")
