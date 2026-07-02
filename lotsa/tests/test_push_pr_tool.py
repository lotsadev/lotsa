"""Tests for the ``push_pr`` action tool (ADR-014 Layer A).

The tool is a thin adapter around ``lotsa.push_step.execute_push``. These
tests verify the (TaskContext, config) → ToolResult contract and the
metadata key parity with today's orchestrator-owned ``_execute_push``
side effects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot and restore the global registry state around each test.

    The registry is process-global; without isolation, a test that registers
    a tool or engine would pollute every subsequent test in the suite.
    Mirrors the fixture in ``test_registry.py`` and
    ``test_orchestrator_typed_jobs.py``. Imports the built-in tool/engine
    packages BEFORE the snapshot so restoration preserves built-ins rather
    than permanently stripping them on first restore.
    """
    import lotsa.engines  # noqa: F401
    import lotsa.tools  # noqa: F401
    from lotsa import registry as reg

    saved_tools = dict(reg._TOOLS)
    saved_engines = dict(reg._ENGINES)
    yield
    reg._TOOLS.clear()
    reg._TOOLS.update(saved_tools)
    reg._ENGINES.clear()
    reg._ENGINES.update(saved_engines)


# ---------------------------------------------------------------------------
# Helpers — minimal in-memory TaskContext + DB stub
# ---------------------------------------------------------------------------


@dataclass
class _FakeDB:
    """Bare-minimum DB stub satisfying the surface ``push_pr`` touches.

    The tool reads artifacts (spec, plan); the stub answers from an
    in-memory mapping. Anything the tool doesn't actually touch is omitted
    deliberately — adding placeholders would hide accidental new dependencies.
    """

    artifacts: dict[str, str] = field(default_factory=dict)
    messages: list[tuple] = field(default_factory=list)

    async def get_messages(self, task_id: str, msg_type: str | None = None, step_name: str | None = None):
        # Return a list of message-like objects with .content and .metadata.
        @dataclass
        class _MsgRow:
            content: str
            metadata: dict

        if msg_type == "artifact":
            return [
                _MsgRow(content=content, metadata={"artifact_name": name}) for name, content in self.artifacts.items()
            ]
        return []


def _make_ctx(worktree: Path, *, metadata: dict | None = None, artifacts: dict | None = None):
    """Construct a TaskContext for use by the push_pr tool."""
    from lotsa.tools import TaskContext

    db = _FakeDB(artifacts=artifacts or {})
    return TaskContext(
        task_id="task-001",
        worktree=worktree,
        metadata=metadata or {},
        db=db,
        process_name="software_process",
        flow_name="main",
        current_flow="main",
        last_run_step="verify",
    )


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_push_pr_tool_is_callable():
    """``lotsa.tools.push_pr`` exports an async ``push_pr`` callable."""
    from lotsa.tools.push_pr import push_pr

    assert asyncio.iscoroutinefunction(push_pr)


def test_push_pr_tool_registered_under_canonical_name():
    """Importing the tool module registers it under the name ``push_pr``."""
    import lotsa.tools  # noqa: F401 — import triggers built-in registration
    from lotsa.registry import get_tool
    from lotsa.tools.push_pr import push_pr

    assert get_tool("push_pr") is push_pr


# ---------------------------------------------------------------------------
# Success path — parity with _execute_push's DB writes and metadata keys
# ---------------------------------------------------------------------------


def test_push_pr_success_returns_tool_result_with_pr_metadata(tmp_path):
    """A successful push returns ToolResult(success=True) with PR metadata."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(
        tmp_path,
        artifacts={"spec": "Add feature X", "plan": "Step 1\nStep 2"},
    )

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (42, "https://github.com/o/r/pull/42", "o", "r")
        result = asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    assert result.success is True
    assert "42" in result.output
    # The metadata mirrors the keys _execute_push merges into task.metadata
    assert result.metadata["pr_number"] == 42
    assert result.metadata["pr_url"] == "https://github.com/o/r/pull/42"
    assert result.metadata["github_owner"] == "o"
    assert result.metadata["github_repo"] == "r"
    assert "pr_pushed_at" in result.metadata


def test_push_pr_passes_base_branch_from_config(tmp_path):
    """``config['base_branch']`` flows through to ``execute_push``."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, artifacts={"spec": "s", "plan": "p"})

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (1, "u", "o", "r")
        asyncio.get_event_loop().run_until_complete(push_pr(ctx, {"base_branch": "develop"}))

    kwargs = mock_push.call_args.kwargs
    assert kwargs["base_branch"] == "develop"


def test_push_pr_reuses_existing_pr_number_from_metadata(tmp_path):
    """When ``task.metadata.pr_number`` is set, the tool updates that PR."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(
        tmp_path,
        metadata={"pr_number": 99},
        artifacts={"spec": "s", "plan": "p"},
    )

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (99, "u", "o", "r")
        asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    kwargs = mock_push.call_args.kwargs
    assert kwargs["pr_number"] == 99


def test_push_pr_forwards_title_and_body_not_spec_plan(tmp_path):
    """The tool reads the ``pr_description`` artifact and forwards ``title``/``body``.

    PR text is now produced by the diff-driven ``pr_summary`` step; ``push_pr``
    is mechanical and no longer hands ``spec``/``plan`` to ``execute_push``.
    The end-to-end parse/fallback behaviour is covered in ``test_pr_summary.py``;
    here we pin the mechanical contract — no ``spec_content``/``plan_content``.
    """
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(
        tmp_path,
        artifacts={"pr_description": "docs: update readme\n\nClarify setup."},
    )

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (1, "u", "o", "r")
        asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    kwargs = mock_push.call_args.kwargs
    assert kwargs["title"] == "docs: update readme"
    assert "Clarify setup." in kwargs["body"]
    assert "spec_content" not in kwargs
    assert "plan_content" not in kwargs


# ---------------------------------------------------------------------------
# Failure paths — error_kind metadata is the contract revise()/retry() read
# ---------------------------------------------------------------------------


def test_push_pr_non_fast_forward_returns_failure_with_error_kind(tmp_path):
    """``NON_FAST_FORWARD: ...`` failures surface ``error_kind=non_fast_forward``."""
    from lotsa.push_step import PushError
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, artifacts={"spec": "s", "plan": "p"})

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.side_effect = PushError("NON_FAST_FORWARD: Push rejected for branch 'lotsa/x'.")
        result = asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    assert result.success is False
    assert "NON_FAST_FORWARD" in result.output
    assert result.metadata.get("error_kind") == "non_fast_forward"


def test_push_pr_no_github_returns_failure_with_no_github_error_kind(tmp_path):
    """``NO_GITHUB: ...`` failures surface ``error_kind=no_github`` — the
    contract the action dispatcher reads to park the task in
    ``awaiting_operator`` (ADR-043) instead of ``blocked``.

    Regression: pre-fix the tool folded the missing-token error into the
    generic ``push_failed`` bucket, so it was indistinguishable from a real
    push failure and the dispatcher routed it to ``blocked``.
    """
    from lotsa.push_step import PushError
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, artifacts={"spec": "s", "plan": "p"})

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.side_effect = PushError("NO_GITHUB: GITHUB_TOKEN environment variable is not set.")
        result = asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    assert result.success is False
    assert result.metadata.get("error_kind") == "no_github"


def test_push_pr_generic_push_error_returns_failure_with_error_kind(tmp_path):
    """Other ``PushError`` instances surface ``error_kind=push_failed``."""
    from lotsa.push_step import PushError
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, artifacts={"spec": "s", "plan": "p"})

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.side_effect = PushError("git push failed: permission denied")
        result = asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    assert result.success is False
    assert "permission denied" in result.output
    assert result.metadata.get("error_kind") == "push_failed"


def test_push_pr_unexpected_exception_returns_failure(tmp_path):
    """An unexpected non-PushError exception is captured as a failure result."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, artifacts={"spec": "s", "plan": "p"})

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.side_effect = RuntimeError("disk full")
        result = asyncio.get_event_loop().run_until_complete(push_pr(ctx, {}))

    assert result.success is False
    assert result.metadata.get("exception_type") == "RuntimeError"
