"""Docker-based AgentRunner — runs Claude Code inside a container.

Implements the rigg ``AgentRunner`` protocol by wrapping execution
in ``docker run`` instead of a local subprocess call. The host work
directory is mounted as a volume at ``/workspace`` inside the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from pathlib import Path

from rigg import CLI_DISPATCH_SHAPE_FRAGMENT, parse_claude_output
from rigg.models import ActivityResult, AgentResult

logger = logging.getLogger(__name__)

# How often the idle watchdog re-reads the liveness probe. Cheap (one directory
# stat walk over a single-project tree), so the resolution cost is negligible
# next to the minutes-scale windows it enforces. Module-level so tests can
# shrink it.
_WATCHDOG_POLL_SECONDS = 15.0

# The container's cwd, and therefore the encoded project-directory name Claude
# Code writes its session JSONL under inside the mounted HOME.
_CONTAINER_WORKDIR = Path("/workspace")

# Auth env vars that Claude Code recognises.
# At least one of ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN must be set.
CLAUDE_AUTH_VARS = [
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_ACCOUNT_UUID",
    "CLAUDE_ORG_UUID",
    "CLAUDE_EMAIL",
]


class DockerAgentRunner:
    """AgentRunner that executes the claude CLI inside a Docker container.

    Implements the rigg ``AgentRunner`` protocol.
    """

    def __init__(
        self,
        image: str = "lotsa-agent:latest",
        model: str = "sonnet",
        budget_usd: float = 5.0,
        credentials: dict[str, str] | None = None,
        docker_args: list[str] | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._image = image
        self._model = model
        self._budget_usd = budget_usd
        self._credentials = credentials or {}
        self._docker_args = docker_args or []
        # See ``rigg.agent_runner.ClaudeCodeRunner.__init__`` for the
        # contract — forwarded into the container via ``-e``.
        self._max_output_tokens = max_output_tokens

    def dispatch_shape_prompt(self) -> str:
        """CLI-shaped dispatch fragment — Docker still runs ``claude --print``."""
        return CLI_DISPATCH_SHAPE_FRAGMENT

    # ADR-040: Docker threads ``session_id`` into ``--resume`` inside the
    # container and the agent-home is a persisted mount, so the session survives
    # a daemon restart and can be resumed (same posture as ``ClaudeCodeRunner``).
    supports_resume = True

    async def read_activity(
        self,
        session_id: str,
        work_dir: Path,
        since_index: int = 0,
        limit: int = 200,
    ) -> ActivityResult:
        """Read activity from the per-task mounted ``~/.claude`` (ADR-017).

        The container writes its session JSONL into the persistent agent HOME
        mounted at ``/agenthome`` (see ``run``), which on the host is
        ``<worktree>/../.agent-home-<task>/.claude``. Point the shared parser at
        that projects root; its glob fallback resolves the session by id
        regardless of the container's cwd encoding. (Previously unavailable
        because the session lived in the ``--rm`` container.)
        """
        from rigg import activity

        wt = work_dir.resolve()
        projects_root = wt.parent / f".agent-home-{wt.name}" / ".claude" / "projects"
        return await activity.read_activity(session_id, work_dir, since_index, limit, projects_root=projects_root)

    async def run(
        self,
        system_prompt: str,
        user_prompt: str,
        work_dir: Path,
        allowed_tools: list[str] | None = None,
        timeout_seconds: int = 3600,
        session_id: str | None = None,
        model: str | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> AgentResult:
        """Run claude inside a Docker container with work_dir mounted.

        Two independent deadlines, both of which stop the *container* and not
        merely the ``docker run`` client:

        * ``idle_timeout_seconds`` — the useful one. The container emits nothing
          on stdout until it exits (``--output-format json`` is a single blob),
          so wall-clock is blind to the difference between a step doing 40
          minutes of real work and one wedged at minute three. The session JSONL
          under the mounted HOME *is* appended as the agent works, so the
          watchdog kills only when that has gone quiet for the whole window.
          ``None`` disables it (wall-clock only, the pre-fix shape).
        * ``timeout_seconds`` — a backstop for the case the probe itself is
          unavailable (no session file ever written, an unreadable mount).

        Prod incident ``f22e232b``: before this, the only deadline was the
        signature's own 3600s default — never overridden by the orchestrator —
        and reaching it killed the client while the container ran on for
        another 7m39s, still writing into a worktree already marked failed.
        """
        # Per-step override (ADR-022): when set, this one invocation runs
        # against ``model`` instead of the construction-time default.
        effective_model = model or self._model
        env_flags = self._build_env_flags()

        wt = work_dir.resolve()
        # A persistent per-task HOME (outside the worktree) mounted at /agenthome.
        # Without it, Claude Code's session JSONL lives in the --rm container and
        # is destroyed on exit, so the next turn's ``--resume <session>`` fails
        # with "No conversation found". This dir survives across runs of the task.
        agent_home = wt.parent / f".agent-home-{wt.name}"
        (agent_home / ".claude").mkdir(parents=True, exist_ok=True)

        # A per-task worktree's ``.git`` is a gitfile pointing into the project's
        # common gitdir, which lives OUTSIDE the worktree. Mount that common dir at
        # its host path so in-container git resolves — otherwise `git diff`/`log`
        # fail ("not a git repository") and the review / pr_summary / resolve steps
        # break in Docker mode. Same path on both sides so the gitfile pointer is valid.
        git_common = self._git_common_dir(wt)
        git_mount = ["-v", f"{git_common}:{git_common}"] if git_common else []

        # Capture the container id so a timeout can stop the *container*.
        # Killing the ``docker run`` client (all ``subprocess`` timeouts do)
        # leaves the daemon's container running. Docker refuses to start if the
        # cidfile already exists, so clear a stale one from a prior run.
        cid_path = agent_home / "container.cid"
        with contextlib.suppress(OSError):
            cid_path.unlink(missing_ok=True)

        cmd = [
            "docker",
            "run",
            "--rm",
            "--cidfile",
            str(cid_path),
            # Run as the host (lotsa) uid so files written to the bind mounts —
            # the worktree AND the session HOME — are owned by, and writable by,
            # the host user. The image's default uid (1000) can't write the
            # lotsa-owned worktree, which silently broke code edits in Docker mode.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/agenthome",
            "-v",
            f"{wt}:/workspace",
            "-v",
            f"{agent_home}:/agenthome",
            *git_mount,
            "-w",
            "/workspace",
            *env_flags,
            *self._docker_args,
            self._image,
            "claude",
            "--print",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--verbose",
            "--model",
            effective_model,
            "--max-budget-usd",
            str(self._budget_usd),
            # See ClaudeCodeRunner: layered authority (ADR-025). Load
            # project-level CLAUDE.md and .claude/ settings as
            # conversation context; isolate operator-global user and
            # local settings; append Lotsa's rules on top of the
            # claude_code preset.
            "--setting-sources",
            "project",
            "--append-system-prompt",
            system_prompt,
        ]

        if session_id:
            cmd.extend(["--resume", session_id])

        # See rigg.agent_runner: --allowedTools is a no-op when
        # combined with --dangerously-skip-permissions. The cross-turn
        # tools are guarded via OPERATIONAL_PREAMBLE instead.
        # allowed_tools is kept on the call signature for compatibility
        # but unused.
        cmd.extend(["-p", user_prompt])

        logger.info(
            "Running docker agent: image=%s, model=%s, work_dir=%s",
            self._image,
            effective_model,
            work_dir,
        )

        start = time.monotonic()
        # The watchdog reports through this holder rather than a return value:
        # the run can finish either by the container exiting on its own or by
        # the watchdog killing it, and the caller has to tell those apart after
        # the fact (a killed container still returns *a* completed process).
        idle_kill: dict[str, float] = {}
        watchdog = (
            asyncio.create_task(self._idle_watchdog(cid_path, agent_home, idle_timeout_seconds, idle_kill))
            if idle_timeout_seconds
            else None
        )
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                ),
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if idle_kill:
                # We stopped it. Whatever the client reported is the corpse of
                # our own kill, not a verdict on the work — report the timeout.
                return AgentResult(
                    success=False,
                    stdout="",
                    stderr=(
                        f"Docker container killed after {idle_kill['idle']:g}s with no agent activity "
                        f"(idle timeout). The agent wrote nothing to its session log for that whole "
                        f"window, so it was treated as wedged rather than working."
                    ),
                    return_code=-1,
                    duration_ms=elapsed_ms,
                    model=effective_model,
                )

            parsed = parse_claude_output(result.stdout)

            return AgentResult(
                success=result.returncode == 0,
                stdout=parsed.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
                duration_ms=elapsed_ms,
                model=effective_model,
                session_id=parsed.session_id,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                cost_usd=parsed.cost_usd,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # The client is dead; the container is not. Stop it explicitly.
            await self._kill_container(cid_path)
            return AgentResult(
                success=False,
                stdout="",
                stderr=f"Docker container timed out after {timeout_seconds}s",
                return_code=-1,
                duration_ms=elapsed_ms,
                model=effective_model,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"docker not found: {exc}") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            with contextlib.suppress(OSError):
                cid_path.unlink(missing_ok=True)

    async def _idle_watchdog(
        self,
        cid_path: Path,
        agent_home: Path,
        idle_timeout_seconds: float,
        report: dict[str, float],
    ) -> None:
        """Kill the container once agent activity has been quiet too long.

        The liveness probe is the session JSONL's mtime under the per-task
        mounted HOME (``rigg.activity.last_activity_mtime``) — the same file the
        Activity tab reads. Before the agent has written anything, the run's own
        start time stands in, so a container that dies silently at startup is
        still bounded by the idle window rather than the wall-clock backstop.

        Scoped to the container's own ``/workspace`` project directory: the
        mounted HOME belongs to exactly one task, and scoping keeps the probe
        honest if that ever stops being true.

        Never raises — an unreadable probe reports ``None``, which is treated as
        "no evidence yet" (fall back to the start time), never as a reason to
        kill. Cancelled by ``run``'s ``finally`` on every normal exit.
        """
        from rigg.activity import encode_cwd, last_activity_mtime

        projects_root = agent_home / ".claude" / "projects"
        project_dir = encode_cwd(_CONTAINER_WORKDIR)
        started = time.time()
        while True:
            await asyncio.sleep(_WATCHDOG_POLL_SECONDS)
            last = await asyncio.to_thread(last_activity_mtime, projects_root, project_dir)
            quiet_for = time.time() - (last if last is not None else started)
            if quiet_for >= idle_timeout_seconds:
                logger.warning(
                    "Agent idle for %.0fs (limit %.0fs) — killing container; agent home=%s",
                    quiet_for,
                    idle_timeout_seconds,
                    agent_home,
                )
                report["idle"] = idle_timeout_seconds
                await self._kill_container(cid_path)
                return

    @staticmethod
    async def _kill_container(cid_path: Path) -> None:
        """Best-effort ``docker kill`` of the container named by *cid_path*.

        Split out so both deadlines share one kill path, and so tests can
        observe the kill without a docker daemon. Silent when the cidfile is
        absent (the container never started, or already exited and ``--rm``
        cleaned up) — there is nothing to kill and nothing to report.

        Argv tokens via the async subprocess API, no shell (Constitution §1.1 /
        §2.1) — the container id is daemon-generated hex, but it is passed as a
        separate positional token regardless.
        """
        try:
            cid = cid_path.read_text().strip()
        except OSError:
            return
        if not cid:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                cid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
        except (OSError, ValueError) as exc:
            logger.warning("Could not kill container %s: %s", cid[:12], exc)
            return
        if proc.returncode != 0:
            # Commonly "No such container" — it exited between the deadline and
            # the kill. Worth a line, never worth raising over.
            logger.warning("docker kill %s returned %s: %s", cid[:12], proc.returncode, err.decode().strip()[:200])

    @staticmethod
    def _git_common_dir(work_dir: Path) -> Path | None:
        """Resolve the worktree's common gitdir (the project's real ``.git``).

        A worktree's ``.git`` is a gitfile pointing here; the dir lives outside
        the worktree, so it must be mounted for in-container git to work. Returns
        ``None`` if *work_dir* isn't a git worktree (then nothing is mounted).
        """
        try:
            out = subprocess.run(
                ["git", "-C", str(work_dir), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        p = Path(out.stdout.strip())
        if not p.is_absolute():
            p = (work_dir / p).resolve()
        return p if p.exists() else None

    def _build_env_flags(self) -> list[str]:
        """Build -e flags for Claude auth env vars and runtime overrides.

        Precedence for ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` matches the
        non-Docker ``ClaudeCodeRunner``:

        1. ``self._max_output_tokens`` (from lotsa.yaml / ``--max-output-tokens``)
           wins when set.
        2. The host's shell export passes through when nothing is configured.
           ``docker run`` does NOT inherit host env by default — we forward
           it explicitly so the shell workaround keeps working in Docker mode.
        3. With neither set, Claude Code uses its built-in 32000 default.
        """
        flags: list[str] = []
        for var in CLAUDE_AUTH_VARS:
            val = self._credentials.get(var) or os.environ.get(var, "")
            if val:
                flags.extend(["-e", f"{var}={val}"])
        if self._max_output_tokens is not None:
            flags.extend(["-e", f"CLAUDE_CODE_MAX_OUTPUT_TOKENS={self._max_output_tokens}"])
        elif shell_value := os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
            flags.extend(["-e", f"CLAUDE_CODE_MAX_OUTPUT_TOKENS={shell_value}"])
        return flags
