"""ADR-038 Phase 1 — the native runner's sandbox/permission posture."""

from __future__ import annotations

import json
from pathlib import Path

from rigg.agent_runner import ClaudeCodeRunner


class TestSandboxSettings:
    def test_confines_writes_to_the_worktree(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        s = ClaudeCodeRunner._sandbox_settings(wt)
        # OS sandbox confines subprocesses to the worktree, fails closed.
        assert s["sandbox"]["enabled"] is True
        assert s["sandbox"]["failIfUnavailable"] is True
        assert s["sandbox"]["allowUnsandboxedCommands"] is False
        assert s["sandbox"]["filesystem"]["allowWrite"] == [str(wt.resolve())]
        # File tools are scoped to the worktree (the sandbox doesn't cover them).
        allow = s["permissions"]["allow"]
        glob = f"//{str(wt.resolve()).lstrip('/')}/**"
        assert f"Write({glob})" in allow
        assert f"Edit({glob})" in allow
        assert "Bash" in allow and "Read" in allow


class TestPermissionArgs:
    def test_default_is_sandboxed_dontAsk_with_settings_file(self, tmp_path):
        runner = ClaudeCodeRunner()  # skip_permissions defaults False
        args, path = runner._permission_args(tmp_path)
        assert "--dangerously-skip-permissions" not in args
        assert args[:2] == ["--permission-mode", "dontAsk"]
        assert "--settings" in args
        assert path is not None and path.exists()
        written = json.loads(Path(path).read_text())
        assert written["sandbox"]["enabled"] is True
        path.unlink(missing_ok=True)

    def test_override_uses_bypass_and_no_settings_file(self, tmp_path):
        runner = ClaudeCodeRunner(skip_permissions=True)
        args, path = runner._permission_args(tmp_path)
        assert args == ["--dangerously-skip-permissions"]
        assert path is None
