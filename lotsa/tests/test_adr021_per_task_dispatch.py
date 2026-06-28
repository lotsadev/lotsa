"""Failing-first tests for ADR-021 — per-task process dispatch.

These pin the behaviour the ADR-021 implementation must deliver:

* every routing decision resolves from the *task's own* process
  (``metadata['process_name']``), not the orchestrator's active-process
  singletons;
* ``_resolve_flow`` resolves ``current_flow`` against the task's process;
* the derived singletons (``_action_states`` / ``_monitor_state`` /
  ``_pr_monitor`` / ``_pr_monitor_config``) are decomposed into
  per-process collections;
* the restart recovery sweep routes each persisted row against its own
  process's action states (the keystone legacy/mixed-population
  regression);
* ``create_task`` accepts any loaded process (``ProcessNotActive`` is
  removed; only ``ProcessNotFound`` survives);
* one engine poll task per monitor-bearing process.

Written before the implementation lands, so they are expected to FAIL
against pre-ADR-021 code. The shared multi-process harness lives in
``test_orchestrator_typed_jobs`` and is reused here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from lotsa.tests.conftest import wait_for_status
from lotsa.tests.test_orchestrator_typed_jobs import (
    _FakeRunner,
    _make_service,
    _make_service_with_inline_processes,
    _register_capture_engine,
    _register_capture_tool,
)
from rigg.models import Item

# ``run`` / ``_loop`` fixtures come from ``lotsa/tests/conftest.py``.


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _two_process_service(tmp_path: Path, run) -> OrchestratorService:
    """Two agent-only inline processes: ``feature_flow`` (default/active) and
    ``bug_flow`` (loaded but non-active).

    Their first-step prompts differ (``spec_feat`` vs ``triage_bug``) so a
    routing decision that consults the *wrong* process is observable in the
    dispatched prompt and the resolved flow's job list.
    """
    return _make_service_with_inline_processes(
        tmp_path,
        run,
        processes={
            "feature_flow": {
                "default": True,
                "steps": [{"name": "spec_feat", "prompt": "spec_feat", "evaluate": True}],
            },
            "bug_flow": {
                "steps": [{"name": "triage_bug", "prompt": "triage_bug", "evaluate": True}],
            },
        },
        prompts=["spec_feat", "triage_bug"],
    )


def _make_deploy_proc_service(tmp_path: Path, run) -> OrchestratorService:
    """Active process (via ``--flow-file``) with a DISTINCT action state
    ``deploy`` (not in the legacy ``pushing``/``rebasing``/``waiting_for_pr``
    set), plus an agent-only inline ``notes`` process that has NO action
    states.

    This is the only configuration that lets the recovery sweep's per-process
    action-state routing diverge between two loaded processes: ``deploy`` is
    an action state of the active process but not of ``notes``.
    """
    _register_capture_tool()  # registers ``capture_call`` for the action job

    process_file = tmp_path / "deploy_proc.yaml"
    process_file.write_text(
        """
process: deploy_proc
jobs:
  - { name: code, type: agent, prompt: coding, queue_state: coding, active_state: coding }
  - { name: deploy, type: action, tool: capture_call }
flows:
  main:
    steps: [code, deploy]
"""
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user", "draft-system", "draft-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n")

    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=process_file,  # highest precedence → deploy_proc is active
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
        processes={"notes": {"steps": [{"name": "draft", "prompt": "draft"}]}},
        config_path=tmp_path / "lotsa.yaml",
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner()
    return svc


# ---------------------------------------------------------------------------
# create_task accepts any loaded process (R5 / AC-3, AC-1)
# ---------------------------------------------------------------------------


def test_process_not_active_class_removed():
    """``ProcessNotActive`` is deleted — any loaded process is dispatchable.

    Fails pre-fix: the class still exists in ``lotsa.orchestrator``.
    """
    import lotsa.orchestrator as orch

    assert not hasattr(orch, "ProcessNotActive"), (
        "ADR-021 removes ProcessNotActive entirely; create_task accepts any "
        "loaded process and only ProcessNotFound remains."
    )


def test_create_task_accepts_non_active_process_and_dispatches_its_flow(tmp_path, run):
    """``create_task(process=<loaded non-active>)`` succeeds and dispatches the
    chosen process's flow — no restart.

    Fails pre-fix: ``create_task`` raises ``ProcessNotActive`` for any name
    other than the active process.
    """
    svc = _two_process_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(svc.create_task(title="bug", process_name="bug_flow"))
        fresh = run(svc.db.get_task(task.id))
        assert fresh.metadata.get("process_name") == "bug_flow"
        assert fresh.flow_name == "bug_flow"

        # The dispatched step is bug_flow's, not the active feature_flow's.
        run(wait_for_status(svc, task.id, "waiting"))
        system_prompts = [c.get("system", "") for c in svc.runner.calls]
        assert any("triage_bug-system" in p for p in system_prompts), (
            "create_task(process='bug_flow') must dispatch bug_flow's first "
            f"step (prompt 'triage_bug'); dispatched prompts were {system_prompts!r}"
        )
        assert not any("spec_feat-system" in p for p in system_prompts), (
            "It must NOT dispatch the active feature_flow's step."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_create_task_unknown_process_raises_process_not_found(tmp_path, run):
    """An unknown process name still raises ``ProcessNotFound`` (the only
    remaining error path)."""
    from lotsa.orchestrator import ProcessNotFound

    svc = _two_process_service(tmp_path, run)
    run(svc.start())
    try:
        with pytest.raises(ProcessNotFound):
            run(svc.create_task(title="x", process_name="no_such_process"))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Per-task flow resolution (R1)
# ---------------------------------------------------------------------------


def test_resolve_flow_uses_tasks_own_process_root_flow(tmp_path, run):
    """``_resolve_flow`` resolves against the task's process, not the active
    one, even with no ``current_flow`` recorded.

    Fails pre-fix: ``_resolve_flow`` returns ``self.flow`` (the active
    process's root flow) when ``current_flow`` is absent.
    """
    svc = _two_process_service(tmp_path, run)
    run(svc.start())
    try:
        item = Item(id="t1", state="backlog", metadata={"process_name": "bug_flow"})
        flow = svc._resolve_flow(item)
        assert [j.name for j in flow.jobs] == ["triage_bug"], (
            "A task owned by bug_flow must resolve to bug_flow's root flow, "
            f"not the active feature_flow; got jobs {[j.name for j in flow.jobs]!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Per-task state-machine validity (R1 / AC-2)
# ---------------------------------------------------------------------------


def test_block_validates_against_tasks_own_state_machine(tmp_path, run):
    """``block()`` validates the (state, 'blocked') edge against the task's own
    process state machine.

    A bug_flow task sitting at bug_flow's ``triage_bug`` active state can be
    blocked because bug_flow's SM has that edge — even though the active
    feature_flow SM has no such state.

    Fails pre-fix: ``block()`` checks ``self.flow.state_machine`` (the active
    root flow), which lacks ``triage_bug``, so the call no-ops and the task
    stays ``working``.
    """
    svc = _two_process_service(tmp_path, run)
    run(svc.start())
    try:
        bug_flow = svc._processes["bug_flow"].flows["main"]
        triage = next(j for j in bug_flow.jobs if j.name == "triage_bug")
        # Sanity: the edge exists in bug_flow's SM but the state is unknown to
        # the active feature_flow's SM (cross-process isolation).
        assert (triage.active_state, "blocked") in bug_flow.state_machine.transitions
        assert (triage.active_state, "blocked") not in svc.flow.state_machine.transitions

        task = run(
            svc.db.create_task(
                "bug",
                state=triage.active_state,
                status="working",
                metadata={"process_name": "bug_flow"},
            )
        )
        run(svc.block(task.id))
        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "blocked", (
            "block() must validate against the task's own process SM. Pre-fix "
            "it checks the active flow's SM, which lacks bug_flow's state, so "
            "the block silently no-ops and the task is stranded at 'working'."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Decomposed singletons → per-process collections (R2)
# ---------------------------------------------------------------------------


def test_action_states_collected_per_process(tmp_path, run):
    """``_action_states`` is replaced by ``_action_states_by_process`` keyed by
    process name; each process gets only its own action states.

    Fails pre-fix: ``_action_states_by_process`` does not exist and the
    singleton ``_action_states`` is still present.
    """
    svc = _make_deploy_proc_service(tmp_path, run)
    run(svc.start())
    try:
        active = svc._active_process_name
        assert svc._action_states_by_process[active] == {"deploy"}
        # The agent-only inline process has no action states of its own.
        assert svc._action_states_by_process["notes"] == set()
        assert not hasattr(svc, "_action_states"), "ADR-021 removes the derived singleton; no backward-compat shim."
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_monitor_state_and_engine_tracked_per_process(tmp_path, run):
    """The monitor-state and engine singletons become per-process dicts, and a
    poll task is tracked per monitor-bearing process.

    Fails pre-fix: ``_monitor_states_by_process`` / ``_pr_monitors_by_process``
    / ``_pr_monitor_tasks_by_process`` do not exist; the orchestrator holds a
    single ``_pr_monitor``.
    """
    _register_capture_engine()
    _register_capture_tool()
    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        active = svc._active_process_name
        assert svc._monitor_states_by_process[active] == "watch"
        assert svc._pr_monitors_by_process[active] is not None
        assert active in svc._pr_monitor_tasks_by_process
        assert not hasattr(svc, "_pr_monitor"), (
            "ADR-021 removes the single-engine singleton in favour of _pr_monitors_by_process."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_monitor_engine_resolves_for_tasks_process(tmp_path, run):
    """``_monitor_engine_for(task)`` returns the engine for the task's process,
    and falls back to the active process for legacy rows.

    Fails pre-fix: ``_monitor_engine_for`` does not exist.
    """
    _register_capture_engine()
    _register_capture_tool()
    svc = _make_service(tmp_path, run)
    run(svc.start())
    try:
        active = svc._active_process_name
        engine = svc._pr_monitors_by_process[active]

        owned = Item(id="a", state="watch", metadata={"process_name": active})
        assert svc._monitor_engine_for(owned) is engine

        legacy = Item(id="b", state="watch", metadata={})
        assert svc._monitor_engine_for(legacy) is engine, (
            "Legacy rows (no process_name) must resolve to the active process's engine."
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Restart recovery sweep — per-process action states (R4 / AC-5, AC-6)
# ---------------------------------------------------------------------------


def test_recovery_sweep_routes_row_by_its_own_process_action_states(tmp_path, run):
    """The keystone mixed-population regression.

    Seed a pre-ADR-021 row (no ``process_name``, mid-action at the active
    process's ``deploy`` state, ``status=working``) AND a post-ADR-021 row
    that belongs to the agent-only ``notes`` process but is recorded at the
    same ``deploy`` state with ``status=waiting``.

    On restart, the recovery sweep must look up EACH row's process:

    * the legacy row → active process → ``deploy`` is an action state →
      flipped to ``blocked`` (control; passes pre- and post-fix);
    * the ``notes`` row → ``notes`` has NO ``deploy`` action state and
      ``deploy`` is not a legacy push state → NOT flipped → stays ``waiting``.

    Fails pre-fix: the singleton ``_action_states`` holds the *active*
    process's action states ({'deploy'}), so the ``notes`` row is mis-routed —
    ``deploy`` is recognised as an action state regardless of the row's
    process, ``push_state`` is True, and the row is wrongly flipped to
    ``blocked``.

    (Observed pre-fix failure to record in the commit body:
    ``assert 'blocked' == 'waiting'`` on the notes row's status.)
    """
    svc = _make_deploy_proc_service(tmp_path, run)

    # Seed BEFORE start() so the recovery sweep encounters both rows.
    legacy_row = run(svc.db.create_task("legacy mid-deploy", state="deploy", status="working"))
    notes_row = run(
        svc.db.create_task(
            "notes task at deploy",
            state="deploy",
            status="waiting",
            metadata={"process_name": "notes"},
        )
    )

    run(svc.start())
    try:
        fresh_notes = run(svc.db.get_task(notes_row.id))
        assert fresh_notes.status == "waiting", (
            "A row owned by the agent-only 'notes' process must NOT be treated "
            "as mid-action just because the active process happens to call "
            "'deploy' an action state. Pre-fix the singleton action-state set "
            "mis-routes it and flips it to 'blocked'."
        )

        # Control: the legacy row routes against the active process and is
        # recovered as a mid-action restart (push-style message).
        fresh_legacy = run(svc.db.get_task(legacy_row.id))
        assert fresh_legacy.status == "blocked"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_recovery_sweep_isolates_per_row_failures(tmp_path, run):
    """One row's recovery failure must not strand later working tasks.

    Regression for the working-orphan that sat ``status="working"`` for ~11h
    (an internal task): the restart-recovery sweep had no per-row error isolation,
    so a single row that raised mid-sweep aborted the whole loop, leaving every
    later working task un-flipped (and therefore not retryable) until some
    *subsequent* restart happened to recover it.

    Two working rows are seeded; the recency-ordered sweep hits ``bad`` first
    (it's newer) and ``_set_status`` is stubbed to raise for it. ``good`` is
    ordered after, so pre-fix the abort leaves it ``working``; post-fix the
    sweep logs + continues and flips it to ``blocked``.
    """
    svc = _make_deploy_proc_service(tmp_path, run)
    # good first → bad newer, so bad sorts first in the DESC-by-recency sweep.
    good = run(svc.db.create_task("good working", state="coding", status="working"))
    bad = run(svc.db.create_task("bad working", state="coding", status="working"))

    orig_set = svc._set_status

    async def flaky_set_status(task_id, status, current_step):
        if task_id == bad.id:
            raise RuntimeError("simulated per-row recovery failure")
        return await orig_set(task_id, status, current_step)

    svc._set_status = flaky_set_status  # type: ignore[assignment]

    run(svc.start())
    try:
        assert run(svc.db.get_task(good.id)).status == "blocked", (
            "a later working row must still be recovered when an earlier row's "
            "recovery raises — the sweep must isolate per-row failures"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ---------------------------------------------------------------------------
# Two monitor-bearing processes — independent engines, no cross-process
# claim or mis-route (R3 / AC-4)
# ---------------------------------------------------------------------------


def _two_monitor_service(tmp_path: Path, run) -> OrchestratorService:
    """A service with TWO monitor-bearing processes, each scoped to its OWN
    monitor state.

    The active process (``proc_a``, via ``--flow-file``) ends in a monitor job
    ``watch_a``; a second monitor-bearing process (``proc_b``, monitor job
    ``watch_b``) is built from its own ``process.yaml`` and injected into the
    catalog before ``start()``. (Inline ``processes:`` entries are agent-only —
    ``build_process_from_inline`` rejects monitor jobs — so a second
    monitor-bearing process must come from a standalone file. ``start()`` ADDs
    to ``self._processes`` rather than clearing it, so the pre-seeded ``proc_b``
    survives and the per-process engine loop builds an engine for it too.)

    Distinct monitor states are the whole point: it lets us prove engine A
    (scoped to ``watch_a``) never claims a task owned by ``proc_b`` (sitting in
    ``watch_b``), and vice versa.
    """
    from lotsa.flows import build_process

    _register_capture_engine()
    _register_capture_tool()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user", "triage-system", "triage-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n")

    file_a = tmp_path / "proc_a.yaml"
    file_a.write_text(
        """
process: proc_a
jobs:
  - { name: code, type: agent, prompt: coding, queue_state: coding, active_state: coding }
  - { name: watch_a, type: monitor, engine: capture_engine }
flows:
  main:
    steps: [code, watch_a]
"""
    )
    file_b = tmp_path / "proc_b.yaml"
    file_b.write_text(
        """
process: proc_b
jobs:
  - { name: triage, type: agent, prompt: triage, queue_state: triage, active_state: triage }
  - { name: watch_b, type: monitor, engine: capture_engine }
flows:
  main:
    steps: [triage, watch_b]
"""
    )

    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=file_a,  # proc_a is the active process
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
    # Pre-seed the second monitor-bearing process so start()'s per-process
    # engine loop instantiates an engine for it alongside the active one.
    svc._processes["proc_b"] = build_process("proc_b", prompts_dir=prompts_dir, process_file=file_b)
    return svc


def test_two_monitor_processes_get_independent_engines_scoped_to_own_state(tmp_path, run):
    """Each monitor-bearing process gets its OWN engine instance + poll task,
    scoped to its OWN monitor state (R3 / AC-4).

    Fails pre-fix: the single ``_pr_monitor`` singleton holds one engine for the
    active process only; the second process's monitor state is never tracked and
    no second poll task is started.
    """
    svc = _two_monitor_service(tmp_path, run)
    run(svc.start())
    try:
        # Two distinct engine instances, two distinct monitor states.
        engine_a = svc._pr_monitors_by_process["proc_a"]
        engine_b = svc._pr_monitors_by_process["proc_b"]
        assert engine_a is not None and engine_b is not None
        assert engine_a is not engine_b, "Each process must get its own engine instance."
        assert svc._monitor_states_by_process["proc_a"] == "watch_a"
        assert svc._monitor_states_by_process["proc_b"] == "watch_b"
        assert engine_a.monitor_state == "watch_a"
        assert engine_b.monitor_state == "watch_b"

        # One poll task per monitor-bearing process.
        assert "proc_a" in svc._pr_monitor_tasks_by_process
        assert "proc_b" in svc._pr_monitor_tasks_by_process
        assert svc._pr_monitor_tasks_by_process["proc_a"] is not svc._pr_monitor_tasks_by_process["proc_b"]
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_engine_does_not_claim_or_misroute_other_processes_tasks(tmp_path, run):
    """The keystone two-monitor regression: an engine scoped to one process's
    monitor state must not claim, mis-route, or apply its config to a task owned
    by the other process (R3 / AC-4).

    Seeds one waiting task per process (each in its own process's monitor state)
    and drives the orchestrator's per-task routing helpers:

    * ``_monitor_engine_for`` / ``_monitor_state_for`` resolve each task to its
      OWN process's engine and state — no cross-process bleed.

    Note (ADR-030): ``PrMonitor._list_waiting_tasks`` no longer scopes by
    ``monitor_state`` — terminal handling must see PR-bearing tasks in any
    state, so both pollers now see both tasks. Cross-process protection
    therefore lives entirely in the per-task routing helpers exercised below
    (and, for feedback dispatch, in the ``observed_status == "waiting_for_pr"``
    gate inside ``_poll_one``), not in a list-time filter.

    Fails pre-ADR-021: with a single ``_pr_monitor`` and a global
    ``_monitor_state``, the routing helpers don't exist.
    """
    from lotsa.engines.pr_monitor import PrMonitorConfig as PrConfig
    from lotsa.pr_monitor import PrMonitor

    svc = _two_monitor_service(tmp_path, run)
    run(svc.start())
    try:
        task_a = run(
            svc.db.create_task(
                "owned by proc_a",
                state="watch_a",
                status="waiting_for_pr",
                metadata={"process_name": "proc_a", "pr_number": 1},
            )
        )
        task_b = run(
            svc.db.create_task(
                "owned by proc_b",
                state="watch_b",
                status="waiting_for_pr",
                metadata={"process_name": "proc_b", "pr_number": 2},
            )
        )

        # The orchestrator callback returns every non-terminal PR-bearing task
        # (ADR-030 discovery), regardless of which process's monitor parked it.
        waiting = run(svc.list_waiting_pr_tasks())
        assert {t["id"] for t in waiting} == {task_a.id, task_b.id}

        # ADR-030: ``_list_waiting_tasks`` is no longer monitor-state-scoped —
        # both pollers see both tasks. The cross-process guarantee is upheld by
        # the per-task routing helpers below, not by dropping rows at list time.
        cfg = PrConfig(triggers=["human_comment"])
        poller_a = PrMonitor(svc, cfg, monitor_state="watch_a")
        poller_b = PrMonitor(svc, cfg, monitor_state="watch_b")
        seen_a = run(poller_a._list_waiting_tasks())
        seen_b = run(poller_b._list_waiting_tasks())
        assert {t["id"] for t in seen_a} == {task_a.id, task_b.id}
        assert {t["id"] for t in seen_b} == {task_a.id, task_b.id}

        # Per-task routing helpers resolve each task to its OWN process — the
        # callback path (config/state lookup) never bleeds across processes.
        item_a = Item(id=task_a.id, state="watch_a", metadata={"process_name": "proc_a"})
        item_b = Item(id=task_b.id, state="watch_b", metadata={"process_name": "proc_b"})
        assert svc._monitor_engine_for(item_a) is svc._pr_monitors_by_process["proc_a"]
        assert svc._monitor_engine_for(item_b) is svc._pr_monitors_by_process["proc_b"]
        assert svc._monitor_engine_for(item_a) is not svc._monitor_engine_for(item_b)
        assert svc._monitor_state_for(item_a) == "watch_a"
        assert svc._monitor_state_for(item_b) == "watch_b"
    finally:
        run(svc.shutdown())
        run(svc.db.close())
