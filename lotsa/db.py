"""SQLite-backed task and message storage for the web dashboard.

Tasks and messages are stored in a local SQLite database at
``{data_dir}/lotsa.db`` (``data_dir`` defaults to ``~/.lotsa``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from lotsa.status import TaskStatusLiteral
from rigg.models import Item
from rigg.scrub import scrub_secrets

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'backlog',
    priority   INTEGER DEFAULT 0,
    flow_name  TEXT DEFAULT '',
    project_id TEXT NOT NULL REFERENCES projects(id),
    metadata   TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    role       TEXT NOT NULL,
    step_name  TEXT DEFAULT '',
    content    TEXT DEFAULT '',
    type       TEXT NOT NULL,
    metadata   TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_task_id ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
-- NOTE: no index on tasks(project_id) here. On an existing pre-ADR-029 DB the
-- tasks table still lacks project_id when _SCHEMA runs, and CREATE INDEX … IF
-- NOT EXISTS guards the index name, not the column — so it would throw "no such
-- column: project_id" before migrations run. _m004 owns that index (it recreates
-- tasks with project_id), covering both fresh and migrated DBs.
"""


@dataclass
class ProjectRow:
    """A registered project (ADR-029). ``name``/``path`` are mutable — a
    ``lotsa.yaml`` upsert overwrites them at startup — so the row carries
    ``updated_at`` per the DB conventions."""

    id: str
    name: str
    path: str
    created_at: str
    updated_at: str


@dataclass
class TaskRow:
    id: str
    title: str
    body: str
    state: str
    status: TaskStatusLiteral
    current_step: str | None
    priority: int
    flow_name: str
    project_id: str
    metadata: dict
    created_at: str
    updated_at: str


@dataclass
class MessageRow:
    id: int
    task_id: str
    role: str
    step_name: str
    content: str
    type: str
    metadata: dict
    created_at: str


# ── Atomic-transition types (ADR-020) ──────────────────────────────────


@dataclass
class TransitionResult:
    """Return value of :meth:`TaskDB.atomic_transition`.

    ``won`` is the authoritative signal — the caller must check it before
    executing any side effect. ``rowcount`` is the raw SQLite rowcount; it
    equals 1 when ``won`` is True and 0 otherwise.
    """

    won: bool
    rowcount: int  # raw — almost never inspected; won is the contract


class AuditPolicy(Enum):
    """Audit behaviour when the CAS in :meth:`TaskDB.atomic_transition` loses.

    SILENT — nothing is written. Default; appropriate for races between
        equivalent actors (e.g. two operator clicks).
    LOG_LOSS — a ``cas_loss`` system message is written in the same
        transaction as the (no-op) UPDATE so the loss is durable even
        though the state did not change.
    """

    SILENT = "silent"
    LOG_LOSS = "log_loss"


@dataclass
class AuditRow:
    """Descriptor for a ``messages`` row that :meth:`TaskDB.atomic_transition`
    writes atomically with the CAS UPDATE.

    ``step_name=None`` is stored as an empty string in the SQLite column —
    that is an internal detail callers do not reproduce.
    """

    role: Literal["system", "user", "agent", "github"]
    step_name: str | None  # None → empty string in DB
    content: str
    msg_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Typed transition shapes (ADR-020 Phase 3) ──────────────────────────


@dataclass(frozen=True)
class PushTransition:
    """Typed descriptor for a fixed-shape push CAS.

    Covers the ``pushing`` → next-state transitions in
    ``OrchestratorService._execute_push``. The ``(from, to)`` tuple is
    declared once as a named constant; call sites unpack it with
    ``**transition.kwargs()``. ``kwargs()`` owns the mapping into
    ``atomic_transition``'s parameter names so the descriptor's own
    ``name``/``from_``/``to_`` fields never leak into a CAS call.
    """

    name: str
    from_: tuple[TaskStatusLiteral, str]
    to_: tuple[TaskStatusLiteral, str, str | None]

    def kwargs(self) -> dict[str, Any]:
        """Unpack into the kwargs ``atomic_transition`` accepts."""
        from_status, from_state = self.from_
        to_status, to_state, to_current_step = self.to_
        return {
            "from_status": from_status,
            "from_state": from_state,
            "to_status": to_status,
            "to_state": to_state,
            "to_current_step": to_current_step,
        }


@dataclass(frozen=True)
class PrFixTransition:
    """Typed descriptor for the canonical pr-fix → blocked CAS.

    Documents the fixed built-in shape of the pr-fix round-cap transition.
    Note: the live cap-fire CAS in
    ``OrchestratorService._pr_fix_round_cap_blocked`` stays on raw kwargs
    rather than ``**PR_FIX_CAP_FIRE.kwargs()`` — it is shared across six
    entry points whose ``from_status``/``from_state`` are dynamic (monitor,
    answer, revise, send_message, retry, jump_to_step), so a fixed
    descriptor cannot stand in for it without breaking the cap on five of
    them. The constant remains the typed record of the built-in shape.
    """

    name: str
    from_: tuple[TaskStatusLiteral, str]
    to_: tuple[TaskStatusLiteral, str, str | None]

    def kwargs(self) -> dict[str, Any]:
        """Unpack into the kwargs ``atomic_transition`` accepts."""
        from_status, from_state = self.from_
        to_status, to_state, to_current_step = self.to_
        return {
            "from_status": from_status,
            "from_state": from_state,
            "to_status": to_status,
            "to_state": to_state,
            "to_current_step": to_current_step,
        }


# Named constants for the built-in state machines (push, pr-fix). One
# declaration per recurring fixed-shape (from, to) pair. YAML-flow sites
# pass raw strings — only built-in state names are stable enough to be
# Literal-tractable.

PUSH_START = PushTransition(
    name="push_start",
    from_=("working", "pushing"),
    to_=("working", "pushing", "push"),
)
"""CAS that claims the push slot at the start of ``_execute_push``."""

PUSH_SUCCESS = PushTransition(
    name="push_success",
    from_=("working", "pushing"),
    to_=("waiting_for_pr", "waiting_for_pr", "push"),
)
"""CAS that records a successful push and parks the task at ``waiting_for_pr``."""

PR_FIX_CAP_FIRE = PrFixTransition(
    name="pr_fix_cap_fire",
    from_=("working", "pr-fixing"),
    to_=("blocked", "blocked", "pr-fix"),
)
"""Documentation-only record of the pr-fix round-cap → ``blocked`` landing.

The ``to_`` triple (``blocked``/``blocked``/``pr-fix``) is the real fixed
shape: every cap-fire CAS lands there. The ``from_`` pair is **illustrative
only** — it names the nominal pr-fix active state, but neither live cap-fire
site uses these literal values:

* the drainer cap-fire (``_completion_drainer``) supplies
  ``from_status="waiting_for_pr"`` and a dynamic ``from_state=item.state``;
* ``_pr_fix_round_cap_blocked`` supplies a dynamic ``from_status``/
  ``from_state`` (its six entry points pass ``needs_input``, ``blocked``,
  ``waiting_for_pr``, etc.).

Because the entry side is dynamic at both sites, no single literal ``from_``
is correct and the constant is intentionally **not** wired via
``**PR_FIX_CAP_FIRE.kwargs()`` — see :class:`PrFixTransition`."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskDB:
    """Async wrapper around a SQLite database for task and message storage."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        from lotsa.migrations import apply_migrations

        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        apply_migrations(conn)
        return conn

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return self._conn

    # Concurrency invariant (audited): every method here is ``async def`` but
    # performs NO real suspension — ``_execute``/``_commit`` call straight into a
    # single shared sqlite3 connection. Because awaiting a coroutine that never
    # yields does not hand control back to the event loop, read-modify-write
    # sequences on the ``metadata`` column are atomic in practice. That safety is
    # implementation-dependent: if a method ever introduces a real suspension
    # (``asyncio.to_thread``, ``aiosqlite``, ``run_in_executor``), the
    # read-merge-write metadata pattern must first be converted to an atomic
    # single-statement merge (e.g. SQLite ``json_patch``). See audit finding #7.
    async def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    async def _executemany(self, sql: str, params_list: list[tuple]) -> None:
        self.conn.executemany(sql, params_list)

    async def _commit(self) -> None:
        # No-op with autocommit (isolation_level=None)
        pass

    # ── Tasks ──────────────────────────────────────────────────────────

    async def create_task(
        self,
        title: str,
        body: str = "",
        state: str = "backlog",
        status: TaskStatusLiteral = "working",
        current_step: str | None = None,
        priority: int = 0,
        flow_name: str = "",
        project_id: str = "default",
        metadata: dict | None = None,
    ) -> TaskRow:
        now = _now()
        meta_json = json.dumps(metadata or {})
        for _ in range(5):
            task_id = uuid.uuid4().hex[:8]
            try:
                cols = (
                    "id, title, body, state, status, current_step, "
                    "priority, flow_name, project_id, metadata, created_at, updated_at"
                )
                await self._execute(
                    f"INSERT INTO tasks ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        title,
                        body,
                        state,
                        status,
                        current_step,
                        priority,
                        flow_name,
                        project_id,
                        meta_json,
                        now,
                        now,
                    ),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("Failed to generate unique task ID after 5 attempts")
        await self._commit()
        return TaskRow(
            id=task_id,
            title=title,
            body=body,
            state=state,
            status=status,
            current_step=current_step,
            priority=priority,
            flow_name=flow_name,
            project_id=project_id,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )

    async def get_task(self, task_id: str) -> TaskRow | None:
        cur = await self._execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self,
        state: str | None = None,
        status: TaskStatusLiteral | None = None,
        status_not_in: Sequence[TaskStatusLiteral] | None = None,
    ) -> list[TaskRow]:
        base = (
            "SELECT t.*, COALESCE(m.last_msg, t.updated_at) AS _sort_key "
            "FROM tasks t "
            "LEFT JOIN (SELECT task_id, MAX(created_at) AS last_msg FROM messages GROUP BY task_id) m "
            "ON t.id = m.task_id"
        )
        clauses: list[str] = []
        params: list[str] = []
        if state is not None:
            clauses.append("t.state = ?")
            params.append(state)
        if status is not None:
            clauses.append("t.status = ?")
            params.append(status)
        if status_not_in:
            placeholders = ", ".join("?" for _ in status_not_in)
            clauses.append(f"t.status NOT IN ({placeholders})")
            params.extend(status_not_in)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = await self._execute(f"{base}{where} ORDER BY _sort_key DESC", tuple(params))
        return [self._row_to_task(r) for r in cur.fetchall()]

    async def update_task_state(self, task_id: str, state: str) -> None:
        await self._execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
            (state, _now(), task_id),
        )
        await self._commit()

    async def claim_task_transition(
        self,
        task_id: str,
        *,
        from_status: TaskStatusLiteral,
        from_state: str,
        to_state: str,
        to_status: TaskStatusLiteral,
        to_current_step: str | None,
    ) -> bool:
        """Atomically transition (status, state) — wins for exactly one caller.

        Closes the TOCTOU window in :meth:`OrchestratorService.approve` (and any
        other caller that needs to claim a row before dispatching). Two
        concurrent requests that both read ``status='waiting'`` will both reach
        this UPDATE, but only the first one matches the WHERE clause; the
        second sees ``rowcount == 0`` and the caller short-circuits.

        Returns True if this caller won the claim, False otherwise.
        """
        cur = await self._execute(
            "UPDATE tasks SET state = ?, status = ?, current_step = ?, updated_at = ? "
            "WHERE id = ? AND status = ? AND state = ?",
            (to_state, to_status, to_current_step, _now(), task_id, from_status, from_state),
        )
        await self._commit()
        return cur.rowcount == 1

    async def atomic_transition(
        self,
        task_id: str,
        *,
        from_status: TaskStatusLiteral,
        from_state: str,
        to_status: TaskStatusLiteral,
        to_state: str,
        to_current_step: str | None,
        audit_on_win: AuditRow | None,
        audit_on_loss: AuditPolicy = AuditPolicy.SILENT,
        to_metadata: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """CAS state transition with an optional audit row, written atomically.

        Either both the state change and the audit row land, or neither does.
        There is no "wrote the audit row, lost the CAS" or vice versa.

        ``audit_on_win`` — when supplied, an INSERT into ``messages`` is written
            in the same transaction as the CAS UPDATE, but only when the CAS wins.
        ``audit_on_loss`` — controls what (if anything) is written when the CAS
            loses. Defaults to ``AuditPolicy.SILENT`` (nothing written).
        ``to_metadata`` — when supplied, the row's ``metadata`` column is
            overwritten in the same UPDATE as the state change (only on a CAS
            win). ADR-027's ``promote_task`` uses this so the destination state
            and the new ``process_name`` land together — a reader never sees the
            destination state under the source process name.

        Returns a :class:`TransitionResult`. Callers must check ``result.won``
        before executing any side effect — the type makes this structurally
        visible where a raw ``bool`` return does not.
        """
        now = _now()
        self.conn.execute("BEGIN")
        try:
            if to_metadata is not None:
                cur = self.conn.execute(
                    "UPDATE tasks SET state = ?, status = ?, current_step = ?, metadata = ?, updated_at = ? "
                    "WHERE id = ? AND status = ? AND state = ?",
                    (
                        to_state,
                        to_status,
                        to_current_step,
                        json.dumps(to_metadata),
                        now,
                        task_id,
                        from_status,
                        from_state,
                    ),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE tasks SET state = ?, status = ?, current_step = ?, updated_at = ? "
                    "WHERE id = ? AND status = ? AND state = ?",
                    (to_state, to_status, to_current_step, now, task_id, from_status, from_state),
                )
            won = cur.rowcount == 1
            if won and audit_on_win is not None:
                self.conn.execute(
                    "INSERT INTO messages (task_id, role, step_name, content, type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        audit_on_win.role,
                        audit_on_win.step_name or "",
                        audit_on_win.content,
                        audit_on_win.msg_type,
                        json.dumps(audit_on_win.metadata),
                        now,
                    ),
                )
            elif not won and audit_on_loss == AuditPolicy.LOG_LOSS:
                content = f"CAS lost: {from_status}/{from_state} → {to_status}/{to_state}"
                self.conn.execute(
                    "INSERT INTO messages (task_id, role, step_name, content, type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        "system",
                        "",
                        content,
                        "cas_loss",
                        json.dumps(
                            {
                                "from_status": from_status,
                                "from_state": from_state,
                                "to_status": to_status,
                                "to_state": to_state,
                                "to_current_step": to_current_step,
                            }
                        ),
                        now,
                    ),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return TransitionResult(won=won, rowcount=cur.rowcount)

    async def update_task(self, task_id: str, **fields: object) -> None:
        allowed = {"title", "body", "state", "status", "current_step", "priority", "flow_name", "metadata"}
        bad_keys = set(fields) - allowed
        if bad_keys:
            raise ValueError(f"Invalid fields for task update: {bad_keys}")
        if not fields:
            return
        updates = dict(fields)
        if "metadata" in updates:
            updates["metadata"] = json.dumps(updates["metadata"])
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = tuple(updates.values()) + (task_id,)
        await self._execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await self._commit()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRow:
        return TaskRow(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            state=row["state"],
            status=row["status"],
            current_step=row["current_step"],
            priority=row["priority"],
            flow_name=row["flow_name"],
            project_id=row["project_id"],
            metadata=json.loads(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Projects (ADR-029) ─────────────────────────────────────────────

    async def upsert_project(self, project_id: str, name: str, path: str) -> ProjectRow:
        """Insert or update a project by id. ``name``/``path`` are overwritten
        on conflict (YAML is authoritative for YAML-declared projects);
        ``created_at`` is preserved across updates, ``updated_at`` moves
        forward."""
        now = _now()
        await self._execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "path = excluded.path, updated_at = excluded.updated_at",
            (project_id, name, path, now, now),
        )
        await self._commit()
        row = await self.get_project(project_id)
        if row is None:  # invariant: just upserted — but don't rely on assert (elided under -O)
            raise RuntimeError(f"upsert_project({project_id!r}) did not produce a row")
        return row

    async def get_project(self, project_id: str) -> ProjectRow | None:
        cur = await self._execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return ProjectRow(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_projects(self) -> list[ProjectRow]:
        cur = await self._execute("SELECT * FROM projects ORDER BY id ASC")
        return [
            ProjectRow(
                id=r["id"],
                name=r["name"],
                path=r["path"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in cur.fetchall()
        ]

    # ── Messages ───────────────────────────────────────────────────────

    async def add_message(
        self,
        task_id: str,
        role: str,
        step_name: str,
        content: str,
        msg_type: str,
        metadata: dict | None = None,
    ) -> MessageRow:
        # Backstop for CONSTITUTION §1.2: agent stdout/stderr/artifacts and audit
        # rows flow through here before they are persisted and served via the API,
        # so scrub credentials centrally — no individual call site can forget.
        content = scrub_secrets(content)
        now = _now()
        meta_json = json.dumps(metadata or {})
        cur = await self._execute(
            "INSERT INTO messages (task_id, role, step_name, content, type, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, role, step_name, content, msg_type, meta_json, now),
        )
        await self._commit()
        return MessageRow(
            id=cur.lastrowid or 0,
            task_id=task_id,
            role=role,
            step_name=step_name,
            content=content,
            type=msg_type,
            metadata=metadata or {},
            created_at=now,
        )

    async def get_messages(
        self,
        task_id: str,
        step_name: str | None = None,
        msg_type: str | None = None,
    ) -> list[MessageRow]:
        sql = "SELECT * FROM messages WHERE task_id = ?"
        params: list[str] = [task_id]
        if step_name is not None:
            sql += " AND step_name = ?"
            params.append(step_name)
        if msg_type is not None:
            sql += " AND type = ?"
            params.append(msg_type)
        sql += " ORDER BY created_at ASC, id ASC"
        cur = await self._execute(sql, tuple(params))
        return [self._row_to_message(r) for r in cur.fetchall()]

    async def get_message_by_id(self, task_id: str, message_id: int) -> MessageRow | None:
        """Return a single message scoped to *task_id*, or ``None`` if absent.

        Used by endpoints that look up a specific message — avoids loading
        every message for the task only to discard all but one. The
        ``task_id`` clause enforces scoping so a message that exists in
        another task is not visible here.
        """
        cur = await self._execute(
            "SELECT * FROM messages WHERE task_id = ? AND id = ?",
            (task_id, message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRow:
        return MessageRow(
            id=row["id"],
            task_id=row["task_id"],
            role=row["role"],
            step_name=row["step_name"],
            content=row["content"],
            type=row["type"],
            metadata=json.loads(row["metadata"]),
            created_at=row["created_at"],
        )


class SQLiteItemSource:
    """ItemSource protocol implementation backed by SQLite.

    Compatible with the rigg dispatch engine and state machine.
    """

    def __init__(self, db: TaskDB) -> None:
        self._db = db

    async def items_in_state(self, state: str) -> list[Item]:
        tasks = await self._db.list_tasks(state=state)
        return [
            Item(id=t.id, state=t.state, priority=t.priority, title=t.title, body=t.body, metadata=t.metadata)
            for t in tasks
        ]

    async def all_items(self) -> list[Item]:
        tasks = await self._db.list_tasks()
        return [
            Item(
                id=t.id,
                state=t.state,
                priority=t.priority,
                title=t.title,
                body=t.body,
                metadata=t.metadata,
            )
            for t in tasks
        ]

    async def save(self, item: Item) -> None:
        await self._db.update_task_state(item.id, item.state)

    def assign_id(self, item: Item) -> str:
        if item.id:
            return item.id
        item.id = uuid.uuid4().hex[:8]
        return item.id

    async def save_step_output(self, item_id: str, job_type: str, content: str, metadata: dict | None = None) -> None:
        """Store step output as a message."""
        await self._db.add_message(item_id, "agent", job_type, content, "output", metadata=metadata)

    async def save_artifact(self, item_id: str, job_type: str, content: str, metadata: dict | None = None) -> None:
        """Store an agent artifact (structured output) as a message."""
        await self._db.add_message(item_id, "agent", job_type, content, "artifact", metadata=metadata)

    async def save_stderr(self, item_id: str, job_type: str, content: str) -> None:
        """Store agent stderr as a message."""
        await self._db.add_message(item_id, "agent", job_type, content, "stderr")

    async def append_event(self, item_id: str, event: dict) -> None:
        """Store event as a system message."""
        content = json.dumps(event)
        await self._db.add_message(item_id, "system", "", content, "status_change")

    def step_output_path(self, item_id: str, job_type: str) -> Path:
        """Not supported — use get_messages instead."""
        raise NotImplementedError("SQLiteItemSource stores output in DB, not files. Use get_messages().")
