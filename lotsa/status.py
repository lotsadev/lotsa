"""Task status enum constants — single source of truth for the eight-state model.

The enum is exported as a class with string-valued class attributes (not
``enum.Enum``) so that JSON-serialisable values can be passed directly
to FastAPI / Pydantic without a converter, and so SQL writes don't need
``.value`` access. ``ALL_STATUSES`` is the canonical ordered tuple of
status values, mirrored by the ``TaskStatusLiteral`` Literal type.

The model is now nine-valued. Beyond the core five, the PR phase, the operator
Archive action, and the operator mark-complete escape hatch introduce
terminal-or-waiting concepts that don't fold cleanly into the core five:

- ``waiting_for_pr`` — the agent has pushed a branch and a PR is open.
  The PrMonitor polls GitHub every poll_interval_seconds. The user can
  send manual feedback (revise → triggers pr-fix) or wait passively for
  reviewers/CI signals to drive the next cycle. Distinct from ``waiting``
  because the affordances and queryability are different.
- ``awaiting_operator`` — a parked, non-terminal "awaiting you" state
  (ADR-043). An Execute run has finished the work the orchestrator can do
  automatically but needs the operator to terminally close it — the escape
  hatch for GitHub-less setups where no PR terminal signal will ever arrive.
  The operator's ``mark_complete`` action drives it (or any non-terminal
  state) to ``complete``. Survives restart untouched (it is not ``working``).
- ``abandoned`` — terminal: PR was closed without merging. Distinct
  from ``complete`` because no code shipped.
- ``archived`` — terminal: the operator stopped the task and tore down
  its workspace (worktree + ``lotsa/{task_id}`` branch removed). The
  ``tasks`` row and append-only ``messages`` log are retained for review.
  ``archived`` has no outgoing transitions — review only, never recover —
  so every action method, the restart recovery sweep, and the PR-monitor
  callback must treat it as a no-op/reject.
"""

from __future__ import annotations

from typing import Final, Literal

TaskStatusLiteral = Literal[
    "working",
    "waiting",
    "waiting_for_pr",
    "awaiting_operator",
    "needs_input",
    "blocked",
    "complete",
    "abandoned",
    "archived",
]


class TaskStatus:
    WORKING: Final[str] = "working"
    WAITING: Final[str] = "waiting"
    WAITING_FOR_PR: Final[str] = "waiting_for_pr"
    AWAITING_OPERATOR: Final[str] = "awaiting_operator"
    NEEDS_INPUT: Final[str] = "needs_input"
    BLOCKED: Final[str] = "blocked"
    COMPLETE: Final[str] = "complete"
    ABANDONED: Final[str] = "abandoned"
    ARCHIVED: Final[str] = "archived"


ALL_STATUSES: Final[tuple[str, ...]] = (
    TaskStatus.WORKING,
    TaskStatus.WAITING,
    TaskStatus.WAITING_FOR_PR,
    TaskStatus.AWAITING_OPERATOR,
    TaskStatus.NEEDS_INPUT,
    TaskStatus.BLOCKED,
    TaskStatus.COMPLETE,
    TaskStatus.ABANDONED,
    TaskStatus.ARCHIVED,
)
