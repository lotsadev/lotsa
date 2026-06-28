"""PR-monitor loop survives a poll-cycle exception instead of dying (audit finding #8)."""

from __future__ import annotations

import asyncio

import pytest

from lotsa.engines.pr_monitor import PrMonitorConfig
from lotsa.pr_monitor import PrMonitor


class _FakeOrch:
    db = None


async def test_run_swallows_poll_exception_and_continues(monkeypatch):
    cfg = PrMonitorConfig(triggers=[], poll_interval_seconds=0)
    monitor = PrMonitor(_FakeOrch(), cfg)

    calls = {"n": 0}

    async def _boom():
        calls["n"] += 1
        raise RuntimeError("poll failed")

    async def _cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "_poll_all", _boom)
    monkeypatch.setattr("lotsa.pr_monitor.asyncio.sleep", _cancel_sleep)

    # The loop must reach sleep (and be cancelled there), proving the RuntimeError
    # from _poll_all was caught and did not tear the monitor down.
    with pytest.raises(asyncio.CancelledError):
        await monitor.run()
    assert calls["n"] == 1
