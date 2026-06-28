"""Tests for the ``pr_monitor`` engine (ADR-014 Layer A).

The engine replaces the old ``PrMonitor`` class. The orchestrator
instantiates it with ``(orchestrator, monitor_state, config)`` rather
than injecting a pre-parsed ``PrConfig``. These tests pin the
constructor contract, config-parsing parity with the old
``_parse_pr_config``, and the sub-flow dispatch wiring.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot and restore the global registry state around each test.

    The registry is process-global; without isolation, a test that registers
    a tool or engine would pollute every subsequent test in the suite.
    Mirrors the fixture in ``test_registry.py`` and
    ``test_orchestrator_typed_jobs.py``. Imports the built-in tool/engine
    packages BEFORE the snapshot so restoration preserves built-ins rather
    than permanently stripping them on first restore.
    """
    import lotsa.engines  # noqa: F401
    import lotsa.tools  # noqa: F401
    from lotsa import registry as reg

    saved_tools = dict(reg._TOOLS)
    saved_engines = dict(reg._ENGINES)
    yield
    reg._TOOLS.clear()
    reg._TOOLS.update(saved_tools)
    reg._ENGINES.clear()
    reg._ENGINES.update(saved_engines)


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_pr_monitor_engine_module_exposes_engine_class():
    """``lotsa.engines.pr_monitor`` exports a ``PrMonitorEngine`` class."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    assert isinstance(PrMonitorEngine, type)


def test_pr_monitor_engine_registered_under_canonical_name():
    """Importing ``lotsa.engines`` registers the engine under ``pr_monitor``."""
    import lotsa.engines  # noqa: F401
    from lotsa.engines.pr_monitor import PrMonitorEngine
    from lotsa.registry import get_engine

    assert get_engine("pr_monitor") is PrMonitorEngine


# ---------------------------------------------------------------------------
# Constructor contract
# ---------------------------------------------------------------------------


class _StubOrchestrator:
    """Minimal stub satisfying the PrMonitorOrchestrator Protocol."""

    def __init__(self):
        self.db = None
        self.transitions: list[tuple[str, str]] = []
        self.sub_flow_calls: list[tuple[str, str, str | None]] = []
        self.waiting: list[dict] = []

    async def transition_task(self, task_id: str, target_state: str) -> None:
        self.transitions.append((task_id, target_state))

    async def dispatch_sub_flow(self, task_id: str, flow_name: str, *, feedback=None, target_job=None) -> bool:
        self.sub_flow_calls.append((task_id, flow_name, feedback))
        return True

    async def list_waiting_pr_tasks(self) -> list[dict]:
        return list(self.waiting)


def test_engine_constructor_takes_orchestrator_state_and_config():
    """``PrMonitorEngine(orchestrator, monitor_state, config)`` is the new signature."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    orch = _StubOrchestrator()
    engine = PrMonitorEngine(
        orch,
        monitor_state="wait_for_pr_signal",
        config={"poll_interval_seconds": 30, "debounce_seconds": 120},
    )
    assert engine is not None


def test_engine_exposes_untrack_method():
    """The engine exposes ``untrack(task_id)`` so the orchestrator can drop
    in-memory state when a task leaves the monitor state by a non-engine path
    (``block()``, ``jump_to_step()``)."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(_StubOrchestrator(), monitor_state="wait_for_pr_signal", config={})
    # Must not raise on an unknown task — matches old PrMonitor.untrack semantics.
    engine.untrack("never-tracked-task")


# ---------------------------------------------------------------------------
# Config parsing — parity with the old _parse_pr_config validations
# ---------------------------------------------------------------------------


def test_engine_default_triggers_match_old_pr_config():
    """Default triggers: all four signal sources, mirroring ``PrConfig``."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(_StubOrchestrator(), monitor_state="wait_for_pr_signal", config={})
    triggers = set(engine.config.triggers)
    assert triggers == {"human_comment", "bot_comment", "review_decision", "failing_check"}


def test_engine_rejects_unknown_triggers():
    """Unknown trigger values are rejected at construction time."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    with pytest.raises(ValueError, match="trigger"):
        PrMonitorEngine(
            _StubOrchestrator(),
            monitor_state="wait_for_pr_signal",
            config={"triggers": ["human_comment", "totally_made_up"]},
        )


def test_engine_rejects_negative_max_pr_fix_rounds():
    from lotsa.engines.pr_monitor import PrMonitorEngine

    with pytest.raises(ValueError, match="max_pr_fix_rounds"):
        PrMonitorEngine(
            _StubOrchestrator(),
            monitor_state="wait_for_pr_signal",
            config={"max_pr_fix_rounds": -5},
        )


def test_engine_rejects_negative_max_consecutive_skipped():
    from lotsa.engines.pr_monitor import PrMonitorEngine

    with pytest.raises(ValueError, match="max_consecutive_skipped"):
        PrMonitorEngine(
            _StubOrchestrator(),
            monitor_state="wait_for_pr_signal",
            config={"max_consecutive_skipped": -1},
        )


def test_engine_zero_caps_disable_the_cap():
    """``0`` is the documented "disable" sentinel — must parse cleanly."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(
        _StubOrchestrator(),
        monitor_state="wait_for_pr_signal",
        config={"max_pr_fix_rounds": 0, "max_consecutive_skipped": 0},
    )
    assert engine.config.max_pr_fix_rounds == 0
    assert engine.config.max_consecutive_skipped == 0


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, True, "30"])
def test_engine_rejects_non_positive_poll_interval(bad_value):
    """``poll_interval_seconds`` feeds ``asyncio.sleep()`` directly — 0 or a
    negative value is a tight busy-wait that hammers the GitHub API."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    with pytest.raises(ValueError, match="poll_interval_seconds"):
        PrMonitorEngine(
            _StubOrchestrator(),
            monitor_state="wait_for_pr_signal",
            config={"poll_interval_seconds": bad_value},
        )


@pytest.mark.parametrize("bad_value", [-1, 1.5, True, "120"])
def test_engine_rejects_invalid_debounce(bad_value):
    """Negative / non-int debounce is nonsensical; only ``>= 0`` ints allowed."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    with pytest.raises(ValueError, match="debounce_seconds"):
        PrMonitorEngine(
            _StubOrchestrator(),
            monitor_state="wait_for_pr_signal",
            config={"debounce_seconds": bad_value},
        )


def test_engine_zero_debounce_disables_debouncing():
    """Unlike ``poll_interval``, ``debounce_seconds: 0`` is the legitimate
    "act on first sighting" config — it is only compared with ``>=``."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(
        _StubOrchestrator(),
        monitor_state="wait_for_pr_signal",
        config={"debounce_seconds": 0},
    )
    assert engine.config.debounce_seconds == 0


def test_engine_config_carries_base_branch_field():
    """The ``base_branch`` field that used to live on ``PrConfig`` lives here now."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(
        _StubOrchestrator(),
        monitor_state="wait_for_pr_signal",
        config={"base_branch": "develop"},
    )
    assert engine.config.base_branch == "develop"


# ---------------------------------------------------------------------------
# Sub-flow dispatch wiring
# ---------------------------------------------------------------------------


async def test_engine_feedback_dispatches_via_sub_flow_not_dispatch_pr_fix():
    """Feedback dispatch goes through ``orchestrator.dispatch_sub_flow("pr_fix", ...)``.

    Layer A replaces the ad-hoc ``dispatch_pr_fix`` orchestrator method with
    a generic sub-flow dispatcher. The engine MUST call the new method
    rather than the old name — there is no longer a ``dispatch_pr_fix``
    bound on the orchestrator for the engine to fall back to.
    """
    from lotsa.engines.pr_monitor import PrMonitorEngine
    from lotsa.github_client import PrInfo

    orch = _StubOrchestrator()
    engine = PrMonitorEngine(orch, monitor_state="wait_for_pr_signal", config={})
    pr = PrInfo(number=7, state="open", merged=False, review_decision=None, head_sha="sha", base_branch="main")

    # Reach into the wrapped PrMonitor's private ``_on_feedback`` directly —
    # this is intentionally a test-only peek at the internal wiring. The
    # production engine exposes only its public surface (``run``, ``untrack``,
    # ``snapshot_triggering_ids``); test access lives in the test, not in a
    # forwarding shim on the production class.
    #
    # ``async def`` so pytest-asyncio (auto-mode) manages the loop. Calling
    # ``asyncio.get_event_loop().run_until_complete`` here is deprecated on
    # 3.10+ and would also close the global loop other sync tests reuse.
    await engine._monitor._on_feedback("task-id", pr, "some review feedback", since_cutoff=None)

    assert len(orch.sub_flow_calls) == 1
    task_id, flow_name, feedback = orch.sub_flow_calls[0]
    assert task_id == "task-id"
    assert flow_name == "pr_fix"
    assert "some review feedback" in (feedback or "")


def test_engine_snapshot_triggering_ids_returns_empty_for_untracked_task():
    """Untracked tasks return an empty triggering-IDs list (parity)."""
    from lotsa.engines.pr_monitor import PrMonitorEngine

    engine = PrMonitorEngine(_StubOrchestrator(), monitor_state="wait_for_pr_signal", config={})
    assert engine.snapshot_triggering_ids("never-seen") == []


async def test_engine_list_waiting_tasks_is_not_scoped_by_monitor_state():
    """ADR-030: ``_list_waiting_tasks`` returns every task, NOT just those in the
    engine's own monitor state.

    Terminal PR handling (merge/close → complete/abandon) must see PR-bearing
    tasks parked in ANY non-terminal state, so the engine can no longer drop
    rows whose ``state`` differs from its ``monitor_state`` — that would
    re-strand exactly the blocked/needs_input tasks the ADR exists to watch.
    The per-monitor-state scoping that used to live here now applies only to
    feedback dispatch, via the ``observed_status == "waiting_for_pr"`` gate in
    ``PrMonitor._poll_one``; terminal CAS is idempotent, so an unscoped list is
    safe even under a future multi-monitor topology.
    """
    from lotsa.engines.pr_monitor import PrMonitorEngine

    orch = _StubOrchestrator()
    # Same dict shape the real ``OrchestratorService.list_waiting_pr_tasks``
    # emits (now also carrying ``status``).
    orch.waiting = [
        {"id": "a", "state": "wait_for_pr_signal", "status": "waiting_for_pr", "metadata": {}},
        {"id": "b", "state": "blocked", "status": "blocked", "metadata": {}},
        {"id": "c", "state": "pr-fixing", "status": "needs_input", "metadata": {}},
    ]
    engine = PrMonitorEngine(orch, monitor_state="wait_for_pr_signal", config={})

    listed = await engine._monitor._list_waiting_tasks()

    assert [t["id"] for t in listed] == ["a", "b", "c"]


async def test_legacy_pr_monitor_without_monitor_state_returns_all_waiting():
    """The legacy direct-instantiation path (``monitor_state=None``) is unscoped.

    ``OrchestratorService`` builds ``PrMonitor`` without a monitor state in the
    pre-ADR-014 synthetic-state model; that path must keep returning every
    waiting task so existing behaviour is preserved.
    """
    from lotsa.engines.pr_monitor import PrMonitorConfig
    from lotsa.pr_monitor import PrMonitor

    orch = _StubOrchestrator()
    orch.waiting = [
        {"id": "a", "state": "wait_for_pr_signal", "metadata": {}},
        {"id": "b", "state": "some_other_monitor", "metadata": {}},
    ]
    monitor = PrMonitor(orch, PrMonitorConfig())

    all_tasks = await monitor._list_waiting_tasks()

    assert [t["id"] for t in all_tasks] == ["a", "b"]
