"""Tests for Rigg BlockingProtocol."""

import logging

import pytest

from rigg.blocking import BlockingProtocol
from rigg.models import BlockingReason, Item
from rigg.state_machine import StateMachine, TransitionRule


class TrackingNotifier:
    def __init__(self, should_fail: bool = False):
        self.calls: list[tuple[str, BlockingReason]] = []
        self._should_fail = should_fail

    async def notify(self, item_id: str, reason: BlockingReason) -> None:
        self.calls.append((item_id, reason))
        if self._should_fail:
            raise RuntimeError("Notification failed")


def make_machine():
    return StateMachine(
        states=["coding", "blocked"],
        transitions={("coding", "blocked"): TransitionRule()},
        initial_state="coding",
    )


@pytest.mark.asyncio
async def test_block_transitions_and_notifies():
    notifier = TrackingNotifier()
    sm = make_machine()
    bp = BlockingProtocol(state_machine=sm, notifier=notifier)

    item = Item(id="42", state="coding")
    reason = BlockingReason(code="AGENT_CRASH", title="Crash", message="Exit 1", context={})

    await bp.block(item, reason)

    assert item.state == "blocked"
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "42"
    assert notifier.calls[0][1].code == "AGENT_CRASH"


@pytest.mark.asyncio
async def test_block_notification_failure_still_transitions(caplog):
    """If notification fails, state still transitions and error is logged."""
    notifier = TrackingNotifier(should_fail=True)
    sm = make_machine()
    bp = BlockingProtocol(state_machine=sm, notifier=notifier)

    item = Item(id="42", state="coding")
    reason = BlockingReason(code="TIMEOUT", title="Timeout", message="Timed out", context={})

    with caplog.at_level(logging.ERROR):
        await bp.block(item, reason)

    # State transitions even though notification failed
    assert item.state == "blocked"
    assert "Notification failed" in caplog.text


@pytest.mark.asyncio
async def test_block_custom_blocked_state():
    notifier = TrackingNotifier()
    sm = StateMachine(
        states=["coding", "error"],
        transitions={("coding", "error"): TransitionRule()},
        initial_state="coding",
    )
    bp = BlockingProtocol(state_machine=sm, notifier=notifier, blocked_state="error")

    item = Item(id="1", state="coding")
    reason = BlockingReason(code="X", title="X", message="X", context={})

    await bp.block(item, reason)
    assert item.state == "error"
