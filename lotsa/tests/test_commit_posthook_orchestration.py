"""Orchestrator wiring for step posthooks + the ``commit`` posthook (ADR-024).

These drive ``OrchestratorService`` end to end to pin the behaviour the spec
requires:

* A producer step's resolved posthooks run **after agent success, before the
  success-state transition**.
* A step that declares no posthooks runs none.
* A posthook failure routes the task to ``blocked`` with no retry.
* ``action`` steps never run posthooks.
* Regression (the ``8c266c53`` verify-path bug): a producer step that leaves
  an uncommitted diff results in a **clean worktree** at hand-back, with a
  deterministic commit — so the downstream push never trips the clean-tree
  guard.

Modelled on ``test_orchestrator_typed_jobs.py`` (the ``_loop``/``run`` +
custom-process pattern). Nothing here exists yet; every test is expected to
fail until the posthook plumbing and the ``commit`` posthook land.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from rigg.models import Item


@pytest.fixture()
def _loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def run(_loop):
    return _loop.run_until_complete


# The ``_isolated_registry`` autouse fixture lives in conftest.py.


@dataclass
class _FakeAgentResult:
    success: bool = True
    stdout: str = "ok"
    stderr: str = ""
    return_code: int = 0
    duration_ms: int = 1
    session_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class _FakeRunner:
    """Runner that returns a canned success result without touching the tree."""

    def __init__(self, result: _FakeAgentResult | None = None):
        self.result = result or _FakeAgentResult()

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        return self.result


class _FileWritingRunner:
    """Runner that writes a file into the worktree, modelling a producer
    that leaves an uncommitted diff (as the verify 'Fixing issues' path does)."""

    def __init__(self, filename: str, content: str = "x\n"):
        self.filename = filename
        self.content = content

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        (Path(work_dir) / self.filename).write_text(self.content)
        return _FakeAgentResult(stdout="wrote a file")


def _write_prompts(prompts_dir: Path, *names: str) -> None:
    prompts_dir.mkdir(exist_ok=True)
    for base in names:
        (prompts_dir / f"{base}-system.md").write_text(f"# {base}-system\n")
        (prompts_dir / f"{base}-user.md").write_text(f"# {base}-user\n{{title}}\n{{body}}\n")


def _build_service(tmp_path: Path, run, process_yaml: str, *, prompt_bases=("coding",)) -> OrchestratorService:
    """Construct (but do not start) an OrchestratorService for a custom process."""
    process_file = tmp_path / "process.yaml"
    process_file.write_text(process_yaml)
    prompts_dir = tmp_path / "prompts"
    _write_prompts(prompts_dir, *prompt_bases)

    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=process_file,
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    return svc


def _lay_working(svc: OrchestratorService, run, task, state: str) -> Item:
    """CAS the row to (working, <state>) so ``_dispatch_step`` can proceed."""
    run(
        svc.db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state=state,
            to_status="working",
            to_current_step=state,
        )
    )
    return Item(id=task.id, state=state, title=task.title)


def _drain(svc: OrchestratorService, run, task_id: str, timeout: float = 3.0) -> None:
    """Spin the loop until the task reaches a settled (non-``working``) status.

    The drainer pops ``_in_flight`` at the *top* of its iteration — before the
    posthook + success CAS run — so waiting on the in-flight map alone returns
    mid-posthook. The row stays at ``status='working'`` from dispatch until the
    drainer's terminal CAS, so polling for a non-``working`` status waits for
    the whole iteration (posthook included) to finish.
    """
    for _ in range(int(timeout / 0.05)):
        row = run(svc.db.get_task(task_id))
        if row is not None and row.status != "working" and svc._in_flight.get(task_id) is None:
            return
        run(asyncio.sleep(0.05))


_AGENT_ONLY_PROCESS = """
process: posthook_wiring
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [record_hook]
flows:
  main:
    steps:
      - code
"""

_AGENT_NO_POSTHOOK_PROCESS = """
process: posthook_absent
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - code
"""


def _register_recording_posthook(*, success: bool = True, output: str = "recorded"):
    """Register a ``record_hook`` posthook that captures the row state seen
    at invocation time, so a test can assert it ran BEFORE the success CAS."""
    from lotsa.registry import register_posthook
    from lotsa.tools import ToolResult

    seen: list[dict[str, Any]] = []

    async def record_hook(ctx, config):
        row = await ctx.db.get_task(ctx.task_id)
        seen.append(
            {
                "status": row.status if row else None,
                "state": row.state if row else None,
                "step_name": ctx.last_run_step,
                "config": dict(config),
            }
        )
        return ToolResult(success=success, output=output, metadata={})

    register_posthook("record_hook", record_hook)
    return seen


# ---------------------------------------------------------------------------
# Posthook runs after agent success, before the success-state transition
# ---------------------------------------------------------------------------


def test_posthook_runs_before_success_transition(tmp_path, run):
    seen = _register_recording_posthook()
    svc = _build_service(tmp_path, run, _AGENT_ONLY_PROCESS)
    svc._worktree_managers["default"].create = AsyncMock(return_value=svc.config.work_dir)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("Producer", state="coding"))
        item = _lay_working(svc, run, task, "coding")
        code_step = next(j for j in svc.flow.jobs if j.name == "code")
        run(svc._dispatch_step(item, code_step))
        _drain(svc, run, task.id)

        assert seen, "the resolved posthook must run on agent-step success"
        # Observed at invocation: still in the active state, status working —
        # i.e. the posthook ran BEFORE the success-state CAS.
        assert seen[0]["status"] == "working"
        assert seen[0]["state"] == "coding"
        assert seen[0]["step_name"] == "code"

        # ...and the task did go on to complete afterwards.
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_posthook_receives_task_title_in_config(tmp_path, run):
    """The orchestrator injects the task title into the posthook config so the
    ``commit`` posthook can build its deterministic message."""
    seen = _register_recording_posthook()
    svc = _build_service(tmp_path, run, _AGENT_ONLY_PROCESS)
    svc._worktree_managers["default"].create = AsyncMock(return_value=svc.config.work_dir)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("Title Carries", state="coding"))
        item = _lay_working(svc, run, task, "coding")
        code_step = next(j for j in svc.flow.jobs if j.name == "code")
        run(svc._dispatch_step(item, code_step))
        _drain(svc, run, task.id)

        assert seen
        assert seen[0]["config"].get("task_title") == "Title Carries"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# A step without posthooks runs none
# ---------------------------------------------------------------------------


def test_step_without_posthooks_runs_none(tmp_path, run):
    seen = _register_recording_posthook()
    svc = _build_service(tmp_path, run, _AGENT_NO_POSTHOOK_PROCESS)
    svc._worktree_managers["default"].create = AsyncMock(return_value=svc.config.work_dir)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("No Hook", state="coding"))
        item = _lay_working(svc, run, task, "coding")
        code_step = next(j for j in svc.flow.jobs if j.name == "code")
        run(svc._dispatch_step(item, code_step))
        _drain(svc, run, task.id)

        assert seen == [], "a step that declares no posthooks must run none"
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Posthook failure → blocked, no retry
# ---------------------------------------------------------------------------


def test_posthook_failure_blocks_task_without_retry(tmp_path, run):
    seen = _register_recording_posthook(success=False, output="boom: commit failed")
    svc = _build_service(tmp_path, run, _AGENT_ONLY_PROCESS)
    svc._worktree_managers["default"].create = AsyncMock(return_value=svc.config.work_dir)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("Failing Hook", state="coding"))
        item = _lay_working(svc, run, task, "coding")
        code_step = next(j for j in svc.flow.jobs if j.name == "code")
        run(svc._dispatch_step(item, code_step))
        _drain(svc, run, task.id)

        row = run(svc.db.get_task(task.id))
        # Blocked, NOT advanced to the success state.
        assert row.status == "blocked"
        assert row.state == "blocked"
        # The hook's error surfaced to the operator as the block reason.
        errs = run(svc.db.get_messages(task.id, msg_type="error"))
        assert any("boom: commit failed" in m.content for m in errs)
        # No retry — the posthook ran exactly once.
        assert len(seen) == 1
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Action steps never run posthooks
# ---------------------------------------------------------------------------


def test_action_step_does_not_run_posthooks(tmp_path, run):
    """Posthooks are an agent-step concern; an ``action`` job never runs them,
    even if one is (nonsensically) declared on it."""
    seen = _register_recording_posthook()

    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    tool_calls: list[str] = []

    async def capture_call(ctx, config):
        tool_calls.append(ctx.task_id)
        return ToolResult(success=True, output="pushed", metadata={})

    register_tool("capture_call", capture_call)

    process_yaml = """
process: action_no_posthook
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
  - name: push
    type: action
    tool: capture_call
    posthooks: [record_hook]
flows:
  main:
    steps:
      - code
      - push
"""
    svc = _build_service(tmp_path, run, process_yaml)
    svc._worktree_managers["default"].create = AsyncMock(return_value=svc.config.work_dir)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("Pushy", state="push"))
        item = _lay_working(svc, run, task, "push")
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        run(asyncio.sleep(0.1))

        assert tool_calls, "the action tool must have run for the test to be meaningful"
        assert seen == [], "action steps must not run posthooks"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Regression — producer leaves a diff → clean worktree + deterministic commit
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Lotsa Test"], path)
    (path / "README.md").write_text("initial\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "initial"], path)


def test_producer_leaving_diff_results_in_clean_worktree(tmp_path, run):
    """Regression for the ``8c266c53`` verify-path bug.

    A producer step that writes an uncommitted file (the diff is produced
    from *inside* the dispatched step, not pre-set) must leave the worktree
    **clean** when control returns to the orchestrator — closing the
    "uncommitted changes detected" class of push failure structurally.

    Observed failure against pre-fix code: with no commit posthook, the
    written file stays uncommitted; ``git status --porcelain`` is non-empty
    and a downstream ``push_pr`` raises "Uncommitted changes detected in the
    working tree." This test fails on that assertion until the ``commit``
    posthook lands.

    Regression discipline (lotsa/CLAUDE.md): the diff is produced from INSIDE
    the dispatched step (the runner writes the file), not pre-set; and the
    assertion is on the committed *tree*, not a metadata field. We do not
    import ``lotsa.posthooks`` here on purpose — pre-fix, ``build_process``
    ignores the unknown ``posthooks`` key, the step runs with no commit, and
    the failure lands on the clean-worktree assertion below (the real bug),
    not on a missing import. Post-fix, ``build_process`` registers the
    built-in ``commit`` posthook and the tree is clean.
    """
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    before = _git(["rev-parse", "HEAD"], worktree)

    process_yaml = """
process: commit_posthook_e2e
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [commit]
flows:
  main:
    steps:
      - code
"""
    svc = _build_service(tmp_path, run, process_yaml)
    # Drive the dispatched agent against the real repo and write a file there.
    svc.runner = _FileWritingRunner("generated.py", "print('generated')\n")
    svc._worktree_managers["default"].create = AsyncMock(return_value=worktree)  # type: ignore[method-assign]
    run(svc.start())
    try:
        task = run(svc.db.create_task("Add widget", state="coding"))
        item = _lay_working(svc, run, task, "coding")
        code_step = next(j for j in svc.flow.jobs if j.name == "code")
        run(svc._dispatch_step(item, code_step))
        _drain(svc, run, task.id)

        # The worktree is clean — the diff the agent wrote was committed.
        assert _git(["status", "--porcelain"], worktree) == "", (
            "producer diff must be committed by the orchestrator so the worktree "
            "is clean at push time (pre-fix: stays dirty → push fails)"
        )
        # A new commit exists with the deterministic message.
        assert _git(["rev-parse", "HEAD"], worktree) != before
        assert _git(["log", "-1", "--format=%s"], worktree) == "chore: Add widget (code)"
        # The generated file is in that commit.
        files = _git(["show", "--name-only", "--format=", "HEAD"], worktree)
        assert "generated.py" in files
    finally:
        run(svc.shutdown())
        run(svc.db.close())
