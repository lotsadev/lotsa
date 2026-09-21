"""Failing-first tests for ADR-045 Phase 2 — ``gate: operator`` on call edges.

These pin the behaviour the Phase 2 implementation must deliver (see the
planning-step write-up). Phase 2 turns the ``chat → build``/``chat → fix``
hand-off from an ADR-027 **promotion** (which re-roots the task, discarding the
``chat`` frame) into an operator-**gated call** (which pushes ``build``/``fix``
as a frame *on top of* a persisting ``chat`` root frame).

The pieces under test:

* a generic ``gate: operator`` step property (``flows.py``) — parsed onto
  ``Job``/``ResolvedJob``, only ``operator`` accepted, and a gate that gates no
  ``call``/``handoff`` route fails the build;
* the chat hand-off gate parks at **``awaiting_operator``** (not ``waiting``),
  and a static ``gate: operator`` + ``call X`` step parks there too instead of
  dispatching the call immediately;
* a new ``accept_call()`` action that performs the Phase-1 ``_dispatch_call``
  **push** (stack becomes ``[chat, build]``, NOT a re-rooted ``[build]``), never
  rewrites ``process_name``, and can carry an operator-edited ``draft_spec``;
* the ``chat`` step declares a ``terminate`` catch, and a PR merge/close on a
  chat-originated Execute task returns control to the **live** ``chat`` frame
  instead of ending the task (``transition_task`` becomes stack-aware);
* decline / keep-talking: ``send_message`` works from ``awaiting_operator`` and
  preserves the ``handoff_suggestion``;
* the ``accept-call`` API endpoint.

Written BEFORE the implementation lands, so they FAIL against the current
(pre-Phase-2) tree. New symbols the plan introduces (``accept_call`` /
``AcceptCallNotAllowed`` on the orchestrator) are imported/accessed *inside* the
test bodies so a missing symbol fails that test cleanly rather than breaking
module collection.

Reused harness from ``test_adr045_workflow_calls`` (the Phase 1 file):
``_bundled_service``, ``_seed``, ``_item_from``, ``_wf_names``, ``_HangRunner``,
``_CompletedRunner`` — same fixtures/factories, no bespoke infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import build_process
from lotsa.orchestrator import OrchestratorService
from lotsa.server.app import create_app
from lotsa.tests.conftest import FakeRunner, wait_for_completion
from lotsa.tests.test_adr045_workflow_calls import (
    _bundled_service,
    _CompletedRunner,
    _HangRunner,
    _seed,
    _wf_names,
)
from lotsa.tests.test_orchestrator import FakeRunner as ResultFakeRunner

# ``run`` / ``_loop`` fixtures come from ``lotsa/tests/conftest.py``.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ROOT = {"workflow": "chat", "step": "chat", "called_from": None}


def _chat_active_state(svc: OrchestratorService) -> str:
    """The resolved active state of the bundled ``chat`` step (``"chat"``)."""
    return svc._processes["chat"].flows["main"].jobs[0].active_state


def _route_targets(job) -> list[str]:
    return [r.target for r in job.rules]


def _write_gate_caller(path: Path, *, callee: str, gate_route: str) -> Path:
    """A one-agent-step workflow whose step declares ``gate: operator`` and
    routes ``COMPLETED`` to ``gate_route`` (e.g. ``call <callee>``)."""
    path.write_text(
        f"""
process: gate_caller
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: backlog
    active_state: gating
    gate: operator
    routes: {{ COMPLETED: {gate_route} }}
flows:
  main:
    steps:
      - name: gate
        prehooks: []
        posthooks: []
"""
    )
    return path


@pytest.fixture()
def chat_app(tmp_path, _loop, run):
    """A started service whose active process is bundled ``chat``, wrapped in the
    FastAPI app so the ``accept-call`` route can be exercised end to end."""
    (tmp_path / "data").mkdir()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="chat",
        model="sonnet",
        budget=5.0,
    )
    app = create_app(config)
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    run(svc.start())
    app.state.service = svc
    app.state.db = db
    yield app, svc
    run(svc.shutdown())
    run(db.close())


# ===========================================================================
# A. flows.py — the generic ``gate: operator`` property
# ===========================================================================


def test_gate_operator_parses_onto_resolved_step(tmp_path):
    """``gate: operator`` survives the build onto the ``ResolvedJob``.

    Fails pre-fix: ``Job``/``ResolvedJob`` have no ``gate`` field, so the
    attribute access raises ``AttributeError`` (the key is silently dropped).
    """
    f = _write_gate_caller(tmp_path / "gc.yaml", callee="pr-monitor", gate_route="call pr-monitor")
    process = build_process("gate_caller", process_file=f)
    step = process.flows["main"].jobs[0]
    assert step.gate == "operator", f"gate must resolve onto the step; got {getattr(step, 'gate', '<missing>')!r}"


def test_gate_with_a_call_route_builds(tmp_path):
    """A static gate (``gate: operator`` + ``routes: {COMPLETED: call X}``)
    builds and the call target survives."""
    f = _write_gate_caller(tmp_path / "gc.yaml", callee="pr-monitor", gate_route="call pr-monitor")
    process = build_process("gate_caller", process_file=f)
    step = process.flows["main"].jobs[0]
    assert "call pr-monitor" in _route_targets(step)


def test_gate_invalid_value_rejected_at_build(tmp_path):
    """Only ``operator`` is a legal gate value in Phase 2; anything else fails
    the build loudly.

    Fails pre-fix: ``gate:`` is an unrecognized YAML key, silently ignored — no
    ``ValueError`` is raised.
    """
    f = tmp_path / "bad_gate.yaml"
    f.write_text(
        """
process: bad_gate
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: gating
    active_state: gating
    gate: yolo
    routes: { COMPLETED: call pr-monitor }
flows:
  main:
    steps: [gate]
"""
    )
    with pytest.raises(ValueError, match="gate|operator"):
        build_process("bad_gate", process_file=f)


def test_gate_without_a_call_or_handoff_route_fails_build(tmp_path):
    """A ``gate: operator`` step that routes nothing to a ``call`` (or ``handoff``)
    gates nothing — a build-time error.

    Fails pre-fix: there is no gate validator; the process builds fine.
    """
    f = tmp_path / "empty_gate.yaml"
    f.write_text(
        """
process: empty_gate
jobs:
  - name: gate
    type: agent
    prompt: coding
    queue_state: gating
    active_state: gating
    gate: operator
    routes: { COMPLETED: next }
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
    with pytest.raises(ValueError, match="gate"):
        build_process("empty_gate", process_file=f)


def test_gate_on_a_handoff_route_builds(tmp_path):
    """The hand-off gate shape (``gate: operator`` + ``routes: {COMPLETED:
    handoff}``, the ``chat`` case) is a valid gate — ``handoff`` is a gateable
    destination-bound-at-accept route."""
    f = tmp_path / "handoff_gate.yaml"
    f.write_text(
        """
process: handoff_gate
jobs:
  - name: gate
    type: agent
    prompt: coding
    conversational: true
    gate: operator
    routes: { COMPLETED: handoff }
flows:
  main:
    steps: [gate]
"""
    )
    process = build_process("handoff_gate", process_file=f)
    assert process.flows["main"].jobs[0].gate == "operator"


# ===========================================================================
# B. chat/process.yaml — the terminate catch that makes the return path reachable
# ===========================================================================


def test_chat_step_declares_a_terminate_catch():
    """The bundled ``chat`` step declares a ``routes: { terminate: <target> }``
    catch so a callee's ``terminate`` (a merged/closed PR) unwinds back into the
    live ``chat`` frame instead of ending the task.

    Fails pre-fix: chat routes only ``COMPLETED → handoff`` — no ``terminate``
    catch (no rule with ``source == "terminate"``).
    """
    chat = build_process("chat")
    step = chat.flows["main"].jobs[0]
    catches = [r for r in step.rules if r.source == "terminate"]
    assert catches, f"chat must declare a terminate catch; rules={[(r.source, r.target) for r in step.rules]!r}"


# ===========================================================================
# C. The gate parks at ``awaiting_operator``
# ===========================================================================


def test_chat_handoff_parks_at_awaiting_operator(tmp_path, run):
    """A chat turn emitting ``AGENT_RESULT: COMPLETED build`` parks the REPL at
    **``awaiting_operator``** (the gate), recording the ``handoff_suggestion`` —
    not at plain ``waiting`` as Phase 1 did.

    Fails pre-fix: the drainer's handoff branch parks at ``status="waiting"``.
    """
    from rigg.models import AgentResult

    svc = _bundled_service(tmp_path, run, flow="chat")
    svc.runner = ResultFakeRunner(
        AgentResult(
            success=True,
            stdout="Full build.\nAGENT_RESULT: COMPLETED build",
            stderr="",
            return_code=0,
            duration_ms=5,
            model="sonnet",
            session_id="s1",
        )
    )
    run(svc.start())
    try:
        task = run(svc.create_task(message="let's talk", process_name="chat"))
        run(wait_for_completion(svc, task.id))
        # Let the completion drainer settle onto the parked status.
        import asyncio

        async def _settle():
            for _ in range(100):
                row = await svc.db.get_task(task.id)
                if row.status != "working":
                    return
                await asyncio.sleep(0.02)

        run(_settle())
        row = run(svc.db.get_task(task.id))
        assert row.status == "awaiting_operator", (
            f"the hand-off gate must park at awaiting_operator, got {row.status!r}"
        )
        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) == "build"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_static_gate_call_parks_instead_of_pushing(tmp_path, run):
    """An AGENT step with ``gate: operator`` + ``routes: {COMPLETED: call X}``
    parks at ``awaiting_operator`` when the agent emits ``COMPLETED`` — it does
    NOT push the callee frame / dispatch the call.

    Fails pre-fix: the gate is ignored, so the ``COMPLETED`` route runs
    ``_route_stack_target`` → ``_dispatch_call`` immediately: the callee frame is
    pushed (stack depth 2) and the task lands ``working``/``waiting_for_pr``,
    never ``awaiting_operator``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    caller = _write_gate_caller(tmp_path / "gc.yaml", callee="pr-monitor", gate_route="call pr-monitor")
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        flow="gate_caller",
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
        task = run(svc.create_task("static gate"))
        run(wait_for_completion(svc, task.id))
        import asyncio

        async def _settle():
            for _ in range(150):
                row = await db.get_task(task.id)
                if row.status != "working":
                    return
                await asyncio.sleep(0.02)

        run(_settle())
        row = run(db.get_task(task.id))
        assert row.status == "awaiting_operator", (
            f"a gated call must park at awaiting_operator; got status={row.status!r} state={row.state!r}"
        )
        assert _wf_names(row) == ["gate_caller"], (
            f"a gated call must NOT push the callee before accept; stack={_wf_names(row)!r}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


# ===========================================================================
# D. accept_call — the load-bearing push (NOT a re-root)
# ===========================================================================


def test_accept_call_pushes_frame_not_reroot(tmp_path, run):
    """``accept_call`` performs the Phase-1 push: ``chat`` stays as frame 0 and
    ``build`` is pushed as frame 1 (``called_from="chat"``). ``process_name`` is
    NOT rewritten — the root process is still ``chat``.

    This is the anti-regression against ``promote_task``'s re-root.

    Fails pre-fix: ``OrchestratorService.accept_call`` does not exist
    (``AttributeError``).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
        )
        svc.runner = _HangRunner()  # keep the pushed build from advancing under assertion
        run(svc.accept_call(task.id, to_workflow="build"))
        row = run(svc.db.get_task(task.id))
        assert _wf_names(row) == ["chat", "build"], (
            f"accept must PUSH build onto the persisting chat frame, not re-root; stack={_wf_names(row)!r}"
        )
        assert row.metadata["call_stack"][-1]["called_from"] == "chat", "the pushed frame records the chat call site"
        pname = row.metadata.get("process_name")
        assert pname == "chat", f"a gated call must not touch process_name (chat is still the root); got {pname!r}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_accept_call_writes_edited_draft_spec(tmp_path, run):
    """An operator-edited spec passed to ``accept_call`` is persisted as the
    ``draft_spec`` artifact (latest-wins) so ``build``'s planning injection reads
    it — no transcript re-seeding needed (chat's artifacts are already
    task-scoped).

    Fails pre-fix: ``accept_call`` does not exist (``AttributeError``).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
        )
        svc.runner = _HangRunner()
        run(svc.accept_call(task.id, to_workflow="build", draft_spec="THE EDITED SPEC"))
        assert run(svc.get_named_artifact(task.id, "draft_spec")) == "THE EDITED SPEC"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_accept_call_lost_cas_writes_nothing(tmp_path, run):
    """A lost push CAS (concurrent decline / duplicate accept) must leave NO side
    effect: the ``draft_spec`` artifact is not persisted and no ``artifact_seeded``
    audit row is written — the Constitution §3.1 / ADR-020 "CAS-loser writes
    nothing" rule (``draft_spec`` seeding is threaded through ``_dispatch_call`` so
    it only fires after a won CAS, mirroring ``promote_task``'s save-after-CAS).

    The race is exercised from *inside* the code under test: a one-shot wrapper on
    ``atomic_transition`` flips the row out of ``awaiting_operator`` the instant
    the push CAS is attempted, so the real CAS (``from_status='awaiting_operator'``)
    loses for real — not a pre-flipped post-bug state.

    Fails pre-fix: the pre-fix ``accept_call`` saved ``draft_spec`` + the audit row
    *before* calling ``_dispatch_call``, so the orphaned artifact ("THE EDITED
    SPEC") and message persist even though the call never pushed.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
        )
        svc.runner = _HangRunner()

        orig_transition = svc.db.atomic_transition
        flipped = {"done": False}

        async def _flip_then_transition(*args, **kwargs):
            # On the push CAS (guarded on the gate status), simulate a concurrent
            # decline landing first so the real CAS loses.
            if not flipped["done"] and kwargs.get("from_status") == "awaiting_operator":
                flipped["done"] = True
                await svc.db.update_task(task.id, status="working")
            return await orig_transition(*args, **kwargs)

        svc.db.atomic_transition = _flip_then_transition
        try:
            run(svc.accept_call(task.id, to_workflow="build", draft_spec="THE EDITED SPEC"))
        finally:
            svc.db.atomic_transition = orig_transition

        row = run(svc.db.get_task(task.id))
        assert _wf_names(row) == ["chat"], f"a lost CAS must not push a frame; stack={_wf_names(row)!r}"
        assert run(svc.get_named_artifact(task.id, "draft_spec")) is None, (
            "a lost CAS must not persist the operator-edited draft_spec artifact"
        )
        seeded = [m for m in run(svc.get_messages(task.id)) if m.msg_type == "artifact_seeded"]
        assert not seeded, f"a lost CAS must not write an artifact_seeded audit row; got {seeded!r}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_accept_call_requires_awaiting_operator(tmp_path, run):
    """``accept_call`` is CAS-guarded on ``from_status="awaiting_operator"`` — a
    task that is not parked at a gate is rejected.

    Fails pre-fix: ``accept_call`` / ``AcceptCallNotAllowed`` do not exist.
    """
    from lotsa.orchestrator import AcceptCallNotAllowed

    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="waiting",  # a plain REPL park, not the gate
            stack=[dict(CHAT_ROOT)],
        )
        with pytest.raises(AcceptCallNotAllowed):
            run(svc.accept_call(task.id, to_workflow="build"))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_accept_call_rejects_unlisted_destination(tmp_path, run):
    """The hand-off gate only accepts a ``hand-off``-invocable destination — the
    same "never accept a destination the operator wasn't shown" rule the drainer
    enforces. ``chat`` (``invocable: [start]``) is not offerable.

    Fails pre-fix: ``accept_call`` / ``AcceptCallNotAllowed`` do not exist.
    """
    from lotsa.orchestrator import AcceptCallNotAllowed

    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
        )
        with pytest.raises(AcceptCallNotAllowed):
            run(svc.accept_call(task.id, to_workflow="chat"))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_accept_call_rejected_after_execute_terminated(tmp_path, run):
    """A ``chat`` frame that has already caught a callee ``terminate`` is
    terminal-ish: it may talk (Q&A) but may **not** call again (a fresh build is
    a new task). ``accept_call`` on such a task is rejected.

    Fails pre-fix: ``accept_call`` / ``AcceptCallNotAllowed`` do not exist.
    """
    from lotsa.orchestrator import AcceptCallNotAllowed

    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
            extra={"execute_terminated": True},
        )
        with pytest.raises(AcceptCallNotAllowed):
            run(svc.accept_call(task.id, to_workflow="build"))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# E. Decline / keep talking
# ===========================================================================


def test_send_message_from_gate_returns_to_repl_preserving_suggestion(tmp_path, run):
    """Declining the gate is the natural operator gesture — sending a chat
    message re-dispatches the ``chat`` REPL (back to ``working``) and the
    ``handoff_suggestion`` artifact is preserved.

    Fails pre-fix: ``send_message`` rejects ``status="awaiting_operator"``
    (``ReviseNotAllowed``: requires waiting/needs_input/blocked).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        task = _seed(
            svc,
            run,
            state=_chat_active_state(svc),
            current_step="chat",
            status="awaiting_operator",
            stack=[dict(CHAT_ROOT)],
        )
        run(
            svc.db.add_message(
                task.id, "agent", "chat", "build", "artifact", metadata={"artifact_name": "handoff_suggestion"}
            )
        )
        svc.runner = _HangRunner()  # the re-dispatched chat hangs → row stays working
        run(svc.send_message(task.id, "actually, let's keep talking"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "working", f"declining re-enters the chat REPL; got status={row.status!r}"
        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) == "build", (
            "the suggestion must survive a decline"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# F. The return path — a PR terminal unwinds into chat's catch
#
# Discovered gap: pr-monitor's merge/close is applied by ``transition_task`` as a
# FLAT terminal CAS (not a ``terminate`` route through ``_unwind_terminate``), so
# the spec's "return path already built, now reachable" is NOT true against
# Phase 1 code. ``transition_task`` must become stack-aware.
# ===========================================================================


def _pr_terminal_chat_stack(svc: OrchestratorService) -> tuple[str, str, list[dict]]:
    """A three-frame stack ``chat → build → pr-monitor`` parked at the monitor,
    where ``chat``'s call-site step catches ``terminate``. Returns
    (monitor_state, monitor_step, stack)."""
    mon = svc._processes["pr-monitor"].flows["main"].jobs[0]
    stack: list[dict] = [
        {"workflow": "chat", "step": "chat", "called_from": None},
        {"workflow": "build", "step": "push_pr", "called_from": "chat"},
        {"workflow": "pr-monitor", "step": mon.name, "called_from": "push_pr"},
    ]
    return mon.queue_state, mon.name, stack


def test_pr_merge_with_chat_caller_returns_to_live_chat(tmp_path, run):
    """A merged PR (``transition_task(complete)``) on a ``chat → build →
    pr-monitor`` task does NOT end the task — it unwinds to ``chat``'s
    ``terminate`` catch, leaving the ``chat`` frame live (parked, not
    ``complete``) with only ``["chat"]`` on the stack.

    Fails pre-fix: ``transition_task`` applies a flat terminal CAS → the task is
    ``complete`` and the call stack is left intact (never unwound).
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        mon_state, mon_step, stack = _pr_terminal_chat_stack(svc)
        task = _seed(
            svc,
            run,
            state=mon_state,
            current_step=mon_step,
            status="waiting_for_pr",
            stack=stack,
            extra={"pr_number": 42},
        )
        run(svc.transition_task(task.id, "complete"))
        row = run(svc.db.get_task(task.id))
        assert row.status != "complete", (
            f"a chat-originated task must return to chat on merge, not complete; status={row.status!r}"
        )
        assert row.state != "complete", f"the task must not be driven to the terminal state; state={row.state!r}"
        assert _wf_names(row) == ["chat"], (
            f"terminate must unwind build+pr-monitor back to the chat frame; stack={_wf_names(row)!r}"
        )
        assert row.current_step == "chat", f"the live chat step is restored; got current_step={row.current_step!r}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_pr_merge_flags_execute_terminated(tmp_path, run):
    """When the return-path catch fires, the ``chat`` frame is marked
    ``execute_terminated`` so a subsequent ``call`` from it is rejected (see
    ``test_accept_call_rejected_after_execute_terminated``) — the "may talk but
    may not call" rule.

    Fails pre-fix: the terminal is flat-applied (task ``complete``); no such flag
    is ever written.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        mon_state, mon_step, stack = _pr_terminal_chat_stack(svc)
        task = _seed(
            svc,
            run,
            state=mon_state,
            current_step=mon_step,
            status="waiting_for_pr",
            stack=stack,
            extra={"pr_number": 42},
        )
        run(svc.transition_task(task.id, "complete"))
        row = run(svc.db.get_task(task.id))
        assert row.metadata.get("execute_terminated") is True, (
            f"the caught chat frame must be flagged execute_terminated; metadata={row.metadata!r}"
        )
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_pr_merge_on_direct_build_still_completes(tmp_path, run):
    """Behaviour-preservation: a directly-selected ``build`` task (stack
    ``[build, pr-monitor]``, no chat catch anywhere) still COMPLETES on merge —
    the stack-aware terminal path unwinds with no catch, reproducing the flat
    completion, and the stack is emptied.

    Fails pre-fix on the stack assertion: the flat terminal CAS never touches
    ``call_stack``, so it stays ``["build", "pr-monitor"]`` instead of unwinding
    to ``[]``.
    """
    svc = _bundled_service(tmp_path, run, flow="build")
    run(svc.start())
    try:
        mon = svc._processes["pr-monitor"].flows["main"].jobs[0]
        task = _seed(
            svc,
            run,
            state=mon.queue_state,
            current_step=mon.name,
            status="waiting_for_pr",
            stack=[
                {"workflow": "build", "step": "push_pr", "called_from": None},
                {"workflow": "pr-monitor", "step": mon.name, "called_from": "push_pr"},
            ],
            extra={"pr_number": 7},
        )
        run(svc.transition_task(task.id, "complete"))
        row = run(svc.db.get_task(task.id))
        assert row.status == "complete", f"a direct build must still complete on merge; status={row.status!r}"
        assert _wf_names(row) == [], f"an uncaught terminal unwinds the whole stack; stack={_wf_names(row)!r}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ===========================================================================
# G. accept-call API endpoint
# ===========================================================================


def test_accept_call_endpoint_pushes_frame(chat_app, run):
    """``POST /api/tasks/{id}/accept-call`` with ``{to_workflow}`` performs the
    push and returns the task detail (200).

    Fails pre-fix: the route does not exist (404).
    """
    app, svc = chat_app

    async def _test():
        task = await svc.db.create_task(
            "gate endpoint",
            state=_chat_active_state(svc),
            status="awaiting_operator",
            current_step="chat",
            metadata={"process_name": "chat", "call_stack": [dict(CHAT_ROOT)]},
        )
        svc.runner = _HangRunner()  # keep the pushed build from advancing
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task.id}/accept-call", json={"to_workflow": "build"})
            assert resp.status_code == 200, resp.text
        row = await svc.db.get_task(task.id)
        assert [f["workflow"] for f in row.metadata.get("call_stack") or []] == ["chat", "build"], (
            f"the endpoint must push build onto chat; stack={row.metadata.get('call_stack')!r}"
        )

    run(_test())


def test_accept_call_endpoint_wrong_status_is_rejected(chat_app, run):
    """The endpoint surfaces the ``awaiting_operator`` guard as a client error
    (not a 200 / not a 500) when the task is not parked at a gate.

    Fails pre-fix: the route does not exist (404, not the intended 400).
    """
    app, svc = chat_app

    async def _test():
        task = await svc.db.create_task(
            "not at a gate",
            state=_chat_active_state(svc),
            status="waiting",
            current_step="chat",
            metadata={"process_name": "chat", "call_stack": [dict(CHAT_ROOT)]},
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task.id}/accept-call", json={"to_workflow": "build"})
            assert resp.status_code == 400, f"expected a 400 guard rejection, got {resp.status_code}: {resp.text}"

    run(_test())
