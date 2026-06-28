"""Posthook runtime (ADR-024).

Posthooks are orchestrator-run operations that fire after an *agent* step
completes successfully, before the success-state transition. Each posthook is
an ``async`` callable matching the ``PosthookCallable`` signature::

    async def my_hook(task: TaskContext, config: dict) -> ToolResult: ...

They register themselves via ``lotsa.registry.register_posthook`` at import
time. The orchestrator runs a step's resolved posthooks (declared in the
process YAML's ``posthooks:`` field) through ``get_posthook(name)``.

The built-in ``commit`` posthook wraps ``lotsa.commit_step.execute_commit``
so producer steps no longer need to commit in prompt prose — commit becomes
mechanical and orchestrator-owned, like push.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.tools import TaskContext, ToolResult


async def commit_posthook(ctx: TaskContext, config: dict[str, Any]) -> ToolResult:
    """Commit the worktree's changes and, once a PR exists, publish them.

    Sources ``task_title`` and ``commit_prefix`` from the posthook ``config``
    (injected by the orchestrator from the task row + the job's
    ``commit_prefix`` field) and the step name from ``ctx.last_run_step``.

    A clean worktree is a no-op commit (``metadata={"noop": True}``); a
    successful commit records the SHA (``metadata={"commit_sha": ...}``); a
    git failure returns ``success=False`` with the error as output so the
    orchestrator can route the task to ``blocked``.

    **Commit-and-publish (ADR-024 addendum).** When the task already carries a
    ``pr_number``, the posthook also pushes HEAD to the PR branch in the same
    step — making "commit ⟹ the pushed PR branch reflects the worktree" an
    invariant enforced at the single place the orchestrator lands agent
    commits, rather than a separately-routable ``push_pr`` step that only the
    ``PR_FIX_DONE`` route reaches. The push fires whenever a PR exists, **not**
    only when *this* step committed, so drift stranded by an earlier round (a
    ``PR_FIX_SKIPPED`` round, or a sync-merge commit) converges on the next
    posthook run; a push of an already-current branch is a cheap remote no-op.
    Pre-PR (``pr_number`` unset) the main pipeline's ``push_pr`` still owns the
    first push and PR creation, so PR-description timing is unchanged.
    """
    # Local imports avoid a circular import (lotsa.tools <-> lotsa.posthooks)
    # and keep the heavy commit_step import off the module-load path.
    from lotsa.commit_step import CommitError, execute_commit
    from lotsa.tools import ToolResult

    try:
        result = await execute_commit(
            work_dir=ctx.worktree,
            task_id=ctx.task_id,
            task_title=config.get("task_title", "") or ctx.task_id,
            step_name=ctx.last_run_step,
            commit_prefix=config.get("commit_prefix", "chore"),
        )
    except CommitError as exc:
        return ToolResult(success=False, output=str(exc), metadata={"error_kind": "commit_failed"})

    metadata: dict[str, Any] = {}
    if result.committed:
        metadata["commit_sha"] = result.sha
        summary = f"Committed {result.sha[:8] if result.sha else '?'}: {result.message}"
    else:
        metadata["noop"] = True
        summary = "No changes to commit (clean worktree)."

    pr_number = ctx.metadata.get("pr_number")
    if pr_number is not None:
        # Publish HEAD to the existing PR branch. With ``pr_number`` set,
        # ``execute_push`` pushes by SHA and locates (does not create) the PR,
        # so ``base_branch`` is unused here.
        from lotsa.push_step import PushError, ReconcileConflict, execute_push, reconcile_branch_with_remote

        async def _publish() -> tuple[int, str, str, str]:
            return await execute_push(
                work_dir=ctx.worktree,
                task_id=ctx.task_id,
                pr_number=int(pr_number),
                base_branch=None,
            )

        try:
            _num, pr_url, _owner, _repo = await _publish()
        except PushError as exc:
            # A NON_FAST_FORWARD means the PR branch advanced underneath us —
            # typically an operator pushed to it directly (e.g. GitHub's
            # resolve-conflicts button). ADR-018 syncs to origin/main but not to
            # the branch's own remote, so reconcile against origin/<branch> and
            # retry once before blocking. A real divergence-conflict routes to
            # ``blocked`` so ``resolve_conflicts`` handles it.
            if "NON_FAST_FORWARD" not in str(exc):
                return ToolResult(
                    success=False,
                    output=f"Publish to PR #{pr_number} failed: {exc}",
                    metadata={"error_kind": "publish_failed"},
                )
            try:
                reconciled = await reconcile_branch_with_remote(ctx.worktree, ctx.task_id)
            except ReconcileConflict as rexc:
                return ToolResult(
                    success=False,
                    output=f"Publish to PR #{pr_number} blocked — PR branch diverged with conflicts: {rexc}",
                    metadata={"error_kind": "publish_conflict"},
                )
            except PushError as rexc:
                return ToolResult(
                    success=False,
                    output=f"Publish to PR #{pr_number} reconcile failed: {rexc}",
                    metadata={"error_kind": "publish_failed"},
                )
            if not reconciled:
                return ToolResult(
                    success=False,
                    output=f"Publish to PR #{pr_number} failed: {exc}",
                    metadata={"error_kind": "publish_failed"},
                )
            try:
                _num, pr_url, _owner, _repo = await _publish()
            except PushError as exc2:
                return ToolResult(
                    success=False,
                    output=f"Publish to PR #{pr_number} failed after reconcile: {exc2}",
                    metadata={"error_kind": "publish_failed"},
                )
        metadata["pr_url"] = pr_url
        summary += f" · published to PR #{pr_number}"

    return ToolResult(success=True, output=summary, metadata=metadata)


# ---------------------------------------------------------------------------
# Built-in posthook registration — side effect on import
# ---------------------------------------------------------------------------

from lotsa.registry import is_posthook_registered, register_posthook  # noqa: E402

# Idempotent on re-import within the same process (test isolation, hot
# reload): only register when absent so we don't trip register_posthook's
# collision guard. Uses the public membership probe rather than catching
# ValueError so genuine validation failures still surface.
if not is_posthook_registered("commit"):
    register_posthook("commit", commit_posthook)

__all__ = ["commit_posthook"]
