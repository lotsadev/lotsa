"""Docker-based AgentRunner — runs Claude Code inside a container.

Implements the rigg ``AgentRunner`` protocol by wrapping execution
in ``docker run`` instead of a local subprocess call. The host work
directory is mounted as a volume at ``/workspace`` inside the container.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

from rigg import CLI_DISPATCH_SHAPE_FRAGMENT, parse_claude_output
from rigg.models import ActivityResult, AgentResult

logger = logging.getLogger(__name__)

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
    ) -> AgentResult:
        """Run claude inside a Docker container with work_dir mounted."""
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

        cmd = [
            "docker",
            "run",
            "--rm",
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
