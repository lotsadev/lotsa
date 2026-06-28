"""Docker runner reads agent activity from the persisted per-task HOME (ADR-017/038)."""

from __future__ import annotations

import json
from pathlib import Path

from lotsa.docker_runner import DockerAgentRunner


async def test_read_activity_reads_persisted_agent_home(tmp_path: Path):
    wt = tmp_path / "wt"
    wt.mkdir()
    session_id = "sess-abc-123"
    # The session JSONL lives in the mounted per-task home, mirroring `run`.
    projects = wt.parent / f".agent-home-{wt.name}" / ".claude" / "projects" / "-workspace"
    projects.mkdir(parents=True)
    record = {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"content": [{"type": "text", "text": "hello from the container"}]},
    }
    (projects / f"{session_id}.jsonl").write_text(json.dumps(record) + "\n")

    res = await DockerAgentRunner(image="x").read_activity(session_id, wt)

    assert res.supported is True  # was False before sessions persisted
    assert any("hello from the container" in (e.summary or "") for e in res.events)


async def test_read_activity_supported_even_when_empty(tmp_path: Path):
    # No session file yet → supported, just empty (not the old hard "unsupported").
    wt = tmp_path / "wt"
    wt.mkdir()
    res = await DockerAgentRunner(image="x").read_activity("no-such-session", wt)
    assert res.supported is True
    assert res.events == []
