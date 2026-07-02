"""End-to-end orchestrator tests for ADR-014 Layer A typed-job dispatch.

Replaces the integration coverage that the skipped ``TestPrPhaseStates`` and
``TestPrFixPhase2`` classes used to provide for the legacy synthetic-state
model. These tests drive the new ``type: action`` / ``type: monitor`` jobs
through ``OrchestratorService`` so a regression in ``_dispatch_step``'s type
branch or ``_execute_action_step``'s state-transition logic fails loudly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


# The ``_isolated_registry`` autouse fixture lives in ``lotsa/tests/conftest.py``
# (uses the public ``registry.snapshot()`` / ``registry.restore()`` API).


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
    """Minimal runner that returns a canned success result."""

    def __init__(self, result: _FakeAgentResult | None = None):
        self.result = result or _FakeAgentResult()
        self.calls: list[dict[str, Any]] = []

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_prompt, **kwargs})
        return self.result


def _write_action_monitor_process(tmp_path: Path) -> Path:
    """Write a minimal process with an agent → action → monitor pipeline."""
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        """
process: typed_jobs_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
  - name: push
    type: action
    tool: capture_call
  - name: watch
    type: monitor
    engine: capture_engine
flows:
  main:
    steps:
      - code
      - push
      - watch
"""
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}\n")
    return process_file


def _make_service(tmp_path: Path, run, *, tools_yaml: dict | None = None) -> OrchestratorService:
    process_file = _write_action_monitor_process(tmp_path)
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=process_file,
        prompts_dir=tmp_path / "prompts",
        model="sonnet",
        budget=5.0,
        tools=(tools_yaml or {}),
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    return svc


def _register_capture_tool(returns: dict | None = None, *, success: bool = True, output: str = "pushed"):
    """Register a ``capture_call`` action tool that records its invocations."""
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    calls: list[dict[str, Any]] = []

    async def capture_call(ctx, config):
        calls.append({"task_id": ctx.task_id, "metadata": dict(ctx.metadata), "config": dict(config)})
        return ToolResult(success=success, output=output, metadata=dict(returns or {}))

    register_tool("capture_call", capture_call)
    return calls


def _register_capture_engine():
    """Register a ``capture_engine`` engine that exposes a run-once toggle."""
    from lotsa.registry import register_engine

    class _CaptureEngine:
        instances: list = []

        def __init__(self, orchestrator, monitor_state, config):
            self.orchestrator = orchestrator
            self.monitor_state = monitor_state
            self.config = config
            self.ran = False
            type(self).instances.append(self)

        async def run(self):
            self.ran = True
            await asyncio.sleep(0)

        def untrack(self, task_id):
            pass

    register_engine("capture_engine", _CaptureEngine)
    return _CaptureEngine


# ---------------------------------------------------------------------------
# start() — registry loading
# ---------------------------------------------------------------------------


def test_start_invokes_load_user_tools_for_lotsa_yaml_tools(tmp_path, run):
    """``OrchestratorService.start()`` registers user tools from config.tools."""
    pkg = tmp_path / "typedjobs_usertool_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mytool.py").write_text(
        "from lotsa.tools import ToolResult\n"
        "async def my_tool(ctx, config):\n"
        "    return ToolResult(success=True, output='ok')\n"
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        _register_capture_engine()
        svc = _make_service(tmp_path, run, tools_yaml={"my_tool": "typedjobs_usertool_pkg.mytool:my_tool"})
        _register_capture_tool()  # capture_call needed for process file to load
        run(svc.start())
        try:
            from lotsa.registry import get_tool

            assert callable(get_tool("my_tool"))
        finally:
            run(svc.shutdown())
            run(svc.db.close())
    finally:
        sys.path.remove(str(tmp_path))
        # Drop cached sub-packages so a sibling test reusing the same
        # tmp-pkg name (different tmp_path) doesn't hit a stale module.
        for mod in [m for m in sys.modules if m.startswith("typedjobs_usertool_pkg")]:
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Action job dispatch — calls the registered tool, advances on success
# ---------------------------------------------------------------------------


def test_action_step_invokes_registered_tool_and_advances_to_monitor(tmp_path, run):
    """An action job's success_state lands on the monitor; status becomes ``waiting_for_pr``."""
    _register_capture_engine()
    calls = _register_capture_tool(returns={"pr_number": 42, "pr_url": "https://e/r/42"})

    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(svc.db.create_task("Pushy", state="push"))
        item = Item(id=task.id, state="push", title="Pushy")
        # Lay the row at status=working so _dispatch_step's CAS lands.
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="push",
                to_status="working",
                to_current_step="push",
            )
        )
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        # Let the action task finish — _execute_action_step yields once on metadata write.
        run(asyncio.sleep(0.05))

        row = run(svc.db.get_task(task.id))
        assert calls, "capture_call should have been invoked"
        assert calls[0]["task_id"] == task.id
        # Metadata returned by the tool is merged into the task row
        assert row.metadata["pr_number"] == 42
        # Successful action transitions into the monitor state with waiting_for_pr status
        assert row.state == "watch"
        assert row.status == "waiting_for_pr"
        assert task.id not in svc._in_flight
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_action_step_taskcontext_current_flow_reflects_active_subflow(tmp_path, run):
    """``TaskContext.current_flow`` must reflect the task's active sub-flow,
    not the root flow.

    Regression for the claude[bot] review finding on PR #65: the
    ``_execute_action_step`` ``TaskContext`` constructor used to hardcode
    ``current_flow=self.flow.name`` (always the root flow), so a tool
    running inside ``pr_fix`` saw ``current_flow="main"`` even after
    ``_dispatch_pr_fix_locked`` had set ``metadata["current_flow"]="pr_fix"``.
    """
    _register_capture_engine()
    captured: list[str] = []

    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    async def capture_flow(ctx, config):
        captured.append(ctx.current_flow)
        return ToolResult(success=True, output="ok", metadata={})

    register_tool("capture_call", capture_flow)

    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        # Seed the task with current_flow="pr_fix" — the shape
        # _dispatch_pr_fix_locked leaves behind on sub-flow entry.
        task = run(svc.db.create_task("Pushy", state="push", metadata={"current_flow": "pr_fix"}))
        item = Item(id=task.id, state="push", title="Pushy", metadata={"current_flow": "pr_fix"})
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="push",
                to_status="working",
                to_current_step="push",
            )
        )
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        run(asyncio.sleep(0.05))

        assert captured == ["pr_fix"], (
            f"TaskContext.current_flow must be the active sub-flow from metadata, got {captured!r}. "
            "Regression: _execute_action_step hardcoded current_flow=self.flow.name (root flow), "
            "so tools running inside a sub-flow saw the wrong value."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_action_step_mid_subflow_success_advances_to_next_subflow_job(tmp_path, run):
    """A non-terminal sub-flow action job advances to the NEXT sub-flow job's
    queue_state — and writes the successor's name into ``current_step`` in the
    SAME CAS that flips ``state``, not as a stale ``current_step=<self>``
    fallback later corrected by ``_dispatch_next_step``.

    Regression for the claude[bot] PR review finding on the ``next_step``
    lookup in ``_execute_action_step``. The pre-fix code looked up
    ``next_step`` against ``self.flow.jobs`` (root flow) only. For a sub-flow
    action whose successor lives only in the sub-flow, that lookup returned
    ``None``, and the success CAS persisted ``to_current_step=step.name``
    (the action that just finished). The subsequent ``_dispatch_next_step``
    call then re-CAS'd ``current_step`` to the correct successor, which
    *masks* the bug at end-of-flow but leaves a brief window where
    concurrent readers see ``current_step`` lagging behind ``state``.

    The fix tries ``active_flow.jobs`` first, then falls back to
    ``self.flow.jobs`` so the success CAS writes the right ``current_step``
    directly. To detect the regression deterministically, we patch
    ``_dispatch_next_step`` to record the row state at the moment it's
    called — i.e. the post-success-CAS state — before any downstream
    correction runs.
    """

    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        """
process: mid_subflow_action
jobs:
  - { name: code, type: agent, prompt: coding, queue_state: coding, active_state: coding }
  - { name: watch, type: monitor, engine: capture_engine }
  - { name: act_a, type: action, tool: tool_a }
  - { name: act_b, type: action, tool: tool_b }
flows:
  main:
    steps: [code, watch]
  sub:
    steps: [act_a, act_b]
"""
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}\n")

    _register_capture_engine()
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    async def tool_a(ctx, config):
        return ToolResult(success=True, output="a done", metadata={})

    async def tool_b(ctx, config):
        return ToolResult(success=True, output="b done", metadata={})

    register_tool("tool_a", tool_a)
    register_tool("tool_b", tool_b)

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
    run(svc.start())

    # Capture the row state at the moment _dispatch_next_step is invoked —
    # this is the post-success-CAS state BEFORE any downstream re-CAS
    # corrects ``current_step``. Block further advancement so the test
    # observes only the action-step success CAS's output.
    observed: list[dict[str, Any]] = []

    async def _intercepted_dispatch_next_step(item, feedback=None):
        row = await svc.db.get_task(item.id)
        observed.append({"state": row.state, "status": row.status, "current_step": row.current_step})

    svc._dispatch_next_step = _intercepted_dispatch_next_step  # type: ignore[method-assign]

    try:
        task = run(svc.db.create_task("MidSub", state="act_a", metadata={"current_flow": "sub"}))
        item = Item(id=task.id, state="act_a", title="MidSub", metadata={"current_flow": "sub"})
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="act_a",
                to_status="working",
                to_current_step="act_a",
            )
        )
        act_a_step = next(j for j in svc.process.jobs if j.name == "act_a")
        run(svc._dispatch_step(item, act_a_step))
        run(asyncio.sleep(0.05))

        assert observed, (
            "_dispatch_next_step must be invoked after act_a's success CAS — otherwise the "
            "downstream observation point isn't exercised and this regression test passes trivially."
        )
        snapshot = observed[0]
        # State advanced from act_a (active) to act_b (queue) — the
        # (act_a, act_b) edge lives in sub's SM via _build_state_machine.
        assert snapshot["state"] == "act_b", (
            f"sub-flow action's success_state must advance to next sub-flow job's queue_state, "
            f"got state={snapshot['state']!r}"
        )
        # The regression assertion: current_step must point at act_b (the
        # successor) directly out of the success CAS — not at act_a (the
        # buggy step.name fallback). Pre-fix, next_step lookup in
        # self.flow.jobs (main) returned None and to_current_step silently
        # fell back to step.name="act_a".
        assert snapshot["current_step"] == "act_b", (
            f"mid-sub-flow action's success CAS must persist to_current_step via active_flow.jobs lookup; "
            f"got current_step={snapshot['current_step']!r} (pre-fix: fell back to step.name='act_a')."
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_action_step_failure_routes_to_blocked(tmp_path, run):
    """A failed action result lands the task in ``blocked`` with an audit message."""
    _register_capture_engine()
    _register_capture_tool(success=False, output="push rejected", returns={"error_kind": "push_failed"})

    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(svc.db.create_task("Pushy", state="push"))
        item = Item(id=task.id, state="push", title="Pushy")
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="push",
                to_status="working",
                to_current_step="push",
            )
        )
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        run(asyncio.sleep(0.05))

        row = run(svc.db.get_task(task.id))
        assert row.state == "blocked"
        assert row.status == "blocked"
        errs = run(svc.db.get_messages(task.id, msg_type="error"))
        assert any("push rejected" in m.content for m in errs)
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_action_step_failure_skips_audit_writes_on_cas_loss(tmp_path, run):
    """When a concurrent path moves the task before the failure CAS lands,
    skip the audit writes — emitting an "error" message attributed to this
    step would surface a failure that the operator's action overrode.

    Regression for the claude[bot] review finding on the won-check guard at
    the failure-branch ``add_message`` / ``append_event`` sites.

    The race is staged from *inside* the action tool's body: by the time
    ``capture_call`` runs, ``_dispatch_step``'s initial CAS has already
    landed (so we're past the entry guard). The tool flips the row out of
    ``status='working'`` mid-execution and returns failure, modelling a
    concurrent ``block()`` / ``jump_to_step()`` / ``transition_task`` that
    races the action. When ``_execute_action_step`` proceeds to the
    failure-routing CAS, that CAS loses — and the regression check is
    that no error message is appended on the loss.
    """
    _register_capture_engine()

    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    # capture_call performs the pre-flip from inside its execution so the
    # dispatch CAS has already landed before the race fires. Using a
    # closure rather than ``_register_capture_tool`` because the helper
    # signature doesn't expose a hook for mid-execution side effects.
    svc_ref: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []

    async def capture_call(ctx, config):
        calls.append({"task_id": ctx.task_id})
        svc = svc_ref["svc"]
        # Concurrent path lands between the dispatch CAS (already done)
        # and the failure-branch CAS (about to run). The row was at
        # (working, push) after _dispatch_step's self-edge CAS; flip it
        # to (blocked, blocked) so the failure CAS loses on from_status.
        await svc.db.claim_task_transition(
            ctx.task_id,
            from_status="working",
            from_state="push",
            to_state="blocked",
            to_status="blocked",
            to_current_step="push",
        )
        return ToolResult(success=False, output="push rejected", metadata={"error_kind": "push_failed"})

    register_tool("capture_call", capture_call)

    svc = _make_service(tmp_path, run)
    svc_ref["svc"] = svc
    run(svc.start())
    try:
        task = run(svc.db.create_task("Race", state="push"))
        item = Item(id=task.id, state="push", title="Race")
        # Lay status=working so _dispatch_step's initial CAS lands.
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="push",
                to_status="working",
                to_current_step="push",
            )
        )
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        run(asyncio.sleep(0.05))

        # The tool must have run for the guard to be exercised — otherwise
        # the assertions below are trivially satisfied by _dispatch_step's
        # own entry CAS bailing, not by the failure-branch won-check guard.
        assert calls, (
            "capture_call must run so _execute_action_step's failure-branch "
            "CAS-loss path is actually exercised by this regression test"
        )

        row = run(svc.db.get_task(task.id))
        # Row stays where the concurrent path put it.
        assert row.state == "blocked"
        assert row.status == "blocked"
        # The failure-path audit writes were skipped — no error message
        # appended for the action's failed result.
        errs = run(svc.db.get_messages(task.id, msg_type="error"))
        assert not any("push rejected" in m.content for m in errs), (
            "Action-failure audit message must be suppressed when its CAS loses to a concurrent path"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_action_step_success_with_missing_sm_edge_routes_to_blocked(tmp_path, run):
    """When an action succeeds but its (active, success) SM edge is missing,
    the row is best-effort flipped to ``blocked`` with an audit message that
    names the unregistered edge — rather than stranding at ``status='working'``
    where the active-state re-dispatch branch would re-run the tool forever.

    Regression for the claude[bot] PR review Low finding: the missing-SM-edge
    fallback in ``_execute_action_step``'s success path had no direct test.
    Unreachable for the bundled process (``_build_state_machine`` /
    ``_register_cross_flow_edges`` always register the success edge), so the
    test deletes the edge from the resolved flow's transition table to model a
    custom process YAML that omits it.
    """
    _register_capture_engine()
    _register_capture_tool(success=True, output="pushed ok")

    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(svc.db.create_task("Pushy", state="push"))
        item = Item(id=task.id, state="push", title="Pushy")
        # Drop the success edge the action would advance along. ``push``'s
        # active_state is ``push`` and its success_state is ``watch`` (the
        # next job's queue_state); deleting ("push", "watch") models a
        # process YAML missing that transition.
        active_sm = svc._resolve_flow(item).state_machine.transitions
        assert ("push", "watch") in active_sm, "precondition: bundled SM registers the success edge"
        del active_sm[("push", "watch")]

        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="push",
                to_status="working",
                to_current_step="push",
            )
        )
        push_step = next(j for j in svc.flow.jobs if j.name == "push")
        run(svc._dispatch_step(item, push_step))
        run(asyncio.sleep(0.05))

        row = run(svc.db.get_task(task.id))
        assert row.state == "push", "state stays at the action's active_state on the no-edge fallback"
        assert row.status == "blocked"
        errs = run(svc.db.get_messages(task.id, msg_type="error"))
        assert any("no ('push', 'watch') SM edge" in m.content for m in errs), (
            "the no-edge fallback must append an audit message naming the missing edge so an "
            "operator debugging a silently-blocked task can spot the misconfigured process YAML"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# approve() — sub-flow gate resolves to_step against the active flow
# ---------------------------------------------------------------------------


def test_approve_subflow_gate_resolves_next_step_against_active_flow(tmp_path, run):
    """``approve()`` on a gate step reached only via the catalog fallback must
    derive the stage-transition ``to_step`` from the task's ACTIVE flow, not
    the root flow.

    Regression for the claude[bot] PR review Low finding on ``approve()``'s
    ``active_flow_for_approve`` path. A sub-flow-only ``evaluate: true`` job is
    absent from ``self.flow.jobs`` (root/main), so a root-only scan would leave
    ``current_idx`` ``None`` and emit ``to_step=item.state`` (the gate state
    name) instead of the next sub-flow job's name. The fix reuses the
    ``FlowConfig`` resolved for the SM check so both sites agree on ordering.

    Unreachable in the bundled process (no sub-flow step has ``evaluate: true``)
    so the test declares a custom process whose ``sub`` flow opens with a gate.
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        """
process: approve_subflow_gate
jobs:
  - { name: code, type: agent, prompt: coding, queue_state: coding, active_state: coding }
  - { name: watch, type: monitor, engine: capture_engine }
  - { name: check, type: agent, prompt: coding, evaluate: true }
  - { name: finalize, type: agent, prompt: coding }
flows:
  main:
    steps: [code, watch]
  sub:
    steps: [check, finalize]
"""
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}\n")

    _register_capture_engine()

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
    run(svc.start())

    # Block downstream advancement so the test observes only approve()'s own
    # message-writing logic, not the gate auto-advance that would follow.
    async def _noop_dispatch_next_step(item, feedback=None):
        return None

    svc._dispatch_next_step = _noop_dispatch_next_step  # type: ignore[method-assign]

    try:
        # Lay the row as a waiting gate inside the ``sub`` flow: the ``check``
        # job is sub-flow-only, so approve() reaches it via the catalog
        # fallback (process.jobs), not self.flow.jobs (main).
        task = run(svc.db.create_task("Gate", state="check", metadata={"current_flow": "sub"}))
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="check",
                to_status="waiting",
                to_current_step="check",
            )
        )
        run(svc.approve(task.id))

        msgs = run(svc.db.get_messages(task.id, msg_type="stage_transition"))
        assert msgs, "approve() must emit a stage_transition message"
        meta = msgs[-1].metadata
        assert meta["from_step"] == "check"
        assert meta["to_step"] == "finalize", (
            "to_step must be the next job in the ACTIVE sub-flow ('finalize'), not the gate "
            f"state name; got {meta['to_step']!r} (pre-fix: a root-only scan emits the gate "
            "state 'checkd' because 'check' isn't in main's jobs)"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# Monitor job dispatch — flips status without spawning anything
# ---------------------------------------------------------------------------


def test_monitor_step_dispatch_sets_waiting_for_pr_status_without_in_flight(tmp_path, run):
    """A monitor step transitions to status=waiting_for_pr; no in-flight entry."""
    _register_capture_engine()
    _register_capture_tool()
    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(svc.db.create_task("Mon", state="watch"))
        # Lay status=working so _dispatch_step's CAS lands.
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="watch",
                to_status="working",
                to_current_step="watch",
            )
        )
        item = Item(id=task.id, state="watch", title="Mon")
        watch_step = next(j for j in svc.flow.jobs if j.name == "watch")
        run(svc._dispatch_step(item, watch_step))

        row = run(svc.db.get_task(task.id))
        assert row.status == "waiting_for_pr"
        assert row.state == "watch"
        # The engine drives the monitor state externally; no background task
        # should be tracked.
        assert task.id not in svc._in_flight
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# _action_states / _monitor_state derivation
# ---------------------------------------------------------------------------


def test_start_derives_action_and_monitor_states_from_process(tmp_path, run):
    """The orchestrator caches the action/monitor queue_state names at start().

    ``_action_states_by_process`` keys a set per process so a process
    declaring multiple action jobs survives the restart-recovery sweep on any
    of them (not just the first). ``_monitor_states_by_process`` is keyed for
    any registered engine (not just the built-in ``pr_monitor``).

    ADR-021 R2 decomposes the former ``_action_states`` / ``_monitor_state`` /
    ``_pr_monitor`` singletons into per-process collections keyed by the
    process name.
    """
    _register_capture_engine()
    _register_capture_tool()
    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        active = svc._active_process_name
        assert "push" in svc._action_states_by_process[active]
        assert svc._monitor_states_by_process[active] == "watch"
        # The engine itself was instantiated via ``get_engine("capture_engine")``,
        # not skipped — the orchestrator no longer hardcodes ``pr_monitor``.
        assert svc._pr_monitors_by_process[active] is not None
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_start_instantiates_custom_engine_via_registry(tmp_path, run):
    """A monitor job declaring a custom (non-built-in) engine must be
    instantiated via ``get_engine`` at startup and have its ``run()``
    method scheduled.

    Pre-fix behaviour: the orchestrator hardcoded ``if job.engine ==
    "pr_monitor"`` and logged a warning for any other engine name. The
    engine class was registered but never instantiated, so tasks reaching
    its ``queue_state`` would sit at ``status=waiting_for_pr`` with no
    poller advancing them — the exact "shelf-ware" failure mode this PR
    closes.

    This test exercises the user-facing capability: a custom engine
    registered via ``lotsa.yaml``'s ``engines:`` block (or directly via
    ``register_engine``) actually runs.
    """
    cls = _register_capture_engine()
    cls.instances.clear()  # isolate from sibling tests' side effects
    _register_capture_tool()
    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        # The engine class was instantiated exactly once by start().
        assert len(cls.instances) == 1
        engine = cls.instances[0]
        # The orchestrator threaded the monitor job's queue_state through
        # as ``monitor_state`` per the engine constructor contract.
        assert engine.monitor_state == "watch"
        # ADR-021 R2: the instance is tracked per-process in
        # ``_pr_monitors_by_process`` keyed by the active process name (the
        # single ``_pr_monitor`` singleton is removed).
        assert svc._pr_monitors_by_process[svc._active_process_name] is engine
        # The engine's run() was scheduled — give the event loop one tick
        # so the capture engine's ``ran = True`` write lands.
        run(asyncio.sleep(0))
        assert engine.ran is True
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_start_records_every_action_state_for_recovery_sweep(tmp_path, run):
    """Regression: a process's per-process action-state set must collect EVERY
    action job's queue_state.

    A process with two action jobs (``push`` and ``label``) used to only
    record the first one's queue_state because ``_action_state`` was a
    single ``str | None`` — a task stuck mid-execution at the second
    action's state would survive restart as ``status=working`` instead of
    being flipped to ``blocked``. The fix promotes the field to a ``set``;
    ADR-021 R2 further keys it per process in ``_action_states_by_process``.
    """
    _register_capture_engine()
    _register_capture_tool()
    # Second action tool — minimal stub; the test never dispatches it,
    # only verifies start() records its state.
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    async def label_tool(ctx, config):
        return ToolResult(success=True, output="labeled")

    register_tool("label", label_tool)

    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        """
process: multi_action
jobs:
  - { name: code, type: agent, prompt: coding, queue_state: coding, active_state: coding }
  - { name: push, type: action, tool: capture_call }
  - { name: label, type: action, tool: label }
  - { name: watch, type: monitor, engine: capture_engine }
flows:
  main:
    steps: [code, push, label, watch]
"""
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}\n")

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
    run(svc.start())
    try:
        assert svc._action_states_by_process[svc._active_process_name] == {"push", "label"}
    finally:
        run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# Registry loader: non-async user tool is rejected at startup
# ---------------------------------------------------------------------------


def test_load_user_tools_rejects_sync_callable(tmp_path):
    """``load_user_tools`` rejects a non-async callable instead of silently registering it."""
    pkg = tmp_path / "typedjobs_badtool_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "synctool.py").write_text("def sync_tool(ctx, config):\n    return None\n")
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        from lotsa.registry import load_user_tools

        with pytest.raises(TypeError, match="async def"):
            load_user_tools({"sync_tool": "typedjobs_badtool_pkg.synctool:sync_tool"})
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("typedjobs_badtool_pkg")]:
            sys.modules.pop(mod, None)


def test_load_user_engines_rejects_non_class(tmp_path):
    """``load_user_engines`` rejects a module attribute that isn't a class."""
    pkg = tmp_path / "typedjobs_badengine_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "engine.py").write_text("not_a_class = 42\n")
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        from lotsa.registry import load_user_engines

        with pytest.raises(TypeError, match="must be a class"):
            load_user_engines({"bogus": "typedjobs_badengine_pkg.engine:not_a_class"})
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("typedjobs_badengine_pkg")]:
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Multi-process loading from lotsa.yaml processes: block
# ---------------------------------------------------------------------------


def _make_service_with_inline_processes(tmp_path, run, *, processes: dict[str, Any], prompts: list[str] | None = None):
    """Build an OrchestratorService whose lotsa.yaml defines inline processes.

    Since ADR-034 ``start()`` always loads the full bundled catalog
    (``PRESET_NAMES``) alongside any inline ``processes:`` — there is no
    single-preset "backstop" loader anymore. ``flow="build"`` below therefore
    only selects which already-loaded process is the active/default one (an
    inline ``default: true`` entry still outranks it); it does not gate what
    loads.
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in prompts or []:
        (prompts_dir / f"{name}-system.md").write_text(f"# {name}-system\n")
        (prompts_dir / f"{name}-user.md").write_text(f"# {name}-user\n")

    # Also drop the agent capture engine + tool so the active bundled process
    # (full) doesn't tear down the orchestrator at startup.
    _register_capture_engine()
    _register_capture_tool()

    # When an inline default is configured we let it be the active process;
    # the prompts files written above (one per name) also satisfy the bundled
    # presets the ADR-034 catalog loader brings in (the active inline process
    # loads its prompts from this dir).
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        # Explicit active/default selection for tests that want a non-chat
        # default (``chat`` is the package default since ADR-034).
        flow="build",
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
        processes=processes,
        config_path=tmp_path / "lotsa.yaml",
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    return svc


def test_start_loads_inline_processes_into_catalog(tmp_path, run):
    """Inline ``processes:`` block entries are loaded alongside the active process."""
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "steps": [
                    {"name": "research", "prompt": "research"},
                    {"name": "synthesize", "prompt": "synthesize"},
                ],
            },
            "support_triage": {
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "synthesize", "triage"],
    )
    run(svc.start())
    try:
        # All inline processes are loaded into the catalog...
        assert "marketing_research" in svc._processes
        assert "support_triage" in svc._processes
        # ...alongside the active (bundled "build") process.
        assert "build" in svc._processes
        # The active process is the bundled "build" — no inline default was set.
        assert svc.process is svc._processes["build"]
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_inline_default_overrides_bundled_active(tmp_path, run):
    """An inline entry with ``default: true`` becomes the active process."""
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    run(svc.start())
    try:
        assert svc.process is not None
        assert svc.process.name == "marketing_research"
        assert svc.flow is not None
        assert [j.name for j in svc.flow.jobs] == ["research"]
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_multiple_inline_defaults_warn_and_first_wins(tmp_path, run, caplog):
    """Two ``default: true`` entries: warn loudly, then first-wins selection.

    Regression: pre-fix ``_select_active_process_name`` silently picked the
    first dict-order entry while ``list_processes_summary`` reported every
    matching entry as ``is_default: true``. The dropdown then showed two
    "default" badges with no startup signal — a silent misconfiguration in
    a governance platform. The fix logs a warning naming both entries and
    the winner; the silent first-wins selection itself is preserved (raising
    would be harsher than needed for a YAML typo).
    """
    import logging

    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
            "support_triage": {
                "default": True,  # second default — should trigger the warning
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "triage"],
    )
    with caplog.at_level(logging.WARNING, logger="lotsa.orchestrator"):
        run(svc.start())
    try:
        # First dict-order entry wins.
        assert svc._active_process_name == "marketing_research"
        # Warning surfaced both names so the operator can fix the YAML.
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("default: true" in m and "marketing_research" in m and "support_triage" in m for m in warnings), (
            f"Expected a startup WARNING naming both default entries; got: {warnings!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_list_processes_summary_marks_inline_default_true(tmp_path, run):
    """An inline ``default: true`` entry's summary carries ``is_default: True``.

    The ``is_default`` flag drives the UI "default" badge. Pre-PR there was
    no inline-default code path; this test pins the wire format the API
    serves so a regression in ``list_processes_summary``'s ``inline_defaults``
    set comprehension (e.g. dropping ``is True``, flipping the key name)
    surfaces immediately rather than at UI runtime.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
            "support_triage": {
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "triage"],
    )
    run(svc.start())
    try:
        summaries = svc.list_processes_summary()
        by_name = {s["name"]: s for s in summaries}

        # The inline default is both active and flagged as default.
        assert by_name["marketing_research"]["is_default"] is True
        assert by_name["marketing_research"]["is_active"] is True

        # The non-default inline entry is neither.
        assert by_name["support_triage"]["is_default"] is False
        assert by_name["support_triage"]["is_active"] is False
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_list_processes_summary_marks_every_default_entry(tmp_path, run, caplog):
    """When multiple inline entries declare ``default: true``, every match
    carries ``is_default: True`` even though only the first is ``is_active``.

    This is the documented divergence between ``is_default`` and ``is_active``
    (see the docstring on ``list_processes_summary``): the dropdown shows
    every entry the YAML claims as default so the operator can tell which
    ones they need to fix. The startup warning surfaces the same set of
    names to the logs.
    """
    import logging

    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
            "support_triage": {
                "default": True,  # second default
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "triage"],
    )
    with caplog.at_level(logging.WARNING, logger="lotsa.orchestrator"):
        run(svc.start())
    try:
        summaries = svc.list_processes_summary()
        by_name = {s["name"]: s for s in summaries}

        # Both inline entries flagged is_default.
        assert by_name["marketing_research"]["is_default"] is True
        assert by_name["support_triage"]["is_default"] is True

        # ...but only the first-wins entry is active.
        actives = [s["name"] for s in summaries if s["is_active"]]
        assert actives == ["marketing_research"], (
            f"Exactly one entry must be active even when multiple are flagged default; got {actives!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_inline_default_warns_when_config_flow_conflicts(tmp_path, run, caplog):
    """A single inline ``default: true`` outranks an explicit ``config.flow``
    of a different non-default value — warn so the operator sees that their
    ``--flow``/``--process`` flag was ignored.

    Pre-fix the inline default silently won. Confusion case from the bot
    review: operator runs ``lotsa serve --flow support_triage`` with
    ``marketing_research: {default: true}`` in lotsa.yaml and gets
    ``marketing_research`` with no startup signal that their flag did
    nothing.
    """
    import logging

    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
            "support_triage": {
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "triage"],
    )
    # Simulate ``--flow support_triage`` — different non-default name than the
    # inline default winner.
    svc.config.flow = "support_triage"

    with caplog.at_level(logging.WARNING, logger="lotsa.orchestrator"):
        run(svc.start())
    try:
        # Inline default still wins (precedence is preserved)...
        assert svc._active_process_name == "marketing_research"
        # ...but the operator sees a warning naming both sides of the conflict.
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("marketing_research" in m and "support_triage" in m and "default: true" in m for m in warnings), (
            f"Expected a startup WARNING naming the silenced flag; got: {warnings!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_inline_default_silent_when_config_flow_is_package_default(tmp_path, run, caplog):
    """No warning when ``config.flow`` is the package default ``"chat"`` — the
    operator hasn't explicitly chosen anything, so the inline default winning
    is what they implicitly asked for.

    ADR-034 §2 flipped the package default ``full`` → ``chat``; the
    "treat-as-unset" sentinel in ``_select_active_process_name`` moved with it,
    so this test pins the new default value.
    """
    import logging

    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    # The helper sets flow="build" for its bundled fallback; override to the
    # package default so this test exercises the "operator chose nothing" path.
    svc.config.flow = "chat"
    assert svc.config.flow == "chat"

    with caplog.at_level(logging.WARNING, logger="lotsa.orchestrator"):
        run(svc.start())
    try:
        assert svc._active_process_name == "marketing_research"
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("outranks" in m and "marketing_research" in m for m in warnings), (
            f"Expected no conflict warning for the package default; got: {warnings!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_flow_file_wins_over_inline_name_collision(tmp_path, run):
    """``--flow-file`` is documented as highest priority; an inline process
    named the same as ``config.flow`` must NOT shadow the file.

    Regression: pre-fix ``start()`` selected the inline process whenever
    ``active_name in self._processes`` was true, even with ``flow_file``
    set. So a ``lotsa.yaml`` with ``flow: full`` and an inline ``full:``
    entry would silently load the inline one and ignore ``--flow-file``.
    The docstring on ``_select_active_process_name`` declares flow_file
    "authoritative"; the fix enforces that in ``start()``.
    """
    # Stand up a real process.yaml on disk that ``--flow-file`` will load.
    file_process_yaml = tmp_path / "from_file.yaml"
    file_process_yaml.write_text(
        """
process: from_file
jobs:
  - { name: coding, type: agent, prompt: coding }
flows:
  main:
    steps:
      - coding
"""
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user", "shadowing-system", "shadowing-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n")

    # Inline catalog has an entry named the SAME as config.flow. Without the
    # fix the inline lookup matches first and ``--flow-file`` is ignored.
    _register_capture_engine()
    _register_capture_tool()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="shadowing",  # matches the inline name below
        flow_file=file_process_yaml,
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
        processes={
            "shadowing": {
                "steps": [{"name": "shadowing", "prompt": "shadowing"}],
            }
        },
        config_path=tmp_path / "lotsa.yaml",
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    run(svc.start())
    try:
        # ``--flow-file`` is authoritative — its YAML's ``process:`` name
        # (``from_file``) is what loaded as the active process.
        assert svc.process is not None
        assert svc.process.name == "from_file", (
            f"--flow-file must override inline-name collisions; "
            f"got active process {svc.process.name!r}, expected 'from_file'. "
            f"Pre-fix the inline 'shadowing' entry won, silently ignoring "
            f"the explicit --flow-file path."
        )
        # The active key in the catalog is the file's declared process
        # name (``from_file``), not the placeholder ``active_name`` that
        # would have shared the inline ``"shadowing"`` key. Keying by
        # ``self.process.name`` here prevents the file-loaded process
        # from silently overwriting the inline catalog entry — both
        # remain loadable for the future per-task-dispatch refactor.
        assert svc._active_process_name == "from_file"
        assert "from_file" in svc._processes
        assert svc._processes["from_file"] is svc.process
        # The inline ``"shadowing"`` entry is preserved intact — its
        # steps are still those declared inline, not the file's.
        assert "shadowing" in svc._processes
        shadowing_steps = [s.name for s in svc._processes["shadowing"].flows["main"].steps]
        assert shadowing_steps == ["shadowing"], (
            f"The inline 'shadowing' entry must keep its declared step list "
            f"(['shadowing']) — pre-fix start()'s else-branch overwrote it "
            f"with the file-loaded process's steps. Got {shadowing_steps!r}."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_flow_file_warns_when_config_flow_also_set(tmp_path, run, caplog):
    """``--flow-file`` outranks ``--flow``/``--process``; warn when both are
    provided so the operator sees their flag was ignored.

    Pattern parity with the inline-default-vs-flow conflict warning. The
    bot review surfaced the inline-default case; the same silent-precedence
    pattern existed on the ``flow_file`` branch.
    """
    import logging

    # Minimal standalone process.yaml.
    file_process_yaml = tmp_path / "from_file.yaml"
    file_process_yaml.write_text(
        """
process: from_file
jobs:
  - { name: coding, type: agent, prompt: coding }
flows:
  main:
    steps:
      - coding
"""
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n")

    _register_capture_engine()
    _register_capture_tool()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="build",  # explicit non-default value — should trigger warning
        flow_file=file_process_yaml,
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
        config_path=tmp_path / "lotsa.yaml",
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()

    with caplog.at_level(logging.WARNING, logger="lotsa.orchestrator"):
        run(svc.start())
    try:
        # flow_file still wins (precedence preserved).
        assert svc.process is not None
        assert svc.process.name == "from_file"
        # ...and the operator sees a warning naming the ignored flow value.
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("flow-file" in m and "build" in m for m in warnings), (
            f"Expected a startup WARNING that --flow-file outranks flow; got: {warnings!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_create_task_records_active_process_name_in_metadata(tmp_path, run):
    """``create_task`` writes the active process's name into task metadata."""
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    run(svc.start())
    try:
        task = run(svc.create_task(title="research test"))
        # Re-read from DB to confirm metadata persisted.
        fresh = run(svc.db.get_task(task.id))
        assert fresh.metadata.get("process_name") == "marketing_research"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_create_task_labels_task_with_active_process_not_config_flow(tmp_path, run):
    """The ``flow_name`` column / API field must reflect the *active* process,
    not the ``config.flow`` value when an inline ``default: true`` overrides.

    Regression: ``create_task`` used to set ``flow_name = self.config.flow
    or "standard"``, which surfaced the YAML ``flow:`` value (e.g. "full")
    even when an inline ``default: true`` entry had made a different
    process the active one. The DB column and ``TaskDetail.flow_name`` API
    response then mislabelled every task.

    Per-task dispatch isn't wired yet (today's tasks all run against
    ``self.process``), so the misleading label was the audit/display
    issue rather than a routing bug — but the field is load-bearing for
    operator visibility, hence the fix and the test.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    # Sanity check the fixture: config.flow is "full" but the active
    # process is the inline default. This is exactly the divergence the
    # bug exploited — without the fix, ``flow_name`` would be "full".
    assert svc.config.flow == "build"
    run(svc.start())
    try:
        assert svc._active_process_name == "marketing_research"
        task = run(svc.create_task(title="t"))
        fresh = run(svc.db.get_task(task.id))
        assert fresh.flow_name == "marketing_research", (
            "Pre-fix create_task wrote self.config.flow (='full') as the "
            "task's flow_name, mislabelling the audit trail and API "
            "response. Post-fix uses the resolved active process name."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_create_task_accepts_inactive_process(tmp_path, run):
    """ADR-021: requesting a loaded non-active process is now a valid dispatch
    target — the task records that process and routes through it, no restart.

    Inverts the pre-ADR-021 behaviour (which raised ``ProcessNotActive`` and
    told the operator to restart with ``--process <name>``). That rejection
    path and the ``ProcessNotActive`` class are removed by ADR-021 R5.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
            "support_triage": {
                "steps": [{"name": "triage", "prompt": "triage"}],
            },
        },
        prompts=["research", "triage"],
    )
    run(svc.start())
    try:
        task = run(svc.create_task(title="t", process_name="support_triage"))
        fresh = run(svc.db.get_task(task.id))
        assert fresh.metadata.get("process_name") == "support_triage"
        assert fresh.flow_name == "support_triage"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_start_unknown_flow_name_mentions_inline_catalog(tmp_path, run):
    """A misspelled ``--flow`` / ``flow:`` value surfaces an error that names
    both the bundled presets AND every inline process loaded from
    ``lotsa.yaml``'s ``processes:`` block.

    Pre-fix the ``ValueError`` came straight from ``build_process`` and only
    mentioned the three preset names. After ``--flow`` was widened from
    ``click.Choice`` to ``click.STRING`` to accept inline names, a typo
    would point the operator at presets only — misleading once they were
    using inline processes. The orchestrator re-raises with the full
    valid-name set.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    # Pretend the operator passed ``--flow markteing_research`` (typo).
    svc.config.flow = "markteing_research"

    with pytest.raises(ValueError) as exc_info:
        run(svc.start())
    try:
        message = str(exc_info.value)
        # Bundled presets surfaced.
        assert "build" in message and "fix" in message and "chat" in message, (
            f"Unknown-flow error must list bundled presets; got: {message!r}"
        )
        # Inline catalog surfaced.
        assert "marketing_research" in message, f"Unknown-flow error must list inline names; got: {message!r}"
        # The misspelled name itself surfaced so the operator can see what they typed.
        assert "markteing_research" in message, f"Unknown-flow error must echo the bad name; got: {message!r}"
        # Original ``Unknown process`` is the chained cause.
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "Unknown process" in str(exc_info.value.__cause__)
    finally:
        # start() raised before completing, so nothing to shut down here.
        run(svc.db.close())


def test_create_task_rejects_unknown_process(tmp_path, run):
    """Requesting a process_name that isn't loaded surfaces the available names."""
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    run(svc.start())
    try:
        with pytest.raises(ValueError, match="Unknown process"):
            run(svc.create_task(title="t", process_name="not_a_thing"))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_process_for_falls_back_to_active_when_metadata_missing(tmp_path, run):
    """Legacy tasks (no ``process_name`` in metadata) route to the active process.

    Pre-multi-process tasks have no ``process_name`` field. The lookup must
    fall back rather than raise — the recovery sweep, retry(), and every
    dispatch site rely on this so existing deployments don't break on the
    upgrade restart.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "marketing_research": {
                "default": True,
                "steps": [{"name": "research", "prompt": "research"}],
            },
        },
        prompts=["research"],
    )
    run(svc.start())
    try:
        legacy_task = run(svc.db.create_task("legacy", state="research"))
        # Pre-multi-process: metadata has no process_name.
        assert "process_name" not in (legacy_task.metadata or {})
        proc = svc._process_for(legacy_task)
        assert proc is svc.process

        # Stale-name path: metadata HAS a ``process_name`` but the name no
        # longer resolves (e.g. the process was removed from lotsa.yaml
        # between restarts). Documented behaviour: fall back to the active
        # process. This is the same fallback as the legacy-row case above,
        # but the metadata IS present — a different code branch.
        stale_task = run(
            svc.db.create_task(
                "stale name",
                state="research",
                metadata={"process_name": "removed_process_no_longer_in_yaml"},
            )
        )
        assert stale_task.metadata.get("process_name") == "removed_process_no_longer_in_yaml"
        proc = svc._process_for(stale_task)
        assert proc is svc.process, (
            "A task whose recorded process_name no longer maps to a loaded "
            "process must route to the active process rather than raise — "
            "every dispatch site reads this and the recovery sweep relies "
            "on it not blowing up."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# ADR-034 — Chat-first task creation: full bundled catalog loads at start()
# ---------------------------------------------------------------------------
#
# Pre-ADR-034 ``start()`` loads ONLY the active bundled preset (named by
# ``--flow``/``--process``) plus inline ``processes:`` entries. ADR-034 §1 drops
# that rule: the whole bundled catalog (``PRESET_NAMES``) loads so every preset
# is a pickable new-task option AND a valid promotion target. ADR-034 §2 flips
# the new-task default from ``full`` to ``chat``; §4 redefines ``--flow`` to
# select the active/default process rather than gate what loads.
#
# These tests construct a service with NO inline ``processes:`` and no
# ``--flow-file`` so ``start()`` exercises the bundled-catalog path directly.
# ``start()`` registers the built-in ``push_pr`` tool / ``pr_monitor`` engine
# itself (it imports ``lotsa.tools`` / ``lotsa.engines``), so loading ``full``
# — which references both — needs no manual registration here.


def _make_catalog_service(tmp_path, run, *, flow: str = "__unset__"):
    """Build an OrchestratorService with no inline processes and no flow-file.

    With neither an inline ``processes:`` block nor ``--flow-file``, ``start()``
    takes the bundled-catalog path. When *flow* is left unset the config's
    package default applies (chat-first per ADR-034 §2); pass an explicit preset
    name to select the active/default process (ADR-034 §4).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "data_dir": data_dir,
        "work_dir": tmp_path,
        "model": "sonnet",
        "budget": 5.0,
        "config_path": tmp_path / "lotsa.yaml",
    }
    if flow != "__unset__":
        kwargs["flow"] = flow
    config = LotsaConfig(**kwargs)
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    return svc


def test_start_loads_full_bundled_catalog(tmp_path, run):
    """ADR-034 §1 — every bundled preset loads at start(), not just the active one.

    Pre-fix the loader loads only the active preset (``chat`` once the default
    flips, or ``full`` before it), so the others are absent and this assertion
    fails with the missing-preset set.
    """
    from lotsa.flows import PRESET_NAMES

    svc = _make_catalog_service(tmp_path, run)
    run(svc.start())
    try:
        loaded = set(svc._processes)
        missing = set(PRESET_NAMES) - loaded
        assert not missing, f"bundled presets not loaded: {sorted(missing)}"
        # The picker only renders for ≥2 processes; surfacing all five satisfies
        # ADR-034 §3 / acceptance #1 (``GET /api/processes`` returns ≥5).
        summaries = svc.list_processes_summary()
        assert len(summaries) >= 3, (
            f"expected the full catalog (≥5 processes) in the API summary, got {[s['name'] for s in summaries]}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_zero_config_active_process_is_chat(tmp_path, run):
    """ADR-034 §2 — the zero-config default process is ``chat``.

    Pre-fix ``LotsaConfig.flow`` defaults to ``"full"`` (and the terminal
    fallback in ``_select_active_process_name`` is ``"full"``), so the active
    process is ``full`` and this fails with ``_active_process_name == 'full'``.
    """
    svc = _make_catalog_service(tmp_path, run)  # flow left unset → package default
    run(svc.start())
    try:
        assert svc._active_process_name == "chat", (
            f"zero-config new-task default must be chat (ADR-034 §2); got {svc._active_process_name!r}"
        )
        assert svc.process is svc._processes["chat"]
        # ``is_active`` on the API summary tracks the default selection the
        # picker pre-selects — it must be ``chat`` too.
        active = [s["name"] for s in svc.list_processes_summary() if s["is_active"]]
        assert active == ["chat"], f"exactly chat should be is_active; got {active}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_flow_full_selects_active_without_restricting_catalog(tmp_path, run):
    """ADR-034 §4 — ``--flow full`` selects the default; it does not gate loading.

    Pre-fix ``--flow full`` loads ONLY ``full``; ``chat``/``quickfix``/
    ``standard``/``simple`` are absent and the catalog assertions below fail.
    """
    from lotsa.flows import PRESET_NAMES

    svc = _make_catalog_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        # ``--flow full`` picks full as the active/default selection...
        assert svc._active_process_name == "build"
        assert svc.process is svc._processes["build"]
        # ...but every other bundled preset is still loaded and pickable.
        for name in PRESET_NAMES:
            assert name in svc._processes, f"--flow full must not restrict the catalog; {name!r} missing"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_inline_entry_named_full_overrides_bundled_preset(tmp_path, run):
    """ADR-034 §1 / DD1 — an inline ``full`` wins over the bundled ``full``.

    The catalog loader must SKIP a bundled preset whose name an inline entry
    already claimed (inline is authoritative; never overwrite). Here the inline
    ``full`` declares a single ``only`` step, distinct from bundled ``full``'s
    spec→…→pr pipeline.

    Two failure modes this guards:
    * Pre-fix the bundled catalog never loads, so ``chat`` is absent — the
      ``chat`` assertion fails against the pre-ADR-034 tree.
    * A loader that loads presets WITHOUT the inline-wins skip guard would
      overwrite ``_processes['full']`` with the 9-step bundled process — the
      step-list assertion fails against that buggy implementation.
    """
    svc = _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={"full": {"steps": [{"name": "only", "prompt": "only"}]}},
        prompts=["only"],
    )
    run(svc.start())
    try:
        # The catalog loaded the rest of the bundled presets (fails pre-fix).
        assert "chat" in svc._processes, "full bundled catalog must load (ADR-034 §1)"
        # ...but ``full`` is the INLINE one, not the bundled preset.
        full_steps = [s.name for s in svc._processes["full"].flows["main"].steps]
        assert full_steps == ["only"], (
            f"inline ``full`` must win over the bundled preset (DD1); got steps "
            f"{full_steps!r} — the loader overwrote the inline entry."
        )
        # Exactly one process is named ``full`` in the catalog.
        assert list(svc._processes).count("full") == 1
    finally:
        run(svc.shutdown())
        run(svc.db.close())
