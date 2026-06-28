"""SDK-shaped AgentRunner backed by Anthropic's Claude Agent SDK (ADR-028).

``ClaudeAgentSDKRunner`` is the third runner *shape* alongside the CLI
runners (``ClaudeCodeRunner``, ``DockerAgentRunner``). It implements the
same ``AgentRunner`` Protocol — identical ``run()`` signature, identical
``AgentResult`` mapping (cost, tokens, session, error classification) — but
drives Anthropic's ``claude-agent-sdk`` package programmatically instead of
shelling out to ``claude --print``.

Auth is supplied via ``ANTHROPIC_API_KEY`` (merged from ``os.environ`` +
``credentials``), honoring ``ANTHROPIC_BASE_URL`` for self-hosted proxies.
There is no keychain fallback and no ``--dangerously-skip-permissions``
login trick — this is the headless-server auth model (ADR-028 §"Auth
model"). Note this is an *auth* simplification only: the permission posture
is unchanged from the CLI runner — ``run()`` sets
``permission_mode="bypassPermissions"`` (the SDK-equivalent bypass) below,
required for headless operation. Real per-tool gating lands with the
interception follow-up.

The package is imported **lazily inside** ``run()`` so this module stays
importable when the SDK isn't installed, and so the missing-dependency path
raises ``AgentRunnerError`` cleanly. The SDK is async-native, so ``run()``
awaits it directly (no executor needed, unlike ``ClaudeCodeRunner``).

What this runner *does not yet* provide: the tool-interception / cross-turn
lifecycle surface (Lotsa-owned ``AskUserQuestion`` → dashboard, background
``Bash`` survival, ``Monitor``/``ScheduleWakeup`` via SDK resume). Those
land in follow-up ADRs. Until then the SDK-shape preamble fragment below is
honest about that: it does *not* advertise those tools as usable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from rigg.agent_runner import AgentRunnerError
from rigg.models import ActivityResult, AgentResult
from rigg.parsing import _bound

logger = logging.getLogger(__name__)


# The SDK-shape dispatch fragment (ADR-028 Phase 2).
#
# Honest to wired capability (ADR-028 requirement #13): because the
# tool-interception / cross-turn lifecycle surface is NOT built in this cut,
# this fragment must not advertise Monitor / ScheduleWakeup / AskUserQuestion
# / background Bash / subagent dispatch as usable. It describes the SDK
# environment truthfully — programmatic, single-turn for now — and keeps the
# universal NEEDS_INPUT blocking channel.
SDK_DISPATCH_SHAPE_FRAGMENT = """\
### Your environment

You're running under Lotsa's Claude Agent SDK runner — a programmatic,
headless dispatch. The operator started this run and reads your output
AFTER you finish, via the Lotsa dashboard. They are not typing responses
between your tool calls.

- **This dispatch is single-turn for now.** Lotsa drives one agent turn
  per dispatch. The orchestrator-participating cross-turn lifecycle
  (background work that survives the turn, Lotsa-owned interactive prompts,
  scheduled wake-ups) is not yet wired into this runner. Do not rely on
  `Monitor`, `ScheduleWakeup`, `Task`/`Agent` subagent delegation,
  `BashOutput` polling, background `Bash`, or `AskUserQuestion` to carry
  work or questions across the turn boundary — that machinery is not
  connected here yet, so treat your dispatch as ending when you stop
  emitting text.
- **No UI to render interactive prompts.** If you need a decision from the
  operator, emit `NEEDS_INPUT:` (see *How to communicate*) rather than
  reaching for an interactive tool.

### Execution patterns

**Foreground, in-turn patterns are fine.** If a command needs to run for a
few minutes, run it foreground (e.g. `pytest -q`) and let your turn block
on it. If a command is genuinely too slow to wait for in one turn, split it
(run a smaller subset) rather than deferring it. If you need the operator's
input, emit `NEEDS_INPUT:` (see *How to communicate*)."""


class ClaudeAgentSDKRunner:
    """AgentRunner backed by Anthropic's Claude Agent SDK.

    Implements the same ``AgentRunner`` Protocol as ``ClaudeCodeRunner``.
    The system prompt, user prompt, ``work_dir``, and ``session_id``
    semantics are identical; cost reporting, output capture, and error
    classification follow the same contract.
    """

    def __init__(
        self,
        model: str = "sonnet",
        budget_usd: float = 5.0,
        credentials: dict[str, str] | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        # Constructor mirrors ``ClaudeCodeRunner`` for drop-in parity.
        self._model = model
        # Kept for parity with the CLI runner, which enforces a per-run USD
        # ceiling via ``claude --print --max-budget-usd``. This cut wires no
        # equivalent cap into the SDK (``ClaudeAgentOptions`` exposes no USD
        # budget), so ``budget_usd`` is currently advisory only — surfaced in
        # the run log for observability but not enforced. The operator-facing
        # gap is documented in lotsa/README.md's "SDK runner — what's wired,
        # and the gaps". Stored for forward use.
        self._budget_usd = budget_usd
        # Warn once, at construction (server startup), so an operator who set
        # ``--budget`` sees the unenforced-cap gap immediately rather than only
        # on reading the docs. ``budget_usd`` is effectively always positive
        # (config defaults it to 5.0), so this fires whenever the SDK runner is
        # selected — which is exactly when the gap is relevant.
        if budget_usd and budget_usd > 0:
            logger.warning(
                "claude-agent-sdk runner does not enforce a per-run USD cap "
                "(budget_usd=$%s is advisory only; the SDK exposes no equivalent "
                "to the CLI's --max-budget-usd). Bound runs another way (e.g. "
                "timeout_seconds) until a cap lands.",
                budget_usd,
            )
        self._credentials = credentials or {}
        # Mirrors the CLI runner. Forwarded into the SDK's env as
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS in run() when set — the SDK drives the
        # same Claude Code runtime, which reads that var (just as the CLI
        # runner sets it on the subprocess env).
        self._max_output_tokens = max_output_tokens

    def dispatch_shape_prompt(self) -> str:
        """SDK-shaped dispatch fragment (programmatic, single-turn for now)."""
        return SDK_DISPATCH_SHAPE_FRAGMENT

    async def read_activity(
        self,
        session_id: str,
        work_dir: Path,
        since_index: int = 0,
        limit: int = 200,
    ) -> ActivityResult:
        """Read activity from the Claude Code session JSONL (ADR-017 §3).

        The SDK runner drives the same Claude Code runtime as ``ClaudeCodeRunner``
        and persists the same per-session JSONL under ``~/.claude/projects``, so
        it delegates to the shared parser too — structural implementers of the
        ``AgentRunner`` Protocol must declare this method explicitly (no inherited
        default body), per the rigg stability contract.
        """
        from rigg import activity

        return await activity.read_activity(session_id, work_dir, since_index, limit)

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
        # Per-step override (ADR-022): when set, this one invocation runs
        # against ``model`` instead of the construction-time default.
        effective_model = model or self._model
        # Lazy import: keep this module importable when the SDK isn't
        # installed, and map a missing dependency to AgentRunnerError
        # ("the runner could not start").
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                CLIConnectionError,
                CLIJSONDecodeError,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                SystemMessage,
                query,
            )
        except ImportError as exc:
            raise AgentRunnerError(f"claude-agent-sdk not installed: {exc}") from exc

        # Auth: merge os.environ + credentials (credentials win, matching
        # ClaudeCodeRunner). No keychain fallback — require an explicit
        # ANTHROPIC_API_KEY. ANTHROPIC_BASE_URL (self-hosted proxy) flows
        # through the merged env untouched.
        env = {**os.environ, **self._credentials}
        if not env.get("ANTHROPIC_API_KEY"):
            raise AgentRunnerError(
                "ANTHROPIC_API_KEY not set (no keychain fallback for the SDK runner). "
                "Set ANTHROPIC_API_KEY in the shell environment."
            )

        # Override PWD so the agent's tools/shell agree with the SDK's cwd
        # below. Without this, PWD leaks from the orchestrator's own cwd
        # (typically the repo root the operator launched `lotsa serve` from)
        # and the agent's tools think the project lives there — leading to
        # agents that escape their assigned worktree and commit into the
        # operator's main checkout. ClaudeCodeRunner pins this for the same
        # reason (agent_runner.py); the SDK drives the same Claude Code
        # runtime, so it needs the same guard.
        env["PWD"] = str(work_dir)

        # Forward the per-response output-token cap the same way the CLI runner
        # does: the SDK drives the Claude Code runtime, which reads
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS from its env.
        if self._max_output_tokens is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self._max_output_tokens)

        options = ClaudeAgentOptions(
            # ADR-025: append Lotsa's rules on top of the claude_code preset
            # rather than replacing it (the SDK's preset-append form).
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            },
            cwd=str(work_dir),
            model=effective_model,
            # ADR-025: load project-level settings only; keep the operator's
            # user/local settings isolated.
            setting_sources=["project"],
            env=env,
            # Resume the session where the SDK supports it.
            resume=session_id or None,
            # No tool gating in this cut — the interception surface is out of
            # scope (ADR-028). Default-allow matches the CLI runner's posture.
            permission_mode="bypassPermissions",
        )
        # NOTE: ``allowed_tools`` is kept on the signature for protocol
        # parity but unused — per-tool gating lands with the interception
        # follow-up ADR.

        logger.info(
            "Running claude-agent-sdk: model=%s, budget=$%s, work_dir=%s",
            effective_model,
            self._budget_usd,
            work_dir,
        )

        start = time.monotonic()
        session_from_init: str | None = None
        result_msg = None
        try:
            async with asyncio.timeout(timeout_seconds):
                async for message in query(prompt=user_prompt, options=options):
                    if isinstance(message, SystemMessage) and getattr(message, "subtype", None) == "init":
                        data = getattr(message, "data", None) or {}
                        session_from_init = data.get("session_id") or session_from_init
                    if isinstance(message, ResultMessage):
                        result_msg = message
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return AgentResult(
                success=False,
                stdout="",
                stderr=f"Timeout after {timeout_seconds}s",
                return_code=-1,
                duration_ms=elapsed_ms,
                model=effective_model,
            )
        except CLINotFoundError as exc:
            # Runner could not start (the Claude Code runtime is absent). This
            # is the SDK analogue of ClaudeCodeRunner's FileNotFoundError →
            # AgentRunnerError path. Must precede the CLIConnectionError catch
            # below: CLINotFoundError subclasses CLIConnectionError in the SDK.
            raise AgentRunnerError(f"claude-agent-sdk runtime not found: {exc}") from exc
        except (CLIConnectionError, ProcessError, CLIJSONDecodeError) as exc:
            # Mid-run SDK failure (connection dropped, runtime process error,
            # or undecodable stream). The runner *did* start, so — mirroring
            # ClaudeCodeRunner, which returns an unsuccessful AgentResult for
            # non-fatal subprocess errors rather than raising — return a
            # failure result with the runner's standard stderr format instead
            # of letting a raw exception repr propagate.
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return AgentResult(
                success=False,
                stdout="",
                stderr=f"claude-agent-sdk error: {type(exc).__name__}: {exc}",
                return_code=1,
                duration_ms=elapsed_ms,
                model=effective_model,
                session_id=session_from_init,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result_msg is None:
            # The stream ended without a terminal result — treat as failure
            # rather than raising, matching the CLI runner's non-fatal paths.
            return AgentResult(
                success=False,
                stdout="",
                stderr="claude-agent-sdk produced no result message",
                return_code=1,
                duration_ms=elapsed_ms,
                model=effective_model,
                session_id=session_from_init,
            )

        is_error = bool(getattr(result_msg, "is_error", False))
        result_text = getattr(result_msg, "result", None)
        stdout = _bound(result_text if isinstance(result_text, str) else "")

        usage = getattr(result_msg, "usage", None) or {}
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None

        session_id_out = getattr(result_msg, "session_id", None) or session_from_init

        return AgentResult(
            success=not is_error,
            stdout=stdout,
            stderr="",
            return_code=0 if not is_error else 1,
            duration_ms=elapsed_ms,
            cost_usd=getattr(result_msg, "total_cost_usd", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=effective_model,
            session_id=session_id_out,
        )
