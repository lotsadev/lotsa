"""Tests for the agent-activity API endpoint, service method, and the
Docker runner's graceful degradation (ADR-017).

Exercises the endpoint, the ``OrchestratorService.get_agent_activity`` method,
and ``DockerAgentRunner.read_activity``.

These tests reuse the ``app_with_service`` / ``run`` fixtures from
``lotsa/tests/conftest.py`` (the service there dispatches through a
``FakeRunner`` that does NOT implement ``read_activity``, which is exactly
the "runner without activity support" degraded case).
"""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

# A minimal recorded Claude Code session: one Bash tool_use + its result.
SESSION_ID = "sess-api-001"
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
    + json.dumps(
        {
            "type": "user",
            "timestamp": "2026-06-14T10:00:04.000Z",
            "sessionId": SESSION_ID,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}],
            },
        }
    )
    + "\n"
)


def _place_session(home: Path, session_id: str, content: str) -> None:
    """Write a session JSONL under a fake ~/.claude/projects tree.

    The reader's UUID-keyed glob fallback resolves by session id, so the
    encoded directory name is irrelevant here.
    """
    d = home / ".claude" / "projects" / "encoded-workdir"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(content)


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestAgentActivityEndpoint:
    def test_unknown_task_returns_404(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            async with _client(app) as client:
                resp = await client.get("/api/tasks/nope/agent-activity")
                assert resp.status_code == 404
                assert resp.json()["detail"]["code"] == "TASK_NOT_FOUND"

        run(_test())

    def test_not_yet_dispatched_returns_empty_not_500(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            # No session_id in metadata → task has not dispatched yet.
            task = await service.db.create_task("No session")
            async with _client(app) as client:
                resp = await client.get(f"/api/tasks/{task.id}/agent-activity")
                assert resp.status_code == 200
                data = resp.json()
                assert data["session_id"] is None
                assert data["events"] == []

        run(_test())

    def test_degrades_when_runner_lacks_activity_support(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            # The conftest FakeRunner has no read_activity → supported=False.
            task = await service.db.create_task("FakeRunner task", metadata={"session_id": SESSION_ID})
            async with _client(app) as client:
                resp = await client.get(f"/api/tasks/{task.id}/agent-activity")
                assert resp.status_code == 200
                data = resp.json()
                assert data["runner_supports_activity"] is False
                assert data["events"] == []

        run(_test())

    def test_returns_events_for_claude_code_runner(self, app_with_service, run, tmp_path, monkeypatch):
        app, service = app_with_service

        async def _test():
            from rigg import ClaudeCodeRunner

            home = tmp_path / "home"
            monkeypatch.setenv("HOME", str(home))
            _place_session(home, SESSION_ID, _SAMPLE_JSONL)
            service.runner = ClaudeCodeRunner()  # reads JSONL; does not invoke the CLI

            task = await service.db.create_task("Active task", metadata={"session_id": SESSION_ID})
            async with _client(app) as client:
                resp = await client.get(f"/api/tasks/{task.id}/agent-activity")
                assert resp.status_code == 200
                data = resp.json()
                assert data["session_id"] == SESSION_ID
                assert data["runner_supports_activity"] is True
                assert len(data["events"]) == 2
                first = data["events"][0]
                assert first["summary"] == "Bash: pytest -q"
                assert set(first) >= {"index", "timestamp", "kind", "summary", "detail", "truncated"}
                assert data["next_index"] == 2

        run(_test())

    def test_never_500_on_runner_error(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            class RaisingRunner:
                def dispatch_shape_prompt(self) -> str:
                    return ""

                async def run(self, *a, **k):  # pragma: no cover - unused
                    raise AssertionError

                async def read_activity(self, *a, **k):
                    raise RuntimeError("boom")

            service.runner = RaisingRunner()
            task = await service.db.create_task("Boom task", metadata={"session_id": SESSION_ID})
            async with _client(app) as client:
                resp = await client.get(f"/api/tasks/{task.id}/agent-activity")
                assert resp.status_code == 200
                assert resp.json()["events"] == []

        run(_test())

    def test_since_query_param_is_honored(self, app_with_service, run, tmp_path, monkeypatch):
        app, service = app_with_service

        async def _test():
            from rigg import ClaudeCodeRunner

            home = tmp_path / "home"
            monkeypatch.setenv("HOME", str(home))
            _place_session(home, SESSION_ID, _SAMPLE_JSONL)
            service.runner = ClaudeCodeRunner()

            task = await service.db.create_task("Active task", metadata={"session_id": SESSION_ID})
            async with _client(app) as client:
                resp = await client.get(f"/api/tasks/{task.id}/agent-activity?since=2")
                assert resp.status_code == 200
                assert resp.json()["events"] == []

        run(_test())


class TestServiceGetAgentActivity:
    def test_unknown_task_returns_none(self, app_with_service, run):
        _, service = app_with_service

        async def _test():
            assert await service.get_agent_activity("does-not-exist", 0, 100) is None

        run(_test())

    def test_no_session_returns_empty_result(self, app_with_service, run):
        _, service = app_with_service

        async def _test():
            task = await service.db.create_task("No session")
            result = await service.get_agent_activity(task.id, 0, 100)
            assert result is not None
            session_id, activity = result
            assert session_id is None
            assert activity.events == []

        run(_test())

    def test_worktreeless_task_reads_from_project_work_dir(self, app_with_service, run):
        """ADR-044 Phase 3 — a worktree-less step (chat) runs in the project
        work_dir, so its session JSONL is keyed there, not under a namespaced
        worktree path. ``get_agent_activity`` must resolve the read directory the
        same way dispatch does (``get_path`` → ``_fallback_work_dir``) so the
        Activity tab reads the right directory instead of a nonexistent worktree.

        RED against the pre-Phase-3 fallback, which hardcoded
        ``data_dir/worktrees/<project>/<task_id>`` — with no worktree on disk
        ``get_path`` returns ``None``, so the read would target that nonexistent
        namespaced path instead of the project root the chat agent actually ran in.
        """
        from rigg.models import ActivityResult

        _, service = app_with_service

        async def _test():
            recorded: dict[str, Path] = {}

            class RecordingRunner:
                def dispatch_shape_prompt(self) -> str:
                    return ""

                async def run(self, *a, **k):  # pragma: no cover - unused
                    raise AssertionError

                async def read_activity(self, session_id, work_dir, since_index, limit):
                    recorded["work_dir"] = work_dir
                    return ActivityResult(events=[], supported=True)

            service.runner = RecordingRunner()
            # A dispatched task with a session but no worktree ever created (the
            # chat / needs_worktree=false shape).
            task = await service.db.create_task("Chatty", metadata={"session_id": SESSION_ID})
            row = await service.db.get_task(task.id)

            result = await service.get_agent_activity(task.id, 0, 100)
            assert result is not None

            # The read targets the project work_dir (fallback), NOT a namespaced
            # worktree path that no chat task ever populates.
            assert recorded["work_dir"] == service._fallback_work_dir(row)
            namespaced = service.config.data_dir / "worktrees" / row.project_id / task.id
            assert recorded["work_dir"] != namespaced

        run(_test())


class TestDockerRunnerActivity:
    def test_read_activity_supported_reads_persisted_home(self, run, tmp_path):
        # ADR-038: the agent HOME is mounted + persisted, so Docker activity is
        # now supported (was unsupported when the session lived in the --rm box).
        async def _test():
            from lotsa.docker_runner import DockerAgentRunner

            wt = tmp_path / "wd"
            wt.mkdir()
            res = await DockerAgentRunner().read_activity("sess", wt, 0, 200)
            assert res.supported is True
            assert res.events == []  # no session file yet → empty but supported

        run(_test())
