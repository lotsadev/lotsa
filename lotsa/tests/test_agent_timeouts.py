"""Agent runs die on inactivity, not on a wall-clock hour.

Prod incident (task ``f22e232b``, 2026-08-25): a ``review`` step was killed with
"Docker container timed out after 3600s". Nothing chose an hour for that step —
``timeout_seconds: int = 3600`` is the *default parameter* on every runner's
``run()`` and the orchestrator never passed the argument, so every step of every
process got exactly one hour. Two things were wrong with that:

1. **A wall-clock cap can't tell working from wedged.** The container runs
   ``claude --print --output-format json``, which emits one blob at exit, so the
   host sees nothing for the whole run. Meanwhile the agent's session JSONL —
   mounted at ``/agenthome`` and already read by ``read_activity`` for the
   Activity tab — is appended continuously. That is the liveness signal, and
   nothing consulted it. An *idle* timeout can be aggressive (minutes) without
   killing a step that legitimately takes 40 minutes.
2. **The kill didn't kill anything.** ``subprocess.run(timeout=)`` kills the
   ``docker run`` client; the daemon's container runs on. In the incident the
   session JSONL kept being written until 16:23:04 — 7m39s past the host's
   16:15:25 deadline — with an unsupervised agent still writing into a worktree
   the orchestrator had already marked failed.

The floor on the idle window comes from that same session: the JSONL does not
advance *during* a tool call, and the longest honest gap was a 600s
``pytest lotsa/tests/`` run. So the idle window must sit above the longest real
tool call, not at "a couple of minutes".
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
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


def _seed_session(projects_root: Path, name: str = "-workspace", mtime: float | None = None) -> Path:
    """Write a session JSONL under *projects_root* and return its path."""
    d = projects_root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    p.write_text('{"type":"user"}\n')
    if mtime is not None:
        import os

        os.utime(p, (mtime, mtime))
    return p


# ===========================================================================
# 1. The liveness probe
# ===========================================================================


def test_last_activity_mtime_reads_the_newest_session_file(tmp_path):
    from rigg.activity import last_activity_mtime

    root = tmp_path / "projects"
    _seed_session(root, mtime=1000.0)
    newer = _seed_session(root, name="-other")
    import os

    os.utime(newer, (5000.0, 5000.0))

    assert last_activity_mtime(root) == pytest.approx(5000.0)


def test_last_activity_mtime_scopes_to_a_project_dir(tmp_path):
    """The Docker runner's container cwd is ``/workspace``; the host's is not.

    Scoping by directory keeps a busy neighbouring project from masking this
    task's silence.
    """
    from rigg.activity import last_activity_mtime

    root = tmp_path / "projects"
    _seed_session(root, name="-workspace", mtime=1000.0)
    _seed_session(root, name="-somewhere-else", mtime=9000.0)

    assert last_activity_mtime(root, "-workspace") == pytest.approx(1000.0)


def test_last_activity_mtime_is_none_when_nothing_written_yet(tmp_path):
    from rigg.activity import last_activity_mtime

    assert last_activity_mtime(tmp_path / "nope") is None


# ===========================================================================
# 2. The container is actually killed
# ===========================================================================


async def test_run_passes_a_cidfile_so_the_container_can_be_killed(runner, tmp_path):
    """Without a captured container id there is no way to stop the container —
    killing the ``docker run`` client leaves it running (the incident)."""
    work_dir = tmp_path / "wt"
    work_dir.mkdir()

    with patch("lotsa.docker_runner.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "done"
        mock_run.return_value.stderr = ""
        await runner.run("sys", "usr", work_dir)

    cmd = mock_run.call_args[0][0]
    assert "--cidfile" in cmd, f"docker run must capture the container id; got {cmd!r}"


async def test_wall_timeout_kills_the_container(runner, tmp_path):
    """``subprocess.run``'s timeout kills the client, not the container."""
    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    killed: list[str] = []

    async def fake_kill(*_args):
        # ``patch.object`` replaces the staticmethod with a plain function, so
        # the bound call passes ``self`` through as well.
        killed.append("killed")

    with (
        patch("lotsa.docker_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 1)),
        patch.object(DockerAgentRunner, "_kill_container", fake_kill),
    ):
        result = await runner.run("sys", "usr", work_dir, timeout_seconds=1)

    assert killed, "a wall-clock timeout must stop the container, not just the client"
    assert result.success is False
    assert result.return_code == -1


# ===========================================================================
# 3. Idle timeout — the actual fix
# ===========================================================================


async def test_idle_run_is_killed_long_before_the_wall_clock(runner, tmp_path, monkeypatch):
    """No activity for the idle window → kill, even though the wall cap is far off."""
    import lotsa.docker_runner as dr

    monkeypatch.setattr(dr, "_WATCHDOG_POLL_SECONDS", 0.05)
    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    killed: list[str] = []

    async def fake_kill(*_args):
        # ``patch.object`` replaces the staticmethod with a plain function, so
        # the bound call passes ``self`` through as well.
        killed.append("killed")

    def slow_run(*_args, **_kwargs):
        time.sleep(0.8)
        proc = subprocess.CompletedProcess(args=["docker"], returncode=137, stdout="", stderr="")
        return proc

    with (
        patch("lotsa.docker_runner.subprocess.run", side_effect=slow_run),
        patch.object(DockerAgentRunner, "_kill_container", fake_kill),
    ):
        result = await runner.run("sys", "usr", work_dir, timeout_seconds=3600, idle_timeout_seconds=0.2)

    assert killed, "an idle run must be killed by the watchdog"
    assert result.success is False
    assert "idle" in result.stderr.lower(), f"stderr must name the idle deadline; got {result.stderr!r}"
    assert "3600" not in result.stderr, "must not report the wall-clock cap for an idle kill"


async def test_a_busy_run_is_not_killed_while_activity_advances(runner, tmp_path, monkeypatch):
    """A 40-minute ``code`` step is normal on a real repo — advancing activity
    must renew the lease indefinitely."""
    import threading

    import lotsa.docker_runner as dr

    monkeypatch.setattr(dr, "_WATCHDOG_POLL_SECONDS", 0.05)
    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    agent_home = work_dir.parent / f".agent-home-{work_dir.name}"
    projects_root = agent_home / ".claude" / "projects"
    _seed_session(projects_root)

    killed: list[str] = []

    async def fake_kill(*_args):
        # ``patch.object`` replaces the staticmethod with a plain function, so
        # the bound call passes ``self`` through as well.
        killed.append("killed")

    stop = threading.Event()

    def touch_loop():
        # The agent is working: the session JSONL keeps being appended.
        while not stop.is_set():
            _seed_session(projects_root, mtime=time.time())
            time.sleep(0.05)

    def slow_run(*_args, **_kwargs):
        time.sleep(0.8)
        return subprocess.CompletedProcess(args=["docker"], returncode=0, stdout="done", stderr="")

    t = threading.Thread(target=touch_loop, daemon=True)
    t.start()
    try:
        with (
            patch("lotsa.docker_runner.subprocess.run", side_effect=slow_run),
            patch.object(DockerAgentRunner, "_kill_container", fake_kill),
        ):
            result = await runner.run("sys", "usr", work_dir, timeout_seconds=3600, idle_timeout_seconds=0.3)
    finally:
        stop.set()
        t.join(timeout=2)

    assert not killed, "a run whose activity keeps advancing must not be killed"
    assert result.success is True


async def test_idle_watchdog_is_disabled_when_no_window_is_given(runner, tmp_path, monkeypatch):
    """``idle_timeout_seconds=None`` preserves the pre-fix wall-clock-only shape."""
    import lotsa.docker_runner as dr

    monkeypatch.setattr(dr, "_WATCHDOG_POLL_SECONDS", 0.05)
    work_dir = tmp_path / "wt"
    work_dir.mkdir()
    killed: list[str] = []

    async def fake_kill(*_args):
        # ``patch.object`` replaces the staticmethod with a plain function, so
        # the bound call passes ``self`` through as well.
        killed.append("killed")

    def slow_run(*_args, **_kwargs):
        time.sleep(0.5)
        return subprocess.CompletedProcess(args=["docker"], returncode=0, stdout="ok", stderr="")

    with (
        patch("lotsa.docker_runner.subprocess.run", side_effect=slow_run),
        patch.object(DockerAgentRunner, "_kill_container", fake_kill),
    ):
        result = await runner.run("sys", "usr", work_dir, timeout_seconds=3600, idle_timeout_seconds=None)

    assert not killed
    assert result.success is True


# ===========================================================================
# 4. Config + orchestrator plumbing — the 3600 default must stop applying
# ===========================================================================


def test_config_carries_agent_timeout_defaults():
    from lotsa.config import LotsaConfig

    config = LotsaConfig()
    assert config.agent_idle_timeout_seconds > 600, (
        "the idle window must clear the longest honest tool call (a 600s full pytest run)"
    )
    assert config.agent_idle_timeout_seconds <= 1800
    assert config.agent_timeout_seconds > config.agent_idle_timeout_seconds, (
        "the wall-clock ceiling is a backstop above the idle window, not below it"
    )


def test_agent_timeouts_are_configurable_from_yaml(tmp_path):
    from lotsa.config import LotsaConfig

    (tmp_path / "lotsa.yaml").write_text("agent_timeout_seconds: 1234\nagent_idle_timeout_seconds: 567\n")
    config = LotsaConfig.load(data_dir=tmp_path)

    assert config.agent_timeout_seconds == 1234
    assert config.agent_idle_timeout_seconds == 567


class TestOrchestratorPassesTimeouts:
    """The incident's root cause: ``runner.run`` was called without a timeout,
    so the runner's own 3600 default applied to every step."""

    @pytest.fixture
    def dispatch(self, tmp_path, _loop, run):
        """Dispatch one agent step and capture the kwargs the runner received."""
        from lotsa.config import LotsaConfig
        from lotsa.db import TaskDB
        from lotsa.orchestrator import OrchestratorService
        from lotsa.tests.conftest import FakeRunner

        def _dispatch(**config_kwargs):
            import asyncio

            data_dir = tmp_path / f"data{len(list(tmp_path.iterdir()))}"
            data_dir.mkdir()
            repo = tmp_path / "repo"
            repo.mkdir(exist_ok=True)
            config = LotsaConfig(
                data_dir=data_dir, work_dir=repo, flow="chat", model="sonnet", budget=5.0, **config_kwargs
            )
            db = TaskDB(data_dir / "lotsa.db")
            run(db.initialize())
            svc = OrchestratorService(config, db)
            captured: dict = {}
            fake = FakeRunner()
            original = fake.run

            async def spy(*args, **kwargs):
                captured.update(kwargs)
                return await original(*args, **kwargs)

            fake.run = spy
            svc.runner = fake
            run(svc.start())
            run(svc.create_task("hello", process_name="chat"))
            run(asyncio.sleep(0.3))
            run(svc.shutdown())
            run(db.close())
            return captured

        return _dispatch

    def test_dispatch_passes_both_deadlines(self, dispatch):
        captured = dispatch()

        assert "timeout_seconds" in captured, (
            "the orchestrator must pass a timeout — otherwise the runner's 3600s default applies"
        )
        assert captured["timeout_seconds"] != 3600
        assert captured.get("idle_timeout_seconds")

    def test_dispatch_honours_configured_values(self, dispatch):
        captured = dispatch(agent_timeout_seconds=4242, agent_idle_timeout_seconds=424)

        assert captured["timeout_seconds"] == 4242
        assert captured["idle_timeout_seconds"] == 424
