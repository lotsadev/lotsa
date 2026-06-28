"""Tests for DockerAgentRunner — mocked subprocess calls."""

from unittest.mock import patch

import pytest

from lotsa.docker_runner import DockerAgentRunner


@pytest.fixture
def runner():
    return DockerAgentRunner(
        image="test-image:latest",
        model="sonnet",
        budget_usd=2.0,
        credentials={"ANTHROPIC_API_KEY": "sk-test-key"},
    )


async def test_run_builds_docker_command(runner, tmp_path):
    """Verify the docker run command is constructed correctly."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "done"
        mock_run.return_value.stderr = ""

        result = await runner.run("system prompt", "user prompt", work_dir)

    assert result.success is True
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--rm" in cmd
    assert f"{work_dir.resolve()}:/workspace" in " ".join(cmd)
    assert "test-image:latest" in cmd
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "system prompt" in cmd
    assert "user prompt" in cmd
    # Layered authority (ADR-025): project CLAUDE.md as context; isolate
    # operator user/local; append on top of the claude_code preset.
    assert "--append-system-prompt" in cmd
    assert "--system-prompt" not in cmd
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"


async def test_run_persists_session_home_and_runs_as_host_uid(runner, tmp_path):
    """The container runs as the host uid with a persistent mounted HOME, so the
    session JSONL survives the --rm container (fixes `--resume` on follow-ups)
    and bind-mount writes are owned by the host user."""
    import os

    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        await runner.run("sys", "usr", work_dir)

    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "--user" in cmd
    assert cmd[cmd.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "HOME=/agenthome" in cmd
    agent_home = work_dir.parent / f".agent-home-{work_dir.name}"
    assert f"{agent_home}:/agenthome" in joined
    assert (agent_home / ".claude").is_dir()  # created so sessions persist across runs


async def test_run_mounts_common_gitdir_for_in_container_git(runner, tmp_path, monkeypatch):
    """A worktree's gitdir lives outside the worktree; it must be mounted so
    in-container `git diff`/`log` work (review/pr_summary steps). Without it,
    git fails 'not a git repository'."""
    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    common = tmp_path / "project" / ".git"
    common.mkdir(parents=True)
    monkeypatch.setattr(runner, "_git_common_dir", lambda _wd: common)

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        await runner.run("sys", "usr", work_dir)

    cmd = mock_run.call_args[0][0]
    # Mounted at the SAME path on both sides so the worktree's gitfile resolves.
    assert f"{common}:{common}" in " ".join(cmd)


async def test_run_no_gitdir_mount_when_not_a_worktree(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_git_common_dir", lambda _wd: None)
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        await runner.run("sys", "usr", tmp_path)
    cmd = mock_run.call_args[0][0]
    assert "/.git:" not in " ".join(cmd)  # no spurious gitdir mount


async def test_run_passes_credentials(runner, tmp_path):
    """Auth env vars are forwarded as -e flags."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "-e ANTHROPIC_API_KEY=sk-test-key" in cmd_str


async def test_run_oauth_credentials(tmp_path):
    """OAuth token credentials are forwarded."""
    runner = DockerAgentRunner(
        credentials={
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "CLAUDE_ACCOUNT_UUID": "acc-uuid",
            "CLAUDE_ORG_UUID": "org-uuid",
        }
    )

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd_str = " ".join(mock_run.call_args[0][0])
    assert "CLAUDE_CODE_OAUTH_TOKEN=oauth-token" in cmd_str
    assert "CLAUDE_ACCOUNT_UUID=acc-uuid" in cmd_str
    assert "CLAUDE_ORG_UUID=org-uuid" in cmd_str


async def test_run_failure(runner, tmp_path):
    """Non-zero exit code maps to failed AgentResult."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "error"

        result = await runner.run("sys", "usr", tmp_path)

    assert result.success is False
    assert result.return_code == 1
    assert result.stderr == "error"


async def test_run_timeout(runner, tmp_path):
    """Timeout returns failed AgentResult."""
    import subprocess

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        result = await runner.run("sys", "usr", tmp_path, timeout_seconds=10)

    assert result.success is False
    assert "timed out" in result.stderr.lower()


async def test_run_docker_not_found(runner, tmp_path):
    """Missing docker binary raises RuntimeError."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("docker")

        with pytest.raises(RuntimeError, match="docker not found"):
            await runner.run("sys", "usr", tmp_path)


async def test_run_does_not_pass_allowed_tools(runner, tmp_path):
    """--allowedTools is a no-op when combined with
    --dangerously-skip-permissions (the CLI bypasses all permission
    checks). PR #100's allowlist provided false confidence; the
    OPERATIONAL_PREAMBLE now carries the cross-turn tool restrictions
    via prompt-level guidance instead.
    """
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd = mock_run.call_args[0][0]
    assert "--allowedTools" not in cmd


async def test_run_with_session_id(runner, tmp_path):
    """session_id is passed as --resume flag."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path, session_id="sess-123")

    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "sess-123"


async def test_custom_docker_args(tmp_path):
    """Extra docker args are passed through."""
    runner = DockerAgentRunner(
        credentials={"ANTHROPIC_API_KEY": "key"},
        docker_args=["--network", "host", "--memory", "4g"],
    )

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd = mock_run.call_args[0][0]
    assert "--network" in cmd
    assert "host" in cmd
    assert "--memory" in cmd
    assert "4g" in cmd


async def test_docker_runner_sets_max_output_tokens_when_configured(tmp_path):
    """When ``max_output_tokens`` is configured, it appears as a ``-e``
    flag on the ``docker run`` command so Claude Code's 32000 default is
    overridden inside the container.

    Mirrors ``test_claude_code_runner_sets_max_output_tokens_when_configured``
    for parity between the docker and non-docker runners.
    """
    runner = DockerAgentRunner(
        credentials={"ANTHROPIC_API_KEY": "key"},
        max_output_tokens=128000,
    )

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "-e CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000" in cmd_str


async def test_docker_runner_omits_max_output_tokens_when_neither_set(tmp_path, monkeypatch):
    """When ``max_output_tokens`` is None AND no shell export is present,
    the runner adds NO ``-e CLAUDE_CODE_MAX_OUTPUT_TOKENS`` flag. The
    container falls through to Claude Code's built-in 32000 default.
    """
    monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
    runner = DockerAgentRunner(
        credentials={"ANTHROPIC_API_KEY": "key"},
    )  # no max_output_tokens kwarg

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd_str = " ".join(mock_run.call_args[0][0])
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in cmd_str


async def test_docker_runner_forwards_shell_export_when_config_unset(tmp_path, monkeypatch):
    """When ``max_output_tokens`` is None but the operator has exported
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` in their shell, the runner forwards
    it explicitly to the container via ``-e``.

    Docker does NOT inherit host env by default — without explicit
    forwarding the shell workaround would silently fail in ``--docker``
    mode (the container would fall back to 32000). Matches the non-Docker
    runner's ``{**os.environ}`` behaviour for parity.
    """
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
    runner = DockerAgentRunner(
        credentials={"ANTHROPIC_API_KEY": "key"},
    )  # no max_output_tokens kwarg

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd_str = " ".join(mock_run.call_args[0][0])
    assert "-e CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000" in cmd_str, (
        "Shell-exported CLAUDE_CODE_MAX_OUTPUT_TOKENS must be forwarded to "
        "the container when no config value is set — docker run doesn't "
        "inherit host env by default."
    )


async def test_docker_runner_config_overrides_shell_export(tmp_path, monkeypatch):
    """When BOTH config and shell-export are set, the config value wins.

    Mirrors ``test_claude_code_runner_config_overrides_shell_export`` for
    parity. lotsa.yaml's value is the authoritative source.
    """
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
    runner = DockerAgentRunner(
        credentials={"ANTHROPIC_API_KEY": "key"},
        max_output_tokens=128000,
    )

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd_str = " ".join(mock_run.call_args[0][0])
    assert "-e CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000" in cmd_str, (
        "lotsa.yaml's max_output_tokens must win over the shell-exported value"
    )
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000" not in cmd_str, (
        "When config wins, the shell value must not also be forwarded"
    )


async def test_run_forwards_model_override(runner, tmp_path):
    """ADR-022: a per-call ``model=`` override is forwarded into the container
    as ``--model <name>`` on the claude command — the same per-invocation
    forwarding the runner already does for ``--max-budget-usd``."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        result = await runner.run("sys", "usr", tmp_path, model="opus")

    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    # The AgentResult reports the resolved model that actually ran (mirrors
    # the ClaudeCodeRunner override test — all return paths use effective_model).
    assert result.model == "opus"


async def test_run_uses_construction_model_when_no_override(runner, tmp_path):
    """When no per-call ``model=`` is given, the runner forwards its
    construction-time model (``sonnet`` for this fixture)."""
    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        await runner.run("sys", "usr", tmp_path)

    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
