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
