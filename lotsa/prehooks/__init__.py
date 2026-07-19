"""Prehook runtime (ADR-044 Phase 3).

Prehooks are orchestrator-run operations that fire *before* an agent/action
step dispatches — they set up the dispatch environment. Each prehook is an
``async`` callable matching the ``PrehookCallable`` signature::

    async def my_hook(ctx: TaskContext, config: dict) -> ToolResult: ...

They register themselves via ``lotsa.registry.register_prehook`` at import
time. The orchestrator runs a step's resolved prehooks (declared in the
process YAML's ``prehooks:`` field, or derived from the agent's
``needs_worktree`` property) through ``get_prehook(name)``.

The built-in ``worktree`` prehook ensures the task's git worktree exists. It
differs from the ``commit`` posthook in two ways worth calling out:

* **It *creates* the worktree rather than acting on an existing one** — so it
  can't read ``ctx.worktree`` (that path is what it's producing). It invokes
  the ``WorktreeManager`` the orchestrator injects into the prehook
  ``TaskContext`` as ``ctx.worktree_manager``. Worktree creation stays
  orchestrator-owned (ADR-013); the hook only invokes the manager.
* **Its failure is non-fatal.** Unlike a posthook failure (which blocks the
  task), a ``worktree`` prehook failure degrades to the project work_dir with
  a warning — preserving the pre-Phase-3 best-effort behaviour. The
  orchestrator's prehook runner is responsible for that fallback; this hook
  only reports ``success=False``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.tools import TaskContext, ToolResult


async def worktree_prehook(ctx: TaskContext, config: dict[str, Any]) -> ToolResult:
    """Ensure the task's git worktree exists via the injected ``WorktreeManager``.

    Returns ``success=True`` with the created path in ``metadata['worktree']``
    (informational — the orchestrator re-resolves the work_dir independently via
    ``get_path``). Returns ``success=False`` when no manager was injected or the
    create fails; the orchestrator's prehook runner treats that as non-fatal and
    falls back to the project work_dir.
    """
    from lotsa.tools import ToolResult

    manager = ctx.worktree_manager
    if manager is None:
        return ToolResult(
            success=False,
            output="worktree prehook: no WorktreeManager in context",
            metadata={"error_kind": "no_worktree_manager"},
        )
    try:
        path = await manager.create(ctx.task_id)
    except Exception as exc:  # noqa: BLE001 — any create failure degrades to the project work_dir
        return ToolResult(
            success=False,
            output=f"worktree prehook: create failed: {exc}",
            metadata={"error_kind": "worktree_create_failed"},
        )
    return ToolResult(success=True, output=f"worktree ready at {path}", metadata={"worktree": str(path)})


# ---------------------------------------------------------------------------
# Built-in prehook registration — side effect on import
# ---------------------------------------------------------------------------

from lotsa.registry import is_prehook_registered, register_prehook  # noqa: E402

# Idempotent on re-import within the same process (test isolation, hot
# reload): only register when absent so we don't trip register_prehook's
# collision guard. Uses the public membership probe rather than catching
# ValueError so genuine validation failures still surface.
if not is_prehook_registered("worktree"):
    register_prehook("worktree", worktree_prehook)

__all__ = ["worktree_prehook"]
