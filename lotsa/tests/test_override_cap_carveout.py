"""Carve-out tests for ADR-019 Commitment 5 — operator-initiated paths bypass
the autonomous ``max_pr_fix_rounds`` cap (R7 / D7).

These are RED tests written before the carve-out lands. Today every
operator-initiated dispatch path (``revise``/``answer``/``send_message``/
``retry``/``jump_to_step("pr-fix")``) calls ``_pr_fix_round_cap_blocked`` and
is refused at the cap; ``revise``'s ``waiting_for_pr``/``rebasing`` routes hit
the same cap inside ``_dispatch_pr_fix_locked``. After the carve-out:

* the five direct sites stop enforcing the cap (the inline check is removed),
* ``_dispatch_pr_fix_locked`` gains an ``operator_initiated`` flag that gates
  cap *enforcement* (the counter still increments on every dispatch),
* only the autonomous monitor path (``operator_initiated=False``) still blocks.

Why each test is red today:

* The operator-path tests assert the call DISPATCHES at the cap (counter
  bumps past the cap, no cap-fire ``pr_decision`` row written by the operator
  action). Today the cap fires instead → counter stays at the cap and a
  ``PR-fix budget exhausted`` row is written → assertions fail.
* The ``_dispatch_pr_fix_locked`` gate test passes ``operator_initiated=True``,
  a keyword that does not exist yet → ``TypeError`` → red for the right reason.

The cap-blocked task is staged directly (the precondition), then the operator
action is invoked so the cap check runs INSIDE the code under test — not by
pre-flipping the task into a post-fix state (per lotsa/CLAUDE.md
regression-test discipline).

The full ``pr_fix`` infrastructure (cap=10) comes from the active
``_stub_full_process_service`` helper in ``test_pr_flow_integration``.
"""

from __future__ import annotations

import asyncio

from lotsa.tests.test_pr_flow_integration import _stub_full_process_service

# The bundled ``full`` process sets max_pr_fix_rounds = 10.
_CAP = 10


def _assert_cap_loaded(svc):
    pr_cfg = svc._pr_monitor_configs_by_process[svc._active_process_name]
    assert pr_cfg is not None and pr_cfg.max_pr_fix_rounds == _CAP, (
        f"precondition: bundled full process must load max_pr_fix_rounds={_CAP}"
    )


def _stage_pr_fix_task(svc, run, *, status, state="pr-fixing", rounds=_CAP):
    """Stage a pr-fix task at the cap in the requested (status, state).

    Adds the spec/plan artifacts pr-fix declares as inputs so a successful
    dispatch is not rolled back to blocked on the missing-artifact check —
    which would otherwise mask whether the carve-out let the dispatch proceed.
    """
    task = run(
        svc.db.create_task(
            "Carve-out",
            state=state,
            metadata={
                "current_flow": "pr_fix",
                "pr_fix_round_count": rounds,
                "pr_number": 1,
                "github_owner": "o",
                "github_repo": "r",
            },
        )
    )
    for art in ("spec", "plan"):
        run(svc.db.add_message(task.id, "agent", art, f"{art} content", "artifact", metadata={"artifact_name": art}))
    run(
        svc.db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state=state,
            to_status=status,
            to_current_step="pr-fix",
        )
    )
    return run(svc.db.get_task(task.id))


def _cap_fire_rows(svc, run, task_id):
    rows = run(svc.db.get_messages(task_id, msg_type="pr_decision"))
    return [r for r in rows if r.content.startswith("PR-fix budget exhausted")]


def _assert_dispatched_past_cap(svc, run, task_id):
    """Assert the operator action dispatched rather than firing the cap."""
    row = run(svc.db.get_task(task_id))
    assert row.metadata.get("pr_fix_round_count") == _CAP + 1, (
        "operator-initiated dispatch must increment the round counter past the cap "
        f"(expected {_CAP + 1}), got {row.metadata.get('pr_fix_round_count')!r} — "
        "the cap was enforced on an operator path"
    )
    assert not _cap_fire_rows(svc, run, task_id), (
        "operator-initiated dispatch must NOT write a 'PR-fix budget exhausted' "
        "pr_decision row — the cap fired on an operator path"
    )
    assert row.status != "blocked", (
        f"operator-initiated dispatch must leave the cap-blocked state, got status={row.status!r}"
    )


def _carveout_test(tmp_path, body):
    """Run *body(svc, run)* inside a started full-process service."""
    loop = asyncio.new_event_loop()
    try:
        run = loop.run_until_complete
        svc, db, _ = _stub_full_process_service(tmp_path, run)
        try:
            run(svc.start())
            _assert_cap_loaded(svc)
            try:
                body(svc, run)
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# The five operator-initiated direct call sites (AC6)
# ---------------------------------------------------------------------------


def test_retry_at_cap_dispatches_not_blocked(tmp_path):
    def body(svc, run):
        task = _stage_pr_fix_task(svc, run, status="blocked")
        run(svc.retry(task.id))
        _assert_dispatched_past_cap(svc, run, task.id)

    _carveout_test(tmp_path, body)


def test_answer_at_cap_dispatches_not_blocked(tmp_path):
    """Regression for the named AC6 bug: an operator answering a
    ``PR_FIX_NEEDS_DECISION`` on a cap-blocked task is no longer silently
    rejected by the cap.
    """

    def body(svc, run):
        task = _stage_pr_fix_task(svc, run, status="needs_input")
        run(svc.answer(task.id, "operator answer at the cap"))
        _assert_dispatched_past_cap(svc, run, task.id)

    _carveout_test(tmp_path, body)


def test_send_message_at_cap_dispatches_not_blocked(tmp_path):
    def body(svc, run):
        task = _stage_pr_fix_task(svc, run, status="needs_input")
        run(svc.send_message(task.id, "operator chat at the cap"))
        _assert_dispatched_past_cap(svc, run, task.id)

    _carveout_test(tmp_path, body)


def test_revise_on_needs_input_at_cap_dispatches_not_blocked(tmp_path):
    def body(svc, run):
        task = _stage_pr_fix_task(svc, run, status="needs_input")
        run(svc.revise(task.id, "operator revise at the cap"))
        _assert_dispatched_past_cap(svc, run, task.id)

    _carveout_test(tmp_path, body)


def test_jump_to_pr_fix_at_cap_dispatches_not_blocked(tmp_path):
    def body(svc, run):
        task = _stage_pr_fix_task(svc, run, status="waiting_for_pr")
        run(svc.jump_to_step(task.id, "pr-fix"))
        _assert_dispatched_past_cap(svc, run, task.id)

    _carveout_test(tmp_path, body)


# ---------------------------------------------------------------------------
# D7 — _dispatch_pr_fix_locked gates cap enforcement on operator_initiated.
# This is the route revise(waiting_for_pr) / revise(rebasing) funnel through,
# so it is what makes AC6 hold for those revise routes.
# ---------------------------------------------------------------------------


def test_dispatch_pr_fix_locked_cap_gated_on_operator_initiated(tmp_path):
    def body(svc, run):
        # Autonomous (default operator_initiated=False) still blocks at the cap.
        auto = _stage_pr_fix_task(svc, run, status="waiting_for_pr")
        auto_result = run(svc._dispatch_pr_fix_locked(auto.id, "monitor feedback"))
        assert auto_result is False, "autonomous dispatch at the cap must be refused"
        auto_row = run(svc.db.get_task(auto.id))
        assert auto_row.metadata.get("pr_fix_round_count") == _CAP, "autonomous cap-fire must not bump the counter"
        assert _cap_fire_rows(svc, run, auto.id), "autonomous cap-fire must write a 'PR-fix budget exhausted' row"

        # Operator-initiated dispatch bypasses cap enforcement (the counter
        # still increments). ``operator_initiated`` does not exist yet → today
        # this raises TypeError, which is the RED signal.
        op = _stage_pr_fix_task(svc, run, status="waiting_for_pr")
        op_result = run(svc._dispatch_pr_fix_locked(op.id, "operator feedback", operator_initiated=True))
        assert op_result is True, "operator-initiated dispatch at the cap must proceed"
        _assert_dispatched_past_cap(svc, run, op.id)

    _carveout_test(tmp_path, body)


# NOTE on revise(waiting_for_pr)/revise(rebasing): both routes funnel through
# ``_dispatch_pr_fix_locked``, so their carve-out behaviour is exactly the
# ``operator_initiated=True`` path asserted in
# ``test_dispatch_pr_fix_locked_cap_gated_on_operator_initiated`` above. An
# end-to-end ``revise(waiting_for_pr)`` test is deliberately omitted here
# because ``revise()`` first calls ``_build_revise_feedback``, which performs a
# live GitHub fetch (api.github.com) that would make the test non-hermetic and
# flaky. The gate test covers the mechanism without the network dependency.
