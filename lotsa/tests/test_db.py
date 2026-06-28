"""Tests for lotsa.db — SQLite task and message storage."""

from __future__ import annotations

import asyncio

import pytest

from lotsa.db import MessageRow, SQLiteItemSource, TaskDB, TaskRow


@pytest.fixture()
def _loop():
    """Provide a dedicated event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def db(tmp_path, _loop):
    """Create and initialize a TaskDB in a temp directory."""
    database = TaskDB(tmp_path / "lotsa.db")
    _loop.run_until_complete(database.initialize())
    yield database
    _loop.run_until_complete(database.close())


@pytest.fixture()
def run(_loop):
    """Helper to run async functions."""
    return _loop.run_until_complete


# ── TaskDB tests ───────────────────────────────────────────────────────


class TestTaskDB:
    def test_create_and_get_task(self, db, run):
        task = run(db.create_task("My Task", body="Do something"))
        assert isinstance(task, TaskRow)
        assert task.title == "My Task"
        assert task.body == "Do something"
        assert task.state == "backlog"
        assert len(task.id) == 8

        fetched = run(db.get_task(task.id))
        assert fetched is not None
        assert fetched.title == "My Task"
        assert fetched.id == task.id

    def test_get_task_not_found(self, db, run):
        assert run(db.get_task("nonexistent")) is None

    def test_list_tasks(self, db, run):
        run(db.create_task("Task A", priority=1))
        run(db.create_task("Task B", priority=2))
        run(db.create_task("Task C", priority=0))

        tasks = run(db.list_tasks())
        assert len(tasks) == 3
        # Sorted by most recent activity (latest message, falling back to updated_at).
        # Without messages, newest task is first.
        assert tasks[0].title == "Task C"
        assert tasks[2].title == "Task A"

    def test_update_task_state(self, db, run):
        task = run(db.create_task("Test"))
        run(db.update_task_state(task.id, "planning"))

        fetched = run(db.get_task(task.id))
        assert fetched is not None
        assert fetched.state == "planning"

    def test_update_task_fields(self, db, run):
        task = run(db.create_task("Old Title"))
        run(db.update_task(task.id, title="New Title", priority=5))

        fetched = run(db.get_task(task.id))
        assert fetched is not None
        assert fetched.title == "New Title"
        assert fetched.priority == 5

    def test_update_task_rejects_unknown_fields(self, db, run):
        task = run(db.create_task("Test"))
        with pytest.raises(ValueError, match="Invalid fields"):
            run(db.update_task(task.id, unknown_field="rejected"))

    def test_create_task_with_metadata(self, db, run):
        task = run(db.create_task("Test", metadata={"key": "value"}))
        fetched = run(db.get_task(task.id))
        assert fetched is not None
        assert fetched.metadata == {"key": "value"}

    def test_add_and_get_messages(self, db, run):
        task = run(db.create_task("Test"))
        msg = run(db.add_message(task.id, "agent", "plan", "Planning output", "output"))
        assert isinstance(msg, MessageRow)
        assert msg.role == "agent"
        assert msg.content == "Planning output"

        messages = run(db.get_messages(task.id))
        assert len(messages) == 1
        assert messages[0].content == "Planning output"

    def test_get_messages_filtered_by_step(self, db, run):
        task = run(db.create_task("Test"))
        run(db.add_message(task.id, "agent", "plan", "Plan output", "output"))
        run(db.add_message(task.id, "agent", "code", "Code output", "output"))

        plan_msgs = run(db.get_messages(task.id, step_name="plan"))
        assert len(plan_msgs) == 1
        assert plan_msgs[0].step_name == "plan"

    def test_get_messages_filtered_by_type(self, db, run):
        task = run(db.create_task("Test"))
        run(db.add_message(task.id, "agent", "plan", "Output", "output"))
        run(db.add_message(task.id, "agent", "plan", "Question?", "question"))

        questions = run(db.get_messages(task.id, msg_type="question"))
        assert len(questions) == 1
        assert questions[0].content == "Question?"

    def test_messages_ordered_chronologically(self, db, run):
        task = run(db.create_task("Test"))
        run(db.add_message(task.id, "agent", "plan", "First", "output"))
        run(db.add_message(task.id, "user", "plan", "Second", "answer"))
        run(db.add_message(task.id, "agent", "code", "Third", "output"))

        messages = run(db.get_messages(task.id))
        assert [m.content for m in messages] == ["First", "Second", "Third"]

    def test_get_message_by_id_returns_matching_row(self, db, run):
        """A single-row lookup scoped to a task, replacing list+scan."""
        task = run(db.create_task("Lookup"))
        m1 = run(db.add_message(task.id, "agent", "plan", "Plan output", "output"))
        m2 = run(db.add_message(task.id, "agent", "code", "Code output", "output"))

        got = run(db.get_message_by_id(task.id, m2.id))
        assert got is not None
        assert got.id == m2.id
        assert got.content == "Code output"

        # Verify the helper hits the right row even when the message
        # isn't the most recent.
        got1 = run(db.get_message_by_id(task.id, m1.id))
        assert got1 is not None
        assert got1.id == m1.id
        assert got1.content == "Plan output"

    def test_get_message_by_id_returns_none_for_missing_message(self, db, run):
        """Lookup with an unknown id returns None (not a row, not an exception)."""
        task = run(db.create_task("Missing"))
        run(db.add_message(task.id, "agent", "plan", "Output", "output"))

        got = run(db.get_message_by_id(task.id, 99999))
        assert got is None

    def test_get_message_by_id_scoped_to_task(self, db, run):
        """A message belonging to task A is invisible to task B."""
        task_a = run(db.create_task("A"))
        task_b = run(db.create_task("B"))
        msg_a = run(db.add_message(task_a.id, "agent", "code", "owned by A", "output"))

        got = run(db.get_message_by_id(task_b.id, msg_a.id))
        assert got is None

        got_a = run(db.get_message_by_id(task_a.id, msg_a.id))
        assert got_a is not None
        assert got_a.id == msg_a.id


# ── SQLiteItemSource tests ─────────────────────────────────────────────


class TestSQLiteItemSource:
    def test_items_in_state(self, db, run):
        run(db.create_task("Backlog task", state="backlog"))
        run(db.create_task("Planning task", state="planning"))
        run(db.create_task("Another backlog", state="backlog"))

        source = SQLiteItemSource(db)
        items = run(source.items_in_state("backlog"))
        assert len(items) == 2
        assert all(i.state == "backlog" for i in items)

    def test_all_items(self, db, run):
        run(db.create_task("A"))
        run(db.create_task("B"))

        source = SQLiteItemSource(db)
        items = run(source.all_items())
        assert len(items) == 2

    def test_save_updates_state(self, db, run):
        task = run(db.create_task("Test", state="backlog"))

        source = SQLiteItemSource(db)
        from rigg.models import Item

        item = Item(id=task.id, state="planning", title="Test")
        run(source.save(item))

        fetched = run(db.get_task(task.id))
        assert fetched is not None
        assert fetched.state == "planning"

    def test_assign_id(self, db, run):
        from rigg.models import Item

        source = SQLiteItemSource(db)
        item = Item(id="", state="backlog")
        new_id = source.assign_id(item)
        assert len(new_id) == 8
        assert item.id == new_id

        # Existing ID is preserved
        item2 = Item(id="existing", state="backlog")
        assert source.assign_id(item2) == "existing"


class TestSaveArtifact:
    def test_save_artifact_stores_message(self, db, run):
        task = run(db.create_task("Test"))
        source = SQLiteItemSource(db)

        async def _test():
            await source.save_artifact(task.id, "plan", "# Plan\n\nDo the thing")
            messages = await db.get_messages(task.id, msg_type="artifact")
            assert len(messages) == 1
            assert messages[0].type == "artifact"
            assert messages[0].step_name == "plan"
            assert "# Plan" in messages[0].content

        run(_test())

    def test_save_named_artifact(self, db, run):
        task = run(db.create_task("Test"))
        source = SQLiteItemSource(db)

        async def _test():
            await source.save_artifact(task.id, "spec", "# Spec content", metadata={"artifact_name": "spec"})
            messages = await db.get_messages(task.id, msg_type="artifact")
            assert len(messages) == 1
            assert messages[0].metadata["artifact_name"] == "spec"

        run(_test())


def test_create_task_persists_default_status_and_current_step(tmp_path, run):
    from lotsa.db import TaskDB

    db = TaskDB(tmp_path / "lotsa.db")
    run(db.initialize())
    task = run(db.create_task(title="x", flow_name="simple"))
    fresh = run(db.get_task(task.id))
    assert fresh.status == "working"
    assert fresh.current_step is None
    run(db.close())


def test_update_task_accepts_status_and_current_step(tmp_path, run):
    from lotsa.db import TaskDB

    db = TaskDB(tmp_path / "lotsa.db")
    run(db.initialize())
    task = run(db.create_task(title="x", flow_name="simple"))
    run(db.update_task(task.id, status="waiting", current_step="spec"))
    fresh = run(db.get_task(task.id))
    assert fresh.status == "waiting"
    assert fresh.current_step == "spec"
    run(db.close())


def test_initialize_upgrades_existing_pre_adr029_db(tmp_path, run):
    """Launching post-ADR-029 against an existing pre-029 DB must succeed.

    Regression: ``_SCHEMA`` ran ``CREATE INDEX … ON tasks(project_id)`` via
    ``executescript`` before migrations. On an existing DB the tasks table still
    lacked project_id, and ``IF NOT EXISTS`` guards the index name, not the
    column — so initialize() raised ``no such column: project_id`` before _m004
    could recreate the table, leaving ``projects`` created, user_version stuck
    at 3, and tasks un-migrated.
    """
    import sqlite3

    db_path = tmp_path / "lotsa.db"
    # Reconstruct the exact pre-ADR-029 on-disk state: tasks at the post-_m003
    # shape (base cols + status/current_step, no project_id), user_version=3.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '',"
        "  state TEXT NOT NULL DEFAULT 'backlog', priority INTEGER DEFAULT 0,"
        "  flow_name TEXT DEFAULT '', metadata TEXT DEFAULT '{}',"
        "  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'working', current_step TEXT);"
        "CREATE TABLE messages ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
        "  role TEXT NOT NULL, step_name TEXT DEFAULT '', content TEXT DEFAULT '',"
        "  type TEXT NOT NULL, metadata TEXT DEFAULT '{}', created_at TEXT NOT NULL);"
        "CREATE INDEX idx_tasks_state ON tasks(state);"
        "CREATE INDEX idx_tasks_status ON tasks(status);"
    )
    conn.execute(
        "INSERT INTO tasks (id,title,state,created_at,updated_at) "
        "VALUES ('old1','Legacy','coding','2026-01-01','2026-01-01')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    # The real launch path must not raise (pre-fix: OperationalError here).
    db = TaskDB(db_path)
    run(db.initialize())
    run(db.close())

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "project_id" in cols, f"_m004 must add project_id; got {cols}"
    conn.close()


# ── atomic_transition tests (ADR-020) ─────────────────────────────────


class TestAtomicTransitionTypes:
    """TransitionResult, AuditPolicy, and AuditRow are importable and correct."""

    def test_transition_result_importable_and_fields(self):
        from lotsa.db import TransitionResult

        r = TransitionResult(won=True, rowcount=1)
        assert r.won is True
        assert r.rowcount == 1

    def test_transition_result_loss(self):
        from lotsa.db import TransitionResult

        r = TransitionResult(won=False, rowcount=0)
        assert r.won is False
        assert r.rowcount == 0

    def test_audit_policy_values(self):
        from lotsa.db import AuditPolicy

        assert AuditPolicy.SILENT.value == "silent"
        assert AuditPolicy.LOG_LOSS.value == "log_loss"

    def test_audit_row_defaults_empty_metadata(self):
        from lotsa.db import AuditRow

        row = AuditRow(role="system", step_name=None, content="ok", msg_type="status_change")
        assert row.metadata == {}

    def test_audit_row_step_name_none_allowed(self):
        from lotsa.db import AuditRow

        row = AuditRow(role="system", step_name=None, content="x", msg_type="t")
        assert row.step_name is None

    def test_audit_row_accepts_all_roles(self):
        from lotsa.db import AuditRow

        for role in ("system", "user", "agent", "github"):
            row = AuditRow(role=role, step_name="s", content="c", msg_type="t")
            assert row.role == role


class TestAtomicTransition:
    """TaskDB.atomic_transition — all code paths."""

    def test_win_no_audit_updates_state(self, db, run):
        task = run(db.create_task("T", state="planned", status="waiting"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
            )
        )
        assert result.won is True
        assert result.rowcount == 1
        updated = run(db.get_task(task.id))
        assert updated.state == "coding"
        assert updated.status == "working"
        assert updated.current_step == "code"

    def test_win_no_audit_writes_no_messages(self, db, run):
        task = run(db.create_task("T", state="planned", status="waiting"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
            )
        )
        msgs = run(db.get_messages(task.id))
        assert msgs == []

    def test_win_with_audit_writes_message(self, db, run):
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="waiting"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name=None,
                    content="stage transition",
                    msg_type="status_change",
                ),
            )
        )
        assert result.won is True
        msgs = run(db.get_messages(task.id))
        assert len(msgs) == 1
        assert msgs[0].content == "stage transition"
        assert msgs[0].type == "status_change"
        assert msgs[0].role == "system"

    def test_win_with_audit_step_name_none_stored_as_empty_string(self, db, run):
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="waiting"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name=None,
                    content="hello",
                    msg_type="status_change",
                ),
            )
        )
        msgs = run(db.get_messages(task.id))
        # The DB column stores "" for None — callers use None; the column stores ""
        assert msgs[0].step_name == ""

    def test_win_with_audit_step_name_string_preserved(self, db, run):
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="waiting"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="code",
                    content="entered coding",
                    msg_type="stage_transition",
                ),
            )
        )
        msgs = run(db.get_messages(task.id))
        assert msgs[0].step_name == "code"

    def test_win_with_audit_metadata_round_trips(self, db, run):
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="waiting"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="code",
                    content="entered coding",
                    msg_type="stage_transition",
                    metadata={"step": "code", "round": 3},
                ),
            )
        )
        msgs = run(db.get_messages(task.id))
        assert msgs[0].metadata == {"step": "code", "round": 3}

    def test_win_with_audit_state_and_message_visible_in_same_read(self, db, run):
        """Both the state change and the audit row must be visible together — atomicity check."""
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="waiting"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="code",
                    content="entered coding",
                    msg_type="stage_transition",
                ),
            )
        )
        assert result.won is True
        # Read back in a single consistent pass — both must be present
        task_after = run(db.get_task(task.id))
        msgs = run(db.get_messages(task.id))
        assert task_after.state == "coding"
        assert len(msgs) == 1
        assert msgs[0].content == "entered coding"

    # ── ADR-027: optional ``to_metadata`` folds a metadata write into the CAS ──

    def test_win_with_to_metadata_updates_metadata_atomically(self, db, run):
        """``promote_task`` needs the process-name swap to land in the same
        UPDATE as the state change, so a reader never sees the destination
        state under the source ``process_name``.

        Fails pre-fix: ``atomic_transition`` has no ``to_metadata`` param
        (TypeError: unexpected keyword argument)."""
        task = run(db.create_task("T", state="speccing", status="working", metadata={"process_name": "chat"}))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="working",
                from_state="speccing",
                to_status="working",
                to_state="planning",
                to_current_step="plan",
                to_metadata={"process_name": "full", "current_flow": "main"},
                audit_on_win=None,
            )
        )
        assert result.won is True
        after = run(db.get_task(task.id))
        assert after.state == "planning"
        assert after.metadata == {"process_name": "full", "current_flow": "main"}

    def test_loss_does_not_write_to_metadata(self, db, run):
        """When the CAS loses, ``to_metadata`` must not be applied — the row's
        metadata stays exactly as it was."""
        task = run(db.create_task("T", state="speccing", status="working", metadata={"process_name": "chat"}))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="working",
                from_state="WRONG_STATE",  # forces a CAS loss
                to_status="working",
                to_state="planning",
                to_current_step="plan",
                to_metadata={"process_name": "full"},
                audit_on_win=None,
            )
        )
        assert result.won is False
        after = run(db.get_task(task.id))
        assert after.metadata == {"process_name": "chat"}
        assert after.state == "speccing"

    def test_loss_wrong_from_status_returns_false(self, db, run):
        task = run(db.create_task("T", state="planned", status="working"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong — task is "working"
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
            )
        )
        assert result.won is False
        assert result.rowcount == 0

    def test_loss_state_unchanged(self, db, run):
        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
            )
        )
        task_after = run(db.get_task(task.id))
        assert task_after.state == "planned"
        assert task_after.status == "working"

    def test_loss_silent_writes_no_messages(self, db, run):
        from lotsa.db import AuditRow

        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong — causes CAS loss
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name=None,
                    content="should not appear",
                    msg_type="status_change",
                ),
            )
        )
        # Default is SILENT — no messages at all
        assert run(db.get_messages(task.id)) == []

    def test_loss_audit_on_win_not_written_on_loss(self, db, run):
        """audit_on_win must NOT be written when the CAS loses."""
        from lotsa.db import AuditPolicy, AuditRow

        task = run(db.create_task("T", state="planned", status="working"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=AuditRow(
                    role="system",
                    step_name=None,
                    content="must not appear",
                    msg_type="status_change",
                ),
                audit_on_loss=AuditPolicy.SILENT,
            )
        )
        assert result.won is False
        assert run(db.get_messages(task.id)) == []

    def test_loss_log_loss_writes_cas_loss_message(self, db, run):
        from lotsa.db import AuditPolicy

        task = run(db.create_task("T", state="planned", status="working"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
                audit_on_loss=AuditPolicy.LOG_LOSS,
            )
        )
        assert result.won is False
        msgs = run(db.get_messages(task.id))
        assert len(msgs) == 1
        assert msgs[0].type == "cas_loss"
        assert msgs[0].role == "system"

    def test_loss_log_loss_content_describes_attempted_transition(self, db, run):
        from lotsa.db import AuditPolicy

        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
                audit_on_loss=AuditPolicy.LOG_LOSS,
            )
        )
        msgs = run(db.get_messages(task.id))
        # Content must name both sides of the transition
        assert "waiting" in msgs[0].content
        assert "planned" in msgs[0].content
        assert "working" in msgs[0].content
        assert "coding" in msgs[0].content

    def test_loss_log_loss_metadata_contains_all_transition_coords(self, db, run):
        from lotsa.db import AuditPolicy

        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
                audit_on_loss=AuditPolicy.LOG_LOSS,
            )
        )
        msgs = run(db.get_messages(task.id))
        meta = msgs[0].metadata
        assert meta["from_status"] == "waiting"
        assert meta["from_state"] == "planned"
        assert meta["to_status"] == "working"
        assert meta["to_state"] == "coding"
        assert meta["to_current_step"] == "code"

    def test_loss_log_loss_step_name_is_empty_string(self, db, run):
        """LOG_LOSS rows always use step_name='' per the uniform schema."""
        from lotsa.db import AuditPolicy

        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
                audit_on_loss=AuditPolicy.LOG_LOSS,
            )
        )
        msgs = run(db.get_messages(task.id))
        assert msgs[0].step_name == ""

    def test_default_audit_on_loss_is_silent(self, db, run):
        """Omitting audit_on_loss must default to SILENT (no message written on loss)."""
        task = run(db.create_task("T", state="planned", status="working"))
        run(
            db.atomic_transition(
                task.id,
                from_status="waiting",  # wrong — CAS will lose
                from_state="planned",
                to_status="working",
                to_state="coding",
                to_current_step="code",
                audit_on_win=None,
                # audit_on_loss omitted — should default to SILENT
            )
        )
        assert run(db.get_messages(task.id)) == []

    def test_to_current_step_none_stored_correctly(self, db, run):
        """to_current_step=None is a valid value (e.g. terminal transitions)."""
        task = run(db.create_task("T", state="planned", status="waiting"))
        result = run(
            db.atomic_transition(
                task.id,
                from_status="waiting",
                from_state="planned",
                to_status="complete",
                to_state="complete",
                to_current_step=None,
                audit_on_win=None,
            )
        )
        assert result.won is True
        updated = run(db.get_task(task.id))
        assert updated.current_step is None
        assert updated.status == "complete"


class TestTypedTransitionConstants:
    """Phase 3 (ADR-020) — PushTransition, PrFixTransition, and named constants.

    The typed descriptors wrap a fixed-shape CAS so the (from, to) tuple is
    declared once. ``kwargs()`` unpacks into the keyword names that
    ``TaskDB.atomic_transition`` accepts — deliberately excluding the
    descriptor's own ``name``/``from_``/``to_`` fields.
    """

    def test_push_transition_kwargs_unpacks_correctly(self):
        from lotsa.db import PUSH_SUCCESS, PushTransition

        assert isinstance(PUSH_SUCCESS, PushTransition)
        kwargs = PUSH_SUCCESS.kwargs()
        assert kwargs["from_status"] == "working"
        assert kwargs["from_state"] == "pushing"
        assert kwargs["to_status"] == "waiting_for_pr"
        assert kwargs["to_state"] == "waiting_for_pr"
        assert kwargs["to_current_step"] == "push"

    def test_push_start_kwargs(self):
        from lotsa.db import PUSH_START, PushTransition

        assert isinstance(PUSH_START, PushTransition)
        kwargs = PUSH_START.kwargs()
        assert kwargs["from_status"] == "working"
        assert kwargs["from_state"] == "pushing"
        assert kwargs["to_status"] == "working"
        assert kwargs["to_state"] == "pushing"
        assert kwargs["to_current_step"] == "push"

    def test_pr_fix_cap_fire_kwargs(self):
        from lotsa.db import PR_FIX_CAP_FIRE, PrFixTransition

        assert isinstance(PR_FIX_CAP_FIRE, PrFixTransition)
        kwargs = PR_FIX_CAP_FIRE.kwargs()
        assert kwargs["from_status"] == "working"
        assert kwargs["from_state"] == "pr-fixing"
        assert kwargs["to_status"] == "blocked"
        assert kwargs["to_state"] == "blocked"
        assert kwargs["to_current_step"] == "pr-fix"

    def test_transition_name_not_exposed_by_kwargs(self):
        from lotsa.db import PUSH_SUCCESS

        kwargs = PUSH_SUCCESS.kwargs()
        assert "name" not in kwargs
        assert "from_" not in kwargs
        assert "to_" not in kwargs

    def test_kwargs_keys_are_exactly_the_atomic_transition_params(self):
        from lotsa.db import PR_FIX_CAP_FIRE, PUSH_START, PUSH_SUCCESS

        expected = {"from_status", "from_state", "to_status", "to_state", "to_current_step"}
        for const in (PUSH_START, PUSH_SUCCESS, PR_FIX_CAP_FIRE):
            assert set(const.kwargs()) == expected

    def test_descriptors_are_frozen(self):
        import dataclasses

        from lotsa.db import PUSH_START

        with pytest.raises(dataclasses.FrozenInstanceError):
            PUSH_START.name = "mutated"  # type: ignore[misc]

    def test_push_start_kwargs_unpacks_into_atomic_transition(self, db, run):
        """The consumer contract: ``**PUSH_START.kwargs()`` is accepted by
        ``atomic_transition`` and lands the transition (AC: call sites use
        ``**transition.kwargs()``)."""
        from lotsa.db import PUSH_START

        task = run(db.create_task("Pushy", state="pushing", status="working"))
        result = run(
            db.atomic_transition(
                task.id,
                **PUSH_START.kwargs(),
                audit_on_win=None,
            )
        )
        assert result.won is True
        updated = run(db.get_task(task.id))
        assert updated.state == "pushing"
        assert updated.status == "working"
        assert updated.current_step == "push"


class TestChatMessageMetadata:
    """Verify that chat-message metadata round-trips through add_message."""

    @pytest.mark.asyncio
    async def test_duration_ms_written_to_chat_message_metadata(self, tmp_path):
        """The chat message metadata must contain duration_ms from AgentResult."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "tasks").mkdir()

        db = TaskDB(data_dir / "lotsa.db")
        await db.initialize()

        task = await db.create_task("test task", "body")

        expected_meta = {
            "duration_ms": 4500,
            "model": "sonnet",
            "runner": "ClaudeCodeRunner",
            "input_tokens": 300,
            "output_tokens": 150,
            "cost_usd": 0.008,
        }
        await db.add_message(
            task.id,
            "agent",
            "chat",
            "Agent response text",
            "chat",
            metadata=expected_meta,
        )

        messages = await db.get_messages(task.id)
        chat_msgs = [m for m in messages if m.type == "chat" and m.role == "agent"]
        assert len(chat_msgs) == 1
        meta = chat_msgs[0].metadata
        assert meta["duration_ms"] == 4500
        assert meta["model"] == "sonnet"
        assert meta["runner"] == "ClaudeCodeRunner"
        assert meta["input_tokens"] == 300
        assert meta["output_tokens"] == 150
        assert meta["cost_usd"] == pytest.approx(0.008)

    @pytest.mark.asyncio
    async def test_missing_token_fields_omitted_from_metadata(self, tmp_path):
        """When tokens are None, they must not appear as null keys in metadata."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "tasks").mkdir()

        db = TaskDB(data_dir / "lotsa.db")
        await db.initialize()
        task = await db.create_task("test task", "body")

        meta = {"duration_ms": 1000, "model": "sonnet", "runner": "ClaudeCodeRunner"}
        await db.add_message(task.id, "agent", "chat", "Response", "chat", metadata=meta)

        messages = await db.get_messages(task.id)
        chat_msgs = [m for m in messages if m.type == "chat" and m.role == "agent"]
        saved_meta = chat_msgs[0].metadata
        assert "input_tokens" not in saved_meta
        assert "output_tokens" not in saved_meta
