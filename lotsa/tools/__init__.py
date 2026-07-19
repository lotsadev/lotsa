"""Action-tool runtime (ADR-014 Layer A).

Tools are the execution surface for ``type: action`` jobs. Each tool is an
``async`` callable matching the ``ToolCallable`` signature:

    async def my_tool(task: TaskContext, config: dict) -> ToolResult: ...

Tools register themselves via ``lotsa.registry.register_tool`` at import
time. The orchestrator calls registered tools through ``get_tool(name)``
on every ``action``-typed job dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.db import TaskDB
    from rigg.git import WorktreeManager


@dataclass
class TaskContext:
    """Context handed to a tool when an action job dispatches.

    ``metadata`` is a fresh-read snapshot at dispatch time. Tools that need
    a post-mutation view should call ``await self.db.get_task(task_id)``.
    """

    task_id: str
    worktree: Path
    metadata: dict[str, Any]
    db: TaskDB
    process_name: str
    flow_name: str  # root flow
    current_flow: str  # may differ during sub-flows
    last_run_step: str
    # ADR-044 Phase 3 — the task's WorktreeManager, injected by the orchestrator
    # only when running prehooks (a prehook may need to CREATE the worktree,
    # which doesn't exist yet). ``None`` for tools/posthooks, which operate on
    # the already-created ``worktree`` path.
    worktree_manager: WorktreeManager | None = None

    async def get_artifact(self, name: str) -> str | None:
        """Look up the latest named artifact for this task.

        Mirrors ``OrchestratorService.get_named_artifact`` so tools don't have
        to re-implement the message-iteration. Returns ``None`` if absent.
        """
        artifacts = await self.db.get_messages(self.task_id, msg_type="artifact")
        for msg in reversed(artifacts):
            if msg.metadata.get("artifact_name") == name:
                return msg.content
        return None


@dataclass
class ToolResult:
    """Outcome of a tool invocation.

    ``metadata`` is merged into the task row's ``metadata`` JSON column on
    success and is preserved on failure so ``revise()`` / ``retry()`` can
    route on ``error_kind`` (e.g. ``"non_fast_forward"`` triggers the
    pr_fix sub-flow recovery).
    """

    success: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in tool registration — side effect on import
# ---------------------------------------------------------------------------

from lotsa.registry import is_tool_registered, register_tool  # noqa: E402
from lotsa.tools.push_pr import push_pr  # noqa: E402

# Re-import in the same process (test isolation, hot reload) is a no-op.
# Check membership via the public ``is_tool_registered`` probe so we only
# swallow the "already registered" case rather than catching ValueError
# broadly (which would mask future validation failures inside register_tool).
# Using the public probe (rather than importing the private ``_TOOLS`` dict)
# keeps this module decoupled from the registry's internal storage name.
#
# Idempotency precondition: tools registered here MUST be safe to re-execute
# on a concurrent ``block()`` race — the action dispatcher merges
# ``ToolResult.metadata`` BEFORE the success/failure CAS (see
# ``orchestrator._execute_action_step`` ~line 2238 for the full analysis), so
# a CAS-loss leaves the tool's side-effect record persisted while the row
# lands at ``status=blocked``. A later ``retry()`` re-dispatches the same
# tool. ``push_pr`` is idempotent (a second ``git push`` of the same commits
# returns the same PR number). A non-idempotent tool (one that creates an
# external ticket, sends a one-shot notification, mints a token) MUST close
# that race before being registered — either by moving the metadata merge
# after the CAS, by gating ``block()`` on ``_in_flight``, or by adding an
# idempotency key the next dispatch can detect.
if not is_tool_registered("push_pr"):
    register_tool("push_pr", push_pr)

__all__ = ["TaskContext", "ToolResult", "push_pr"]
