"""Failing-first tests for ADR-045 Phase 1 — workflows call workflows.

These pin the behaviour the ADR-045 Phase 1 implementation must deliver
(see the planning-step spec):

* the ``pr_fix`` sub-flow is deleted from ``build``/``fix``; ``pr-monitor``
  becomes a standalone top-level workflow whose entry step is
  ``wait_for_pr_signal``;
* ``build``/``fix`` main end at ``push_pr → call pr-monitor``;
* the cross-flow edge registrar and the drainer's ``SKIPPED`` /
  ``current_flow``-reset special cases are removed;
* ``call <workflow>[@<step>]`` and ``terminate`` become reserved routing
  targets, resolved behind the same seam as ``next``/``blocked``/``complete``;
* an unknown call target or a call cycle fails at process-build time;
* the single ``current_flow`` metadata string is replaced by a persisted
  ``call_stack`` (a list of ``{workflow, step, called_from}`` records) with a
  reserved-but-absent ``vars`` key; push/pop go through CAS;
* ``complete`` pops one frame; ``terminate`` unwinds subject to catches;
* ``dispatch_sub_flow`` completes Layer B — dispatches into any named
  workflow and honours ``target_job`` for named re-entry;
* legacy ``current_flow`` rows with no ``call_stack`` route to ``blocked``
  with the standard recovery message (no migration — decided);
* restart-resume reconstructs the stack from the DB (the sharpest risk).

Written BEFORE the implementation lands, so they are expected to FAIL against
pre-ADR-045 code. New symbols the plan introduces (``validate_call_graph`` in
``lotsa.flows``; ``make_frame``/``push_frame``/``pop_frame``/``stack_top`` in
``lotsa.call_stack``) are imported *inside* the test bodies so a missing symbol
fails that test cleanly (ImportError) rather than breaking module collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import PRESET_NAMES, build_process, resolve_output_target
from lotsa.orchestrator import OrchestratorService
from lotsa.tests.conftest import FakeRunner
from lotsa.tests.test_adr040_restart_resume import (
    RecordingRunner,
    _settle,
    restart_with_seed,
)
from rigg.models import Item

# ``run`` / ``_loop`` fixtures come from ``lotsa/tests/conftest.py``.


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _bundled_service(tmp_path: Path, run, flow: str = "build") -> OrchestratorService:
    """An OrchestratorService whose active process is a bundled preset.

    No ``flow_file`` → ``start()`` loads the FULL bundled catalog
    (``PRESET_NAMES``), so post-ADR-045 ``pr-monitor`` is a loaded, callable
    workflow. Not started — the caller runs ``svc.start()``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        flow=flow,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    return svc


def _write_caller_process(path: Path, name: str, call_target: str) -> Path:
    """A minimal one-agent-step workflow whose step routes ``COMPLETED`` to a
    ``call <call_target>`` edge. Used to exercise call-target parsing and the
    build-time call-graph validator."""
    path.write_text(
        f"""
process: {name}
jobs:
  - name: work
    type: agent
    prompt: coding
    queue_state: working_state
    active_state: working_state
    routes:
      COMPLETED: call {call_target}
flows:
  main:
    steps: [work]
"""
    )
    return path


def _route_targets(job) -> list[str]:
    """All rule targets declared on a ResolvedJob (routes desugar to rules)."""
    return [r.target for r in job.rules]


# ===========================================================================
# A. Topology — build / fix mains and the extracted pr-monitor workflow
# ===========================================================================


def test_pr_monitor_is_a_bundled_preset():
    """``pr-monitor`` is a top-level, independently loadable workflow.

    Fails pre-fix: ``PRESET_NAMES`` is ``("chat", "build", "fix")``.
    """
    assert "pr-monitor" in PRESET_NAMES


def test_pr_monitor_process_loads_with_monitor_entry_step():
    """``pr-monitor`` loads; its entry step is the ``wait_for_pr_signal`` monitor.

    Fails pre-fix: ``build_process("pr-monitor")`` raises ``Unknown process``.
    """
    process = build_process("pr-monitor")
    assert process.name == "pr-monitor"
    main = process.flows["main"]
    entry = main.bindings[0].name
    assert entry == "wait_for_pr_signal", f"pr-monitor must start at wait_for_pr_signal, got {entry!r}"
    by_name = {rj.name: rj for rj in main.jobs}
    assert by_name["wait_for_pr_signal"].type == "monitor"


def test_pr_monitor_contains_the_pr_fix_branches():
    """The extracted workflow carries the former ``pr_fix`` steps."""
    main = build_process("pr-monitor").flows["main"]
    names = {rj.name for rj in main.jobs}
    for step in ("pr-fix", "resolve_conflicts", "review", "push_pr"):
        assert step in names, f"pr-monitor missing {step!r}; has {sorted(names)}"


def test_pr_monitor_skipped_edge_is_internal():
    """``pr-fix``'s ``SKIPPED`` route targets ``wait_for_pr_signal`` inside
    pr-monitor — no longer a cross-flow edge."""
    main = build_process("pr-monitor").flows["main"]
    pr_fix = next(rj for rj in main.jobs if rj.name == "pr-fix")
    assert "wait_for_pr_signal" in _route_targets(pr_fix), (
        f"pr-fix SKIPPED must route to wait_for_pr_signal within pr-monitor; targets were {_route_targets(pr_fix)}"
    )


def test_pr_monitor_push_pr_loops_back_to_monitor():
    """``push_pr`` in pr-monitor routes back to ``wait_for_pr_signal`` (the loop
    that replaces the old sub-flow terminal override)."""
    main = build_process("pr-monitor").flows["main"]
    push = next(rj for rj in main.jobs if rj.name == "push_pr")
    assert "wait_for_pr_signal" in _route_targets(push), (
        f"push_pr must loop back to wait_for_pr_signal; targets were {_route_targets(push)}"
    )


@pytest.mark.parametrize("preset", ["build", "fix"])
def test_bundled_main_has_no_pr_fix_flow(preset):
    """``build``/``fix`` no longer declare a ``pr_fix`` flow.

    Fails pre-fix: both processes carry a parse-identical ``pr_fix`` flow.
    """
    process = build_process(preset)
    assert "pr_fix" not in process.flows, f"{preset} still declares a pr_fix flow: {sorted(process.flows)}"


@pytest.mark.parametrize("preset", ["build", "fix"])
def test_bundled_main_has_no_wait_for_pr_signal_step(preset):
    """``wait_for_pr_signal`` moved out of ``build``/``fix`` main into pr-monitor.

    Fails pre-fix: ``wait_for_pr_signal`` is the last step of main.
    """
    main = build_process(preset).flows["main"]
    names = [b.name for b in main.bindings]
    assert "wait_for_pr_signal" not in names, f"{preset} main still contains wait_for_pr_signal: {names}"


@pytest.mark.parametrize("preset", ["build", "fix"])
def test_bundled_main_ends_at_call_pr_monitor(preset):
    """``build``/``fix`` main end at ``push_pr → call pr-monitor``.

    Fails pre-fix: main ends at ``wait_for_pr_signal`` and push_pr carries no
    call route.
    """
    main = build_process(preset).flows["main"]
    assert main.bindings[-1].name == "push_pr", f"{preset} main must end at push_pr, got {main.bindings[-1].name!r}"
    push = next(rj for rj in main.jobs if rj.name == "push_pr")
    assert any(t == "call pr-monitor" for t in _route_targets(push)), (
        f"{preset} push_pr must route COMPLETED → 'call pr-monitor'; targets were {_route_targets(push)}"
    )


# ===========================================================================
# B. Routing vocabulary — call / terminate as reserved targets
# ===========================================================================


def test_resolve_output_target_recognizes_terminate():
    """``terminate`` resolves to the unwind sink, not the ``blocked`` fallback.

    Fails pre-fix: an unrecognized target resolves to ``"blocked"``.
    """
    main = build_process("fix").flows["main"]
    job = main.jobs[0]
    resolved = resolve_output_target("terminate", job, main)
    assert resolved == "terminate", f"terminate must resolve to the unwind sink, got {resolved!r}"


def test_resolve_output_target_recognizes_call():
    """``call <workflow>`` resolves to a call sink, not ``blocked``.

    Fails pre-fix: ``call pr-monitor`` is unknown → ``"blocked"``.
    """
    main = build_process("fix").flows["main"]
    job = main.jobs[0]
    resolved = resolve_output_target("call pr-monitor", job, main)
    assert resolved != "blocked", "a call target must not degrade to blocked"
    assert resolved.startswith("call"), f"call target must resolve to a call sink, got {resolved!r}"
    assert "pr-monitor" in resolved


# ===========================================================================
# C. Build-time validation — call syntax accepted; unknown/cycle rejected
# ===========================================================================


def test_call_target_accepted_at_build_time(tmp_path):
    """A step may declare ``routes: { COMPLETED: call <workflow> }`` and build.

    Fails pre-fix: ``_validate_rule_targets`` rejects ``call pr-monitor`` as an
    out-of-process target and ``build_process`` raises ``ValueError``.
    """
    f = _write_caller_process(tmp_path / "caller.yaml", "caller_test", "pr-monitor")
    process = build_process("caller_test", process_file=f)
    work = next(rj for rj in process.flows["main"].jobs if rj.name == "work")
    assert "call pr-monitor" in _route_targets(work), (
        f"the call target must survive build; got rule targets {_route_targets(work)}"
    )


def test_terminate_route_key_accepted_at_build_time(tmp_path):
    """A frame may *catch* an unwind via ``routes: { terminate: <target> }``.

    Fails pre-fix: ``_parse_routes`` rejects the ``terminate`` key because it
    is not in ``AGENT_OUTCOMES``, so ``build_process`` raises.
    """
    f = tmp_path / "catcher.yaml"
    f.write_text(
        """
process: catcher_test
jobs:
  - name: a
    type: agent
    prompt: coding
    queue_state: a_state
    active_state: a_state
    routes:
      COMPLETED: b
      terminate: b
  - name: b
    type: agent
    prompt: coding
    queue_state: b_state
    active_state: b_state
flows:
  main:
    steps: [a, b]
"""
    )
    # Must not raise — ``terminate`` is an accepted (reserved) route key.
    process = build_process("catcher_test", process_file=f)
    assert "main" in process.flows


def test_unknown_call_target_fails_the_build(tmp_path):
    """Calling a workflow that isn't in the catalog fails at build time.

    Fails pre-fix: ``validate_call_graph`` does not exist (ImportError).
    """
    from lotsa.flows import validate_call_graph

    f = _write_caller_process(tmp_path / "caller.yaml", "caller_test", "does-not-exist")
    proc = build_process("caller_test", process_file=f)
    with pytest.raises(ValueError, match="does-not-exist|unknown|not found"):
        validate_call_graph({"caller_test": proc})


def test_call_cycle_fails_the_build(tmp_path):
    """A call cycle (A → B → A) fails at build time with a clear error.

    Fails pre-fix: ``validate_call_graph`` does not exist (ImportError).
    """
    from lotsa.flows import validate_call_graph

    fa = _write_caller_process(tmp_path / "a.yaml", "wf_a", "wf_b")
    fb = _write_caller_process(tmp_path / "b.yaml", "wf_b", "wf_a")
    proc_a = build_process("wf_a", process_file=fa)
    proc_b = build_process("wf_b", process_file=fb)
    with pytest.raises(ValueError, match="cycle"):
        validate_call_graph({"wf_a": proc_a, "wf_b": proc_b})


# ===========================================================================
# D. Cross-flow machinery removed (not generalised)
# ===========================================================================


def test_cross_flow_edge_registrar_is_removed():
    """``_register_cross_flow_edges`` is deleted — no cross-flow machinery.

    Fails pre-fix: the function still exists in ``lotsa.flows``.
    """
    import lotsa.flows as flows_mod

    assert not hasattr(flows_mod, "_register_cross_flow_edges"), (
        "ADR-045 removes _register_cross_flow_edges; the one edge it existed for is now internal to pr-monitor."
    )


# ===========================================================================
# E. The persisted call stack (pure helpers — pins the lotsa.call_stack API)
# ===========================================================================


def test_make_frame_is_a_record_without_vars():
    """A frame is a ``{workflow, step, called_from}`` record; ``vars`` is
    reserved-but-absent (ADR-045 constraint 1/4).

    Fails pre-fix: ``lotsa.call_stack`` does not exist (ImportError).
    """
    from lotsa.call_stack import make_frame

    frame = make_frame(workflow="build", step="plan", called_from=None)
    assert frame["workflow"] == "build"
    assert frame["step"] == "plan"
    assert frame["called_from"] is None
    assert "vars" not in frame, "vars must be reserved-but-absent in v1 (a pure future addition)"


def test_push_and_pop_frame_round_trip():
    """``push_frame`` adds a top frame; ``pop_frame`` removes it; ``stack_top``
    reads the active frame."""
    from lotsa.call_stack import make_frame, pop_frame, push_frame, stack_top

    root = make_frame(workflow="build", step="push_pr", called_from=None)
    meta = {"call_stack": [root]}
    assert stack_top(meta)["workflow"] == "build"

    callee = make_frame(workflow="pr-monitor", step="wait_for_pr_signal", called_from="push_pr")
    pushed = push_frame(meta, callee)
    assert stack_top(pushed)["workflow"] == "pr-monitor"
    assert stack_top(pushed)["called_from"] == "push_pr"

    # complete = pop one frame → returns to the caller.
    popped = pop_frame(pushed)
    assert stack_top(popped)["workflow"] == "build", "popping one frame returns to the caller workflow"


def test_pop_last_frame_yields_empty_stack():
    """Popping the root frame empties the stack (root return → task end)."""
    from lotsa.call_stack import make_frame, pop_frame, stack_top

    meta = {"call_stack": [make_frame(workflow="build", step="plan", called_from=None)]}
    popped = pop_frame(meta)
    assert not (popped.get("call_stack") or []), "popping the only frame leaves an empty stack"
    assert stack_top(popped) is None


# ===========================================================================
# F. create_task seeds a root frame (no current_flow)
# ===========================================================================


def test_create_task_seeds_root_call_stack_frame(tmp_path, run):
    """A new task carries a single-frame ``call_stack`` naming its own workflow;
    the legacy ``current_flow`` slot is gone.

    Fails pre-fix: ``metadata`` has ``current_flow="main"`` and no ``call_stack``.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = run(svc.create_task("root frame test"))
        fresh = run(svc.db.get_task(task.id))
        stack = fresh.metadata.get("call_stack")
        assert stack, f"new task must seed a call_stack; metadata was {fresh.metadata!r}"
        top = stack[-1]
        assert top["workflow"] == fresh.metadata.get("process_name") == "build"
        assert top["called_from"] is None
        assert "current_flow" not in fresh.metadata, "current_flow is replaced by call_stack"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# G. Active-workflow resolution reads the top frame
# ===========================================================================


def test_resolve_flow_reads_top_call_stack_frame(tmp_path, run):
    """``_resolve_flow`` resolves the active workflow from the top stack frame,
    not from ``process_name`` / a legacy ``current_flow`` string.

    A ``build`` task whose stack top is ``pr-monitor`` must resolve to
    pr-monitor's flow (its jobs include ``pr-fix``).

    Fails pre-fix: ``_resolve_flow`` reads ``current_flow`` (absent here) and
    falls back to the task's own (build) root flow, which has no ``pr-fix`` job.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        item = Item(
            id="t-active",
            state="reviewing",
            metadata={
                "process_name": "build",
                "call_stack": [
                    {"workflow": "build", "step": "push_pr", "called_from": None},
                    {"workflow": "pr-monitor", "step": "review", "called_from": "push_pr"},
                ],
            },
        )
        flow = svc._resolve_flow(item)
        job_names = {j.name for j in flow.jobs}
        assert "pr-fix" in job_names, (
            f"active workflow must be pr-monitor (top frame); resolved flow jobs were {sorted(job_names)}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# H. dispatch_sub_flow — Layer B (dispatch any workflow; honour target_job)
# ===========================================================================


def test_dispatch_sub_flow_enters_named_workflow_at_target_job(tmp_path, run):
    """``dispatch_sub_flow`` dispatches into any named workflow and honours
    ``target_job`` for named re-entry (``call pr-monitor@pr-fix``).

    Stages a build task already inside the pr-monitor frame (parked at the
    monitor state) and re-enters at ``pr-fix``; the CAS must land the row in
    pr-fix's active state.

    Fails pre-fix: ``dispatch_sub_flow`` rejects every ``flow_name`` other than
    ``"pr_fix"`` (and validates against the task's own process flows, which do
    not include ``pr-monitor``), so it returns ``False`` and the row never
    moves.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        pr_monitor = svc._processes["pr-monitor"].flows["main"]
        monitor_job = next(rj for rj in pr_monitor.jobs if rj.type == "monitor")
        pr_fix_job = next(rj for rj in pr_monitor.jobs if rj.name == "pr-fix")

        task = run(
            svc.db.create_task(
                "layer B re-entry",
                state=monitor_job.queue_state,
                status="waiting_for_pr",
                current_step=monitor_job.name,
                metadata={
                    "pr_number": 7,
                    "process_name": "build",
                    "call_stack": [
                        {"workflow": "build", "step": "push_pr", "called_from": None},
                        {"workflow": "pr-monitor", "step": monitor_job.name, "called_from": "push_pr"},
                    ],
                },
            )
        )
        # pr-fix historically declares spec/plan inputs — seed them so the
        # missing-artifact branch doesn't short-circuit before the CAS.
        for art in ("spec", "plan"):
            run(
                svc.db.add_message(task.id, "agent", art, f"{art} content", "artifact", metadata={"artifact_name": art})
            )

        dispatched = run(svc.dispatch_sub_flow(task.id, "pr-monitor", target_job="pr-fix"))
        assert dispatched is True, (
            "dispatch_sub_flow must accept any loaded workflow and honour target_job; "
            "pre-fix it rejects non-'pr_fix' names and returns False"
        )
        row = run(svc.db.get_task(task.id))
        assert row.state == pr_fix_job.active_state, (
            f"re-entry must land at pr-fix's active state {pr_fix_job.active_state!r}, got {row.state!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_jump_to_step_resolves_target_via_active_workflow(tmp_path, run):
    """``jump_to_step`` resolves its target step against the task's ACTIVE
    workflow (top call-stack frame), mirroring ``dispatch_pr_fix`` /
    ``_block_after_sync`` / retry's ``_resolve_step_for_row``.

    A ``build`` task parked inside ``pr-monitor`` jumps to ``pr-fix`` (a job that
    lives only in ``pr-monitor`` now) and lands at pr-fixing.

    Fails pre-fix: ``jump_to_step`` resolved the target via ``_root_flow_for`` /
    ``_process_for`` (the creation process = ``build``), which no longer carries
    ``pr-fix``, so it raised ``ValueError("Unknown step: pr-fix")``.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        pr_monitor = svc._processes["pr-monitor"].flows["main"]
        pr_fix_job = next(rj for rj in pr_monitor.jobs if rj.name == "pr-fix")
        task = run(
            svc.db.create_task(
                "jump into pr-fix",
                state="reviewing",
                status="working",
                current_step="review",
                metadata={
                    "pr_number": 3,
                    "process_name": "build",
                    "call_stack": [
                        {"workflow": "build", "step": "push_pr", "called_from": None},
                        {"workflow": "pr-monitor", "step": "review", "called_from": "push_pr"},
                    ],
                },
            )
        )
        # pr-fix historically declares spec/plan inputs — seed them so the
        # missing-artifact branch doesn't roll the dispatch back to blocked.
        for art in ("spec", "plan"):
            run(
                svc.db.add_message(task.id, "agent", art, f"{art} content", "artifact", metadata={"artifact_name": art})
            )

        run(svc.jump_to_step(task.id, "pr-fix"))
        row = run(svc.db.get_task(task.id))
        assert row.current_step == "pr-fix" and row.state == pr_fix_job.active_state, (
            f"jump must resolve pr-fix via the active pr-monitor workflow and land at "
            f"{pr_fix_job.active_state!r}; got current_step={row.current_step!r} state={row.state!r}"
        )
        # ADR-045 — jump does not write the removed ``current_flow`` slot; the
        # active workflow is the (unchanged) top call-stack frame.
        assert "current_flow" not in row.metadata
        assert [f["workflow"] for f in row.metadata["call_stack"]] == ["build", "pr-monitor"]
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# I. Legacy rows — current_flow with no call_stack routes to blocked
# ===========================================================================


def test_legacy_current_flow_row_blocks_on_restart(tmp_path, run):
    """A task persisted with a legacy ``current_flow`` slot and no ``call_stack``
    routes to ``blocked`` with the standard recovery message on restart — no
    migration (decided).

    Fails pre-fix: the sweep resumes the row normally (it is not flipped to
    ``blocked`` for being legacy).
    """
    runner = RecordingRunner(supports_resume=True)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="legacy pr_fix row",
        state="coding",
        status="working",
        current_step="coding",
        metadata={"current_flow": "pr_fix"},
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: False, timeout=0.6)  # let the start() sweep run
            row = await db.get_task(task.id)
            assert row.status == "blocked", (
                f"a legacy current_flow row with no call_stack must route to blocked; got status={row.status!r}"
            )

        run(_t())


# ===========================================================================
# J. Restart-resume of a partially-unwound stack (the sharpest risk)
# ===========================================================================


def test_partially_unwound_stack_is_reconstructable_from_db(tmp_path, run):
    """A task interrupted mid-flow with a multi-frame ``call_stack`` reconstructs
    its active workflow from the DB on restart — the stack is state of record.

    Seeds a two-frame stack (``build`` → ``pr-monitor``) and asserts the
    restarted service resolves the active workflow to the top frame
    (pr-monitor), independent of ``process_name``. This is the reconstruction
    the ADR-040 resume path has nothing to build on today.

    Fails pre-fix: ``_resolve_flow`` ignores ``call_stack`` and falls back to
    the task's own root flow (no ``pr-fix`` job).
    """
    runner = RecordingRunner(supports_resume=True)
    # Seed the top frame at ``pr-fix`` (a worker) — the resumed worker advances on
    # its default COMPLETED, so the reconstruction is observable without the
    # incidental gate-no-marker block a resumed gate would produce under a stub
    # runner (that block is a separate mechanism, not what this test pins).
    seeded_stack = [
        {"workflow": "build", "step": "push_pr", "called_from": None},
        {"workflow": "pr-monitor", "step": "pr-fix", "called_from": "push_pr"},
    ]
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="mid-unwind",
        state="pr-fixing",
        status="working",
        current_step="pr-fix",
        metadata={"process_name": "build", "call_stack": seeded_stack},
    ) as (svc, db, task):

        async def _t():
            row = await db.get_task(task.id)
            item = Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)
            flow = svc._resolve_flow(item)
            job_names = {j.name for j in flow.jobs}
            assert "pr-fix" in job_names, (
                "restart must reconstruct the active workflow (pr-monitor) from the "
                f"persisted call_stack; resolved flow jobs were {sorted(job_names)}"
            )
            # And the row must not have been force-blocked simply for carrying a
            # (modern) multi-frame stack.
            assert row.status != "blocked", "a modern call_stack row must resume, not block"

        run(_t())


# ===========================================================================
# K. APPROVED never terminates (ADR-030 constrains where terminate is reachable)
# ===========================================================================


def test_pr_monitor_has_no_agent_route_to_terminate_on_approval():
    """Only merged/closed (the ADR-030 terminal CAS) end the task; no agent step
    in pr-monitor routes to ``terminate``, so an APPROVED review keeps the PR
    monitored (a local edge, no unwind).

    Fails pre-fix: ``pr-monitor`` does not exist to inspect.
    """
    main = build_process("pr-monitor").flows["main"]
    for rj in main.jobs:
        assert "terminate" not in _route_targets(rj), (
            f"{rj.name} routes to terminate; approval/feedback branches must stay local — "
            "only merged/closed terminate, via the untouched ADR-030 terminal CAS"
        )


# ===========================================================================
# L. Runtime stack semantics — complete pops, terminate unwinds, call dispatches
#
# The topology/parse tests above prove the graph is shaped right; these prove
# the RUNTIME is wired to it. The review flagged that ``pop_frame`` had zero
# production callers, that a ``terminate`` catch was treated as a file path, and
# that an agent step routing to ``call``/``terminate``/``complete`` fell through
# to the "no SM edge" strand-warning and stalled at ``status="working"``. Each
# test below fails against that pre-wiring code.
# ===========================================================================


class _CompletedRunner:
    """Agent runner that emits ``AGENT_RESULT: COMPLETED`` (drives the drainer's
    rule-match → route path end-to-end)."""

    def __init__(self) -> None:
        from rigg.models import AgentResult

        self.result = AgentResult(
            success=True, stdout="AGENT_RESULT: COMPLETED\n", stderr="", return_code=0, duration_ms=5
        )

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        return self.result


class _HangRunner:
    """Agent runner whose ``run`` never returns within a test — so a step
    dispatched as a side effect stays put and the row under assertion is stable."""

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        import asyncio

        await asyncio.Event().wait()  # never set


def _item_from(row):
    return Item(id=row.id, state=row.state, title=row.title, body=row.body, metadata=row.metadata)


def _seed(svc, run, *, state, current_step, stack, status="working", extra=None):
    metadata = {"process_name": stack[0]["workflow"], "call_stack": stack}
    if extra:
        metadata.update(extra)
    return run(
        svc.db.create_task(
            f"seed-{current_step}",
            state=state,
            status=status,
            current_step=current_step,
            metadata=metadata,
        )
    )


def _wf_names(row):
    return [f["workflow"] for f in (row.metadata.get("call_stack") or [])]


def test_complete_at_root_frame_ends_the_task(tmp_path, run):
    """``complete`` on a depth-1 (root) stack pops the last frame and ends the
    task — status ``complete`` with an empty stack.

    Fails pre-fix: ``_return_to_caller`` does not exist; the drainer completed
    the task without ever popping, leaving ``call_stack`` with a stale frame.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state="summarizing",
            current_step="pr_summary",
            stack=[{"workflow": "build", "step": "pr_summary", "called_from": None}],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._return_to_caller(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete", f"root complete must end the task; status={row.status!r}"
        assert row.metadata.get("call_stack") == [], "the root frame must be popped (pop_frame wired)"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_complete_folds_through_tail_caller_to_terminal(tmp_path, run):
    """``complete`` pops one frame and returns to the caller; a caller whose call
    site was its LAST step (a tail call — ``build``'s ``push_pr``) has nothing
    after it, so the unwind folds through it to a terminal completion.

    Fails pre-fix: the completing frame is never popped and the caller is never
    consulted — the task simply ends with the two-frame stack intact.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state="reviewing",
            current_step="review",
            stack=[
                {"workflow": "build", "step": "push_pr", "called_from": None},
                {"workflow": "pr-monitor", "step": "review", "called_from": "push_pr"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._return_to_caller(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete", f"tail-call return must reach terminal; status={row.status!r}"
        assert row.metadata.get("call_stack") == [], "both frames unwind on a tail-call completion"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_complete_returns_to_caller_next_step(tmp_path, run):
    """``complete`` with a caller that has a step AFTER its call site resumes the
    caller there (not the whole task ending, and not the call site re-running).

    Caller ``build`` is parked at ``plan`` (call site); its binding successor is
    ``test``. The callee (``pr-monitor``) completing pops one frame and lands the
    task at ``test`` with only the caller frame remaining.

    Fails pre-fix: a mid-stack ``complete`` ended the whole task (no pop, no
    resume).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    svc.runner = _HangRunner()  # the resumed ``test`` agent hangs → row stays put
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state="reviewing",
            current_step="review",
            stack=[
                {"workflow": "build", "step": "plan", "called_from": None},
                {"workflow": "pr-monitor", "step": "review", "called_from": "plan"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._return_to_caller(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert _wf_names(row) == ["build"], f"only the caller frame remains; stack={row.metadata.get('call_stack')!r}"
        assert row.current_step == "test", f"must resume at the caller's next step after plan; got {row.current_step!r}"
        assert row.state == "testing"
        assert row.status != "complete", "a mid-stack complete returns to the caller, it does not end the task"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_terminate_unwinds_to_root_and_completes(tmp_path, run):
    """``terminate`` with no catch anywhere unwinds every frame and ends the task
    (the graph-driven route into ADR-030's terminal CAS shape).

    Fails pre-fix: ``terminate`` resolved to the literal string ``"terminate"``,
    found no queue_state, and fell through to the best-effort ``blocked`` — never
    a terminal completion, and the stack was never unwound.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state="pr-fixing",
            current_step="pr-fix",
            stack=[
                {"workflow": "build", "step": "push_pr", "called_from": None},
                {"workflow": "pr-monitor", "step": "pr-fix", "called_from": "push_pr"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._unwind_terminate(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete", f"uncaught terminate ends the task; status={row.status!r}"
        assert row.metadata.get("call_stack") == [], "terminate unwinds the whole stack"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def _write_terminate_catcher(path: Path) -> Path:
    path.write_text(
        """
process: catcher
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: gating
    active_state: gating
    routes: { terminate: blocked }
  - name: after
    type: agent
    prompt: coding
    queue_state: after_state
    active_state: after_state
flows:
  main:
    steps: [gate, after]
"""
    )
    return path


def test_terminate_caught_by_caller_frame(tmp_path, run):
    """A frame catches ``terminate`` when its call-site step declares a
    ``routes: { terminate: <target> }`` catch — the task routes to ``<target>``
    within that caller and the caught frame STAYS on the stack (no further
    unwinding). Frames above it are still unwound.

    Fails pre-fix: no unwind logic consulted a frame's ``terminate`` catch, so
    the catch could never fire (it was compiled to an inert rule and, worse,
    ``evaluate_output_rules`` treated ``source="terminate"`` as a file path).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        # Inject a catcher workflow whose ``gate`` step catches terminate → blocked.
        catcher = build_process("catcher", process_file=_write_terminate_catcher(tmp_path / "catcher.yaml"))
        svc._processes["catcher"] = catcher
        task = _seed(
            svc,
            run,
            state="coding",
            current_step="code",
            stack=[
                {"workflow": "catcher", "step": "gate", "called_from": None},
                {"workflow": "build", "step": "code", "called_from": "gate"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._unwind_terminate(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "blocked", f"the catch routes to blocked; status={row.status!r}"
        # A catch resolving to a bare state (``blocked``) must preserve
        # ``current_step`` as the catch-site step, mirroring every other blocked
        # transition in the orchestrator (``block()`` / the drainer's ``blocked``
        # branch) — not null it. Pre-fix this landed ``current_step=None``.
        assert row.current_step == "gate", (
            f"a caught terminate → blocked keeps the catch-site step as current_step; got {row.current_step!r}"
        )
        assert _wf_names(row) == ["catcher"], (
            f"the emitting (build) frame is unwound; the catching (catcher) frame stays — stack={_wf_names(row)!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def _write_terminate_complete_catcher(path: Path) -> Path:
    path.write_text(
        """
process: completer
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: gating
    active_state: gating
    routes: { terminate: complete }
flows:
  main:
    steps: [gate]
"""
    )
    return path


def test_terminate_catch_to_a_terminal_status_ends_the_task(tmp_path, run):
    """A ``routes: { terminate: complete }`` catch (``complete``/``abandoned`` are
    accepted sentinels, so this is valid YAML) must END the task, not park it at
    ``status="working"``.

    Fails pre-fix: the bare-state catch branch only special-cased ``blocked``
    (``to_status = "blocked" if dest == "blocked" else "working"``). A catch
    resolving to ``"complete"`` therefore landed ``status="working"`` /
    ``state="complete"`` / ``current_step="gate"`` and then re-dispatched into a
    non-existent step — the task was stuck (never shown complete, never resumable).
    Post-fix the transition folds to ``status="complete"`` with a null step.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        completer = build_process(
            "completer", process_file=_write_terminate_complete_catcher(tmp_path / "completer.yaml")
        )
        svc._processes["completer"] = completer
        task = _seed(
            svc,
            run,
            state="coding",
            current_step="code",
            stack=[
                {"workflow": "completer", "step": "gate", "called_from": None},
                {"workflow": "build", "step": "code", "called_from": "gate"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._unwind_terminate(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete", (
            f"a terminate → complete catch must end the task, not leave it working; status={row.status!r}"
        )
        assert row.state == "complete", f"the terminal state must be persisted; state={row.state!r}"
        assert row.current_step is None, (
            f"a terminal catch nulls current_step (matches every other terminal transition); got {row.current_step!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def _write_monitor_successor_caller(path: Path) -> Path:
    """A caller workflow whose call-site step (``work``) is followed by a MONITOR
    step (``watch``) in binding order — so a callee ``complete`` returns *into* a
    monitor, exercising the monitor-typed landing in ``_return_to_caller``."""
    path.write_text(
        """
process: monitor_caller
jobs:
  - name: work
    type: agent
    prompt: coding
    queue_state: working_state
    active_state: working_state
  - name: watch
    type: monitor
    engine: pr_monitor
    config:
      poll_interval_seconds: 30
      debounce_seconds: 120
      triggers: [human_comment]
      max_pr_fix_rounds: 10
      max_consecutive_skipped: 3
flows:
  main:
    steps: [work, watch]
"""
    )
    return path


def test_complete_returning_to_a_monitor_successor_parks_waiting_for_pr(tmp_path, run):
    """``complete`` returning to a caller whose next step is a MONITOR must land
    the task at ``status="waiting_for_pr"`` (so the monitor engine picks it up),
    with only the caller frame left on the stack.

    Coverage (not a failing-first regression — the behaviour is already correct):
    ``_return_to_caller`` lands the successor at ``working`` and delegates to
    ``_dispatch_next_step``, whose ``_dispatch_step`` monitor branch flips a
    monitor step to ``waiting_for_pr`` (and clears interruption markers). This
    pins that self-heal for a monitor successor — the previously-untested landing
    type the review flagged — so a future refactor that hardcodes the landing
    status (and would drift from the marker-clearing) can't silently regress it.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        caller = build_process(
            "monitor_caller", process_file=_write_monitor_successor_caller(tmp_path / "monitor_caller.yaml")
        )
        svc._processes["monitor_caller"] = caller
        watch = next(s for s in caller.flows["main"].jobs if s.name == "watch")
        task = _seed(
            svc,
            run,
            state="callee_active",
            current_step="last",
            stack=[
                {"workflow": "monitor_caller", "step": "work", "called_from": None},
                {"workflow": "pr-monitor", "step": "push_pr", "called_from": "work"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._return_to_caller(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "waiting_for_pr", (
            f"a monitor successor must park at waiting_for_pr for the engine; status={row.status!r}"
        )
        assert row.current_step == "watch", f"must land at the monitor successor; got {row.current_step!r}"
        assert row.state == watch.queue_state, f"state must be the monitor's queue_state; got {row.state!r}"
        assert _wf_names(row) == ["monitor_caller"], "only the caller frame remains after the pop"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def _write_monitor_terminate_catcher(path: Path) -> Path:
    """A catcher whose ``gate`` step catches ``terminate`` to a MONITOR step
    (``watch``) — exercising the monitor-typed catch landing in
    ``_unwind_terminate``."""
    path.write_text(
        """
process: monitor_catcher
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: gating
    active_state: gating
    routes: { terminate: watch }
  - name: watch
    type: monitor
    engine: pr_monitor
    config:
      poll_interval_seconds: 30
      debounce_seconds: 120
      triggers: [human_comment]
      max_pr_fix_rounds: 10
      max_consecutive_skipped: 3
flows:
  main:
    steps: [gate, watch]
"""
    )
    return path


def test_terminate_caught_by_a_monitor_step_parks_waiting_for_pr(tmp_path, run):
    """A ``terminate`` catch whose target is a MONITOR step lands the task at
    ``status="waiting_for_pr"`` (engine picks it up), with the catching frame
    left on the stack.

    Coverage (not a failing-first regression — the behaviour is already correct):
    the step-catch branch of ``_unwind_terminate`` lands the catch target at
    ``working`` and re-dispatches through ``_dispatch_next_step``, whose monitor
    branch flips it to ``waiting_for_pr``. Pins that self-heal for a monitor catch
    target — the previously-untested landing type the review flagged.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        catcher = build_process(
            "monitor_catcher", process_file=_write_monitor_terminate_catcher(tmp_path / "monitor_catcher.yaml")
        )
        svc._processes["monitor_catcher"] = catcher
        watch = next(s for s in catcher.flows["main"].jobs if s.name == "watch")
        task = _seed(
            svc,
            run,
            state="coding",
            current_step="code",
            stack=[
                {"workflow": "monitor_catcher", "step": "gate", "called_from": None},
                {"workflow": "build", "step": "code", "called_from": "gate"},
            ],
        )
        row = run(svc.db.get_task(task.id))
        run(svc._unwind_terminate(_item_from(row), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "waiting_for_pr", (
            f"a monitor catch target must park at waiting_for_pr; status={row.status!r}"
        )
        assert row.current_step == "watch", f"must land at the monitor catch target; got {row.current_step!r}"
        assert row.state == watch.queue_state, f"state must be the monitor's queue_state; got {row.state!r}"
        assert _wf_names(row) == ["monitor_catcher"], "the catching frame stays; the emitting frame is unwound"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_route_stack_target_dispatches_a_call(tmp_path, run):
    """``_route_stack_target`` (the single seam the three finalize sites share)
    treats a ``call <workflow>`` target as a stack push + callee dispatch — the
    caller frame stays and a callee frame is pushed on top.

    Fails pre-fix: ``_route_stack_target`` does not exist; only ``_execute_action_step``
    handled ``call``, so an agent step routing to ``call`` had no dispatch path.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state="coding",
            current_step="code",
            stack=[{"workflow": "build", "step": "code", "called_from": None}],
        )
        row = run(svc.db.get_task(task.id))
        handled = run(svc._route_stack_target(_item_from(row), "call pr-monitor", from_status="working"))
        assert handled is True, "a call target must be handled by the stack router"
        row = run(svc.db.get_task(task.id))
        assert _wf_names(row) == ["build", "pr-monitor"], f"the callee frame must be pushed; stack={_wf_names(row)!r}"
        top = row.metadata["call_stack"][-1]
        assert top["called_from"] == "code", "the pushed frame records the calling step"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_agent_step_routing_to_call_enters_the_callee(tmp_path, run):
    """End-to-end: an AGENT step whose ``COMPLETED`` routes to ``call pr-monitor``
    actually enters ``pr-monitor`` through the completion drainer — it does not
    stall at ``status="working"``.

    This is the review's headline gap: pre-fix, the drainer resolved
    ``target="call pr-monitor"``, found no ``(state, "call pr-monitor")`` SM edge,
    logged the strand-warning, and ``continue``d — the task never moved and the
    stack never grew. Post-fix the drainer routes the target through
    ``_route_stack_target`` before the edge check.
    """
    import asyncio

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    caller = tmp_path / "wf_caller.yaml"
    caller.write_text(
        """
process: wf_caller
jobs:
  - name: work
    type: agent
    prompt: coding
    queue_state: backlog
    active_state: working_state
    routes: { COMPLETED: call pr-monitor }
flows:
  main:
    steps:
      - name: work
        prehooks: []
        posthooks: []
"""
    )
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        flow="wf_caller",
        flow_file=caller,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _CompletedRunner()
    run(svc.start())
    try:
        task = run(svc.create_task("agent calls a workflow"))

        async def _entered() -> bool:
            row = await db.get_task(task.id)
            return bool(row) and _wf_names(row)[-1:] == ["pr-monitor"]

        async def _drive() -> None:
            for _ in range(150):
                if await _entered():
                    return
                await asyncio.sleep(0.02)

        run(_drive())
        row = run(db.get_task(task.id))
        assert _wf_names(row) == ["wf_caller", "pr-monitor"], (
            f"the agent's COMPLETED must call pr-monitor (drainer wiring); stack={_wf_names(row)!r}, "
            f"status={row.status!r}, state={row.state!r}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


# ===========================================================================
# M. Return/catch resolve the call site from the popped frame's ``called_from``
#
# The frame's ``step`` is the step it was PUSHED at (its entry) and is never
# advanced as the frame moves through its flow. So a ``call`` issued from a
# non-entry step (``build``'s ``push_pr``, the real bundled case) leaves the
# caller frame's ``step`` stale. ``_return_to_caller`` / ``_unwind_terminate``
# must therefore locate the call site via the POPPED frame's ``called_from``
# (recorded at call time by ``_dispatch_call``), not the caller's ``step``.
#
# The section-L tests above hand-built stacks where ``caller.step`` already
# equalled the call site, so they never exercised this gap. These two drive the
# REAL ``_dispatch_call`` push from a mid-flow step, so the caller frame's
# ``step`` and the true call site genuinely differ.
# ===========================================================================


def _write_outer_inner(tmp_path: Path) -> tuple[Path, Path]:
    """A three-step ``outer`` workflow that calls a one-step ``inner`` from its
    MIDDLE step (``mid``), and ``mid`` also declares a ``terminate: blocked``
    catch. ``outer``'s root frame is pushed at ``first`` (its entry) — so once a
    call is issued from ``mid``, the frame's ``step`` ("first") differs from the
    true call site ("mid")."""
    outer = tmp_path / "outer.yaml"
    outer.write_text(
        """
process: outer
jobs:
  - name: first
    type: agent
    prompt: coding
    queue_state: first_queue
    active_state: first_active
  - name: mid
    type: agent
    prompt: coding
    queue_state: mid_queue
    active_state: mid_active
    routes: { COMPLETED: call inner, terminate: blocked }
  - name: last
    type: agent
    prompt: coding
    queue_state: last_queue
    active_state: last_active
flows:
  main:
    steps: [first, mid, last]
"""
    )
    inner = tmp_path / "inner.yaml"
    inner.write_text(
        """
process: inner
jobs:
  - name: work
    type: agent
    prompt: coding
    queue_state: work_queue
    active_state: work_active
flows:
  main:
    steps: [work]
"""
    )
    return outer, inner


def test_complete_after_real_dispatch_call_returns_after_the_call_site(tmp_path, run):
    """``complete`` returns to the step AFTER the REAL call site, resolved from the
    popped frame's ``called_from`` — not the caller frame's stale entry ``step``.

    Drives the real ``_dispatch_call`` from ``outer``'s mid-flow ``mid`` step
    (root frame still pushed at ``first``), then completes the callee. The task
    must resume at ``last`` (the step after ``mid``).

    Fails pre-fix: ``_return_to_caller`` read the caller frame's ``step`` ("first")
    and resumed at ``mid`` (the step after ``first``) — silently re-running the
    call site instead of advancing past it.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    svc.runner = _HangRunner()  # callee entry + resumed caller step hang → row stays put
    outer_path, inner_path = _write_outer_inner(tmp_path)
    run(svc.start())
    try:
        svc._processes["outer"] = build_process("outer", process_file=outer_path)
        svc._processes["inner"] = build_process("inner", process_file=inner_path)
        # Caller parked mid-flow at ``mid``; its frame still carries the stale
        # entry step ``first`` (frames are never refreshed on step advance).
        task = _seed(
            svc,
            run,
            state="mid_active",
            current_step="mid",
            stack=[{"workflow": "outer", "step": "first", "called_from": None}],
        )
        row = run(svc.db.get_task(task.id))
        assert run(svc._dispatch_call(row, "inner")) is True
        pushed = run(svc.db.get_task(task.id))
        # ``_dispatch_call`` records the REAL call site on the callee frame...
        assert pushed.metadata["call_stack"][-1]["called_from"] == "mid"
        # ...and does NOT rewrite the caller frame's stale entry step.
        assert pushed.metadata["call_stack"][0]["step"] == "first"

        # The callee completes → return to the caller AFTER the real call site.
        run(svc._return_to_caller(_item_from(pushed), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert _wf_names(row) == ["outer"], f"only the caller frame remains; stack={row.metadata.get('call_stack')!r}"
        assert row.current_step == "last", (
            "complete must resume after the real call site 'mid' (→ 'last'); pre-fix it read the "
            f"caller frame's stale entry step 'first' and resumed at 'mid'. got {row.current_step!r}"
        )
        assert row.status != "complete", "a mid-stack complete returns to the caller, it does not end the task"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_terminate_after_real_dispatch_call_catches_at_the_real_call_site(tmp_path, run):
    """A ``terminate`` catch is looked up on the REAL call-site step (via the
    popped frame's ``called_from``), not the caller frame's stale entry ``step``.

    ``outer``'s ``mid`` declares ``routes: { terminate: blocked }``. After a real
    ``_dispatch_call`` from ``mid``, an uncaught callee ``terminate`` must be
    caught at ``mid`` → the task blocks there.

    Fails pre-fix: the catch lookup used the caller frame's ``step`` ("first",
    which has no catch), so the terminate propagated to the root and ended the
    task instead of being caught.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    svc.runner = _HangRunner()  # callee entry hangs → row stays put
    outer_path, inner_path = _write_outer_inner(tmp_path)
    run(svc.start())
    try:
        svc._processes["outer"] = build_process("outer", process_file=outer_path)
        svc._processes["inner"] = build_process("inner", process_file=inner_path)
        task = _seed(
            svc,
            run,
            state="mid_active",
            current_step="mid",
            stack=[{"workflow": "outer", "step": "first", "called_from": None}],
        )
        row = run(svc.db.get_task(task.id))
        assert run(svc._dispatch_call(row, "inner")) is True
        pushed = run(svc.db.get_task(task.id))
        assert pushed.metadata["call_stack"][-1]["called_from"] == "mid"

        # The callee terminates → the catch declared on the call site ``mid`` fires.
        run(svc._unwind_terminate(_item_from(pushed), from_status="working"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "blocked", (
            "the terminate catch on the call-site step 'mid' must fire; pre-fix the lookup used the "
            f"caller's stale entry step 'first' (no catch) and unwound to a terminal completion. status={row.status!r}"
        )
        assert row.current_step == "mid", (
            f"a caught terminate → blocked keeps the call-site step; got {row.current_step!r}"
        )
        assert _wf_names(row) == ["outer"], "the caught frame stays on the stack (no further unwinding)"
    finally:
        run(svc.shutdown())
        run(svc.db.close())
