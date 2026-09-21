"""In-memory monitor-liveness registry (ADR-046).

A ``MonitorRegistry`` holds one :class:`~lotsa.monitors.base.MonitorHeartbeat`
per running monitor, keyed by the monitor's name. Monitors update their own
heartbeat object in place each tick; the registry stores the *same* object
reference, so :meth:`snapshot` reflects live state without any copy-back.

Per the ADR-040 invariant the registry is a **rebuildable cache** — it is
reconstructed from scratch every time the monitors are re-constructed at
``start()`` and is never the state of record. Nothing here is persisted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lotsa.monitors.base import MonitorHeartbeat


class MonitorRegistry:
    """A process-local store of monitor heartbeats."""

    def __init__(self) -> None:
        self._beats: dict[str, MonitorHeartbeat] = {}

    def upsert(self, heartbeat: MonitorHeartbeat) -> None:
        """Register (or replace) the heartbeat for ``heartbeat.name``."""
        self._beats[heartbeat.name] = heartbeat

    def get(self, name: str) -> MonitorHeartbeat | None:
        """Return the heartbeat for *name*, or ``None`` if unregistered."""
        return self._beats.get(name)

    def snapshot(self) -> list[MonitorHeartbeat]:
        """Return the registered heartbeats (live references, name-sorted)."""
        return [self._beats[name] for name in sorted(self._beats)]
