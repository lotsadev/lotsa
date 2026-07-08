"""Tests for the operator Stop and Archive actions (failing-first / red).

Spec: Stop interrupts a running agent and parks the task at ``blocked`` via
``atomic_transition`` (state + current_step preserved so Retry resumes the
same step). Archive is a terminal action — it stops any running agent,
untracks from the PR monitor, removes the worktree + ``lotsa/{task_id}``
branch, then atomically transitions to the new terminal status ``archived``.
The ``tasks`` row and the append-only ``messages`` log are retained forever.

``archived`` is structurally terminal: every action method, the restart
recovery sweep, and the PR-monitor ``transition_task`` callback must treat it
as a no-op/reject — no transition ever moves a task out of ``archived``.

These tests are written before the implementation exists, so:
- ``stop``/``archive`` tests fail with ``AttributeError`` (method missing) or
  ``ImportError`` (new exception types missing).
- The invariant-guard tests are written to *bite* against the pre-fix code:
  with no ``archived`` guard, ``block``/``transition_task``/``jump_to_step``
  and the restart sweep would all move the task out of ``archived``.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from lotsa.tests.conftest import (
    FakeRunner,
    make_server_config,
    wait_for_status,
)
from rigg.models import AgentResult


class HangRunner:
    """Agent runner that blocks on a never-set event so the task stays
    ``working`` / in-flight until the test cancels it (or releases it)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.started.set()
        await self.release.wait()
        return AgentResult(success=True, stdout="done", stderr="", return_code=0, duration_ms=1)


@contextlib.contextmanager
def service_ctx(run, tmp_path: Path, *, runner=None, flow: str = "custom"):
    """Build, start, and tear down an OrchestratorService.

    ``flow="custom"`` uses the single agent step ``coding`` (evaluate gate) so
    ``create_task`` dispatches a normal agent step. Any other value is treated
    as a bundled preset name (e.g. ``"build"``) whose richer state machine the
    invariant tests introspect for ``blocked`` / ``complete`` edges.
    """
    if flow == "custom":
        config = make_server_config(tmp_path)
    else:
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        config = LotsaConfig(
            data_dir=data_dir,
            work_dir=tmp_path,
            flow=flow,
            model="sonnet",
            budget=5.0,
        )
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = runner or FakeRunner()
    run(svc.start())
    try:
        yield svc, db
    finally:
        run(svc.shutdown())
        run(db.close())


def _spy_worktree_remove(svc: OrchestratorService) -> list[str]:
    """Replace ``worktree_manager.remove`` with an async recorder. Returns the
    list that captures each ``task_id`` passed to ``remove``."""
    removed: list[str] = []

    async def fake_remove(task_id: str) -> None:
        removed.append(task_id)

    svc._worktree_managers["default"].remove = fake_remove  # type: ignore[method-assign]
    return removed


def _count_messages(messages, content: str) -> int:
    return sum(1 for m in messages if m.content == content)


# ── Stop ─────────────────────────────────────────────────────────────────


class TestStop:
    def test_stop_cancels_agent_and_blocks_preserving_step(self, tmp_path, run):
        """Stop cancels the in-flight agent, flips working → blocked, and
        preserves ``state`` + ``current_step`` so Retry can resume."""
        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):

            async def _t():
                task = await svc.create_task("Stop me")
                await wait_for_status(svc, task.id, "working")
                assert task.id in svc._in_flight

                before = await db.get_task(task.id)
                assert before.current_step is not None
                prior_step = before.current_step
                prior_state = before.state

                await svc.stop(task.id)

                row = await db.get_task(task.id)
                assert row.status == "blocked"
                assert row.current_step == prior_step  # preserved for Retry
                assert row.state == prior_state  # state untouched
                assert task.id not in svc._in_flight  # in-flight cancelled/untracked

                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Stopped by operator") == 1

            run(_t())

    def test_stop_then_retry_resumes_same_step(self, tmp_path, run):
        """After Stop, the existing Retry resumes from the preserved step."""
        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):

            async def _t():
                task = await svc.create_task("Stop then retry")
                await wait_for_status(svc, task.id, "working")
                stopped_step = (await db.get_task(task.id)).current_step

                await svc.stop(task.id)
                assert (await db.get_task(task.id)).status == "blocked"

                await svc.retry(task.id)
                await wait_for_status(svc, task.id, "working")
                row = await db.get_task(task.id)
                assert row.status == "working"
                assert row.current_step == stopped_step
                assert task.id in svc._in_flight

            run(_t())

    def test_stop_rejects_when_no_running_agent(self, tmp_path, run):
        """Stop is only valid while the agent is working / in-flight; a task
        parked at a gate (``waiting``) must reject."""
        from lotsa.orchestrator import StopNotAllowed

        with service_ctx(run, tmp_path, runner=FakeRunner()) as (svc, db):

            async def _t():
                task = await svc.create_task("No agent")
                await wait_for_status(svc, task.id, "waiting")  # evaluate gate
                assert task.id not in svc._in_flight
                with pytest.raises(StopNotAllowed):
                    await svc.stop(task.id)

            run(_t())

    def test_stop_after_natural_completion_is_race_safe(self, tmp_path, run):
        """A Stop that arrives after the agent finished on its own must not
        corrupt state or write a duplicate audit message."""
        from lotsa.orchestrator import StopNotAllowed

        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):

            async def _t():
                task = await svc.create_task("Race")
                await wait_for_status(svc, task.id, "working")
                # Let the agent finish naturally → drains to the evaluate gate.
                runner.release.set()
                await wait_for_status(svc, task.id, "waiting")

                with pytest.raises(StopNotAllowed):
                    await svc.stop(task.id)

                row = await db.get_task(task.id)
                assert row.status == "waiting"  # natural outcome preserved
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Stopped by operator") == 0

            run(_t())

    def test_stop_cas_loss_branch_does_not_cancel_or_message(self, tmp_path, run):
        """Exercise stop()'s ``result.won == False`` branch directly.

        Unlike the guard-path test above, here the agent is still ``working``
        and in-flight when ``stop()`` reads the row and passes the guard — but
        the agent drains *between* the guard read and the CAS, so the
        ``working → blocked`` CAS loses. The branch must NOT cancel the
        in-flight task and must NOT write a "Stopped by operator" message; the
        natural outcome stands.

        The race window is forced from inside the code under test by wrapping
        ``db.atomic_transition`` to flip ``working → waiting`` (simulating the
        drainer winning) immediately before stop()'s own CAS runs — per the
        regression-test discipline, the failure is exercised mid-execution
        rather than by pre-flipping the row externally. The biting assertion is
        that the in-flight entry survives: pre-fix code lacking the
        ``if not result.won: return`` early-out would cancel and untrack it.
        """
        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):

            async def _t():
                task = await svc.create_task("CAS-loss race")
                await wait_for_status(svc, task.id, "working")
                assert task.id in svc._in_flight

                real_transition = db.atomic_transition
                flipped = {"done": False}

                async def racing_transition(*args, **kwargs):
                    # First time stop() attempts its working→blocked CAS,
                    # simulate the drainer advancing the task working→waiting
                    # so stop()'s subsequent CAS from working loses.
                    if not flipped["done"] and kwargs.get("from_status") == "working":
                        flipped["done"] = True
                        await real_transition(
                            args[0],
                            from_status="working",
                            from_state=kwargs["from_state"],
                            to_status="waiting",
                            to_state=kwargs["from_state"],
                            to_current_step=kwargs.get("to_current_step"),
                            audit_on_win=None,
                        )
                    return await real_transition(*args, **kwargs)

                db.atomic_transition = racing_transition  # type: ignore[method-assign]
                try:
                    await svc.stop(task.id)
                finally:
                    db.atomic_transition = real_transition  # type: ignore[method-assign]

                row = await db.get_task(task.id)
                assert row.status == "waiting"  # natural outcome stands
                assert task.id in svc._in_flight  # NOT cancelled on CAS loss
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Stopped by operator") == 0

            run(_t())


# ── Chat / stop torn-state (regression: VPS task c79aaf7f) ──────────────────


class TestChatTornState:
    """A chat message (send_message) to an error-blocked task must not strand it
    at ``(status=working, state=blocked)`` with no agent, and stop() must be able
    to clear such a row.

    Pre-fix: send_message CASes ``to_state=row.state`` — for a task blocked via
    the error/posthook path that state is the literal ``'blocked'``, from which
    ``_dispatch_step`` can't advance (no ``(blocked, active_state)`` edge). The
    status flip to ``working`` is never rolled back, and stop() then raises
    instead of clearing it. This is exactly how VPS task c79aaf7f wedged.
    """

    def test_send_message_on_error_blocked_task_reanchors_not_torn(self, tmp_path, run):
        """send_message on a ``(blocked, blocked)`` task re-anchors to the step's
        queue_state (a dispatchable entry) and dispatches — never leaving the
        torn ``(working, blocked)``."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                task = await db.create_task(
                    "error-blocked",
                    state="blocked",
                    status="blocked",
                    current_step="resolve_conflicts",
                    metadata={"current_flow": "pr_fix", "pr_number": 1},
                )
                calls: list[dict] = []

                async def rec(item, step, feedback=None, triggering_comment_ids=None):
                    calls.append({"state": item.state, "step": step.name})

                svc._dispatch_step = rec  # type: ignore[method-assign]
                await svc.send_message(task.id, "rebase or merge with remote branch")

                row = await db.get_task(task.id)
                assert (row.status, row.state) == ("working", "resolving_conflicts"), (
                    f"must re-anchor to the dispatchable queue_state, got {(row.status, row.state)} "
                    "— pre-fix this was the torn ('working', 'blocked')"
                )
                assert calls and calls[0]["state"] == "resolving_conflicts"
                assert calls[0]["step"] == "resolve_conflicts"

            run(_t())

    def test_send_message_on_stop_parked_task_keeps_active_state(self, tmp_path, run):
        """A stop()-parked task sits on its ACTIVE state (dispatchable via the
        self-loop) — send_message must dispatch from there unchanged, not re-route."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                task = await db.create_task(
                    "stop-parked",
                    state="resolving_conflicts",
                    status="blocked",
                    current_step="resolve_conflicts",
                    metadata={"current_flow": "pr_fix", "pr_number": 1},
                )
                seen: list[str] = []

                async def rec(item, step, feedback=None, triggering_comment_ids=None):
                    seen.append(item.state)

                svc._dispatch_step = rec  # type: ignore[method-assign]
                await svc.send_message(task.id, "one more thing")

                row = await db.get_task(task.id)
                assert (row.status, row.state) == ("working", "resolving_conflicts")
                assert seen == ["resolving_conflicts"], "dispatchable active state must be preserved"

            run(_t())

    def test_stop_clears_torn_working_blocked_row(self, tmp_path, run):
        """stop() on a ``(working, blocked)`` row with no in-flight agent parks it
        to a consistent ``(blocked, blocked)`` instead of raising StopNotAllowed."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                task = await db.create_task(
                    "torn",
                    state="blocked",
                    status="working",  # torn: working status, no agent
                    current_step="resolve_conflicts",
                    metadata={"current_flow": "pr_fix", "pr_number": 1},
                )
                assert task.id not in svc._in_flight

                await svc.stop(task.id)  # must NOT raise

                row = await db.get_task(task.id)
                assert (row.status, row.state) == ("blocked", "blocked")
                assert row.current_step == "resolve_conflicts"  # preserved for Retry
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Cleared a stranded 'working' status (no agent was running).") == 1

            run(_t())


# ── Archive ────────────────────────────────────────────────────────────────


class TestArchive:
    def test_archive_running_task_stops_cleans_and_terminates(self, tmp_path, run):
        """Archive on a running task: cancel the agent, remove the worktree +
        branch, land on ``archived``, and retain the message log."""
        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):
            removed = _spy_worktree_remove(svc)

            async def _t():
                task = await svc.create_task("Archive me")
                await wait_for_status(svc, task.id, "working")
                assert task.id in svc._in_flight
                msgs_before = len(await db.get_messages(task.id))

                await svc.archive(task.id)

                row = await db.get_task(task.id)
                assert row.status == "archived"
                assert task.id not in svc._in_flight  # agent cancelled
                assert removed == [task.id]  # worktree + branch torn down once

                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Archived by operator") == 1
                # Append-only: nothing the agent/user already logged is deleted.
                assert len(msgs) >= msgs_before + 1

            run(_t())

    def test_archive_is_idempotent(self, tmp_path, run):
        """Archiving an already-archived task is a no-op — no second audit
        message, no second worktree removal."""
        with service_ctx(run, tmp_path) as (svc, db):
            removed = _spy_worktree_remove(svc)

            async def _t():
                task = await db.create_task("Already archived", flow_name="custom", state="coding", status="archived")
                await svc.archive(task.id)

                row = await db.get_task(task.id)
                assert row.status == "archived"
                assert removed == []  # early return before teardown
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Archived by operator") == 0

            run(_t())

    def test_archive_from_terminal_complete(self, tmp_path, run):
        """Archive is available from any state, including a completed task —
        it removes the (now-stale) worktree and moves to ``archived``."""
        with service_ctx(run, tmp_path) as (svc, db):
            removed = _spy_worktree_remove(svc)

            async def _t():
                task = await db.create_task("Done task", flow_name="custom", state="complete", status="complete")
                await svc.archive(task.id)

                row = await db.get_task(task.id)
                assert row.status == "archived"
                assert removed == [task.id]
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Archived by operator") == 1

            run(_t())

    def test_archive_untracks_from_pr_monitor(self, tmp_path, run):
        """Archiving a ``waiting_for_pr`` task untracks it from the PR monitor
        so the poller stops polling it."""
        with service_ctx(run, tmp_path) as (svc, db):
            _spy_worktree_remove(svc)

            class FakeEngine:
                def __init__(self) -> None:
                    self.untracked: list[str] = []

                def untrack(self, task_id: str) -> None:
                    self.untracked.append(task_id)

            async def _t():
                task = await db.create_task(
                    "PR task",
                    flow_name="custom",
                    state="waiting_for_pr",
                    status="waiting_for_pr",
                    metadata={"pr_number": 7},
                )
                row = await db.get_task(task.id)
                proc_name = svc._process_name_for(row)
                fake = FakeEngine()
                svc._pr_monitors_by_process[proc_name] = fake
                svc._monitor_states_by_process[proc_name] = "waiting_for_pr"

                await svc.archive(task.id)

                assert (await db.get_task(task.id)).status == "archived"
                assert fake.untracked == [task.id]

            run(_t())

    def test_archive_after_completion_reaches_archived(self, tmp_path, run):
        """Archive must reach ``archived`` even when the task drained to a
        non-running state between the operator's click and the CAS — the
        transition is re-read fresh and CAS'd race-safely."""
        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):
            _spy_worktree_remove(svc)

            async def _t():
                task = await svc.create_task("Drain then archive")
                await wait_for_status(svc, task.id, "working")
                runner.release.set()
                await wait_for_status(svc, task.id, "waiting")  # drained off-flight

                await svc.archive(task.id)

                row = await db.get_task(task.id)
                assert row.status == "archived"
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Archived by operator") == 1

            run(_t())

    def test_archive_missing_task_raises(self, tmp_path, run):
        from lotsa.orchestrator import ArchiveNotAllowed

        with service_ctx(run, tmp_path) as (svc, db):

            async def _t():
                with pytest.raises(ArchiveNotAllowed):
                    await svc.archive("does-not-exist")

            run(_t())

    def test_archive_raises_when_cas_never_converges(self, tmp_path, run):
        """If every attempt of the terminal CAS loses, archive() must raise
        ``ArchiveFailed`` rather than return silently — a silent return would
        let the route respond HTTP 200 with a non-archived task. The
        non-convergence is forced by making every ``working → archived`` CAS
        lose (wrapping ``atomic_transition`` to never win for this task)."""
        from lotsa.orchestrator import ArchiveFailed

        runner = HangRunner()
        with service_ctx(run, tmp_path, runner=runner) as (svc, db):
            _spy_worktree_remove(svc)

            async def _t():
                task = await svc.create_task("Never converges")
                await wait_for_status(svc, task.id, "working")

                real_transition = db.atomic_transition

                async def always_lose(*args, **kwargs):
                    # Force the archive CAS to lose every iteration: report the
                    # row as still its pre-CAS status so to_status="archived"
                    # never lands, simulating a perpetually-racing writer.
                    if kwargs.get("to_status") == "archived":
                        return await real_transition(
                            args[0],
                            from_status="__never__",
                            from_state=kwargs["from_state"],
                            to_state=kwargs["to_state"],
                            to_status="archived",
                            to_current_step=kwargs.get("to_current_step"),
                            audit_on_win=None,
                        )
                    return await real_transition(*args, **kwargs)

                db.atomic_transition = always_lose  # type: ignore[method-assign]
                try:
                    with pytest.raises(ArchiveFailed):
                        await svc.archive(task.id)
                finally:
                    db.atomic_transition = real_transition  # type: ignore[method-assign]

                # Task is NOT archived and no "Archived by operator" message
                # was written on a non-converging run.
                row = await db.get_task(task.id)
                assert row.status != "archived"
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Archived by operator") == 0

            run(_t())


# ── ``archived`` is terminal: no path moves a task out of it ────────────────


class TestArchivedInvariant:
    def test_restart_recovery_leaves_archived_untouched(self, tmp_path, run):
        """The restart sweep must not flip ``archived`` to ``blocked`` even
        when the preserved ``state`` is a (legacy) push state. Built inline so
        the archived row exists *before* ``start()`` runs the sweep."""
        config = make_server_config(tmp_path)
        db = TaskDB(config.data_dir / "lotsa.db")
        run(db.initialize())

        async def _stage():
            # ``pushing`` is a legacy push state the sweep would otherwise
            # route to blocked; status != 'blocked' so it isn't skipped pre-fix.
            return await db.create_task("Archived push", flow_name="custom", state="pushing", status="archived")

        task = run(_stage())

        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        try:
            row = run(db.get_task(task.id))
            assert row.status == "archived", (
                f"restart sweep moved an archived task to {row.status!r}; archived must be skipped like blocked"
            )
        finally:
            run(svc.shutdown())
            run(db.close())

    def test_transition_task_noop_on_archived(self, tmp_path, run):
        """A stale PR-monitor ``transition_task`` callback must not un-archive
        a task whose preserved ``state`` still has an outgoing edge."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                transitions = svc.flow.state_machine.transitions
                complete_srcs = [src for (src, dst) in transitions if dst == "complete" and src != "complete"]
                assert complete_srcs, "test setup: build flow should have an edge into 'complete'"
                state = complete_srcs[0]

                task = await db.create_task("Archived w/ edge", flow_name="build", state=state, status="archived")
                await svc.transition_task(task.id, "complete")

                row = await db.get_task(task.id)
                assert row.status == "archived"
                assert row.state == state

            run(_t())

    def test_block_noop_on_archived(self, tmp_path, run):
        """``block()`` on an archived task is a no-op (no transition, no
        ``Task blocked`` message)."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                transitions = svc.flow.state_machine.transitions
                blocked_srcs = [src for (src, dst) in transitions if dst == "blocked" and src != "blocked"]
                assert blocked_srcs, "test setup: build flow should have an edge into 'blocked'"
                state = blocked_srcs[0]

                task = await db.create_task("Archived blockable", flow_name="build", state=state, status="archived")
                await svc.block(task.id)

                row = await db.get_task(task.id)
                assert row.status == "archived"
                msgs = await db.get_messages(task.id)
                assert _count_messages(msgs, "Task blocked") == 0

            run(_t())

    def test_jump_to_step_noop_on_archived(self, tmp_path, run):
        """``jump_to_step()`` must treat ``archived`` as terminal (like
        complete/abandoned) and return early without reopening the task."""
        with service_ctx(run, tmp_path, flow="build") as (svc, db):

            async def _t():
                jobs = svc.flow.jobs
                assert len(jobs) >= 2, "test setup: build flow should have multiple jobs"
                start_state = jobs[0].queue_state
                target_name = jobs[1].name

                task = await db.create_task("Archived jump", flow_name="build", state=start_state, status="archived")
                await svc.jump_to_step(task.id, target_name)

                row = await db.get_task(task.id)
                assert row.status == "archived"

            run(_t())

    def test_retry_rejects_archived(self, tmp_path, run):
        """``retry()`` rejects an archived task (its ``status == 'blocked'``
        guard already excludes archived; this locks the invariant)."""
        from lotsa.orchestrator import RetryNotAllowed

        with service_ctx(run, tmp_path) as (svc, db):

            async def _t():
                task = await db.create_task("Archived retry", flow_name="custom", state="coding", status="archived")
                with pytest.raises(RetryNotAllowed):
                    await svc.retry(task.id)
                assert (await db.get_task(task.id)).status == "archived"

            run(_t())


# ── API endpoints ───────────────────────────────────────────────────────────


class TestStopArchiveAPI:
    def test_stop_endpoint_blocks_running_task(self, app_with_service, run):
        app, service = app_with_service
        service.runner = HangRunner()

        async def _test():
            from httpx import ASGITransport, AsyncClient

            task = await service.create_task("Stop via API")
            await wait_for_status(service, task.id, "working")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/stop")
                assert resp.status_code == 200
                assert resp.json()["task"]["status"] == "blocked"

        run(_test())

    def test_stop_endpoint_rejects_with_400(self, app_with_service, run):
        app, service = app_with_service  # conftest FakeRunner completes immediately

        async def _test():
            from httpx import ASGITransport, AsyncClient

            from lotsa.tests.conftest import wait_for_completion

            task = await service.create_task("Idle task")
            await wait_for_completion(service, task.id)  # parked at evaluate gate
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/stop")
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "STOP_NOT_ALLOWED"

        run(_test())

    def test_archive_endpoint_terminates_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            from httpx import ASGITransport, AsyncClient

            from lotsa.tests.conftest import wait_for_completion

            task = await service.create_task("Archive via API")
            await wait_for_completion(service, task.id)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/archive")
                assert resp.status_code == 200
                assert resp.json()["task"]["status"] == "archived"

        run(_test())

    def test_archive_endpoint_returns_503_when_archive_fails(self, app_with_service, run):
        """When ``archive()`` raises ``ArchiveFailed`` (CAS never converged),
        the route must surface a 503 with code ``ARCHIVE_FAILED`` rather than a
        false 200 — the contract is that a 200 means the task is archived."""
        app, service = app_with_service

        async def _test():
            from httpx import ASGITransport, AsyncClient

            from lotsa.orchestrator import ArchiveFailed
            from lotsa.tests.conftest import wait_for_completion

            task = await service.create_task("Archive fails")
            await wait_for_completion(service, task.id)

            async def boom(task_id: str) -> None:
                raise ArchiveFailed("did not converge")

            service.archive = boom  # type: ignore[method-assign]
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/archive")
                assert resp.status_code == 503
                assert resp.json()["detail"]["code"] == "ARCHIVE_FAILED"

        run(_test())
