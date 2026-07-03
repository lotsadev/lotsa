"""Agent invocation protocol and Claude Code CLI implementation.

Extracted from: bot/orchestrator.py run_claude_code() (lines 575-616).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rigg.models import ActivityResult, AgentResult
from rigg.parsing import parse_claude_output

logger = logging.getLogger(__name__)


# The CLI-shape dispatch fragment (ADR-028 Phase 2).
#
# This is the portion of Lotsa's operational preamble that describes the
# *CLI dispatch shape* — the ``claude --print`` one-shot, no-daemon
# environment and the cross-turn tools that silently fail in it. It is
# specific to the CLI-shaped runners (``ClaudeCodeRunner``,
# ``DockerAgentRunner``); the SDK-shaped runner ships its own fragment.
#
# The orchestrator concatenates ``OPERATIONAL_PREAMBLE`` (universal,
# task-shape rules) + the active runner's ``dispatch_shape_prompt()``
# (this fragment, for CLI runners) + the per-step prompt at dispatch time.
# It lives here — not in ``lotsa/orchestrator.py`` — because it crosses the
# lotsa↔rigg boundary: ``DockerAgentRunner`` (in CE) imports it, so it
# must be on rigg's public surface (``__all__``).
CLI_DISPATCH_SHAPE_FRAGMENT = """\
### Your environment

You're running inside `claude --print`, a headless invocation that
produces one turn of output and then exits. Important consequences:

- **No human is watching your output stream.** The operator started
  this dispatch — possibly minutes or hours ago — and will read
  your output AFTER you finish, via the Lotsa dashboard. They are
  not typing responses between your tool calls. The conversation
  feels live to you but is fundamentally one-shot.
- **No UI to render interactive prompts.** `AskUserQuestion` and
  similar interactive tools either return error results or quietly
  fail. If a tool result looks like *"dismissed"*, *"answer
  questions?"*, or similar — that is not the operator declining;
  that is the tool finding no UI to render in. Do not interpret
  such results as user feedback or as instruction to proceed
  unilaterally; instead emit `NEEDS_INPUT:` (see *How to
  communicate*).
- **No daemon to manage cross-turn work.** `Monitor`,
  `ScheduleWakeup`, `Task`/`Agent` subagent delegation, `BashOutput`
  polling, and Bash background mode all assume a long-lived REPL
  with a daemon polling and re-dispatching. None of that exists
  here. Subprocesses are reaped at turn-end. Wakeups never fire.
  Subagents never report back.

When in doubt: assume your dispatch ends when you stop emitting
text, and choose tools and patterns that fit that constraint.

### Execution patterns

The *Your environment* section above explains why cross-turn
patterns don't work. Concretely, the following tools and modes
fail in this dispatch — not because Lotsa blocks them at the CLI
level (we don't, and the agent may call them successfully and get
results that look meaningful) but because the *underlying machinery*
isn't there:

- `Bash` background mode (`run_in_background: true`, or
  auto-promoted long-running commands). Tool result says *"You will
  be notified when it completes"* — the notification never fires;
  the subprocess is reaped at turn-end and the output file is left
  empty.
- `Monitor` — streams events from a long-running process across
  turns. No next turn exists.
- `ScheduleWakeup` — schedules a future agent invocation. No
  daemon will fire it.
- `Task` / `Agent` subagent delegation — the parent turn ends before
  the subagent reports. The subagent may run and produce output,
  but you will not see it in your current turn.
- `BashOutput` polling a background shell that has already been
  reaped.
- `AskUserQuestion` — no UI to render in; returns an error result
  the operator never sees.

**Foreground, in-turn patterns are fine.** If a command needs to
run for a few minutes, run it foreground (e.g. `pytest -q`) and let
your turn block on it. If a command is genuinely too slow to wait
for in one turn, split it (run a smaller subset) rather than
deferring it. If you need the operator's input, emit
`NEEDS_INPUT:` (see *How to communicate*)."""


class AgentRunnerError(Exception):
    """Raised when the runner itself cannot start (e.g., binary not found)."""


# Claude Code releases the activity reader (ADR-017) has been validated
# against. The session JSONL format is an internal Claude Code contract, so a
# version outside this range *may* break the parser — the failure mode is an
# empty Activity tab, not corruption. Bump the upper bound when a newer CLI is
# verified against the fixtures in ``rigg/tests/fixtures``.
_TESTED_CLAUDE_VERSION_RANGE = (1, 0), (2, 0)  # [1.0.0, 2.0.0)


def warn_if_claude_version_untested() -> None:
    """Log a warning if the installed ``claude`` CLI is outside the tested range.

    Best-effort and side-effect-free beyond logging: any failure (binary
    absent, unparseable output, timeout) is swallowed. Called once from the
    orchestrator's ``start()`` — deliberately NOT from ``__init__``, because the
    default runner self-registers at import time and shelling out there would
    hit every import (including tests with no ``claude`` installed). This is the
    ADR-017 mitigation for reading a file format Lotsa does not own.
    """
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return
    raw = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        return
    major, minor = int(match.group(1)), int(match.group(2))
    low, high = _TESTED_CLAUDE_VERSION_RANGE
    if not (low <= (major, minor) < high):
        logger.warning(
            "Claude Code version %s is outside the activity-reader's tested range "
            "[%d.%d, %d.%d); the in-Lotsa Activity tab (ADR-017) may be empty if the "
            "session JSONL format changed. Other functionality is unaffected.",
            raw,
            low[0],
            low[1],
            high[0],
            high[1],
        )


class AgentRunner(Protocol):
    """Protocol for invoking AI agents."""

    async def run(
        self,
        system_prompt: str,
        user_prompt: str,
        work_dir: Path,
        allowed_tools: list[str] | None = None,
        timeout_seconds: int = 3600,
        session_id: str | None = None,
        model: str | None = None,
    ) -> AgentResult: ...

    async def read_activity(
        self,
        session_id: str,
        work_dir: Path,
        since_index: int = 0,
        limit: int = 200,
    ) -> ActivityResult:
        """Return recent activity events from the runner's native persistence.

        Read-only — never mutates session state, safe against an in-flight
        (append-only) session file. The Protocol carries a real default body
        (unlike ``dispatch_shape_prompt``) because there is a sensible
        universal answer — "this runner exposes no activity" — and ADR-017
        specifies a Protocol-level default. Runners that can read their own
        persistence override it. See ADR-017 §1.
        """
        return ActivityResult(events=[], supported=False)

    def dispatch_shape_prompt(self) -> str:
        """Return this runner's dispatch-shape preamble fragment (ADR-028).

        The orchestrator appends this after the universal
        ``OPERATIONAL_PREAMBLE`` so each runner can describe the dispatch
        shape it actually provides (CLI one-shot vs. SDK programmatic).

        Declared explicitly on every concrete runner — no Protocol default
        body, because rigg's runners satisfy this Protocol
        *structurally* (duck typing), and a default body is silently absent
        for structural implementers (``AttributeError`` at runtime). See
        ADR-028 §"Why not a Protocol default with body?".
        """
        ...

    @property
    def supports_resume(self) -> bool:
        """Whether this runner can reattach to a prior session across a restart
        (ADR-040 R3).

        ``True`` means a persisted ``session_id`` survives a daemon restart and
        the runner threads it into a resume (``--resume`` for the CLI runners),
        so the orchestrator resumes the interrupted step instead of re-running
        it from scratch. ``False`` routes interrupted steps to the safe
        idempotent re-run-from-start path.

        Declared explicitly on every concrete runner (same simultaneous-update
        discipline as ``dispatch_shape_prompt``). The orchestrator still reads
        it defensively (``getattr(runner, "supports_resume", False)``) so a
        mid-rollout runner or a test double without it falls back to re-run.
        """
        ...


class ClaudeCodeRunner:
    """AgentRunner implementation using the Claude Code CLI.

    Calls: claude --print --output-format json --verbose --dangerously-skip-permissions
    """

    def __init__(
        self,
        model: str = "sonnet",
        budget_usd: float = 5.0,
        credentials: dict[str, str] | None = None,
        max_output_tokens: int | None = None,
        skip_permissions: bool = False,
    ) -> None:
        self._model = model
        self._budget_usd = budget_usd
        self._credentials = credentials or {}
        # When set, this overrides Claude Code's built-in 32000 default by
        # exporting CLAUDE_CODE_MAX_OUTPUT_TOKENS into the subprocess env.
        # When None, the runner leaves the variable to whatever the operator
        # has (or hasn't) exported in their shell — so the long-standing
        # env-var workaround keeps working unchanged.
        self._max_output_tokens = max_output_tokens
        # ADR-038: default is the host-sandboxed posture (OS sandbox + dontAsk
        # + worktree-scoped file-tool rules). ``skip_permissions=True`` is the
        # explicit, per-launch operator opt-out (``--dangerously-skip-permissions``)
        # used only on hosts without an OS sandbox — the agent can then modify the
        # host, so it is gated behind the preflight and documented as not advised.
        self._skip_permissions = skip_permissions

    def _subprocess_env(self, work_dir: Path) -> dict[str, str]:
        """Build the env for the agent subprocess.

        Inherits the orchestrator env plus any explicit credentials, then:

        * Strips ``GITHUB_TOKEN``/``GH_TOKEN`` — git is orchestrator-owned, so the
          agent never pushes and has no need for the GitHub token. Withholding it
          means a task cannot read or echo it (least privilege + §1.2; the auth
          tokens the agent *does* need to call ``claude`` remain, and persisted
          output is scrubbed as a second layer).
        * Pins ``PWD`` to *work_dir* so the agent's shell agrees with the
          subprocess ``cwd=`` — otherwise PWD leaks the orchestrator's own cwd
          (the operator's main checkout) and the agent can escape its worktree.
        """
        env = {**os.environ, **self._credentials}
        for var in ("GITHUB_TOKEN", "GH_TOKEN"):
            env.pop(var, None)
        env["PWD"] = str(work_dir)
        if self._max_output_tokens is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self._max_output_tokens)
        return env

    @staticmethod
    def _sandbox_settings(work_dir: Path) -> dict:
        """Managed Claude Code settings that confine the agent to *work_dir* (ADR-038).

        Two layers, because the OS sandbox covers subprocesses but NOT Claude's
        in-process file tools (verified in the ADR-038 spike):
          * ``sandbox`` — OS-enforced (Seatbelt / bubblewrap): Bash and every
            subprocess may only write under the worktree; ``failIfUnavailable``
            fails closed if the sandbox can't start.
          * worktree-scoped ``Write``/``Edit``/``MultiEdit`` allow-rules — confine
            the file tools (``//abs/**`` is Claude's absolute-path rule syntax).
        Read and Bash are allowed (the sandbox bounds where Bash can write);
        ``dontAsk`` (set on the CLI) auto-denies anything unmatched so a headless
        run never hangs on a prompt.
        """
        wt = str(work_dir.resolve())
        glob = f"//{wt.lstrip('/')}/**"
        return {
            "permissions": {
                "allow": ["Bash", "Read", f"Write({glob})", f"Edit({glob})", f"MultiEdit({glob})"],
            },
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"allowWrite": [wt]},
            },
        }

    def _permission_args(self, work_dir: Path) -> tuple[list[str], Path | None]:
        """CLI permission/sandbox args for one run; returns (args, temp-settings-path).

        Default (host-sandboxed): write the managed settings (``_sandbox_settings``)
        to a temp file and run ``--permission-mode dontAsk --settings <file>``.
        Override (``skip_permissions``): the legacy ``--dangerously-skip-permissions``
        bypass — only on sandbox-less hosts where the operator opted in (ADR-038 §2).
        """
        if self._skip_permissions:
            return ["--dangerously-skip-permissions"], None
        fd, name = tempfile.mkstemp(suffix=".json", prefix="lotsa-sandbox-")
        with os.fdopen(fd, "w") as f:
            json.dump(self._sandbox_settings(work_dir), f)
        path = Path(name)
        return ["--permission-mode", "dontAsk", "--settings", str(path)], path

    def dispatch_shape_prompt(self) -> str:
        """CLI-shaped dispatch fragment (``claude --print`` one-shot)."""
        return CLI_DISPATCH_SHAPE_FRAGMENT

    # ADR-040: the CLI runner threads ``session_id`` into ``--resume`` (see
    # ``run`` below) and its session JSONL persists on disk under the worktree
    # agent-home, so it survives a daemon restart and can be resumed.
    supports_resume = True

    async def read_activity(
        self,
        session_id: str,
        work_dir: Path,
        since_index: int = 0,
        limit: int = 200,
    ) -> ActivityResult:
        """Read activity from the Claude Code session JSONL (ADR-017 §3).

        Delegates to the shared parser in ``rigg.activity`` — the single
        source of truth for the JSONL→event mapping and truncation, reused by
        ``DockerAgentRunner`` (when a host mount lands), the dashboard endpoint,
        and the ``lotsa inspect`` CLI.
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
        # ADR-038: host-sandboxed by default (settings file + dontAsk); the
        # legacy bypass only when the operator opted out per-launch.
        perm_args, settings_path = self._permission_args(work_dir)
        cmd = [
            "claude",
            "--print",
            "--output-format",
            "json",
            *perm_args,
            "--verbose",
            "--model",
            effective_model,
            "--max-budget-usd",
            str(self._budget_usd),
            # Layered authority (ADR-025). Load project-level settings
            # only — the operator's `user` settings (where plugins,
            # SessionStart hooks, and global skills live) and `local`
            # settings (operator-personal per-repo overrides, often
            # gitignored) stay isolated. Project-level `CLAUDE.md`
            # auto-loads as conversation context, informing the agent
            # of domain conventions without sitting in the system
            # prompt.
            "--setting-sources",
            "project",
            # Append Lotsa's operational rules and step instructions
            # *on top of* the claude_code preset (the Agent SDK's
            # "lowest-risk customization" pattern — preset preserved,
            # Lotsa's rules layered after). The preset gives the agent
            # baseline tool/safety/style guidance; Lotsa's append sits
            # in the highest-authority system-prompt slot and names the
            # operational rules that take precedence over project
            # CLAUDE.md when they conflict on flow/orchestration.
            "--append-system-prompt",
            system_prompt,
        ]

        if session_id:
            cmd.extend(["--resume", session_id])

        # NOTE: per-step `allowed_tools` is still a no-op here. Tool permissions
        # come from the managed settings file (ADR-038 sandbox mode: the
        # ``permissions.allow`` list) or are bypassed entirely (override mode);
        # passing --allowedTools alongside either is redundant/ineffective. The
        # cross-turn deferral tools (Monitor, ScheduleWakeup, Task, …) are kept
        # out of the agent's behavior via the OPERATIONAL_PREAMBLE in
        # lotsa/orchestrator.py instead. `allowed_tools` is retained on the
        # signature for forward use (ADR-028's SDK runner grants programmatically).
        cmd.extend(["-p", user_prompt])

        env = self._subprocess_env(work_dir)

        logger.info(
            "Running claude: model=%s, budget=$%s, work_dir=%s",
            effective_model,
            self._budget_usd,
            work_dir,
        )

        start = time.monotonic()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=env,
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
                cost_usd=parsed.cost_usd,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                model=effective_model,
                session_id=parsed.session_id,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return AgentResult(
                success=False,
                stdout="",
                stderr=f"Timeout after {timeout_seconds}s",
                return_code=-1,
                duration_ms=elapsed_ms,
                model=effective_model,
            )
        except FileNotFoundError as exc:
            raise AgentRunnerError(f"claude CLI not found: {exc}") from exc
        finally:
            if settings_path is not None:
                settings_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AgentRunner registry (ADR-023)
# ---------------------------------------------------------------------------
#
# Maps a model name (or model-name prefix) to a runner *instance* and resolves
# one per dispatch. Mirrors the ``lotsa.registry`` (ADR-014) tool/engine
# registry shape so operators learn one mechanism — but with two deliberate
# divergences, both load-bearing:
#
#   1. **Instances, not classes.** Runners carry construction state (model,
#      budget, max_output_tokens) that must come from config; ADR-014's
#      class-registration shape doesn't fit because runners aren't built from
#      a uniform ``(orch, state, config)`` triple. ``resolve_runner`` returns a
#      ready-to-call instance as ADR-023 specifies.
#   2. **Collision-by-name, last-wins (not raise).** ``register_tool`` raises on
#      any duplicate name. Here, re-registering the *same name* is a silent
#      refresh (the ADR-028 default-override path: ``OrchestratorService.start()``
#      re-registers ``default`` with the config-derived runner shape), and a
#      *cross-name* prefix collision warns + last-registration-wins per ADR-023's
#      "Identical-prefix collisions" rule. Overriding the default slot is allowed
#      and never warned; prefix collisions across names are warned.
#
# Lives in ``rigg/`` because it is a both-editions primitive that must
# survive the CE→OSS split (ADR-023; rigg/CLAUDE.md).


@dataclass(frozen=True)
class ResolvedRunner:
    """The outcome of ``resolve_runner`` — the registered *name* (for audit) and
    the runner *instance* (ready to call).

    The name is what the audit trail records as ``agent_runner`` (e.g. ``gpt``,
    ``default``) — a registered name, not a Python class name — so a reader can
    tell ``gpt-5`` via one runner apart from ``gpt-5`` via another.
    """

    name: str
    runner: AgentRunner


class RunnerNotFound(Exception):
    """Raised when no exact, prefix, or default runner matches a model name."""


# Model-name prefixes the built-in default runner answers. Defined once and
# shared by the import-time self-registration below and the config-derived
# re-registration in ``OrchestratorService.start()`` (ADR-028), so the
# zero-config fallback and the production default can't drift apart.
DEFAULT_RUNNER_PREFIXES: list[str] = ["claude-", "sonnet", "opus", "haiku"]

# name -> runner instance
_RUNNERS: dict[str, AgentRunner] = {}
# prefix string -> owning registered name
_PREFIXES: dict[str, str] = {}
# the registered name currently in the default slot (or None)
_DEFAULT: str | None = None


def register_runner(
    name: str,
    runner: AgentRunner,
    *,
    prefixes: list[str] | None = None,
    default: bool = False,
) -> None:
    """Register a runner *instance* under *name*.

    *prefixes* are model-name prefixes this runner answers (e.g. ``["gpt-",
    "openai/"]``); *default* marks it as the fallback when no exact/prefix match
    applies.

    Re-registering an existing *name* is a **refresh**: the name's previous
    prefixes are dropped before the new ones are applied, so the ADR-028 default
    re-registration (same name ``default``, same prefixes) is collision-free.
    A prefix already owned by a **different** name is a collision — the later
    registration wins and a ``WARNING`` names both runners and the shared prefix
    (ADR-023). Overriding the default slot (same name, ``default=True``) is
    allowed and not warned.
    """
    # Refresh: drop every prefix this name previously owned so a re-registration
    # (notably the ``default`` slot) doesn't collide with itself.
    for prefix in [p for p, owner in _PREFIXES.items() if owner == name]:
        del _PREFIXES[prefix]

    for prefix in prefixes or []:
        existing = _PREFIXES.get(prefix)
        if existing is not None and existing != name:
            logger.warning(
                "Runner prefix %r registered by %r overrides %r; the later "
                "registration wins. Disambiguate in lotsa.yaml.",
                prefix,
                name,
                existing,
            )
        _PREFIXES[prefix] = name

    _RUNNERS[name] = runner

    if default:
        global _DEFAULT
        _DEFAULT = name


def resolve_runner(model: str) -> ResolvedRunner:
    """Resolve *model* to a runner (ADR-023 resolution rules).

    Order: (1) exact name match; (2) longest-prefix match; (3) default
    registration; (4) no match and no default → ``RunnerNotFound`` naming the
    model and the registered prefixes.
    """
    if model in _RUNNERS:
        return ResolvedRunner(model, _RUNNERS[model])

    # Longest-prefix wins so a more specific prefix (``claude-opus-``) beats a
    # broader one (``claude-``).
    best: str | None = None
    for prefix in _PREFIXES:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is not None:
        name = _PREFIXES[best]
        return ResolvedRunner(name, _RUNNERS[name])

    if _DEFAULT is not None:
        return ResolvedRunner(_DEFAULT, _RUNNERS[_DEFAULT])

    raise RunnerNotFound(f"No runner registered for model {model!r}. Registered prefixes: {sorted(_PREFIXES)}")


def resolve_runner_by_name(name: str) -> ResolvedRunner:
    """Resolve a runner by *exact registered name* (ADR-028 Phase 3).

    Unlike ``resolve_runner`` (model-based: exact → prefix → default), this is
    the resolver for an explicit per-step ``runner:`` choice: an operator who
    names a runner that doesn't resolve has a configuration error, not an
    invitation to silently fall back to the default. Raises ``RunnerNotFound``
    naming the registered runners on a miss.
    """
    if name in _RUNNERS:
        return ResolvedRunner(name, _RUNNERS[name])
    raise RunnerNotFound(f"No runner registered under name {name!r}. Registered runners: {sorted(_RUNNERS)}.")


def registered_prefixes_in_priority_order() -> list[tuple[str, str]]:
    """Return ``(prefix, owning name)`` pairs in resolution-priority order
    (longest prefix first).

    Shared by ``resolve_runner``'s longest-match intent and the startup INFO
    log, so a ``lotsa.yaml`` prefix typo (``gpt5-`` vs ``gpt-``) is visible at
    startup without reading dispatch traces.
    """
    return sorted(_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True)


def clear_registry() -> None:
    """Drop every registered runner, prefix, and the default slot.

    Test-isolation surface — production code registers via ``register_runner``.
    """
    _RUNNERS.clear()
    _PREFIXES.clear()
    global _DEFAULT
    _DEFAULT = None


def snapshot() -> dict[str, object]:
    """Capture registry state for a later ``restore()``.

    Opaque to callers — pass it back unchanged. Pairs with ``restore()`` to
    bracket per-test fixtures that mutate the process-global registry, same
    shape as ``lotsa.registry``'s snapshot/restore.
    """
    return {
        "runners": dict(_RUNNERS),
        "prefixes": dict(_PREFIXES),
        "default": _DEFAULT,
    }


def restore(state: dict[str, object]) -> None:
    """Replace registry contents with a previously captured ``snapshot()``."""
    global _DEFAULT
    runners = state.get("runners", {})
    prefixes = state.get("prefixes", {})
    default = state.get("default")
    assert isinstance(runners, dict)
    assert isinstance(prefixes, dict)
    _RUNNERS.clear()
    _RUNNERS.update(runners)
    _PREFIXES.clear()
    _PREFIXES.update(prefixes)
    _DEFAULT = default if isinstance(default, str) else None


# ``ClaudeCodeRunner`` self-registers as the default at import time (ADR-023
# Scope step 3) so the zero-config / import-only path — notably tests — has a
# working default with no ``start()`` call. A default-constructed instance is
# the no-``start()`` fallback; ``OrchestratorService.start()`` re-registers the
# config-derived runner shape over this same ``default`` name (ADR-028).
register_runner(
    "default",
    ClaudeCodeRunner(),
    prefixes=DEFAULT_RUNNER_PREFIXES,
    default=True,
)
