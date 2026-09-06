"""Failing tests for ADR-046 — the ``Monitor`` base + heartbeat registry.

These target the new monitor framework the ADR extracts from the implicit
shape inside ``PrMonitor``:

- ``lotsa.monitors.base.Monitor`` — an interval-driven ``run()`` loop with
  exception isolation, a per-tick heartbeat record, and an ``aclose()``
  teardown hook. Subclasses implement ``tick()`` + the ``interval_seconds``
  property and declare a ``name`` + ``kind`` (``standing`` | ``step_scoped``).
- ``lotsa.monitors.registry.MonitorRegistry`` — an in-memory liveness store
  (ADR-040 invariant: a rebuildable cache, never state-of-record).
- ``lotsa.monitors.base.MonitorHeartbeat`` — the per-monitor liveness record.

Pre-fix failure shape: the ``lotsa.monitors`` package does not exist, so the
module-level import raises ``ModuleNotFoundError`` and the whole file is red —
the intended "the abstraction is unimplemented" signal.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from lotsa.monitors.base import Monitor, MonitorHeartbeat
from lotsa.monitors.registry import MonitorRegistry


class _FakeMonitor(Monitor):
    """Minimal concrete Monitor for exercising the base loop.

    Counts ticks, can be told to fail every tick, and records whether
    ``aclose()`` ran so the teardown-hook contract is observable.
    """

    def __init__(self, registry=None, *, interval=0.01, fail=False, name="fake", kind="standing"):
        super().__init__(name=name, kind=kind, registry=registry)
        self._interval = interval
        self._fail = fail
        self.ticks = 0
        self.closed = False

    @property
    def interval_seconds(self) -> float:
        return self._interval

    async def tick(self) -> None:
        self.ticks += 1
        if self._fail:
            raise RuntimeError("boom")

    async def aclose(self) -> None:
        self.closed = True


def _hb_by_name(registry: MonitorRegistry) -> dict[str, MonitorHeartbeat]:
    return {hb.name: hb for hb in registry.snapshot()}


async def _drive_until(monitor: Monitor, predicate, timeout: float = 2.0) -> asyncio.Task:
    """Spawn ``monitor.run()`` and await until *predicate* holds (then leave it running)."""
    task = asyncio.create_task(monitor.run())
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    raise TimeoutError("predicate never held")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_run_calls_tick_repeatedly_on_interval(run):
    """``run()`` drives ``tick()`` on a loop until cancelled."""

    async def _test():
        reg = MonitorRegistry()
        mon = _FakeMonitor(reg, interval=0.01)
        task = await _drive_until(mon, lambda: mon.ticks >= 3)
        await _cancel(task)
        assert mon.ticks >= 3

    run(_test())


def test_successful_tick_records_heartbeat(run):
    """A completed tick stamps ``last_tick_at`` / ``next_due_at`` and ``last_ok=True``."""

    async def _test():
        reg = MonitorRegistry()
        mon = _FakeMonitor(reg, interval=0.01, name="probe")
        task = await _drive_until(mon, lambda: mon.ticks >= 1)
        await _cancel(task)
        hb = _hb_by_name(reg)["probe"]
        assert hb.last_ok is True
        assert hb.last_tick_at is not None
        assert hb.next_due_at is not None
        assert hb.next_due_at >= hb.last_tick_at
        assert hb.consecutive_failures == 0

    run(_test())


def test_tick_exception_is_isolated_and_recorded(run):
    """A raising tick doesn't kill the loop; the failure lands on the heartbeat."""

    async def _test():
        reg = MonitorRegistry()
        mon = _FakeMonitor(reg, interval=0.01, fail=True, name="flaky")
        # The loop must keep ticking despite the exception each cycle.
        task = await _drive_until(mon, lambda: mon.ticks >= 2)
        await _cancel(task)
        hb = _hb_by_name(reg)["flaky"]
        assert hb.last_ok is False
        assert hb.consecutive_failures >= 1
        assert hb.last_error and "boom" in hb.last_error

    run(_test())


def test_cancel_propagates_and_runs_aclose(run):
    """Cancelling the loop runs ``aclose()`` in the ``finally`` and re-raises."""

    async def _test():
        reg = MonitorRegistry()
        mon = _FakeMonitor(reg, interval=0.01)
        task = await _drive_until(mon, lambda: mon.ticks >= 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mon.closed is True

    run(_test())


def test_construction_registers_heartbeat_with_kind(run):
    """A Monitor registers an initial heartbeat carrying its name + kind at construction."""

    async def _test():
        reg = MonitorRegistry()
        _FakeMonitor(reg, name="standing-one", kind="standing")
        _FakeMonitor(reg, name="step-one", kind="step_scoped")
        beats = _hb_by_name(reg)
        assert set(beats) == {"standing-one", "step-one"}
        assert beats["standing-one"].kind == "standing"
        assert beats["step-one"].kind == "step_scoped"

    run(_test())


def test_registry_none_is_tolerated(run):
    """A Monitor with no registry still runs (registry is an optional cache)."""

    async def _test():
        mon = _FakeMonitor(None, interval=0.01)
        task = await _drive_until(mon, lambda: mon.ticks >= 1)
        await _cancel(task)
        assert mon.ticks >= 1

    run(_test())
