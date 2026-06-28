"""``pr_monitor`` engine — polls GitHub for PR signal changes.

Registered under the name ``pr_monitor`` in ``lotsa/engines/__init__.py``.
The orchestrator's ``start()`` looks it up via ``get_engine(job.engine)``
when loading a monitor job, so the engine instance is constructed entirely
from registry + the monitor job's typed-dataclass config — no hardcoded
import path in the orchestrator.

The polling logic itself lives in ``lotsa.pr_monitor`` (signal classification,
feedback aggregation, debounce, the polling loop). This module is a thin
wrapper that:

- parses the monitor job's YAML ``config:`` block into ``PrMonitorConfig``;
- adapts the legacy ``orchestrator.dispatch_pr_fix(task_id, feedback)``
  call into the typed-job ``dispatch_sub_flow(task_id, "pr_fix", feedback=…)``
  contract via ``_SubFlowAdapter``;
- forwards the operational surface (``run``, ``untrack``,
  ``snapshot_triggering_ids``, ``gather_pending_feedback``) to the wrapped
  poller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.pr_monitor import PrMonitorOrchestrator

logger = logging.getLogger(__name__)

_VALID_TRIGGERS = {"human_comment", "bot_comment", "review_decision", "failing_check", "merge_conflict"}


@dataclass
class PrMonitorConfig:
    """Typed config parsed from a monitor job's ``config:`` block.

    Mirrors the fields the old ``PrConfig`` carried — the rename is solely
    about ownership (the engine owns its config, not the flow).
    """

    base_branch: str | None = None
    triggers: list[str] = field(
        default_factory=lambda: ["human_comment", "bot_comment", "review_decision", "failing_check"]
    )
    poll_interval_seconds: int = 30
    debounce_seconds: int = 120
    max_pr_fix_rounds: int = 10
    max_consecutive_skipped: int = 3


def parse_config(raw: dict[str, Any]) -> PrMonitorConfig:
    """Build a ``PrMonitorConfig`` from a raw dict with validation.

    Validation mirrors the old ``_parse_pr_config`` in flows.py — the rules
    haven't changed, only where they live. Public (no leading underscore)
    because ``OrchestratorService.start()`` needs to parse a monitor job's
    ``config:`` block at startup — keeping it private and importing across
    the module boundary would leak the underscore convention.
    """
    defaults = PrMonitorConfig()

    if "completion" in raw:
        raise ValueError("pr_monitor.config.completion is no longer supported. The PR phase always completes on merge.")

    triggers = list(raw.get("triggers", defaults.triggers))
    invalid = set(triggers) - _VALID_TRIGGERS
    if invalid:
        raise ValueError(f"Invalid pr_monitor trigger(s): {invalid} (expected values from {_VALID_TRIGGERS})")

    max_rounds = raw.get("max_pr_fix_rounds", defaults.max_pr_fix_rounds)
    max_skipped = raw.get("max_consecutive_skipped", defaults.max_consecutive_skipped)
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 0:
        raise ValueError(f"max_pr_fix_rounds must be a non-negative int (got {max_rounds!r}); 0 disables the cap")
    if not isinstance(max_skipped, int) or isinstance(max_skipped, bool) or max_skipped < 0:
        raise ValueError(
            f"max_consecutive_skipped must be a non-negative int (got {max_skipped!r}); 0 disables the cap"
        )

    # ``poll_interval_seconds`` feeds ``asyncio.sleep()`` directly in the polling
    # loop, so 0 (or negative) is a tight busy-wait that hammers the GitHub API —
    # require a strictly positive int. ``debounce_seconds`` is only ever compared
    # with ``>=`` against an elapsed delta, so 0 is the legitimate "act on first
    # sighting, no debounce window" config; only a negative value is nonsensical.
    poll_interval = raw.get("poll_interval_seconds", defaults.poll_interval_seconds)
    debounce = raw.get("debounce_seconds", defaults.debounce_seconds)
    if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or poll_interval <= 0:
        raise ValueError(f"poll_interval_seconds must be a positive int (got {poll_interval!r})")
    if not isinstance(debounce, int) or isinstance(debounce, bool) or debounce < 0:
        raise ValueError(f"debounce_seconds must be a non-negative int (got {debounce!r}); 0 disables debouncing")

    return PrMonitorConfig(
        base_branch=raw.get("base_branch"),
        triggers=triggers,
        poll_interval_seconds=poll_interval,
        debounce_seconds=debounce,
        max_pr_fix_rounds=int(max_rounds),
        max_consecutive_skipped=int(max_skipped),
    )


class _SubFlowAdapter:
    """Adapter — translates monitor's ``dispatch_pr_fix`` call into
    ``dispatch_sub_flow("pr_fix", feedback=…)`` on the real orchestrator.

    The underlying ``PrMonitor`` (in lotsa.pr_monitor) calls
    ``orchestrator.dispatch_pr_fix(task_id, feedback)``; the new orchestrator
    Protocol exposes ``dispatch_sub_flow(task_id, flow_name, feedback=...)``.
    This adapter forwards every other attribute access verbatim so the
    rest of the PrMonitor surface (``transition_task``, ``db``,
    ``list_waiting_pr_tasks``) is untouched.
    """

    def __init__(self, real_orchestrator: PrMonitorOrchestrator) -> None:
        self._real = real_orchestrator

    async def dispatch_pr_fix(self, task_id: str, feedback: str) -> bool:
        return await self._real.dispatch_sub_flow(task_id, "pr_fix", feedback=feedback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class PrMonitorEngine:
    """Engine implementation registered under ``pr_monitor``.

    Wraps the existing ``lotsa.pr_monitor.PrMonitor`` polling loop. The
    polling logic itself is unchanged — only the constructor surface and
    sub-flow dispatch wiring are new.
    """

    def __init__(
        self,
        orchestrator: PrMonitorOrchestrator,
        monitor_state: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        # Local import avoids a startup cost for non-PR flows and prevents
        # any circular-import risk between flows.py and pr_monitor.py.
        from lotsa.pr_monitor import PrMonitor

        self.config = parse_config(config or {})
        self.monitor_state = monitor_state
        self._adapter = _SubFlowAdapter(orchestrator)
        # ``PrMonitor`` reads ``config.triggers`` / ``config.poll_interval_seconds``
        # / etc. as plain attribute access. ``PrMonitorConfig`` already exposes
        # those exact names so no adapter is needed — pass it through directly.
        # ``monitor_state`` scopes the poller's waiting-task query to this
        # engine's own state so two monitor jobs in one process could coexist
        # without double-dispatching. Today no bundled process declares more
        # than one monitor, so the filter is functionally a no-op — the
        # wiring is here so a multi-monitor topology becomes a pure config
        # addition rather than a code change.
        self._monitor = PrMonitor(self._adapter, self.config, monitor_state=monitor_state)

    async def run(self) -> None:
        await self._monitor.run()

    def untrack(self, task_id: str) -> None:
        self._monitor.untrack(task_id)

    def take_terminal_pending(self, task_id: str) -> str | None:
        """Forward to the wrapped poller (ADR-030).

        The orchestrator's drainer calls this on the per-process engine to
        consume a deferred terminal (``"complete"`` / ``"abandoned"``) recorded
        when a merge/close landed on a ``working`` task.
        """
        return self._monitor.take_terminal_pending(task_id)

    def snapshot_triggering_ids(self, task_id: str) -> list[int]:
        return self._monitor.snapshot_triggering_ids(task_id)

    async def gather_pending_feedback(
        self,
        task_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        token: str,
        default_since: str | None = None,
    ) -> str | None:
        """Forward to the wrapped poller.

        The orchestrator's ``revise()`` path calls this on a waiting task to
        combine the operator's feedback with any debounced-but-not-yet-
        dispatched signals the poller has buffered. Required for parity with
        the direct ``PrMonitor`` surface — without it, callers reaching into
        ``svc._pr_monitor.gather_pending_feedback`` after the engine wrapper
        is wired in would ``AttributeError``.
        """
        return await self._monitor.gather_pending_feedback(task_id, owner, repo, pr_number, token, default_since)
