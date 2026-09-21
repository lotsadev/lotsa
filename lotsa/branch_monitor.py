"""Branch-freshness monitor — the first standing consumer of ADR-046.

A ``standing`` :class:`~lotsa.monitors.base.Monitor` (always-on, project-scoped,
gates nothing) that preemptively tracks how far each task is behind its default
branch and whether it still merges cleanly — so the dashboard can surface
freshness without a per-open network cost and offer a one-click sync.

Each tick:

1. fetches ``origin/<default>`` **once per project** (worktrees share the
   repo's object store, so one fetch updates the remote-tracking ref for every
   task worktree in that project);
2. runs a **non-mutating** probe per non-terminal worktree-bearing task
   (``git rev-list`` + ``git merge-tree --write-tree``), persisting the result
   to task metadata.

All the git logic lives on the orchestrator (ADR-013 owns git state); this
module only orchestrates the loop and talks to it through the
``BranchMonitorOrchestrator`` Protocol — mirroring how ``pr_monitor`` depends
on ``PrMonitorOrchestrator``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from lotsa.monitors.base import Monitor

if TYPE_CHECKING:
    from lotsa.monitors.registry import MonitorRegistry

logger = logging.getLogger(__name__)

# Cap concurrent per-task probes so a backlog of watched tasks can't fan out an
# unbounded number of git subprocesses at once. Mirrors the pr_monitor cap.
_MAX_PROBE_CONCURRENCY = 8


class BranchMonitorOrchestrator(Protocol):
    """Subset of ``OrchestratorService`` the branch monitor depends on.

    Typing the constructor parameter against this Protocol lets the type
    checker catch renames/removals on ``OrchestratorService`` — mirroring
    ``PrMonitorOrchestrator``.
    """

    async def list_branch_watch_project_ids(self) -> list[str]: ...
    async def fetch_project_default(self, project_id: str) -> None: ...
    async def list_branch_watch_tasks(self) -> list[dict]: ...
    async def refresh_branch_status(self, task_id: str) -> None: ...


class BranchMonitor(Monitor):
    """Standing monitor that keeps each task's branch-freshness metadata current."""

    def __init__(
        self,
        orchestrator: BranchMonitorOrchestrator,
        interval_seconds: int,
        registry: MonitorRegistry | None = None,
    ) -> None:
        super().__init__(name="branch_freshness", kind="standing", registry=registry)
        self._orchestrator = orchestrator
        self._interval = interval_seconds

    @property
    def interval_seconds(self) -> float:
        return self._interval

    async def tick(self) -> None:
        """Fetch once per project, then probe every watched task.

        A per-project fetch failure is isolated (logged + skipped) so one
        unreachable remote can't starve the probe phase; likewise a per-task
        probe failure can't abort the tick. The base ``run()`` loop wraps the
        whole tick, so an unexpected raise still degrades to a skipped cycle.
        """
        project_ids = await self._orchestrator.list_branch_watch_project_ids()
        for project_id in project_ids:
            try:
                await self._orchestrator.fetch_project_default(project_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BranchMonitor: fetch failed for project %s; continuing", project_id)

        tasks = await self._orchestrator.list_branch_watch_tasks()
        if not tasks:
            return

        sem = asyncio.Semaphore(_MAX_PROBE_CONCURRENCY)

        async def _probe(task: dict) -> None:
            task_id = task.get("id")
            if not task_id:
                return
            async with sem:
                try:
                    await self._orchestrator.refresh_branch_status(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("BranchMonitor: probe failed for task %s; continuing", task_id)

        await asyncio.gather(*(_probe(t) for t in tasks))
