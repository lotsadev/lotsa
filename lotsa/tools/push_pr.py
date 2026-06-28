"""``push_pr`` action tool — wraps the deterministic push step.

Replaces ``OrchestratorService._execute_push`` per ADR-014 Layer A. The
tool reads ``spec`` and ``plan`` artifacts, calls
``lotsa.push_step.execute_push``, and translates the outcome into a
``ToolResult`` whose ``metadata`` is what the orchestrator merges into
the task row.

``error_kind`` in the failure metadata is the contract revise()/retry()
read to route ``non_fast_forward`` failures into the ``pr_fix`` sub-flow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lotsa.push_step import PushError, build_pr_text, execute_push

if TYPE_CHECKING:
    from lotsa.tools import TaskContext, ToolResult


async def push_pr(task: TaskContext, config: dict[str, Any]) -> ToolResult:
    """Push the task branch and create or update its pull request."""
    # Local import to avoid a circular import between lotsa.tools and this module.
    from lotsa.tools import ToolResult

    pr_number = task.metadata.get("pr_number")
    base_branch = config.get("base_branch")

    # The PR title/body are generated only at creation (pr_number is None).
    # A re-push (e.g. from pr_fix) keeps the existing PR and does NOT
    # regenerate — execute_push ignores title/body when pr_number is set.
    title: str | None = None
    body: str | None = None
    if pr_number is None:
        pr_description = await task.get_artifact("pr_description") or ""
        spec = await task.get_artifact("spec") or ""
        title, body = await build_pr_text(
            work_dir=task.worktree,
            task_id=task.task_id,
            base_branch=base_branch,
            flow_name=task.flow_name,
            pr_description=pr_description,
            spec=spec,
        )

    try:
        new_pr_number, pr_url, owner, repo = await execute_push(
            work_dir=task.worktree,
            task_id=task.task_id,
            pr_number=pr_number,
            base_branch=base_branch,
            title=title,
            body=body,
        )
    except PushError as exc:
        msg = str(exc)
        kind = "non_fast_forward" if msg.startswith("NON_FAST_FORWARD:") else "push_failed"
        return ToolResult(
            success=False,
            output=msg,
            metadata={"error_kind": kind},
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            output=f"{type(exc).__name__}: {exc}",
            metadata={
                "error_kind": "exception",
                "exception_type": type(exc).__name__,
            },
        )

    return ToolResult(
        success=True,
        output=f"Pushed to PR #{new_pr_number}: {pr_url}",
        metadata={
            "pr_number": new_pr_number,
            "pr_url": pr_url,
            "github_owner": owner,
            "github_repo": repo,
            "pr_pushed_at": datetime.now(UTC).isoformat(),
        },
    )
