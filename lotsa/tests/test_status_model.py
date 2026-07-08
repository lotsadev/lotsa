"""Acceptance tests for the (status, current_step) model."""

from __future__ import annotations

import asyncio

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from lotsa.tests.conftest import wait_for_status
from rigg.models import AgentResult


class FakeRunner:
    def __init__(self, result: AgentResult):
        self.result = result
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.calls.append({"user_prompt": user_prompt, "work_dir": work_dir, **kwargs})
        return self.result


@pytest.fixture()
def conv_service(tmp_path, _loop, run):
    """Service with a single conversational step that has output='spec'."""
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text(
        "name: conv\njobs:\n"
        "  - name: spec\n    prompt: spec\n    conversational: true\n"
        "    output: spec\n"
        "    rules:\n"
        "      - source: stdout\n        pattern: '^SPEC_COMPLETE:'\n        target: next\n"
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "spec-system.md").write_text("# spec\n")
    (prompts / "spec-user.md").write_text("{title}\n{body}\n")
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    yield svc
    run(svc.shutdown())
    run(db.close())


def test_spec_complete_persists_artifact_before_approve(conv_service, run):
    svc = conv_service
    svc.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="SPEC_COMPLETE: Build a thing\n## Requirements\n- Be fast",
            stderr="",
            return_code=0,
            duration_ms=100,
            session_id="s1",
        )
    )
    task = run(svc.create_task(message="Build a thing"))
    # Wait for the drainer to process the agent result. Poll DB status.
    run(wait_for_status(svc, task.id, "waiting"))

    # Artifact must exist *before* approve() is called.
    artifacts = run(svc.db.get_messages(task.id, msg_type="artifact"))
    assert len(artifacts) == 1
    assert artifacts[0].metadata.get("artifact_name") == "spec"
    assert "## Requirements" in artifacts[0].content
    assert "SPEC_COMPLETE" not in artifacts[0].content


def test_drainer_writes_waiting_status_for_conversational(conv_service, run):
    svc = conv_service
    svc.runner = FakeRunner(
        AgentResult(
            success=True, stdout="SPEC_COMPLETE: x\n## R\n- a", stderr="", return_code=0, duration_ms=10, session_id="s"
        )
    )
    task = run(svc.create_task(message="x"))
    run(wait_for_status(svc, task.id, "waiting"))
    fresh = run(svc.db.get_task(task.id))
    assert fresh.status == "waiting"
    assert fresh.current_step == "spec"


def test_needs_input_sets_status_and_persists_question(tmp_path, _loop, run):
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="Working...\nNEEDS_INPUT: Use Postgres or SQLite?",
            stderr="",
            return_code=0,
            duration_ms=100,
        )
    )
    run(svc.start())
    try:
        task = run(svc.create_task("Q"))
        run(wait_for_status(svc, task.id, "needs_input"))
        fresh = run(db.get_task(task.id))
        assert fresh.status == "needs_input"
        assert fresh.current_step == "coding"
        questions = run(db.get_messages(task.id, msg_type="question"))
        assert len(questions) == 1
        assert questions[-1].content == "Use Postgres or SQLite?"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_agent_failure_sets_blocked_status(tmp_path, _loop, run):
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=False, stdout="", stderr="boom", return_code=1, duration_ms=10))
    run(svc.start())
    try:
        task = run(svc.create_task("Fail"))
        run(wait_for_status(svc, task.id, "blocked"))
        fresh = run(db.get_task(task.id))
        assert fresh.status == "blocked"
        assert fresh.current_step == "coding"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_auto_advance_to_complete_sets_status_complete(tmp_path, _loop, run):
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n")  # no evaluate -> auto-advance
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="done", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        task = run(svc.create_task("auto"))
        run(wait_for_status(svc, task.id, "complete"))
        fresh = run(db.get_task(task.id))
        assert fresh.status == "complete"
        assert fresh.current_step is None
    finally:
        run(svc.shutdown())
        run(db.close())


def test_approve_rejects_when_status_not_waiting(tmp_path, _loop, run):
    from lotsa.orchestrator import ApproveNotAllowed

    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        # No agent run yet → status='working' (or initial). approve must reject.
        task = run(db.create_task(title="x", flow_name="t", status="working", current_step="coding"))
        with pytest.raises(ApproveNotAllowed):
            run(svc.approve(task.id))
    finally:
        run(svc.shutdown())
        run(db.close())


def test_approve_rejects_when_output_artifact_missing(conv_service, run):
    from lotsa.orchestrator import ApproveNotAllowed

    svc = conv_service
    # Force a waiting status without an artifact in the DB.
    task = run(svc.db.create_task(title="x", flow_name="conv", status="waiting", current_step="spec"))
    with pytest.raises(ApproveNotAllowed):
        run(svc.approve(task.id))


def test_answer_rejects_when_status_not_needs_input(tmp_path, _loop, run):
    from lotsa.orchestrator import AnswerNotAllowed

    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(db.create_task(title="x", flow_name="t", status="waiting", current_step="coding"))
        with pytest.raises(AnswerNotAllowed):
            run(svc.answer(task.id, "no"))
    finally:
        run(svc.shutdown())
        run(db.close())


def test_revise_rejects_when_status_blocked(tmp_path, _loop, run):
    from lotsa.orchestrator import ReviseNotAllowed

    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(db.create_task(title="x", flow_name="t", status="blocked", current_step="coding"))
        with pytest.raises(ReviseNotAllowed):
            run(svc.revise(task.id, "x"))
    finally:
        run(svc.shutdown())
        run(db.close())


def test_retry_rejects_unless_blocked(tmp_path, _loop, run):
    from lotsa.orchestrator import RetryNotAllowed

    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(db.create_task(title="x", flow_name="t", status="waiting", current_step="coding"))
        with pytest.raises(RetryNotAllowed):
            run(svc.retry(task.id))
    finally:
        run(svc.shutdown())
        run(db.close())


def test_retry_rejects_rebasing_with_revise_hint(tmp_path, _loop, run):
    """Regression: retry() on a 'rebasing' (NON_FAST_FORWARD) task must raise
    rather than re-attempt the same raw push that just got rejected.
    The error must point the user at Revise, which is the actual recovery."""
    from lotsa.orchestrator import RetryNotAllowed

    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="rebase-required", flow_name="build", status="blocked", current_step="push", state="rebasing"
            )
        )
        with pytest.raises(RetryNotAllowed, match="non-fast-forward"):
            run(svc.retry(task.id))
        # Row must be untouched.
        row = run(db.get_task(task.id))
        assert row.status == "blocked" and row.state == "rebasing"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_retry_blocked_re_dispatches_same_step(tmp_path, _loop, run):
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        task = run(db.create_task(title="x", flow_name="t", status="blocked", current_step="coding"))
        run(svc.retry(task.id))
        run(wait_for_status(svc, task.id, "waiting"))
        fresh = run(db.get_task(task.id))
        assert fresh.current_step == "coding"
        assert fresh.status == "waiting"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_retry_from_drainer_blocked_state_does_not_raise(tmp_path, _loop, run):
    """The drainer writes state='blocked' on agent failure (not the DB default
    state='backlog'). retry() must dispatch successfully from that starting
    state — _dispatch_step's state_machine.transition starts from the Item's
    queue_state, not the DB row's state, so the (queue_state, active_state)
    edge is the one being walked.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        # Mirror the post-drainer state for a failed agent: state='blocked'.
        task = run(
            db.create_task(
                title="drainer-blocked",
                flow_name="t",
                status="blocked",
                current_step="coding",
                state="blocked",
            )
        )
        run(svc.retry(task.id))
        run(wait_for_status(svc, task.id, "waiting"))
        fresh = run(db.get_task(task.id))
        assert fresh.status == "waiting"
        assert fresh.current_step == "coding"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_start_resumes_interrupted_working_tasks(tmp_path, _loop, run):
    """ADR-040 — a ``working`` task on restart is treated as *interrupted* and
    resumed, not destroyed. The sweep records ``interrupted_at`` + ``resume_count``
    in metadata and re-dispatches the step instead of unconditionally flipping
    the row to ``blocked`` (the pre-ADR-040 destructive behaviour)."""
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    # Pre-create a "working" task simulating a server crash mid-run.
    task = run(db.create_task(title="killed", flow_name="t", status="working", current_step="coding"))
    svc = OrchestratorService(config, db)
    # Stub the runner so the resume dispatch doesn't shell out to real claude.
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        fresh = run(db.get_task(task.id))
        assert fresh.status != "blocked", "an interrupted working task must be resumed, not blocked"
        assert fresh.metadata.get("interrupted_at") is not None
        assert int(fresh.metadata.get("resume_count", 0)) >= 1
        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        assert any("restart" in m.content.lower() for m in msgs)
    finally:
        run(svc.shutdown())
        run(db.close())


def test_restart_preserves_spec_artifact_and_waiting_status(tmp_path, _loop, run):
    """Bug 1+3: artifact persists across restart; status stays 'waiting'."""
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text(
        "name: conv\njobs:\n"
        "  - name: spec\n    prompt: spec\n    conversational: true\n"
        "    output: spec\n    queue_state: speccing\n    active_state: spec\n"
        "    rules:\n"
        "      - source: stdout\n        pattern: '^SPEC_COMPLETE:'\n        target: next\n"
        "  - name: plan\n    prompt: planning\n    inputs: [spec]\n"
        "    queue_state: backlog\n    active_state: planning\n"
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for n in ("spec-system", "spec-user", "planning-system", "planning-user"):
        (prompts / f"{n}.md").write_text("# stub")
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())

    svc1 = OrchestratorService(config, db)
    svc1.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="SPEC_COMPLETE: build x\n## R\n- a",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="s1",
        )
    )
    run(svc1.start())
    task = run(svc1.create_task(message="build x"))
    run(wait_for_status(svc1, task.id, "waiting"))
    run(svc1.shutdown())

    # Pretend the server restarted: drop the service, keep the DB, build a new svc.
    db2 = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db2.initialize())
    svc2 = OrchestratorService(config, db2)
    svc2.runner = FakeRunner(AgentResult(success=True, stdout="planning ok", stderr="", return_code=0, duration_ms=10))
    run(svc2.start())
    try:
        fresh = run(db2.get_task(task.id))
        assert fresh.status == "waiting"
        assert fresh.current_step == "spec"
        artifacts = run(db2.get_messages(task.id, msg_type="artifact"))
        assert len(artifacts) == 1
        assert "## R" in artifacts[-1].content

        # Approving must dispatch the plan step (it sees the artifact).
        run(svc2.approve(task.id))
        run(asyncio.sleep(0.2))
        # plan should have been dispatched (status returns to working then waiting/complete).
        post = run(db2.get_task(task.id))
        assert post.current_step in ("plan",) or post.status == "complete"
    finally:
        run(svc2.shutdown())
        run(db2.close())


def test_plan_artifact_present_when_status_is_waiting(tmp_path, _loop, run):
    """Regression: plan artifact must be in DB by the time status='waiting' is set.

    The `plan` step in the full flow is evaluate=true + output='plan'.
    After the agent completes, the artifact write must be committed BEFORE
    the task status is set to 'waiting', otherwise the frontend sees
    status='waiting' but no artifact → canApprove is false → Accept button
    never shows.

    This test intercepts atomic_transition to snapshot artifacts AT THE
    EXACT MOMENT status='waiting' is written, catching the fire-and-forget
    race.  (The drainer's auto-advance writes go through atomic_transition
    so a concurrent block() can't silently clobber them — ADR-020 Phase 2
    migrated the drainer CAS sites off the raw claim_task_transition
    primitive.)
    """
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text(
        "name: full\njobs:\n"
        "  - name: spec\n    prompt: spec\n    conversational: true\n"
        "    output: spec\n    queue_state: speccing\n    active_state: spec\n"
        "    rules:\n"
        "      - source: stdout\n        pattern: '^SPEC_COMPLETE:'\n        target: next\n"
        "  - name: plan\n    prompt: planning\n    evaluate: true\n"
        "    inputs: [spec]\n    output: plan\n"
        "    queue_state: backlog\n    active_state: planning\n    gate_state: planned\n"
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for n in ("spec-system", "spec-user", "planning-system", "planning-user"):
        (prompts / f"{n}.md").write_text("# stub\n{title}\n{body}\n")
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "data").mkdir()
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)

    # Track artifacts snapshot taken at the exact moment status='waiting' is set
    artifacts_at_waiting: list[list] = []

    # Intercept atomic_transition so we can snapshot artifacts synchronously
    _original_cas = db.atomic_transition

    async def _intercepted_cas(task_id, **kwargs):
        if kwargs.get("to_status") == "waiting" and kwargs.get("to_current_step") == "plan":
            # Snapshot artifact DB state RIGHT NOW, before any event loop yields
            snap = await db.get_messages(task_id, msg_type="artifact")
            artifacts_at_waiting.append(snap)
        return await _original_cas(task_id, **kwargs)

    db.atomic_transition = _intercepted_cas  # type: ignore[method-assign]

    # --- Spec step ---
    svc.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="SPEC_COMPLETE: Build a thing\n## Requirements\n- Be fast",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="s1",
        )
    )
    run(svc.start())
    task = run(svc.create_task(message="Build a thing"))
    run(wait_for_status(svc, task.id, "waiting"))

    # Approve spec → dispatches plan
    svc.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="## Plan\n- Step 1: Do something\n- Step 2: Do another thing",
            stderr="",
            return_code=0,
            duration_ms=10,
        )
    )
    run(svc.approve(task.id))

    # Wait for plan step to complete and set status=waiting
    run(wait_for_status(svc, task.id, "waiting"))

    # Verify the interceptor fired
    assert artifacts_at_waiting, "Interceptor never captured artifacts-at-waiting"

    # KEY ASSERTION: at the exact moment status='waiting' was written,
    # the plan artifact must already be in the DB.
    snap = artifacts_at_waiting[0]
    plan_artifacts = [a for a in snap if a.metadata.get("artifact_name") == "plan"]
    assert len(plan_artifacts) >= 1, (
        f"plan artifact not in DB when status='waiting' was written. "
        f"All artifacts at that moment: {[(a.metadata, a.content[:40]) for a in snap]}"
    )
    assert "Step 1" in plan_artifacts[-1].content

    run(svc.shutdown())
    run(db.close())


def test_auto_advance_skips_waiting(tmp_path, _loop, run):
    """A non-evaluate step that produces its declared artifact and no rule
    routing must dispatch the next step inline — never visible as 'waiting'."""
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: code\n    output: artifact\n  - name: review\n    evaluate: true\n")
    prompts = tmp_path / "p"
    prompts.mkdir()
    for n in ("code-system", "code-user", "review-system", "review-user"):
        (prompts / f"{n}.md").write_text("# stub")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(
        AgentResult(
            success=True, stdout="# Output\n\nThe step's artifact body.", stderr="", return_code=0, duration_ms=10
        )
    )
    run(svc.start())
    try:
        task = run(svc.create_task("Auto"))
        # The first step (code) auto-advances to (review). review has evaluate=true,
        # so the task ends in waiting at "review", not at "code".
        run(wait_for_status(svc, task.id, "waiting"))
        fresh = run(db.get_task(task.id))
        assert fresh.current_step == "review"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_approve_only_one_wins(conv_service, run):
    """TOCTOU regression: two concurrent approve() calls must not both dispatch.

    Before the atomic CAS in db.claim_task_transition, two requests reading
    the same (status='waiting', state=active_state) snapshot would both pass
    the read-then-check guard, both transition state, both dispatch the next
    step. The second dispatch would overwrite ``_in_flight[task_id]``, orphan
    the first agent task reference, and run two agents in parallel against
    the same worktree.
    """
    svc = conv_service
    svc.runner = FakeRunner(
        AgentResult(
            success=True,
            stdout="SPEC_COMPLETE: x\n## R\n- a",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="s",
        )
    )
    task = run(svc.create_task(message="x"))
    run(wait_for_status(svc, task.id, "waiting"))

    async def _race():
        # Fire two approves in parallel. One must win, one must be a no-op.
        results = await asyncio.gather(
            svc.approve(task.id),
            svc.approve(task.id),
            return_exceptions=True,
        )
        return results

    from lotsa.orchestrator import ApproveNotAllowed

    results = run(_race())
    # The loser either returns silently (saw the same waiting snapshot but
    # lost the CAS) or raises ApproveNotAllowed (read after the winner
    # already updated status). Both are acceptable; what's NOT acceptable is
    # both succeeding by both side-effecting — caught below.
    for r in results:
        assert r is None or isinstance(r, ApproveNotAllowed), f"unexpected error: {r!r}"

    # Exactly one transition happened: spec auto-advances to its success
    # state once, not twice. There must be exactly one 'Approved' feedback
    # message regardless of whether the loser silently no-op'd or raised.
    feedbacks = run(svc.db.get_messages(task.id, msg_type="feedback"))
    approved = [m for m in feedbacks if m.content == "Approved"]
    assert len(approved) == 1, f"Expected exactly one 'Approved' message, got {len(approved)}"


def test_start_does_not_re_block_already_blocked_rebasing(tmp_path, _loop, run):
    """A task that crashed mid-rebase and was already moved to blocked must not
    accumulate a new "Server restarted while task was rebasing" message on
    every subsequent restart.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    # Pre-seed: task already in (state='rebasing', status='blocked') from a
    # prior NON_FAST_FORWARD push that was already handled.
    task = run(
        db.create_task(
            title="rebased",
            flow_name="t",
            state="rebasing",
            status="blocked",
            current_step="push",
        )
    )

    # First start — should NOT add a duplicate message because status is
    # already 'blocked'.
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        restart_msgs = [m for m in msgs if "restart" in m.content.lower()]
        assert restart_msgs == [], (
            f"start() should skip already-blocked rebasing tasks; got messages: {[m.content for m in restart_msgs]}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_retry_only_one_wins(tmp_path, _loop, run):
    """Race regression: two concurrent retry() calls on a blocked task must
    not both pass the status guard and both spawn an agent. Same race shape
    as the approve() one — fixed by an atomic claim_task_transition CAS that
    flips status from blocked to working before _dispatch_step runs.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        task = run(db.create_task(title="retry race", flow_name="t", status="blocked", current_step="coding"))

        from lotsa.orchestrator import RetryNotAllowed

        async def _race():
            return await asyncio.gather(
                svc.retry(task.id),
                svc.retry(task.id),
                return_exceptions=True,
            )

        results = run(_race())
        # Loser is either silent (lost CAS) or RetryNotAllowed (read after winner).
        for r in results:
            assert r is None or isinstance(r, RetryNotAllowed), f"unexpected: {r!r}"

        # Exactly one "Retrying" status_change message — not two — proves a
        # single dispatch path executed.
        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        retrying = [m for m in msgs if m.content == "Retrying"]
        assert len(retrying) == 1, f"Expected one 'Retrying' message, got {len(retrying)}: {[m.content for m in msgs]}"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_answer_only_one_wins(tmp_path, _loop, run):
    """Race regression: two concurrent answer() calls on a needs_input task
    must not both pass the guard and both dispatch. Same shape as the
    approve()/retry() races; closed by reusing claim_task_transition.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="ans race",
                flow_name="t",
                status="needs_input",
                current_step="coding",
                state="coding",
            )
        )

        from lotsa.orchestrator import AnswerNotAllowed

        async def _race():
            return await asyncio.gather(
                svc.answer(task.id, "first"),
                svc.answer(task.id, "second"),
                return_exceptions=True,
            )

        results = run(_race())
        for r in results:
            assert r is None or isinstance(r, AnswerNotAllowed), f"unexpected: {r!r}"

        # Exactly one 'answer' message lands — proves only one dispatch happened.
        msgs = run(db.get_messages(task.id, msg_type="answer"))
        assert len(msgs) == 1, f"Expected one answer message, got {len(msgs)}: {[m.content for m in msgs]}"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_rules_no_match_blocks_status(tmp_path, _loop, run):
    """Drainer's rules-defined-but-none-matched branch must update status to
    'blocked', not just the legacy state column. Otherwise the React UI sees
    status='working' indefinitely and the Retry button never appears.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text(
        "name: t\njobs:\n"
        "  - name: review\n    prompt: review\n"
        "    rules:\n"
        "      - source: stdout\n        pattern: '^REVIEW_PASS'\n        target: next\n"
        "      - source: stdout\n        pattern: '^REVIEW_FAIL'\n        target: blocked\n"
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "review-system.md").write_text("# stub")
    (prompts / "review-user.md").write_text("{title}\n{body}")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    # Agent stdout matches NEITHER REVIEW_PASS nor REVIEW_FAIL — rule list
    # evaluated, no match → drainer falls into the "no recognized marker" branch.
    svc.runner = FakeRunner(
        AgentResult(success=True, stdout="rambling output with no marker", stderr="", return_code=0, duration_ms=10)
    )
    run(svc.start())
    try:
        task = run(svc.create_task("blocked test"))
        run(wait_for_status(svc, task.id, "blocked"))
        fresh = run(db.get_task(task.id))
        assert fresh.status == "blocked"
        assert fresh.current_step == "review"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_push_retry_only_one_wins(tmp_path, _loop, run):
    """Race regression: two concurrent retry() calls on a blocked push task
    must not both write 'Retrying push' messages or both reset state→pushing.
    The _dispatching_push set in _dispatch_next_step prevents duplicate
    _execute_push spawning, but the audit trail (status_change message,
    state column write) needs the same CAS guard the non-push branch uses.
    """
    # Use the bundled "full" flow — it has pr_config and the pr-fix job
    # registered, which the push retry branch needs.
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="push retry race",
                flow_name="build",
                status="blocked",
                current_step="push",
                # state='pushing' — task crashed mid-push, retry is the right
                # recovery.  ('rebasing' would be NON_FAST_FORWARD, where
                # retry() raises RetryNotAllowed and revise() is the recovery.)
                state="pushing",
            )
        )

        # Patch _execute_push so the test doesn't actually try to git push.
        from unittest.mock import AsyncMock

        svc._execute_push = AsyncMock()

        from lotsa.orchestrator import RetryNotAllowed

        async def _race():
            return await asyncio.gather(
                svc.retry(task.id),
                svc.retry(task.id),
                return_exceptions=True,
            )

        results = run(_race())
        for r in results:
            assert r is None or isinstance(r, RetryNotAllowed), f"unexpected: {r!r}"

        # Exactly one 'Retrying push' status_change message — proves the audit
        # trail wasn't duplicated by the second retry.
        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        retrying = [m for m in msgs if m.content == "Retrying push"]
        assert len(retrying) == 1, f"Expected one 'Retrying push' message, got {len(retrying)}"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_revise_rebasing_only_one_wins(tmp_path, _loop, run):
    """Race regression: two concurrent revise() calls on a rebasing task
    must produce exactly one 'Recovering from rebasing' stage_transition
    message in the audit trail.
    """
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="rebasing race",
                flow_name="build",
                status="blocked",
                current_step="push",
                state="rebasing",
            )
        )

        # Patch the locked dispatch path so the test doesn't try to spawn
        # an agent. _dispatch_pr_fix_locked is the inner method called by
        # both revise()'s rebasing branch and dispatch_pr_fix().
        from unittest.mock import AsyncMock

        svc._dispatch_pr_fix_locked = AsyncMock()

        async def _race():
            return await asyncio.gather(
                svc.revise(task.id, "rebase on main"),
                svc.revise(task.id, "rebase on main"),
                return_exceptions=True,
            )

        results = run(_race())
        for r in results:
            assert r is None or not isinstance(r, BaseException), f"unexpected: {r!r}"

        transition_msgs = run(db.get_messages(task.id, msg_type="stage_transition"))
        rebasing_msgs = [m for m in transition_msgs if m.content.startswith("Recovering from rebasing")]
        assert len(rebasing_msgs) == 1, (
            f"Expected one rebasing stage_transition, got {len(rebasing_msgs)}: {[m.content for m in transition_msgs]}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_revise_rebasing_dispatches_pr_fix_agent(tmp_path, _loop, run):
    """Regression: revise() on a legacy ``rebasing`` task must actually
    dispatch the pr-fix agent, not silently strand the task.

    The rebasing-recovery CAS must land the task in the process's monitor
    state (e.g. ``wait_for_pr_signal``) so ``_dispatch_pr_fix_locked`` →
    ``_dispatch_step`` finds the registered ``(monitor_state, "pr-fixing")``
    edge and runs the agent. A prior bug hardcoded the legacy synthetic
    ``"waiting_for_pr"`` here; under the ADR-014 SM that state has no outgoing
    dispatch edge, so ``_dispatch_step`` silently returned and the task
    stranded at ``status="working"`` with no agent (the runner was never
    invoked, recoverable only via the restart sweep).

    Asserting the runner ran is what distinguishes a real recovery from a
    silent strand — inspecting the DB row alone would not, because the
    pre-dispatch CAS in ``_dispatch_pr_fix_locked`` populates
    ``current_step="pr-fix"`` even when the subsequent dispatch no-ops.
    """

    class _RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def dispatch_shape_prompt(self) -> str:
            # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
            return ""

        async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
            self.calls.append(user_prompt)
            # NEEDS_DECISION halts the pr_fix sub-flow at needs_input so the
            # test doesn't cascade into review / the push_pr action.
            return AgentResult(
                success=True,
                stdout="PR_FIX_NEEDS_DECISION: need more info",
                stderr="",
                return_code=0,
                duration_ms=10,
            )

    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _RecordingRunner()
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="rebase recover",
                flow_name="build",
                status="blocked",
                current_step="push_pr",
                state="rebasing",
            )
        )
        # pr-fix declares ``inputs: [spec, plan]``; seed both so the dispatch
        # reaches the runner instead of deferring on a missing-artifact check.
        run(db.add_message(task.id, "assistant", "spec", "the spec", "artifact", metadata={"artifact_name": "spec"}))
        run(db.add_message(task.id, "assistant", "plan", "the plan", "artifact", metadata={"artifact_name": "plan"}))

        run(svc.revise(task.id, "rebase on main"))

        # Dispatch runs the agent in a background task — wait for it.
        async def _wait_for_dispatch():
            for _ in range(60):
                if svc.runner.calls:
                    return
                await asyncio.sleep(0.05)

        run(_wait_for_dispatch())

        assert svc.runner.calls, (
            "rebasing recovery dispatched no agent — the task was stranded "
            "(rebasing CAS landed in a state with no dispatch edge)"
        )
        updated = run(db.get_task(task.id))
        assert updated.state != "rebasing", f"task should have left rebasing, got state={updated.state!r}"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_transition_task_only_one_writes_audit_message(tmp_path, _loop, run):
    """Race + atomicity regression: transition_task() used to do
    source.save → add_message → _set_status as three separate writes,
    leaving a crash window. The fix folds the (state, status) write into
    a single claim_task_transition CAS; two concurrent callers must only
    produce one 'PR # ... complete' message in the audit trail.
    """
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="transition race",
                flow_name="build",
                status="waiting_for_pr",
                current_step="push",
                # ADR-014 Layer A renamed the monitor's state from the
                # synthetic ``waiting_for_pr`` to the job-named ``wait_for_pr_signal``.
                # The status enum value is unchanged.
                state="wait_for_pr_signal",
                metadata={"pr_number": 99},
            )
        )

        from unittest.mock import AsyncMock

        svc._cleanup_worktree_if_done = AsyncMock()

        async def _race():
            return await asyncio.gather(
                svc.transition_task(task.id, "complete"),
                svc.transition_task(task.id, "complete"),
                return_exceptions=True,
            )

        results = run(_race())
        for r in results:
            assert r is None or not isinstance(r, BaseException), f"unexpected: {r!r}"

        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        complete_msgs = [m for m in msgs if "complete" in m.content]
        assert len(complete_msgs) == 1, (
            f"Expected one PR-complete status_change, got {len(complete_msgs)}: {[m.content for m in msgs]}"
        )

        fresh = run(db.get_task(task.id))
        assert fresh.status == "complete"
        assert fresh.state == "complete"
        assert fresh.current_step is None
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_revise_waiting_for_pr_only_one_records_feedback(tmp_path, _loop, run):
    """Race regression: two concurrent revise() calls on a waiting_for_pr
    task must produce exactly one 'feedback' message — not two.

    Before round 12 the feedback row was written before the
    _dispatching_pr_fix guard, leaving a duplicate row in chat history
    (and a duplicate entry in the next pr-fix cycle's combined context)
    when two double-submits arrived. Now matches the rebasing branch's
    CAS-then-record pattern.
    """
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="waiting_for_pr race",
                flow_name="build",
                status="waiting_for_pr",
                current_step="push",
                state="waiting_for_pr",
                metadata={"pr_number": 1},
            )
        )

        # Patch the locked dispatch with a fake that records the feedback
        # message (mirroring the real method's contract — see
        # _dispatch_pr_fix_locked) but doesn't spawn an agent. Slow down
        # _build_revise_feedback so the second caller actually starts while
        # the first still holds the dispatch lock — without that delay, sync
        # SQLite serialises the two coroutines and the second's check-then-
        # add lands after the first's finally has cleared the set, defeating
        # the guard the test is meant to exercise.
        async def _fake_dispatch(task_id, _feedback, *, user_feedback=None, operator_initiated=False):
            if user_feedback is not None:
                await svc.db.add_message(task_id, "user", "", user_feedback, "feedback")
            return True

        svc._dispatch_pr_fix_locked = _fake_dispatch

        async def _slow_build(_row, feedback):
            await asyncio.sleep(0.05)
            return feedback

        svc._build_revise_feedback = _slow_build

        async def _race():
            return await asyncio.gather(
                svc.revise(task.id, "fix the lint"),
                svc.revise(task.id, "fix the lint"),
                return_exceptions=True,
            )

        from lotsa.orchestrator import ReviseNotAllowed

        results = run(_race())
        # Exactly one caller wins (returns None); the loser raises
        # ReviseNotAllowed so the API can return 400 instead of accepting
        # the message silently and then dropping it.
        nones = [r for r in results if r is None]
        rejects = [r for r in results if isinstance(r, ReviseNotAllowed)]
        assert len(nones) == 1 and len(rejects) == 1, f"unexpected: {results!r}"

        feedback_msgs = run(db.get_messages(task.id, msg_type="feedback"))
        assert len(feedback_msgs) == 1, (
            f"Expected one feedback message, got {len(feedback_msgs)}: {[m.content for m in feedback_msgs]}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_revise_loses_to_transition_task_no_orphan_feedback(tmp_path, _loop, run):
    """Race regression: when PrMonitor's transition_task wins between
    revise()'s status check and the dispatch's add_message, no orphan
    feedback row should land.

    Models the production sequence: user clicks revise, GitHub fetch is
    slow, mid-fetch the PR is merged, PrMonitor calls transition_task
    (CAS-flips status from waiting_for_pr to complete). The dispatch's
    own CAS now fails, so the user-feedback message is never written.
    """
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(
                title="orphan race",
                flow_name="build",
                status="waiting_for_pr",
                current_step="push",
                # ADR-014 Layer A: monitor state renamed; status unchanged.
                state="wait_for_pr_signal",
                metadata={"pr_number": 42},
            )
        )

        from unittest.mock import AsyncMock

        svc._cleanup_worktree_if_done = AsyncMock()

        # Mock the agent dispatch so the locked path doesn't try to spawn.
        # _dispatch_pr_fix_locked still runs its CAS — we want to see that
        # transition_task can race in and the CAS-loser leaves no message.
        svc._dispatch_step = AsyncMock()

        async def _slow_build(_row, feedback):
            await asyncio.sleep(0.05)
            return feedback

        svc._build_revise_feedback = _slow_build

        async def _race():
            # transition_task fires while revise's _build_revise_feedback is
            # awaiting — simulates PrMonitor seeing the merge mid-fetch.
            return await asyncio.gather(
                svc.revise(task.id, "fix the lint"),
                svc.transition_task(task.id, "complete"),
                return_exceptions=True,
            )

        run(_race())

        # If transition_task won (typical): no feedback message recorded.
        # If revise() won (also acceptable): exactly one feedback message.
        # The bug was: feedback recorded AND no dispatch — strictly forbidden.
        feedback_msgs = run(db.get_messages(task.id, msg_type="feedback"))
        fresh = run(db.get_task(task.id))
        if fresh.status == "complete":
            assert len(feedback_msgs) == 0, (
                f"Orphan feedback row recorded after transition_task won: {[m.content for m in feedback_msgs]}"
            )
        else:
            assert len(feedback_msgs) == 1, (
                f"Expected exactly one feedback row when revise won, got {len(feedback_msgs)}"
            )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_list_tasks_status_filter(tmp_path, _loop, run):
    """db.list_tasks accepts an optional status filter — push the predicate
    into SQL instead of scanning all rows in Python (PrMonitor hot path)."""
    db = TaskDB(tmp_path / "lotsa.db")
    run(db.initialize())
    try:
        run(db.create_task(title="A", flow_name="t", status="working"))
        run(db.create_task(title="B", flow_name="t", status="waiting_for_pr"))
        run(db.create_task(title="C", flow_name="t", status="waiting_for_pr"))
        run(db.create_task(title="D", flow_name="t", status="complete"))

        all_rows = run(db.list_tasks())
        assert len(all_rows) == 4

        wfp = run(db.list_tasks(status="waiting_for_pr"))
        assert len(wfp) == 2
        assert {r.title for r in wfp} == {"B", "C"}

        # state + status both honored.
        none = run(db.list_tasks(state="nonexistent", status="waiting_for_pr"))
        assert none == []
    finally:
        run(db.close())


def test_concurrent_block_only_one_message(tmp_path, _loop, run):
    """Race regression: two concurrent block() calls must produce exactly
    one 'Task blocked' message in the audit trail. Idempotent before the
    fix (double-block was a benign no-op state-wise) but accumulated
    duplicate messages — same shape as the round-3 rules-no-match issue.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        task = run(
            db.create_task(title="block race", flow_name="t", status="waiting", current_step="coding", state="coding")
        )

        async def _race():
            return await asyncio.gather(svc.block(task.id), svc.block(task.id), return_exceptions=True)

        results = run(_race())
        for r in results:
            assert r is None, f"unexpected: {r!r}"

        msgs = run(db.get_messages(task.id, msg_type="status_change"))
        blocked_msgs = [m for m in msgs if m.content == "Task blocked"]
        assert len(blocked_msgs) == 1, f"expected one 'Task blocked' message, got {len(blocked_msgs)}"

        fresh = run(db.get_task(task.id))
        assert fresh.status == "blocked"
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_revise_loser_does_not_record_feedback(tmp_path, _loop, run):
    """Race regression: two concurrent revise() calls — only the winner
    records a feedback message AND dispatches. Before the fix, both calls
    wrote 'feedback' rows to the DB but only the winner's feedback string
    reached _dispatch_step, so the loser's text was silently dropped from
    the agent's view despite the user thinking it was registered.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        task = run(
            db.create_task(title="rev race", flow_name="t", status="waiting", current_step="coding", state="coding")
        )

        async def _race():
            return await asyncio.gather(
                svc.revise(task.id, "first revision"),
                svc.revise(task.id, "second revision"),
                return_exceptions=True,
            )

        run(_race())

        # Exactly one feedback message landed in the DB — the winner's.
        # Before the fix, both messages would land but only one would dispatch.
        msgs = run(db.get_messages(task.id, msg_type="feedback"))
        assert len(msgs) == 1, f"expected one feedback message, got {len(msgs)}: {[m.content for m in msgs]}"
        assert msgs[0].content in ("first revision", "second revision")
    finally:
        run(svc.shutdown())
        run(db.close())


def test_approve_atomically_flips_status_to_working(tmp_path, _loop, run):
    """Crash-window regression: after approve()'s CAS, status must already be
    'working' (not 'waiting'). Otherwise a server crash between the CAS and
    the subsequent _dispatch_step status write would leave the task stuck
    at status='waiting' with state advanced to success_state — start()'s
    recovery sweep ignores 'waiting' rows, and a follow-up approve fails
    the CAS because state has moved.

    Seeds a 2-step flow (plan with evaluate=true → code) and a task at the
    plan gate so approve()'s success_state is a non-terminal gate state,
    letting us observe the post-CAS state before terminal-state mirroring
    or a real next-step dispatch overwrites it.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: plan\n    evaluate: true\n  - name: code\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        plan_step = svc.flow.jobs[0]
        task = run(
            db.create_task(
                title="approve atomicity",
                flow_name="t",
                status="waiting",
                current_step=plan_step.name,
                state=plan_step.active_state,
            )
        )

        # Stub _dispatch_next_step so we observe DB state immediately after
        # the CAS, before the next-step dispatch's own status write.
        from unittest.mock import AsyncMock

        svc._dispatch_next_step = AsyncMock()
        svc._cleanup_worktree_if_done = AsyncMock()

        run(svc.approve(task.id))

        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "working", (
            f"approve() must atomically flip status to 'working' so start() can "
            f"recover a crashed approve; got {fresh.status!r}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_concurrent_jump_only_one_wins(tmp_path, _loop, run):
    """Race regression: two concurrent jump_to_step() calls on the same task
    used to both reach _dispatch_next_step (no idempotency check), spawning
    two agents on the same worktree. Fixed by claim_task_transition CAS
    before dispatch — exactly one caller wins.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: spec\n    evaluate: true\n  - name: code\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner(AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=10))
    run(svc.start())
    try:
        spec_step = svc.flow.jobs[0]
        task = run(
            db.create_task(
                title="jump race",
                flow_name="t",
                status="waiting",
                current_step=spec_step.name,
                state=spec_step.success_state,
            )
        )

        async def _race():
            return await asyncio.gather(
                svc.jump_to_step(task.id, "code"),
                svc.jump_to_step(task.id, "code"),
                return_exceptions=True,
            )

        results = run(_race())
        for r in results:
            assert r is None or not isinstance(r, BaseException), f"unexpected: {r!r}"

        # Exactly one stage_transition for the jump — not two — proves a
        # single dispatch path executed.
        msgs = run(db.get_messages(task.id, msg_type="stage_transition"))
        jump_msgs = [m for m in msgs if m.content == "Jumped to code"]
        assert len(jump_msgs) == 1, (
            f"Expected one 'Jumped to code' message, got {len(jump_msgs)}: {[m.content for m in msgs]}"
        )
    finally:
        run(svc.shutdown())
        run(db.close())


def test_jump_to_step_on_complete_task_is_noop(tmp_path, _loop, run):
    """Regression: jump_to_step on a complete/abandoned task must not reopen it.

    Without the terminal-state guard, the CAS happily rewrote a complete row
    to (status='working', state=target_queue_state) — bypassing the FSM
    entirely (claim_task_transition is a raw UPDATE, doesn't consult the
    state machine) and silently reopening a finished task.
    """
    flow_yaml = tmp_path / "f.yaml"
    flow_yaml.write_text("name: t\njobs:\n  - name: spec\n    evaluate: true\n  - name: code\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=tmp_path / "d",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    (tmp_path / "d").mkdir()
    db = TaskDB(tmp_path / "d" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    run(svc.start())
    try:
        for terminal in ("complete", "abandoned"):
            task = run(db.create_task(title=terminal, status=terminal, state=terminal, current_step=None))
            run(svc.jump_to_step(task.id, "spec"))
            row = run(db.get_task(task.id))
            assert row.status == terminal, f"jump_to_step reopened {terminal!r} task to {row.status!r}"
            assert row.state == terminal, f"jump_to_step changed state of {terminal!r} task to {row.state!r}"
    finally:
        run(svc.shutdown())
        run(db.close())
