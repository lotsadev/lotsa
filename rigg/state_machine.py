"""Configurable workflow state machine with guards and side effects.

Extracted from: bot/project_config.py (states), bot/orchestrator.py
set_item_status() and main() dispatch logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from rigg.models import Item


class Guard(Protocol):
    """Predicate that must pass before a transition fires."""

    def check(self, item: Item, context: dict) -> bool: ...


class SideEffect(Protocol):
    """Action triggered when a transition fires."""

    async def execute(self, item: Item, context: dict) -> None: ...


@dataclass
class TransitionRule:
    guards: list[Guard] = field(default_factory=list)
    side_effects: list[SideEffect] = field(default_factory=list)


class InvalidTransition(Exception):
    """Raised when a transition is not allowed."""


class StateMachine:
    """Configurable workflow state machine.

    States and transitions are defined at construction time.
    Guards must all pass for a transition to fire.
    Side effects run after the state has been updated.
    """

    def __init__(
        self,
        states: list[str],
        transitions: dict[tuple[str, str], TransitionRule],
        initial_state: str,
        state_field: str = "state",
    ) -> None:
        self._states = set(states)
        self._transitions = transitions
        self._initial_state = initial_state
        self._state_field = state_field

        if initial_state not in self._states:
            raise ValueError(f"initial_state '{initial_state}' not in states: {states}")

        for src, dst in transitions:
            for s in (src, dst):
                if s not in self._states:
                    raise ValueError(f"Unknown state '{s}' in transition ({src}, {dst}). Known: {states}")

    @property
    def states(self) -> set[str]:
        """All valid states."""
        return self._states

    @property
    def transitions(self) -> dict[tuple[str, str], TransitionRule]:
        """All transition rules keyed by (source, target)."""
        return self._transitions

    def has_transition(self, source: str, target: str) -> bool:
        """Check if a transition exists (ignoring guards)."""
        return (source, target) in self._transitions

    def get_state(self, item: Item) -> str:
        """Read current state from item."""
        return getattr(item, self._state_field)

    def can_transition(self, item: Item, target: str, context: dict | None = None) -> bool:
        """Check if transition is valid and all guards pass."""
        ctx = context or {}
        current = self.get_state(item)
        key = (current, target)
        rule = self._transitions.get(key)
        if rule is None:
            return False
        return all(g.check(item, ctx) for g in rule.guards)

    async def transition(self, item: Item, target: str, context: dict) -> Item:
        """Execute transition: check guards, update state, run side effects.

        Raises InvalidTransition if guards fail or transition not defined.
        """
        current = self.get_state(item)
        key = (current, target)
        rule = self._transitions.get(key)
        if rule is None:
            raise InvalidTransition(f"No transition defined from '{current}' to '{target}'")

        for guard in rule.guards:
            if not guard.check(item, context):
                raise InvalidTransition(
                    f"Transition from '{current}' to '{target}' blocked by guard {type(guard).__name__}"
                )

        setattr(item, self._state_field, target)

        for effect in rule.side_effects:
            await effect.execute(item, context)

        return item

    def available_transitions(self, item: Item, context: dict | None = None) -> list[str]:
        """List valid target states from current state."""
        ctx = context or {}
        current = self.get_state(item)
        targets = []
        for (src, dst), rule in self._transitions.items():
            if src != current:
                continue
            if all(g.check(item, ctx) for g in rule.guards):
                targets.append(dst)
        return targets
