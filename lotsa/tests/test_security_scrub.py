"""Credential scrubbing on the persist + activity paths (audit findings #1, #2, #3)."""

from __future__ import annotations

from lotsa.db import TaskDB
from rigg.activity import _blocks_to_events


class TestActivityScrub:
    def test_bash_tool_use_is_scrubbed(self, monkeypatch):
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        tok = "ghp_" + "a" * 36
        record = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"echo {tok}"}}]},
        }
        blob = str(_blocks_to_events(record))
        assert tok not in blob
        assert "***" in blob

    def test_tool_result_is_scrubbed(self, monkeypatch):
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        tok = "sk-ant-" + "b" * 30
        record = {"type": "user", "message": {"content": [{"type": "tool_result", "content": f"key={tok}"}]}}
        assert tok not in str(_blocks_to_events(record))


class TestAddMessageScrub:
    async def test_content_is_scrubbed_before_persist(self, tmp_path, monkeypatch):
        tok = "ghp_" + "c" * 36
        monkeypatch.setenv("GITHUB_TOKEN", tok)
        db = TaskDB(tmp_path / "lotsa.db")
        await db.initialize()
        try:
            row = await db.add_message("task1", "agent", "step", f"leaked {tok} here", "stderr")
            assert tok not in row.content
            assert "***" in row.content
            # And it is scrubbed in storage, not just the returned row.
            stored = await db.get_messages("task1")
            assert tok not in stored[0].content
        finally:
            await db.close()
