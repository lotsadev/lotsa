"""Headless orchestration service — no I/O, event-driven.

Manages tasks through flow steps, dispatches agents in the background,
and emits events for consumers (web dashboard, CLI, tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lotsa.engines.pr_monitor import PrMonitorConfig

from lotsa.config import LotsaConfig, resolve_project_specs
from lotsa.db import (
    PUSH_START,
    PUSH_SUCCESS,
    AuditRow,
    MessageRow,
    ProjectRow,
    SQLiteItemSource,
    TaskDB,
    TaskRow,
)
from lotsa.diff import compute_branch_diff
from lotsa.flows import (
    BUNDLED_PROMPTS,
    PRESET_NAMES,
    FlowConfig,
    FlowStep,
    Job,
    Process,
    build_process,
    build_process_from_inline,
    check_conversational_rules,
    evaluate_output_rules,
    find_step,
    resolve_output_target,
)
from lotsa.push_step import CC_TITLE_RE
from lotsa.status import TaskStatusLiteral
from rigg import (
    DEFAULT_RUNNER_PREFIXES,
    AgentRunner,
    ClaudeCodeRunner,
    ResolvedRunner,
    RunnerNotFound,
    WorktreeManager,
    register_runner,
    registered_prefixes_in_priority_order,
    resolve_runner,
    resolve_runner_by_name,
)
from rigg.models import ActivityResult, AgentResult, Item
from rigg.scrub import scrub_secrets

logger = logging.getLogger(__name__)


# Trailing "See 'docker run --help'" / "Run 'git ... --help'" pointers carry no
# cause — strip them so the line before (the actual error) is what surfaces.
_HELP_HINT_RE = re.compile(r"(?i)^(see|run)\b.*--help")


def _summarize_agent_error(return_code: int | None, stderr: str | None) -> str:
    """Build a one-line failure summary from an agent's exit code + stderr.

    Surfaces the last *meaningful* stderr line: trailing generic usage pointers
    (e.g. docker's "See 'docker run --help'." after a missing-image error) are
    dropped so the operator sees the real cause, not the hint.
    """
    msg = f"Agent exited with code {return_code}"
    if not stderr:
        return msg
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    while lines and _HELP_HINT_RE.match(lines[-1]):
        lines.pop()
    if lines:
        msg += ": " + lines[-1]
    return msg


def _marker_requirement_footer(rules: list) -> str:
    """A mandatory-marker footer derived from a step's stdout output rules.

    Marker-driven steps advance only if the agent emits the literal token (e.g.
    ``VERIFIED:``). Agents — especially on cheaper models — often write a prose
    conclusion and omit it, stranding the task (ADR-039 records the longer-term
    fix). This footer makes the marker non-optional, and is derived from the
    step's own ``rules`` so it can never drift from process.yaml. Returns ``""``
    for steps with no stdout markers.
    """
    markers = [r.pattern.lstrip("^") for r in (rules or []) if getattr(r, "source", None) == "stdout"]
    if not markers:
        return ""
    listed = "\n".join(f"- `{m}`" for m in markers)
    return (
        "\n\n## Required outcome marker (mandatory)\n"
        "This step advances ONLY when your reply contains, on a line by itself, **exactly one** of:\n"
        f"{listed}\n"
        "Emit the one matching your conclusion as the final line. A reply with analysis but no "
        "marker leaves the task stuck with no transition — never omit it."
    )


class ApproveNotAllowed(Exception):
    """Raised when approve() is called with no waiting task or no artifact."""


class AnswerNotAllowed(Exception):
    """Raised when answer() is called with no needs_input task."""


class RetryNotAllowed(Exception):
    """Raised when retry() is called on a task whose status != 'blocked'."""


class AcknowledgeOverrideNotAllowed(Exception):
    """Raised when acknowledge_override() targets an unregistered guard or a
    task where the guard's detect() returns False.

    Both cases map to one exception so the registry contents aren't leaked
    through differentiated responses — the client sees a uniform "this override
    isn't applicable to this task" (ADR-019 R3)."""


class ReviseNotAllowed(Exception):
    """Raised when revise()/send_message() is called from a non-actionable status."""


class StopNotAllowed(Exception):
    """Raised when stop() is called on a task with no actively-working agent.

    Stop is only valid while the task is ``status='working'`` AND present in
    ``_in_flight`` — a task parked at a gate (``waiting``/``needs_input``) or
    already finished has no agent to interrupt.
    """


class ArchiveNotAllowed(Exception):
    """Raised when archive() is called for a task that does not exist."""


class ArchiveFailed(Exception):
    """Raised when archive()'s terminal CAS never converges.

    ``archive()`` re-reads the row and CASes to ``archived`` in a bounded
    loop to absorb a racing natural completion. If every attempt loses, the
    task is NOT archived — returning silently would make ``archive_task``
    respond HTTP 200 with a non-archived task, breaking the contract that a
    200 means the teardown completed. Raising lets the route surface a 5xx
    instead of a false success.
    """


class PromoteNotAllowed(Exception):
    """Raised when ``promote_task`` cannot run: the task is missing, the
    destination process isn't loaded, or the task is in a terminal state.

    Follows the ``ApproveNotAllowed`` / ``ReviseNotAllowed`` pattern — the
    route maps it to a 400 ``PROMOTE_NOT_ALLOWED``. Per ADR-027 §5 there are no
    state-aware preconditions beyond "non-terminal source": any non-terminal
    state is a valid promotion source.
    """


class ProcessNotFound(ValueError):
    """Raised when ``create_task`` is given a ``process_name`` that doesn't
    match any loaded process. The operator's recovery action is "add it to
    lotsa.yaml's processes: block" (or pick a bundled name).

    Per ADR-021 this is the *only* process-resolution error: any name in the
    loaded catalog is a valid dispatch target, so the former ``ProcessNotActive``
    ("loaded but not the active one") rejection path no longer exists.

    Inherits from ``ValueError`` so existing ``except ValueError:`` callers
    keep working.
    """


class ProjectNotFound(ValueError):
    """Raised when ``create_task`` is given a ``project_id`` that doesn't match
    any registered project, or whose path is not a git repository (ADR-029).

    Validation happens at task-creation time — not first dispatch — so the
    operator learns immediately. Inherits from ``ValueError`` so existing
    ``except ValueError:`` callers keep working.
    """


OPERATIONAL_PREAMBLE = """\
## Lotsa Operational Rules (authoritative)

You are running as a step in Lotsa's orchestrated flow. These rules
govern Lotsa's flow itself — branch lifecycle, push behavior, file
scope, and the marker protocol the orchestrator uses to advance the
task. **They take precedence over project `CLAUDE.md`, `AGENTS.md`,
or any other convention file when there is a conflict on these
matters.** Project conventions still inform domain decisions (code
style, naming, architecture, testing patterns) — that's their
channel — but on the operational rules below, Lotsa wins.

### How to communicate with the operator

- **Your final stdout becomes a message in the audit log.** The
  operator reads it via the dashboard, asynchronously. State
  decisions, findings, and rationale explicitly — they cannot see
  your tool-call reasoning, only your final report.
- **For blocking questions, emit `NEEDS_INPUT: <question>` as your
  final line and stop.** The orchestrator catches the marker,
  pauses the task, surfaces the question in the dashboard,
  captures the operator's answer, and resumes you on the next
  dispatch with their answer in your context. NEEDS_INPUT is the
  only blocking-question channel that works.
- **For non-blocking judgment calls, state the decision plus
  rationale and proceed.** The operator can redirect by replying
  via the dashboard's chat input; their reply arrives on your next
  dispatch under `## Revision Feedback`.

### Git authority

- **The orchestrator owns git state.** It created a per-task worktree
  on the dedicated branch `lotsa/<task_id>` before dispatching you.
  That branch is where your work belongs.
- **Do not create, switch, rebase, or reset branches.** Running
  `git checkout -b`, `git branch`, `git rebase`, `git reset --hard`,
  or any equivalent pulls work off the orchestrator's branch and
  breaks the push step downstream. If a project `CLAUDE.md` documents
  a branch-naming convention like `feature/issue-N-…`, treat it as
  *informational about the project's normal workflow* — Lotsa's
  per-task worktree is the active workspace for your dispatch.
- **Do not commit. Do not push.** Commit and push are both
  orchestrator-owned steps. Leave your changes staged or unstaged in
  the worktree; the orchestrator commits your work deterministically
  after your step (a `commit` posthook), then pushes it. Authoring your
  own commit is unnecessary and its message would be discarded.

### File scope

- Modify only files inside the worktree (your current working
  directory and its subtree). Do not write outside it.
- **Do not change the working directory.** Stay in the worktree the
  orchestrator placed you in. `cd`, `pushd`, `os.chdir`, `git -C
  <other-path>`, and equivalent escapes are forbidden — your
  worktree is the workspace for this dispatch. The worktree's
  `.git` file points at the operator's main checkout for shared
  object storage; do not follow that pointer to navigate or operate
  there. If a tool or test reports a path outside the worktree as
  "the project root," trust the worktree's path, not the tool.

"""

_NEEDS_INPUT_RE = re.compile(r"^NEEDS_INPUT:\s*(.+)", re.MULTILINE)

_MAX_TITLE_LEN = 80


def _auto_title(message: str) -> str:
    """Derive a short title from a chat message.

    Strategy: take the first sentence (split on '.') or first line (split
    on '\\n'), whichever is shorter, then truncate to 80 characters.
    """
    # First line
    first_line = message.split("\n", 1)[0].strip()
    # First sentence
    first_sentence = message.split(".", 1)[0].strip()

    # Use whichever is shorter (but non-empty)
    candidates = [c for c in (first_sentence, first_line) if c]
    if not candidates:
        return "Untitled"
    title = min(candidates, key=len)

    if len(title) > _MAX_TITLE_LEN:
        title = title[: _MAX_TITLE_LEN - 1].rstrip() + "\u2026"
    return title


def _run_stats(result: AgentResult) -> dict | None:
    """Build metadata dict with run stats from an AgentResult."""
    stats: dict = {}
    if result.duration_ms:
        stats["duration_ms"] = result.duration_ms
    if result.input_tokens is not None:
        stats["input_tokens"] = result.input_tokens
    if result.output_tokens is not None:
        stats["output_tokens"] = result.output_tokens
    if result.cost_usd is not None:
        stats["cost_usd"] = result.cost_usd
    return stats or None


def _extract_needs_input(stdout: str) -> str | None:
    """Extract the last NEEDS_INPUT question from agent output."""
    matches = _NEEDS_INPUT_RE.findall(stdout)
    return matches[-1].strip() if matches else None


_PR_FIX_NEEDS_DECISION_RE = re.compile(r"^PR_FIX_NEEDS_DECISION:\s*(.*)$", re.MULTILINE)


def _extract_needs_decision_question(stdout: str) -> str:
    """Extract the question from a PR_FIX_NEEDS_DECISION: marker.

    Returns the trimmed question text after the marker. If the marker is
    present but has no question text, returns a fallback placeholder so
    the operator-facing chat input never renders an empty prompt.
    """
    m = _PR_FIX_NEEDS_DECISION_RE.search(stdout or "")
    if m:
        text = m.group(1).strip()
        if text:
            return text
    return "Agent emitted PR_FIX_NEEDS_DECISION without a question."


_PR_FIX_MARKER_PREFIX_RE = re.compile(r"^PR_FIX_(?:DONE|SKIPPED|BLOCKED|NEEDS_DECISION):\s*(.*)$")
# A pr-fix dispatched right after resolve_conflicts receives that agent's stdout
# as feedback (the rule-route carry-forward, ``feedback=result.stdout``), which
# carries this marker. Used to recognise that "feedback" as the conflict-
# resolution echo, not genuine reviewer input.
_CONFLICTS_RESOLVED_RE = re.compile(r"^CONFLICTS_RESOLVED:", re.MULTILINE)


def _strip_pr_fix_marker_prefix(line: str) -> str:
    """Strip a ``PR_FIX_<MARKER>:`` prefix from ``line`` if present.

    The drainer captures the agent's outcome by scanning the tail of stdout
    for the last non-empty line and writing it into the ``pr_decision``
    audit row's ``reasoning`` field. That line typically still carries the
    marker prefix (``PR_FIX_DONE: addressed the lint comments``), while
    the parallel NEEDS_DECISION path uses
    ``_extract_needs_decision_question`` which strips the marker. Without
    this helper the audit field's format would diverge across decision
    types, forcing display and query callers to pattern-match per
    ``decision`` value.

    Returns the substring after the marker (whitespace-trimmed) when the
    line starts with one of the four ``PR_FIX_*`` markers; otherwise
    returns the input unchanged. Empty/whitespace input returns ``""``.
    """
    if not line:
        return ""
    m = _PR_FIX_MARKER_PREFIX_RE.match(line.strip())
    if m:
        return m.group(1).strip()
    return line.strip()


def _feedback_is_actionable(feedback: str | None) -> bool:
    """Whether feedback delivered to a pr-fix dispatch was real.

    A ``PR_FIX_SKIPPED`` only counts toward ``max_consecutive_skipped`` when
    the agent actually had something to skip. These are benign — the agent
    correctly had nothing to do, so such skips must not burn the cap:

    - Empty/whitespace feedback (an empty retry, or a dispatch with no operator
      text and nothing pending) and the ``aggregate_feedback``
      ``"No specific feedback found."`` sentinel (an internal task: in-progress-
      review skips tripped the cap before the real review landed).
    - The ``resolve_conflicts`` echo: a pr-fix dispatched right after that step
      is fed its stdout (the ``CONFLICTS_RESOLVED:`` report) as feedback via the
      rule-route carry-forward. The conflict is already resolved, so skipping it
      is benign (internal tasks / 04ee0735: the echo skip burned the cap and
      re-blocked a conflict-resolved, review-ready PR).
    """
    delivered = (feedback or "").strip()
    if _CONFLICTS_RESOLVED_RE.search(delivered):
        return False
    return bool(delivered) and delivered != "No specific feedback found."


async def _read_head_sha(work_dir: Path) -> str | None:
    """Return the current HEAD SHA at *work_dir*, or ``None`` on failure.

    Uses ``asyncio.create_subprocess_exec`` to match the git-invocation
    pattern in ``lotsa.push_step`` — arguments are passed as separate
    tokens (no shell), keeping the event loop responsive across the git
    invocation.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = stdout.decode().strip()
    return sha or None


def _strip_spec_marker(stdout: str) -> str:
    """Remove the leading SPEC_COMPLETE: (or similar) marker line from *stdout*.

    The conversational rule's regex matches a line like ``SPEC_COMPLETE: foo``;
    the persisted artifact should be everything that follows that line, with
    surrounding whitespace trimmed.
    """
    lines = stdout.split("\n", 1)
    return lines[1].strip() if len(lines) > 1 else ""


# Anchors that mark where an artifact's real content starts. Agents narrate
# before delivering ("I have enough to write the PR description.") despite
# prompts forbidding preamble — task ``c94e3ed9`` shipped that narration line
# as a PR title. Structured artifacts open with a Markdown heading (specs,
# plans) or a Conventional Commits title (pr_description), so the earliest
# line matching either anchor is the content start; anything before it is
# discarded at capture.
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# Floor for a captured artifact's usable size. Below this, the step's output
# was narration-only or otherwise unusable — capture fails the step so the
# operator's Retry re-runs the agent, rather than persisting a garbage
# artifact that downstream prompt injection ({artifact:NAME}) would consume.
_MIN_ARTIFACT_CHARS = 20


class ArtifactCaptureError(Exception):
    """Raised when a step's declared output artifact is unusable after
    narration stripping — routes the dispatch to the standard failure path
    (task blocked; Retry re-runs the step)."""


def _strip_artifact_narration(text: str) -> str:
    """Drop leading agent narration from an artifact's content.

    Scans for the earliest line that looks like real content — a Markdown
    heading or a Conventional Commits title — and returns everything from
    that line on. When no anchor exists anywhere, returns the text unchanged
    (free-form artifacts from custom processes are legitimate); the caller's
    size floor still guards the degenerate cases.
    """
    stripped = text.strip()
    lines = stripped.splitlines()
    for i, line in enumerate(lines):
        candidate = line.strip()
        if _MD_HEADING_RE.match(candidate) or CC_TITLE_RE.match(candidate):
            return "\n".join(lines[i:]).strip()
    return stripped


# ── Data types ─────────────────────────────────────────────────────────


@dataclass
class InFlightStep:
    """Tracks one background agent execution."""

    item: Item
    step: FlowStep
    task: asyncio.Task | None = None
    feedback: str | None = None
    agent_result: AgentResult | None = None
    step_work_dir: Path | None = None
    # Registered name of the runner this step resolved to (ADR-023). Carried on
    # the in-flight record so the conversational drainer (a different method
    # from the dispatch body) can record ``agent_runner`` in chat metadata.
    agent_runner_name: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    # IDs of GitHub comments captured by the PrMonitor at dispatch time.
    # Plumbed to the ``pr_decision`` audit row so an operator can cross-
    # reference the decision with the ``pr_feedback`` rows it responded to.
    triggering_comment_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of ``_sync_branch_to_main`` (ADR-015).

    ``status`` is the closed set of non-error outcomes:
      - ``already_current`` — ``HEAD..origin/main`` is empty; no merge/push.
      - ``clean`` — origin/main auto-merged and the merged ref was pushed.
      - ``conflicts`` — the auto-merge conflicted; ``conflicting_files`` names
        the unmerged paths; conflict markers remain in the worktree for the
        ``resolve_conflicts`` agent step (ADR-015 Phase 2).

    Fetch/push failures are raised, not encoded here (per the ADR they ride
    the generic dispatch error path to ``blocked``).
    """

    status: Literal["already_current", "clean", "conflicts"]
    conflicting_files: tuple[str, ...] = ()


@dataclass
class TaskSummary:
    """Lightweight task info for list views."""

    id: str
    title: str
    state: str  # legacy compat field, do not branch on it
    priority: int
    created_at: str
    status: TaskStatusLiteral = "working"
    current_step: str | None = None
    elapsed_s: int = 0
    is_conversational: bool = False
    # ADR-029 — the task's project (FK). Surfaced so the task list can render a
    # project badge and the project filter.
    project_id: str = "default"
    # ADR-017 soft-timeout indicator. Computed at response-build time from
    # ``elapsed_s`` against the active step's thresholds. ``ok`` (no dot) /
    # ``warn`` (yellow) / ``over`` (red). Stays ``ok`` for non-in-flight tasks.
    timeout_status: Literal["ok", "warn", "over"] = "ok"
    metadata: dict = field(default_factory=dict)


@dataclass
class TaskDetail(TaskSummary):
    """Full task info — adds body, flow_name, work_dir, and project context."""

    body: str = ""
    flow_name: str = ""
    work_dir: str = ""
    # ADR-029 — project name/path surfaced in the task detail view.
    project_name: str = ""
    project_path: str = ""


# ── Service ────────────────────────────────────────────────────────────

# The default runner's model-name prefixes live in ``rigg.agent_runner``
# as ``DEFAULT_RUNNER_PREFIXES`` (imported above). The built-in
# ``ClaudeCodeRunner`` self-registers under them at import time; the
# orchestrator re-registers the config-derived runner shape under the same
# ``default`` slot at ``start()`` so ``--docker`` / ``--runner`` still pick the
# shape (ADR-028 reconciliation). Sharing the one constant keeps the
# import-time fallback and the start-time registration from drifting apart.


class OrchestratorService:
    """Headless orchestration service.

    Dispatches agents, manages state transitions, and emits events.
    No I/O — designed to be wrapped by a web server or CLI.
    """

    def __init__(self, config: LotsaConfig, db: TaskDB) -> None:
        self.config = config
        self.db = db
        self.source = SQLiteItemSource(db)
        self.flow: FlowConfig | None = None
        self.process: Process | None = None
        # ``_processes`` is the catalog of every process loaded at start() time
        # — the FULL bundled catalog (``PRESET_NAMES``, each keyed by its preset
        # name, e.g. ``full``/``chat``/``quickfix``) PLUS every entry from
        # ``lotsa.yaml``'s ``processes:`` block, PLUS an explicit ``--flow-file``
        # process (keyed by its YAML's declared ``process:`` field). ADR-034 §1
        # loads every bundled preset (not just the active one) so each is a
        # pickable new-task option and a valid promotion target; an inline entry
        # sharing a preset's name wins (inline is authoritative, never
        # overwritten). ``self.process`` points at the active one (selected by the
        # ``flow:`` / ``--flow`` / ``--process`` config field, an inline
        # ``default: true`` entry, or ``--flow-file``). Per ADR-021 the active
        # process is only the *default* applied at task-creation time and the
        # legacy fallback inside ``_process_for`` / ``_resolve_flow``; every
        # routing decision resolves from the task's own
        # ``metadata['process_name']`` instead. The catalog loads everything it
        # knows about so each task can dispatch against the process it declares,
        # and so ``GET /api/processes`` / the new-task UI can surface what's
        # available without re-parsing YAML.
        self._processes: dict[str, Process] = {}
        # Externally-visible name of the active process (the key in
        # ``_processes`` whose value is ``self.process``). For inline
        # processes this matches both the dict key and the process's
        # internal name. For bundled processes it's the preset name
        # (``full``/``standard``/``simple``), which differs from the
        # internal ``self.process.name`` (e.g. ``software_process``).
        self._active_process_name: str = ""
        # ADR-023 — the dispatch path resolves a runner per dispatch from the
        # process-global registry (``resolve_runner``) instead of holding one
        # instance for the whole process. ``_runner_override`` is a per-instance
        # escape hatch: when set (via the ``runner`` property, the long-standing
        # test-injection point), every dispatch on *this* service uses it,
        # bypassing the registry. It stays per-instance so two services in one
        # process don't fight over the global default slot.
        self._runner_override: AgentRunner | None = None
        # ADR-029 — per-project worktree resolution replaces the former single
        # ``self.worktree_manager`` (deleted entirely so any straggler call site
        # fails loudly at startup, not silently into the wrong repo). Projects
        # are seeded/synced from ``lotsa.yaml`` in ``_sync_projects`` at start().
        # ``_projects`` holds EVERY DB project row (so tasks whose project was
        # later removed from YAML still resolve); ``_yaml_project_ids`` records
        # which are currently YAML-declared (only these are offered on the
        # new-task picker). ``_worktree_managers`` caches one manager per
        # project id (same shape as ADR-021's ``_process_for``).
        self._projects: dict[str, ProjectRow] = {}
        self._yaml_project_ids: set[str] = set()
        # Pre-seed the ``default`` manager from ``work_dir`` so it exists at
        # ``__init__`` time (as the former singleton did) — callers that resolve
        # before ``start()`` runs the project sync still get a manager. When the
        # resolved ``default`` project has a different repo path (an explicit
        # ``projects: default: {path: X}`` with no ``work_dir:``), the sync's
        # warm loop rebuilds this entry — ``_worktree_manager_for`` reconciles a
        # cached manager whose repo no longer matches the project's path, so the
        # pre-seed is never stale at dispatch time.
        self._worktree_managers: dict[str, WorktreeManager] = {
            "default": WorktreeManager(config.work_dir, config.data_dir / "worktrees" / "default")
        }

        self._completions: asyncio.Queue[InFlightStep] = asyncio.Queue()
        self._in_flight: dict[str, InFlightStep] = {}
        self._drainer_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        # ADR-021 — the derived per-process state below replaces the former
        # singletons (``_pr_monitor`` / ``_pr_monitor_config`` /
        # ``_action_states`` / ``_monitor_state``). Each is keyed by the
        # *catalog name* (the key in ``_processes`` / the value of a task's
        # ``metadata['process_name']``), which for bundled processes differs
        # from ``Process.name`` (``full`` → ``software_process``). Route every
        # per-process lookup through ``_process_name_for(task)`` so the keying
        # never diverges.
        #
        # ``_pr_monitors_by_process`` holds one engine instance per
        # monitor-bearing process (the engine declared via the ``engine:`` field
        # on that process's monitor job). Built-in ``pr_monitor`` resolves to
        # ``lotsa.engines.pr_monitor.PrMonitorEngine``; custom engines registered
        # via ``lotsa.yaml`` work the same way. Annotated ``Any`` because the
        # engine surface is duck-typed across registered engines — every engine
        # must expose ``run``, ``untrack``, ``snapshot_triggering_ids``, and (for
        # pr-fix flows) ``gather_pending_feedback``. ``_pr_monitor_tasks_by_process``
        # holds each engine's running ``run()`` poll task so shutdown can cancel
        # them all.
        self._pr_monitors_by_process: dict[str, Any] = {}
        self._pr_monitor_tasks_by_process: dict[str, asyncio.Task] = {}
        # ``_pr_monitor_configs_by_process`` is the parsed config of each
        # process's monitor job using engine=pr_monitor (absent for processes
        # with no monitor, or whose monitor uses a different engine — the cap
        # logic is pr-fix-specific and only triggers for the built-in engine).
        self._pr_monitor_configs_by_process: dict[str, PrMonitorConfig] = {}
        # ``_push_tasks`` and ``_dispatching_push`` are NOT deprecated — they
        # remain load-bearing for ``_execute_push`` (the legacy ``state="pushing"``
        # dispatch path) and for surfacing / re-driving tasks persisted with
        # ``state="pushing"`` or ``state="rebasing"`` from before ADR-014 Layer A
        # landed. ``_execute_action_step`` (the new typed-job action dispatcher)
        # does not have its own per-task guard set: concurrent action dispatches
        # are excluded by the generic ``_in_flight`` map (set in ``_dispatch_step``
        # before the action task is created), which is sufficient because actions
        # are not re-entrant via user-initiated entry points the way pr-fix is.
        #
        # ``_action_states_by_process`` maps each process's catalog name to the
        # SET of every action job's queue_state in that process — a custom
        # ``process.yaml`` can declare more than one action job, and the
        # restart-recovery sweep must flip any of them to ``blocked``, looked up
        # against the row's OWN process (ADR-021), not a global active-process
        # set. ``_monitor_states_by_process`` maps each process to its monitor
        # job's queue_state (or None when the process has no monitor).
        self._action_states_by_process: dict[str, set[str]] = {}
        self._monitor_states_by_process: dict[str, str | None] = {}
        self._push_tasks: dict[str, asyncio.Task] = {}
        self._dispatching_pr_fix: set[str] = set()
        self._dispatching_push: set[str] = set()
        self._dispatching_jump: set[str] = set()
        self._acknowledging_override: set[str] = set()

    @property
    def runner(self) -> AgentRunner:
        """The runner this service dispatches through.

        Returns the per-instance override when one was set, else the registry's
        resolution for the global ``config.model``. Kept as a property (ADR-023)
        so the long-standing ``svc.runner = FakeRunner()`` injection point still
        works: assigning sets a per-instance override that every dispatch
        prefers over the registry.
        """
        if self._runner_override is not None:
            return self._runner_override
        return resolve_runner(self.config.model).runner

    @runner.setter
    def runner(self, value: AgentRunner) -> None:
        self._runner_override = value

    def _resolve_runner(self, step: FlowStep) -> ResolvedRunner:
        """Resolve the runner for *step* (ADR-023 / ADR-028 Phase 3).

        Precedence (highest first):
        1. ``_runner_override`` — the ``svc.runner = …`` injection used by
           tests and programmatic callers; always wins, recorded as "default".
        2. ``step.runner`` — explicit per-step runner name (ADR-028 Phase 3).
           Resolved via ``resolve_runner_by_name`` (exact-name-only; raises
           ``RunnerNotFound`` on a miss — never silently falls to the default).
           The *model* passed to ``run()`` is still ``step.model or
           config.model``; ``runner:`` picks the runner shape, not the model.
        3. ``step.model or config.model`` — today's model-prefix resolution
           (ADR-022 / ADR-023): exact name → longest prefix → default.
        """
        if self._runner_override is not None:
            return ResolvedRunner("default", self._runner_override)
        if step.runner is not None:
            return resolve_runner_by_name(step.runner)
        model = step.model or self.config.model
        return resolve_runner(model)

    async def start(self) -> None:
        """Load flow, kill orphaned in-flight tasks, start the drainer.

        Restart safety: any task with status='working' was mid-execution when
        the server died. We mark it blocked with an explicit message and let
        the user explicitly retry. We do NOT try to reconstruct in-memory
        state from the DB — restart is destructive on purpose.
        """
        # Register built-in + user-supplied tools and engines BEFORE the YAML
        # parser runs so any ``tool:``/``engine:`` reference in process.yaml
        # can be resolved against the registry. The built-in import side
        # effects fire here so an isolated test (importing OrchestratorService
        # in a fresh process) sees a populated registry without each test
        # having to import the packages manually.
        import lotsa.engines  # noqa: F401 — registers built-in pr_monitor engine
        import lotsa.overrides  # noqa: F401 — registers built-in override handlers (ADR-019)
        import lotsa.tools  # noqa: F401 — registers built-in push_pr tool
        from lotsa.registry import load_user_engines, load_user_runners, load_user_tools

        if self.config.tools:
            load_user_tools(self.config.tools)
        if self.config.engines:
            load_user_engines(self.config.engines)

        # Agent-runner registry (ADR-023). Load user runners FIRST so a
        # ``runners:`` entry that claims a ``claude-*`` prefix is in place before
        # the default re-registration below — the default's prefixes are then the
        # last-wins baseline and a deliberate override stays in effect. Then
        # re-register the config-derived runner *shape* (``--docker`` /
        # ``--runner`` selection) under the global ``default`` slot, overriding
        # the import-time ``ClaudeCodeRunner`` default (ADR-028 reconciliation).
        if self.config.runners:
            load_user_runners(
                self.config.runners,
                model=self.config.model,
                budget_usd=self.config.budget,
                max_output_tokens=self.config.max_output_tokens,
            )
        register_runner(
            "default",
            _build_runner(self.config),
            prefixes=DEFAULT_RUNNER_PREFIXES,
            default=True,
        )

        # Built-in ``claude-agent-sdk`` name (ADR-028 Phase 3). Register only
        # when no operator ``runners:`` entry already claimed the name — the
        # load_user_runners call above runs first, so a deliberate override
        # stays in effect (last-registration-wins is already the registry rule,
        # but registering only-if-absent makes the intent explicit and avoids
        # clobbering an operator who intentionally named their runner the same).
        # Construction is safe without the package installed — the SDK import
        # is lazy inside ``run()``; the constructor only stores config.
        try:
            resolve_runner_by_name("claude-agent-sdk")
        except RunnerNotFound:
            from rigg import ClaudeAgentSDKRunner

            register_runner(
                "claude-agent-sdk",
                ClaudeAgentSDKRunner(
                    model=self.config.model,
                    budget_usd=self.config.budget,
                    max_output_tokens=self.config.max_output_tokens,
                ),
            )

        logger.info(
            "Registered runner prefixes (priority order): %s",
            [f"{prefix}->{name}" for prefix, name in registered_prefixes_in_priority_order()],
        )

        # ADR-017 — warn once if the installed Claude Code CLI is outside the
        # range the activity reader's JSONL parser has been validated against.
        # Best-effort: swallows a missing binary. Done here (not at runner
        # construction) so the import-time default runner doesn't shell out.
        # Offloaded to a thread because the helper shells out synchronously
        # (``subprocess.run`` with a 5s timeout) — running it inline would block
        # the event loop at startup if the ``claude`` binary is slow or absent.
        from rigg.agent_runner import warn_if_claude_version_untested

        await asyncio.to_thread(warn_if_claude_version_untested)

        # Load every process this orchestrator can dispatch.
        #
        # 1. Inline processes from ``lotsa.yaml``'s ``processes:`` block —
        #    each defined under a user-chosen name. The block parser resolves
        #    ``prompts_dir`` paths against the YAML's directory (so relative
        #    paths in lotsa.yaml work).
        # 2. The FULL bundled catalog (``PRESET_NAMES``) — ADR-034 §1. Every
        #    preset loads (not just the active one) so each is a pickable
        #    new-task option AND a valid promotion target (ADR-027).
        # 3. The file-loaded process named by ``--flow-file`` (below) — when
        #    set, it becomes the ACTIVE process. Otherwise the active process is
        #    one of the already-loaded entries selected by ``config.flow`` /
        #    an inline ``default: true``.
        if self.config.processes:
            base_dir = self.config.config_path.parent if self.config.config_path is not None else Path.cwd()
            for inline_name, raw_entry in self.config.processes.items():
                inline_process = build_process_from_inline(inline_name, raw_entry, base_dir=base_dir)
                self._processes[inline_name] = inline_process

        # ADR-034 §1 — load the FULL bundled catalog, not just the active preset.
        # Every preset becomes a pickable new-task option AND a valid promotion
        # target (ADR-027), which is the whole point of chat-first creation: a
        # task starts in ``chat`` and is promoted into ``full``/``quickfix``/…
        # without any of them being unreachable. Each preset is keyed by its
        # operator-facing name via the same ``build_process`` path the active
        # preset uses (DD2). The skip guard implements DD1 — an inline
        # ``processes:`` entry sharing a preset's name is authoritative, so we
        # never overwrite it. (Only inline entries are in ``_processes`` at this
        # point; the active process is resolved and loaded below.)
        for preset_name in PRESET_NAMES:
            if preset_name in self._processes:
                continue  # inline entry wins (DD1 — inline is authoritative, never overwrite)
            self._processes[preset_name] = build_process(preset_name, prompts_dir=self.config.prompts_dir)

        # The active process — what dispatch and ``self.process``/``self.flow``
        # point at. With the full catalog loaded above, this no longer gates
        # *what* loads; it only picks *which* loaded process is the default the
        # picker pre-selects (ADR-034 §4). Selection precedence (highest first;
        # see ``_select_active_process_name`` for the implementing logic):
        #   1. ``--flow-file`` (config.flow_file) — explicit file path wins
        #      over every other source.
        #   2. An inline entry with ``default: true``.
        #   3. ``config.flow`` (the ``--flow`` CLI flag or ``flow:`` YAML field),
        #      resolving against inline names first then bundled names.
        #   4. ``"chat"`` — the package default (ADR-034 §2).
        active_name = self._select_active_process_name()
        # ``--flow-file`` is documented as the highest-priority source.
        # Force the file-loading branch when it's set, even if the inline
        # catalog happens to contain ``active_name``. Otherwise an inline
        # process named the same as ``config.flow`` (or "chat") would
        # silently shadow the explicit file path — the documented
        # invariant in ``_select_active_process_name`` would be a lie.
        if active_name in self._processes and self.config.flow_file is None:
            # Process already in the catalog — an inline ``processes:`` entry
            # OR a bundled preset pre-loaded by the ADR-034 catalog loop above
            # (every preset is in ``_processes`` now, so a bundled active lands
            # here, not in the ``else`` build-by-name branch). Use it directly.
            self.process = self._processes[active_name]
        else:
            # Only two cases reach here now that ADR-034 pre-loads every
            # bundled preset into ``_processes`` (a bundled active takes the
            # ``if`` branch above):
            #
            #   1. ``--flow-file`` is set — build the process from the file and
            #      key it by ``self.process.name`` (the YAML's declared
            #      ``process:`` field), NOT by the placeholder ``active_name``
            #      (``config.flow`` or ``"chat"``). Keying by the file's own
            #      name avoids silently overwriting an inline catalog entry
            #      that happens to share that placeholder; the file's declared
            #      name is the honest label, and any colliding inline entry
            #      stays accessible in the catalog.
            #   2. ``active_name`` matches neither a bundled preset nor an
            #      inline entry — a misspelled ``--flow``/``flow:``.
            #      ``build_process`` raises ``ValueError`` below and we re-raise
            #      it with the full set of valid names.
            try:
                self.process = build_process(
                    active_name,
                    prompts_dir=self.config.prompts_dir,
                    process_file=self.config.flow_file,
                )
            except ValueError as exc:
                # ``build_process`` only knows about bundled presets; its
                # "Choose from: {PRESET_NAMES}" message (interpolating the
                # full ('simple', 'standard', 'full', 'chat', 'quickfix') tuple)
                # omits the inline catalog the orchestrator also accepts.
                # Re-raise with the full set of valid names when the
                # operator is in the no-file, not-a-preset branch — that
                # case can only be reached by a misspelled inline name (a
                # match would have been caught by the inline branch above).
                if self.config.flow_file is None and active_name not in PRESET_NAMES:
                    inline_names = sorted(self._processes.keys())
                    hint = (
                        f"Active process name {active_name!r} matched neither a bundled preset "
                        f"({list(PRESET_NAMES)}) nor an inline entry in lotsa.yaml's "
                        f"``processes:`` block (loaded: {inline_names}). "
                        f"Add it to ``processes:``, pick a bundled name, or supply ``--flow-file``."
                    )
                    raise ValueError(hint) from exc
                raise
            catalog_key = self.process.name if self.config.flow_file is not None else active_name
            self._processes[catalog_key] = self.process
            active_name = catalog_key
        self._active_process_name = active_name

        # Root flow defaults to "main"; fall back to the only flow when "main"
        # is absent.
        self.flow = self.process.flows.get("main") or next(iter(self.process.flows.values()))

        # Derive PR-phase plumbing per process (ADR-021). Each loaded process
        # contributes its own action states, monitor state, monitor engine, and
        # (for pr_monitor) config — keyed by catalog name so a task's routing
        # decisions resolve against its OWN process, not a global active one.
        # The new full process names its action job ``push_pr`` and its monitor
        # job ``wait_for_pr_signal`` — replaces the hardcoded ``pushing``/
        # ``waiting_for_pr`` synthetic states from the pre-ADR-014 model.
        from lotsa.registry import get_engine

        for proc_name, process in self._processes.items():
            action_states: set[str] = set()
            monitor_state: str | None = None
            for job in process.jobs:
                if job.type == "action":
                    action_states.add(job.queue_state)
                if job.type == "monitor":
                    # Engine via registry — the engine class is looked up by
                    # name (the ``engine:`` field on the monitor job) and
                    # instantiated with ``(orchestrator, monitor_state, config)``.
                    # Any engine registered via ``lotsa.yaml``'s ``engines:``
                    # block (or a built-in like ``pr_monitor``) works the same
                    # way. The registered-name check that
                    # ``_validate_registry_references`` ran at process-build time
                    # guarantees ``get_engine`` resolves; we wrap defensively
                    # anyway so a race between process load and engine teardown
                    # surfaces a clear error here rather than an opaque
                    # ``KeyError``.
                    try:
                        engine_cls = get_engine(job.engine or "")
                    except KeyError as exc:
                        raise RuntimeError(
                            f"Monitor job {job.name!r} declares engine {job.engine!r} "
                            f"which is not registered. This should have been caught at "
                            f"process-build time by _validate_registry_references; if you "
                            f"see this in production, the registry was mutated after build."
                        ) from exc
                    # Each monitor-bearing process gets its own engine instance,
                    # scoped to its own monitor state. Processes that happen to
                    # share a monitor_state string still dispatch correctly:
                    # every monitor→orchestrator callback resolves the task's own
                    # process per-task, and re-entrant dispatch is deduped by
                    # ``_dispatching_pr_fix`` + ``_in_flight`` (each process has
                    # at most one monitor job, so the last one wins per process).
                    monitor_state = job.queue_state
                    engine = engine_cls(self, job.queue_state, dict(job.config))
                    self._pr_monitors_by_process[proc_name] = engine
                    # The orchestrator's pr-fix cap logic
                    # (``_completion_drainer``'s SKIPPED branch,
                    # ``_pr_fix_round_cap_blocked``) reads
                    # ``max_consecutive_skipped`` / ``max_pr_fix_rounds`` /
                    # ``base_branch`` directly. These are pr_monitor-specific
                    # fields, so we only populate the config map when the engine
                    # is the built-in pr_monitor (the cap logic itself is
                    # pr-fix-specific and only triggers for that engine's tasks).
                    # A custom engine wouldn't surface here and the cap logic
                    # short-circuits because no pr-fix sub-flow gets dispatched.
                    #
                    # Reach into the engine's already-parsed config rather than
                    # calling ``parse_config`` a second time:
                    # ``PrMonitorEngine.__init__`` ran the parser when the
                    # instance was constructed above, so the typed dataclass
                    # already exists. The ``pr_monitor`` branch guards the type
                    # narrowing.
                    if job.engine == "pr_monitor":
                        self._pr_monitor_configs_by_process[proc_name] = engine.config
            self._action_states_by_process[proc_name] = action_states
            self._monitor_states_by_process[proc_name] = monitor_state

        # ADR-029 — seed/sync projects (and run path-change resets + legacy
        # worktree cleanup) BEFORE the restart recovery sweep, so projects exist
        # for per-task worktree resolution and any relocation reset lands first.
        await self._sync_projects()

        rows = await self.db.list_tasks()
        # Legacy synthetic states (pre-ADR-014) — pinned here so an upgrade
        # with rows persisted at ``state="pushing"``, ``state="rebasing"``,
        # or ``state="waiting_for_pr"`` still surfaces the push-specific
        # recovery message instead of the generic "agent killed" one.
        #
        # ``waiting_for_pr`` is included because the new SM has no edge from
        # that state: ``transition_task`` warn-and-returns (see line ~1599)
        # and ``block()`` warn-and-returns, so a legacy row stranded at
        # ``state="waiting_for_pr"`` would otherwise sit forever with the
        # engine polling it indefinitely (a real engine merge-detection would
        # call ``transition_task(task_id, "complete")`` and silently no-op).
        # Flipping it to ``blocked`` on the next restart lets the operator
        # decide whether to retry or abandon. Drop after the legacy-state
        # migration spec lands.
        _legacy_push_states = ("pushing", "rebasing", "waiting_for_pr")
        for row in rows:
            # Per-row isolation: a single row's recovery failure must NOT abort
            # the sweep and strand every later working task as a stuck
            # working-orphan — one that sits ``status="working"`` (not
            # retryable) until some *subsequent* restart happens to recover it.
            # Observed on an internal task: a review completion was lost on a
            # restart, and the next restart's sweep failed to flip the working
            # row, leaving it stuck for ~11h. The sweep ordering (most-recent
            # message first) meant a later-listed row was downstream of whatever
            # raised. Catch + log + continue so one bad row can't take the rest
            # down, and so partial-sweep failures are visible (they weren't).
            try:
                # Tasks mid-action (e.g. push_pr) when the server crashed need a
                # more specific message so the user knows what to retry. ADR-021:
                # look up the row's OWN process's action states — a legacy row with
                # no ``process_name`` falls back to the active process, while a row
                # recorded against a non-default process is checked against that
                # process's action states (not a global active-process set, which
                # would mis-route it).
                row_actions = self._action_states_by_process.get(self._process_name_for(row), set())
                push_state = row.state in row_actions or row.state in _legacy_push_states
                # ``blocked`` is already terminal-for-restart (avoids duplicate
                # recovery messages); ``archived`` is terminal full-stop and must
                # never be reopened — an archived row preserves its prior ``state``
                # (which may be an action/push state), so without this skip the
                # ``push_state`` branch below would flip it to ``blocked``.
                if row.status in ("blocked", "archived"):
                    continue
                if row.status == "working" or push_state:
                    await self._set_status(row.id, "blocked", row.current_step or row.state)
                    msg = (
                        f"Server restarted while task was {row.state} — moved to blocked. Retry when ready."
                        if push_state
                        else "Agent killed by server restart — click Retry."
                    )
                    await self.db.add_message(
                        row.id,
                        "system",
                        row.current_step or row.state,
                        msg,
                        "status_change",
                    )
            except Exception:
                logger.exception("restart recovery failed for task %s; continuing sweep", row.id)
        self._drainer_task = asyncio.create_task(self._completion_drainer())

        # Start one polling loop per monitor-bearing process (ADR-021). The
        # engine instances were already constructed in the per-process
        # job-iteration loop above (via the registry); here we just spawn each
        # ``run()`` task. Done after the recovery sweep so legacy rows are
        # already routed to ``blocked`` before any poller could see them.
        for proc_name, engine in self._pr_monitors_by_process.items():
            self._pr_monitor_tasks_by_process[proc_name] = asyncio.create_task(engine.run())

    async def shutdown(self) -> None:
        """Cancel all background work and clean up."""
        self._shutdown_event.set()

        # Cancel ALL monitor tasks first, then await them concurrently — a
        # serial cancel+await would make worst-case shutdown 5s × N processes.
        # Each monitor's `finally` block still runs (closing pooled httpx
        # clients) before the loop tears down; the 5s per-task budget now
        # overlaps across processes instead of summing.
        monitor_tasks = list(self._pr_monitor_tasks_by_process.values())
        for task in monitor_tasks:
            task.cancel()
        if monitor_tasks:
            await asyncio.gather(
                *(asyncio.wait_for(task, timeout=5.0) for task in monitor_tasks),
                return_exceptions=True,
            )
        self._pr_monitor_tasks_by_process.clear()
        self._pr_monitors_by_process.clear()

        for task in self._push_tasks.values():
            if not task.done():
                task.cancel()
        self._push_tasks.clear()

        if self._drainer_task:
            self._drainer_task.cancel()
            self._drainer_task = None

        for info in list(self._in_flight.values()):
            if info.task and not info.task.done():
                info.task.cancel()
        # Don't await in-flight tasks — they run subprocess.run in threads
        # that can't be interrupted. Just abandon them and exit.
        self._in_flight.clear()

    # ── Queries ────────────────────────────────────────────────────────

    async def list_tasks_async(self) -> list[TaskSummary]:
        """Return summaries of all tasks, enriched with runtime state."""
        tasks = await self.db.list_tasks()
        return self._enrich_summaries(tasks)

    def list_processes_summary(self) -> list[dict[str, Any]]:
        """Return a summary of every loaded process for the API / UI dropdown.

        Each entry is a plain dict (intentionally not a Pydantic model — the
        service layer stays decoupled from the HTTP layer's response types).
        Fields:

        - ``name``: the externally-visible key in ``_processes`` (an inline
          ``lotsa.yaml`` name, or the preset name for bundled processes).
        - ``is_active``: ``True`` for the *configured default* process — the
          one new tasks dispatch against when the caller doesn't pick a
          process. Per ADR-021 it is no longer "the only one that works": any
          loaded process is a valid ``POST /api/tasks`` target.
        - ``is_default``: ``True`` for the inline entry with
          ``default: true``. Note that ``is_active`` and ``is_default`` can
          diverge — ``--flow``/``--process`` at startup can pick a non-default.
        - ``step_names``: the ordered job names of the process's main flow.

        Ordering is stable: the active entry first, then alphabetical. This
        means the new-task dropdown's first item is the default selection
        without the UI needing to sort client-side.
        """
        inline_defaults = {
            name
            for name, entry in self.config.processes.items()
            if isinstance(entry, dict) and entry.get("default") is True
        }
        active_name = self._active_process_name
        summaries: list[dict[str, Any]] = []
        for name, process in self._processes.items():
            flow = process.flows.get("main") or next(iter(process.flows.values()))
            summaries.append(
                {
                    "name": name,
                    "is_active": name == active_name,
                    "is_default": name in inline_defaults,
                    "step_names": [s.name for s in flow.steps],
                    # ADR-027 §3/§4 — surfaced so the dashboard promotion modal
                    # can render per-destination input fields and so the chat
                    # triage block can describe each destination.
                    "description": process.description,
                    "promotion_inputs": [
                        {"name": pi.name, "description": pi.description} for pi in process.promotion_inputs
                    ],
                }
            )
        summaries.sort(key=lambda s: (not s["is_active"], s["name"]))
        return summaries

    @staticmethod
    def _timeout_status(elapsed_s: int, step: FlowStep | None) -> Literal["ok", "warn", "over"]:
        """Classify *elapsed_s* against the active *step*'s soft-timeout thresholds.

        ``over`` (red) wins over ``warn`` (yellow); a missing step or unset
        threshold yields ``ok`` (no dot). Informational only — ADR-017 ships the
        indicator, not auto-kill, so ``timeout_kill_seconds`` just drives the dot.
        """
        if step is None:
            return "ok"
        kill = step.timeout_kill_seconds
        warn = step.timeout_warn_seconds
        if kill is not None and elapsed_s >= kill:
            return "over"
        if warn is not None and elapsed_s >= warn:
            return "warn"
        return "ok"

    def _enrich_summaries(self, tasks: list[TaskRow]) -> list[TaskSummary]:
        summaries = []
        for t in tasks:
            # Resolve against the task's ACTIVE flow first (see
            # ``_resolve_step_for_row`` — active flow → root → catalog) so
            # ``is_conversational``/``timeout_status`` reflect the sub-flow step
            # actually running, not the same-named root job.
            step = self._resolve_step_for_row(t)
            summary = TaskSummary(
                id=t.id,
                title=t.title,
                state=t.state,
                priority=t.priority,
                created_at=t.created_at,
                status=t.status,
                current_step=t.current_step,
                is_conversational=bool(step and step.conversational),
                project_id=t.project_id,
                metadata=t.metadata,
            )
            if t.id in self._in_flight:
                info = self._in_flight[t.id]
                summary.elapsed_s = int(time.monotonic() - info.started_at)
                summary.timeout_status = self._timeout_status(summary.elapsed_s, step)
            summaries.append(summary)
        return summaries

    async def get_task(self, task_id: str) -> TaskDetail | None:
        """Get full task detail."""
        row = await self.db.get_task(task_id)
        if row is None:
            return None
        # ADR-029 — resolve the worktree against the task's project.
        project = self._projects.get(row.project_id)
        wt_path = self._worktree_manager_for(project).get_path(row.id) if project else None
        # Resolve against the task's ACTIVE flow first (see
        # ``_resolve_step_for_row`` — active flow → root → catalog) so
        # ``is_conversational`` reflects the sub-flow step actually running, not
        # the same-named root job.
        step = self._resolve_step_for_row(row)
        detail = TaskDetail(
            id=row.id,
            title=row.title,
            state=row.state,
            priority=row.priority,
            created_at=row.created_at,
            status=row.status,
            current_step=row.current_step,
            is_conversational=bool(step and step.conversational),
            body=row.body,
            flow_name=row.flow_name,
            work_dir=str(wt_path) if wt_path else "",
            project_id=row.project_id,
            project_name=project.name if project else "",
            project_path=project.path if project else "",
            metadata=row.metadata,
        )
        if row.id in self._in_flight:
            info = self._in_flight[row.id]
            detail.elapsed_s = int(time.monotonic() - info.started_at)
            detail.timeout_status = self._timeout_status(detail.elapsed_s, step)
        return detail

    async def get_agent_activity(
        self,
        task_id: str,
        since_index: int = 0,
        limit: int = 200,
    ) -> tuple[str | None, ActivityResult] | None:
        """Read in-flight agent activity for a task (ADR-017).

        Returns ``None`` when the task doesn't exist (the API maps that to 404).
        Otherwise returns ``(session_id, ActivityResult)`` — ``session_id`` is
        ``None`` when the task hasn't dispatched yet (no ``session_id`` in
        metadata). Never raises for a missing file / parse error / a runner that
        doesn't implement ``read_activity``: those degrade to an empty result so
        the dashboard shows an empty state, never a 500.
        """
        row = await self.db.get_task(task_id)
        if row is None:
            return None

        session_id = row.metadata.get("session_id")
        # Resolve the runner this task used: prefer the in-flight record's
        # resolved runner (honours a per-step ``runner:`` choice), else the
        # service default. The JSONL persists after dispatch, so activity is
        # still readable once the task leaves ``_in_flight``.
        runner: AgentRunner = self.runner
        info = self._in_flight.get(task_id)
        if info is not None and info.agent_runner_name:
            with contextlib.suppress(RunnerNotFound):
                runner = resolve_runner_by_name(info.agent_runner_name).runner

        read = getattr(runner, "read_activity", None)
        if read is None:
            # A runner (e.g. a third-party or test double) without the Protocol
            # method — the dashboard shows "Activity unavailable for this runner".
            return session_id, ActivityResult(events=[], supported=False)

        if not session_id:
            # Not dispatched yet — the runner supports activity, there's just no
            # session. The dashboard keys "not dispatched" off the null session.
            return None, ActivityResult(events=[], supported=True)

        # ADR-029 — resolve the worktree against the task's project (best-effort:
        # this method must never 500, so fall back to the namespaced on-disk path
        # without raising when the project is unknown).
        project = self._projects.get(row.project_id)
        wt_path = self._worktree_manager_for(project).get_path(task_id) if project else None
        work_dir = wt_path or (self.config.data_dir / "worktrees" / row.project_id / task_id)
        try:
            result = await read(session_id, work_dir, since_index, limit)
        except Exception:
            logger.debug("read_activity failed for task %s", task_id, exc_info=True)
            return session_id, ActivityResult(events=[], supported=True)
        return session_id, result

    async def get_messages(self, task_id: str, step_name: str | None = None) -> list[MessageRow]:
        """Get conversation messages for a task."""
        return await self.db.get_messages(task_id, step_name=step_name)

    async def get_message_by_id(self, task_id: str, message_id: int) -> MessageRow | None:
        """Fetch a single message scoped to *task_id*, or ``None`` if absent."""
        return await self.db.get_message_by_id(task_id, message_id)

    async def get_diff(self, task_id: str) -> str:
        """Return the task worktree's full PR-style unified diff.

        Delegates to ``lotsa.diff.compute_branch_diff``: a merge-base diff of
        the ``lotsa/<task_id>`` branch against the branch it was created from
        (resolved from the task's project's ``WorktreeManager.default_branch``),
        capturing committed + staged + unstaged tracked changes plus
        untracked/new files. Read-only (ADR-013) and best-effort — returns ``""``
        when there's no worktree yet or nothing has changed.
        """
        row = await self.db.get_task(task_id)
        if row is None:
            return ""
        project = self._projects.get(row.project_id)
        if project is None:
            return ""
        wtm = self._worktree_manager_for(project)
        wt_path = wtm.get_path(task_id)
        if not wt_path:
            return ""
        # ``compute_branch_diff`` is already best-effort — it catches and
        # swallows every failure internally, returning "" — so no outer guard
        # is needed here.
        return await compute_branch_diff(
            Path(wt_path),
            wtm.default_branch,
        )

    async def get_question(self, task_id: str) -> str | None:
        """Return the latest ``NEEDS_INPUT`` question for a task, or ``None``."""
        questions = await self.db.get_messages(task_id, msg_type="question")
        if not questions:
            return None
        return questions[-1].content

    async def get_task_totals(self, task_id: str) -> dict:
        """Sum duration, tokens, and cost across all dispatch events."""
        messages = await self.db.get_messages(task_id, msg_type="status_change")
        total_ms = 0
        total_tokens = 0
        total_cost = 0.0
        for msg in messages:
            try:
                event = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                continue
            if event.get("type") != "dispatch":
                continue
            total_ms += event.get("duration_ms") or 0
            total_tokens += (event.get("input_tokens") or 0) + (event.get("output_tokens") or 0)
            if event.get("cost_usd") is not None:
                total_cost += event["cost_usd"]
        secs = total_ms // 1000
        parts = []
        if secs:
            parts.append(f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s")
        if total_tokens:
            parts.append(f"{total_tokens:,} tokens")
        if total_cost:
            parts.append(f"${total_cost:.2f}")
        return {
            "total_duration_s": secs,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "display": " · ".join(parts),
        }

    async def get_named_artifact(self, task_id: str, artifact_name: str) -> str | None:
        """Get the content of a named artifact."""
        artifacts = await self.db.get_messages(task_id, msg_type="artifact")
        for msg in reversed(artifacts):  # latest first
            if msg.metadata.get("artifact_name") == artifact_name:
                return msg.content
        return None

    async def get_named_artifacts(self, task_id: str) -> list[MessageRow]:
        """Get all artifacts for a task in insertion order."""
        return await self.db.get_messages(task_id, msg_type="artifact")

    # ── Actions ────────────────────────────────────────────────────────

    async def create_task(
        self,
        title: str | None = None,
        body: str = "",
        priority: int = 0,
        message: str | None = None,
        process_name: str | None = None,
        project_id: str | None = None,
    ) -> TaskRow:
        """Create a new task and dispatch its first step.

        Args:
            title: Explicit task title. If omitted and message is provided,
                   a title is auto-generated from the message.
            body: Task body (used for non-conversational flows).
            priority: Task priority (0 = normal).
            message: Full user message. When provided, becomes the first
                     chat message and the title is derived from it.
            project_id: Which registered project (repo) the task belongs to
                        (ADR-029). When omitted, defaults to ``default`` if it
                        exists, else the sole registered project. Validated at
                        creation (must exist and be a git repo) — an unknown
                        project raises ``ProjectNotFound``.
            process_name: Which loaded process to dispatch this task against.
                          Must match a name in ``self._processes`` (the names
                          surfaced by ``GET /api/processes``). When omitted,
                          defaults to the active process. Per ADR-021 any
                          loaded process is a valid target — the task records
                          the name in metadata and every routing decision
                          resolves against it. An unknown name (not in the
                          catalog) raises ``ProcessNotFound``.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        # Resolve the requested process. ``self.process`` cannot be None here
        # because ``start()`` always populates it before ``create_task`` is
        # reachable, but mypy doesn't know that. Use an explicit guard rather
        # than ``assert`` so the check survives ``python -O`` and matches the
        # ``self.flow`` guard above.
        if self.process is None:
            raise RuntimeError("OrchestratorService not started")
        active_name = self._active_process_name
        # ADR-021: any loaded process is a valid dispatch target. The only
        # error is an unknown name (not in the catalog at all).
        if process_name is None:
            resolved_process_name = active_name
        elif process_name in self._processes:
            resolved_process_name = process_name
        else:
            available = sorted(self._processes.keys())
            raise ProcessNotFound(
                f"Unknown process {process_name!r}. Available: {available}. "
                f"Add it to ``lotsa.yaml``'s ``processes:`` block, or load a "
                f"bundled process by name (full/standard/simple)."
            )
        resolved_process = self._processes[resolved_process_name]
        resolved_flow = resolved_process.flows.get("main") or next(iter(resolved_process.flows.values()))

        # Resolve + validate the project (ADR-029). Validation is at create
        # time, not first dispatch, so the operator learns immediately.
        resolved_project_id = self._resolve_project_id(project_id)

        # Resolve title from message if not provided explicitly
        if title is None and message is not None:
            title = _auto_title(message)
        elif title is None:
            title = "Untitled"

        # The chat content is the full message (not the truncated title)
        chat_content = message or title

        # Label the task with the resolved process name, not ``config.flow``.
        # The two diverge when an inline ``default: true`` entry overrides
        # the configured ``flow:`` value (e.g. config.flow="full" but the
        # active inline process is "marketing_research"), or when the caller
        # picks a non-active process (ADR-021). Using config.flow here would
        # surface the wrong label in audit logs and the TaskDetail.flow_name
        # API response.
        flow_name = resolved_process_name
        # First step comes from the RESOLVED process's root flow (ADR-021), so
        # a task created against a non-active process dispatches that process's
        # pipeline rather than the active one's.
        first_step = resolved_flow.steps[0] if resolved_flow.steps else None
        # Persist the resolved process name in metadata so every routing
        # decision (dispatch, recovery sweep, transitions) resolves against it
        # without a schema migration. ``project_id`` is mirrored here too so the
        # ``Item`` dispatch path resolves the task's worktree manager without a
        # DB round-trip (same pattern as ``process_name``).
        metadata = {"process_name": resolved_process_name, "project_id": resolved_project_id}

        # Conversational first step: store message as chat, dispatch spec step
        if first_step and first_step.conversational:
            task = await self.db.create_task(
                title=title,
                body="",
                priority=priority,
                flow_name=flow_name,
                project_id=resolved_project_id,
                state=first_step.queue_state,
                metadata=metadata,
            )
            item = Item(
                id=task.id,
                state=first_step.queue_state,
                priority=priority,
                title=title,
                body="",
                metadata=metadata,
            )
            await self.db.add_message(task.id, "user", first_step.job_type, chat_content, "chat")
            await self._dispatch_step(item, first_step, feedback=chat_content)
            return task

        # Normal flow: create in backlog, dispatch immediately
        task = await self.db.create_task(
            title=title,
            body=body,
            priority=priority,
            flow_name=flow_name,
            project_id=resolved_project_id,
            metadata=metadata,
        )
        item = Item(id=task.id, state="backlog", priority=priority, title=title, body=body, metadata=metadata)
        # Store the original message as a chat message if provided
        if message is not None:
            step_name = first_step.job_type if first_step else ""
            await self.db.add_message(task.id, "user", step_name, message, "chat")
        await self._dispatch_next_step(item)
        return task

    async def _chat_transcript(self, task_id: str) -> str:
        """Render a task's chat conversation as a transcript for promotion handover.

        Returns ``""`` for a task with no chat turns (a non-chat-origin task), so
        the promote path only auto-carries context when there genuinely is a
        conversation. Captures the operator's full messages (``chat``) and the
        agent's replies (``output``) in order — the context ``spec`` needs so a
        promoted task isn't reduced to its truncated title.
        """
        messages = await self.db.get_messages(task_id)
        if not any(m.type == "chat" for m in messages):
            return ""
        lines: list[str] = []
        for m in messages:
            if m.type == "chat":
                lines.append(f"**User:** {m.content}")
            elif m.type == "output" and m.role == "agent":
                lines.append(f"**Assistant:** {m.content}")
        return "\n\n".join(lines)

    async def promote_task(
        self,
        task_id: str,
        to_process: str,
        initial_artifacts: dict[str, str] | None = None,
    ) -> None:
        """Switch a task to a different loaded process (ADR-027).

        Operator-only mid-life process change. The destination takes over from
        its first step via a single CAS-guarded, audited transition. The
        worktree, the ``messages`` log, and the task identity are unchanged;
        only ``metadata.process_name`` / ``metadata.current_flow`` / the state
        machine position move. Any work to carry forward is delivered as
        ``initial_artifacts`` (artifact-name → content), which the destination's
        first-step prompt reads (e.g. ``full``'s spec step reads ``draft_spec``).

        Preconditions (else ``PromoteNotAllowed``): the task exists, the
        destination is loaded, and the task is not terminal. Per ADR-027 §5
        there are no state-aware source preconditions — any non-terminal state
        is valid; the source's flow/sub-flow context is discarded.

        Returns ``None`` in two cases the caller cannot distinguish: a clean
        promotion, and a lost CAS race (a concurrent promote / terminal
        transition mutated the row underneath). On a lost race nothing is
        applied and the ``POST /promote`` route still returns HTTP 200 with the
        task's *current* state — same convention as ``approve``/``retry``/
        ``stop``. A caller that needs to confirm the switch took effect should
        re-read ``metadata.process_name`` rather than infer it from the 200.
        """
        if self.flow is None or self.process is None:
            raise RuntimeError("OrchestratorService not started")

        row = await self.db.get_task(task_id)
        if row is None:
            raise PromoteNotAllowed(f"Task {task_id} not found")
        # ADR-027 §7 — no demotion. Promotion flows OUT of ``chat`` into a
        # concrete process; a task is never promoted back INTO ``chat`` (it
        # would muddy what the task represents). The dashboard filters ``chat``
        # from the destination picker, but the CLI / raw API must enforce it
        # server-side too — otherwise ``lotsa promote <id> chat`` slips through.
        if to_process == "chat":
            raise PromoteNotAllowed(
                "Cannot promote to the chat process (ADR-027 §7): promotion "
                "flows out of chat, not into it. For a fresh conversation on "
                "related work, create a new task."
            )
        if to_process not in self._processes:
            # Exclude ``chat`` from the suggested destinations: it is loaded
            # like any other process but is never a valid promotion target (the
            # no-demotion guard above rejects it). Listing it here would invite
            # the operator to retry into a second confusing rejection.
            available = sorted(p for p in self._processes if p != "chat")
            raise PromoteNotAllowed(
                f"Unknown process {to_process!r}. Available: {available}. "
                f"Promotion destinations must be loaded (active process or a "
                f"``lotsa.yaml`` ``processes:`` entry)."
            )
        # ADR-027 §5 — terminal tasks cannot be promoted. ``complete`` /
        # ``abandoned`` are observable on either column depending on the path
        # that finalized the task, so check both. ``archived`` is status-only
        # (``archive()`` preserves the prior ``state`` — see status.py): it is
        # terminal full-stop and, critically, its worktree + ``lotsa/<task_id>``
        # branch have already been torn down, so reopening it would dispatch the
        # destination's first step against a workspace that no longer exists.
        # The sibling action method ``jump_to_step`` rejects the same triad.
        if row.status in {"complete", "abandoned", "archived"} or row.state in {"complete", "abandoned"}:
            raise PromoteNotAllowed(f"Cannot promote a terminal task (status={row.status!r}, state={row.state!r})")

        dest = self._processes[to_process]
        dest_flow = dest.flows.get("main") or next(iter(dest.flows.values()))
        if not dest_flow.steps:
            raise PromoteNotAllowed(f"Destination process {to_process!r} has no steps")
        first = dest_flow.steps[0]

        prior_process = self._process_name_for(row)
        # Reset to the destination's main flow and drop any stored session — the
        # destination's first step is a fresh agent dispatch, not a resume.
        new_meta = dict(row.metadata or {})
        new_meta["process_name"] = to_process
        new_meta["current_flow"] = "main"
        new_meta.pop("session_id", None)

        # Single CAS. This is a DELIBERATE cross-state-machine jump: the target
        # ``first.queue_state`` belongs to the destination's SM, not the
        # source's, so there is NO pre-validation against any SM here (unlike
        # ``approve``). The ``from_status``/``from_state`` guard protects against
        # concurrent mutation; the destination's queue→active edge is validated
        # later by ``_dispatch_step`` against the destination SM (resolved via
        # the now-updated ``metadata.process_name``). Folding the metadata write
        # into the same UPDATE keeps process_name and state consistent.
        result = await self.db.atomic_transition(
            task_id,
            from_status=row.status,
            from_state=row.state,
            to_status="working",
            to_state=first.queue_state,
            to_current_step=first.name,
            to_metadata=new_meta,
            audit_on_win=AuditRow(
                role="system",
                step_name=None,
                content=f"Promoted from {prior_process} to {to_process}",
                msg_type="process_promotion",
                metadata={"old_process": prior_process, "new_process": to_process},
            ),
        )
        if not result.won:
            logger.warning(
                "promote_task CAS lost for task %s (row changed underneath: status=%r state=%r)",
                task_id,
                row.status,
                row.state,
            )
            return

        # Keep the ``flow_name`` label column in step with the new process — it
        # feeds ``TaskDetail.flow_name`` and audit logs (the CAS above only moved
        # ``metadata.process_name``). A plain update is fine: it's a display label,
        # not safety-critical state.
        await self.db.update_task(task_id, flow_name=to_process)

        # The source process may have had a working agent mid-run — cancel it so
        # its eventual completion can't CAS the (now-stale) source state.
        if task_id in self._in_flight:
            await self._cancel_in_flight(task_id)

        # Handover (ADR-027 §4). When the caller supplies no explicit context,
        # carry the chat conversation forward so the destination's first step has
        # the FULL discussion — not just the (truncated) task title. Without this,
        # a task promoted from chat reaches e.g. ``spec`` with only ``{title}`` /
        # empty ``{body}`` and reports the request "cut off". Seeded under both the
        # generic ``promotion_context`` and ``draft_spec`` (what ``full``'s spec
        # step reads); harmless extras for destinations that read neither.
        seed_artifacts = dict(initial_artifacts or {})
        if not seed_artifacts:
            transcript = await self._chat_transcript(task_id)
            if transcript:
                seed_artifacts["draft_spec"] = transcript
                seed_artifacts["promotion_context"] = transcript

        # Each lands twice: as an ``artifact`` row (so the destination's first step
        # can read it via ``get_named_artifact`` / the ``{artifact:NAME}`` prompt
        # injection) and as an ``artifact_seeded`` audit row recording the source.
        for name, content in seed_artifacts.items():
            await self.source.save_artifact(
                task_id,
                first.job_type,
                content,
                metadata={"artifact_name": name, "source": "promotion"},
            )
            await self.db.add_message(
                task_id,
                "system",
                first.name,
                f"Seeded artifact '{name}' from promotion",
                "artifact_seeded",
                metadata={"artifact_name": name, "source": "promotion"},
            )

        # Dispatch the destination's first step. The item carries the updated
        # metadata so ``_resolve_flow``/``_process_for`` route against the
        # destination process.
        item = Item(
            id=row.id,
            state=first.queue_state,
            priority=row.priority,
            title=row.title,
            body=row.body,
            metadata=new_meta,
        )
        await self._dispatch_next_step(item)

    def _render_available_processes(self) -> str:
        """Render the loaded process catalog as an *available processes* block
        for the chat agent's triage prompt (ADR-027 §3).

        Data-driven, not a hardcoded taxonomy: each loaded process that carries
        a ``description`` contributes one line. Processes without a description
        are skipped (they opt out of triage), and the ``chat`` process excludes
        itself — the chat agent never suggests promoting to chat.
        """
        lines: list[str] = []
        for name, process in self._processes.items():
            if name == "chat" or not process.description:
                continue
            desc = " ".join(process.description.split())
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    async def approve(self, task_id: str) -> None:
        """Approve a waiting (or needs_input) task — advance to next step.

        Accepts ``status='waiting'`` (the normal gate) and, at an evaluate
        gate only, ``status='needs_input'`` (the operator overrides an
        agent counter-question to accept the gate output — see the status
        guard below).

        Race safety: two concurrent approve() calls (double-click, React
        re-fire) used to be guarded by a read-then-check ``row.state ==
        step.active_state`` idempotency check, but that read was from the
        same stale snapshot for both callers, so both could pass. We now
        atomically CAS-claim the ``(status=row.status, state=active_state)``
        transition in a single UPDATE; only one caller's rowcount is 1, the
        other returns silently.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise ApproveNotAllowed(f"Task {task_id} not found")
        # ``needs_input`` is accepted alongside ``waiting`` so an agent's
        # counter-question at an evaluate gate can't trap the operator: when
        # the agent answers a clarification *and* asks its own question
        # (emitting NEEDS_INPUT), the operator who is satisfied with the
        # gate's output can still Accept rather than being forced to answer
        # (which re-runs the whole step). Restricted to evaluate gates below —
        # accepting a non-gate step that merely asked a question is meaningless.
        if row.status not in ("waiting", "needs_input"):
            raise ApproveNotAllowed(f"approve() requires status in (waiting, needs_input), got {row.status!r}")
        # Resolve against the task's ACTIVE flow first (see
        # ``_resolve_step_for_row``). No sub-flow step has ``evaluate: true``
        # today (the only way to reach ``approve()`` from ``needs_input``), so
        # the active-flow path is moot for current flows — but routing through
        # the shared helper keeps step resolution consistent with the
        # active-flow validation below (line ~1583) and with the sibling action
        # methods, so a future sub-flow gate resolves its own ``success_state``
        # instead of the same-named root job's.
        step = self._resolve_step_for_row(row)
        if step is None:
            raise ApproveNotAllowed(f"Unknown current_step {row.current_step!r}")
        # Accept-from-needs_input is only meaningful at an evaluate gate (the
        # operator is overriding the agent's counter-question to accept the
        # gate's output). A non-gate step that emitted NEEDS_INPUT has no gate
        # to accept — the operator answers it instead.
        if row.status == "needs_input" and not step.evaluate:
            raise ApproveNotAllowed("approve() from needs_input is only valid at an evaluate gate")
        # Approve only applies at a GATE — a step with an ``output`` artifact to
        # accept or an ``evaluate`` gate. A plain conversational REPL (``chat``)
        # has neither and is ended by promoting or abandoning it (ADR-034/027),
        # not by approval — approving it would just complete the task with nothing
        # to accept. (This is why the chat panel must not show an Accept button.)
        if not step.output and not step.evaluate:
            raise ApproveNotAllowed(
                "This step is not an approval gate — nothing to accept. End a chat task by promoting or abandoning it."
            )
        if step.output:
            artifact = await self.get_named_artifact(task_id, step.output)
            if artifact is None:
                raise ApproveNotAllowed(f"Cannot approve — declared output artifact {step.output!r} not present")

        # Validate the transition exists in the state machine before claiming
        # — catches misconfigured flows without leaving a half-updated row.
        # Resolve the SM against the task's active flow so a sub-flow gate
        # (no such job exists today, but the cross-flow ``step`` fallback
        # above would let one through) is validated against its own SM
        # rather than the root SM. Without this, the root SM would either
        # raise spuriously (the sub-flow edge isn't registered there) or
        # silently pass on a stale root edge that doesn't reflect the
        # sub-flow's actual binding order.
        item_for_flow = Item(
            id=row.id,
            state=step.active_state,
            title=row.title,
            body=row.body,
            metadata=row.metadata,
        )
        active_flow_for_approve = self._resolve_flow(item_for_flow)
        if (step.active_state, step.success_state) not in active_flow_for_approve.state_machine.transitions:
            raise ApproveNotAllowed(f"No transition defined from {step.active_state!r} to {step.success_state!r}")

        # Atomic claim: exactly one concurrent approve call wins. Fold the
        # terminal status flip into the same CAS when success_state is
        # 'complete' — otherwise the (state='complete', status='working')
        # intermediate state is observable between this CAS and the post-CAS
        # _set_status. For non-terminal transitions, status='working' here
        # closes the crash window between the state advance and
        # _dispatch_step's later status='working' write; a crash with
        # status='waiting' would leave the row stuck (start()'s recovery
        # only sweeps 'working').
        approve_to_status: TaskStatusLiteral = "complete" if step.success_state == "complete" else "working"
        approve_to_step = None if step.success_state == "complete" else row.current_step
        result = await self.db.atomic_transition(
            task_id,
            from_status=row.status,
            from_state=step.active_state,
            to_state=step.success_state,
            to_status=approve_to_status,
            to_current_step=approve_to_step,
            audit_on_win=None,
        )
        if not result.won:
            # Concurrent approve is the expected loss path. But if state
            # drifted (legacy migration, bug, manual edit), the row is still
            # at its pre-approve status (waiting OR needs_input) on the same
            # step — the user clicks Accept and the button just re-appears
            # with no feedback. Log so the drift surfaces. Both accept-able
            # statuses are checked so a needs_input drift isn't silently
            # skipped (asymmetric-guard bug class, per CLAUDE.md).
            fresh = await self.db.get_task(task_id)
            if (
                fresh is not None
                and fresh.status in ("waiting", "needs_input")
                and fresh.current_step == row.current_step
            ):
                logger.warning(
                    "approve() CAS lost for task %s but row is still status=%r on step %r — "
                    "state=%r drifted from expected active_state=%r",
                    task_id,
                    fresh.status,
                    fresh.current_step,
                    fresh.state,
                    step.active_state,
                )
            return  # another concurrent approve already advanced this task

        item = Item(id=row.id, state=step.success_state, title=row.title, body=row.body, metadata=row.metadata)

        from_step = step.name
        # Record the override explicitly when the operator accepted past an
        # open agent question, so the audit trail shows the question was
        # consciously dismissed rather than answered.
        approve_text = (
            "Approved (accepted past the agent's open question)" if row.status == "needs_input" else "Approved"
        )
        await self.db.add_message(task_id, "user", step.name, approve_text, "feedback")

        # Use the next job in the flow ordering, not the queue_state lookup.
        # Lookup-by-state breaks when the success_state is a gate state (e.g.
        # plan \u2192 'planned'), since gate names aren't any step's queue_state and
        # the message would say "planned started" instead of "test started".
        # Resolve against the task's active flow (reusing the FlowConfig already
        # resolved for the SM check above) rather than ``self.flow`` (root/main):
        # a sub-flow step reached via the catalog fallback at the top of this
        # method isn't in ``self.flow.jobs``, so a root-only scan would leave
        # ``current_idx`` None and emit ``to_step=item.state`` (the gate/success
        # state name) instead of the next job's name. Mirrors the SM-check site.
        active_jobs = active_flow_for_approve.jobs
        current_idx = next((i for i, s in enumerate(active_jobs) if s.name == step.name), None)
        next_step_obj = (
            active_jobs[current_idx + 1] if current_idx is not None and current_idx + 1 < len(active_jobs) else None
        )
        if next_step_obj is not None:
            to_step = next_step_obj.name
        elif item.state == "complete":
            to_step = "complete"
        else:
            to_step = item.state
        await self.db.add_message(
            task_id,
            "system",
            "",
            f"\u2713 {from_step} approved \u2014 {to_step} started",
            "stage_transition",
            metadata={"from_step": from_step, "to_step": to_step},
        )
        # Status='complete' was already folded into the CAS above when
        # success_state == 'complete'; no separate _set_status needed here.

        await self._dispatch_next_step(item)
        await self._cleanup_worktree_if_done(item)

    async def revise(self, task_id: str, feedback: str) -> None:
        """Revise a task — re-dispatch the current step with feedback.

        For ``waiting_for_pr`` the feedback is combined with anything the
        PrMonitor has already gathered (review comments, failing checks)
        and dispatched as a pr-fix cycle.  For ``rebasing`` (legacy state),
        we transition back to ``waiting_for_pr`` first then run pr-fix —
        the user's message ("rebase on main") is the cue to resolve.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise ReviseNotAllowed(f"Task {task_id} not found")

        allowed_statuses = ("waiting", "needs_input", "waiting_for_pr")
        # ``rebasing`` is a legacy state from PR-flow that lives in the state
        # column rather than the status enum; surface it as a valid revise target.
        rebasing = row.state == "rebasing"
        if row.status not in allowed_statuses and not rebasing:
            raise ReviseNotAllowed(f"revise() requires status in {allowed_statuses}, got {row.status!r}")

        # NOTE: the feedback message is recorded inside each branch below
        # rather than unconditionally up-front. Doing it before the CAS in
        # the waiting/needs_input branch leaves a "ghost" feedback row in
        # the DB if the CAS loses — the loser's text is recorded but
        # nothing dispatches it, so the user thinks their input registered
        # but the agent never sees it. Each branch records the message at
        # the point where dispatch is committed.

        # waiting_for_pr — combine user feedback with anything the monitor
        # already gathered, then dispatch a pr-fix cycle. The pr-fix dispatch
        # guard prevents a concurrent monitor poll from racing this path.
        if row.status == "waiting_for_pr":
            # Record only after both guards pass:
            #   1. _dispatching_pr_fix — prevents duplicate feedback rows from
            #      a concurrent double-submit racing within this worker.
            #   2. fresh.status == 'waiting_for_pr' — prevents an orphan
            #      feedback row when PrMonitor transitions the task out of
            #      waiting_for_pr (e.g. PR merged) during the slow GitHub
            #      fetch in _build_revise_feedback. The user's message would
            #      otherwise show in chat history with no dispatch behind it.
            # Raise (rather than silently returning) when guard 1 fires so
            # the API returns 400 and the frontend can show "agent busy"
            # instead of accepting a 200 that quietly drops the user's text.
            if task_id in self._in_flight or task_id in self._dispatching_pr_fix:
                raise ReviseNotAllowed(
                    "Another dispatch is already in flight for this task — wait for it to finish, then retry."
                )
            self._dispatching_pr_fix.add(task_id)
            try:
                combined = await self._build_revise_feedback(row, feedback)
                # Hand the user-feedback text to _dispatch_pr_fix_locked rather
                # than writing it here. The locked dispatch CAS-claims the
                # transition; if PrMonitor's transition_task wins the race
                # (PR merged/closed mid-fetch), the dispatch returns False,
                # nothing gets dispatched, and no orphan feedback row is left
                # in chat history.
                await self._dispatch_pr_fix_locked(task_id, combined, user_feedback=feedback, operator_initiated=True)
            finally:
                self._dispatching_pr_fix.discard(task_id)
            return

        # rebasing — transition out first, then pr-fix.
        if rebasing:
            # Two-layer guard against duplicate audit messages from concurrent
            # revise() calls:
            #   1. In-process: _dispatching_pr_fix lets the second caller bail
            #      cleanly within a single FastAPI worker (sync SQLite means
            #      both reads can land before either CAS commits).
            #   2. Cross-process: atomic_transition CAS — only one writer
            #      flips (status='blocked', state='rebasing') in the DB.
            # We add to _dispatching_pr_fix up front (before the CAS) so the
            # second concurrent call short-circuits before it can write a
            # duplicate feedback row, then call _dispatch_pr_fix_locked to
            # avoid dispatch_pr_fix's own re-entrant guard check.
            # Raise (not silent return) so the API surfaces 400 — matches the
            # waiting_for_pr branch above.
            if task_id in self._in_flight or task_id in self._dispatching_pr_fix:
                raise ReviseNotAllowed(
                    "Another dispatch is already in flight for this task — wait for it to finish, then retry."
                )
            self._dispatching_pr_fix.add(task_id)
            try:
                # Land in the YAML-defined monitor state (e.g.
                # ``wait_for_pr_signal``), not the legacy synthetic
                # ``"waiting_for_pr"``. ``_dispatch_pr_fix_locked`` carries this
                # state straight into the pr_fix sub-flow's entry CAS, whose edge
                # ``(monitor_state, "pr-fixing")`` is the one
                # ``_register_cross_flow_edges`` registers. Hardcoding
                # ``"waiting_for_pr"`` would leave ``item.state`` with no matching
                # edge, so ``_dispatch_step`` would silently strand the task at
                # ``status="working"`` with no agent. ``"waiting_for_pr"`` stays as
                # the fallback for flows that define no monitor state.
                result = await self.db.atomic_transition(
                    task_id,
                    from_status=row.status,
                    from_state="rebasing",
                    to_state=self._monitor_state_for(row) or "waiting_for_pr",
                    to_status="waiting_for_pr",
                    to_current_step=row.current_step,
                    audit_on_win=None,
                )
                if not result.won:
                    return
                await self.db.add_message(task_id, "user", "", feedback, "feedback")
                await self.db.add_message(
                    task_id,
                    "system",
                    "",
                    "Recovering from rebasing — dispatching pr-fix",
                    "stage_transition",
                    metadata={"from_step": "rebasing", "to_step": "pr-fix"},
                )
                # Operator-initiated recovery — bypass the autonomous cap
                # (ADR-019 Commitment 5). The feedback row is already written
                # above, so ``user_feedback`` stays at its ``None`` default.
                await self._dispatch_pr_fix_locked(task_id, feedback, operator_initiated=True)
            finally:
                self._dispatching_pr_fix.discard(task_id)
            return

        # waiting / needs_input — re-dispatch the current step. Resolve against
        # the task's ACTIVE flow first (see ``_resolve_step_for_row``): a pr_fix
        # ``review`` resolved against root gets *main's* review (success_state
        # ``verify``), whose REVIEW_PASS auto-advance targets an edge absent from
        # pr_fix's SM and silently strands the task — the same failure mode
        # ``retry()`` fixes (an internal task).
        step = self._resolve_step_for_row(row)
        if step is None:
            raise ReviseNotAllowed(f"Unknown current_step {row.current_step!r}")

        # Phase 2 — when revising a pr-fix task (status=waiting/needs_input),
        # bump the round counter and snapshot the triggering comment IDs so the
        # ``pr_decision`` audit metadata matches ``_dispatch_pr_fix_locked`` /
        # ``answer()``. ADR-019 Commitment 5: the ``max_pr_fix_rounds`` cap is
        # NOT enforced on this operator-initiated path — only autonomous
        # dispatch (``_dispatch_pr_fix_locked`` with ``operator_initiated=False``)
        # blocks at the cap. The counter still increments for audit completeness.
        is_pr_fix = step.name == "pr-fix"
        current_rounds = 0
        if is_pr_fix:
            current_rounds = int(row.metadata.get("pr_fix_round_count", 0))

        # Atomic claim — closes the same TOCTOU race that approve() and retry()
        # had. Two concurrent revise() calls on the same waiting task would both
        # pass the status guard above and both reach _dispatch_step, spawning
        # duplicate agents on the worktree.
        result = await self.db.atomic_transition(
            task_id,
            from_status=row.status,
            from_state=row.state,
            to_state=row.state,
            to_status="working",
            to_current_step=step.name,
            audit_on_win=None,
        )
        if not result.won:
            return

        # Record the feedback only after the CAS wins — the loser's call
        # would otherwise leave a feedback row in the DB that no agent
        # ever sees (the dispatch passes ``feedback`` as a kwarg, not via
        # message history).
        await self.db.add_message(task_id, "user", "", feedback, "feedback")
        item = Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)
        # Phase 2 — for pr-fix: bump the round counter and snapshot the
        # comment IDs the monitor is tracking. Mirrors the post-CAS work
        # in ``_dispatch_pr_fix_locked`` and ``answer()`` so monitor-driven,
        # answer-driven, and revise-driven runs produce equivalent audit
        # metadata.
        triggering_ids: list[int] = []
        if is_pr_fix:
            await self._merge_task_metadata(item, {"pr_fix_round_count": current_rounds + 1})
            engine = self._monitor_engine_for(item)
            if engine is not None:
                triggering_ids = list(engine.snapshot_triggering_ids(task_id))
        await self._dispatch_step(item, step, feedback=feedback, triggering_comment_ids=triggering_ids)

    async def _build_revise_feedback(self, task: TaskRow, user_feedback: str) -> str:
        """Combine user revise feedback with any pending PR monitor feedback.

        The PR monitor may have collected reviews/comments/failing-checks in
        a partially-debounced state.  Pull those in, aggregate them, and
        prepend the user's message.

        On any error fetching pending feedback, fall back to the user
        message alone — better to dispatch with partial context than fail
        the revise altogether.
        """
        pending = await self._gather_pending_pr_feedback(task)
        if not pending:
            return user_feedback
        return f"## User feedback\n\n{user_feedback}\n\n---\n\n{pending}"

    async def _gather_pending_pr_feedback(self, task: TaskRow) -> str | None:
        """Aggregate the PR's current feedback for *task*, or ``None``.

        Orchestrator-owned feedback resolution (not the agent): reuses the
        monitor engine's deduped, ``pr_comments_since``-bounded aggregation,
        so a fetch never re-feeds comments the agent already addressed — the
        reason this is fetched here rather than by the agent shelling out to
        ``gh`` (which has no cursor and would re-address handled feedback).

        Returns ``None`` when there is no monitor engine, the PR coordinates
        / token are missing, the fetch errors, or nothing is pending. Callers
        treat ``None`` as "no feedback delivered" — the resulting pr-fix skip
        is then benign and does not count toward ``max_consecutive_skipped``
        (see the skip accounting in ``_run_agent``'s drainer).
        """
        engine = self._monitor_engine_for(task)
        if engine is None:
            return None
        owner = task.metadata.get("github_owner")
        repo = task.metadata.get("github_repo")
        pr_number = task.metadata.get("pr_number")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not (owner and repo and pr_number and token):
            return None
        try:
            pending = await engine.gather_pending_feedback(
                task_id=task.id,
                owner=owner,
                repo=repo,
                pr_number=int(pr_number),
                token=token,
                default_since=task.metadata.get("pr_pushed_at"),
            )
        except Exception:
            logger.exception("Failed to gather pending PR feedback for task %s", task.id)
            return None
        return pending or None

    async def answer(self, task_id: str, answer: str) -> None:
        """Answer a NEEDS_INPUT question — resume the agent.

        Phase 2: when the resumed step is ``pr-fix``, an operator answer
        produces a new agent run that costs budget like any other dispatch.
        The round-cap pre-check runs BEFORE the working CAS so an at-cap
        task transitions cleanly from ``needs_input`` → ``blocked`` instead
        of burning another round. After the CAS wins, the round counter is
        bumped and ``triggering_comment_ids`` is snapshotted from the
        monitor so the resumed run's eventual ``pr_decision`` row references
        the comments the agent is responding to.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise AnswerNotAllowed(f"Task {task_id} not found")
        if row.status != "needs_input":
            raise AnswerNotAllowed(f"answer() requires status='needs_input', got {row.status!r}")
        # Resolve against the task's ACTIVE flow first (see
        # ``_resolve_step_for_row``) so a pr_fix step's own ``success_state`` is
        # used, not the same-named root job's — the failure mode ``retry()``
        # fixes (an internal task). The catalog fallback inside the helper keeps
        # ``answer()`` on a NEEDS_DECISION pr-fix task (the canonical recovery
        # entry point) from raising on the sub-flow-only ``pr-fix`` job.
        step = self._resolve_step_for_row(row)
        if step is None:
            raise AnswerNotAllowed(f"Unknown current_step {row.current_step!r}")

        # Phase 2 — for pr-fix resumes, capture the round count for the
        # post-CAS counter bump. ADR-019 Commitment 5: the ``max_pr_fix_rounds``
        # cap is NOT enforced on this operator-initiated path — an operator
        # answering a ``PR_FIX_NEEDS_DECISION`` is supervised dialogue, not an
        # autonomous loop, so the very answer the agent requested can be
        # delivered even after the cap has fired. The counter still increments.
        is_pr_fix = step.name == "pr-fix"
        current_rounds = 0
        if is_pr_fix:
            current_rounds = int(row.metadata.get("pr_fix_round_count", 0))

        # Atomic claim — same TOCTOU shape as approve()/retry(). Two concurrent
        # answer() calls would both pass the needs_input guard and both spawn
        # an agent.
        result = await self.db.atomic_transition(
            task_id,
            from_status="needs_input",
            from_state=row.state,
            to_state=row.state,
            to_status="working",
            to_current_step=step.name,
            audit_on_win=None,
        )
        if not result.won:
            return

        item = Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)
        # Phase 2 — for pr-fix resumes: bump the round counter and snapshot
        # the comment IDs the monitor is tracking. Mirrors the post-CAS work
        # in ``_dispatch_pr_fix_locked`` so monitor-driven and answer-driven
        # runs produce equivalent audit metadata.
        triggering_ids: list[int] = []
        if is_pr_fix:
            await self._merge_task_metadata(item, {"pr_fix_round_count": current_rounds + 1})
            engine = self._monitor_engine_for(item)
            if engine is not None:
                triggering_ids = list(engine.snapshot_triggering_ids(task_id))
        await self.db.add_message(task_id, "user", step.job_type, answer, "answer")
        await self._dispatch_step(item, step, feedback=answer, triggering_comment_ids=triggering_ids)

    async def send_message(self, task_id: str, message: str) -> None:
        """Send a message in a conversational step — re-dispatch with --resume.

        Phase 2: when the resumed step is ``pr-fix`` (e.g. an operator using
        ``send_message()`` instead of ``answer()`` to resolve a NEEDS_DECISION
        escalation), this dispatches a new agent run that costs budget like any
        other entry point. Apply the same round-cap pre-check and post-CAS
        counter/triggering-ID bookkeeping that ``answer()``, ``revise()`` and
        ``_dispatch_pr_fix_locked`` apply — otherwise this fourth entry point
        silently bypasses ``max_pr_fix_rounds`` and emits ``pr_decision`` rows
        with empty ``triggering_comment_ids``, breaking audit-trail continuity.

        ``blocked`` is accepted alongside ``waiting``/``needs_input`` so the
        stop → amend → resume flow works: ``stop()`` parks a task at
        ``blocked`` preserving ``state``/``current_step``, and the operator's
        natural next move is to send a corrected message rather than a bare
        Retry (which re-runs the step without their input). The CAS below
        already reads ``from_status`` dynamically, and stopped tasks sit on
        their active state, which carries the revision self-loop.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise ReviseNotAllowed(f"Task {task_id} not found")
        if row.status not in ("waiting", "needs_input", "blocked"):
            raise ReviseNotAllowed(
                f"send_message() requires status in (waiting, needs_input, blocked), got {row.status!r}"
            )
        # Resolve against the task's ACTIVE flow first (see
        # ``_resolve_step_for_row``) so a pr_fix step's own ``success_state`` is
        # used, not the same-named root job's — the failure mode ``retry()``
        # fixes (an internal task). The catalog fallback inside the helper keeps the
        # conversational recovery path working for the sub-flow-only ``pr-fix`` job.
        step = self._resolve_step_for_row(row)
        if step is None:
            raise ReviseNotAllowed(f"Unknown current_step {row.current_step!r}")

        # Phase 2 — for pr-fix: capture the round count for the post-CAS
        # counter bump. ADR-019 Commitment 5: the ``max_pr_fix_rounds`` cap is
        # NOT enforced on this operator-initiated path (operator chat is
        # supervised dialogue); only autonomous dispatch blocks at the cap. The
        # counter still increments. Mirrors ``answer()``/``revise()``.
        is_pr_fix = step.name == "pr-fix"
        current_rounds = 0
        if is_pr_fix:
            current_rounds = int(row.metadata.get("pr_fix_round_count", 0))

        # Atomic claim — same TOCTOU shape as approve()/retry()/answer(). Two
        # concurrent send_message() calls would both reach _dispatch_step.
        result = await self.db.atomic_transition(
            task_id,
            from_status=row.status,
            from_state=row.state,
            to_state=row.state,
            to_status="working",
            to_current_step=step.name,
            audit_on_win=None,
        )
        if not result.won:
            return

        item = Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)
        # Phase 2 — for pr-fix: bump the round counter and snapshot the
        # comment IDs the monitor is tracking. Mirrors the post-CAS work
        # in ``answer()``/``revise()``/``_dispatch_pr_fix_locked`` so every
        # dispatch entry point produces equivalent audit metadata.
        triggering_ids: list[int] = []
        if is_pr_fix:
            await self._merge_task_metadata(item, {"pr_fix_round_count": current_rounds + 1})
            engine = self._monitor_engine_for(item)
            if engine is not None:
                triggering_ids = list(engine.snapshot_triggering_ids(task_id))
        await self.db.add_message(task_id, "user", step.job_type, message, "chat")
        await self._dispatch_step(item, step, feedback=message, triggering_comment_ids=triggering_ids)

    async def block(self, task_id: str) -> None:
        """Block a task — idempotent CAS transition to status='blocked'.

        Two concurrent block() calls produce exactly one ``Task blocked``
        message in the audit trail rather than two, matching the CAS
        pattern used by approve/answer/retry/revise/send_message.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        task = await self.db.get_task(task_id)
        if task is None:
            return
        # ``blocked`` → idempotent no-op; ``archived`` is terminal and must
        # never be moved out of (block() would otherwise CAS it to ``blocked``
        # since the CAS doesn't consult the status enum).
        if task.status in ("blocked", "archived"):
            return

        item = Item(id=task.id, state=task.state, title=task.title, body=task.body, metadata=task.metadata)

        # Pre-validate the FSM transition. Tasks in terminal/non-blockable
        # states (e.g., 'complete') don't have a (state, 'blocked') edge
        # registered; silently skip rather than raise InvalidTransition.
        #
        # ADR-021: validate against the task's OWN process state machine
        # (``_resolve_flow`` — sub-flow aware) rather than a global active-flow
        # SM. A task owned by a non-default process must be blockable against
        # that process's transitions; checking the active flow's SM would
        # silently no-op for any state the active process doesn't define.
        if (task.state, "blocked") not in self._resolve_flow(item).state_machine.transitions:
            return

        # ADR-014 Layer A — the monitor's live state is whatever the YAML job
        # names (e.g. ``wait_for_pr_signal`` in the bundled ``full`` process);
        # the legacy synthetic ``"waiting_for_pr"`` is still recognized so
        # tasks persisted under the old model untrack correctly during a
        # mid-rollout block(). Without both checks, untrack silently no-ops
        # for new-model tasks, leaking the ``_tracked`` entry in PrMonitor
        # (stale ``comments_since`` cursor / ``consecutive_failures`` counter
        # if the same task ever re-enters waiting_for_pr). ADR-021: the monitor
        # state is the task's OWN process's monitor state.
        _mon = self._monitor_state_for(task) or "waiting_for_pr"
        was_waiting_for_pr = task.state == _mon or task.state == "waiting_for_pr"

        result = await self.db.atomic_transition(
            task_id,
            from_status=task.status,
            from_state=task.state,
            to_state="blocked",
            to_status="blocked",
            to_current_step=task.current_step or task.state,
            audit_on_win=AuditRow(
                role="system",
                step_name=None,
                content="Task blocked",
                msg_type="status_change",
            ),
        )
        if not result.won:
            return

        if was_waiting_for_pr:
            engine = self._monitor_engine_for(task)
            if engine is not None:
                engine.untrack(task_id)

    async def retry(self, task_id: str) -> None:
        """Retry a blocked task — re-dispatch the step that failed.

        Raises RetryNotAllowed if status != 'blocked'. Uses current_step to
        determine which step to re-run.

        Push-retry routing:
          * ``state == "rebasing"``: raises RetryNotAllowed immediately —
            a non-fast-forward needs rebase-then-push, which is what
            ``revise()`` does (routes through pr-fix). A raw retry would
            just re-trigger the same NON_FAST_FORWARD failure.
          * ``current_step in {"push", "pushing"}`` or
            ``state == "pushing"``: routes back through ``_execute_push``
            (legacy synthetic-state code path; new typed-job action
            dispatch goes through ``_execute_action_step``).
        """
        if not self.flow:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise RetryNotAllowed(f"Task {task_id} not found")
        if row.status != "blocked":
            raise RetryNotAllowed(f"retry() requires status='blocked', got {row.status!r}")
        # NON_FAST_FORWARD recovery is rebase-then-push, not push-again — a
        # raw retry would just re-trigger the same NON_FAST_FORWARD failure.
        # revise() handles rebasing by routing through pr-fix.
        if row.state == "rebasing":
            raise RetryNotAllowed("Push was rejected (non-fast-forward) — use Revise to rebase and re-push, not Retry")

        # Push retry: the task crashed mid-push.  current_step may be a
        # sentinel ("push" / "pushing") or the prior state lived on the
        # legacy state column.  Either way, dispatch via _dispatch_next_step
        # with state='pushing' so it lands in _execute_push.
        push_retry = row.current_step in ("push", "pushing") or row.state == "pushing"
        if push_retry:
            # Atomic claim — same race shape as the non-push branch below.
            # _dispatching_push inside _dispatch_next_step already prevents
            # double-spawning of _execute_push, but without this CAS two
            # concurrent retries would still both write state='pushing' and
            # both append a "Retrying push" status_change message, polluting
            # the audit trail. CAS makes the loser a clean no-op.
            result = await self.db.atomic_transition(
                task_id,
                from_status="blocked",
                from_state=row.state,
                to_state="pushing",
                to_status="working",
                to_current_step="push",
                audit_on_win=AuditRow(
                    role="system",
                    step_name=None,
                    content="Retrying push",
                    msg_type="status_change",
                ),
            )
            if not result.won:
                return
            item = Item(id=row.id, state="pushing", title=row.title, body=row.body, metadata=row.metadata)
            # ADR-018 — pre-retry-from-blocked / pre-rebase-after-restart sync.
            # A push retry (incl. restart-recovery-blocked push-state tasks,
            # which keep state="pushing") re-runs the branch sync before
            # re-pushing, so a branch that fell behind origin/<default_branch>
            # while blocked merges upstream first. Mirrors the pr-fix retry sync
            # block below; the sync belongs on this re-entry/recovery path, NOT
            # in the forward push (the first push stays unsynced).
            try:
                sync_result = await self._sync_branch_to_main(item.id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Branch sync to main failed for task %s", task_id)
                await self._block_after_sync(
                    item,
                    from_status="working",
                    from_state="pushing",
                    message=f"Branch sync to main failed: {type(exc).__name__}: {exc}",
                    # Keep current_step on the push sentinel so a subsequent
                    # Retry re-enters this synced push branch rather than the
                    # generic pr-fix path.
                    to_current_step="push",
                    step_name="push",
                )
                return
            if sync_result.status == "conflicts":
                # (pushing, resolving_conflicts) is not a state-machine edge —
                # re-anchor the row at the pr_fix sub-flow's entry state
                # (pr-fixing) so _handle_conflict_dispatch's _dispatch_step
                # guard sees the real (pr-fixing, resolving_conflicts) edge.
                # Mirrors the pr-fix retry conflict path. The DB CAS is
                # unguarded by the SM, consistent with the blocked→pushing CAS
                # above (pushing is a non-SM legacy sentinel).
                reanchor = await self.db.atomic_transition(
                    task_id,
                    from_status="working",
                    from_state="pushing",
                    to_state="pr-fixing",
                    to_status="working",
                    to_current_step="pr-fix",
                    audit_on_win=None,
                )
                if not reanchor.won:
                    return
                item.state = "pr-fixing"
                # Pass the task's accumulated pr-fix round count, not a hardcoded
                # 0: _handle_conflict_dispatch writes current_rounds + 1 to
                # pr_fix_round_count, and the conflict re-anchor enters the pr_fix
                # sub-flow. Hardcoding 0 would reset an accrued budget (e.g. 5 → 1)
                # and let post-resolution pr-fix dispatches exceed max_pr_fix_rounds.
                # Mirrors the pr-fix retry conflict path (reads the same field).
                current_rounds = int(item.metadata.get("pr_fix_round_count", 0))
                await self._handle_conflict_dispatch(item, sync_result.conflicting_files, current_rounds)
                return
            await self._dispatch_next_step(item)
            return

        # Resolve ``current_step`` against the task's ACTIVE flow first (see
        # ``_resolve_step_for_row`` — active flow → root → catalog). ``root_flow``
        # is still needed below for the restart-from-first fallback when the
        # recorded step matches no job at all.
        root_flow = self._root_flow_for(row)
        step = self._resolve_step_for_row(row)
        if step is None:
            # Fall back to the first step if the recorded step is missing.
            # This can happen for legacy rows where _m002_backfill_status set
            # current_step from an FSM state name (e.g. "speccing") rather
            # than a job name (e.g. "spec"). Log so silent flow-restarts
            # surface in operator logs.
            if not root_flow.jobs:
                raise RetryNotAllowed("flow has no jobs")
            logger.warning(
                "retry() task %s current_step=%r matches no job; restarting from %r",
                task_id,
                row.current_step,
                root_flow.jobs[0].name,
            )
            step = root_flow.jobs[0]

        # Phase 2 — for pr-fix: capture the round count for the post-CAS
        # counter bump. ADR-019 Commitment 5: the ``max_pr_fix_rounds`` cap is
        # NOT enforced on retry — operator-initiated retry is supervised, so an
        # operator who has reviewed a cap-blocked task and clicks Retry gets a
        # fresh dispatch instead of being immediately re-blocked. (The override
        # action is the separate tool for resetting the counter itself.) Only
        # autonomous dispatch blocks at the cap. The counter still increments.
        is_pr_fix = step.name == "pr-fix"
        current_rounds = 0
        if is_pr_fix:
            current_rounds = int(row.metadata.get("pr_fix_round_count", 0))

        # Atomic claim: two concurrent retries on the same task (double-click,
        # parallel HTTP calls) would both pass the status='blocked' guard above
        # and both reach _dispatch_step, spawning two background agents on the
        # same worktree. Same shape as the approve() race fixed earlier.
        # CAS lands the row at step.queue_state so _dispatch_step's CAS can
        # advance queue_state → active_state in a second atomic write —
        # matches answer()/send_message() which CAS to a state that
        # _dispatch_step expects to find in the DB.
        result = await self.db.atomic_transition(
            task_id,
            from_status="blocked",
            from_state=row.state,
            to_state=step.queue_state,
            to_status="working",
            to_current_step=step.name,
            audit_on_win=None,
        )
        if not result.won:
            return  # another concurrent retry already won

        item = Item(id=row.id, state=step.queue_state, title=row.title, body=row.body, metadata=row.metadata)
        await self.db.add_message(task_id, "system", "", "Retrying", "status_change")
        # ADR-015 Phase 2 — a pr-fix retry re-runs the branch sync (symmetric
        # with ``_dispatch_pr_fix_locked``). On fetch/push error the task
        # blocks (generic retry path). On conflict, dispatch resolve_conflicts
        # via the shared helper — same as the monitor-driven path.
        if is_pr_fix:
            try:
                sync_result = await self._sync_branch_to_main(item.id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Branch sync to main failed for task %s", task_id)
                await self._block_after_sync(
                    item,
                    from_status="working",
                    from_state=step.queue_state,
                    message=f"Branch sync to main failed: {type(exc).__name__}: {exc}",
                )
                return
            if sync_result.status == "conflicts":
                await self._handle_conflict_dispatch(item, sync_result.conflicting_files, current_rounds)
                return
        # Phase 2 — for pr-fix: bump the round counter and snapshot the
        # comment IDs the monitor is tracking. Mirrors the post-CAS work
        # in ``answer()``/``revise()``/``send_message()``/
        # ``_dispatch_pr_fix_locked`` so every dispatch entry point
        # produces equivalent audit metadata. Without this, a retry of a
        # blocked pr-fix task would emit a ``pr_decision`` row with a
        # stale round number and empty ``triggering_comment_ids``.
        triggering_ids: list[int] = []
        dispatch_feedback: str | None = None
        if is_pr_fix:
            await self._merge_task_metadata(item, {"pr_fix_round_count": current_rounds + 1})
            engine = self._monitor_engine_for(item)
            if engine is not None:
                triggering_ids = list(engine.snapshot_triggering_ids(task_id))
            # Retry carries no operator input — resolve the PR's current
            # feedback so the agent gets the real review instead of an empty
            # "nothing to do" skip. A bare re-dispatch (feedback=None) is how a
            # retry of a PR-feedback task used to immediately re-skip and, with
            # the old skip accounting, re-block (an internal task).
            dispatch_feedback = await self._gather_pending_pr_feedback(row)
        await self._dispatch_step(item, step, feedback=dispatch_feedback, triggering_comment_ids=triggering_ids)

    async def _cancel_in_flight(self, task_id: str) -> InFlightStep | None:
        """Pop and cancel the in-flight agent task for ``task_id``. Idempotent.

        Shared by ``stop()``, ``archive()``, and ``jump_to_step()`` — the one
        place that interrupts a running agent. ``_run_agent`` re-raises
        ``CancelledError`` without queuing a completion, and the drainer's own
        ``_in_flight.pop(..., None)`` is idempotent, so cancelling here can
        never double-drain. Returns the popped ``InFlightStep`` if an entry was
        found and cancelled (so callers like ``jump_to_step`` can read its
        ``item`` / ``step``), or ``None`` if the task wasn't running.

        Cancellation only abandons the asyncio task; the underlying
        ``subprocess.run`` thread (if any) keeps running and is reaped at the
        next orchestrator boundary — same semantics as ``shutdown()``.
        """
        info = self._in_flight.pop(task_id, None)
        if info is None:
            return None
        if info.task and not info.task.done():
            info.task.cancel()
        return info

    async def stop(self, task_id: str) -> None:
        """Stop a running agent — cancel it and park the task at ``blocked``.

        Non-destructive: ``state`` and ``current_step`` are preserved so the
        existing ``retry()`` resumes from exactly where it stopped (same effect
        as crash recovery, but via an atomic CAS rather than ``_set_status``).

        Only valid while the agent is actively working (``status='working'``
        and present in ``_in_flight``); otherwise raises ``StopNotAllowed``.

        Race safety: the CAS is checked (``result.won``) BEFORE any side effect.
        If the agent finished naturally between the guard and the CAS, the CAS
        from ``working`` loses, we don't cancel or write a second message, and
        the natural completion's transition stands (no audit drift).
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise StopNotAllowed(f"Task {task_id} not found")
        if row.status != "working" or task_id not in self._in_flight:
            raise StopNotAllowed(
                f"stop() requires an actively-working agent (status='working' and in-flight), got status={row.status!r}"
            )

        result = await self.db.atomic_transition(
            task_id,
            from_status="working",
            from_state=row.state,
            to_state=row.state,  # preserved — Retry resumes the same step
            to_status="blocked",
            to_current_step=row.current_step or row.state,
            audit_on_win=AuditRow(
                role="system",
                step_name=row.current_step or row.state,
                content="Stopped by operator",
                msg_type="status_change",
            ),
        )
        if not result.won:
            # Agent finished on its own; the drainer already advanced the task.
            # Don't cancel (nothing to interrupt) and don't double-message.
            return

        await self._cancel_in_flight(task_id)

    async def archive(self, task_id: str) -> None:
        """Archive a task — terminal teardown that composes ``stop``.

        Order (per spec): cancel any in-flight agent → untrack from the PR
        monitor → remove the worktree + ``lotsa/{task_id}`` branch → atomically
        transition to the terminal ``archived`` status. The ``tasks`` row and
        the append-only ``messages`` log are retained; only worktree-side
        artifacts are removed (the DB is the durable record per ADR-016/017).

        Idempotent: an already-archived task returns immediately (no second
        teardown, no second audit message). Available from any state.

        Race safety: the final transition re-reads the row and CASes from its
        fresh ``(status, state)`` in a bounded loop. A natural completion that
        the drainer processes between the cancel and the CAS simply shifts the
        ``from_status``; the next iteration CASes from the new status. Only one
        ``"Archived by operator"`` message is ever written (on the winning CAS).
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        row = await self.db.get_task(task_id)
        if row is None:
            raise ArchiveNotAllowed(f"Task {task_id} not found")
        if row.status == "archived":
            return  # idempotent — already torn down

        # 1. Stop the agent (shared cancel machinery).
        await self._cancel_in_flight(task_id)

        # 2. Untrack from the PR monitor if parked in a monitor state. Accept
        #    both the YAML-named monitor state and the legacy ``waiting_for_pr``
        #    synthetic, resolving the engine against the task's OWN process —
        #    mirrors block()/jump_to_step().
        _mon = self._monitor_state_for(row) or "waiting_for_pr"
        if row.state in (_mon, "waiting_for_pr"):
            engine = self._monitor_engine_for(row)
            if engine is not None:
                engine.untrack(task_id)

        # 3. Remove the worktree + branch (idempotent — no error if absent).
        await self._worktree_manager_for_task(row).remove(task_id)

        # 4. Transition to the terminal ``archived`` status, race-safe against
        #    a natural completion the drainer may process concurrently.
        for _ in range(5):
            fresh = await self.db.get_task(task_id)
            if fresh is None or fresh.status == "archived":
                return
            result = await self.db.atomic_transition(
                task_id,
                from_status=fresh.status,
                from_state=fresh.state,
                to_state=fresh.state,  # preserved; ``archived`` is a status, not a flow state
                to_status="archived",
                to_current_step=fresh.current_step,
                audit_on_win=AuditRow(
                    role="system",
                    step_name=fresh.current_step or fresh.state,
                    content="Archived by operator",
                    msg_type="status_change",
                ),
            )
            if result.won:
                return

        # Every attempt lost the CAS — the task is NOT archived. Don't return
        # silently (that would let the route respond HTTP 200 with a
        # non-archived task); surface the non-convergence so the caller sees a
        # 5xx and can retry. In CE's single-writer SQLite context this is
        # effectively unreachable, but the contract must hold.
        raise ArchiveFailed(f"archive() did not converge for task {task_id} after 5 attempts")

    async def jump_to_step(self, task_id: str, step_name: str) -> None:
        """Force-jump a task to an arbitrary flow step.

        Cancels any in-flight agent, transitions to the target step's
        queue_state, emits a stage_transition message, and dispatches
        the target step.

        Phase 2: when ``step_name == "pr-fix"`` this is a sixth dispatch
        entry point (alongside ``_dispatch_pr_fix_locked``/``answer()``/
        ``revise()``/``send_message()``/``retry()``). It applies the same
        round-cap pre-check and post-CAS counter / triggering-ID
        bookkeeping so every dispatch path produces consistent audit
        metadata — closes the asymmetric gap flagged in the PR #58
        review where ``jump_to_step("pr-fix")`` previously bypassed all
        Phase 2 augmentation (no cap check, no counter increment, no
        ``triggering_comment_ids`` snapshot).
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        # Read the row up front so step resolution runs against the task's OWN
        # process (ADR-021). A missing row resolves to the active process via
        # the helpers' ``None`` fallback, preserving the original "valid
        # step_name + missing task → silent return" behaviour below.
        task = await self.db.get_task(task_id)
        root_flow = self._root_flow_for(task)

        # Find the target step by name
        target_step = None
        target_index = None
        for i, step in enumerate(root_flow.steps):
            if step.name == step_name:
                target_step = step
                target_index = i
                break
        if target_step is None and self.process is not None:
            # ADR-014 Layer A — ``step_name`` may identify a sub-flow job
            # (e.g. ``pr-fix`` lives in the ``pr_fix`` sub-flow, not in
            # ``main``'s bindings). Walk the task's process-level catalog so
            # cross-flow jumps work. ``target_index`` stays ``None`` —
            # cross-flow jumps have no comparable index in main's binding
            # order; downstream direction-computation treats that as
            # "forward". Mirrors the catalog fallback at
            # ``_dispatch_pr_fix_locked``.
            target_step = next((s for s in self._process_for(task).jobs if s.name == step_name), None)
        if target_step is None:
            raise ValueError(f"Unknown step: {step_name}")

        # In-process guard — concurrent jump_to_step calls (double-click,
        # parallel HTTP) would otherwise both reach _dispatch_next_step and
        # spawn two agents on the same worktree. The check-then-add is
        # atomic in single-threaded asyncio (no await between them); the
        # second caller bails out cleanly.
        if task_id in self._dispatching_jump:
            return
        self._dispatching_jump.add(task_id)
        try:
            if task is None:
                return
            # Terminal-state guard.  Without this the CAS below would happily
            # rewrite a (status="complete", state="complete") row to
            # (status="working", state=target_queue_state), reopening a
            # finished task — there's no FSM edge stopping it because the
            # CAS doesn't consult the state machine.  Match the silent-return
            # style of block() and the row-not-found check above. ``archived``
            # is terminal too — a jump must never reopen an archived task.
            if task.status in ("complete", "abandoned", "archived"):
                return

            # Phase 2 — when jumping to pr-fix, capture the round count for the
            # post-CAS counter bump. ADR-019 Commitment 5: the
            # ``max_pr_fix_rounds`` cap is NOT enforced on this
            # operator-initiated manual jump; only autonomous dispatch blocks at
            # the cap. The counter still increments. Mirrors ``answer()``/
            # ``revise()``/``send_message()``/``retry()``.
            is_pr_fix = target_step.name == "pr-fix"
            current_rounds = 0
            if is_pr_fix:
                current_rounds = int(task.metadata.get("pr_fix_round_count", 0))

            # Atomic claim — defends against concurrent jumps from a different
            # process. CAS from the row's current (status, state) so exactly
            # one writer wins at the DB level.
            result = await self.db.atomic_transition(
                task_id,
                from_status=task.status,
                from_state=task.state,
                to_state=target_step.queue_state,
                to_status="working",
                to_current_step=target_step.name,
                audit_on_win=None,
            )
            if not result.won:
                return

            from_step_name: str | None
            item: Item
            # Reuse the shared cancel helper (same machinery as stop()/archive())
            # so the cancel logic can't drift; it returns the popped InFlightStep
            # whose ``item`` / ``step`` we still need for the direction calc below.
            info = await self._cancel_in_flight(task_id)
            if info is not None:
                item = info.item
                from_step_name = info.step.name
            else:
                item = Item(id=task.id, state=task.state, title=task.title, body=task.body, metadata=task.metadata)
                # Pass ``item`` so the lookup resolves against the task's
                # active flow (see ``_resolve_flow``). Without it, a task
                # blocked in a sub-flow state (e.g. ``state="pr-fixing"``)
                # falls through to the main flow, returns ``None``, and
                # ``from_step_name`` becomes the raw state string rather
                # than the job name — breaking the direction calculation
                # below and any downstream analytics keyed on the step.
                current = self._find_step_for_state(task.state, item=item)
                if current is None:
                    current = find_step(root_flow, task.state)
                from_step_name = current.name if current else task.state

            # Drop any PR-monitor tracking before transitioning away from
            # the monitor state — otherwise the in-memory entry leaks until
            # restart. Accept both the new YAML-named state (Layer A) and
            # the legacy ``"waiting_for_pr"`` synthetic for cross-rollout
            # safety; see the same pattern in ``block()``. ADR-021: monitor
            # state and engine resolve against the task's own process.
            _mon = self._monitor_state_for(item) or "waiting_for_pr"
            if item.state in (_mon, "waiting_for_pr"):
                engine = self._monitor_engine_for(item)
                if engine is not None:
                    engine.untrack(task_id)

            # Determine direction. ``target_index`` is None when the target
            # step came from the cross-flow fallback (sub-flow job not in
            # main's binding order) — there is no comparable position to
            # diff against ``current_index``, so default to "forward".
            if target_index is None:
                direction = "forward"
            else:
                current_index = None
                if from_step_name:
                    for i, step in enumerate(root_flow.steps):
                        if step.name == from_step_name:
                            current_index = i
                            break
                direction = "backward" if current_index is not None and target_index < current_index else "forward"

            # The CAS already wrote state to target_step.queue_state; mirror it
            # on the in-memory Item so _dispatch_next_step sees the new state.
            item.state = target_step.queue_state

            # Emit stage_transition message
            await self.db.add_message(
                task_id,
                "system",
                "",
                f"Jumped to {target_step.name}",
                "stage_transition",
                metadata={
                    "from_step": from_step_name or "unknown",
                    "to_step": target_step.name,
                    "direction": direction,
                },
            )

            # Sync ``current_flow`` metadata to the target step's owning flow
            # before dispatching. Two failure modes this closes:
            #
            #  (a) Jumping OUT of a sub-flow into the root flow: a task with
            #      ``current_flow="pr_fix"`` (blocked pr-fix agent) jumped to
            #      ``"code"`` would leave the metadata as ``"pr_fix"``;
            #      ``_dispatch_next_step`` → ``_resolve_flow`` → pr_fix flow,
            #      and ``"code"`` is not in pr_fix's bindings, so
            #      ``_find_step_for_state`` returns ``None`` and the task
            #      silently stalls at ``status="working"`` with no agent
            #      running (only recoverable by server restart).
            #
            #  (b) Jumping INTO pr-fix: ``jump_to_step("pr-fix")`` previously
            #      did not write ``current_flow="pr_fix"``, so the subsequent
            #      ``review`` completion evaluated main-flow rule overrides
            #      (``REVIEW_FAIL → code``) instead of the pr_fix-flow
            #      overrides (``REVIEW_FAIL → pr-fix``). The
            #      ``_dispatch_pr_fix_locked`` entry point already writes
            #      ``current_flow="pr_fix"`` (see line ~1700); this brings
            #      jump_to_step's pr-fix path to the same invariant.
            #
            # Conservative rule per the PR-round-7 review recommendation:
            # pr-fix target → ``current_flow="pr_fix"``; any other target →
            # reset to the root flow name (no-op when already root).
            current_flow_name = (item.metadata or {}).get("current_flow") or root_flow.name
            target_flow_name = "pr_fix" if is_pr_fix else root_flow.name
            if target_flow_name != current_flow_name:
                await self._merge_task_metadata(item, {"current_flow": target_flow_name})

            # Phase 2 — for pr-fix: bump the round counter and snapshot the
            # comment IDs the monitor is tracking. Mirrors the post-CAS work
            # in the other five entry points so jump-driven runs produce
            # equivalent audit metadata. Then dispatch via ``_dispatch_step``
            # directly so ``triggering_comment_ids`` flows through to the
            # ``InFlightStep`` — going through ``_dispatch_next_step`` (the
            # non-pr-fix path below) would drop the IDs since that helper
            # doesn't accept the kwarg.
            if is_pr_fix:
                await self._merge_task_metadata(item, {"pr_fix_round_count": current_rounds + 1})
                triggering_ids: list[int] = []
                engine = self._monitor_engine_for(item)
                if engine is not None:
                    triggering_ids = list(engine.snapshot_triggering_ids(task_id))
                await self._dispatch_step(item, target_step, feedback=None, triggering_comment_ids=triggering_ids)
            else:
                # Dispatch the target step
                await self._dispatch_next_step(item)
        finally:
            self._dispatching_jump.discard(task_id)

    async def acknowledge_override(self, task_id: str, guard_name: str, reason: str | None) -> None:
        """Acknowledge a fired guard via its registered override handler (ADR-019).

        Looks the handler up in the override registry and invokes its
        ``acknowledge`` only when the guard currently applies to this task.
        Raises ``AcknowledgeOverrideNotAllowed`` when the guard name is
        unregistered OR when the handler's ``detect`` returns False — both map
        to one exception so registry contents aren't leaked (R3).

        After the override resets the guard, this **resumes the blocked step**
        (ADR-019 revised 2026-06-16): a single "Acknowledge & continue" action
        both clears the guard and re-dispatches, rather than leaving the
        operator to find a separate Retry. The audit trail still records two
        rows — the ``overridden`` pr_decision from the handler and the
        ``Retrying`` row from the resume — so the override and the dispatch
        remain distinct events; only the click count collapses to one.

        The detect→acknowledge pair is serialised per task by the
        ``_acknowledging_override`` guard (mirroring ``_dispatching_pr_fix``).
        Without it, two concurrent calls would both pass ``detect`` and both
        write an ``overridden`` audit row — the second re-reading
        ``pr_fix_round_count`` *after* the first reset it to 0, so it would
        record a misleading ``round=0`` row inconsistent with the cap-fire
        round captured by the first.
        """
        # State-mutating action method (the resume below calls retry() →
        # atomic_transition), so it carries the flow-not-loaded guard every such
        # method has. Without it, a pre-start() call would let the handler commit
        # its counter reset + overridden audit row and only THEN have retry()
        # raise — leaving counters reset, the override button gone, and a 500.
        if not self.flow:
            raise RuntimeError("OrchestratorService not started")

        import lotsa.overrides as overrides

        task = await self.db.get_task(task_id)
        if task is None:
            raise AcknowledgeOverrideNotAllowed(f"Task {task_id} not found")
        try:
            handler = overrides.get_override(guard_name)
        except KeyError:
            raise AcknowledgeOverrideNotAllowed(f"Override {guard_name!r} is not applicable to this task") from None

        # In-process guard — closes the detect→acknowledge TOCTOU. The
        # check-then-add is atomic in single-threaded asyncio (no await between
        # them); a concurrent second caller bails cleanly rather than writing a
        # duplicate audit row. Mirrors ``_dispatching_pr_fix``/``_dispatching_jump``.
        if task_id in self._acknowledging_override:
            return
        self._acknowledging_override.add(task_id)
        try:
            if not await handler.detect(task, self.db):
                raise AcknowledgeOverrideNotAllowed(f"Override {guard_name!r} is not applicable to this task")
            await handler.acknowledge(task, reason, self.db)
        finally:
            self._acknowledging_override.discard(task_id)

        # Resume the blocked step in the same action (ADR-019 revised). The
        # override reset the guard counters; retry() re-reads the freshly-reset
        # task, passes the (now-cleared) cap pre-check, and for pr-fix re-fetches
        # PR feedback (#145). Guarded on a still-blocked, non-rebasing row:
        # ``rebasing`` recovery is revise→pr-fix (retry rejects it), and a guard
        # that fired without blocking has nothing to resume. Done outside the
        # ``_acknowledging_override`` guard so retry()'s own CAS owns the
        # dispatch claim.
        fresh = await self.db.get_task(task_id)
        if fresh is not None and fresh.status == "blocked" and fresh.state != "rebasing":
            await self.retry(task_id)

    # ── PR-phase helpers (used by PrMonitor) ────────────────────────────

    async def list_waiting_pr_tasks(self) -> list[dict]:
        """List PR-bearing tasks the PrMonitor must watch (ADR-030).

        Returns every task that has opened a PR (``metadata.pr_number`` set)
        and is not yet terminal (status not in
        ``complete``/``abandoned``/``archived``) — regardless of where it is
        parked. This is the discovery half of the ADR-030 invariant *open PR ⇒
        watched until terminal*: a task blocked / needs_input / crash-recovered
        with an open PR must still be polled, so an operator's manual merge
        completes it instead of stranding it.

        The non-terminal filter is pushed down to SQL (``status_not_in``) so
        the per-poll scan stays O(active tasks), not O(all tasks) — a
        long-running instance accumulates thousands of ``complete`` rows and
        millions of audit messages, and the query joins the ``messages``
        ``MAX(created_at)`` subquery, so an unbounded scan would be costly. The
        ``pr_number`` predicate has no SQL column to index, so it stays in
        Python over the already-narrow active set (open PRs per instance is
        small). Each dict carries:

        - ``status`` — the observed status, which ``PrMonitor._poll_one`` uses
          to branch (terminal acts from any status; feedback stays gated to
          ``waiting_for_pr``);
        - ``state`` — retained for engine introspection;
        - ``metadata`` — carries ``pr_number`` + ``github_owner``/``github_repo``.
        """
        rows = await self.db.list_tasks(status_not_in=("complete", "abandoned", "archived"))
        return [
            {"id": r.id, "state": r.state, "status": r.status, "metadata": r.metadata}
            for r in rows
            if r.metadata.get("pr_number") is not None
        ]

    async def transition_task(self, task_id: str, target_state: str) -> None:
        """Transition a task to a new state (used by PrMonitor).

        Mirrors the transition to the status enum so the React UI sees the
        terminal state immediately. ``target_state`` is one of {complete,
        abandoned, blocked}.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        task = await self.db.get_task(task_id)
        if task is None:
            return
        # ``archived`` is terminal — never transition out of it. archive()
        # untracks from the monitor, but a stale in-flight poll could still
        # call back here; the CAS below validates only the (state, target_state)
        # SM edge, and an archived row preserves its prior ``state`` (which may
        # still have an outgoing edge), so without this guard the CAS would
        # un-archive the task.
        if task.status == "archived":
            return
        item = Item(id=task.id, state=task.state, title=task.title, body=task.body, metadata=task.metadata)
        # Idempotency: a concurrent caller (or a retry from PrMonitor) may
        # have already advanced the task. Return silently rather than raising
        # InvalidTransition on the no-op self-edge.
        if task.state == target_state:
            return
        # Validate the FSM transition exists before claiming.
        #
        # ADR-021: validate against the task's OWN process state machine
        # (``_resolve_flow`` — sub-flow aware). ``_register_cross_flow_edges``
        # stitches each process's monitor-driven sub-flow entry/exit edges
        # (e.g. ``(wait_for_pr_signal, pr-fix)``, ``(<terminal>,
        # wait_for_pr_signal)``) into that process's flow SMs at build time, so
        # the resolved flow contains every edge an engine could plausibly
        # target for this task. A monitor→orchestrator callback thus validates
        # against the process that owns the polled task, not a global one.
        #
        # ADR-030 — terminal PR outcomes (``complete`` / ``abandoned``) are NOT
        # gated on the flow SM. A merged/closed PR is a fact about the world,
        # not a flow transition: it must complete/abandon a task parked in ANY
        # non-terminal state — including ``state="blocked"`` (a SM sink with no
        # outgoing edge, the PR #116 incident shape) and ``state="pr-fixing"``
        # (a sub-flow active state with no terminal edge). The edge check below
        # therefore applies only to ``blocked`` (the missing-token park path);
        # terminal targets skip it and rely on the status-agnostic CAS, which
        # still preserves audit integrity (Constitution §3.1 / ADR-020).
        #
        # This also subsumes the former legacy-state special case: a task
        # stranded at the synthetic ``state="waiting_for_pr"`` now completes on
        # merge instead of log-and-returning (that earlier fallback was itself
        # the bug — a real engine merge-detection silently no-op'd).
        if target_state not in ("complete", "abandoned") and (
            (task.state, target_state) not in self._resolve_flow(item).state_machine.transitions
        ):
            # Non-terminal (``blocked``) target with no registered edge: log +
            # return mirrors the silent-no-op pattern at ``block()`` and lets
            # the operator clear the row manually instead of spinning.
            logger.warning(
                "transition_task: no (%r, %r) transition for task %s; "
                "row left at status=%r state=%r (legacy task or flow misconfig?)",
                task.state,
                target_state,
                task_id,
                task.status,
                task.state,
            )
            return
        # Atomic CAS — the previous two-write sequence (source.save then
        # _set_status) had a crash window: a server crash between the writes
        # left state=complete/abandoned/blocked while status stayed
        # waiting_for_pr, and start()'s recovery only sweeps status='working',
        # so the row would stick and PrMonitor would keep polling.
        target_status: TaskStatusLiteral
        target_current_step: str | None
        if target_state == "complete":
            target_status = "complete"
            target_current_step = None
        elif target_state == "abandoned":
            target_status = "abandoned"
            target_current_step = None
        elif target_state == "blocked":
            target_status = "blocked"
            # Fallback to ``task.state`` (not the hardcoded legacy ``"push"``
            # sentinel) when ``current_step`` is unset: for ADR-014 Layer A
            # tasks every dispatch path sets ``current_step`` (see e.g. the
            # SKIPPED CAS at line 2783 which persists ``current_step=monitor_state``),
            # so this fallback is normally unreachable. If a bug ever leaves
            # ``current_step=None`` on a row reaching this branch, surfacing
            # ``state`` (an actual job/monitor name) is more meaningful in the
            # UI than the legacy ``"push"`` sentinel — which conflates the old
            # synthetic state with new job names. Matches the recovery-sweep
            # fallback in ``start()`` (``row.current_step or row.state``).
            target_current_step = task.current_step or task.state
        else:
            raise RuntimeError(f"Unsupported transition_task target {target_state!r}")
        pr_number = task.metadata.get("pr_number", "?")
        if target_state == "complete":
            reason = "complete"
        elif target_state == "abandoned":
            reason = "closed"
        else:
            reason = target_state
        # ADR-030: when a terminal PR outcome lands on a task parked OUTSIDE the
        # normal ``waiting_for_pr`` flow, name the parked status in the audit
        # row so the operator can see exactly what happened (e.g. a hand-merge
        # of a blocked task). The plain wording is kept for the common
        # ``waiting_for_pr`` path so existing audit expectations don't churn.
        if target_state in ("complete", "abandoned") and task.status != "waiting_for_pr":
            verb = "merged" if target_state == "complete" else "closed"
            action = "completing" if target_state == "complete" else "abandoning"
            audit_content = f"PR #{pr_number} {verb} while task was {task.status} — {action}"
        else:
            audit_content = f"PR #{pr_number} {reason}"
        result = await self.db.atomic_transition(
            task_id,
            from_status=task.status,
            from_state=task.state,
            to_state=target_state,
            to_status=target_status,
            to_current_step=target_current_step,
            audit_on_win=AuditRow(
                role="system",
                # ADR-014: this method also serves ``pr_monitor`` engine
                # transitions whose ``current_step`` is the monitor job
                # (``wait_for_pr_signal``), so the literal ``"push"`` sentinel
                # would mislabel the audit row. Use the actual step name; fall
                # back to the engine name.
                step_name=task.current_step or "pr_monitor",
                content=audit_content,
                msg_type="status_change",
            ),
        )
        if not result.won:
            return  # already transitioned by another caller
        if target_state in ("complete", "abandoned"):
            item = Item(id=task.id, state=target_state, title=task.title, body=task.body, metadata=task.metadata)
            await self._cleanup_worktree_if_done(item)

    async def dispatch_pr_fix(self, task_id: str, feedback: str) -> bool:
        """Dispatch a pr-fix step for a task (used by PrMonitor).

        Acquires the per-task pr-fix dispatch guard, then delegates to
        ``_dispatch_pr_fix_locked``. Returns ``False`` when the guard is
        already held by another path (concurrent monitor poll, in-progress
        agent, or revise()) or when the locked body declines (CAS lost,
        round-cap fired, task no longer in ``waiting_for_pr``). Returns
        ``True`` only when a pr-fix dispatch actually proceeded.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        if task_id in self._in_flight or task_id in self._dispatching_pr_fix:
            return False
        self._dispatching_pr_fix.add(task_id)
        try:
            return await self._dispatch_pr_fix_locked(task_id, feedback)
        finally:
            self._dispatching_pr_fix.discard(task_id)

    async def dispatch_sub_flow(
        self,
        task_id: str,
        flow_name: str,
        *,
        feedback: str | None = None,
        target_job: str | None = None,
    ) -> bool:
        """Forward-compatible sub-flow entry point (ADR-014 Layer B prep).

        Engines (see ``lotsa.engines.pr_monitor.PrMonitorEngine``) call this
        instead of the legacy ``dispatch_pr_fix`` so the orchestrator can
        eventually dispatch into any named sub-flow. Today only ``pr_fix``
        is wired with a dispatch body; other names that ARE present in the
        loaded process but lack wiring also warn-and-return rather than
        silently no-op. Names that don't appear in the process at all are
        rejected the same way — catches an engine renaming its target flow
        without the orchestrator's matching update.

        Returns ``True`` on a dispatched sub-flow and ``False`` if the
        underlying dispatch declined (CAS lost, cap fired, or task no
        longer in a dispatchable state).
        """
        # ADR-021: validate the requested sub-flow against the task's OWN
        # process's flows, not a global active process.
        row = await self.db.get_task(task_id)
        known_flows = set(self._process_for(row).flows.keys()) if self.process is not None else set()
        if flow_name not in known_flows:
            logger.warning(
                "dispatch_sub_flow: unknown flow %r for task %s (known flows: %s)",
                flow_name,
                task_id,
                sorted(known_flows),
            )
            return False
        # Layer A: only ``pr_fix`` has dispatch wiring. The signature accepts
        # any ``flow_name`` for forward compatibility with Layer B (which will
        # dispatch into any named sub-flow); until that lands, every other
        # name is rejected with a warning rather than silently no-op'd so a
        # mis-typed engine target surfaces immediately.
        if flow_name != "pr_fix":
            logger.warning(
                "dispatch_sub_flow: flow %r is declared in the process but has no "
                "dispatch wiring in Layer A (only 'pr_fix' is wired); task %s stays put",
                flow_name,
                task_id,
            )
            return False
        # ``target_job`` is accepted for forward compatibility; pr_fix has a
        # single entry point (pr-fix) so the parameter is currently unused.
        # Log a debug line when a caller passes it so the silent-discard isn't
        # invisible — a Layer B engine that starts routing to a named target
        # job will then see the parameter being ignored and either advocate
        # for Layer B wiring or stop passing it.
        if target_job is not None:
            logger.debug(
                "dispatch_sub_flow: target_job=%r ignored in Layer A "
                "(pr_fix has a single entry point); Layer B will route to the named job",
                target_job,
            )
        if feedback is None:
            feedback = ""
        return await self.dispatch_pr_fix(task_id, feedback)

    async def _sync_branch_to_main(self, task_id: str) -> SyncResult:
        """Sync the task's worktree branch to ``origin/<default_branch>`` (ADR-015).

        Deterministic, orchestrator-owned (ADR-013): fetch the canonical
        upstream branch (the task's project ``WorktreeManager.default_branch``,
        ``main`` by default), measure divergence, and — when behind — auto-merge and push
        the merged ref to the task's PR branch. The pr-fix agent therefore
        always works on a branch that is current or already past the merge it
        would otherwise have flagged.

        Returns a :class:`SyncResult`. Fetch/push failures propagate as
        exceptions (the caller routes them to ``blocked`` via the generic
        dispatch error path — no bespoke retry logic). On a merge conflict the
        markers are left in the worktree for the ``resolve_conflicts`` agent
        step (ADR-015 Phase 2) to edit; the conflicting file list is returned
        in ``SyncResult.conflicting_files``.

        All git calls go through ``asyncio.create_subprocess_exec`` with
        positional tokens (Constitution §1.1 / §2.1; ``lotsa/push_step.py`` is
        the canonical reference).
        """
        from lotsa.push_step import execute_push

        row = await self.db.get_task(task_id)
        wtm = self._worktree_manager_for_task(row) if row is not None else None
        wt = wtm.get_path(task_id) if wtm is not None else None
        if wt is None:
            # No per-task worktree to sync. Unlike ``_execute_push`` we do NOT
            # fall back to ``config.work_dir`` — that shared checkout is the
            # operator's main repo, and auto-merging origin/main into it would
            # be destructive. Skipping is production-safe: it leaves the
            # dispatch exactly as it was before ADR-015 (no sync) rather than
            # blocking a task that legitimately ran in the fallback work_dir.
            logger.warning("No worktree for task %s; skipping branch-to-main sync", task_id)
            return SyncResult(status="already_current")
        work_dir = Path(wt)
        branch = wtm.default_branch

        # Authenticate the fetch with GITHUB_TOKEN — a private/auth'd remote
        # otherwise prompts for a username and dies "could not read Username"
        # (no TTY). Reuse the SDK credential strategy (GIT_CONFIG credential
        # helper; no temp file). Local/file remotes ignore it, so tests are
        # unaffected. Token is scrubbed from any surfaced stderr (§1.2).
        from rigg.git import TokenCredentialStrategy

        git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        _gh_token = os.environ.get("GITHUB_TOKEN")
        if _gh_token:
            git_env.update(TokenCredentialStrategy(_gh_token).env())

        async def _git(*args: str) -> tuple[int, str, str]:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env,
            )
            stdout, stderr = await proc.communicate()
            # Scrub git stderr at the source: it can carry a tokenized remote URL,
            # and these strings flow into RuntimeError messages that are persisted
            # as audit rows and logged (§1.2; audit finding #3).
            return proc.returncode or 0, stdout.decode(), scrub_secrets(stderr.decode())

        # 1. Unconditional fetch — without it the local ``origin/<branch>`` ref
        #    can be stale and the divergence count below silently reads zero.
        rc, _out, err = await _git("fetch", "origin", branch)
        if rc != 0:
            raise RuntimeError(f"git fetch origin {branch} failed: {err.strip()}")

        # 2. Divergence: how many commits is origin/<branch> ahead of HEAD?
        rc, out, err = await _git("rev-list", "--count", f"HEAD..origin/{branch}")
        if rc != 0:
            raise RuntimeError(f"git rev-list HEAD..origin/{branch} failed: {err.strip()}")
        behind = int(out.strip() or "0")

        # 3. Already current — nothing to merge or push.
        if behind == 0:
            return SyncResult(status="already_current")

        # 4. Behind — auto-merge origin/<branch>.
        rc, _out, merge_err = await _git("merge", f"origin/{branch}", "--no-edit")
        if rc != 0:
            # 6. Conflict: capture the unmerged paths and leave the markers in
            #    place for the ``resolve_conflicts`` agent step (ADR-015 Phase 2).
            #    The agent edits the conflicted files directly — no new git
            #    authority; the commit posthook completes the merge commit.
            _rc, files_out, _ferr = await _git("diff", "--name-only", "--diff-filter=U")
            conflicting = tuple(line.strip() for line in files_out.splitlines() if line.strip())
            if not conflicting:
                # A non-zero merge with NO unmerged paths is not a content
                # conflict (dirty/locked worktree, index lock, "local changes
                # would be overwritten"). Routing it to resolve_conflicts with an
                # empty file set blocks the task on a phantom reason (finding #10).
                # Abort the half-done merge and surface the real cause.
                await _git("merge", "--abort")
                raise RuntimeError(f"git merge origin/{branch} failed (no conflicts): {merge_err.strip()}")
            return SyncResult(status="conflicts", conflicting_files=conflicting)

        # 5. Clean merge — push the merged ref to the task's PR branch. The PR
        #    already exists (this runs inside the pr-fix funnel), so reuse the
        #    deterministic push helper with the existing ``pr_number``; with it
        #    set, ``execute_push`` pushes by SHA and locates (does not create)
        #    the PR. CI re-runs and the bot re-reviews on the new SHA —
        #    expected and acceptable.
        task = await self.db.get_task(task_id)
        pr_number = task.metadata.get("pr_number") if task is not None else None
        # Base off the configured default branch (ADR-018 contract item 5).
        # Per-task base branches remain out of scope per the ADR
        # (``_pr_monitor_config_for`` is the future hook).
        await execute_push(
            work_dir=work_dir,
            task_id=task_id,
            pr_number=pr_number,
            base_branch=branch,
        )
        return SyncResult(status="clean")

    async def _handle_conflict_dispatch(
        self,
        item: Item,
        conflicting_files: tuple[str, ...],
        current_rounds: int,
    ) -> bool:
        """Dispatch ``resolve_conflicts`` (or block) after a merge conflict.

        Shared by ``_dispatch_pr_fix_locked`` and ``retry()`` — both sync
        entry points call this on a ``SyncResult(status='conflicts')`` so
        the conflict path is handled identically regardless of how the pr-fix
        was triggered (monitor-driven or operator-initiated retry).

        If the process declares a ``resolve_conflicts`` job: bumps the round
        counter, sets ``current_flow``, and dispatches the step. Returns
        ``True``.

        If the process has no ``resolve_conflicts`` job (backward compat with
        custom processes): delegates to ``_block_after_sync``. Returns
        ``False``.
        """
        resolve_step = next((s for s in self._process_for(item).jobs if s.name == "resolve_conflicts"), None)
        if resolve_step is None:
            files = ", ".join(conflicting_files) or "(unknown)"
            return await self._block_after_sync(
                item,
                from_status="working",
                from_state=item.state,
                message=(
                    f"Auto-merge of origin/main hit conflicts in: {files}. "
                    "Process has no resolve_conflicts step — resolve the conflict "
                    "and click Retry."
                ),
            )
        dispatched_at = datetime.now(UTC).isoformat()
        await self._merge_task_metadata(
            item,
            {
                "pr_fix_dispatched_at": dispatched_at,
                "pr_fix_round_count": current_rounds + 1,
                "current_flow": "pr_fix",
            },
        )
        await self.db.add_message(
            item.id,
            "system",
            "",
            "Merge conflicts detected — dispatching resolve_conflicts",
            "stage_transition",
            metadata={
                "from_step": self._monitor_state_for(item) or "waiting_for_pr",
                "to_step": "resolve_conflicts",
            },
        )
        triggering_ids: list[int] = []
        engine = self._monitor_engine_for(item)
        if engine is not None:
            triggering_ids = list(engine.snapshot_triggering_ids(item.id))
        files_list = "\n".join(f"- {f}" for f in conflicting_files)
        conflict_feedback = (
            "The orchestrator merged origin/main and hit conflicts. "
            f"Resolve the conflict markers in these files only:\n{files_list}"
        )
        await self._dispatch_step(
            item,
            resolve_step,
            feedback=conflict_feedback,
            triggering_comment_ids=triggering_ids,
        )
        return True

    async def _block_after_sync(
        self,
        item: Item,
        *,
        from_status: TaskStatusLiteral,
        from_state: str,
        message: str,
        to_current_step: str = "pr-fix",
        step_name: str = "pr-fix",
    ) -> bool:
        """Transition a task to ``blocked`` after a failed/conflicted sync.

        Mirrors ``_execute_push``'s failure handler: a single atomic CAS to
        ``blocked`` carrying the reason as an ``error`` audit row, then untrack
        the PR monitor (the task was being polled in the monitor state) and
        emit a dispatch event so ``retry()`` can locate and re-run the failed
        step (which re-runs the sync). Always returns ``False`` so the caller
        short-circuits its dispatch.

        ``to_current_step`` / ``step_name`` default to ``"pr-fix"`` (the pr-fix
        sync funnel). The ADR-018 push-retry path passes ``"push"`` so a blocked
        task re-enters the synced push branch (``retry()``'s ``push_retry``
        predicate keys on ``current_step``), not the generic pr-fix path.
        """
        result = await self.db.atomic_transition(
            item.id,
            from_status=from_status,
            from_state=from_state,
            to_state="blocked",
            to_status="blocked",
            to_current_step=to_current_step,
            audit_on_win=AuditRow(
                role="system",
                step_name=step_name,
                content=message,
                msg_type="error",
            ),
        )
        if not result.won:
            # Another path redirected the task between the sync and now — don't
            # pollute the audit log with a stale block message.
            return False
        engine = self._monitor_engine_for(item)
        if engine is not None:
            engine.untrack(item.id)
        await self.source.append_event(item.id, {"type": "dispatch", "job_type": step_name, "success": False})
        return False

    async def _dispatch_pr_fix_locked(
        self,
        task_id: str,
        feedback: str,
        *,
        user_feedback: str | None = None,
        operator_initiated: bool = False,
    ) -> bool:
        """Body of the pr-fix dispatch.

        Caller is responsible for ensuring the per-task guard
        (``_dispatching_pr_fix``) is held — this lets ``revise()`` hold the
        guard across its slow GitHub fetch without re-entering through
        ``dispatch_pr_fix`` (which would short-circuit on its own guard).

        Atomically CAS-claims the transition out of ``waiting_for_pr`` so a
        concurrent ``transition_task`` (PrMonitor seeing the PR merge / close
        during this dispatch) cannot leave us with audit messages written for
        a dispatch that didn't happen.

        ``user_feedback`` is the user's revise text. It is recorded inside
        the locked path AFTER the CAS wins so a CAS-loss leaves no orphan
        feedback row. Pass ``None`` when the dispatch is monitor-driven
        (no user feedback to attribute).

        ``operator_initiated`` gates the ``max_pr_fix_rounds`` cap *enforcement*
        (ADR-019 Commitment 5). ``revise()``'s ``waiting_for_pr`` / ``rebasing``
        routes funnel through here and pass ``operator_initiated=True`` so the
        cap is bypassed (operator dialogue is supervised, not autonomous); the
        monitor path (``dispatch_pr_fix`` / ``dispatch_sub_flow``) keeps the
        default ``False`` and still blocks at the cap. The round counter
        increments regardless of source — only cap enforcement is scoped to
        autonomous dispatch.

        Returns ``True`` if the CAS won and dispatch proceeded, ``False`` if
        the task was no longer in ``waiting_for_pr`` (or the autonomous cap
        fired).
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        task = await self.db.get_task(task_id)
        if task is None or task.status != "waiting_for_pr":
            return False
        # ADR-014 Layer A / ADR-021 — pr-fix lives in the ``pr_fix`` sub-flow's
        # bindings, never in the root flow, so scan the whole job catalog of
        # the task's OWN process directly. ``Process.jobs`` is a superset of
        # every flow's bindings.
        pr_fix_step = next((s for s in self._process_for(task).jobs if s.name == "pr-fix"), None)
        if pr_fix_step is None:
            logger.warning("dispatch_pr_fix: no pr-fix step in flow for task %s", task_id)
            return False

        # Phase 2 — round-cap pre-check, ENFORCED ONLY for autonomous dispatch
        # (ADR-019 Commitment 5). When ``operator_initiated`` is True
        # (revise() on waiting_for_pr / rebasing), the cap is skipped — the
        # counter still increments below, but enforcement is reserved for the
        # monitor-driven path. The ``and not operator_initiated`` short-circuit
        # means ``_pr_fix_round_cap_blocked`` (which writes the cap-fire audit
        # row) never runs on an operator path.
        current_rounds = int(task.metadata.get("pr_fix_round_count", 0))
        if not operator_initiated and await self._pr_fix_round_cap_blocked(
            task_id,
            task_state=task.state,
            current_rounds=current_rounds,
            from_status="waiting_for_pr",
        ):
            # Persist the operator's revise text even when the cap blocks
            # the dispatch — mirrors the ``answer()``/``revise()``/
            # ``send_message()`` cap-fire paths so the ``pr_decision(blocked)``
            # row is accompanied by a record of what the operator typed. The
            # normal-path counterpart is the ``add_message("feedback")`` write
            # at the end of this method (after the CAS wins). Pass ``None``
            # when the dispatch is monitor-driven (PrMonitor has no operator
            # text to attribute) — the guard on ``user_feedback is not None``
            # prevents the monitor path from synthesising a spurious row.
            if user_feedback is not None:
                await self.db.add_message(task_id, "user", "", user_feedback, "feedback")
            return False

        # Atomic claim — closes the orphan-feedback window. Without this CAS,
        # transition_task can win between the get_task above and the
        # add_message below, leaving the user's revise text recorded but no
        # pr-fix dispatched.
        result = await self.db.atomic_transition(
            task_id,
            from_status="waiting_for_pr",
            from_state=task.state,
            to_state=task.state,
            to_status="working",
            to_current_step="pr-fix",
            audit_on_win=None,
        )
        if not result.won:
            return False
        item = Item(id=task.id, state=task.state, title=task.title, body=task.body, metadata=task.metadata)
        # Persist the operator's revise text now — after the CAS win (so a
        # CAS-loss still leaves no orphan row, the deferral's original reason),
        # but BEFORE the ADR-015 sync below. The sync can block the task on a
        # merge conflict / fetch-push error and short-circuit the dispatch; if
        # the feedback write stayed after that point the operator's instruction
        # would be silently dropped from the audit trail and the agent would
        # never see it on the eventual Retry. Mirrors the cap-fire path above,
        # which persists the feedback before its own pre-dispatch return.
        if user_feedback is not None:
            await self.db.add_message(task_id, "user", "", user_feedback, "feedback")
        # ADR-015 Phase 2 — sync the branch to origin/main before dispatching.
        # Runs only after the CAS win above (so it never fires on a CAS-loss).
        # Fetch/push errors block the task (generic retry path). On conflicts:
        # dispatch resolve_conflicts if the process declares it; otherwise fall
        # back to a block for backward compatibility with custom processes.
        try:
            sync_result = await self._sync_branch_to_main(item.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Branch sync to main failed for task %s", task_id)
            return await self._block_after_sync(
                item,
                from_status="working",
                from_state=task.state,
                message=f"Branch sync to main failed: {type(exc).__name__}: {exc}",
            )

        if sync_result.status == "conflicts":
            return await self._handle_conflict_dispatch(item, sync_result.conflicting_files, current_rounds)

        # Clean merge or already-current: proceed to pr-fix dispatch.
        # Record the round's dispatch cutoff so PR_FIX_SKIPPED: can advance
        # pr_comments_since to this cursor without re-deriving it. Both the
        # monitor-driven and revise-driven entry paths land here, so this
        # is the single source of truth for "the agent saw feedback up to
        # this point in time". Using _merge_task_metadata preserves any
        # concurrent writes (e.g. PrMonitor advancing pr_comments_since on
        # an in-flight write).
        dispatched_at = datetime.now(UTC).isoformat()
        # Set ``current_flow`` here — the task has just been claimed into
        # ``pr-fixing`` and every subsequent step lookup must resolve against
        # the ``pr_fix`` flow's bindings (e.g. so ``review``'s per-flow rule
        # override ``REVIEW_FAIL → pr-fix`` is the one the drainer evaluates,
        # not the root flow's ``REVIEW_FAIL → code``). Reset is owned by the
        # sub-flow exit paths: the SKIPPED drainer branch (``pr-fix →
        # wait_for_pr_signal``) and ``_execute_action_step``'s success path
        # when the next step is a monitor.
        await self._merge_task_metadata(
            item,
            {
                "pr_fix_dispatched_at": dispatched_at,
                "pr_fix_round_count": current_rounds + 1,
                "current_flow": "pr_fix",
            },
        )
        # NOTE: the operator's ``user_feedback`` row is persisted earlier (right
        # after the CAS win, before the ADR-015 sync) so a sync-block can't drop
        # it — see the comment there. Do not re-add the write here.
        await self.db.add_message(
            task_id,
            "system",
            "",
            "PR feedback received — dispatching pr-fix",
            "stage_transition",
            # Use the YAML-defined monitor state name when known. Falls back to
            # the legacy ``"waiting_for_pr"`` sentinel for flows without a
            # monitor job (still used by some tests).
            metadata={
                "from_step": self._monitor_state_for(item) or "waiting_for_pr",
                "to_step": "pr-fix",
            },
        )
        # Snapshot the comment-IDs the monitor is tracking for this PR so
        # the eventual pr_decision row can cross-reference the comments
        # the agent responded to. The monitor stores the per-comment
        # fingerprint as ``last_updated_at_by_comment_id``; its keys are
        # the IDs we've delivered feedback for so far. ADR-021: resolve the
        # engine for the task's own process.
        triggering_ids: list[int] = []
        engine = self._monitor_engine_for(item)
        if engine is not None:
            triggering_ids = list(engine.snapshot_triggering_ids(task_id))
        # Pass feedback via the feedback kwarg — injected as "## Revision Feedback"
        # by _run_agent. Don't overwrite task.body (preserves original task description).
        await self._dispatch_step(item, pr_fix_step, feedback=feedback, triggering_comment_ids=triggering_ids)
        return True

    async def _execute_push(self, item: Item) -> None:
        """Execute the deterministic push step (no agent)."""
        from lotsa.push_step import PushError, build_pr_text, execute_push

        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        # Mark the task as actively pushing.  current_step="push" is a
        # sentinel — there is no "push" job in flow.jobs — but it lets the
        # UI show "push agent is working…" and lets retry() route back here.
        # CAS so a concurrent block() that landed between dispatch and now
        # isn't overwritten by a plain UPDATE.  Lost CAS → another path
        # already redirected this task; bail before the push subprocess runs.
        result = await self.db.atomic_transition(
            item.id,
            **PUSH_START.kwargs(),
            audit_on_win=None,
        )
        if not result.won:
            return

        work_dir = Path(self._worktree_manager_for_task(item).get_path(item.id) or self._fallback_work_dir(item))
        pr_number = item.metadata.get("pr_number")
        # ADR-021: base branch comes from the task's own process's pr_monitor
        # config (None = resolve via GitHub API, handled inside execute_push).
        pr_cfg = self._pr_monitor_config_for(item)
        base_branch = pr_cfg.base_branch if pr_cfg else None

        # Generate the PR title/body only at creation (pr_number is None);
        # a re-push keeps the existing PR. Resolution (parse pr_description
        # artifact → deterministic fallback → Lotsa trailer) lives in
        # build_pr_text so this path stays a thin wrapper over execute_push.
        title: str | None = None
        body: str | None = None
        if pr_number is None:
            pr_description = await self.get_named_artifact(item.id, "pr_description") or ""
            spec = await self.get_named_artifact(item.id, "spec") or ""
            title, body = await build_pr_text(
                work_dir=work_dir,
                task_id=item.id,
                base_branch=base_branch,
                # Match the action dispatcher's TaskContext.flow_name (root
                # flow): the PR trailer's flow name must be identical whether
                # the push runs via the push_pr tool or this legacy path.
                flow_name=self._root_flow_for(item).name,
                pr_description=pr_description,
                spec=spec,
            )

        try:
            new_pr_number, pr_url, push_owner, push_repo = await execute_push(
                work_dir=work_dir,
                task_id=item.id,
                pr_number=pr_number,
                base_branch=base_branch,
                title=title,
                body=body,
            )
        except PushError as exc:
            error_msg = str(exc)
            target_state = "rebasing" if error_msg.startswith("NON_FAST_FORWARD:") else "blocked"
            # Atomic CAS — the previous source.save → add_message → _set_status
            # sequence had a brief window where DB held (state=target_state,
            # status='working'), which UI polls could render as "still pushing".
            # Both rebasing and generic failure surface as status='blocked' so
            # the UI shows Retry; revise() detects the 'rebasing' state and
            # routes into a pr-fix cycle.
            result = await self.db.atomic_transition(
                item.id,
                from_status="working",
                from_state="pushing",
                to_state=target_state,
                to_status="blocked",
                to_current_step="push",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="push",
                    content=error_msg,
                    msg_type="error",
                ),
            )
            if not result.won:
                # Another path (e.g. jump_to_step) already redirected this task —
                # don't pollute the audit log with a misleading push-error message.
                return
            item.state = target_state
            # Emit a dispatch event so retry() can locate the push step.
            await self.source.append_event(item.id, {"type": "dispatch", "job_type": "push", "success": False})
            return
        except Exception as exc:
            logger.exception("Unexpected error in push step for task %s", item.id)
            push_err_msg = f"Push failed: {type(exc).__name__}: {exc}"
            result = await self.db.atomic_transition(
                item.id,
                from_status="working",
                from_state="pushing",
                to_state="blocked",
                to_status="blocked",
                to_current_step="push",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="push",
                    content=push_err_msg,
                    msg_type="error",
                ),
            )
            if not result.won:
                return
            item.state = "blocked"
            await self.source.append_event(item.id, {"type": "dispatch", "job_type": "push", "success": False})
            return

        # Persist metadata immediately after successful push+PR so it's
        # never lost if a subsequent step (state transition, message) fails.
        # Re-read fresh metadata to avoid clobbering concurrent writes
        # (e.g. PrMonitor stats updates on a previous waiting_for_pr cycle).
        await self._merge_task_metadata(
            item,
            {
                "pr_number": new_pr_number,
                "pr_url": pr_url,
                "github_owner": push_owner,
                "github_repo": push_repo,
                # Record the push instant so the PR monitor's first poll uses
                # this (rather than first-poll time) as its `comments_since`
                # cutoff — ensures we don't drop comments posted between
                # push and first poll.
                "pr_pushed_at": datetime.now(UTC).isoformat(),
            },
        )

        # Atomic CAS for state + status to close the same brief-mismatch
        # window the failure paths above just closed.
        result = await self.db.atomic_transition(
            item.id,
            **PUSH_SUCCESS.kwargs(),
            audit_on_win=AuditRow(
                role="system",
                step_name="push",
                content=f"Pushed to PR #{new_pr_number}: {pr_url}",
                msg_type="status_change",
            ),
        )
        if not result.won:
            # Task was redirected by another path (e.g. jump_to_step) while the
            # push was in flight — don't claim success on a stale dispatch.
            return
        item.state = "waiting_for_pr"
        # Emit a dispatch event so retry() can locate the push step on a
        # later failure (e.g. server restart from waiting_for_pr → blocked).
        await self.source.append_event(item.id, {"type": "dispatch", "job_type": "push", "success": True})

    # ── Dispatch ───────────────────────────────────────────────────────

    async def _dispatch_next_step(self, item: Item, feedback: str | None = None) -> None:
        """Find the next applicable step and dispatch it."""
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        # PR-phase: "pushing" is a deterministic step, not an agent dispatch.
        # Guard against concurrent dispatches for the same task — a stray
        # second invocation (retry() racing with verify-approve, etc.) would
        # otherwise spawn a parallel git-push subprocess on the same worktree.
        if item.state == "pushing":
            if item.id in self._dispatching_push or item.id in self._push_tasks:
                return
            self._dispatching_push.add(item.id)
            task = asyncio.create_task(self._execute_push(item))
            self._push_tasks[item.id] = task

            def _cleanup(_: asyncio.Task, task_id: str = item.id) -> None:
                self._push_tasks.pop(task_id, None)
                self._dispatching_push.discard(task_id)

            task.add_done_callback(_cleanup)
            return

        # Resolve once — the item's current_flow drives every step lookup
        # below so a task mid-sub-flow dispatches the sub-flow's bindings
        # (with the right per-flow rule overrides), not the root flow's.
        active_flow = self._resolve_flow(item)

        # 1. Queue state — dispatch matching step
        step = self._find_step_for_state(item.state, item=item)
        if step is not None:
            await self._dispatch_step(item, step, feedback=feedback)
            return

        # 2. Active state — re-dispatch same step
        step = find_step(active_flow, item.state)
        if step is not None:
            await self._dispatch_step(item, step, feedback=feedback)
            return

        # 3. Gate state — an evaluate=true step was just approved; advance to the
        #    next job's queue_state automatically. The human already approved by
        #    clicking Accept — the gate_state is an intermediate artifact, not a
        #    second manual gate.
        if item.state in active_flow.gate_states:
            # Gate states have exactly one outgoing edge to a queue_state by
            # construction (see flows._build_state_machine). If a future flow
            # feature introduces a conditional branch, picking the first match
            # silently would hide the ambiguity — surface it explicitly.
            candidates = [
                dst
                for src, dst in active_flow.state_machine.transitions
                if src == item.state and self._find_step_for_state(dst, item=item) is not None
            ]
            if len(candidates) > 1:
                logger.warning(
                    "Gate state %r has multiple outgoing edges to queue_states %r; "
                    "auto-advance is taking the first by insertion order. "
                    "Define explicit routing for conditional gates.",
                    item.state,
                    candidates,
                )
            if candidates:
                dst = candidates[0]
                next_step = self._find_step_for_state(dst, item=item)
                assert next_step is not None  # candidates were filtered on this
                # Atomic CAS — the previous source.save → _dispatch_step path
                # had a crash window: state was advanced to the next step's
                # queue_state but current_step still pointed at the old
                # (approved) step. start()'s recovery flips status to blocked
                # and retry() then re-dispatches the already-approved step
                # because that's what current_step said. Folding state +
                # current_step into one UPDATE closes the window.
                result = await self.db.atomic_transition(
                    item.id,
                    from_status="working",
                    from_state=item.state,
                    to_state=dst,
                    to_status="working",
                    to_current_step=next_step.name,
                    audit_on_win=None,
                )
                if not result.won:
                    return  # another path already advanced this task
                item.state = dst
                await self._dispatch_step(item, next_step, feedback=feedback)
            return  # No next job found (e.g. gate is terminal) — do nothing

        # 4. Complete, blocked, or unknown — do nothing

    async def _dispatch_step(
        self,
        item: Item,
        step: FlowStep,
        feedback: str | None = None,
        triggering_comment_ids: list[int] | None = None,
    ) -> None:
        """Dispatch a specific step for an item.

        Callers must have already CAS'd the row to ``status='working'`` (this
        is the established invariant for every action method that ends up
        here). Both transitions in this method — missing-artifact → blocked
        and queue/active-state → ``step.active_state`` — therefore CAS from
        ``from_status='working'`` and short-circuit on lost races.

        ``triggering_comment_ids`` is forwarded to the ``InFlightStep`` so
        the eventual ``pr_decision`` audit row can reference which PR
        comments the agent responded to. Only the pr-fix dispatch path
        populates this; other steps pass ``None`` and the row falls back
        to an empty list.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")

        self.source.assign_id(item)

        # Resolve the active flow once — sub-flow steps (e.g. pr_fix's
        # ``pr-fix`` agent) live in the sub-flow's SM, not main's. Both
        # transitions guarded below (missing-artifact → blocked, queue →
        # active) must be validated against the SM that actually owns this
        # step. Today main's SM happens to carry every pr_fix edge that
        # ``_register_cross_flow_edges`` stitches in, but a future sub-flow
        # step with no cross-flow rule target would silently no-op against
        # ``self.flow`` (the root) here. Match the resolution shape used by
        # ``_dispatch_next_step`` one level up.
        active_flow = self._resolve_flow(item)

        # Validate required input artifacts exist
        if step.inputs:
            for artifact_name in step.inputs:
                content = await self.get_named_artifact(item.id, artifact_name)
                if content is None:
                    logger.error(
                        "Step %s requires artifact '%s' but it doesn't exist for task %s",
                        step.name,
                        artifact_name,
                        item.id,
                    )
                    if (item.state, "blocked") not in active_flow.state_machine.transitions:
                        return
                    result = await self.db.atomic_transition(
                        item.id,
                        from_status="working",
                        from_state=item.state,
                        to_state="blocked",
                        to_status="blocked",
                        to_current_step=step.name,
                        audit_on_win=AuditRow(
                            role="system",
                            step_name=step.job_type,
                            content=f"Blocked: missing required artifact '{artifact_name}'",
                            msg_type="error",
                        ),
                    )
                    if not result.won:
                        return
                    item.state = "blocked"
                    return

        # ADR-014 Layer A — monitor jobs collapse queue_state and active_state
        # into a single state (see flows._resolve_jobs), so no self-edge is
        # registered. They never transition state via _dispatch_step; only
        # status flips from ``working`` to ``waiting_for_pr`` so the engine's
        # ``list_waiting_pr_tasks`` query picks them up. Do the status flip
        # BEFORE the generic state-CAS path which would otherwise reject the
        # (state, state) tuple as missing from the transition map.
        #
        # Reachability note: only the *agent* completion path lands here (via
        # the drainer → ``_dispatch_next_step`` → ``_dispatch_step``).
        # Action→monitor transitions bypass ``_dispatch_next_step`` and fold
        # ``to_status="waiting_for_pr"`` directly into the success CAS inside
        # ``_execute_action_step``. So a custom action job followed by a
        # monitor job still reaches ``waiting_for_pr`` correctly — it just
        # never exercises this branch. Keep both paths in sync when changing
        # the monitor entry contract.
        if step.type == "monitor":
            result = await self.db.atomic_transition(
                item.id,
                from_status="working",
                from_state=item.state,
                to_state=item.state,
                to_status="waiting_for_pr",
                to_current_step=step.name,
                audit_on_win=None,
            )
            if not result.won:
                # A concurrent block()/jump_to_step()/transition_task already
                # claimed the row. Bail without re-asserting the status flip —
                # matches the won-check pattern at every other CAS site in
                # this file.
                return
            return

        # Single CAS: state → active_state, status → working, current_step →
        # step.name. Replaces the prior source.save + _set_status pair which
        # had a window where (state=active_state, current_step=null) was
        # observable to readers and concurrent action methods.
        if (item.state, step.active_state) not in active_flow.state_machine.transitions:
            return
        result = await self.db.atomic_transition(
            item.id,
            from_status="working",
            from_state=item.state,
            to_state=step.active_state,
            to_status="working",
            to_current_step=step.name,
            audit_on_win=None,
        )
        if not result.won:
            return
        item.state = step.active_state

        # Create or reuse a git worktree for this task (under the task's project)
        try:
            wt_path = await self._worktree_manager_for_task(item).create(item.id)
        except Exception:
            logger.warning("Worktree creation failed for %s, using project work_dir", item.id)
            wt_path = self._fallback_work_dir(item)

        info = InFlightStep(
            item=item,
            step=step,
            feedback=feedback,
            step_work_dir=wt_path,
            triggering_comment_ids=list(triggering_comment_ids or []),
        )
        # Branch on the typed job's execution mode.
        #   * ``agent``: existing _run_agent path (loads prompts, runs runner).
        #   * ``action``: dispatch via the registered tool callable.
        self._in_flight[item.id] = info
        if step.type == "action":
            info.task = asyncio.create_task(self._execute_action_step(info))
        else:
            info.task = asyncio.create_task(self._run_agent(info))

    async def _execute_action_step(self, info: InFlightStep) -> None:
        """Execute an ``action``-typed job by dispatching its registered tool.

        Layer A action dispatch. The tool is looked up via the registry and
        called with a ``TaskContext`` + the job's resolved ``config`` block.
        Successful results merge metadata into the task row and advance the
        state machine to ``step.success_state``. Failed results route to
        ``blocked`` by default. The failure handler also probes for an
        ``(item.state, "rebasing")`` edge when ``error_kind ==
        "non_fast_forward"`` so a process YAML that explicitly registers the
        edge could opt in to the legacy ``rebasing`` recovery path — but the
        bundled SE process has no ``rebasing`` state in its SM, so the probe
        falls through to ``blocked`` and recovery is a manual unblock-then-
        retry. See ADR-014 § "rebasing state collapsed into action-failure
        routing" and the inline comment below for the same caveat.
        """
        step, item = info.step, info.item
        if self.flow is None:
            self._in_flight.pop(item.id, None)
            return
        try:
            from lotsa.registry import get_tool
            from lotsa.tools import TaskContext, ToolResult

            tool = get_tool(step.tool or "")
            # Refresh metadata so the tool sees the latest task row — the
            # tool may read pr_number/github_owner/etc. that monitor polls
            # have written since dispatch was queued.
            fresh = await self.db.get_task(item.id)
            metadata = dict(fresh.metadata) if fresh else dict(item.metadata)
            # ``current_flow`` must reflect the task's active sub-flow, not the
            # root flow — a tool running inside ``pr_fix`` needs to see
            # ``current_flow="pr_fix"`` (set by ``_dispatch_pr_fix_locked``), not
            # ``"main"``. The fresh-read ``metadata`` above already carries the
            # right value; fall back to the root only when metadata is missing
            # the key (e.g. legacy rows created before sub-flow plumbing landed).
            ctx = TaskContext(
                task_id=item.id,
                worktree=info.step_work_dir or self._fallback_work_dir(info.item),
                metadata=metadata,
                db=self.db,
                process_name=self._process_for(item).name,
                flow_name=self._root_flow_for(item).name,
                current_flow=metadata.get("current_flow") or self._root_flow_for(item).name,
                last_run_step=step.name,
            )
            result = await tool(ctx, dict(step.config))
        except asyncio.CancelledError:
            self._in_flight.pop(item.id, None)
            raise
        except Exception as exc:
            logger.exception("Unhandled error in _execute_action_step for task %s step %s", item.id, step.name)
            from lotsa.tools import ToolResult

            result = ToolResult(
                success=False,
                output=f"{type(exc).__name__}: {exc}",
                metadata={"error_kind": "exception", "exception_type": type(exc).__name__},
            )

        self._in_flight.pop(item.id, None)

        # Merge any returned metadata before the state transition so a crash
        # between the two writes never loses the tool's side-effect record
        # (e.g. PR number, URL).
        #
        # Layer A race window — see claude[bot] round-9 review. Between this
        # merge and the success/failure CAS below, ``block()`` can win a CAS
        # against ``status="working"`` (it does not gate on ``_in_flight``).
        # If that happens, the action's CAS loses silently but the tool's
        # metadata is already persisted — the row ends up
        # ``status="blocked"`` with e.g. ``pr_number`` set, and a later
        # ``retry()`` re-dispatches this same action. Today this is
        # harmless: the only registered tool is ``push_pr``, which is
        # idempotent (a second ``git push`` of the same commits is a no-op
        # returning the same PR number). The agent drainer
        # (``_completion_drainer`` ~line 2462) has the same pre-CAS-pop
        # shape, so any real fix has to be applied symmetrically.
        #
        # A non-idempotent tool (one that creates an external ticket, mints
        # a token, sends a one-shot notification, etc.) must close this race
        # before being registered. Three options:
        #   1. Move this merge AFTER the CAS win — but a crash between
        #      CAS and merge then loses the tool's side-effect record
        #      (the deliberate ordering documented above).
        #   2. Make ``block()`` (and any other concurrent state-mutator)
        #      cancel/await ``_in_flight`` actions before its own CAS.
        #   3. Tag the tool result with an idempotency key the next
        #      dispatch can detect to skip re-execution.
        if result.metadata:
            await self._merge_task_metadata(item, dict(result.metadata))

        # Resolve the active flow once — sub-flow bindings (e.g. pr_fix's
        # push_pr) declare their success/failure transitions in the
        # sub-flow's SM, not main's. Validating against main would reject
        # legitimate edges and silently strand the task.
        active_flow = self._resolve_flow(item)
        active_sm = active_flow.state_machine.transitions

        if not result.success:
            error_kind = (result.metadata or {}).get("error_kind")
            # ADR-014 Layer A note: the legacy ``rebasing`` synthetic state
            # used to absorb ``non_fast_forward`` push failures and let
            # ``revise()`` route them back into pr-fix. The new SM has no
            # ``rebasing`` state (see
            # ``test_full_process_main_flow_has_no_synthetic_pr_states``);
            # the ``rebasing`` probe below is left in place so a process
            # YAML that explicitly registers the edge could opt in, but for
            # the bundled SE process this falls through to ``blocked`` and
            # recovery is a manual unblock-then-retry. See ADR-014 §
            # "rebasing state collapsed into action-failure routing".
            target_state = "rebasing" if error_kind == "non_fast_forward" else "blocked"
            if (item.state, target_state) not in active_sm:
                target_state = "blocked"
            if (item.state, target_state) not in active_sm:
                # No recoverable edge exists for this step — log and leave
                # the row's ``state`` column as-is (no SM edge to claim).
                # There is no CAS to lose here, so the audit writes below
                # are the only surface that records the failure to the
                # operator; emit them unconditionally.
                #
                # Best-effort status flip to ``blocked``: without it the row
                # is stranded at ``status="working"`` with an empty
                # ``_in_flight`` map until the next server restart's
                # recovery sweep runs. For the bundled SE process this path
                # is unreachable (``_build_state_machine`` always registers
                # ``(active_state, "blocked")``); the flip hardens custom
                # process YAMLs that forget the blocked edge.
                #
                # CAS rather than bare ``_set_status`` for symmetry with every
                # other status-flip in this file: a concurrent ``block()`` /
                # ``jump_to_step()`` / ``transition_task`` that already moved
                # the task would otherwise be silently overwritten with
                # ``blocked``. ``to_state=item.state`` preserves the original
                # ``_set_status`` semantics (only flip status; leave state at
                # the active value so ``retry()`` can find the step). On CAS
                # loss, the concurrent path owns the audit row; skip the
                # writes here.
                logger.warning(
                    "Action step %r for task %s failed but no (%r, %r) transition exists",
                    step.name,
                    item.id,
                    item.state,
                    target_state,
                )
                result_cas = await self.db.atomic_transition(
                    item.id,
                    from_status="working",
                    from_state=item.state,
                    to_state=item.state,
                    to_status="blocked",
                    to_current_step=step.name,
                    audit_on_win=AuditRow(
                        role="system",
                        step_name=step.job_type,
                        content=result.output,
                        msg_type="error",
                    ),
                )
                if not result_cas.won:
                    return
                await self.source.append_event(
                    item.id, {"type": "dispatch", "job_type": step.job_type, "success": False}
                )
                return
            # CAS-loss case: a concurrent ``block()`` / ``jump_to_step()`` /
            # ``transition_task`` already moved the task. Skip the audit
            # writes — emitting an "error" message attributed to this step
            # would surface a failure that the operator's action overrode,
            # and the concurrent path is responsible for its own audit row.
            # Matches the won-check pattern at every other CAS site in this
            # file.
            result_cas = await self.db.atomic_transition(
                item.id,
                from_status="working",
                from_state=item.state,
                to_state=target_state,
                to_status="blocked",
                to_current_step=step.name,
                audit_on_win=AuditRow(
                    role="system",
                    step_name=step.job_type,
                    content=result.output,
                    msg_type="error",
                ),
            )
            if not result_cas.won:
                return
            item.state = target_state
            await self.source.append_event(item.id, {"type": "dispatch", "job_type": step.job_type, "success": False})
            return

        # Success: advance to step.success_state. For action jobs that
        # transition into a monitor state, status flips to ``waiting_for_pr``
        # so the monitor engine's list query picks the task up. For terminal
        # success_state (complete), fold the status flip into the same CAS.
        #
        # Sub-flow terminal override: a sub-flow's last binding resolves to
        # ``success_state="complete"`` (the standard _resolve_jobs derivation),
        # but operationally the sub-flow's last step should return to the
        # host flow's monitor — not mark the task complete. ``pr_fix → push_pr``
        # is the canonical case: a successful pr-fix push lands new commits on
        # the PR, and the task must return to ``wait_for_pr_signal`` to await
        # merge / further feedback. The (last.active, host_monitor.queue) edge
        # is registered in the sub-flow's SM by ``_register_cross_flow_edges``;
        # this block redirects the runtime transition to match.
        # ADR-021: "root" is the task's OWN process's root flow, not a global
        # active flow.
        root_flow = self._root_flow_for(item)
        success_state = step.success_state
        is_subflow = active_flow.name != root_flow.name
        if is_subflow and success_state == "complete":
            host_monitor = next((s for s in root_flow.jobs if s.type == "monitor"), None)
            if host_monitor is not None:
                success_state = host_monitor.queue_state
        to_status: TaskStatusLiteral
        to_current_step: str | None
        # ``next_step`` resolution: try the active flow first so a sub-flow
        # action job's mid-sub-flow successor (which only exists in the
        # sub-flow's bindings) is found, then fall back to the root flow so
        # the sub-flow terminal override above still works — that override
        # redirects ``success_state`` to the host monitor's ``queue_state``
        # (e.g. ``pr_fix → push_pr`` rewrites to ``wait_for_pr_signal``),
        # which lives in the root flow's jobs, not in the sub-flow.
        # Both paths matter:
        #   * Root-flow action: active_flow == root_flow, found immediately.
        #   * Bundled sub-flow terminal (pr_fix.push_pr): active_flow=pr_fix
        #     lacks ``wait_for_pr_signal`` as a queue_state; main carries it.
        #   * Custom mid-sub-flow action: successor's queue_state lives only
        #     in the sub-flow's bindings; the active-flow lookup finds it
        #     and the (then-correct) ``next_step.name`` is what gets
        #     persisted to ``to_current_step`` below.
        next_step = next((s for s in active_flow.jobs if s.queue_state == success_state), None)
        if next_step is None:
            next_step = next((s for s in root_flow.jobs if s.queue_state == success_state), None)
        if success_state == "complete":
            to_status = "complete"
            to_current_step = None
        elif next_step is not None and next_step.type == "monitor":
            to_status = "waiting_for_pr"
            to_current_step = next_step.name
        else:
            to_status = "working"
            to_current_step = next_step.name if next_step else step.name

        if (item.state, success_state) not in active_sm:
            # The success edge is missing from the SM — log and best-effort
            # flip status to ``blocked`` so the row doesn't strand at
            # ``status="working"`` (the dispatch loop only re-fires from
            # ``queue_state``, but the active-state re-dispatch branch in
            # ``_dispatch_next_step`` would otherwise re-run this same
            # action indefinitely). For the bundled SE process this is
            # unreachable — ``_register_cross_flow_edges`` covers the
            # sub-flow success edges and ``_build_state_machine`` covers
            # the in-flow ones. Hardens custom process YAMLs that omit a
            # required success transition.
            #
            # CAS rather than bare ``_set_status`` for symmetry with every
            # other status-flip in this file: a concurrent ``block()`` /
            # ``jump_to_step()`` / ``transition_task`` that already moved
            # the task would otherwise be silently overwritten with
            # ``blocked``. ``to_state=item.state`` preserves the original
            # ``_set_status`` semantics (only flip status). On CAS loss the
            # concurrent path owns the audit row; skip the writes here.
            logger.warning(
                "Action step %r for task %s succeeded but (%r, %r) transition is unregistered",
                step.name,
                item.id,
                item.state,
                success_state,
            )
            # Mirror the failure-with-no-edge audit path above: on CAS-loss a
            # concurrent ``block()`` / ``jump_to_step()`` / ``transition_task``
            # owns the audit row, so skip the writes. On CAS-win the operator
            # debugging a silently-blocked task needs an in-task message that
            # names the missing edge — without this, only a log warning records
            # the misconfig and the UI shows ``blocked`` with no explanation.
            no_edge_msg = (
                f"Action succeeded but no ({item.state!r}, {success_state!r}) "
                f"SM edge; routing to blocked. Output: {result.output}"
            )
            result_cas = await self.db.atomic_transition(
                item.id,
                from_status="working",
                from_state=item.state,
                to_state=item.state,
                to_status="blocked",
                to_current_step=step.name,
                audit_on_win=AuditRow(
                    role="system",
                    step_name=step.job_type,
                    content=no_edge_msg,
                    msg_type="error",
                ),
            )
            if not result_cas.won:
                return
            await self.source.append_event(item.id, {"type": "dispatch", "job_type": step.job_type, "success": False})
            return
        result_cas = await self.db.atomic_transition(
            item.id,
            from_status="working",
            from_state=item.state,
            to_state=success_state,
            to_status=to_status,
            to_current_step=to_current_step,
            audit_on_win=AuditRow(
                role="system",
                step_name=step.job_type,
                content=result.output,
                msg_type="status_change",
            ),
        )
        if not result_cas.won:
            return
        item.state = success_state
        # Sub-flow exit: an action that lands the task into a monitor state
        # (e.g. ``push_pr`` → ``wait_for_pr_signal``) is the canonical return
        # to the root flow. Reset ``current_flow`` to the task's OWN process's
        # root flow name (ADR-021; not a hardcoded ``"main"``) so a custom
        # process whose root flow is named something else still persists the
        # right value. The merge happens after the CAS wins — a CAS-loss
        # leaves ``current_flow`` untouched, matching the won-check pattern
        # elsewhere.
        if next_step is not None and next_step.type == "monitor":
            await self._merge_task_metadata(item, {"current_flow": root_flow.name})
        await self.source.append_event(item.id, {"type": "dispatch", "job_type": step.job_type, "success": True})

        # If the next state is a monitor, the engine drives it from here.
        # Otherwise drive the next step via the standard dispatch loop.
        if next_step is None or next_step.type != "monitor":
            await self._dispatch_next_step(item)
            await self._cleanup_worktree_if_done(item)

    async def _run_step_posthooks(self, item: Item, step: FlowStep, work_dir: Path) -> str | None:
        """Run *step*'s resolved posthooks in order (ADR-024).

        Called by the completion drainer after an **agent** step succeeds and
        BEFORE the success-state CAS, so commit (the built-in posthook) lands
        the agent's diff before any downstream gate/push runs against it.
        Action/monitor steps never reach here, so they never run posthooks.

        Returns ``None`` on success (every posthook reported success), or the
        first posthook's error message to use as the task's block reason. Each
        posthook result is recorded as an audit message (commit SHA or no-op),
        mirroring how ``push_pr`` surfaces ``pr_number``/``pr_url``.
        """
        from lotsa.registry import get_posthook
        from lotsa.tools import TaskContext

        for name in step.posthooks:
            try:
                hook = get_posthook(name)
                # Mirror the TaskContext the action dispatcher builds. The
                # commit posthook reads ``worktree`` + ``last_run_step``; the
                # injected config carries the task title and the job's
                # configured commit prefix so the deterministic message can be
                # built without the posthook reaching into the task row.
                ctx = TaskContext(
                    task_id=item.id,
                    worktree=work_dir,
                    metadata=dict(item.metadata),
                    db=self.db,
                    process_name=self._process_for(item).name,
                    flow_name=self._root_flow_for(item).name,
                    current_flow=item.metadata.get("current_flow") or self._root_flow_for(item).name,
                    last_run_step=step.name,
                )
                config = {"task_title": item.title or "", "commit_prefix": step.commit_prefix or "chore"}
                result = await hook(ctx, config)
            except Exception as exc:  # noqa: BLE001 — any posthook crash blocks the task
                logger.exception("Posthook %r crashed for task %s step %s", name, item.id, step.name)
                return f"Posthook {name!r} crashed: {type(exc).__name__}: {exc}"

            if not result.success:
                return result.output or f"Posthook {name!r} failed"

            # Merge any posthook metadata (e.g. commit_sha) into the task row,
            # then record an operator-visible audit line.
            if result.metadata:
                await self._merge_task_metadata(item, dict(result.metadata))
            await self.db.add_message(
                item.id,
                "system",
                step.job_type,
                f"posthook {name}: {result.output}",
                "posthook",
                metadata=dict(result.metadata),
            )

        return None

    async def _run_agent(self, info: InFlightStep) -> None:
        """Run agent in background, push to completions queue on finish.

        Any unexpected exception (runner subprocess crash, prompt-load
        failure, post-run persistence error) is caught and converted to a
        synthetic failed AgentResult before re-entering the drainer.
        Without this, a crash anywhere in the body would leave the task
        stuck at ``status='working'`` with no completion event until the
        next server restart (``start()``'s recovery sweep) — the asyncio
        task would die silently and the drainer would never advance the
        row to ``blocked``.
        """
        step, item = info.step, info.item
        # Per-step model selection (ADR-022): the job's ``model:`` overrides the
        # global default for this one dispatch; unset jobs fall back to it.
        resolved_model = step.model or self.config.model
        try:
            if self.flow is None:
                raise RuntimeError("OrchestratorService not started")

            # Resolve the runner for this dispatch (ADR-023). Done once so the
            # dispatch-shape prompt, the ``run()`` call, and the audit metadata
            # all reflect the same runner. ``RunnerNotFound`` propagates into the
            # body's failure handler (→ blocked), which is the loud, intended
            # outcome for an unroutable model name.
            resolved = self._resolve_runner(step)
            runner = resolved.runner
            info.agent_runner_name = resolved.name

            system = self._build_system_prompt(step, item, runner=runner)

            session_id = None
            if (step.resume_session or (step.conversational and info.feedback)) and "session_id" in item.metadata:
                session_id = item.metadata["session_id"]

            # For resumed conversational sessions, send just the user's message
            if step.conversational and session_id and info.feedback:
                user_prompt = info.feedback
            else:
                user_template = self._resolve_flow(item).registry.load(f"{step.prompt_name}-user")
                subs = {"{title}": item.title or "", "{body}": item.body or ""}
                user_prompt = re.sub(
                    r"\{title\}|\{body\}",
                    lambda m: subs[m.group()],
                    user_template,
                )

                # Inject named artifacts: {artifact:spec} → artifact content
                artifact_refs = re.findall(r"\{artifact:(\w+)\}", user_prompt)
                for art_name in artifact_refs:
                    content = await self.get_named_artifact(item.id, art_name)
                    user_prompt = user_prompt.replace(f"{{artifact:{art_name}}}", content or "(not available)")

                if info.feedback:
                    user_prompt += f"\n\n## Revision Feedback\n\n{info.feedback}"

            work_dir = info.step_work_dir or self._fallback_work_dir(info.item)
            result = await runner.run(
                system_prompt=system,
                user_prompt=user_prompt,
                work_dir=work_dir,
                session_id=session_id,
                model=resolved_model,
            )

            info.agent_result = result

            # Persist output as messages (skip stdout for conversational — stored as chat in drainer)
            if not step.conversational:
                output_meta = _run_stats(result) or {}
                output_meta["agent_model"] = resolved_model
                # ADR-023 — record which registered runner answered (e.g. ``gpt``,
                # ``default``), a sibling to ``agent_model``, so an audit reader
                # can tell two runners for the same model apart.
                output_meta["agent_runner"] = resolved.name
                await self.source.save_step_output(item.id, step.job_type, result.stdout or "", metadata=output_meta)
            if result.stderr:
                await self.source.save_stderr(item.id, step.job_type, result.stderr)

            # Save stdout as named artifact if this step declares an output
            # (skip conversational steps — their artifact is saved in the drainer
            # at SPEC_COMPLETE detection with cleaned content). Narration ahead
            # of the content anchor is stripped at the source; an artifact that
            # is unusable after stripping fails the dispatch (→ blocked, Retry
            # re-runs the agent) instead of persisting garbage for downstream
            # {artifact:NAME} prompt injection and the push step to consume.
            if step.output and not step.conversational and result.stdout and result.stdout.strip():
                cleaned = _strip_artifact_narration(result.stdout)
                if result.success and len(cleaned) < _MIN_ARTIFACT_CHARS:
                    raise ArtifactCaptureError(
                        f"Declared output artifact {step.output!r} is unusable: "
                        f"step stdout reduced to {len(cleaned)} chars after narration "
                        f"stripping (minimum {_MIN_ARTIFACT_CHARS}). Retry re-runs the step."
                    )
                artifact_meta = {"artifact_name": step.output}
                await self.source.save_artifact(item.id, step.job_type, cleaned, metadata=artifact_meta)

            await self.source.append_event(
                item.id,
                {
                    "type": "dispatch",
                    "job_type": step.job_type,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                    "cost_usd": result.cost_usd,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            )
        except asyncio.CancelledError:
            # Shutdown / jump_to_step cancellation — don't synthesize a
            # completion (the cancelling path is responsible for cleanup).
            raise
        except Exception as exc:
            logger.exception("Unhandled error in _run_agent for task %s step %s", item.id, step.name)
            elapsed_ms = int((time.monotonic() - info.started_at) * 1000)
            info.agent_result = AgentResult(
                success=False,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                return_code=-1,
                duration_ms=elapsed_ms,
            )

        logger.info(
            "drainer enqueue: task=%s step=%s success=%s",
            item.id,
            step.name,
            info.agent_result.success if info.agent_result else None,
        )
        await self._completions.put(info)

    async def _completion_drainer(self) -> None:
        """Background task: process finished agents."""
        while True:
            # ADR-030: bound to the iteration so the ``finally`` can apply any
            # PR terminal the PrMonitor deferred while this task was ``working``
            # — AFTER the agent's own completion routing below has run. ``None``
            # until ``get()`` returns so a cancellation at ``get()`` skips it.
            info: InFlightStep | None = None
            try:
                info = await self._completions.get()
                item = info.item
                result = info.agent_result
                logger.info(
                    "drainer dequeue: task=%s step=%s state=%s success=%s",
                    item.id,
                    info.step.name,
                    item.state,
                    result.success if result is not None else None,
                )
                if result is None:
                    continue

                self._in_flight.pop(item.id, None)

                if result.session_id:
                    await self._merge_task_metadata(item, {"session_id": result.session_id})

                if not result.success:
                    if self.flow is None:
                        raise RuntimeError("OrchestratorService not started")
                    err_msg = _summarize_agent_error(result.return_code, result.stderr)
                    # Validate against the active flow's SM — a sub-flow-only
                    # agent step whose ``active_state`` isn't stitched into
                    # main's SM (custom processes only; the bundled ``full``
                    # process is covered by ``_register_cross_flow_edges``
                    # for every pr_fix binding) would otherwise silently
                    # ``continue`` and strand the task at ``status=working``.
                    active_flow_for_fail = self._resolve_flow(item)
                    if (item.state, "blocked") not in active_flow_for_fail.state_machine.transitions:
                        logger.warning(
                            "drainer: task=%s agent-failed at step=%s but no (%s -> blocked) edge in active "
                            "flow — cannot block, task left status=working",
                            item.id,
                            info.step.name,
                            item.state,
                        )
                        continue
                    cas = await self.db.atomic_transition(
                        item.id,
                        from_status="working",
                        from_state=item.state,
                        to_state="blocked",
                        to_status="blocked",
                        to_current_step=info.step.name,
                        audit_on_win=AuditRow(
                            role="system",
                            step_name=info.step.job_type,
                            content=err_msg,
                            msg_type="error",
                            metadata={"return_code": result.return_code},
                        ),
                    )
                    if not cas.won:
                        continue
                    item.state = "blocked"
                    # Phase 2 — pr-fix audit trail must cover the agent-crash
                    # path too. A sandboxed OOM, network failure, or
                    # unhandled exception in the agent process produces
                    # ``result.success == False`` and lands here; without a
                    # pr_decision row the audit trail acceptance criterion
                    # (every pr-fix dispatch produces a pr_decision message)
                    # would be violated. Mirror the agent-emitted-BLOCKED
                    # branch below: decision="blocked", reasoning=err_msg,
                    # commit_sha=None, round read from item.metadata which
                    # is the post-increment value set by
                    # ``_dispatch_pr_fix_locked`` (``_in_flight`` blocked
                    # concurrent dispatch and no other writer touches
                    # ``pr_fix_round_count``).
                    if info.step.name == "pr-fix":
                        this_round = int(item.metadata.get("pr_fix_round_count", 0))
                        await self._record_pr_decision(
                            item.id,
                            decision="blocked",
                            reasoning=err_msg,
                            triggering_comment_ids=info.triggering_comment_ids,
                            commit_sha=None,
                            duration_ms=result.duration_ms,
                            cost_usd=result.cost_usd,
                            round_n=this_round,
                        )
                else:
                    if self.flow is None:
                        raise RuntimeError("OrchestratorService not started")

                    # Refresh ``item.metadata`` from the DB on every non-review
                    # step completion so the NEEDS_DECISION / SKIPPED branches
                    # below see the post-increment ``pr_fix_round_count`` that
                    # ``_dispatch_pr_fix_locked`` persisted (``_in_flight`` blocks
                    # concurrent dispatch, so the post-increment value is the
                    # one this drainer should route on).
                    #
                    # ADR-014 Layer A removed ``target: previous`` and the
                    # ``previous_step`` metadata field it routed on. The field
                    # has no remaining readers (the tests that asserted on it
                    # are skipped with the matching reason in
                    # ``test_orchestrator.TestPreviousStepTracking``); drop the
                    # write so the row's metadata reflects only fields the
                    # current code actually consumes. The fresh-read in
                    # ``get_task`` is the load-bearing side effect; passing an
                    # empty merge keeps the existing helper's "fresh read +
                    # in-place item.metadata refresh" contract without
                    # introducing a parallel inline path.
                    if info.step.name != "review":
                        await self._merge_task_metadata(item, {})

                    # ADR-024 — run the step's posthooks (e.g. ``commit``) on
                    # agent success, BEFORE any of the success/advance CAS
                    # sites below. A single insertion point here covers every
                    # producer exit path (rule-route, conversational, gate,
                    # auto-advance) — the symmetric-paths invariant this
                    # codebase relies on (lotsa/CLAUDE.md). A posthook failure
                    # blocks the task with the error as the reason and no
                    # retry; the success CAS never runs.
                    #
                    # Skip posthooks when the agent emitted ``NEEDS_INPUT:``.
                    # A pausing agent has NOT finished its step — its work is
                    # incomplete by definition (it is asking the operator a
                    # question and will resume the same session on
                    # ``answer()``). Running completion posthooks here is wrong
                    # for every step, and actively corrupting for
                    # ``resolve_conflicts``: the ``commit`` posthook's
                    # ``git add -A`` stages the still-unresolved conflict paths
                    # (raw ``<<<<<<<`` markers) at stage 0 and commits them,
                    # leaving the worktree on a merge commit full of markers
                    # with no MERGE_HEAD — a different, confusing task than the
                    # one the prompt describes when the operator answers. The
                    # posthooks run on the resume pass once the agent emits its
                    # terminal marker (``CONFLICTS_RESOLVED:`` etc.).
                    pending_question = _extract_needs_input(result.stdout)
                    if info.step.posthooks and pending_question is None:
                        posthook_work_dir = info.step_work_dir or self._fallback_work_dir(info.item)
                        posthook_err = await self._run_step_posthooks(item, info.step, posthook_work_dir)
                        if posthook_err is not None:
                            active_flow_for_hook = self._resolve_flow(item)
                            if (item.state, "blocked") in active_flow_for_hook.state_machine.transitions:
                                cas = await self.db.atomic_transition(
                                    item.id,
                                    from_status="working",
                                    from_state=item.state,
                                    to_state="blocked",
                                    to_status="blocked",
                                    to_current_step=info.step.name,
                                    audit_on_win=AuditRow(
                                        role="system",
                                        step_name=info.step.job_type,
                                        content=f"Posthook failed: {posthook_err}",
                                        msg_type="error",
                                    ),
                                )
                                if cas.won:
                                    item.state = "blocked"
                                    # Preserve the "every pr-fix dispatch writes
                                    # a pr_decision row" audit invariant — mirror
                                    # the agent-crash branch above.
                                    if info.step.name == "pr-fix":
                                        this_round = int(item.metadata.get("pr_fix_round_count", 0))
                                        await self._record_pr_decision(
                                            item.id,
                                            decision="blocked",
                                            reasoning=posthook_err,
                                            triggering_comment_ids=info.triggering_comment_ids,
                                            commit_sha=None,
                                            duration_ms=result.duration_ms,
                                            cost_usd=result.cost_usd,
                                            round_n=this_round,
                                        )
                            continue

                    rule_target = None
                    if info.step.rules:
                        rule_work_dir = info.step_work_dir or self._fallback_work_dir(info.item)
                        rule_target = evaluate_output_rules(info.step.rules, result, rule_work_dir)
                    if rule_target is not None and rule_target != "next":
                        # Phase 2 — pr-fix NEEDS_DECISION escalation. The
                        # flow.yaml rule targets the synthetic "needs_input"
                        # value; ``resolve_output_target`` would route it
                        # to "blocked", so handle it here BEFORE the
                        # generic target-resolution path. Behavioural
                        # contract: persist the question, write a
                        # pr_decision audit row, keep state="pr-fixing",
                        # flip status to needs_input so ``answer()`` can
                        # resume the same step.
                        if info.step.name == "pr-fix" and rule_target == "needs_input":
                            question = _extract_needs_decision_question(result.stdout or "")
                            await self._persist_question(item.id, info.step.name, question)
                            # ``_record_pr_decision`` takes ``round_n`` as a
                            # required parameter — it never reads the counter
                            # itself. Read from ``item.metadata`` which the
                            # ``_merge_task_metadata`` call above just refreshed
                            # from a fresh DB row, so ``pr_fix_round_count`` is
                            # the post-increment value set by
                            # ``_dispatch_pr_fix_locked``. The field is invariant
                            # during this drainer pass (``_in_flight`` blocks
                            # concurrent dispatch and no other writer touches
                            # it), so this avoids a redundant DB round-trip
                            # without races. Same pattern as the SKIPPED branch
                            # below.
                            this_round = int(item.metadata.get("pr_fix_round_count", 0))
                            await self._record_pr_decision(
                                item.id,
                                decision="needs_decision",
                                reasoning=question,
                                triggering_comment_ids=info.triggering_comment_ids,
                                commit_sha=None,
                                duration_ms=result.duration_ms,
                                cost_usd=result.cost_usd,
                                round_n=this_round,
                            )
                            # State is unchanged (to_state=item.state) and the
                            # audit row + question are already durable pre-CAS,
                            # so the audit trail is intact whether or not this
                            # CAS lands. The drainer-iteration outcome is also
                            # the same either way (we ``continue`` below).
                            #
                            # The operator-facing outcome diverges, however:
                            # on a CAS loss (narrow race: a concurrent
                            # ``block()`` or similar writer claims the
                            # (status, state) row between the pre-CAS audit
                            # writes above and this CAS), the task stays in
                            # ``working`` with an orphaned ``question`` the
                            # operator can't answer via ``answer()``. The task
                            # is recoverable via ``start()``'s startup sweep
                            # on the next process restart, but the warning
                            # below is what tells the operator why ``answer()``
                            # didn't work right now.
                            #
                            # The unconditional ``continue`` below is what
                            # actually prevents fall-through to the SKIPPED
                            # branch / generic resolve_output_target path —
                            # don't drop it.
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=item.state,
                                to_status="needs_input",
                                to_current_step=info.step.name,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                logger.warning(
                                    "pr-fix NEEDS_DECISION fired for task %s but CAS lost to a "
                                    "concurrent writer; task remains in working status with an "
                                    "orphaned question — recoverable via start() sweep on next "
                                    "process restart",
                                    item.id,
                                )
                            continue

                        # R4: pr-fix may emit PR_FIX_SKIPPED: with a target
                        # naming the monitor's state — ``waiting_for_pr`` in
                        # the legacy synthetic-state model, ``wait_for_pr_signal``
                        # (or whatever the YAML names its pr_monitor job) in
                        # the ADR-014 Layer A model. resolve_output_target
                        # would happily route a real monitor state name, but
                        # this branch also handles the legacy synthetic name
                        # and the side-effect bookkeeping (skipped counter,
                        # comment-cursor advance). ADR-021: resolve the
                        # destination against the task's own process monitor
                        # state (and validate against its SM) so both shapes
                        # work per process.
                        monitor_state = self._monitor_state_for(item) or "waiting_for_pr"
                        if info.step.name == "pr-fix" and rule_target in (
                            "waiting_for_pr",
                            monitor_state,
                        ):
                            if (item.state, monitor_state) not in self._resolve_flow(item).state_machine.transitions:
                                logger.warning(
                                    "drainer: task=%s step=%s skip→monitor but no (%s -> %s) edge — "
                                    "task left status=working",
                                    item.id,
                                    info.step.name,
                                    item.state,
                                    monitor_state,
                                )
                                continue
                            fresh = await self.db.get_task(item.id)
                            dispatched_at = (
                                fresh.metadata.get("pr_fix_dispatched_at") if fresh else None
                            ) or datetime.now(UTC).isoformat()
                            # ``to_current_step=monitor_state`` (not ``None``):
                            # leaving ``current_step`` unset means a later
                            # ``transition_task("blocked")`` would hit the
                            # fallback at line 1588 (``task.current_step or
                            # "push"``) and write ``current_step="push"``,
                            # which then makes ``retry()`` (line 1203) route
                            # back through the legacy ``_execute_push`` path
                            # instead of re-entering the monitor. Persisting
                            # the monitor's name keeps the row addressable
                            # under both the operator block() path and the
                            # engine-driven block path.
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=monitor_state,
                                to_status="waiting_for_pr",
                                to_current_step=monitor_state,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                continue
                            item.state = monitor_state
                            # Phase 2 — bump pr_fix_consecutive_skipped and
                            # advance pr_comments_since in one merge so a
                            # concurrent writer can't see the counter
                            # bump without the cursor advance.
                            #
                            # Read from item.metadata (no fresh fetch): the
                            # in-place ``_merge_task_metadata`` call in
                            # ``_dispatch_pr_fix_locked`` refreshed item.metadata
                            # just before dispatch, and ``_in_flight`` /
                            # ``_dispatching_pr_fix`` exclude concurrent writes
                            # to ``pr_fix_consecutive_skipped`` while the agent
                            # runs. ``_merge_task_metadata`` itself does a
                            # fresh get_task() to merge cleanly with any other
                            # field updates (e.g. PrMonitor advancing
                            # ``pr_comments_since`` on an in-flight write).
                            # A skip counts toward max_consecutive_skipped ONLY
                            # when actionable feedback was actually delivered to
                            # the agent (see ``_feedback_is_actionable``). A skip
                            # with no feedback injected — the reviewer's review
                            # still streaming in, an empty retry, or bot chatter
                            # that aggregated to nothing — is benign (the agent
                            # correctly had nothing to do) and must not burn the
                            # cap. Task 9c7c28e9: in-progress-review skips plus an
                            # empty retry tripped the cap ~38s before the real
                            # review posted, blocking the task with its findings
                            # unhandled. The cap's intent is "agent keeps dodging
                            # REAL feedback," which requires real feedback present.
                            feedback_was_actionable = _feedback_is_actionable(info.feedback)
                            prev_skipped = int(item.metadata.get("pr_fix_consecutive_skipped", 0))
                            new_skipped = prev_skipped + 1 if feedback_was_actionable else prev_skipped
                            # Sub-flow exit: task is back in the monitor's
                            # state (root flow). Reset ``current_flow`` to the
                            # task's OWN process's root flow name (ADR-021; not
                            # a hardcoded ``"main"``) so a custom process whose
                            # root flow is named something else still persists
                            # the right value. The reset rides on the same
                            # merge as the counter/cursor advance so a
                            # concurrent reader never sees a partial update.
                            await self._merge_task_metadata(
                                item,
                                {
                                    "pr_comments_since": dispatched_at,
                                    "pr_fix_consecutive_skipped": new_skipped,
                                    "current_flow": self._root_flow_for(item).name,
                                },
                            )
                            # Capture the dispatch's round once. ``_record_pr_decision``
                            # takes ``round_n`` as a required parameter — it
                            # never reads the counter itself. Read from
                            # ``item.metadata`` which the ``_merge_task_metadata`` call
                            # above just refreshed from a fresh DB row, so
                            # ``pr_fix_round_count`` is the post-increment value set
                            # by ``_dispatch_pr_fix_locked``. The field is invariant
                            # during this drainer pass (no concurrent writer touches
                            # it), so both the SKIPPED row and any cap-fire row below
                            # reference the SAME dispatch without a redundant DB
                            # round-trip. Matches the pre-dispatch cap-fire path in
                            # ``_pr_fix_round_cap_blocked``.
                            this_round = int(item.metadata.get("pr_fix_round_count", 0))
                            last_line = ""
                            if result.stdout:
                                for line in reversed(result.stdout.splitlines()):
                                    if line.strip():
                                        # Strip the ``PR_FIX_<MARKER>:`` prefix so
                                        # the ``pr_decision.reasoning`` audit field
                                        # carries the human-readable summary in the
                                        # same format as NEEDS_DECISION (which uses
                                        # ``_extract_needs_decision_question``) and
                                        # the cap-fire synthesised messages.
                                        # Contract documented on
                                        # ``_record_pr_decision``.
                                        last_line = _strip_pr_fix_marker_prefix(line)
                                        break
                            await self.db.add_message(
                                item.id,
                                "system",
                                info.step.job_type,
                                f"pr-fix skipped: {last_line}",
                                "stage_transition",
                                metadata={"from_step": "pr-fix", "to_step": monitor_state},
                            )
                            # Audit-write the SKIPPED outcome BEFORE the
                            # consecutive-skip cap check so the row is
                            # durable regardless of whether the cap fires.
                            await self._record_pr_decision(
                                item.id,
                                decision="skipped",
                                reasoning=last_line or "no reasoning provided",
                                triggering_comment_ids=info.triggering_comment_ids,
                                commit_sha=None,
                                duration_ms=result.duration_ms,
                                cost_usd=result.cost_usd,
                                round_n=this_round,
                            )
                            # Consecutive-skip cap post-check. Cap-fire
                            # writes a SECOND pr_decision row (decision=
                            # blocked) so an operator can see both: the
                            # skip that just landed and the cap that fired
                            # as a result. Same ``this_round`` so the
                            # operator can attribute both rows to the same
                            # dispatch.
                            pr_cfg = self._pr_monitor_config_for(item)
                            cap_skipped = pr_cfg.max_consecutive_skipped if pr_cfg else 0
                            # Only fire on a counted (actionable) skip — a benign
                            # skip left ``new_skipped`` unchanged and must never
                            # trip the cap.
                            if feedback_was_actionable and cap_skipped > 0 and new_skipped >= cap_skipped:
                                cap_reason = (
                                    f"Agent skipped {new_skipped} reviewer comments in a row "
                                    f"(cap={cap_skipped}). Please verify the agent's reasoning."
                                )
                                # Audit-first: write the ``pr_decision(blocked)`` row
                                # BEFORE the state machine check and CAS, mirroring
                                # ``_pr_fix_round_cap_blocked``. Guarantees the operator
                                # can see why ``blocked`` was attempted even on a
                                # PrMonitor race where the cap CAS loses to a concurrent
                                # dispatch flipping status back to ``working`` — without
                                # this, the operator would see the SKIPPED row but no
                                # explanation of why ``blocked`` didn't land.
                                await self._record_pr_decision(
                                    item.id,
                                    decision="blocked",
                                    reasoning=cap_reason,
                                    triggering_comment_ids=info.triggering_comment_ids,
                                    commit_sha=None,
                                    duration_ms=None,
                                    cost_usd=None,
                                    round_n=this_round,
                                )
                                if (item.state, "blocked") in self._resolve_flow(item).state_machine.transitions:
                                    cas = await self.db.atomic_transition(
                                        item.id,
                                        from_status="waiting_for_pr",
                                        # ``item.state`` (rather than a hardcoded
                                        # ``"waiting_for_pr"``) keeps this CAS in lock-
                                        # step with the sibling SKIPPED CAS above —
                                        # which also uses ``from_state=item.state``.
                                        # Equivalent today because the SKIPPED CAS
                                        # targets ``to_state="waiting_for_pr"``, but
                                        # if a custom flow ever renames that target
                                        # the hardcode would silently mis-target this
                                        # cap-fire CAS (transitions check above would
                                        # also use the wrong tuple), leaving the cap
                                        # permanently unable to fire on the custom
                                        # flow's waiting state.
                                        from_state=item.state,
                                        to_state="blocked",
                                        to_status="blocked",
                                        to_current_step="pr-fix",
                                        audit_on_win=AuditRow(
                                            role="system",
                                            step_name="pr-fix",
                                            content=cap_reason,
                                            msg_type="status_change",
                                        ),
                                    )
                                    if cas.won:
                                        item.state = "blocked"
                                    else:
                                        # Narrow race: between the SKIPPED CAS win above
                                        # and this cap-fire CAS, a PrMonitor poll can
                                        # dispatch a new round (flipping status back to
                                        # ``working``), so ``from_status="waiting_for_pr"``
                                        # no longer matches and the cap CAS loses. The
                                        # pre-CAS ``pr_decision`` row is already durable
                                        # (audit-first), so operators will see why the cap
                                        # fired even though the transition didn't land.
                                        # The counter was correctly persisted, so the cap
                                        # will fire on the next SKIPPED outcome — one
                                        # dispatch late but not lost.
                                        logger.warning(
                                            "pr-fix consecutive-skip cap fired for task %s "
                                            "(skipped=%d/cap=%d) but CAS lost to a concurrent "
                                            "dispatch; cap will fire on next SKIPPED outcome",
                                            item.id,
                                            new_skipped,
                                            cap_skipped,
                                        )
                                else:
                                    # Custom flows that omit the ``(<state>, "blocked")``
                                    # transition leave the cap unable to fire — the
                                    # pre-CAS ``pr_decision`` row is durable, but the
                                    # task stays in its current state with the
                                    # consecutive-skip counter incremented, so the
                                    # cap will keep re-firing on every future SKIPPED
                                    # outcome with no recoverable path short of a
                                    # manual DB edit. Surface this in logs so a
                                    # custom-flow operator can spot the
                                    # misconfiguration; the bundled "full" flow
                                    # registers ``(waiting_for_pr, blocked)`` via
                                    # ``_build_state_machine`` so this branch is
                                    # unreachable in production. Mirrors the
                                    # else-branch warning in
                                    # ``_pr_fix_round_cap_blocked``.
                                    logger.warning(
                                        "pr-fix consecutive-skip cap fired for task %s "
                                        "(skipped=%d/cap=%d) but state machine has no "
                                        "(%r, 'blocked') transition; task remains in "
                                        "state=%r without a recoverable path",
                                        item.id,
                                        new_skipped,
                                        cap_skipped,
                                        item.state,
                                        item.state,
                                    )
                            # No _dispatch_next_step — task stays in
                            # waiting_for_pr (or blocked, if the cap fired)
                            # until the next monitor poll or manual action.
                            continue
                        # ADR-014 Layer A — ``previous_step_name`` kwarg removed
                        # from ``resolve_output_target``. The autonomous code↔
                        # review loop is now spelled by name in the main flow's
                        # per-binding rule override (REVIEW_FAIL → code).
                        # Resolve against the task's *active* flow so sub-flow
                        # rule targets (e.g. ``pr_fix.review.REVIEW_FAIL → pr-fix``)
                        # find their target job in the right bindings.
                        active_flow_for_rule = self._resolve_flow(item)
                        target = resolve_output_target(
                            rule_target,
                            info.step,
                            active_flow_for_rule,
                        )
                        # Save chat message for conversational steps so the conversation
                        # history isn't lost when routing (e.g. NEEDS_REVIEW from verify)
                        if info.step.conversational:
                            await self.db.add_message(
                                item.id,
                                "agent",
                                info.step.job_type,
                                result.stdout,
                                "chat",
                                metadata=_run_stats(result),
                            )
                        # Validate the rule-target transition against the *active*
                        # flow's SM, not main's. Sub-flow bindings (e.g.
                        # pr_fix.review's REVIEW_FAIL → pr-fix) declare edges that
                        # only exist in the sub-flow's SM — checking against main
                        # would reject the CAS and strand the task in working.
                        if (item.state, target) not in active_flow_for_rule.state_machine.transitions:
                            logger.warning(
                                "drainer: task=%s step=%s rule routed to %r but no (%s -> %s) edge in active "
                                "flow %s — completion dropped, task left status=working",
                                item.id,
                                info.step.name,
                                target,
                                item.state,
                                target,
                                self._resolve_flow(item).name,
                            )
                            continue
                        # End-state (status, current_step) depends on target:
                        # terminal targets fold the status flip into the same
                        # CAS; non-terminal targets keep status='working' so
                        # _dispatch_next_step can drive the next step's CAS.
                        if target == "blocked":
                            to_status: TaskStatusLiteral = "blocked"
                            to_current_step: str | None = info.step.name
                        elif target == "complete":
                            to_status = "complete"
                            to_current_step = None
                        else:
                            to_status = "working"
                            to_current_step = info.step.name
                        cas = await self.db.atomic_transition(
                            item.id,
                            from_status="working",
                            from_state=item.state,
                            to_state=target,
                            to_status=to_status,
                            to_current_step=to_current_step,
                            audit_on_win=None,
                        )
                        if not cas.won:
                            continue
                        item.state = target
                        # Phase 2 — pr-fix outcome audit. DONE / agent-emitted
                        # BLOCKED both produce a pr_decision row. SKIPPED and
                        # NEEDS_DECISION are handled in their own special-case
                        # branches above. DONE additionally resets the
                        # consecutive-skip counter; BLOCKED deliberately does
                        # not (a manual unblock-then-skip should count).
                        if info.step.name == "pr-fix":
                            if target == "blocked":
                                pr_decision: Literal["done", "blocked"] = "blocked"
                                commit_sha: str | None = None
                            else:
                                pr_decision = "done"
                                work_dir = info.step_work_dir or self._fallback_work_dir(info.item)
                                commit_sha = await _read_head_sha(work_dir)
                                await self._merge_task_metadata(item, {"pr_fix_consecutive_skipped": 0})
                            reasoning_text = ""
                            if result.stdout:
                                for line in reversed(result.stdout.splitlines()):
                                    if line.strip():
                                        # Strip ``PR_FIX_<MARKER>:`` prefix —
                                        # keeps the audit field format consistent
                                        # across decision types. See
                                        # ``_strip_pr_fix_marker_prefix`` and
                                        # ``_record_pr_decision``'s docstring.
                                        reasoning_text = _strip_pr_fix_marker_prefix(line)
                                        break
                            # ``_record_pr_decision`` takes ``round_n`` as a
                            # required parameter — it never reads the counter
                            # itself. ``pr_fix_round_count`` is invariant for
                            # the duration of the agent run: ``_in_flight``
                            # blocks concurrent dispatch (the only writer of
                            # this field) and no other code path touches it.
                            # That invariant is what makes reading from
                            # ``item.metadata`` sound in BOTH branches, but
                            # the freshness story differs:
                            #
                            #   * DONE: the ``_merge_task_metadata(
                            #     {"pr_fix_consecutive_skipped": 0})`` call
                            #     a few lines up just re-read ``item.metadata``
                            #     from a fresh DB row, so ``this_round`` is
                            #     trivially current.
                            #   * BLOCKED: no merge happens in this branch
                            #     (deliberate — BLOCKED must NOT reset the
                            #     consecutive-skip counter; see the comment
                            #     at line 2302). ``item.metadata`` may be
                            #     many agent-run seconds stale by now, but
                            #     ``pr_fix_round_count`` specifically was
                            #     written by ``_dispatch_pr_fix_locked``
                            #     BEFORE the agent started and is pinned by
                            #     ``_in_flight`` for the whole run, so the
                            #     stale read still returns the correct
                            #     post-increment value. The asymmetry is
                            #     intentional: avoiding the redundant fresh
                            #     fetch keeps the drainer tight, and the
                            #     ``_in_flight`` invariant makes it safe.
                            #
                            # Same invariant underpins the SKIPPED branch's
                            # ``item.metadata`` read above.
                            this_round = int(item.metadata.get("pr_fix_round_count", 0))
                            await self._record_pr_decision(
                                item.id,
                                decision=pr_decision,
                                reasoning=reasoning_text,
                                triggering_comment_ids=info.triggering_comment_ids,
                                commit_sha=commit_sha,
                                duration_ms=result.duration_ms,
                                cost_usd=result.cost_usd,
                                round_n=this_round,
                            )
                        if target != "blocked":
                            # Pass the agent's output as feedback so the next step
                            # knows why it was routed (e.g. REVIEW_FAIL summary)
                            await self._dispatch_next_step(item, feedback=result.stdout)
                        await self._cleanup_worktree_if_done(item)
                    else:
                        # Check for conversational step completion via rules
                        if info.step.conversational:
                            spec = check_conversational_rules(info.step, result.stdout)
                            # Store AI response as chat message with execution metadata.
                            # ``agent_model`` records the resolved per-step model
                            # (ADR-022: step.model or the global default), applied
                            # here independently against the drained ResolvedJob.
                            chat_meta: dict[str, object] = {"duration_ms": result.duration_ms}
                            chat_meta["agent_model"] = info.step.model or self.config.model
                            if result.model:
                                chat_meta["model"] = result.model
                            # ADR-023 — the registered runner name (e.g. ``gpt``,
                            # ``default``), carried from the dispatch body on the
                            # in-flight record. Replaces the former class-name
                            # ``runner`` field; the frontend reads ``agent_model``,
                            # not ``runner``, so this is a clean swap.
                            if info.agent_runner_name is not None:
                                chat_meta["agent_runner"] = info.agent_runner_name
                            if result.input_tokens is not None:
                                chat_meta["input_tokens"] = result.input_tokens
                            if result.output_tokens is not None:
                                chat_meta["output_tokens"] = result.output_tokens
                            if result.cost_usd is not None:
                                chat_meta["cost_usd"] = result.cost_usd
                            await self.db.add_message(
                                item.id, "agent", info.step.job_type, result.stdout, "chat", metadata=chat_meta
                            )
                            # Persist the artifact RIGHT NOW (not at approve time). The drainer is
                            # the only place where SPEC_COMPLETE can be reliably observed; if we
                            # defer to approve, closing the browser between detection and approve
                            # loses the spec content.
                            if spec is not None and info.step.output:
                                cleaned = _strip_spec_marker(spec)
                                if cleaned:
                                    await self.source.save_artifact(
                                        item.id,
                                        info.step.job_type,
                                        cleaned,
                                        metadata={"artifact_name": info.step.output},
                                    )
                                    await self.db.update_task(item.id, body=cleaned)
                                    item.body = cleaned
                            # Auto-advance during pr-fix loops: the conversational
                            # gate is useful in the initial pipeline (operator
                            # eyeballs spec/verify before push) but pure friction
                            # once a PR exists — the operator already approved the
                            # original work; iterating on PR feedback shouldn't
                            # require re-approving each verify pass. Detect via
                            # task.metadata.pr_number: if set, a PR exists and
                            # we're post-initial-pipeline. spec runs before any
                            # push so this condition naturally excludes it; verify
                            # is the conversational step this matters for.
                            pr_number_set = bool(item.metadata.get("pr_number"))
                            if pr_number_set and rule_target is not None:
                                active_flow_for_pr = self._resolve_flow(item)
                                target = resolve_output_target(
                                    rule_target,
                                    info.step,
                                    active_flow_for_pr,
                                )
                                # Validate against the active flow's SM — see
                                # the rule-target branch above for the same
                                # rationale (sub-flow edges only exist in the
                                # sub-flow's SM).
                                if (item.state, target) not in active_flow_for_pr.state_machine.transitions:
                                    continue
                                if target == "blocked":
                                    auto_status: TaskStatusLiteral = "blocked"
                                    auto_current_step: str | None = info.step.name
                                elif target == "complete":
                                    auto_status = "complete"
                                    auto_current_step = None
                                else:
                                    auto_status = "working"
                                    auto_current_step = info.step.name
                                cas = await self.db.atomic_transition(
                                    item.id,
                                    from_status="working",
                                    from_state=item.state,
                                    to_state=target,
                                    to_status=auto_status,
                                    to_current_step=auto_current_step,
                                    audit_on_win=None,
                                )
                                if not cas.won:
                                    continue
                                item.state = target
                                if target != "blocked":
                                    await self._dispatch_next_step(item, feedback=result.stdout)
                                await self._cleanup_worktree_if_done(item)
                                continue
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=item.state,
                                to_status="waiting",
                                to_current_step=info.step.name,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                continue
                            continue

                        # Same value the posthook gate computed at the top of
                        # this branch — reuse it so the gate and the NEEDS_INPUT
                        # handler can never diverge on a future change to
                        # ``_extract_needs_input``.
                        question = pending_question

                        # If the step declared output rules but NONE matched,
                        # block rather than silently auto-advancing to the
                        # sequential next step.  This catches cases like a
                        # pr-fix agent that fails to emit PR_FIX_DONE: /
                        # PR_FIX_BLOCKED: — without this guard the task would
                        # fall through to whatever step happens to follow
                        # pr-fix in YAML (e.g. verify), bypassing review.
                        # ``rule_target is None`` means the rule list was
                        # evaluated and nothing matched; ``"next"`` means a
                        # rule explicitly matched with target=next, which is
                        # a successful auto-advance and should not block.
                        if info.step.rules and rule_target is None and question is None:
                            # Validate against the active flow's SM (same
                            # rationale as the agent-failure branch above) —
                            # a sub-flow-only step's ``(active_state,
                            # "blocked")`` edge may not exist in main's SM.
                            active_flow_for_unmatched = self._resolve_flow(item)
                            if (item.state, "blocked") not in active_flow_for_unmatched.state_machine.transitions:
                                logger.warning(
                                    "drainer: task=%s step=%s emitted no recognized marker and no (%s -> blocked) "
                                    "edge — cannot block, task left status=working",
                                    item.id,
                                    info.step.name,
                                    item.state,
                                )
                                continue
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state="blocked",
                                to_status="blocked",
                                to_current_step=info.step.name,
                                audit_on_win=AuditRow(
                                    role="system",
                                    step_name=info.step.job_type,
                                    content=f"{info.step.job_type} did not emit a recognized output marker — blocking",
                                    msg_type="error",
                                ),
                            )
                            if not cas.won:
                                continue
                            item.state = "blocked"
                            # Phase 2 — pr-fix audit trail must cover the
                            # unmatched-marker block too. The drainer reaches
                            # here when the agent process succeeded
                            # (``result.success=True``) but emitted nothing
                            # matching any of the four ``PR_FIX_*`` patterns;
                            # without an explicit audit write the row is
                            # absent and the acceptance criterion (every
                            # pr-fix dispatch produces a pr_decision message)
                            # is violated. Mirror the agent-crash branch
                            # above: audit-after-CAS, before the operator-
                            # facing ``error`` message. Read the round from
                            # ``item.metadata`` — the ``_merge_task_metadata``
                            # call at the top of this branch refreshed the
                            # dict, so ``pr_fix_round_count`` is the post-
                            # increment value set by
                            # ``_dispatch_pr_fix_locked`` (``_in_flight``
                            # blocks concurrent dispatch).
                            if info.step.name == "pr-fix":
                                this_round = int(item.metadata.get("pr_fix_round_count", 0))
                                await self._record_pr_decision(
                                    item.id,
                                    decision="blocked",
                                    reasoning=f"{info.step.job_type} did not emit a recognized output marker",
                                    triggering_comment_ids=info.triggering_comment_ids,
                                    commit_sha=None,
                                    duration_ms=result.duration_ms,
                                    cost_usd=result.cost_usd,
                                    round_n=this_round,
                                )
                            continue

                        # Auto-advance non-gated steps that don't need input
                        if not info.step.evaluate and question is None:
                            # Validate against the active flow's SM. In a
                            # sub-flow, info.step.success_state reflects the
                            # sub-flow's binding order (e.g. pr_fix's review →
                            # push_pr), an edge that is registered only in the
                            # sub-flow's SM, not main's.
                            active_flow_for_auto = self._resolve_flow(item)
                            if (
                                item.state,
                                info.step.success_state,
                            ) not in active_flow_for_auto.state_machine.transitions:
                                logger.warning(
                                    "drainer: task=%s step=%s auto-advance blocked — no (%s -> %s) edge in active "
                                    "flow %s; completion dropped, task left status=working",
                                    item.id,
                                    info.step.name,
                                    item.state,
                                    info.step.success_state,
                                    active_flow_for_auto.name,
                                )
                                continue
                            # Terminal success_state (e.g. 'complete') folds the
                            # status flip into the same CAS; non-terminal
                            # success_states keep status='working' so the
                            # following _dispatch_next_step can drive the next
                            # step's CAS.
                            if info.step.success_state == "complete":
                                advance_status: TaskStatusLiteral = "complete"
                                advance_step: str | None = None
                            else:
                                advance_status = "working"
                                advance_step = info.step.name
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=info.step.success_state,
                                to_status=advance_status,
                                to_current_step=advance_step,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                continue
                            item.state = info.step.success_state
                            await self._dispatch_next_step(item)
                            await self._cleanup_worktree_if_done(item)
                        elif question is not None:
                            await self._persist_question(item.id, info.step.name, question)
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=item.state,
                                to_status="needs_input",
                                to_current_step=info.step.name,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                continue
                        else:
                            cas = await self.db.atomic_transition(
                                item.id,
                                from_status="working",
                                from_state=item.state,
                                to_state=item.state,
                                to_status="waiting",
                                to_current_step=info.step.name,
                                audit_on_win=None,
                            )
                            if not cas.won:
                                continue

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error processing completion")
                # A swallowed completion-processing error must NOT silently drop
                # the completion — that strands the task at status=working with
                # no agent in flight (a "working-orphan" that isn't retryable and
                # only a restart recovers; observed on an internal task, stuck for an extended period).
                # Best-effort block instead: make the failure visible and the
                # task retryable. Guarded on a still-``working`` row so a task
                # that already transitioned before the error isn't clobbered, and
                # wrapped so a failure here can't escape and kill the drainer loop.
                if info is not None and info.agent_result is not None:
                    try:
                        fresh = await self.db.get_task(info.item.id)
                        if fresh is not None and fresh.status == "working":
                            step_name = fresh.current_step or fresh.state
                            # Single CAS so the block can't clobber a task that
                            # transitioned between the get_task above and here
                            # (§3.1 / ADR-020), and so the audit row is written
                            # atomically with the status flip. ``to_state`` is the
                            # *current* state, not ``"blocked"``: this is an
                            # infrastructure failure (completion routing threw),
                            # so we mirror restart recovery (status→blocked, state
                            # preserved) — not the agent-exit path, which moves to
                            # the ``blocked`` SM sink and edge-checks first. A
                            # state-preserving self-transition needs no edge.
                            await self.db.atomic_transition(
                                info.item.id,
                                from_status="working",
                                from_state=fresh.state,
                                to_state=fresh.state,
                                to_status="blocked",
                                to_current_step=step_name,
                                audit_on_win=AuditRow(
                                    role="system",
                                    step_name=step_name,
                                    content="Completion processing failed — moved to blocked. Retry when ready.",
                                    msg_type="status_change",
                                ),
                            )
                    except Exception:
                        logger.exception("Failed to block task %s after completion error", info.item.id)
            finally:
                # ADR-030: apply a deferred PR terminal once the agent's own
                # routing (the try body above) has completed — the happens-before
                # the ADR requires. Runs on every exit path (the body's many
                # ``continue``s included). Skipped when no real completion was
                # drained (``get()`` cancelled, or ``agent_result is None``).
                # ``_apply_pending_terminal`` swallows its own errors so a
                # failure here never escapes the ``finally`` and kills the loop.
                if info is not None and info.agent_result is not None:
                    await self._apply_pending_terminal(info.item)

    async def _apply_pending_terminal(self, item: Item) -> None:
        """Apply a PR terminal the PrMonitor deferred for a ``working`` task (ADR-030).

        When a merge/close lands while an agent is in flight, the monitor records
        ``terminal_pending`` instead of transitioning (cancelling the agent would
        discard work mid-write). The drainer calls this after the agent's
        completion routing has run; we consume the flag from the task's monitor
        engine and complete/abandon the task. ``transition_task`` CASes from the
        task's CURRENT status, so it applies whatever state routing left it in.

        Best-effort by design: any error is logged, not raised — the widened
        discovery predicate (``list_waiting_pr_tasks``) re-polls the
        still-non-terminal task on the next cycle as the backstop. The
        ``getattr`` guard tolerates custom monitor engines that predate this hook.
        """
        try:
            engine = self._monitor_engine_for(item)
            if engine is None:
                return
            take = getattr(engine, "take_terminal_pending", None)
            if take is None:
                return
            target = take(item.id)
            if target:
                await self.transition_task(item.id, target)
        except Exception:
            logger.exception("Error applying deferred PR terminal for task %s", item.id)

    async def _persist_question(self, task_id: str, step_name: str, question: str) -> None:
        """Store the agent's NEEDS_INPUT question as a ``type='question'`` message.

        Callers in pre-CAS contexts (e.g. the pr-fix NEEDS_DECISION drainer
        branch) write this row BEFORE the status-flip CAS so the question is
        durable even if the CAS subsequently races and loses. A lost CAS
        leaves an orphaned ``type='question'`` message whose task ends in a
        different state — acceptable for the audit-first design, same
        trade-off documented in ``_record_pr_decision``.
        """
        await self.db.add_message(
            task_id,
            "agent",
            step_name,
            question,
            "question",
        )

    async def _pr_fix_round_cap_blocked(
        self,
        task_id: str,
        *,
        task_state: str,
        current_rounds: int,
        from_status: TaskStatusLiteral,
    ) -> bool:
        """Apply the ``max_pr_fix_rounds`` cap; transition to blocked if hit.

        ADR-019 Commitment 5 scopes cap *enforcement* to AUTONOMOUS dispatch
        only. The single caller is therefore:

        * ``_dispatch_pr_fix_locked`` with ``operator_initiated=False``
          (monitor-driven, ``from_status="waiting_for_pr"``) — the PrMonitor's
          autonomous re-dispatch loop, the one path the cap is designed to
          limit.

        The operator-initiated entry points (``revise()``, ``answer()``,
        ``send_message()``, ``retry()``, ``jump_to_step("pr-fix")``, and
        ``revise()``'s ``waiting_for_pr`` / ``rebasing`` routes via
        ``_dispatch_pr_fix_locked(operator_initiated=True)``) deliberately do
        NOT call this helper — operator dialogue is supervised, not autonomous,
        so it bypasses the cap by design. Those paths still increment
        ``pr_fix_round_count`` for audit completeness; only enforcement is
        scoped here. An operator who wants to reset the counter itself uses the
        ``pr_fix_budget`` override action (ADR-019 Commitment 2).

        Audit-writes the cap-fire ``pr_decision`` row BEFORE the CAS so the
        reason is durable even if the CAS subsequently races. The row reports
        ``round=current_rounds`` (pre-increment) — the round that triggered
        the block, not an unused next round.

        Returns ``True`` if the cap fired (caller must short-circuit dispatch),
        ``False`` otherwise.
        """
        if self.flow is None:
            return False
        # ADR-021: the cap and the (state, "blocked") edge come from the task's
        # OWN process. Fetch the row so config / SM resolve per-task; a missing
        # row falls back to the active process via the helpers' None handling.
        row = await self.db.get_task(task_id)
        cap_flow = self._resolve_flow(Item(id=task_id, state=task_state, metadata=row.metadata)) if row else self.flow
        pr_cfg = self._pr_monitor_config_for(row)
        cap_rounds = pr_cfg.max_pr_fix_rounds if pr_cfg else 0
        if cap_rounds <= 0 or current_rounds < cap_rounds:
            return False
        reason = f"PR-fix budget exhausted ({current_rounds}/{cap_rounds} rounds). Human review required."
        await self._record_pr_decision(
            task_id,
            decision="blocked",
            reasoning=reason,
            triggering_comment_ids=[],
            commit_sha=None,
            duration_ms=None,
            cost_usd=None,
            round_n=current_rounds,
        )
        if task_state == "blocked":
            # ``retry()`` entry point: task is already in ``state="blocked"``
            # (a previous cap-fire / agent BLOCKED outcome / push failure left
            # it here). The audit row above is durable, the cap fired, and
            # the task is already where ``to_state="blocked"`` would send it
            # — so skip the no-op CAS and the redundant
            # ``status_change("PR-fix budget exhausted…")`` write. Otherwise
            # the audit trail accumulates a duplicate status_change for every
            # retry attempt against a cap-fired task. The "no recoverable
            # path" warning branch below is reserved for misconfigured
            # custom flows that genuinely cannot transition; reaching it from
            # ``retry()`` would be misleading because the operator did
            # attempt recovery.
            pass
        elif (task_state, "blocked") in cap_flow.state_machine.transitions:
            result = await self.db.atomic_transition(
                task_id,
                from_status=from_status,
                from_state=task_state,
                to_state="blocked",
                to_status="blocked",
                to_current_step="pr-fix",
                audit_on_win=AuditRow(
                    role="system",
                    step_name="pr-fix",
                    content=reason,
                    msg_type="status_change",
                ),
            )
            if not result.won:
                # Narrow race: another writer claimed the (status, state) row
                # between the cap pre-check and this CAS. The pre-CAS
                # ``pr_decision`` row is already durable (audit-first), so
                # operators will see why the cap fired even though the
                # transition didn't land — but ``blocked`` won't take effect
                # until the next dispatch attempt re-trips the same cap.
                # Mirrors the consecutive-skip cap CAS-loss warning in the
                # drainer's SKIPPED branch.
                logger.warning(
                    "pr-fix round cap fired for task %s (rounds=%d/cap=%d) but CAS lost to a "
                    "concurrent writer; cap will fire on next dispatch attempt",
                    task_id,
                    current_rounds,
                    cap_rounds,
                )
        else:
            # Custom flows that omit the ``(<state>, "blocked")`` transition leave
            # the task stuck — the cap fires (we return True so callers
            # short-circuit dispatch) but no state change happens, so the
            # round-cap will keep firing on every future dispatch attempt with
            # no recoverable path short of a manual DB edit. Surface this in
            # logs so a custom-flow operator can spot the misconfiguration; the
            # bundled "full" flow registers the needed transitions so this
            # branch is unreachable in production.
            logger.warning(
                "pr-fix round cap fired for task %s but state machine has no (%r, 'blocked') transition; "
                "task remains in state=%r without a recoverable path",
                task_id,
                task_state,
                task_state,
            )
        return True

    async def _record_pr_decision(
        self,
        task_id: str,
        *,
        decision: Literal["done", "skipped", "needs_decision", "blocked"],
        reasoning: str,
        triggering_comment_ids: list[int],
        commit_sha: str | None,
        duration_ms: int | None,
        cost_usd: float | None,
        round_n: int,
    ) -> None:
        """Append a ``pr_decision`` row symmetric with ``pr_feedback``.

        The audit row records the agent's outcome on each pr-fix dispatch.
        Callers always pass the round number explicitly via ``round_n`` —
        the helper does NOT read it from the task row — because the
        correct round value depends on the dispatch lifecycle moment the
        row is being written:

        * Normal outcomes (DONE/SKIPPED/NEEDS_DECISION/agent-emitted
          BLOCKED) pass ``this_round`` captured at dispatch time after the
          counter increment, so the row reports the round that actually
          ran. Reading from the task row instead would race with concurrent
          ``_merge_task_metadata`` writes (e.g. PrMonitor stats polling).
        * Cap-fire paths (round-cap blocks dispatch entirely; consecutive-
          skip cap fires after a SKIPPED row was already written) pass
          ``current_rounds`` captured BEFORE the next increment — so the
          row reports the round that triggered the block, not an unused
          next round.

        ``reasoning`` format contract: a plain human-readable summary
        with the ``PR_FIX_<MARKER>:`` prefix already stripped — display
        and query callers can treat the field uniformly across
        ``decision`` values without per-type pattern-matching:

        * DONE/SKIPPED/agent-emitted BLOCKED: the drainer extracts the
          last non-empty stdout line and passes it through
          ``_strip_pr_fix_marker_prefix`` before calling this helper, so
          a stdout line of ``"PR_FIX_DONE: addressed the lint comments"``
          arrives as ``"addressed the lint comments"``.
        * NEEDS_DECISION: ``_extract_needs_decision_question`` strips
          the marker and returns just the question text (or a fallback
          placeholder when the marker has no trailing text).
        * Cap-fire paths: synthesised sentences with no marker prefix to
          strip.
        * Agent-crash path: ``err_msg`` is a system-emitted string with
          no marker (``"agent exited with code N: <stderr tail>"``).
        """
        await self.db.add_message(
            task_id,
            "agent",
            "pr-fix",
            reasoning,
            "pr_decision",
            metadata={
                "decision": decision,
                "round": round_n,
                "triggering_comment_ids": list(triggering_comment_ids),
                "commit_sha": commit_sha,
                "duration_ms": duration_ms,
                "cost_usd": cost_usd,
            },
        )

    async def _set_status(
        self,
        task_id: str,
        status: TaskStatusLiteral,
        current_step: str | None,
    ) -> None:
        """Persist ``(status, current_step)`` on the task row.

        ``status`` is annotated as ``TaskStatusLiteral`` so type-checkers
        catch typos like ``"wokring"`` at the call site rather than letting
        them silently land in the DB.
        """
        await self.db.update_task(task_id, status=status, current_step=current_step)

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _merge_task_metadata(self, item: Item, updates: dict) -> None:
        """Merge updates into the task's metadata, re-reading fresh state first.

        ``item.metadata`` is captured at dispatch time and may be stale by
        the time the drainer runs — concurrent writers (PrMonitor stats
        polling, ``_on_feedback``'s ``pr_comments_since`` write, ``_execute_push``'s
        post-push persist) can have updated the row in the meantime.  Reading
        fresh, merging, and writing avoids clobbering those updates.

        Also updates ``item.metadata`` in place so subsequent reads in the
        drainer (e.g. ``pr_fix_round_count`` lookup in the NEEDS_DECISION /
        SKIPPED branches) see the merged dict.
        """
        fresh = await self.db.get_task(item.id)
        if fresh is None:
            return
        fresh.metadata.update(updates)
        item.metadata = fresh.metadata
        await self.db.update_task(item.id, metadata=fresh.metadata)

    def _select_active_process_name(self) -> str:
        """Pick the process name the orchestrator dispatches against at startup.

        Precedence (highest wins):

        1. An explicit ``--flow-file`` was given — its loaded process is the
           active one regardless of any inline ``default: true``. We return
           ``config.flow`` here and let ``start()`` fall through to
           ``build_process(..., process_file=...)``; the returned process
           name comes from the YAML's ``process:`` field, not ``config.flow``.
        2. An inline entry in ``lotsa.yaml``'s ``processes:`` block declares
           ``default: true`` — that name wins.
        3. ``config.flow`` — the ``--flow`` CLI flag or ``flow:`` YAML field.
           This is the path bundled presets (``chat``, ``full``, ``standard``,
           ``simple``, ``quickfix``) take.
        4. ``"chat"`` — the package default (ADR-034 §2).

        The active name is matched first against the inline catalog (so a
        user can name an inline process ``full`` to override the bundled
        one), then against the bundled set.

        ``"chat"`` doubles as the "operator didn't choose" sentinel below: the
        warning guards treat ``config.flow == "chat"`` as unset (it equals the
        package default) so the common zero-config path never warns, while an
        operator who explicitly set ``--flow full`` IS warned when an inline
        default or ``--flow-file`` outranks them.
        """
        if self.config.flow_file is not None:
            # The file path is authoritative; the name we return here is just
            # the placeholder ``build_process`` uses when it computes search
            # paths. The actual loaded process carries its own name from the
            # YAML's ``process:`` field.
            #
            # If the operator also set ``config.flow`` to a non-default value,
            # their flag is silently dropped — surface it the same way as the
            # inline-default-vs-flow conflict below. The package default
            # ``"chat"`` is treated as "unset" so we don't warn for the common
            # case of a lotsa.yaml with only ``--flow-file`` on the CLI.
            if self.config.flow and self.config.flow != "chat":
                logger.warning(
                    "``--flow-file`` outranks ``flow=%r``; drop the conflicting "
                    "--flow/--process / flow: value to silence this warning.",
                    self.config.flow,
                )
            return self.config.flow or "chat"

        # Collect every entry with ``default: true``. If multiple are present,
        # the precedence is "first dict-order entry wins" — but warn loudly
        # because the dropdown in ``list_processes_summary`` reports
        # ``is_default: true`` for every match, which would otherwise surface
        # as a silent misconfiguration ("two entries are marked default but
        # only one is actually active"). The first-wins choice itself is
        # arbitrary; the warning gives the operator the cue to fix the YAML.
        defaults = [
            name
            for name, entry in self.config.processes.items()
            if isinstance(entry, dict) and entry.get("default") is True
        ]
        if len(defaults) > 1:
            logger.warning(
                "Multiple inline processes declare ``default: true`` (%s) — using %r. "
                "Edit lotsa.yaml so exactly one entry is the default.",
                defaults,
                defaults[0],
            )
        if defaults:
            # An inline ``default: true`` outranks ``--flow``/``--process`` and
            # the YAML ``flow:`` field (see the precedence list above). When the
            # operator also set ``config.flow`` to a *different* non-default
            # value (i.e. they explicitly chose ``--flow X`` or wrote
            # ``flow: X`` in lotsa.yaml) their selection is silently dropped.
            # Surface that at startup so they aren't left wondering why their
            # flag did nothing. The bundled package default ``"chat"`` is
            # treated as "unset" — warning on it would be noisy for the common
            # case of a lotsa.yaml with only inline processes and no ``flow:``.
            if self.config.flow and self.config.flow != "chat" and self.config.flow != defaults[0]:
                logger.warning(
                    "Inline process %r declares ``default: true`` and outranks ``flow=%r``; "
                    "remove the inline default or drop the conflicting --flow/--process "
                    "/ flow: value to silence this warning.",
                    defaults[0],
                    self.config.flow,
                )
            return defaults[0]

        return self.config.flow or "chat"

    # ── Per-task process resolution (ADR-021) ───────────────────────────
    #
    # ``_process_name_for`` is the single source of truth for which catalog
    # key owns a task; ``_process_for`` and every per-process accessor below
    # are layered on it so the keying never diverges. ``self.process`` /
    # ``self.flow`` survive only as the active-process default these helpers
    # fall back to (legacy rows, stale names).

    def _process_name_for(self, item_or_row: Any) -> str:
        """Return the catalog name of the process that owns this task.

        Reads ``metadata['process_name']`` (Item or TaskRow both expose
        ``metadata``). Falls back to the active process name for tasks created
        before multi-process support, or whose recorded name no longer
        resolves (e.g. the process was removed from lotsa.yaml between
        restarts). The returned name is always a valid key into
        ``_processes`` and the per-process collections.
        """
        metadata = getattr(item_or_row, "metadata", None) or {}
        name = metadata.get("process_name")
        if name and name in self._processes:
            return name
        return self._active_process_name

    def _process_for(self, item_or_row: Any) -> Process:
        """Return the process that owns this task (see ``_process_name_for``).

        This is the canonical per-task process accessor (ADR-021): every
        routing decision resolves the task's own process here rather than
        reading the ``self.process`` singleton. Legacy rows and stale names
        fall back to the active process.
        """
        if self.process is None:
            raise RuntimeError("_process_for called before start()")
        return self._processes.get(self._process_name_for(item_or_row), self.process)

    def _root_flow_for(self, item_or_row: Any) -> FlowConfig:
        """Return the root (``main``) flow of the task's own process."""
        process = self._process_for(item_or_row)
        return process.flows.get("main") or next(iter(process.flows.values()))

    def root_flow_for(self, item_or_row: Any) -> FlowConfig:
        """Public accessor for a task's own root flow (ADR-021/027).

        The API renders each task's flow / stage bar against ITS process, not the
        server's active default — a chat task, a ``full`` task, and a task
        promoted to ``full`` must each show their own flow.
        """
        return self._root_flow_for(item_or_row)

    def _monitor_state_for(self, item_or_row: Any) -> str | None:
        """Return the monitor queue_state of the task's process (or None)."""
        return self._monitor_states_by_process.get(self._process_name_for(item_or_row))

    def _monitor_engine_for(self, item_or_row: Any) -> Any:
        """Return the monitor engine for the task's process (or None)."""
        return self._pr_monitors_by_process.get(self._process_name_for(item_or_row))

    def _pr_monitor_config_for(self, item_or_row: Any) -> PrMonitorConfig | None:
        """Return the pr_monitor config for the task's process (or None)."""
        return self._pr_monitor_configs_by_process.get(self._process_name_for(item_or_row))

    def _resolve_flow(self, item: Item) -> FlowConfig:
        """Return the flow currently driving ``item``'s dispatch.

        ADR-014 Layer A / ADR-021: a task can be running inside a sub-flow
        (``pr_fix``) whose binding rules differ from the root flow (``main``).
        The sub-flow's name is recorded in ``item.metadata['current_flow']``
        at sub-flow entry (``_dispatch_pr_fix_locked``) and reset at sub-flow
        exit (the SKIPPED drainer branch and ``_execute_action_step``'s
        success path into a monitor state). Resolution runs against the
        task's OWN process (``_process_for``): tasks created before
        ``current_flow`` existed, and any task whose ``current_flow`` names a
        flow that no longer exists in that process, fall back to that
        process's root flow.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        if self.process is None:
            return self.flow
        process = self._process_for(item)
        root = process.flows.get("main") or next(iter(process.flows.values()))
        name = (item.metadata or {}).get("current_flow")
        if not name:
            return root
        return process.flows.get(name) or root

    def _resolve_step_for_row(self, row: Any) -> Job | None:
        """Resolve ``row.current_step`` to its ``Job`` against the task's flow.

        Resolution order — **active flow first**, then root, then catalog:

        1. **Active flow** (``_resolve_flow`` — honours ``current_flow``).
        2. **Root flow** (``main``) — covers legacy rows with no
           ``current_flow`` and the common case where active == root.
        3. **Process catalog** (``_process_for``) — covers sub-flow-only jobs
           (e.g. ``pr-fix``) that aren't in any flow's top-level job list.

        The active-flow-first order is load-bearing. A sub-flow step's
        ``success_state`` differs from the same-named root job (pr_fix
        ``review`` → ``push_pr`` vs main ``review`` → ``verify``). Resolving
        only against root dispatches *main's* job on a sub-flow task; the
        REVIEW_PASS auto-advance then targets ``(reviewing → verify)`` — an
        edge absent from pr_fix's SM — so the completion is silently dropped
        and the task stalls at ``reviewing/working`` (an internal task's multi-day
        stall, confirmed via the drainer strand-warning).

        Every site that resolves ``current_step`` to a ``Job`` — the action
        methods that re-dispatch (``retry``/``revise``/``answer``/
        ``send_message``/``approve``) and the read paths that derive display
        attributes (``_enrich_summaries``/``get_task``) — routes through here so
        the resolution order can't drift between siblings. Returns ``None`` when
        the service isn't started or no job matches; callers decide whether that's
        a restart-from-first (``retry``), an error (the action methods), or an
        absent attribute (the read paths).
        """
        if self.flow is None:
            return None
        active_flow = self._resolve_flow(
            Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)
        )
        step = next((s for s in active_flow.jobs if s.name == row.current_step), None)
        if step is None:
            step = next((s for s in self._root_flow_for(row).jobs if s.name == row.current_step), None)
        if step is None and self.process is not None:
            step = next((s for s in self._process_for(row).jobs if s.name == row.current_step), None)
        return step

    def _find_step_for_state(self, state: str, item: Item | None = None) -> FlowStep | None:
        """Find the flow step whose queue_state matches ``state``.

        When ``item`` is supplied, the lookup runs against the item's
        currently-active flow (see ``_resolve_flow``); this is what makes
        sub-flow rule overrides (e.g. ``pr_fix.review.REVIEW_FAIL → pr-fix``)
        take effect at dispatch time. Callers that don't have an item
        (e.g. dispatch-table sanity checks) fall back to the root flow.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        flow = self._resolve_flow(item) if item is not None else self.flow
        for step in flow.steps:
            if step.queue_state == state:
                return step
        return None

    # ── Per-project worktree resolution (ADR-029) ──────────────────────

    def _worktree_manager_for(self, project: ProjectRow) -> WorktreeManager:
        """Return (building + caching on first use) the WorktreeManager for a
        project. Worktrees are namespaced under ``worktrees/<project_id>/``.

        Reconciles the cache against the project's resolved repo path: the
        ``default`` manager is pre-seeded from ``config.work_dir`` at
        ``__init__`` time, but an explicit ``projects: default: {path: X}`` (no
        ``work_dir:``) resolves ``default`` to ``X`` instead. A plain
        "build-on-first-use" cache would keep returning the stale pre-seed and
        silently branch tasks off ``work_dir``/CWD rather than ``X`` — the
        wrong-repo dispatch ADR-029's singleton removal set out to prevent. So
        when a cached manager's repo no longer matches the project's resolved
        path, rebuild it.
        """
        repo = Path(project.path).resolve()
        cached = self._worktree_managers.get(project.id)
        if cached is None or cached.repo != repo:
            self._worktree_managers[project.id] = WorktreeManager(repo, self.config.data_dir / "worktrees" / project.id)
        return self._worktree_managers[project.id]

    def _project_id_for(self, item_or_row: Item | TaskRow) -> str:
        """Resolve the id of the project owning a task.

        Prefers ``metadata['project_id']`` (the Item dispatch path, mirrored at
        create time), then the ``project_id`` column (TaskRow), then ``default``
        — a task row always carries a ``project_id`` column (NOT NULL), but an
        ``Item`` built without the mirrored metadata (e.g. a test using
        ``db.create_task`` directly) falls back to the ``default`` project.
        """
        metadata = getattr(item_or_row, "metadata", None) or {}
        return metadata.get("project_id") or getattr(item_or_row, "project_id", None) or "default"

    def _project_for(self, item_or_row: Item | TaskRow) -> ProjectRow:
        """Resolve the project row owning a task (raises if unknown).

        Used where the project's name/path is needed. A missed/unknown project
        must fail loudly rather than silently land a worktree in the wrong repo.
        """
        pid = self._project_id_for(item_or_row)
        project = self._projects.get(pid)
        if project is None:
            raise ProjectNotFound(f"Task {getattr(item_or_row, 'id', '?')!r} references unknown project {pid!r}.")
        return project

    def _worktree_manager_for_task(self, item_or_row: Item | TaskRow) -> WorktreeManager:
        """Resolve the WorktreeManager for a task via its project id.

        Prefers the registered project row; falls back to a manager cached by
        id (e.g. the pre-seeded ``default``) so dispatch works even before the
        project sync has run or for legacy rows without a synced project."""
        pid = self._project_id_for(item_or_row)
        project = self._projects.get(pid)
        if project is not None:
            return self._worktree_manager_for(project)
        if pid in self._worktree_managers:
            return self._worktree_managers[pid]
        raise ProjectNotFound(f"Task {getattr(item_or_row, 'id', '?')!r} references unknown project {pid!r}.")

    def _resolve_project_id(self, project_id: str | None) -> str:
        """Resolve + validate the project a new task belongs to (ADR-029).

        Explicit id: must be registered and its path must be a git repository
        (validated at create time). Omitted: ``default`` if present, else the
        sole *offered* project, else raise (the operator must pick).
        """
        if project_id is None:
            # Auto-pick scopes to currently-offered (YAML-declared) projects
            # via ``_yaml_project_ids``, NOT every DB row in ``_projects``: a
            # project removed from ``lotsa.yaml`` persists in ``_projects`` for
            # its existing tasks (ADR-029 §2 removal policy) but must never be
            # silently chosen for a NEW task. Scoping on ``_projects`` here also
            # wrongly raised when exactly one project is offered but a
            # removed-from-YAML row still lingered in the DB.
            if "default" in self._yaml_project_ids:
                resolved = "default"
            elif len(self._yaml_project_ids) == 1:
                resolved = next(iter(self._yaml_project_ids))
            else:
                raise ProjectNotFound(
                    "No project specified and no ``default`` project is registered. "
                    f"Pick one of: {sorted(self._yaml_project_ids)}."
                )
        else:
            resolved = project_id
        # Registration is the create-time check: a project is only in
        # ``_projects`` if it was git-validated by ``resolve_project_specs`` at
        # startup (explicit ``projects:`` entries) or seeded leniently from
        # ``work_dir`` (the backward-compatible ``default``). Re-checking the git
        # repo here would wrongly reject a ``default`` seeded from a non-git
        # ``work_dir`` — existing single-project deployments. The unknown-project
        # case is the only create-time rejection.
        if resolved not in self._projects:
            raise ProjectNotFound(f"Unknown project {resolved!r}. Registered: {sorted(self._projects)}.")
        return resolved

    def _fallback_work_dir(self, item_or_row: Item | TaskRow) -> Path:
        """Best-effort working directory when no per-task worktree exists.

        Resolves the task's project root (the right repo in a multi-project
        world) and falls back to ``config.work_dir`` only when the project
        cannot be resolved (legacy/edge rows)."""
        project = self._projects.get(self._project_id_for(item_or_row))
        if project is not None:
            return Path(project.path)
        return Path(self.config.work_dir)

    async def _sync_projects(self) -> None:
        """Seed/sync the ``projects`` table from ``lotsa.yaml`` at startup.

        * Validates + normalizes the ``projects:`` block (hard error on bad
          config — fail fast).
        * Removes the old flat ``worktrees/`` tree from before namespacing.
        * Upserts each declared project; on a CHANGED ``path`` removes that
          project's worktree tree and resets its non-terminal tasks so they
          rebuild a fresh worktree off ``origin`` on next dispatch (safe — the
          branch pushed to its PR is the source of truth and
          ``WorktreeManager.create`` is idempotent).
        * Loads EVERY DB project row into ``_projects`` (a project removed from
          YAML persists and stays dispatchable; only YAML-declared ids are
          offered for new tasks — ``_yaml_project_ids``).
        """
        specs = resolve_project_specs(self.config)
        await self._cleanup_legacy_worktrees()

        for spec in specs:
            existing = await self.db.get_project(spec.id)
            if existing is not None and existing.path != str(spec.path):
                await self._relocate_project(spec.id)
            await self.db.upsert_project(spec.id, spec.name, str(spec.path))

        self._projects = {p.id: p for p in await self.db.list_projects()}
        self._yaml_project_ids = {s.id for s in specs}
        # Pre-warm the manager cache so the per-project resolution is a drop-in
        # replacement for the former singleton at every dispatch/test seam.
        for project in self._projects.values():
            self._worktree_manager_for(project)

    async def _cleanup_legacy_worktrees(self) -> None:
        """Delete pre-multi-project flat worktrees (``worktrees/<task_id>/``).

        Old-style task worktrees carry a ``.git`` entry directly under
        ``worktrees/<id>/``; namespaced project dirs (``worktrees/<project_id>/``)
        contain task subdirs instead, so they are never matched — the sweep is
        idempotent and safe to run alongside live namespaced dirs. Paired with
        the _m004 DB clean break (see migrations.py).

        The scan + ``rmtree`` walk is blocking filesystem work, so the whole
        body is offloaded to a worker thread in a single hop — it runs on the
        async startup path and must not stall the event loop (Constitution
        §2.1)."""
        await asyncio.to_thread(self._cleanup_legacy_worktrees_blocking)

    def _cleanup_legacy_worktrees_blocking(self) -> None:
        """Blocking worker for :meth:`_cleanup_legacy_worktrees`. Never call
        directly from the event loop — go through the async wrapper."""
        root = self.config.data_dir / "worktrees"
        if not root.is_dir():
            return
        for entry in root.iterdir():
            if entry.is_dir() and (entry / ".git").exists():
                shutil.rmtree(entry, ignore_errors=True)

    async def _relocate_project(self, project_id: str) -> None:
        """Handle a changed project ``path``: drop the stale worktree tree and
        reset the project's non-terminal tasks so they rebuild on next dispatch.

        A stale worktree's ``.git`` gitdir points into the OLD repo's
        ``.git/worktrees/…``; git operations there break after a move (ADR-029
        §2). Uncommitted worktree state is lost — path changes are an at-rest
        operator action.
        """
        # Offload the blocking tree delete — this runs on the async startup
        # path and must not stall the event loop (Constitution §2.1).
        await asyncio.to_thread(shutil.rmtree, self.config.data_dir / "worktrees" / project_id, ignore_errors=True)
        self._worktree_managers.pop(project_id, None)
        terminal = ("complete", "abandoned", "archived")
        for row in await self.db.list_tasks():
            if row.project_id != project_id or row.status in terminal:
                continue
            # Isolate per-row failures: a transient DB error on one task must
            # not abort the sweep (and so propagate through ``_sync_projects``
            # → ``start()`` and crash startup), leaving the remaining tasks in
            # this project un-reset. Mirrors the restart recovery sweep's
            # per-task guard in ``start()``.
            try:
                await self._set_status(row.id, "blocked", row.current_step or row.state)
                await self.db.add_message(
                    row.id,
                    "system",
                    row.current_step or row.state,
                    "Project path changed — worktree discarded; any unpushed commits are lost. "
                    "Retry restarts this task from origin/main.",
                    "status_change",
                )
            except Exception:
                logger.exception("project relocate reset failed for task %s; continuing sweep", row.id)

    def list_projects_summary(self) -> list[dict[str, Any]]:
        """Offerable projects for the new-task picker (ADR-029).

        Only YAML-declared projects are offered for NEW tasks; projects removed
        from YAML persist for dispatch but are not listed here."""
        return [
            {"id": p.id, "name": p.name, "path": p.path}
            for p in self._projects.values()
            if p.id in self._yaml_project_ids
        ]

    async def _cleanup_worktree_if_done(self, item: Item) -> None:
        """Remove worktree if task reached a terminal state (complete/abandoned)."""
        if item.state in ("complete", "abandoned"):
            await self._worktree_manager_for_task(item).remove(item.id)

    def _build_system_prompt(
        self, step: FlowStep, item: Item | None = None, *, runner: AgentRunner | None = None
    ) -> str:
        """Load prompt file, prepend operational preamble.

        Loads from the task's own process registry (ADR-021) when ``item`` is
        supplied; callers without an item fall back to the active flow's
        registry.

        *runner* is the runner resolved for this dispatch (ADR-023); its
        ``dispatch_shape_prompt()`` is injected so the prompt describes the
        dispatch shape that will actually run. Callers without a resolved runner
        (e.g. direct unit tests) get the service's current ``runner``.
        """
        if self.flow is None:
            raise RuntimeError("OrchestratorService not started")
        registry = self._resolve_flow(item).registry if item is not None else self.flow.registry
        base = registry.load(f"{step.prompt_name}-system")
        # ADR-027 §3 — the chat triage step renders the loaded process catalog
        # into its prompt at dispatch time. Substitution is data-driven (the
        # ``{available_processes}`` placeholder), so any process author can opt
        # in without orchestrator changes.
        if "{available_processes}" in base:
            base = base.replace("{available_processes}", self._render_available_processes())
        # ADR-029 §6 — prompt portability: ``{lotsa_prompts_dir}`` resolves to the
        # installed bundled prompts directory so a prompt (e.g. review-system.md)
        # can address its workflow files by an absolute path that works on every
        # repo, not just the Lotsa repo. Done before the conversational return so
        # every step prompt (conversational or not) can use it.
        if "{lotsa_prompts_dir}" in base:
            base = base.replace("{lotsa_prompts_dir}", str(BUNDLED_PROMPTS))
        # ADR-029 §5 — project context for spec-stage exploration: opt-in
        # ``{project_name}`` / ``{project_path}`` placeholders, resolved from the
        # task's project so the agent explores the right repo.
        if item is not None and ("{project_name}" in base or "{project_path}" in base):
            try:
                project = self._project_for(item)
                base = base.replace("{project_name}", project.name).replace("{project_path}", str(project.path))
            except ProjectNotFound:
                pass
        # Make any stdout-marker requirement non-optional (ADR-039). Appended
        # BEFORE the conversational early-return below, because the marker steps
        # (spec, verify) are themselves conversational.
        base += _marker_requirement_footer(step.rules)
        # Skip preamble for conversational steps — it instructs file writing
        # which conflicts with spec-style prompts that say "do not create files"
        if step.conversational:
            return base
        # Runner-aware preamble (ADR-028 Phase 2): the universal preamble is
        # followed by the active runner's dispatch-shape fragment (CLI
        # one-shot vs. SDK programmatic), then the per-step prompt.
        active_runner = runner if runner is not None else self.runner
        return OPERATIONAL_PREAMBLE + "\n\n" + active_runner.dispatch_shape_prompt() + "\n\n" + base


def _build_runner(config: LotsaConfig) -> AgentRunner:
    """Build the appropriate agent runner from config.

    Selection order (ADR-028 Phase 1): an explicit ``runner`` wins (and
    overrides ``docker``), then ``docker``, then today's default CLI runner.
    A minimal global ``runner`` selector mirrors ``docker``; ADR-028 Phase 3
    later supersedes it with per-step registry resolution (ADR-023).
    """
    if config.runner is not None:
        if config.runner == "claude-agent-sdk":
            if config.docker:
                logger.warning("--runner=claude-agent-sdk overrides --docker; ignoring Docker mode.")
            from rigg import ClaudeAgentSDKRunner

            return ClaudeAgentSDKRunner(
                model=config.model,
                budget_usd=config.budget,
                max_output_tokens=config.max_output_tokens,
            )
        raise ValueError(f"Unknown runner {config.runner!r}; expected 'claude-agent-sdk' or unset.")
    if config.docker:
        from lotsa.docker_runner import DockerAgentRunner

        return DockerAgentRunner(
            image=config.docker_image,
            model=config.model,
            budget_usd=config.budget,
            max_output_tokens=config.max_output_tokens,
        )
    return ClaudeCodeRunner(
        model=config.model,
        budget_usd=config.budget,
        max_output_tokens=config.max_output_tokens,
        skip_permissions=config.skip_permissions,
    )
