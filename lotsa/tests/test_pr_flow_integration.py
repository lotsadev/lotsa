"""End-to-end flow integration tests for the PR phase (ADR-014 Layer A).

Rewritten for the typed-job model: the PR phase is now expressed as a
``push_pr`` action job and a ``wait_for_pr_signal`` monitor job, with a
separate ``pr_fix`` sub-flow that the engine dispatches into. No
synthetic states, no ``pr_config`` attribute, no ``target: previous``.
"""

from __future__ import annotations

import yaml

from lotsa.flows import build_process

# The ``_isolated_registry`` autouse fixture lives in ``lotsa/tests/conftest.py``
# (uses the public ``registry.snapshot()`` / ``registry.restore()`` API). Tests
# in this module pop the built-in ``push_pr`` and register stubs inline; the
# fixture restores the built-in after each test.


def test_full_process_has_push_pr_action_and_wait_for_pr_signal_monitor():
    """The full process exposes the new typed PR-phase jobs."""
    process = build_process("build")
    push_pr = next(j for j in process.jobs if j.name == "push_pr")
    wait = next(j for j in process.jobs if j.name == "wait_for_pr_signal")
    assert push_pr.type == "action"
    assert push_pr.tool == "push_pr"
    assert wait.type == "monitor"
    assert wait.engine == "pr_monitor"


def test_full_process_main_flow_has_no_synthetic_pr_states():
    """The new state machine has no ``pushing``/``waiting_for_pr``/``rebasing``."""
    process = build_process("build")
    main = process.flows["main"]
    for synthetic in ("pushing", "waiting_for_pr", "rebasing"):
        assert synthetic not in main.state_machine.states, (
            f"{synthetic!r} should not appear in the new SM — it was a synthetic state"
        )


def test_full_process_main_review_fail_targets_code():
    """Per-flow override puts the autonomous code↔review loop in ``main``."""
    process = build_process("build")
    main = process.flows["main"]
    review_binding = next(b for b in main.bindings if b.name == "review")
    fail = next(r for r in (review_binding.rules or []) if "REVIEW_FAIL" in r.pattern)
    assert fail.target == "code"


def test_full_process_pr_fix_has_no_verify_step():
    """The pr_fix sub-flow skips verify (supersedes PR #62 stopgap)."""
    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    assert not any(b.name == "verify" for b in pr_fix.bindings)


def test_pr_fix_flow_skipped_targets_wait_for_pr_signal():
    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    pr_fix_binding = next(b for b in pr_fix.bindings if b.name == "pr-fix")
    skipped = next(r for r in (pr_fix_binding.rules or []) if "SKIPPED" in r.pattern)
    assert skipped.target == "wait_for_pr_signal"


def test_custom_process_with_monitor_job(tmp_path):
    """A custom YAML process with a monitor job builds without a ``pr:`` block."""
    process_file = tmp_path / "myproc.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "myproc",
                "jobs": [
                    {"name": "coding", "type": "agent", "prompt": "coding"},
                    {
                        "name": "watch",
                        "type": "monitor",
                        "engine": "pr_monitor",
                        "config": {"poll_interval_seconds": 30},
                    },
                ],
                "flows": {"main": {"steps": ["coding", "watch"]}},
            }
        )
    )
    process = build_process("myproc", process_file=process_file)
    assert "watch" in process.flows["main"].state_machine.states


def test_flow_without_monitor_has_no_pr_phase_states(tmp_path):
    """A process with no monitor job has no PR-phase states. ADR-043's bundled
    catalog (chat/build/fix) is either conversational or PR-terminated, so this
    is exercised with a minimal single-step inline process."""
    import yaml as _yaml

    process_file = tmp_path / "nomonitor.yaml"
    process_file.write_text(
        _yaml.dump(
            {
                "process": "nomonitor",
                "jobs": [{"name": "coding", "type": "agent", "prompt": "coding"}],
                "flows": {"main": {"steps": ["coding"]}},
            }
        )
    )
    process = build_process("nomonitor", process_file=process_file)
    main = process.flows["main"]
    for synthetic in ("pushing", "waiting_for_pr", "rebasing", "wait_for_pr_signal"):
        assert synthetic not in main.state_machine.states


def test_full_process_pr_fix_review_fail_targets_pr_fix():
    """Per-flow override: in pr_fix, review's REVIEW_FAIL loops back to pr-fix."""
    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    review_binding = next(b for b in pr_fix.bindings if b.name == "review")
    fail = next(r for r in (review_binding.rules or []) if "REVIEW_FAIL" in r.pattern)
    assert fail.target == "pr-fix"


def test_pr_fix_flow_pr_fix_active_to_wait_for_pr_signal_transition_registered():
    """SKIPPED routes ``pr-fix`` back into the monitor — the SM transition must exist."""
    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    pr_fix_step = next(rj for rj in pr_fix.jobs if rj.name == "pr-fix")
    assert (pr_fix_step.active_state, "wait_for_pr_signal") in pr_fix.state_machine.transitions


# ---------------------------------------------------------------------------
# Cross-flow edges registered on main's SM — the orchestrator uses a single
# ``self.flow.state_machine`` for every CAS check, so main must know about
# sub-flow entry / exit transitions or the dispatch CAS rejects them.
# ---------------------------------------------------------------------------


def test_main_flow_has_sub_flow_entry_edge_into_pr_fix():
    """``_dispatch_pr_fix_locked`` CAS's into pr-fix's queue_state against main's SM.

    Without the (wait_for_pr_signal, pr-fixing) edge, ``_dispatch_step``'s
    pre-CAS guard rejects the dispatch and the task stalls in ``working``.
    """
    process = build_process("build")
    main = process.flows["main"]
    pr_fix_step = next(rj for rj in process.flows["pr_fix"].jobs if rj.name == "pr-fix")
    assert (
        "wait_for_pr_signal",
        pr_fix_step.queue_state,
    ) in main.state_machine.transitions, (
        "Main's SM must contain the (monitor_state, pr_fix_queue_state) sub-flow entry edge."
    )


def test_main_flow_has_sub_flow_exit_edges_back_through_pr_fix_rules():
    """PR_FIX_SKIPPED / PR_FIX_DONE land on main's SM via ``self.flow``.

    The drainer evaluates pr-fix's rules and CAS's against ``self.flow`` (= main).
    Each rule target that names a main-flow job needs the corresponding edge
    registered in main's SM, even though the rules live in the pr_fix sub-flow.
    """
    process = build_process("build")
    main = process.flows["main"]
    pr_fix_step = next(rj for rj in process.flows["pr_fix"].jobs if rj.name == "pr-fix")
    # PR_FIX_SKIPPED → wait_for_pr_signal
    assert (pr_fix_step.active_state, "wait_for_pr_signal") in main.state_machine.transitions
    # PR_FIX_DONE → review
    review = next(rj for rj in main.jobs if rj.name == "review")
    assert (pr_fix_step.active_state, review.queue_state) in main.state_machine.transitions
    # PR_FIX_BLOCKED → blocked
    assert (pr_fix_step.active_state, "blocked") in main.state_machine.transitions


def test_pr_fix_flow_has_sub_flow_entry_edge_from_monitor():
    """The (wait_for_pr_signal, pr-fixing) entry edge must be present in BOTH
    main's SM AND pr_fix's SM.

    Regression for the silent-no-op bug: ``_dispatch_pr_fix_locked`` merges
    ``current_flow="pr_fix"`` into the task's metadata before calling
    ``_dispatch_step``. ``_dispatch_step`` then resolves ``active_flow`` via
    ``_resolve_flow(item)`` — which reads that metadata and returns the
    pr_fix FlowConfig — and validates the pre-CAS transition against
    ``pr_fix.state_machine.transitions``. If the entry edge is only
    registered in main's SM, the guard fires and the dispatch silently
    returns. The task is stranded at ``(status=working, state=wait_for_pr_signal)``
    until the next server restart flips it to blocked. The companion runtime
    test below exercises the full path.
    """
    process = build_process("build")
    pr_fix_first = next(rj for rj in process.flows["pr_fix"].jobs if rj.name == "pr-fix")
    assert (
        "wait_for_pr_signal",
        pr_fix_first.queue_state,
    ) in process.flows["pr_fix"].state_machine.transitions, (
        "pr_fix's SM must contain the sub-flow entry edge — _dispatch_step "
        "validates against the resolved active flow, which is pr_fix after "
        "_dispatch_pr_fix_locked merges current_flow into metadata."
    )


def test_dispatch_pr_fix_transitions_task_to_pr_fixing(tmp_path):
    """Companion runtime test for the entry-edge cross-flow registration.

    Stages a task at ``(state=wait_for_pr_signal, status=waiting_for_pr)`` on
    the bundled ``full`` process, then drives ``svc.dispatch_pr_fix(task_id,
    "feedback")``. With the entry edge registered in BOTH SMs the dispatch
    proceeds and ``_dispatch_step`` CAS's the row to
    ``(state=pr-fixing, status=working)``. Without it (the pre-fix shape) the
    pre-CAS guard rejects the transition and the dispatch silently returns,
    leaving the task at ``state=wait_for_pr_signal`` — which this test asserts
    against.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _hang = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Stage a task as if the pr_monitor engine had just landed it
                # in waiting_for_pr at the monitor's queue_state.
                task = run(
                    svc.db.create_task(
                        "PR fix dispatch test",
                        state="wait_for_pr_signal",
                        status="waiting_for_pr",
                        metadata={"pr_number": 1},
                    )
                )
                # Seed the spec/plan artifacts pr-fix declares as inputs, so
                # the missing-artifact branch in _dispatch_step doesn't
                # short-circuit before the entry-edge CAS we're testing.
                run(
                    svc.db.add_message(
                        task.id,
                        "agent",
                        "spec",
                        "spec content",
                        "artifact",
                        metadata={"artifact_name": "spec"},
                    )
                )
                run(
                    svc.db.add_message(
                        task.id,
                        "agent",
                        "plan",
                        "plan content",
                        "artifact",
                        metadata={"artifact_name": "plan"},
                    )
                )

                dispatched = run(svc.dispatch_pr_fix(task.id, "fix the lint errors"))
                assert dispatched, (
                    "dispatch_pr_fix returned False — the locked body declined. "
                    "Most likely the pre-CAS guard in _dispatch_step rejected "
                    "(wait_for_pr_signal, pr-fixing) against pr_fix's SM. The "
                    "entry edge must be registered in BOTH main's SM (CAS source) "
                    "and pr_fix's SM (post-merge dispatch validation)."
                )

                row = run(svc.db.get_task(task.id))
                assert row.state == "pr-fixing", (
                    f"Expected dispatch to CAS state→pr-fixing, got state={row.state!r}. "
                    "Regression: _dispatch_pr_fix_locked merged current_flow=pr_fix "
                    "into metadata, then _dispatch_step resolved active_flow=pr_fix and "
                    "found no (wait_for_pr_signal, pr-fixing) edge there, so the "
                    "transition silently no-op'd."
                )
                assert row.status == "working"
                assert row.metadata.get("current_flow") == "pr_fix"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Regression: ``block()`` and ``jump_to_step()`` must drop the PrMonitor's
# in-memory ``_tracked`` entry when the task leaves the monitor state by a
# non-engine path. The pre-fix code checked ``task.state == "waiting_for_pr"``
# (the legacy synthetic state name) — for tasks running the new ``full``
# process the live monitor state is ``wait_for_pr_signal``, so the untrack
# never fired and the ``_tracked`` entry leaked, leaving a stale
# ``comments_since`` cursor / ``consecutive_failures`` counter for any later
# re-entry into ``waiting_for_pr``.
# ---------------------------------------------------------------------------


def test_block_untracks_pr_monitor_for_new_monitor_state(tmp_path):
    """``block()`` must call ``_pr_monitor.untrack`` when leaving the new
    ``wait_for_pr_signal`` monitor state, not just the legacy
    ``"waiting_for_pr"`` synthetic.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _hang = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Confirm fixture parity with the production setup: the
                # bundled ``full`` process names its monitor state
                # ``wait_for_pr_signal``. If this changes the test must
                # change with it.
                active = svc._active_process_name
                assert svc._monitor_states_by_process[active] == "wait_for_pr_signal"

                # Stand up a fake PrMonitor that records untrack calls. The
                # real PrMonitor's polling loop isn't relevant here — we
                # only care that block() routes through ``untrack``. ADR-021:
                # the engine is keyed per-process, resolved by the task's
                # process at block() time.
                untracked: list[str] = []

                class _RecordingMonitor:
                    def untrack(self, task_id: str) -> None:
                        untracked.append(task_id)

                svc._pr_monitors_by_process[active] = _RecordingMonitor()  # type: ignore[assignment]

                task = run(
                    svc.db.create_task(
                        "blocked from monitor state",
                        state="wait_for_pr_signal",
                        status="waiting_for_pr",
                        metadata={"pr_number": 1},
                    )
                )
                run(svc.block(task.id))
                assert untracked == [task.id], (
                    "block() must call _pr_monitor.untrack when the task "
                    "leaves the new monitor state (wait_for_pr_signal). "
                    "Pre-fix, the literal 'waiting_for_pr' check never "
                    "matched and the _tracked entry leaked."
                )
                # Note: the legacy ``"waiting_for_pr"`` fallback in the
                # check expression remains as a defensive guard for
                # mid-rollout data, but the bundled ``full`` process SM
                # has no ``"waiting_for_pr"`` state, so block() short-
                # circuits before reaching the untrack call for any
                # task persisted under the old name. The fallback only
                # becomes exercisable if a process YAML explicitly
                # registers the legacy state — out of scope here.
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_start_instantiates_monitor_engine_via_registry(tmp_path):
    """``start()`` must instantiate the monitor engine via the registry —
    ``get_engine(job.engine)`` — and thread the queue_state through as the
    engine's ``monitor_state``.

    The full process names its monitor job ``wait_for_pr_signal`` with
    ``engine: pr_monitor``. After ``start()`` the engine instance must:
      - be a ``PrMonitorEngine`` (looked up by name in the registry, not
        hardcoded);
      - carry ``monitor_state="wait_for_pr_signal"`` so its poller scopes
        its waiting-task filter correctly;
      - propagate the same value down to the wrapped ``PrMonitor`` so the
        client-side filter actually fires at the polling site.

    Pre-fix the engine wrapper existed but was never instantiated by
    ``start()`` — the orchestrator went direct to ``PrMonitor(...)``,
    bypassing the registry entirely and preventing any custom engine from
    ever running.
    """
    import asyncio

    from lotsa.engines.pr_monitor import PrMonitorEngine

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _hang = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                active = svc._active_process_name
                assert svc._monitor_states_by_process[active] == "wait_for_pr_signal"
                engine = svc._pr_monitors_by_process[active]
                assert isinstance(engine, PrMonitorEngine), (
                    "start() must instantiate the engine via get_engine(job.engine), "
                    "not via a hardcoded PrMonitor() call. Pre-fix the orchestrator "
                    "imported PrMonitor directly and the registry's engine wrapper "
                    "was shelf-ware."
                )
                assert engine.monitor_state == "wait_for_pr_signal"
                # Confirm the value reaches the wrapped poller too — the
                # client-side filter (PrMonitor._list_waiting_tasks) reads
                # the underscore-prefixed attribute, so a missed thread
                # would still surface as None there even if the engine's
                # public field looked right.
                assert engine._monitor._monitor_state == "wait_for_pr_signal"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_transition_task_legacy_waiting_for_pr_state_completes_on_merge(tmp_path, caplog):
    """ADR-030: a legacy task at ``state="waiting_for_pr"`` (a synthetic state the
    bundled SM no longer carries) COMPLETES on merge — it is not stranded.

    Pre-ADR-030 this case log-and-returned because terminal completion was
    gated on a ``(state, target)`` SM edge that doesn't exist for the synthetic
    state — leaving the task stuck with the engine polling it forever (the
    fallback was itself the bug). ADR-030 makes terminal PR outcomes bypass the
    edge gate (a merge is global truth, not a flow transition), so the legacy
    row now completes and no "no transition" warning is emitted.
    """
    import asyncio
    import logging

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _hang = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = run(
                    db.create_task(
                        "legacy waiting_for_pr task",
                        # Force the synthetic state the bundled SM no longer carries.
                        state="waiting_for_pr",
                        status="waiting_for_pr",
                        metadata={"pr_number": 1},
                    )
                )
                assert ("waiting_for_pr", "complete") not in svc.flow.state_machine.transitions
                caplog.set_level(logging.WARNING, logger="lotsa.orchestrator")
                # Must not raise.
                run(svc.transition_task(task.id, "complete"))
                # ADR-030: the merge completes the task despite the missing edge.
                fresh = run(db.get_task(task.id))
                assert fresh.state == "complete"
                assert fresh.status == "complete"
                # No "no transition" warning — terminal completion isn't edge-gated.
                assert not any(
                    "no ('waiting_for_pr', 'complete') transition" in rec.getMessage() for rec in caplog.records
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Runtime: ``current_flow`` metadata drives step lookup so sub-flow rule
# overrides take effect at dispatch time. Regression for the PR #65 review
# finding that ``_dispatch_next_step``'s ``find_step(self.flow, ...)`` always
# picked the root flow's review binding (REVIEW_FAIL → code), breaking the
# pr_fix → review → pr-fix cycle that was meant to replace ``target: previous``.
# ---------------------------------------------------------------------------


def test_resolve_flow_returns_subflow_when_metadata_set(tmp_path):
    """``_resolve_flow(item)`` honours the ``current_flow`` metadata field."""
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from rigg.models import Item

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        # Use the bundled "full" process — it ships both ``main`` and
        # ``pr_fix`` flows with different ``review`` bindings.
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        try:
            run(svc.start())
            try:
                # Item without metadata → root flow
                item_main = Item(id="t1", state="reviewing", title="x")
                assert svc._resolve_flow(item_main).name == "main"

                # Item with ``current_flow=pr_fix`` → pr_fix flow
                item_subflow = Item(
                    id="t2",
                    state="reviewing",
                    title="x",
                    metadata={"current_flow": "pr_fix"},
                )
                assert svc._resolve_flow(item_subflow).name == "pr_fix"

                # Unknown flow name → falls back to root flow rather than crashing
                item_bogus = Item(
                    id="t3",
                    state="reviewing",
                    title="x",
                    metadata={"current_flow": "no_such_flow"},
                )
                assert svc._resolve_flow(item_bogus).name == "main"

                # The two flows' review bindings have different rules — the
                # whole point of the override. Confirm the lookup returns
                # bindings that actually carry the expected per-flow rules.
                main_review = svc._find_step_for_state("reviewing", item=item_main)
                assert main_review is not None
                main_fail = next(r for r in main_review.rules if "REVIEW_FAIL" in r.pattern)
                assert main_fail.target == "code", (
                    "root-flow lookup must return main's review binding (REVIEW_FAIL → code)"
                )

                subflow_review = svc._find_step_for_state("reviewing", item=item_subflow)
                assert subflow_review is not None
                subflow_fail = next(r for r in subflow_review.rules if "REVIEW_FAIL" in r.pattern)
                assert subflow_fail.target == "pr-fix", (
                    "pr_fix sub-flow lookup must return pr_fix's review binding "
                    "(REVIEW_FAIL → pr-fix). Regression for PR #65 review: a task "
                    "mid-pr_fix-cycle was dispatching main's review and looping "
                    "back into ``code`` instead of ``pr-fix``."
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Runtime: pr_fix happy path end-to-end. Drives a task through the canonical
# sub-flow round-trip (pr-fix → review → push_pr → wait_for_pr_signal) and
# asserts the task lands back in the host flow's monitor state with
# ``current_flow`` reset to ``main``. Regression for two bugs where the
# orchestrator validated transitions against the root flow's SM and rejected
# legitimate sub-flow edges (REVIEW_PASS → push_pr, push_pr → wait_for_pr_signal),
# stranding the task at ``status=working`` with no advance.
# ---------------------------------------------------------------------------


def test_pr_fix_sub_flow_review_pass_advances_to_push_pr(tmp_path):
    """In pr_fix, the drainer's REVIEW_PASS routing must land on push_pr,
    not be rejected by main's SM.

    The pre-fix bug: ``self.flow.state_machine.transitions`` (main's SM) has
    no ``(reviewing, push_pr)`` edge because in main, review's next is
    ``verify``. The drainer's rule-target CAS check rejected the transition
    and continued, stranding the task at ``(state=reviewing, status=working)``.
    The fix validates against the active flow's SM via ``_resolve_flow(item)``.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import InFlightStep, OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult
    from rigg.models import AgentResult, Item

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        # Once the drainer auto-advances state to ``push_pr``, it calls
        # ``_dispatch_next_step`` which immediately dispatches the push_pr
        # action. The built-in ``push_pr`` tool then runs the real
        # ``execute_push`` helper against ``work_dir`` — which is not a git
        # repo in CI's tmpdir — and fails, cascading the task to ``blocked``
        # before this test's assertion runs. Locally the test passes only by
        # timing luck (the cascade hasn't completed yet). Replace the tool
        # with a stub that hangs on a never-set ``asyncio.Event`` so the
        # action task freezes at ``state=push_pr`` and the assertion observes
        # the rule-target CAS landing site, not the downstream failure.
        # The autouse ``_isolated_registry`` fixture restores the built-in
        # ``push_pr`` after the test — just pop and re-register here.
        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()  # never set

        async def stub_push_pr(ctx, config):
            await hang_event.wait()
            return ToolResult(success=True, output="never reached", metadata={})

        register_tool("push_pr", stub_push_pr)

        try:
            run(svc.start())
            try:
                # Stage a task mid-pr_fix, sitting at reviewing/working as if
                # the pr_fix review agent has just finished.
                task = run(svc.db.create_task("Test", state="reviewing", metadata={"current_flow": "pr_fix"}))
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="reviewing",
                        to_status="working",
                        to_current_step="review",
                    )
                )
                item = Item(id=task.id, state="reviewing", title="Test", metadata={"current_flow": "pr_fix"})
                review_step = next(rj for rj in svc.process.flows["pr_fix"].jobs if rj.name == "review")

                # Synthesise the drainer completion the agent would have produced.
                info = InFlightStep(item=item, step=review_step, feedback=None, step_work_dir=tmp_path)
                info.agent_result = AgentResult(
                    success=True, stdout="REVIEW_PASS", stderr="", return_code=0, duration_ms=1
                )
                svc._in_flight[item.id] = info
                svc._completions.put_nowait(info)
                # Pump the drainer for a few ticks so the rule-target CAS lands.
                for _ in range(5):
                    run(asyncio.sleep(0.01))

                row = run(svc.db.get_task(task.id))
                assert row.state == "push_pr", (
                    f"Expected pr_fix's REVIEW_PASS to land on push_pr, got state={row.state!r}. "
                    "Regression: drainer was validating against main's SM, where "
                    "(reviewing, push_pr) is not a registered edge."
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_pr_fix_done_then_review_pass_real_dispatch_advances_to_push_pr(tmp_path, monkeypatch):
    """Real-dispatch repro: PR_FIX_DONE → review → REVIEW_PASS must reach push_pr.

    The sibling test above hand-builds the review InFlightStep from
    ``flows["pr_fix"]`` (so its ``success_state`` is already push_pr) — it never
    exercises how the orchestrator RESOLVES which review step to dispatch when
    routing ``PR_FIX_DONE → review``. This drives that real path: a pr-fix
    completion is drained (routing to review = a real dispatch), then the review
    agent emits ``REVIEW_PASS``.

    This test PASSES — it confirms the routing is correct: real dispatch resolves
    pr_fix's review (``success_state == push_pr``) and the ``(reviewing, push_pr)``
    auto-advance edge holds. It therefore does NOT reproduce an internal task's stall:
    that was a runtime completion-delivery issue (the review completion never
    processed by the drainer), not this routing path. Kept as coverage of the real
    dispatch resolution the sibling shortcut test skips.
    """
    import asyncio
    import subprocess

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import InFlightStep, OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tests.test_orchestrator import FakeRunner
    from lotsa.tools import ToolResult
    from rigg.models import AgentResult, Item

    # Git identity via env so EVERY git subprocess inherits it — incl. the pr-fix
    # ``commit`` posthook's commit, which runs in the task worktree (not tmp_path),
    # so repo-local config wouldn't reach it. CI has no global identity.
    for _k, _v in (
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
    ):
        monkeypatch.setenv(_k, _v)

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        # The pr-fix step's ``commit`` posthook runs ``git add``/commit on
        # PR_FIX_DONE; init a real repo so it succeeds and routing actually reaches
        # the review dispatch under test (otherwise the task blocks at pr-fix on
        # "not a git repository").
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        config = LotsaConfig(data_dir=tmp_path / "data", work_dir=tmp_path, flow="build", model="sonnet", budget=5.0)
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        # Freeze push_pr so a successful advance parks at state=push_pr for the
        # assertion (same rationale as the sibling test).
        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()  # never set

        async def stub_push_pr(ctx, config):
            await hang_event.wait()
            return ToolResult(success=True, output="never reached", metadata={})

        register_tool("push_pr", stub_push_pr)
        # The review step the drainer dispatches (after PR_FIX_DONE) returns REVIEW_PASS.
        svc.runner = FakeRunner(
            AgentResult(success=True, stdout="REVIEW_PASS", stderr="", return_code=0, duration_ms=1)
        )

        run(svc.start())
        try:
            task = run(svc.db.create_task("Test", state="pr-fixing", metadata={"current_flow": "pr_fix"}))
            run(
                svc.db.claim_task_transition(
                    task.id,
                    from_status=task.status,
                    from_state=task.state,
                    to_state="pr-fixing",
                    to_status="working",
                    to_current_step="pr-fix",
                )
            )
            # review declares inputs [spec, plan]; seed them so the dispatch
            # validates (in a real run the main-flow spec/plan steps produced them).
            run(svc.db.add_message(task.id, "agent", "spec", "s", "artifact", metadata={"artifact_name": "spec"}))
            run(svc.db.add_message(task.id, "agent", "plan", "p", "artifact", metadata={"artifact_name": "plan"}))
            item = Item(id=task.id, state="pr-fixing", title="Test", metadata={"current_flow": "pr_fix"})
            pr_fix_step = next(rj for rj in svc.process.flows["pr_fix"].jobs if rj.name == "pr-fix")

            # Synthesise the pr-fix completion → drainer routes PR_FIX_DONE → review
            # (the REAL review dispatch under test) → review agent returns REVIEW_PASS.
            info = InFlightStep(item=item, step=pr_fix_step, feedback=None, step_work_dir=tmp_path)
            info.agent_result = AgentResult(
                success=True, stdout="PR_FIX_DONE: applied the fix", stderr="", return_code=0, duration_ms=1
            )
            svc._in_flight[item.id] = info
            svc._completions.put_nowait(info)

            row = None
            for _ in range(60):
                run(asyncio.sleep(0.02))
                row = run(svc.db.get_task(task.id))
                if row.state == "push_pr":
                    break
            assert row is not None and row.state == "push_pr", (
                "PR_FIX_DONE → review → REVIEW_PASS must advance to push_pr in pr_fix; "
                f"got state={row.state if row else None!r} status={row.status if row else None!r}. "
                "Real dispatch must resolve pr_fix's review (success_state=push_pr) so the "
                "(reviewing, push_pr) auto-advance edge holds."
            )
        finally:
            run(svc.shutdown())
            run(db.close())
    finally:
        loop.close()


def test_retry_review_in_pr_fix_advances_to_push_pr(tmp_path):
    """retry() of a blocked review step in pr_fix must dispatch pr_fix's review
    (success_state=push_pr), not main's (verify).

    Definitive root cause of an internal task's multi-day stall: ``retry()`` resolved
    ``current_step`` against the ROOT flow (main), so retrying review-in-pr_fix ran
    *main's* review job. Its REVIEW_PASS auto-advance targeted ``(reviewing ->
    verify)`` — an edge that doesn't exist in pr_fix's SM — so the completion was
    silently dropped and the task stayed ``reviewing/working``. Every Retry
    re-stalled it (confirmed live: drainer WARNING "no (reviewing -> verify) edge
    in active flow pr_fix").

    Pre-fix: stays reviewing/working. Post-fix: advances to push_pr.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tests.test_orchestrator import FakeRunner
    from lotsa.tools import ToolResult
    from rigg.models import AgentResult

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(data_dir=tmp_path / "data", work_dir=tmp_path, flow="build", model="sonnet", budget=5.0)
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()  # never set — freeze at push_pr for the assertion

        async def stub_push_pr(ctx, config):
            await hang_event.wait()
            return ToolResult(success=True, output="never reached", metadata={})

        register_tool("push_pr", stub_push_pr)
        svc.runner = FakeRunner(
            AgentResult(success=True, stdout="REVIEW_PASS", stderr="", return_code=0, duration_ms=1)
        )

        run(svc.start())
        try:
            # The exact shape a Retry acts on: a blocked review step inside pr_fix.
            task = run(
                svc.db.create_task(
                    "Test",
                    state="reviewing",
                    status="blocked",
                    current_step="review",
                    metadata={"current_flow": "pr_fix"},
                )
            )
            run(svc.db.add_message(task.id, "agent", "spec", "s", "artifact", metadata={"artifact_name": "spec"}))
            run(svc.db.add_message(task.id, "agent", "plan", "p", "artifact", metadata={"artifact_name": "plan"}))

            run(svc.retry(task.id))

            row = None
            for _ in range(60):
                run(asyncio.sleep(0.02))
                row = run(svc.db.get_task(task.id))
                if row.state in ("push_pr", "blocked"):
                    break
            assert row is not None and row.state == "push_pr", (
                "retry() of review-in-pr_fix must advance to push_pr; got "
                f"state={row.state if row else None!r} status={row.status if row else None!r}. "
                "retry() must resolve the step from the ACTIVE flow (pr_fix review → push_pr), "
                "not the root flow (main review → verify)."
            )
        finally:
            run(svc.shutdown())
            run(db.close())
    finally:
        loop.close()


def test_answer_review_in_pr_fix_advances_to_push_pr(tmp_path):
    """answer() of a needs_input review step in pr_fix must dispatch pr_fix's
    review (success_state=push_pr), not main's (verify).

    Same bug class as ``retry()`` (an internal task), in a sibling action method:
    ``answer()``/``revise()``/``send_message()`` all resolved ``current_step``
    against the ROOT flow only. If a pr_fix ``review`` agent emits
    ``NEEDS_INPUT:`` and the operator answers, root resolution runs *main's*
    review; its REVIEW_PASS auto-advance targets ``(reviewing -> verify)`` — an
    edge absent from pr_fix's SM — so the completion is silently dropped and the
    task stalls at ``reviewing/working``. All three siblings now resolve via the
    shared ``_resolve_step_for_row`` helper (active flow first).

    Pre-fix: stays reviewing/working. Post-fix: advances to push_pr.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tests.test_orchestrator import FakeRunner
    from lotsa.tools import ToolResult
    from rigg.models import AgentResult

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(data_dir=tmp_path / "data", work_dir=tmp_path, flow="build", model="sonnet", budget=5.0)
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()  # never set — freeze at push_pr for the assertion

        async def stub_push_pr(ctx, config):
            await hang_event.wait()
            return ToolResult(success=True, output="never reached", metadata={})

        register_tool("push_pr", stub_push_pr)
        svc.runner = FakeRunner(
            AgentResult(success=True, stdout="REVIEW_PASS", stderr="", return_code=0, duration_ms=1)
        )

        run(svc.start())
        try:
            # The exact shape answer() acts on: a needs_input review step inside
            # pr_fix (the review agent emitted NEEDS_INPUT and parked).
            task = run(
                svc.db.create_task(
                    "Test",
                    state="reviewing",
                    status="needs_input",
                    current_step="review",
                    metadata={"current_flow": "pr_fix"},
                )
            )
            run(svc.db.add_message(task.id, "agent", "spec", "s", "artifact", metadata={"artifact_name": "spec"}))
            run(svc.db.add_message(task.id, "agent", "plan", "p", "artifact", metadata={"artifact_name": "plan"}))

            run(svc.answer(task.id, "proceed"))

            row = None
            for _ in range(60):
                run(asyncio.sleep(0.02))
                row = run(svc.db.get_task(task.id))
                if row.state in ("push_pr", "blocked"):
                    break
            assert row is not None and row.state == "push_pr", (
                "answer() of review-in-pr_fix must advance to push_pr; got "
                f"state={row.state if row else None!r} status={row.status if row else None!r}. "
                "answer()/revise()/send_message() must resolve the step from the ACTIVE flow "
                "(pr_fix review → push_pr), not the root flow (main review → verify)."
            )
        finally:
            run(svc.shutdown())
            run(db.close())
    finally:
        loop.close()


def test_resolve_step_for_row_prefers_active_flow(tmp_path):
    """``_resolve_step_for_row`` resolves the same-named step against the task's
    active flow, so a pr_fix ``review`` carries success_state=push_pr while a
    main ``review`` carries success_state=verify.

    This is the single chokepoint every step-by-current_step caller routes
    through (retry/revise/answer/send_message/approve + the read paths). Asserting
    the resolution order here protects all of them against drift.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(data_dir=tmp_path / "data", work_dir=tmp_path, flow="build", model="sonnet", budget=5.0)
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        run(svc.start())
        try:
            main_row = run(svc.db.create_task("main", state="reviewing", status="blocked", current_step="review"))
            pr_fix_row = run(
                svc.db.create_task(
                    "prfix",
                    state="reviewing",
                    status="blocked",
                    current_step="review",
                    metadata={"current_flow": "pr_fix"},
                )
            )

            main_step = svc._resolve_step_for_row(main_row)
            pr_fix_step = svc._resolve_step_for_row(pr_fix_row)

            assert main_step is not None and main_step.success_state == "verify", (
                f"main review should resolve to success_state=verify, got "
                f"{main_step.success_state if main_step else None!r}"
            )
            assert pr_fix_step is not None and pr_fix_step.success_state == "push_pr", (
                f"pr_fix review should resolve to success_state=push_pr, got "
                f"{pr_fix_step.success_state if pr_fix_step else None!r}"
            )
        finally:
            run(svc.shutdown())
            run(db.close())
    finally:
        loop.close()


def test_pr_fix_sub_flow_push_pr_success_returns_to_monitor(tmp_path):
    """A successful push_pr in pr_fix must route back to wait_for_pr_signal,
    not mark the task complete.

    The pre-fix bug: pr_fix's push_pr is the last binding, so _resolve_jobs
    derives success_state="complete". _execute_action_step validated
    (push_pr, "complete") against main's SM (which has only
    (push_pr, wait_for_pr_signal) and (push_pr, blocked)) — the check
    rejected the CAS and stranded the task at (push_pr, working). The
    fix: (a) register a sub-flow exit edge (last.active, host_monitor.queue)
    in the sub-flow's SM and (b) override success_state to the host monitor
    when running in a sub-flow with terminal "complete".
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult
    from rigg.models import Item

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        # Override the built-in push_pr tool with a stub that just returns
        # success — we're testing the orchestrator's routing, not the push.
        # The autouse ``_isolated_registry`` fixture restores the built-in
        # after the test.
        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)

        async def stub_push_pr(ctx, config):
            return ToolResult(success=True, output="pushed", metadata={"pr_number": 42})

        register_tool("push_pr", stub_push_pr)

        try:
            run(svc.start())
            try:
                task = run(svc.db.create_task("Test", state="push_pr", metadata={"current_flow": "pr_fix"}))
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="push_pr",
                        to_status="working",
                        to_current_step="push_pr",
                    )
                )
                item = Item(id=task.id, state="push_pr", title="Test", metadata={"current_flow": "pr_fix"})
                push_pr_step = next(j for j in svc.process.flows["pr_fix"].jobs if j.name == "push_pr")
                run(svc._dispatch_step(item, push_pr_step))
                # Pump the action task: it yields on metadata writes.
                for _ in range(5):
                    run(asyncio.sleep(0.01))

                row = run(svc.db.get_task(task.id))
                assert row.state == "wait_for_pr_signal", (
                    f"Expected sub-flow exit to wait_for_pr_signal, got state={row.state!r}. "
                    "Regression: action-step success path was routing pr_fix.push_pr to "
                    "'complete' (the binding's natural success_state), which is rejected "
                    "by main's SM and stranded the task at (push_pr, working)."
                )
                assert row.status == "waiting_for_pr"
                assert row.metadata.get("current_flow") == "main", (
                    "Sub-flow exit must reset current_flow to the host flow."
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_push_pr_no_github_parks_task_in_awaiting_operator(tmp_path):
    """A ``push_pr`` action that fails only because no GitHub is configured
    (``error_kind='no_github'``) parks the task in ``awaiting_operator`` — the
    ADR-043 escape hatch — NOT ``blocked``. This is the live producer for the
    parked status the README/UI advertise; without it a GitHub-less operator
    sees "blocked" with a token error instead of "Awaiting you".

    Regression: pre-fix the action dispatcher routed every failed action to
    ``blocked`` (via the ``rebasing``/``blocked`` branch), so this task landed
    at ``status='blocked'`` and no code path ever produced ``awaiting_operator``.
    The failure is driven from inside the code under test — the stub tool
    returns the real ``no_github`` failure contract, not a pre-flipped row.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult
    from rigg.models import Item

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        from lotsa import registry as reg

        reg._TOOLS.pop("push_pr", None)

        async def stub_push_pr(ctx, config):
            # Mirror the real push_pr tool's NO_GITHUB failure contract.
            return ToolResult(
                success=False,
                output="NO_GITHUB: GITHUB_TOKEN environment variable is not set.",
                metadata={"error_kind": "no_github"},
            )

        register_tool("push_pr", stub_push_pr)

        try:
            run(svc.start())
            try:
                task = run(svc.db.create_task("No GitHub", state="push_pr", metadata={"process_name": "build"}))
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="push_pr",
                        to_status="working",
                        to_current_step="push_pr",
                    )
                )
                item = Item(id=task.id, state="push_pr", title="No GitHub", metadata={"process_name": "build"})
                push_pr_step = next(j for j in svc.process.flows["main"].jobs if j.name == "push_pr")
                run(svc._dispatch_step(item, push_pr_step))
                for _ in range(5):
                    run(asyncio.sleep(0.01))

                row = run(svc.db.get_task(task.id))
                assert row.status == "awaiting_operator", (
                    f"Expected GitHub-less push to park in awaiting_operator, got "
                    f"status={row.status!r} state={row.state!r}"
                )
                assert row.state == "awaiting_operator"
                assert "pr_number" not in row.metadata
                # Parked with a status_change (an invitation to act), not an error.
                msgs = run(svc.db.get_messages(task.id))
                assert any(m.type == "status_change" and "Mark complete" in m.content for m in msgs), (
                    "expected an 'awaiting you' status_change audit row inviting Mark complete"
                )
                assert not any(m.type == "error" for m in msgs), (
                    "a GitHub-less park must not surface as an error message"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_execute_push_no_github_parks_task_in_awaiting_operator(tmp_path):
    """The *legacy* ``_execute_push`` path (``state='pushing'``, reachable via
    ``retry()`` on a pre-ADR-014 push-state row) parks a GitHub-less push in
    ``awaiting_operator`` — the ADR-043 escape hatch — NOT ``blocked``.

    This is the sibling of ``test_push_pr_no_github_parks_task_in_awaiting_operator``:
    the same ``NO_GITHUB:`` → ``awaiting_operator`` handling exists at two
    push sites (the ``push_pr`` action tool and this legacy ``_execute_push``
    branch), and only the former was covered. Without the ``_execute_push``
    branch a GitHub-less operator who lands on the legacy retry path would see
    ``blocked`` with a token error instead of "Awaiting you".

    The failure is driven from inside the code under test: ``execute_push`` is
    stubbed to raise the real ``PushError('NO_GITHUB: …')`` contract, and the
    row enters at the genuine ``(status='working', state='pushing')`` push
    precondition that ``PUSH_START`` claims — not pre-flipped into the parked
    state.
    """
    import asyncio

    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.push_step import PushError
    from rigg.models import Item

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        (tmp_path / "data").mkdir()
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)

        import lotsa.push_step as push_step

        orig_execute_push = push_step.execute_push
        orig_build_pr_text = push_step.build_pr_text

        async def stub_execute_push(**kwargs):
            # Mirror push_step's machine-detectable GitHub-less contract.
            raise PushError("NO_GITHUB: GITHUB_TOKEN environment variable is not set.")

        async def stub_build_pr_text(**kwargs):
            # Avoid needing a real git worktree for title/body synthesis;
            # the push raises before the value is ever used anyway.
            return ("t", "b")

        push_step.execute_push = stub_execute_push
        push_step.build_pr_text = stub_build_pr_text

        try:
            run(svc.start())
            try:
                task = run(
                    svc.db.create_task("No GitHub (legacy push)", state="pushing", metadata={"process_name": "build"})
                )
                # Enter at the genuine push precondition PUSH_START claims.
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="pushing",
                        to_status="working",
                        to_current_step="push",
                    )
                )
                item = Item(
                    id=task.id, state="pushing", title="No GitHub (legacy push)", metadata={"process_name": "build"}
                )
                run(svc._execute_push(item))

                row = run(svc.db.get_task(task.id))
                assert row.status == "awaiting_operator", (
                    f"Expected GitHub-less legacy push to park in awaiting_operator, got "
                    f"status={row.status!r} state={row.state!r}"
                )
                assert row.state == "awaiting_operator"
                # Parked with a status_change (an invitation to act), not an error.
                msgs = run(svc.db.get_messages(task.id))
                assert any(m.type == "status_change" and "Mark complete" in m.content for m in msgs), (
                    "expected an 'awaiting you' status_change audit row inviting Mark complete"
                )
                assert not any(m.type == "error" for m in msgs), (
                    "a GitHub-less park must not surface as an error message"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
        finally:
            push_step.execute_push = orig_execute_push
            push_step.build_pr_text = orig_build_pr_text
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Smoke tests for the cross-flow step-lookup fallback. ``retry()`` and
# ``jump_to_step()`` historically scanned only ``self.flow`` (main); under
# ADR-014 Layer A, ``pr-fix`` lives in the ``pr_fix`` sub-flow's bindings
# rather than main's. Without the ``self.process.jobs`` fallback added in
# this PR, ``retry(pr-fix)`` silently restarted from spec and
# ``jump_to_step("pr-fix")`` raised ValueError. These tests pin the fallback.
# ---------------------------------------------------------------------------


def _stub_full_process_service(tmp_path, run):
    """Build an OrchestratorService loaded against the bundled ``full`` process
    with the ``push_pr`` action tool stubbed out so the action dispatch loop
    cannot reach the real ``execute_push`` (which would fail in the tmpdir).

    Returns ``(service, db, hang_event)``. The autouse ``_isolated_registry``
    fixture restores the global registry after the test — callers no longer
    save/restore tools manually. The hang_event is exposed so callers that
    want a stub that returns immediately can flip it; by default it is never
    set so the push_pr action task freezes if it ever dispatches.
    """
    import asyncio

    from lotsa import registry as reg
    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    (tmp_path / "data").mkdir()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())

    # ``register_tool`` raises on collision, so pop the built-in before
    # installing the stub. The autouse fixture restores the built-in after
    # the test.
    reg._TOOLS.pop("push_pr", None)
    hang_event = asyncio.Event()  # never set by default

    async def stub_push_pr(ctx, config):
        await hang_event.wait()
        return ToolResult(success=True, output="never reached", metadata={})

    register_tool("push_pr", stub_push_pr)

    svc = OrchestratorService(config, db)

    # Replace the real agent runner with a stub that hangs on a never-set
    # event so dispatched agents don't try to spawn a Claude subprocess in
    # the test process. retry()/jump_to_step() only need to reach the
    # post-CAS state — the agent task's completion is not asserted on.
    class _HangRunner:
        def dispatch_shape_prompt(self) -> str:
            return ""

        async def run(self, *args, **kwargs):
            await hang_event.wait()
            from rigg.models import AgentResult

            return AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=1)

    svc.runner = _HangRunner()
    return svc, db, hang_event


def test_retry_on_blocked_pr_fix_task_resumes_pr_fix_not_spec(tmp_path):
    """``retry()`` on a blocked task whose ``current_step='pr-fix'`` must find
    pr-fix via the process catalog fallback, not silently restart from spec.

    Pre-fix behavior: ``self.flow.jobs`` (main's resolved jobs) doesn't
    contain pr-fix (it lives in the pr_fix sub-flow's bindings), so the
    lookup returned None and the silent ``self.flow.jobs[0]`` fallback fired,
    re-dispatching the entire pipeline from spec. Fix: walk
    ``self.process.jobs`` (the union catalog) before the spec fallback.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Stage a task at (state=pr-fixing, status=blocked,
                # current_step=pr-fix) — the shape a blocked pr-fix agent
                # round would leave behind. ``current_flow=pr_fix`` so the
                # drainer's rule lookups resolve against the right SM.
                task = run(
                    svc.db.create_task(
                        "Test",
                        state="pr-fixing",
                        metadata={"current_flow": "pr_fix"},
                    )
                )
                # pr-fix has inputs=[spec, plan]; without these artifacts
                # ``_dispatch_step`` rolls the task back to blocked on its
                # missing-artifact check, which would mask whether the
                # bug fix landed pr-fix's queue_state in the first place.
                run(
                    svc.db.add_message(
                        task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}
                    )
                )
                run(
                    svc.db.add_message(
                        task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}
                    )
                )
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="pr-fixing",
                        to_status="blocked",
                        to_current_step="pr-fix",
                    )
                )

                run(svc.retry(task.id))

                row = run(svc.db.get_task(task.id))
                assert row.current_step == "pr-fix", (
                    f"retry() must resume pr-fix, got current_step={row.current_step!r}. "
                    "Regression: self.flow.jobs (main flow) does not contain pr-fix; "
                    "without the self.process.jobs fallback, retry() silently restarts "
                    "from spec instead."
                )
                assert row.state == "pr-fixing", f"retry() must land state at pr-fix's queue_state, got {row.state!r}"
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_jump_to_step_pr_fix_finds_subflow_step_via_process_catalog(tmp_path):
    """``jump_to_step("pr-fix")`` must find pr-fix via the process catalog
    fallback rather than raising ValueError.

    Pre-fix behavior: ``self.flow.steps`` is the main flow's bindings; pr-fix
    lives only in the ``pr_fix`` sub-flow, so the for-loop matched nothing
    and the function raised ``ValueError("Unknown step: pr-fix")``. Fix:
    walk ``self.process.jobs`` (the union catalog) after the main-flow scan.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Stage a task at wait_for_pr_signal — the canonical state
                # from which an operator would jump into pr-fix.
                task = run(svc.db.create_task("Test", state="wait_for_pr_signal"))
                # pr-fix has inputs=[spec, plan]; without these artifacts
                # ``_dispatch_step`` rolls the task back to blocked on its
                # missing-artifact check, masking the cross-flow lookup.
                run(
                    svc.db.add_message(
                        task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}
                    )
                )
                run(
                    svc.db.add_message(
                        task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}
                    )
                )
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="wait_for_pr_signal",
                        to_status="waiting_for_pr",
                        to_current_step="wait_for_pr_signal",
                    )
                )

                # Pre-fix this raised ValueError("Unknown step: pr-fix").
                run(svc.jump_to_step(task.id, "pr-fix"))

                row = run(svc.db.get_task(task.id))
                assert row.state == "pr-fixing", (
                    f"jump_to_step('pr-fix') must land state at pr-fix's queue_state, "
                    f"got {row.state!r}. Regression: cross-flow jumps were rejected "
                    "before this PR's process-catalog fallback."
                )
                assert row.current_step == "pr-fix"
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_jump_to_step_into_pr_fix_sets_current_flow_metadata(tmp_path):
    """Regression: ``jump_to_step("pr-fix")`` must write ``current_flow="pr_fix"``.

    Pre-fix behavior: ``_dispatch_pr_fix_locked`` set ``current_flow="pr_fix"``
    so the subsequent ``review`` completion evaluated pr_fix-flow rule
    overrides (e.g. ``REVIEW_FAIL → pr-fix``). ``jump_to_step("pr-fix")``
    was the sixth entry point (per its own docstring) but never wrote the
    metadata — silently letting the drainer evaluate main-flow overrides
    (``REVIEW_FAIL → code``) instead. Round-7 review fix.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Stage a task at wait_for_pr_signal with NO current_flow
                # metadata — the canonical shape after a push_pr success.
                task = run(svc.db.create_task("Test", state="wait_for_pr_signal"))
                run(
                    svc.db.add_message(
                        task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}
                    )
                )
                run(
                    svc.db.add_message(
                        task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}
                    )
                )
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="wait_for_pr_signal",
                        to_status="waiting_for_pr",
                        to_current_step="wait_for_pr_signal",
                    )
                )

                run(svc.jump_to_step(task.id, "pr-fix"))

                row = run(svc.db.get_task(task.id))
                assert row.metadata.get("current_flow") == "pr_fix", (
                    f"jump_to_step('pr-fix') must set current_flow=pr_fix so subsequent "
                    f"review evaluates pr_fix rule overrides, got "
                    f"{row.metadata.get('current_flow')!r}"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_jump_to_step_out_of_pr_fix_resets_current_flow_to_root(tmp_path):
    """Regression: ``jump_to_step("code")`` from a pr_fix-flow task must reset
    ``current_flow`` to the root flow so the dispatch loop can find "code".

    Pre-fix behavior: a task with ``current_flow="pr_fix"`` (blocked pr-fix
    agent) jumped to ``"code"`` left the metadata as ``"pr_fix"``;
    ``_dispatch_next_step`` → ``_resolve_flow`` → pr_fix flow, and "code"
    is not in pr_fix's bindings, so ``_find_step_for_state`` returned
    ``None`` and the task silently stalled at ``status="working"`` with no
    agent running. Round-7 review fix.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # Stage a task at (state=pr-fixing, status=blocked,
                # current_flow=pr_fix) — the shape a blocked pr-fix round
                # leaves behind. Operator decides to jump back into "code"
                # for a redo.
                task = run(
                    svc.db.create_task(
                        "Test",
                        state="pr-fixing",
                        metadata={"current_flow": "pr_fix"},
                    )
                )
                run(
                    svc.db.add_message(
                        task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}
                    )
                )
                run(
                    svc.db.add_message(
                        task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}
                    )
                )
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="pr-fixing",
                        to_status="blocked",
                        to_current_step="pr-fix",
                    )
                )

                run(svc.jump_to_step(task.id, "code"))

                row = run(svc.db.get_task(task.id))
                # Must land at code's queue_state — the silent-stall bug
                # would leave it stuck after CAS at status=working with
                # no dispatch ever firing.
                assert row.current_step == "code", (
                    f"jump_to_step('code') from pr_fix sub-flow must reach code's "
                    f"queue_state; got current_step={row.current_step!r}. "
                    "Regression: stale current_flow=pr_fix would cause "
                    "_find_step_for_state to return None → silent stall."
                )
                assert row.metadata.get("current_flow") == "main", (
                    f"jump_to_step out of pr_fix must reset current_flow to root "
                    f"(main), got {row.metadata.get('current_flow')!r}"
                )
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Smoke tests for the cross-flow lookup fallback in the three remaining
# action methods — ``answer()``, ``send_message()``, and ``revise()`` (the
# waiting/needs_input branch). All three previously raised
# ``Unknown current_step 'pr-fix'`` against the bundled ``full`` process
# because pr-fix lives only in the ``pr_fix`` sub-flow's bindings — i.e.,
# the canonical NEEDS_DECISION recovery loop was broken. Mirrors the
# fallback added in ``retry()``/``jump_to_step``/``_dispatch_pr_fix_locked``.
# ---------------------------------------------------------------------------


def _stage_needs_input_pr_fix_task(svc, run):
    """Stage a task at (state=pr-fixing, status=needs_input, current_step=pr-fix)
    — the shape the bundled ``full`` process leaves behind after the pr-fix
    agent emits ``^PR_FIX_NEEDS_DECISION:``. Seeds the spec/plan artifacts so
    a subsequent ``_dispatch_step`` doesn't bail on the missing-artifact
    check (which would mask whether the cross-flow lookup landed correctly).
    """
    task = run(
        svc.db.create_task(
            "Test",
            state="pr-fixing",
            metadata={"current_flow": "pr_fix"},
        )
    )
    run(svc.db.add_message(task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}))
    run(svc.db.add_message(task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}))
    run(
        svc.db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state="pr-fixing",
            to_status="needs_input",
            to_current_step="pr-fix",
        )
    )
    return task


def test_answer_on_needs_input_pr_fix_task_finds_subflow_step_via_process_catalog(tmp_path):
    """``answer()`` on a NEEDS_DECISION pr-fix task must find pr-fix via the
    process catalog fallback, not raise ``Unknown current_step 'pr-fix'``.

    Pre-fix behavior: ``self.flow.jobs`` (main's resolved jobs) doesn't
    contain pr-fix (it lives in the pr_fix sub-flow's bindings), so the
    lookup returned None and ``AnswerNotAllowed`` was raised — breaking the
    canonical NEEDS_DECISION → operator answer → resume loop.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = _stage_needs_input_pr_fix_task(svc, run)
                run(svc.answer(task.id, "yes, touch module X"))
                row = run(svc.db.get_task(task.id))
                assert row.current_step == "pr-fix", (
                    f"answer() must resume pr-fix, got current_step={row.current_step!r}. "
                    "Regression: self.flow.jobs (main flow) does not contain pr-fix; "
                    "without the self.process.jobs fallback, answer() raises "
                    "AnswerNotAllowed('Unknown current_step pr-fix')."
                )
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_send_message_on_needs_input_pr_fix_task_finds_subflow_step_via_process_catalog(tmp_path):
    """``send_message()`` on a NEEDS_DECISION pr-fix task must find pr-fix
    via the process catalog fallback rather than raising
    ``Unknown current_step 'pr-fix'``.

    Mirrors ``answer()``: the bundled ``full`` process leaves pr-fix in the
    ``pr_fix`` sub-flow's bindings, so the main-flow scan misses; the
    fallback to ``self.process.jobs`` resolves the cross-flow lookup.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = _stage_needs_input_pr_fix_task(svc, run)
                run(svc.send_message(task.id, "more context"))
                row = run(svc.db.get_task(task.id))
                assert row.current_step == "pr-fix", (
                    f"send_message() must resume pr-fix, got current_step={row.current_step!r}. "
                    "Regression: missing self.process.jobs fallback raises "
                    "ReviseNotAllowed('Unknown current_step pr-fix')."
                )
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Phase 2 round-cap / consecutive-skip / pr_decision audit coverage (ADR-014
# Layer A replacement for the skipped ``TestPrFixPhase2`` in
# ``test_orchestrator.py``).
#
# The cap-firing helper ``_pr_fix_round_cap_blocked`` is shared by every
# pr-fix dispatch entry point — exercising it directly verifies that:
#   - the cap pre-check returns True at the cap value and False below it,
#   - a ``pr_decision(blocked)`` row is written before the CAS (audit-first),
#   - the task transitions to ``status="blocked"`` via the registered
#     ``(pr-fixing, blocked)`` SM edge,
#   - the row reports ``round=current_rounds`` (pre-increment, NOT a stale
#     or future round value).
# ---------------------------------------------------------------------------


def test_pr_fix_round_cap_fires_writes_audit_row_and_blocks(tmp_path):
    """``_pr_fix_round_cap_blocked`` at the cap value writes a ``pr_decision(blocked)``
    row, transitions the task to blocked, and reports the current round.

    Replaces ``TestPrFixPhase2.test_round_cap_blocks_dispatch`` from the
    skipped legacy class — same invariant, new model (the helper is the
    shared cap-fire path that every entry point funnels through).
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                # max_pr_fix_rounds is 10 in the bundled ``full`` process —
                # confirm the fixture loaded the monitor config so the cap
                # we're testing actually fires. ADR-021: config is keyed
                # per-process.
                pr_cfg = svc._pr_monitor_configs_by_process[svc._active_process_name]
                assert pr_cfg is not None
                assert pr_cfg.max_pr_fix_rounds == 10

                # Stage a task at (state=pr-fixing, status=waiting_for_pr,
                # current_step=pr-fix) so the cap-fire CAS lands on the
                # (pr-fixing, blocked) edge registered in the bundled SM.
                task = run(
                    svc.db.create_task(
                        "Cap test",
                        state="pr-fixing",
                        metadata={"current_flow": "pr_fix"},
                    )
                )
                run(
                    svc.db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="pr-fixing",
                        to_status="waiting_for_pr",
                        to_current_step="pr-fix",
                    )
                )

                # Below the cap → False, no transition, no pr_decision row.
                fired_below = run(
                    svc._pr_fix_round_cap_blocked(
                        task.id,
                        task_state="pr-fixing",
                        current_rounds=9,  # cap is 10
                        from_status="waiting_for_pr",
                    )
                )
                assert fired_below is False
                row_below = run(svc.db.get_task(task.id))
                assert row_below.status == "waiting_for_pr", (
                    f"Below-cap call must not transition; got status={row_below.status!r}"
                )

                # At the cap → True, pr_decision(blocked) row written, task blocked.
                fired_at = run(
                    svc._pr_fix_round_cap_blocked(
                        task.id,
                        task_state="pr-fixing",
                        current_rounds=10,
                        from_status="waiting_for_pr",
                    )
                )
                assert fired_at is True
                row_at = run(svc.db.get_task(task.id))
                assert row_at.status == "blocked", f"Cap fire must transition to blocked; got status={row_at.status!r}"

                # Audit-first: the pr_decision row is durable, reports the
                # round that triggered the block (10, NOT 11), and carries
                # decision='blocked' so an operator can attribute the block
                # to the cap rather than to an agent-emitted BLOCKED.
                decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
                assert len(decisions) == 1, f"Cap fire must write exactly one pr_decision row; got {len(decisions)}"
                assert decisions[0].metadata.get("decision") == "blocked"
                assert decisions[0].metadata.get("round") == 10, "pr_decision row must report pre-increment round value"
                assert "PR-fix budget exhausted" in decisions[0].content
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Recovery sweep — legacy synthetic states (ADR-014 Layer A replacement for
# the recovery-sweep portion of the skipped ``TestPrPhaseStates`` class).
#
# ``start()`` sweeps any row with ``status='working'`` or state in either the
# new ``_action_states`` set OR the legacy ``("pushing", "rebasing")`` set.
# Legacy rows must surface the push-state recovery message ("Server restarted
# while task was X — moved to blocked. Retry when ready.") so the operator
# knows the task crashed mid-push, not mid-agent.
# ---------------------------------------------------------------------------


def test_start_recovery_sweep_treats_legacy_pushing_state_as_action_state(tmp_path):
    """Legacy ``state='pushing'`` / ``rebasing`` / ``waiting_for_pr`` rows get
    the push-state recovery message on start().

    The recovery sweep at ``orchestrator.start()`` (~line 358) treats both
    new-model action states (read from ``self._action_states``) AND legacy
    synthetic names (``pushing``, ``rebasing``, ``waiting_for_pr``) as
    action-style recovery. Without this, a row persisted by the pre-ADR-014
    schema would surface the generic "Agent killed by server restart"
    message — confusing operators who'd see "agent" terminology on what was
    actually a PR-phase event. Worse, ``waiting_for_pr`` rows would silently
    stall: the new SM has no edge from that state, so
    ``transition_task(task_id, "complete")`` from the engine and ``block()``
    from an operator both warn-and-return — the row sits forever with no
    progression.

    Confirms all three legacy state names route through the action-style
    message in a single test so a regression that drops one of them surfaces
    visibly.
    """
    import asyncio

    from lotsa import registry as reg

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete

        # Pre-create the legacy-state rows BEFORE start() so the recovery
        # sweep on startup picks them up. We can't use start()'s service
        # to create them — start() runs the sweep before returning.
        from lotsa.config import LotsaConfig
        from lotsa.db import TaskDB
        from lotsa.orchestrator import OrchestratorService

        (tmp_path / "data").mkdir()
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        # Stage one task per legacy state name — both at status="waiting_for_pr"
        # (the steady-state status the PR monitor would leave a row at). The
        # sweep matches on EITHER status=working OR state-in-push-set, so this
        # exercises the legacy-state branch specifically (not the
        # status=working branch).
        push_task = run(db.create_task("Legacy push", state="pushing"))
        run(
            db.claim_task_transition(
                push_task.id,
                from_status=push_task.status,
                from_state=push_task.state,
                to_state="pushing",
                to_status="waiting_for_pr",
                to_current_step="push",
            )
        )
        rebase_task = run(db.create_task("Legacy rebase", state="rebasing"))
        run(
            db.claim_task_transition(
                rebase_task.id,
                from_status=rebase_task.status,
                from_state=rebase_task.state,
                to_state="rebasing",
                to_status="waiting_for_pr",
                to_current_step="push",
            )
        )
        # Legacy ``state="waiting_for_pr"`` — the steady-state shape of a row
        # that the pre-ADR-014 PrMonitor was polling when the schema rolled
        # over. Without ``waiting_for_pr`` in ``_legacy_push_states`` this row
        # would be silently stranded: the new SM has no edge from
        # ``waiting_for_pr``, so engine ``_on_complete`` calls warn-and-return
        # and the row would sit forever.
        wait_task = run(db.create_task("Legacy waiting_for_pr", state="waiting_for_pr"))
        run(
            db.claim_task_transition(
                wait_task.id,
                from_status=wait_task.status,
                from_state=wait_task.state,
                to_state="waiting_for_pr",
                to_status="waiting_for_pr",
                to_current_step="push",
            )
        )

        # Stub push_pr so start()'s registry resolution succeeds without
        # the real ``execute_push`` body — same idea as ``_stub_full_process_service``,
        # inlined because we need the rows seeded before start() runs. The
        # autouse ``_isolated_registry`` fixture restores the built-in after
        # the test.
        from lotsa.registry import register_tool
        from lotsa.tools import ToolResult

        reg._TOOLS.pop("push_pr", None)

        async def stub_push_pr(ctx, config):
            return ToolResult(success=True, output="stub", metadata={})

        register_tool("push_pr", stub_push_pr)

        svc = OrchestratorService(config, db)
        try:
            run(svc.start())
            try:
                # All three rows should now be blocked with the push-state message.
                for task_id, state_name in [
                    (push_task.id, "pushing"),
                    (rebase_task.id, "rebasing"),
                    (wait_task.id, "waiting_for_pr"),
                ]:
                    row = run(svc.db.get_task(task_id))
                    assert row.status == "blocked", (
                        f"Recovery sweep must block legacy state={state_name!r} row; got status={row.status!r}"
                    )
                    messages = run(svc.db.get_messages(task_id, msg_type="status_change"))
                    push_recovery_msg = next(
                        (m for m in messages if "Server restarted" in m.content),
                        None,
                    )
                    assert push_recovery_msg is not None, (
                        f"Legacy state={state_name!r} must surface the push-state recovery "
                        f"message, not the generic 'Agent killed' one. Messages: "
                        f"{[m.content for m in messages]}"
                    )
                    assert "moved to blocked" in push_recovery_msg.content
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


def test_revise_on_needs_input_pr_fix_task_finds_subflow_step_via_process_catalog(tmp_path):
    """``revise()`` on a NEEDS_DECISION pr-fix task must find pr-fix via the
    process catalog fallback.

    The waiting_for_pr branch of revise() routes through
    ``_dispatch_pr_fix_locked`` (which already has the catalog fallback);
    the waiting/needs_input branch — line 800 in orchestrator.py — has its
    own step lookup that previously had no fallback. This test pins the
    needs_input branch path.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = _stage_needs_input_pr_fix_task(svc, run)
                run(svc.revise(task.id, "try a different approach"))
                row = run(svc.db.get_task(task.id))
                assert row.current_step == "pr-fix", (
                    f"revise() must resume pr-fix, got current_step={row.current_step!r}. "
                    "Regression: revise()'s waiting/needs_input branch missed the "
                    "self.process.jobs fallback and raised ReviseNotAllowed."
                )
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Regression: a pr-fix SKIPPED round must not duplicate the agent's reasoning
# across two messages, and its stage_transition divider must stay short.
#
# Live bug (operator report): when a pr-fix round declined feedback as
# non-actionable (PR_FIX_SKIPPED), the drainer wrote the agent's one-line
# reasoning into BOTH a ``stage_transition`` divider (as
# ``f"pr-fix skipped: {last_line}"``) AND the append-only ``pr_decision`` audit
# row. The same sentence therefore rendered twice — once as a monospaced
# divider label that could not wrap (forcing the window to scroll horizontally)
# and once as a chat bubble. The divider must carry a bare marker with no
# reasoning; the ``pr_decision`` row stays the single carrier of the reasoning.
# ---------------------------------------------------------------------------


def _drain_in_flight(run, svc, task_id):
    """Pump the completion drainer until the dispatched agent is retired."""
    import asyncio

    for _ in range(40):
        if task_id not in svc._in_flight:
            break
        run(asyncio.sleep(0.05))
    # One more tick so the drainer lands the SKIPPED transition + audit writes.
    run(asyncio.sleep(0.1))


def test_pr_fix_skipped_writes_short_divider_and_single_reasoning(tmp_path):
    """A SKIPPED round emits a bare ``pr-fix skipped`` divider and carries the
    reasoning in exactly one message — the ``pr_decision`` row.

    The reasoning here is deliberately long so a divider that still embedded it
    would be the overflowing monospaced block the operator reported.
    """
    import asyncio

    from lotsa import registry as reg
    from lotsa.registry import register_posthook
    from lotsa.tests.test_orchestrator import FakeRunner
    from lotsa.tools import ToolResult
    from rigg.models import AgentResult

    long_reason = "reviewer comment is non-actionable " * 20

    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _hang = _stub_full_process_service(tmp_path, run)

        # The pr-fix step's ``commit`` posthook runs on agent success BEFORE the
        # SKIPPED routing (ADR-024); its publish step needs a real git remote +
        # GITHUB_TOKEN, which the tmpdir lacks. Stub it to succeed so routing
        # reaches the SKIPPED branch under test rather than blocking at pr-fix.
        # The autouse ``_isolated_registry`` fixture restores the built-in.
        reg._POSTHOOKS.pop("commit", None)

        async def stub_commit(ctx, config):
            return ToolResult(success=True, output="no changes to commit", metadata={})

        register_posthook("commit", stub_commit)

        # The pr-fix agent declines the feedback with a long reasoning line.
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout=f"Read the feedback.\nPR_FIX_SKIPPED: {long_reason}\n",
                stderr="",
                return_code=0,
                duration_ms=88,
                cost_usd=0.003,
                session_id="sk",
            )
        )
        try:
            run(svc.start())
            try:
                # Stage a task as if the pr_monitor engine had just landed it in
                # waiting_for_pr at the monitor's queue_state, with the pr-fix
                # input artifacts seeded so the dispatch is not short-circuited.
                task = run(
                    svc.db.create_task(
                        "PR fix skipped divider test",
                        state="wait_for_pr_signal",
                        status="waiting_for_pr",
                        metadata={"pr_number": 1, "github_owner": "o", "github_repo": "r"},
                    )
                )
                for art in ("spec", "plan"):
                    run(
                        svc.db.add_message(
                            task.id,
                            "agent",
                            art,
                            f"{art} content",
                            "artifact",
                            metadata={"artifact_name": art},
                        )
                    )

                dispatched = run(svc.dispatch_pr_fix(task.id, "bot left an approval comment"))
                assert dispatched, "dispatch_pr_fix returned False — the locked body declined"
                _drain_in_flight(run, svc, task.id)

                # The pr-fix SKIPPED divider must be a short, constant marker.
                transitions = run(svc.db.get_messages(task.id, msg_type="stage_transition"))
                pr_fix_dividers = [m for m in transitions if (m.metadata or {}).get("from_step") == "pr-fix"]
                assert pr_fix_dividers, "SKIPPED path must still emit a stage_transition divider"
                divider = pr_fix_dividers[-1]
                assert divider.content == "pr-fix skipped", (
                    f"divider must be a bare marker with no reasoning, got {divider.content!r}"
                )

                # No stage_transition may embed the reasoning — that duplication
                # (and its horizontal overflow) is exactly what this fix removes.
                for m in transitions:
                    assert long_reason.strip() not in (m.content or ""), (
                        "a stage_transition divider must not duplicate the agent's "
                        f"reasoning (found it in {m.content!r})"
                    )

                # Exactly one message carries the reasoning: the pr_decision row.
                decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
                skipped_rows = [m for m in decisions if (m.metadata or {}).get("decision") == "skipped"]
                assert len(skipped_rows) == 1, (
                    f"SKIPPED must write exactly one skipped pr_decision row, got {len(skipped_rows)}"
                )
                assert long_reason.strip() in (skipped_rows[0].content or ""), (
                    "the pr_decision row is the single carrier of the reasoning text"
                )
                assert skipped_rows[0].metadata.get("commit_sha") is None
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()
