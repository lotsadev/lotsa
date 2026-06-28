"""Tests for the shared agent-activity JSONL parser (ADR-017).

Covers the new ``rigg.activity`` module and the ``ActivityEvent`` /
``ActivityResult`` dataclasses: the Claude Code session-JSONL path encoding,
the block→event mapping table, truncation policy, incremental ``next_index``
reads, ``session_complete`` detection, and graceful degradation.

Imports of ``rigg.activity`` / the activity models are done INSIDE each
test (not at module top) to keep each test self-contained around the symbols it
exercises.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_ID = "sess-fixture-001"
COMPLETE_SESSION_ID = "sess-complete-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _activity():
    from rigg import activity

    return activity


def _place(home: Path, session_id: str, content: str, *, subdir: str = "encoded-workdir") -> None:
    """Write a session JSONL under a fake ``~/.claude/projects`` tree.

    Uses an arbitrary ``subdir`` name — the reader's encoding-drift glob
    fallback resolves the file by its globally-unique session id, so the
    exact encoded directory name is irrelevant to these read tests (the
    encoding itself is covered by ``test_encode_cwd_*`` below).
    """
    d = home / ".claude" / "projects" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(content)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_activity_event_dataclass_fields():
    from rigg.models import ActivityEvent

    ev = ActivityEvent(index=0, timestamp=datetime(2026, 6, 14), kind="text", summary="hi", detail=None)
    assert ev.index == 0
    assert ev.kind == "text"
    assert ev.summary == "hi"
    assert ev.detail is None


def test_activity_result_dataclass_fields():
    from rigg.models import ActivityResult

    res = ActivityResult(events=[], supported=False, session_complete=False, next_index=0)
    assert res.events == []
    assert res.supported is False
    assert res.session_complete is False
    assert res.next_index == 0


def test_activity_models_are_public_exports():
    import rigg

    assert hasattr(rigg, "ActivityEvent")
    assert hasattr(rigg, "ActivityResult")


# ---------------------------------------------------------------------------
# Path encoding — the spec's "/"→"-" rule is incomplete; "." also becomes "-"
# ---------------------------------------------------------------------------


def test_encode_cwd_replaces_slash_and_dot():
    # Verified against the real ~/.claude/projects/ on the host:
    # /Users/.../.lotsa/... encodes the dot of ".lotsa" to "-" too, yielding
    # the tell-tale "--lotsa". A "/"-only rule would be wrong.
    encoded = _activity().encode_cwd(Path("/Users/alice/.lotsa/worktrees/abcd1234"))
    assert encoded == "-Users-alice--lotsa-worktrees-abcd1234"


def test_session_jsonl_path_resolves_primary(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    work_dir = Path("/Users/alice/.lotsa/worktrees/abcd1234")
    encoded = "-Users-alice--lotsa-worktrees-abcd1234"
    target = home / ".claude" / "projects" / encoded
    target.mkdir(parents=True)
    (target / f"{SESSION_ID}.jsonl").write_text("{}\n")

    resolved = _activity().session_jsonl_path(work_dir, SESSION_ID)
    assert resolved == target / f"{SESSION_ID}.jsonl"


def test_session_jsonl_path_glob_fallback_by_session_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # File placed under a directory that does NOT match the encoding of work_dir;
    # the UUID-keyed glob fallback must still find it.
    _place(home, SESSION_ID, "{}\n", subdir="totally-different-dir")
    resolved = _activity().session_jsonl_path(Path("/some/other/work/dir"), SESSION_ID)
    assert resolved is not None
    assert resolved.name == f"{SESSION_ID}.jsonl"


def test_session_jsonl_path_missing_returns_none(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude" / "projects").mkdir(parents=True)
    assert _activity().session_jsonl_path(Path("/no/such/dir"), "missing-session") is None


# ---------------------------------------------------------------------------
# Block → event mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_activity_maps_all_block_kinds(tmp_path, monkeypatch):
    from rigg.models import ActivityResult

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)

    assert isinstance(res, ActivityResult)
    assert res.supported is True
    # queue-operation + attachment records are skipped; 6 events remain.
    kinds = [e.kind for e in res.events]
    assert kinds == ["thinking", "text", "tool_use", "tool_result", "tool_use", "tool_result"]


@pytest.mark.asyncio
async def test_read_activity_summaries_follow_adr_table(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)
    by_index = {e.index: e for e in res.events}

    assert by_index[0].summary == "Let me start by analyzing the problem."
    assert by_index[1].summary == "I'll read the config file first."
    assert by_index[2].summary == "Bash: pytest -q tests/test_foo.py"
    assert by_index[3].summary == "← ok"
    assert by_index[4].summary == "Read: /repo/config.py"
    assert by_index[5].summary == "← error"


@pytest.mark.asyncio
async def test_read_activity_indices_are_monotonic_and_timestamps_parsed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)

    assert [e.index for e in res.events] == [0, 1, 2, 3, 4, 5]
    assert all(isinstance(e.timestamp, datetime) for e in res.events)
    # next_index points one past the last emitted event.
    assert res.next_index == 6


@pytest.mark.asyncio
async def test_tool_result_detail_carries_ok_flag(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)
    ok_result = next(e for e in res.events if e.index == 3)
    err_result = next(e for e in res.events if e.index == 5)

    assert ok_result.detail is not None and ok_result.detail.get("ok") is True
    assert err_result.detail is not None and err_result.detail.get("ok") is False


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_tool_use_input_is_truncated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    long_cmd = "echo " + ("x" * 500)
    block = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": long_cmd}}
    record = {
        "type": "assistant",
        "timestamp": "2026-06-14T12:00:00.000Z",
        "sessionId": "sess-trunc",
        "message": {"role": "assistant", "content": [block]},
    }
    _place(home, "sess-trunc", json.dumps(record) + "\n")

    res = await _activity().read_activity("sess-trunc", Path("/w/d"), 0, 200)
    ev = res.events[0]
    assert ev.detail is not None
    assert ev.detail.get("truncated") is True
    # The full 500-char payload must not survive verbatim in the detail.
    assert len(json.dumps(ev.detail)) < len(long_cmd)


@pytest.mark.asyncio
async def test_long_text_is_truncated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    long_text = "y" * 4000
    record = {
        "type": "assistant",
        "timestamp": "2026-06-14T12:00:00.000Z",
        "sessionId": "sess-trunc2",
        "message": {"role": "assistant", "content": [{"type": "text", "text": long_text}]},
    }
    _place(home, "sess-trunc2", json.dumps(record) + "\n")

    res = await _activity().read_activity("sess-trunc2", Path("/w/d"), 0, 200)
    ev = res.events[0]
    assert ev.detail is not None
    assert ev.detail.get("truncated") is True


# ---------------------------------------------------------------------------
# Incremental reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_index_returns_only_newer_events(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 4, 200)
    assert [e.index for e in res.events] == [4, 5]
    assert res.next_index == 6


@pytest.mark.asyncio
async def test_limit_caps_batch_and_chains_next_index(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    first = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 2)
    assert [e.index for e in first.events] == [0, 1]
    assert first.next_index == 2

    second = await _activity().read_activity(SESSION_ID, Path("/w/d"), first.next_index, 2)
    assert [e.index for e in second.events] == [2, 3]
    assert second.next_index == 4


@pytest.mark.asyncio
async def test_limit_zero_does_not_wedge_polling(tmp_path, monkeypatch):
    """``limit <= 0`` must not silently drop every event and freeze next_index.

    Against the pre-fix code (``selected[:0] == []`` with ``next_index`` left at
    ``since_index``) this returned ``events == []`` and ``next_index == 0`` — a
    client polling with ``limit=0`` would never advance. The clamp to ``>= 1``
    keeps the cursor moving.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 0)
    assert len(res.events) >= 1
    assert res.next_index > 0


@pytest.mark.asyncio
async def test_negative_since_index_is_clamped_to_zero(tmp_path, monkeypatch):
    """A negative ``since_index`` floors to 0 and never leaks back via ``next_index``.

    Pre-fix, ``_read_activity_sync`` used the raw ``since_index``: the
    ``index >= since_index`` filter was trivially true (so a populated file
    returned the whole session, looking fine), but the missing-file / empty
    early returns set ``next_index = since_index`` — handing the caller a
    *negative* cursor it would echo back on every subsequent poll. Against the
    pre-fix code the missing-file assertion below failed with
    ``assert -5 == 0``. The clamp floors the cursor at 0 for every caller (the
    API route, the ``lotsa inspect`` CLI, the orchestrator).
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    # Populated file: a negative cursor behaves exactly like ``since_index == 0``.
    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), -5, 200)
    baseline = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)
    assert [e.index for e in res.events] == [e.index for e in baseline.events]
    assert res.next_index == baseline.next_index
    assert res.next_index >= 0

    # Missing file: the early-return cursor must be floored to 0, never echo the
    # negative input back (this is the assertion that fails pre-fix).
    missing = await _activity().read_activity("does-not-exist", Path("/w/d"), -5, 200)
    assert missing.next_index == 0


# ---------------------------------------------------------------------------
# Session-completion detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_record_marks_session_complete(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, COMPLETE_SESSION_ID, (FIXTURES / "session_complete.jsonl").read_text())

    res = await _activity().read_activity(COMPLETE_SESSION_ID, Path("/w/d"), 0, 200)
    assert res.session_complete is True
    # The summary record projects to a "system" event.
    system_events = [e for e in res.events if e.kind == "system" and e.summary == "Implemented feature X"]
    assert system_events
    # The summary JSONL record carries no ``timestamp`` of its own. It must
    # inherit the prior event's time (carry-forward), not fall back to
    # ``datetime.min`` — otherwise the dashboard renders it as "year 1".
    assert system_events[0].timestamp == datetime.fromisoformat("2026-06-14T11:00:00+00:00")
    assert system_events[0].timestamp != datetime.min


@pytest.mark.asyncio
async def test_no_summary_record_means_not_complete(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    res = await _activity().read_activity(SESSION_ID, Path("/w/d"), 0, 200)
    assert res.session_complete is False


# ---------------------------------------------------------------------------
# Graceful degradation — never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_session_file_returns_supported_but_empty(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude" / "projects").mkdir(parents=True)

    res = await _activity().read_activity("does-not-exist", Path("/w/d"), 0, 200)
    assert res.supported is True
    assert res.events == []


@pytest.mark.asyncio
async def test_malformed_line_does_not_crash_the_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    good = {
        "type": "assistant",
        "timestamp": "2026-06-14T12:00:00.000Z",
        "sessionId": "sess-bad",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "still here"}]},
    }
    content = "this is not json\n" + json.dumps(good) + "\n"
    _place(home, "sess-bad", content)

    res = await _activity().read_activity("sess-bad", Path("/w/d"), 0, 200)
    # The malformed line is skipped; the valid record still parses.
    assert any(e.summary == "still here" for e in res.events)


# ---------------------------------------------------------------------------
# Runner protocol surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_code_runner_read_activity_reads_session(tmp_path, monkeypatch):
    from rigg.agent_runner import ClaudeCodeRunner

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _place(home, SESSION_ID, (FIXTURES / "session_sample.jsonl").read_text())

    runner = ClaudeCodeRunner()
    res = await runner.read_activity(SESSION_ID, Path("/w/d"), 0, 200)
    assert res.supported is True
    assert len(res.events) == 6


@pytest.mark.asyncio
async def test_protocol_default_returns_unsupported():
    """A structural runner that does NOT override read_activity degrades.

    The Protocol carries a real default body (read-only, safe) returning
    supported=False, so callers that reach it get the documented empty shape.
    """
    from rigg.agent_runner import AgentRunner

    res = await AgentRunner.read_activity(object(), "sess", Path("/w/d"))
    assert res.supported is False
    assert res.events == []
