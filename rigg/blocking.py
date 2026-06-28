"""Structured error escalation with pluggable notification.

Extracted from: bot/orchestrator.py try/except blocks at lines 724-725,
827-828, 898-901, 956-960. Preserves the secondary failure pattern:
if notification fails after state transition, log but don't re-raise.
"""

from __future__ import annotations

import logging
from typing import Protocol

from rigg.models import BlockingReason, Item
from rigg.state_machine import StateMachine

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Pluggable notification channel."""

    async def notify(self, item_id: str, reason: BlockingReason) -> None: ...


class BlockingProtocol:
    """Transition to blocked state and notify humans.

    If notification fails after a successful state transition,
    logs the secondary failure but does not re-raise. This matches
    the bot's production pattern of nested try/except.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        notifier: Notifier,
        blocked_state: str = "blocked",
    ) -> None:
        self._state_machine = state_machine
        self._notifier = notifier
        self._blocked_state = blocked_state

    async def block(self, item: Item, reason: BlockingReason) -> None:
        """Transition to blocked state and notify."""
        await self._state_machine.transition(item, self._blocked_state, {"reason": reason})

        try:
            await self._notifier.notify(item.id, reason)
        except Exception:
            logger.exception(
                "Notification failed for item %s (reason: %s) — state already transitioned to %s",
                item.id,
                reason.code,
                self._blocked_state,
            )
