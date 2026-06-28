"""Tests for Rigg AgentRunner protocol and ClaudeCodeRunner."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rigg.agent_runner import AgentRunnerError, ClaudeCodeRunner
from rigg.models import AgentResult


@pytest.mark.asyncio
async def test_claude_code_runner_success(tmp_path):
    runner = ClaudeCodeRunner(model="sonnet", budget_usd=5.0)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "output"
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        result = await runner.run("system", "user", tmp_path)

    assert isinstance(result, AgentResult)
    assert result.success is True
    assert result.stdout == "output"
    assert result.return_code == 0
    assert result.duration_ms >= 0

    # Verify claude CLI was called
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert cmd[0] == "claude"
    assert "--print" in cmd
    # Layered authority (ADR-025): append Lotsa's rules on top of the
    # claude_code preset, don't replace it.
    assert "--append-system-prompt" in cmd
    assert "--system-prompt" not in cmd
    # Load project-level CLAUDE.md / .claude/ as conversation context;
    # keep operator user/local settings isolated.
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"


@pytest.mark.asyncio
async def test_claude_code_runner_failure(tmp_path):
    runner = ClaudeCodeRunner()
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "error"

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result):
        result = await runner.run("system", "user", tmp_path)

    assert result.success is False
    assert result.return_code == 1
    assert result.stderr == "error"


@pytest.mark.asyncio
async def test_claude_code_runner_timeout(tmp_path):
    runner = ClaudeCodeRunner()

    with patch("rigg.agent_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 60)):
        result = await runner.run("system", "user", tmp_path, timeout_seconds=60)

    assert result.success is False
    assert result.return_code == -1
    assert "timeout" in result.stderr.lower()


@pytest.mark.asyncio
async def test_claude_code_runner_binary_not_found(tmp_path):
    runner = ClaudeCodeRunner()

    with (
        patch("rigg.agent_runner.subprocess.run", side_effect=FileNotFoundError("claude not found")),
        pytest.raises(AgentRunnerError, match="claude"),
    ):
        await runner.run("system", "user", tmp_path)


@pytest.mark.asyncio
async def test_claude_code_runner_session_id_passed(tmp_path):
    runner = ClaudeCodeRunner()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path, session_id="ses-123")

    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    assert "ses-123" in cmd


@pytest.mark.asyncio
async def test_claude_code_runner_does_not_pass_allowed_tools(tmp_path):
    """The runner does NOT pass --allowedTools. The flag is a no-op
    in combination with --dangerously-skip-permissions (the CLI
    bypasses all permission checks including allowlist enforcement),
    so passing a restrictive list provided false confidence without
    affecting runtime. Cross-turn tool restrictions live in
    OPERATIONAL_PREAMBLE instead.

    Empirical confirmation: on an internal task (2026-06-08) the agent
    successfully called `Agent`/Task subagent delegation despite
    PR #100 supposedly excluding it from the allowlist.
    """
    runner = ClaudeCodeRunner()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    cmd = mock_run.call_args[0][0]
    assert "--allowedTools" not in cmd


@pytest.mark.asyncio
async def test_claude_code_runner_overrides_pwd_to_work_dir(tmp_path, monkeypatch):
    """The subprocess env's PWD must match the cwd= argument, regardless
    of what the orchestrator's own PWD is. Otherwise the agent's shell
    trusts $PWD and the agent escapes the worktree — committing into the
    operator's main checkout instead of the assigned worktree.
    """
    monkeypatch.setenv("PWD", "/some/other/place")
    runner = ClaudeCodeRunner()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    call_kwargs = mock_run.call_args[1]
    assert call_kwargs["cwd"] == tmp_path
    assert call_kwargs["env"]["PWD"] == str(tmp_path)


@pytest.mark.asyncio
async def test_claude_code_runner_credentials_injected(tmp_path):
    runner = ClaudeCodeRunner(credentials={"MY_TOKEN": "secret"})
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    call_kwargs = mock_run.call_args[1]
    assert call_kwargs["env"]["MY_TOKEN"] == "secret"


@pytest.mark.asyncio
async def test_claude_code_runner_sets_max_output_tokens_when_configured(tmp_path):
    """When ``max_output_tokens`` is configured, the runner exports it as
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` in the subprocess env so Claude Code's
    32000 default is overridden.

    The original failure mode this fixes: tasks failed with "Claude's
    response exceeded the 32000 output token maximum" and operators had no
    in-lotsa way to raise the ceiling — they had to ``export
    CLAUDE_CODE_MAX_OUTPUT_TOKENS=N`` before ``lotsa serve``.
    """
    runner = ClaudeCodeRunner(max_output_tokens=128000)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    env = mock_run.call_args[1]["env"]
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"


@pytest.mark.asyncio
async def test_claude_code_runner_inherits_max_output_tokens_from_os_environ(tmp_path, monkeypatch):
    """When ``max_output_tokens`` is unset (the default), the runner does
    NOT clobber whatever the operator has exported in their shell. The
    long-standing workaround (``export CLAUDE_CODE_MAX_OUTPUT_TOKENS=N``)
    must keep working unchanged.
    """
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
    runner = ClaudeCodeRunner()  # no max_output_tokens kwarg
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    env = mock_run.call_args[1]["env"]
    # The shell-exported value passes through via os.environ.
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


@pytest.mark.asyncio
async def test_claude_code_runner_config_overrides_shell_export(tmp_path, monkeypatch):
    """When ``max_output_tokens`` is set AND a shell export exists, the
    config value wins. Lotsa's configured value is the authoritative one
    so operators can manage the cap from lotsa.yaml without having to
    unset their shell.
    """
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
    runner = ClaudeCodeRunner(max_output_tokens=128000)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    env = mock_run.call_args[1]["env"]
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000", (
        "lotsa.yaml's max_output_tokens must win over the shell-exported value"
    )


@pytest.mark.asyncio
async def test_claude_code_runner_forwards_model_override(tmp_path):
    """ADR-022: a per-call ``model=`` override is passed as ``--model <name>``
    per invocation, overriding the construction-time model."""
    runner = ClaudeCodeRunner(model="sonnet")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        result = await runner.run("system", "user", tmp_path, model="opus")

    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    # The AgentResult reports the resolved model that actually ran.
    assert result.model == "opus"


@pytest.mark.asyncio
async def test_claude_code_runner_uses_construction_model_when_no_override(tmp_path):
    """With no per-call ``model=``, the runner passes its construction-time
    model as ``--model``. Previously the construction-time model was metadata
    only; this PR makes it an explicit ``--model`` flag per invocation."""
    runner = ClaudeCodeRunner(model="haiku")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("rigg.agent_runner.subprocess.run", return_value=mock_result) as mock_run:
        await runner.run("system", "user", tmp_path)

    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
