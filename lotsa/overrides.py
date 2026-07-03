"""Guard-override registry (ADR-019).

Lotsa raises guard conditions that demand human judgment (today, the
``max_pr_fix_rounds`` budget cap). An ``OverrideHandler`` is the operator's
first-class, audited response to one such guard: it knows how to ``detect``
whether a task is currently blocked by its guard and how to ``acknowledge``
(clear) that block, writing an audit row in the process.

This mirrors the tool / engine registry house style in ``lotsa/registry.py``
exactly: ``register_override`` raises ``ValueError`` on a duplicate name,
``get_override`` raises ``KeyError`` (with the registered set in the message)
on a miss, and built-in handlers self-register at import behind an
``is_override_registered`` guard so re-import under test isolation is a no-op.

Built-in handlers register at module import; third-party guards (Enterprise
policy violations, custom engines) register at startup the same way — the
orchestrator imports this module in ``OrchestratorService.start()`` alongside
``lotsa.engines`` / ``lotsa.tools``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lotsa.db import TaskDB, TaskRow


@runtime_checkable
class OverrideHandler(Protocol):
    """Operator-acknowledged override for a single Lotsa guard condition.

    ``guard_name`` is the stable snake_case identifier the API and UI key on;
    ``label`` / ``description`` drive the dashboard affordance.
    """

    guard_name: str
    label: str
    description: str

    async def detect(self, task: TaskRow, db: TaskDB) -> bool:
        """Return ``True`` if *task* is currently blocked by this guard."""
        ...

    async def acknowledge(self, task: TaskRow, db: TaskDB) -> None:
        """Clear the block and write the audit row.

        Performs no state-transition side effects beyond what is necessary to
        clear the specific block this guard imposes.
        """
        ...


_HANDLERS: dict[str, OverrideHandler] = {}


def register_override(handler: OverrideHandler) -> None:
    """Register *handler* under its ``guard_name``.

    Raises ``ValueError`` on collision so a duplicate import side-effect or
    typo surfaces loudly, matching ``register_tool``.
    """
    if handler.guard_name in _HANDLERS:
        raise ValueError(f"Override {handler.guard_name!r} already registered")
    _HANDLERS[handler.guard_name] = handler


def get_override(name: str) -> OverrideHandler:
    """Look up an override handler by guard name.

    Raises ``KeyError`` with the registered set in the message so the caller
    can immediately spot a typo or missing import.
    """
    if name not in _HANDLERS:
        raise KeyError(f"Override {name!r} is not registered. Registered overrides: {sorted(_HANDLERS)}")
    return _HANDLERS[name]


def is_override_registered(name: str) -> bool:
    """Return ``True`` if an override with *name* is already registered.

    Public membership probe so the built-in re-registration guards don't need
    to import the private ``_HANDLERS`` dict — symmetric to
    ``registry.is_tool_registered``.
    """
    return name in _HANDLERS


def list_overrides() -> list[str]:
    """Return the sorted list of registered guard names."""
    return sorted(_HANDLERS)


async def list_available_for(task: TaskRow, db: TaskDB) -> list[OverrideHandler]:
    """Return the registered handlers whose ``detect(task, db)`` is ``True``.

    Iterates in sorted guard-name order for deterministic output. For most
    tasks the result is empty (every handler's ``detect`` returns ``False``),
    so the per-handler cost is only paid on the task-detail fetch that calls
    this — see ADR-019 D6.
    """
    available: list[OverrideHandler] = []
    for name in sorted(_HANDLERS):
        handler = _HANDLERS[name]
        if await handler.detect(task, db):
            available.append(handler)
    return available


# ---------------------------------------------------------------------------
# Snapshot / restore (test-isolation surface)
# ---------------------------------------------------------------------------


def snapshot() -> dict[str, OverrideHandler]:
    """Capture a copy of the registry state for later ``restore()``.

    Mirrors ``registry.snapshot`` — pass the returned dict back to
    ``restore()`` unchanged. Lets a per-test fixture bracket registrations
    without reaching into the private ``_HANDLERS`` dict.
    """
    return dict(_HANDLERS)


def restore(state: dict[str, OverrideHandler]) -> None:
    """Replace the registry contents with a previously captured ``snapshot()``."""
    _HANDLERS.clear()
    _HANDLERS.update(state)


# ---------------------------------------------------------------------------
# First built-in handler — pr-fix budget cap (ADR-019 Commitment 2)
# ---------------------------------------------------------------------------

# The cap emits this reasoning at exactly one site
# (``_pr_fix_round_cap_blocked``). Substring/prefix match is acceptable for v1
# (ADR-019 D4); a structured ``block_reason`` field is a deferred follow-up.
_CAP_FIRE_PREFIX = "PR-fix budget exhausted"
# The consecutive-skip cap fires with this phrase in its block reason
# (orchestrator.py: "Agent skipped N reviewer comments in a row (cap=N) ...").
# Both caps are the pr-fix budget family, so one override covers both.
_SKIP_CAP_FIRE_MARKER = "reviewer comments in a row"


class PrFixBudgetOverride:
    """Override for the pr-fix autonomous budget caps (ADR-019).

    Covers BOTH budget caps: ``max_pr_fix_rounds`` and
    ``max_consecutive_skipped``. ``detect`` is True when the task's most recent
    ``pr_decision`` row records either cap's block. ``acknowledge`` resets BOTH
    counters (so the task doesn't immediately re-block on the other cap) and
    appends one ``pr_decision`` row with ``decision="overridden"``. The handler
    itself does NOT transition the task; ``acknowledge_override`` calls
    ``retry()`` downstream to resume the step (ADR-019 revised 2026-06-16 — the
    one-button reset-and-resume that replaced the original two-click design).
    """

    guard_name = "pr_fix_budget"
    label = "Acknowledge & continue"
    description = "Reset the PR-fix budget (round + consecutive-skip counters) so the loop can continue."

    async def detect(self, task: TaskRow, db: TaskDB) -> bool:
        rows = await db.get_messages(task.id, msg_type="pr_decision")
        if not rows:
            return False
        # ``get_messages`` returns ASC by (created_at, id) — the last row is
        # the most recent pr_decision.
        latest = rows[-1]
        if latest.metadata.get("decision") != "blocked":
            return False
        content = latest.content
        return content.startswith(_CAP_FIRE_PREFIX) or _SKIP_CAP_FIRE_MARKER in content

    async def acknowledge(self, task: TaskRow, db: TaskDB) -> None:
        # Reset the counters via an inline read-merge-write (not
        # ``_merge_task_metadata``, which takes an ``Item``). Safe because the
        # task is ``status="blocked"`` and not in-flight.
        fresh = await db.get_task(task.id)
        if fresh is None:
            return
        round_at_cap_fire = int(fresh.metadata.get("pr_fix_round_count", 0))
        # Reset BOTH pr-fix budget counters — round AND consecutive-skip — so the
        # task doesn't immediately re-block on the other cap after the operator
        # says continue (04ee0735: a round-cap override left consecutive_skipped
        # at 2, so one more skip re-blocked).
        fresh.metadata["pr_fix_round_count"] = 0
        fresh.metadata["pr_fix_consecutive_skipped"] = 0
        await db.update_task(task.id, metadata=fresh.metadata)

        # Write the audit row directly (bypassing ``_record_pr_decision`` so the
        # new ``"overridden"`` enum value isn't constrained by that helper's
        # four-value Literal). ``role="user"`` — operator action (D3). The row
        # content is bare — the operator-reason field was removed (ADR-019
        # revised 2026-07-02); rationale, when wanted, is a normal chat message.
        content = "Operator acknowledged budget cap"
        await db.add_message(
            task.id,
            "user",
            "pr-fix",
            content,
            "pr_decision",
            metadata={
                "decision": "overridden",
                "round": round_at_cap_fire,
                "triggering_comment_ids": [],
                "commit_sha": None,
                "duration_ms": None,
                "cost_usd": None,
            },
        )


# Built-in registration — side effect on import. Re-import in the same process
# (test isolation, hot reload) is a no-op via the public membership probe.
if not is_override_registered("pr_fix_budget"):
    register_override(PrFixBudgetOverride())
