"""Tests for Rigg OrchestrationEngine."""

import pytest

from rigg.blocking import BlockingProtocol
from rigg.models import AgentResult, Item
from rigg.orchestration import DispatchRule, OrchestrationEngine
from rigg.state_machine import StateMachine, TransitionRule

# --- Fake implementations for testing ---


class FakeItemSource:
    def __init__(self, items_by_state: dict[str, list[Item]]):
        self._items = items_by_state

    async def items_in_state(self, state: str) -> list[Item]:
        return self._items.get(state, [])


class FakeRunner:
    def __init__(self, result: AgentResult):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        return self._result


class FakeNotifier:
    async def notify(self, item_id, reason):
        pass


def make_simple_engine(items_by_state, agent_result=None):
    """Create an engine with a simple 3-state machine."""
    sm = StateMachine(
        states=["queue", "active", "done", "blocked"],
        transitions={
            ("queue", "active"): TransitionRule(),
            ("active", "done"): TransitionRule(),
            ("active", "blocked"): TransitionRule(),
        },
        initial_state="queue",
    )

    runner = FakeRunner(
        agent_result or AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=100)
    )

    source = FakeItemSource(items_by_state)
    notifier = FakeNotifier()
    blocking = BlockingProtocol(state_machine=sm, notifier=notifier)

    rules = [
        DispatchRule(
            queue_state="queue",
            active_state="active",
            job_type="work",
            build_prompts=lambda item: ("system", "user"),
        ),
    ]

    engine = OrchestrationEngine(
        state_machine=sm,
        item_source=source,
        agent_runner=runner,
        blocking=blocking,
        dispatch_rules=rules,
    )

    return engine, runner


@pytest.mark.asyncio
async def test_dispatch_processes_queue_item():
    engine, runner = make_simple_engine({"queue": [Item(id="1", state="queue", priority=0)]})
    result = await engine.dispatch()
    assert result.job_type == "work"
    assert result.item is not None
    assert result.item.id == "1"
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_idle_when_empty():
    engine, runner = make_simple_engine({})
    result = await engine.dispatch()
    assert result.job_type is None
    assert result.item is None
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_dispatch_picks_highest_priority():
    items = [
        Item(id="low", state="queue", priority=3),
        Item(id="high", state="queue", priority=1),
        Item(id="med", state="queue", priority=2),
    ]
    engine, runner = make_simple_engine({"queue": items})
    result = await engine.dispatch()
    assert result.item.id == "high"


@pytest.mark.asyncio
async def test_dispatch_transitions_to_active():
    item = Item(id="1", state="queue")
    engine, _ = make_simple_engine({"queue": [item]})
    await engine.dispatch()
    assert item.state == "active"


@pytest.mark.asyncio
async def test_dispatch_blocks_on_failure():
    failing_result = AgentResult(success=False, stdout="", stderr="crash", return_code=1, duration_ms=50)
    item = Item(id="1", state="queue")
    engine, _ = make_simple_engine({"queue": [item]}, agent_result=failing_result)
    result = await engine.dispatch()
    assert item.state == "blocked"
    assert result.agent_result.success is False
