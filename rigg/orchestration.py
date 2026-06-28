"""Priority-based dispatch decision tree.

Extracted from: bot/orchestrator.py main() (lines 920-994).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rigg.agent_runner import AgentRunner
from rigg.blocking import BlockingProtocol
from rigg.models import AgentResult, BlockingReason, DispatchResult, Item
from rigg.state_machine import StateMachine

logger = logging.getLogger(__name__)


class ItemSource(Protocol):
    """Fetch items from any backend."""

    async def items_in_state(self, state: str) -> list[Item]: ...


@dataclass
class DispatchRule:
    """Configuration for one dispatch priority level.

    queue_state: state to look for items in
    active_state: state to transition to before running agent
    job_type: label for this type of work (e.g., "spec", "coding", "fix")
    build_prompts: function that takes an Item and returns (system_prompt, user_prompt)
    work_dir: optional function to get work directory for an item
    """

    queue_state: str
    active_state: str
    job_type: str
    build_prompts: Callable[[Item], tuple[str, str]]
    work_dir: Callable[[Item], Path] | None = None


class OrchestrationEngine:
    """Evaluate queues, pick highest-priority work, execute agent.

    Dispatch rules are evaluated in order. The first rule with items
    in its queue_state wins. Within a queue, items are sorted by
    priority (lowest value = highest priority).
    """

    def __init__(
        self,
        state_machine: StateMachine,
        item_source: ItemSource,
        agent_runner: AgentRunner,
        blocking: BlockingProtocol,
        dispatch_rules: list[DispatchRule],
    ) -> None:
        self._sm = state_machine
        self._source = item_source
        self._runner = agent_runner
        self._blocking = blocking
        self._rules = dispatch_rules

    async def dispatch(self) -> DispatchResult:
        """Single dispatch cycle. Evaluate rules in order, run first match."""
        for rule in self._rules:
            items = await self._source.items_in_state(rule.queue_state)
            if not items:
                continue

            # Pick highest priority (lowest priority value)
            items.sort(key=lambda x: x.priority)
            item = items[0]

            logger.info("Dispatching %s job for item %s", rule.job_type, item.id)

            # Transition to active state
            await self._sm.transition(item, rule.active_state, {})

            # Build prompts and run agent
            system_prompt, user_prompt = rule.build_prompts(item)
            work_dir = rule.work_dir(item) if rule.work_dir else Path(".")

            result: AgentResult = await self._runner.run(system_prompt, user_prompt, work_dir)

            if not result.success:
                logger.warning("Agent failed for item %s (exit %d)", item.id, result.return_code)
                reason = BlockingReason(
                    code="AGENT_FAILURE",
                    title=f"{rule.job_type} agent failed",
                    message=f"Exit code {result.return_code}",
                    context={"stderr": result.stderr[:500]},
                )
                await self._blocking.block(item, reason)

            return DispatchResult(job_type=rule.job_type, item=item, agent_result=result)

        logger.info("Nothing to do — all queues empty")
        return DispatchResult(job_type=None, item=None, agent_result=None)
