"""Tests for the ``lotsa inspect <task-id>`` CLI command (ADR-017).

``lotsa inspect`` reads the task's ``session_id`` from the SQLite metadata
and prints recent agent-activity events via the shared parser — no running
server required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lotsa.cli import cli
from lotsa.db import TaskDB


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Leave a fresh current event loop after each test.

    ``lotsa inspect`` (and this module's seed helper) run via ``asyncio.run``,
    which sets the main thread's current loop to ``None`` on exit. Sibling test
    modules that use the deprecated ``asyncio.get_event_loop()`` (e.g.
    ``test_push_pr_tool``) then raise "no current event loop" — a global-state
    leak across the in-process suite, not a product bug. Reinstating a loop
    keeps the suite order-independent.
    """
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


SESSION_ID = "sess-cli-001"
_SAMPLE_JSONL = (
    json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-06-14T10:00:03.000Z",
            "sessionId": SESSION_ID,
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}],
            },
        }
    )
    + "\n"
)


def _seed_task(data_dir: Path, *, session_id: str | None) -> str:
    metadata = {"session_id": session_id} if session_id else {}

    async def _seed() -> str:
        db = TaskDB(data_dir / "lotsa.db")
        await db.initialize()
        try:
            row = await db.create_task("Inspect me", metadata=metadata)
            return row.id
        finally:
            await db.close()

    return asyncio.run(_seed())


def _place_session(home: Path, session_id: str, content: str) -> None:
    d = home / ".claude" / "projects" / "encoded-workdir"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(content)


def test_inspect_prints_recent_events(tmp_path, monkeypatch):
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])

    task_id = _seed_task(data_dir, session_id=SESSION_ID)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place_session(home, SESSION_ID, _SAMPLE_JSONL)

    result = runner.invoke(cli, ["inspect", task_id, "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    assert "Bash: pytest -q" in result.output


def _multi_event_jsonl(n: int, *, complete: bool) -> str:
    """A session with *n* Bash tool_use events (commands ``cmd-0``..``cmd-{n-1}``).

    When *complete*, append the session-level ``summary`` record Claude Code
    writes on a clean exit (drives ``session_complete=True``).
    """
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-14T10:00:00.000Z",
                "sessionId": SESSION_ID,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"cmd-{i}"}}],
                },
            }
        )
        for i in range(n)
    ]
    if complete:
        lines.append(json.dumps({"type": "summary", "summary": "Session done"}))
    return "\n".join(lines) + "\n"


def test_inspect_prints_the_last_n_not_the_first_n(tmp_path, monkeypatch):
    """``--limit`` returns the tail of the session (ADR-017 §7), not the head.

    Against the pre-fix code (which sliced ``events[index>=0][:limit]``) this
    failed: the output contained ``cmd-0`` and omitted the most recent
    ``cmd-9``.
    """
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])
    task_id = _seed_task(data_dir, session_id=SESSION_ID)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # No ``summary`` record, so the events are exactly the ten Bash commands.
    _place_session(home, SESSION_ID, _multi_event_jsonl(10, complete=False))

    result = runner.invoke(cli, ["inspect", task_id, "--limit", "3", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    # The last three commands are present; earlier ones are not.
    assert "cmd-9" in result.output
    assert "cmd-8" in result.output
    assert "cmd-7" in result.output
    assert "cmd-0" not in result.output
    assert "cmd-6" not in result.output


def test_inspect_watch_drains_completed_session_backlog(tmp_path, monkeypatch):
    """``--watch`` on an already-complete session prints the tail and exits.

    Against the pre-fix code the loop broke on the first ``session_complete``
    after one ``limit``-sized batch, dropping the rest of the backlog. The fix
    drains fully before honouring completion; here ``--limit`` still bounds the
    initial tail, and the command terminates (no hang) because the session is
    complete.
    """
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])
    task_id = _seed_task(data_dir, session_id=SESSION_ID)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place_session(home, SESSION_ID, _multi_event_jsonl(5, complete=True))

    result = runner.invoke(cli, ["inspect", task_id, "--watch", "--limit", "2", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    # Tail of the completed session — the most recent command is shown (pre-fix
    # printed the head, ``cmd-0``, and stopped); the command terminates rather
    # than hanging because the session is complete.
    assert "cmd-4" in result.output
    assert "cmd-0" not in result.output


def test_inspect_rejects_non_positive_limit(tmp_path, monkeypatch):
    """``--limit 0``/negative is a usage error, not "print everything".

    Against the pre-fix code (``type=int``) the slice ``events[-limit:] if
    limit > 0 else events`` routed ``0`` (and ``events[-(-1):] == events[-1:]``
    notwithstanding, the ``> 0`` guard) to the *full-events* branch — so
    ``--limit 0`` and ``--limit -1`` both silently dumped the entire session
    with exit code 0. ``IntRange(min=1)`` now rejects them at parse time.
    """
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])
    task_id = _seed_task(data_dir, session_id=SESSION_ID)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place_session(home, SESSION_ID, _multi_event_jsonl(10, complete=False))

    for bad in ("0", "-1"):
        result = runner.invoke(cli, ["inspect", task_id, "--limit", bad, "--data-dir", str(data_dir)])
        # Click rejects an out-of-range option with a usage error (exit 2),
        # and no event is printed (pre-fix dumped all ten, e.g. ``cmd-9``).
        assert result.exit_code != 0, result.output
        assert "cmd-9" not in result.output


def test_inspect_reports_when_not_dispatched(tmp_path, monkeypatch):
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])

    task_id = _seed_task(data_dir, session_id=None)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    result = runner.invoke(cli, ["inspect", task_id, "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    assert "dispatch" in result.output.lower()
