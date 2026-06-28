"""Tests for Rigg StateMachine."""

import pytest

from rigg.models import Item
from rigg.state_machine import (
    InvalidTransition,
    StateMachine,
    TransitionRule,
)


def make_machine():
    """Bot-like 4-state machine for testing."""
    return StateMachine(
        states=["backlog", "coding", "review", "complete", "blocked"],
        transitions={
            ("backlog", "coding"): TransitionRule(),
            ("coding", "review"): TransitionRule(),
            ("review", "complete"): TransitionRule(),
            ("review", "coding"): TransitionRule(),  # fix loop
            ("coding", "blocked"): TransitionRule(),
            ("review", "blocked"): TransitionRule(),
        },
        initial_state="backlog",
    )


def test_valid_transition():
    sm = make_machine()
    item = Item(id="1", state="backlog")
    assert sm.can_transition(item, "coding") is True


def test_invalid_transition():
    sm = make_machine()
    item = Item(id="1", state="backlog")
    assert sm.can_transition(item, "complete") is False


@pytest.mark.asyncio
async def test_transition_updates_state():
    sm = make_machine()
    item = Item(id="1", state="backlog")
    result = await sm.transition(item, "coding", {})
    assert result.state == "coding"


@pytest.mark.asyncio
async def test_transition_returns_same_item():
    sm = make_machine()
    item = Item(id="1", state="backlog")
    result = await sm.transition(item, "coding", {})
    assert result is item


@pytest.mark.asyncio
async def test_invalid_transition_raises():
    sm = make_machine()
    item = Item(id="1", state="backlog")
    with pytest.raises(InvalidTransition):
        await sm.transition(item, "complete", {})


def test_available_transitions():
    sm = make_machine()
    item = Item(id="1", state="review")
    targets = sm.available_transitions(item)
    assert set(targets) == {"complete", "coding", "blocked"}


def test_available_transitions_terminal():
    sm = make_machine()
    item = Item(id="1", state="complete")
    assert sm.available_transitions(item) == []


def test_get_state():
    sm = make_machine()
    item = Item(id="1", state="coding")
    assert sm.get_state(item) == "coding"


# --- Guard tests ---


class RequiresSpec:
    def check(self, item: Item, context: dict) -> bool:
        return bool(context.get("has_spec"))


def test_guard_blocks_transition():
    sm = StateMachine(
        states=["backlog", "coding"],
        transitions={
            ("backlog", "coding"): TransitionRule(guards=[RequiresSpec()]),
        },
        initial_state="backlog",
    )
    item = Item(id="1", state="backlog")
    assert sm.can_transition(item, "coding", {}) is False
    assert sm.can_transition(item, "coding", {"has_spec": True}) is True


@pytest.mark.asyncio
async def test_guard_blocks_transition_raises():
    sm = StateMachine(
        states=["backlog", "coding"],
        transitions={
            ("backlog", "coding"): TransitionRule(guards=[RequiresSpec()]),
        },
        initial_state="backlog",
    )
    item = Item(id="1", state="backlog")
    with pytest.raises(InvalidTransition, match="guard"):
        await sm.transition(item, "coding", {})


# --- Side effect tests ---


class TrackingSideEffect:
    def __init__(self):
        self.calls = []

    async def execute(self, item: Item, context: dict) -> None:
        self.calls.append((item.id, item.state))


@pytest.mark.asyncio
async def test_side_effects_run_after_transition():
    tracker = TrackingSideEffect()
    sm = StateMachine(
        states=["backlog", "coding"],
        transitions={
            ("backlog", "coding"): TransitionRule(side_effects=[tracker]),
        },
        initial_state="backlog",
    )
    item = Item(id="1", state="backlog")
    await sm.transition(item, "coding", {})
    assert len(tracker.calls) == 1
    # Side effect sees the NEW state (already transitioned)
    assert tracker.calls[0] == ("1", "coding")


@pytest.mark.asyncio
async def test_multiple_guards_all_must_pass():
    class AlwaysPass:
        def check(self, item, context):
            return True

    class AlwaysFail:
        def check(self, item, context):
            return False

    sm = StateMachine(
        states=["a", "b"],
        transitions={("a", "b"): TransitionRule(guards=[AlwaysPass(), AlwaysFail()])},
        initial_state="a",
    )
    item = Item(id="1", state="a")
    assert sm.can_transition(item, "b", {}) is False


def test_unknown_state_in_config():
    """Transitions referencing unknown states should raise at construction."""
    with pytest.raises(ValueError, match="Unknown state"):
        StateMachine(
            states=["a", "b"],
            transitions={("a", "c"): TransitionRule()},  # "c" not in states
            initial_state="a",
        )


def test_unknown_initial_state():
    with pytest.raises(ValueError, match="initial_state"):
        StateMachine(states=["a", "b"], transitions={}, initial_state="z")
