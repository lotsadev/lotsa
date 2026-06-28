"""Monitor-engine runtime (ADR-014 Layer A).

Engines drive ``type: monitor`` jobs. Each engine is constructed with
``(orchestrator, monitor_state, config)``; its ``run()`` polls or
listens; its ``untrack(task_id)`` is called by the orchestrator when a
task leaves the monitor state by a non-engine path (``block()``,
``jump_to_step()``).
"""

from __future__ import annotations

from lotsa.engines.pr_monitor import PrMonitorEngine
from lotsa.registry import is_engine_registered, register_engine

# Re-import in the same process is a no-op. Check membership via the public
# ``is_engine_registered`` probe so we only short-circuit the "already
# registered" case rather than swallowing any ValueError raised inside
# register_engine. Using the public probe (rather than importing the private
# ``_ENGINES`` dict) keeps this module decoupled from the registry's internal
# storage name.
if not is_engine_registered("pr_monitor"):
    register_engine("pr_monitor", PrMonitorEngine)

__all__ = ["PrMonitorEngine"]
