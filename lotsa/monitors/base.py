"""The ``Monitor`` base + heartbeat record (ADR-046).

A standing/step-scoped background poller extracted from the implicit shape
that lived inside ``PrMonitor``. The base owns the *loop mechanics*:

- an interval-driven ``run()`` loop (``interval_seconds`` supplied by the
  subclass),
- exception isolation (one failed ``tick()`` is logged and skipped, never
  killing the loop; ``CancelledError`` still propagates),
- a per-tick **heartbeat** written into an optional :class:`MonitorRegistry`
  so liveness is observable (ADR-040: a rebuildable cache, not state-of-record),
- an ``aclose()`` teardown hook run in the loop's ``finally`` (e.g. to close
  pooled clients).

A subclass implements ``tick()`` (one poll cycle) + the ``interval_seconds``
property, and declares a ``name`` and a ``kind``:

- ``"standing"`` — always-on, project-scoped, gates nothing (branch-freshness).
- ``"step_scoped"`` — bound to a ``queue_state``, gates a flow edge (pr_monitor).
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from lotsa.monitors.registry import MonitorRegistry

logger = logging.getLogger(__name__)

MonitorKind = Literal["standing", "step_scoped"]


@dataclass
class MonitorHeartbeat:
    """Liveness record for one monitor.

    Timestamps are epoch seconds (``time.time()``) so ``next_due_at`` can be
    compared against wall-clock now for stall detection. ``interval_seconds``
    is populated on the first tick (a subclass's ``interval_seconds`` property
    may depend on state not yet set when the base ctor runs).
    """

    name: str
    kind: MonitorKind
    interval_seconds: float = 0.0
    last_tick_at: float | None = None
    next_due_at: float | None = None
    last_ok: bool | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class Monitor(ABC):
    """Base class for interval-driven background monitors."""

    def __init__(self, name: str, kind: MonitorKind, registry: MonitorRegistry | None = None) -> None:
        self.name = name
        self.kind = kind
        self._registry = registry
        # One heartbeat object, updated in place each tick. The registry stores
        # this reference, so its ``snapshot()`` reflects live state with no copy.
        self._heartbeat = MonitorHeartbeat(name=name, kind=kind)
        if registry is not None:
            registry.upsert(self._heartbeat)

    # ── Subclass contract ────────────────────────────────────────────────

    @property
    @abstractmethod
    def interval_seconds(self) -> float:
        """Seconds to sleep between ticks. Supplied by the subclass."""

    @abstractmethod
    async def tick(self) -> None:
        """Run one poll cycle. Exceptions are isolated by ``run()``."""

    async def aclose(self) -> None:
        """Teardown hook run once in ``run()``'s ``finally``. Default: no-op."""
        return None

    # ── Loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Async polling loop. Runs until cancelled.

        A raise from ``tick()`` degrades to a logged, skipped cycle (recorded
        on the heartbeat) rather than killing the monitor; ``CancelledError``
        propagates so shutdown can cancel the task cleanly. ``aclose()`` runs
        in the ``finally`` regardless of how the loop ends.
        """
        try:
            while True:
                self._record_tick_start()
                try:
                    await self.tick()
                    self._record_tick_ok()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — isolate one bad tick
                    logger.exception("%s: tick failed; continuing", self.name)
                    self._record_tick_failure(exc)
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            logger.debug("%s cancelled", self.name)
            raise
        finally:
            try:
                await self.aclose()
            except Exception:
                logger.exception("%s: error during aclose()", self.name)

    # ── Heartbeat bookkeeping ────────────────────────────────────────────

    def _record_tick_start(self) -> None:
        now = time.time()
        interval = self.interval_seconds
        self._heartbeat.interval_seconds = interval
        self._heartbeat.last_tick_at = now
        self._heartbeat.next_due_at = now + interval

    def _record_tick_ok(self) -> None:
        self._heartbeat.last_ok = True
        self._heartbeat.consecutive_failures = 0
        self._heartbeat.last_error = None

    def _record_tick_failure(self, exc: BaseException) -> None:
        self._heartbeat.last_ok = False
        self._heartbeat.consecutive_failures += 1
        self._heartbeat.last_error = str(exc) or repr(exc)
