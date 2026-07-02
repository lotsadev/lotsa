"""ADR-040 — restart-resilient orchestration (failing-first / red).

Spec: on startup, a task left ``status='working'`` (or a push/action state) is
treated as **interrupted**, not failed. The recovery sweep records
``interrupted_at`` + ``resume_count`` in ``tasks.metadata`` (no new columns)
and **auto-dispatches a continuation**:

* **Resume the agent** — agent step + a persisted ``session_id`` + a
  resume-capable runner → re-dispatch with ``--resume <session_id>`` (the
  runner receives the session id).
* **Idempotent re-run** — no ``session_id``, a non-resume runner, or a
  deterministic/action step → re-dispatch from the step's start (session id is
  ``None``); step idempotency makes already-done work a no-op.
* **Cap → block** — bounded by ``resume_count`` (default 2, ``lotsa.yaml``
  configurable). Past the cap, fall back to today's ``blocked`` with a
  "couldn't resume after N attempts" message.

A resumed dispatch's layered system prompt carries an interrupted-and-resumed
note (ADR-025 layer); a normal dispatch does not; ``OPERATIONAL_PREAMBLE`` is
unchanged.

On ``shutdown()`` the service stops accepting new dispatches (``_accepting``),
drains in-flight agents up to a bounded grace window, then cancels survivors.

These tests are written before the implementation lands, so against pre-fix
code they fail because:
* the sweep flips working tasks straight to ``blocked`` — so no
  ``interrupted_at`` is written, the runner is never re-dispatched, and the
  cap-block message differs;
* ``_build_system_prompt`` has no ``resume=`` parameter (``TypeError``);
* there is no ``_accepting`` flag and ``shutdown()`` abandons in-flight work
  immediately rather than draining it;
* ``LotsaConfig`` has no ``shutdown_grace_seconds`` field.

Each interrupted-classification/resume assertion is driven from inside
``start()``'s sweep against a genuinely-``working`` seed row (the real
interrupted precondition), never by pre-seeding the post-bug ``blocked`` state.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from lotsa.db import TaskDB
from lotsa.orchestrator import OPERATIONAL_PREAMBLE, OrchestratorService
from lotsa.tests.conftest import FakeRunner, make_server_config, wait_for_status
from rigg.models import AgentResult

# The interrupted-and-resumed note text (ADR-040 R4 / ADR-025 layer). Asserted
# as a substring so the test is robust to the exact constant name the
# implementation chooses.
_RESUME_NOTE_A = "interrupted and resumed"
_RESUME_NOTE_B = "do not redo completed work"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingRunner:
    """Runner that records every ``run()`` call and advertises a configurable
    resume capability (the ADR-040 ``supports_resume`` signal)."""

    def __init__(self, *, supports_resume: bool = True) -> None:
        self.supports_resume = supports_resume
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "work_dir": work_dir, **kwargs})
        return AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            return_code=0,
            duration_ms=1,
            session_id=kwargs.get("session_id") or "fresh-session",
        )


class HangRunner:
    """Blocks on a never-set event so a resumed task stays ``working`` /
    in-flight until the test releases it. Also reports resume support."""

    def __init__(self, *, supports_resume: bool = True) -> None:
        self.supports_resume = supports_resume
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "work_dir": work_dir, **kwargs})
        self.started.set()
        await self.release.wait()
        return AgentResult(success=True, stdout="done", stderr="", return_code=0, duration_ms=1)


class DrainRunner:
    """In-flight agent for drain tests: signals ``started``, blocks on
    ``release``, and records whether it ran to completion (``finished``)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.calls = 0

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.finished = True
        return AgentResult(success=True, stdout="done", stderr="", return_code=0, duration_ms=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _settle(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true or *timeout* elapses (non-raising)."""
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)


@contextlib.contextmanager
def restart_with_seed(run, tmp_path, *, runner, **seed):
    """Pre-seed a task row in a fresh DB, then start a NEW service against it —
    simulating a daemon restart whose ``start()`` sweep sees the row.

    ``seed`` is forwarded to ``db.create_task`` (e.g. ``status='working'``,
    ``state='coding'``, ``current_step='coding'``, ``metadata=...``).
    """
    config = make_server_config(tmp_path)
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    task = run(db.create_task(**seed))
    svc = OrchestratorService(config, db)
    svc.runner = runner
    run(svc.start())
    try:
        yield svc, db, task
    finally:
        # Release any in-flight HangRunner so the drain in shutdown() doesn't
        # block on it, then tear down.
        rel = getattr(runner, "release", None)
        if rel is not None:
            rel.set()
        with contextlib.suppress(Exception):
            run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# R2 — interrupted, not failed
# ---------------------------------------------------------------------------


def test_working_task_on_restart_is_marked_interrupted_not_blocked(tmp_path, run):
    """A ``status='working'`` task is reclassified as *interrupted*: the sweep
    records ``interrupted_at`` + ``resume_count`` in metadata and routes it to
    the resume path instead of unconditionally flipping it to ``blocked``.

    Fails pre-fix: the sweep sets ``blocked`` and writes no ``interrupted_at``.
    """
    runner = HangRunner()
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="interrupted",
        state="coding",
        status="working",
        current_step="coding",
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: bool(runner.started.is_set()))
            row = await db.get_task(task.id)
            assert row.status != "blocked", "interrupted task must not be flipped straight to blocked"
            assert row.metadata.get("interrupted_at") is not None, "interrupted_at must be recorded in metadata"
            assert int(row.metadata.get("resume_count", 0)) >= 1, "resume_count must be incremented"
            assert task.id in svc._in_flight, "the interrupted step must be re-dispatched (in-flight)"

        run(_t())


# ---------------------------------------------------------------------------
# R3 — resume dispatch (happy path)
# ---------------------------------------------------------------------------


def test_interrupted_agent_step_resumes_with_session_id(tmp_path, run):
    """An interrupted agent step with a persisted ``session_id`` and a
    resume-capable runner is re-dispatched with that session id (``--resume``).

    Fails pre-fix: the task is blocked and the runner is never called.
    """
    runner = RecordingRunner(supports_resume=True)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="resume me",
        state="coding",
        status="working",
        current_step="coding",
        # ``session_step`` marks the persisted session as belonging to the step
        # being resumed (``coding``), so the restart-resume reattaches to it.
        metadata={"session_id": "sess-xyz", "session_step": "coding"},
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: len(runner.calls) >= 1)
            assert runner.calls, "resumed agent step was not re-dispatched"
            assert runner.calls[0].get("session_id") == "sess-xyz", (
                "the runner must receive the persisted session id so it adds --resume"
            )

        run(_t())


def test_interrupted_step_does_not_resume_a_prior_steps_session(tmp_path, run):
    """A persisted ``session_id`` that belongs to a *different* (earlier) step
    must NOT be reused when the interrupted step is resumed. ``session_id`` is a
    single global slot overwritten by whichever step last completed; without the
    ``session_step`` guard a step interrupted before producing its own session
    (e.g. ``plan``/``review``/``pr_summary``, none of which set ``resume: true``)
    would ``--resume`` into the previous step's unrelated conversation.

    The interrupted step here is ``coding`` but the persisted session belongs to
    ``planning`` → the runner must be re-dispatched from the step's start with
    ``session_id=None`` (idempotent re-run), not resumed with the stale id.

    Fails pre-fix: ``want_resume`` ignored session ownership, so the runner
    received ``session_id='sess-planning'`` (a resume into the wrong session).
    """
    runner = RecordingRunner(supports_resume=True)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="stale session",
        state="coding",
        status="working",
        current_step="coding",
        metadata={"session_id": "sess-planning", "session_step": "planning"},
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: len(runner.calls) >= 1)
            assert runner.calls, "interrupted step was not re-dispatched"
            assert runner.calls[0].get("session_id") is None, (
                "a stale session owned by a different step must not be resumed — "
                "the step must re-run from its start (session_id=None)"
            )

        run(_t())


# ---------------------------------------------------------------------------
# R3 — idempotent re-run fallback
# ---------------------------------------------------------------------------


def test_interrupted_step_without_session_reruns_from_start(tmp_path, run):
    """No ``session_id`` in metadata → re-dispatch the step from its start with
    ``session_id=None`` (idempotent re-run), not a resume.

    Fails pre-fix: the task is blocked and the runner is never called.
    """
    runner = RecordingRunner(supports_resume=True)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="rerun me",
        state="coding",
        status="working",
        current_step="coding",
        metadata={},
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: len(runner.calls) >= 1)
            assert runner.calls, "interrupted step was not re-dispatched"
            assert runner.calls[0].get("session_id") is None, (
                "without a persisted session_id the step must re-run from start, not resume"
            )

        run(_t())


def test_non_resume_runner_reruns_even_with_session_id(tmp_path, run):
    """A runner reporting ``supports_resume=False`` must re-run from start even
    when a ``session_id`` exists — the orchestrator selects resume-vs-re-run via
    the capability signal, not the runner's type (ADR-040 R3 / AC #9).

    Fails pre-fix: the task is blocked and the runner is never called.
    """
    runner = RecordingRunner(supports_resume=False)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="no resume support",
        state="coding",
        status="working",
        current_step="coding",
        metadata={"session_id": "sess-abc"},
    ) as (svc, db, task):

        async def _t():
            await _settle(lambda: len(runner.calls) >= 1)
            assert runner.calls, "interrupted step was not re-dispatched"
            assert runner.calls[0].get("session_id") is None, (
                "a non-resume-capable runner must re-run from start, ignoring the session id"
            )

        run(_t())


# ---------------------------------------------------------------------------
# R3 — cap → block
# ---------------------------------------------------------------------------


def test_resume_cap_exceeded_blocks_and_does_not_resume(tmp_path, run):
    """A task whose ``resume_count`` already exceeds the cap (default 2) is set
    ``blocked`` with a "couldn't resume after N attempts" message and is NOT
    resumed again.

    Fails pre-fix: the task is still blocked, but with the generic
    "Agent killed by server restart" message (not the resume-specific one),
    so the message assertion bites.
    """
    runner = RecordingRunner(supports_resume=True)
    with restart_with_seed(
        run,
        tmp_path,
        runner=runner,
        title="crash looper",
        state="coding",
        status="working",
        current_step="coding",
        metadata={"session_id": "sess-1", "resume_count": 5},
    ) as (svc, db, task):

        async def _t():
            await wait_for_status(svc, task.id, "blocked")
            # give any (erroneous) dispatch a chance to fire
            await asyncio.sleep(0.1)
            assert runner.calls == [], "a cap-exceeded task must not be resumed"

            msgs = await db.get_messages(task.id)
            joined = " ".join(m.content.lower() for m in msgs)
            assert "resume" in joined and "attempt" in joined, (
                "the cap-exceeded block message must explain the exhausted resume attempts"
            )

        run(_t())


def test_resume_count_resets_when_task_advances_past_interrupted_step(tmp_path, run):
    """``resume_count`` bounds repeated failure at a *single* interruption, not
    the task's whole life. Once the interrupted step completes and the task
    advances to the next step, the cap must reset — otherwise a long-running
    multi-step task interrupted by ``resume_cap`` *separate, unrelated* deploys
    (each making real forward progress) is wrongly forced to ``blocked``.

    Two-step flow (code → review): seed the task interrupted at ``code`` with a
    non-zero ``resume_count``; after the resumed ``code`` step completes and the
    task advances into ``review``, ``resume_count`` / ``interrupted_at`` are gone.

    Fails pre-fix: ``resume_count`` was never cleared, so it survives (and only
    ever accumulates) across the task's life.
    """
    from lotsa.config import LotsaConfig

    flow_yaml = tmp_path / "reset_flow.yaml"
    flow_yaml.write_text(
        "name: reset-test\njobs:\n"
        "  - name: code\n    prompt: coding\n    resume: true\n"
        "    queue_state: backlog\n    active_state: coding\n"
        "  - name: review\n    prompt: review\n"
        "    queue_state: reviewing\n    active_state: reviewing\n"
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for name in ("coding-system", "coding-user", "review-system", "review-user"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
    (tmp_path / "data").mkdir()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        prompts_dir=prompts_dir,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    task = run(
        db.create_task(
            title="progress",
            state="coding",
            status="working",
            current_step="code",
            metadata={"resume_count": 1, "interrupted_at": "2026-07-02T00:00:00+00:00"},
        )
    )
    runner = RecordingRunner(supports_resume=True)
    svc = OrchestratorService(config, db)
    svc.runner = runner
    run(svc.start())
    try:

        async def _t():
            # The resumed ``code`` step completes and the task advances into
            # ``review`` — wait for that forward-progress edge.
            await _settle(lambda: len(runner.calls) >= 2)
            row = await db.get_task(task.id)
            assert row.state in ("reviewing", "complete"), "task should have advanced past the interrupted step"
            assert "resume_count" not in row.metadata, (
                "resume_count must reset once the task advances past the interrupted step"
            )
            assert "interrupted_at" not in row.metadata, "interrupted_at must clear on forward progress too"

        run(_t())
    finally:
        with contextlib.suppress(Exception):
            run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# R4 — resumed-agent prompt note
# ---------------------------------------------------------------------------


def test_resume_prompt_note_present_only_on_resume(tmp_path, run):
    """``_build_system_prompt(..., resume=True)`` carries the interrupted-and-
    resumed note; ``resume=False`` does not; ``OPERATIONAL_PREAMBLE`` is
    unchanged (still leads the prompt and never contains the note).

    Fails pre-fix: ``_build_system_prompt`` has no ``resume`` parameter
    (``TypeError``).
    """
    config = make_server_config(tmp_path)
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    run(svc.start())
    try:
        step = svc.flow.steps[0]

        with_note = svc._build_system_prompt(step, None, runner=svc.runner, resume=True)
        without_note = svc._build_system_prompt(step, None, runner=svc.runner, resume=False)

        assert _RESUME_NOTE_A in with_note and _RESUME_NOTE_B in with_note, (
            "a resumed dispatch's system prompt must carry the interrupted-and-resumed note"
        )
        assert _RESUME_NOTE_A not in without_note, "a normal dispatch must not carry the resume note"

        # OPERATIONAL_PREAMBLE stays the authoritative lead and is itself untouched.
        assert with_note.startswith(OPERATIONAL_PREAMBLE)
        assert _RESUME_NOTE_A not in OPERATIONAL_PREAMBLE, "the resume note must not be baked into OPERATIONAL_PREAMBLE"
    finally:
        run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# R5 — graceful drain on shutdown
# ---------------------------------------------------------------------------


def test_shutdown_stops_accepting_and_drains_inflight_within_grace(tmp_path, run):
    """``shutdown()`` sets ``_accepting=False`` (stops accepting new dispatches)
    and awaits the in-flight agent; when the agent finishes within the grace
    window it runs to completion rather than being abandoned.

    Fails pre-fix: no ``_accepting`` attribute, and in-flight work is cancelled
    immediately (``finished`` never set).
    """
    config = make_server_config(tmp_path)
    config.shutdown_grace_seconds = 5.0  # ample: the agent finishes well within it
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    runner = DrainRunner()
    svc.runner = runner
    run(svc.start())
    try:

        async def _t():
            task = await svc.create_task("drain me")
            await wait_for_status(svc, task.id, "working")
            await runner.started.wait()

            shutdown_task = asyncio.create_task(svc.shutdown())
            await asyncio.sleep(0.05)
            assert svc._accepting is False, "shutdown must stop accepting new dispatches first"

            runner.release.set()  # let the in-flight agent finish inside the grace window
            await shutdown_task
            assert runner.finished is True, "shutdown must drain (await) the in-flight agent, not abandon it"

        run(_t())
    finally:
        run(db.close())


def test_shutdown_applies_completion_that_lands_inside_grace_window(tmp_path, run):
    """An agent that finishes cleanly *inside* the grace window must have its
    completion **applied** (state transition committed) before ``shutdown()``
    cancels the drainer — not merely awaited.

    ``_run_agent`` ends at ``_completions.put(info)``; the state-transition CAS
    and ``_in_flight`` pop happen later in ``_completion_drainer``. If
    ``shutdown()`` cancels the drainer right after awaiting the agent task (no
    intervening drain), a cleanly-finished agent's row is left
    ``status='working'`` with no agent in flight — a "working-orphan" that burns
    a pointless resume cycle on the next start.

    Fails pre-fix: ``shutdown()`` awaits only the raw agent task, then cancels
    the drainer with no ``_completions.join()``; the completion is discarded and
    the row stays ``status='working'`` (this custom flow has no monitors, so
    there is no incidental await to let the drainer run first).
    """
    config = make_server_config(tmp_path)
    config.shutdown_grace_seconds = 5.0  # ample: the agent finishes well within it
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    runner = DrainRunner()
    svc.runner = runner
    run(svc.start())

    # Widen the genuine race so it resolves deterministically: the drainer's
    # completion-processing spends a real, controllable interval mid-apply (a
    # DB-bound step in production; a sleep here). Pre-fix, ``shutdown()`` cancels
    # the drainer while it is still inside this window, so the transition is
    # never committed. Post-fix, ``shutdown()``'s bounded ``_completions.join()``
    # waits for the drainer to finish applying (call ``task_done``) first. This
    # exercises the real drainer path — it does not pre-seed any post-bug state.
    _orig_merge = svc._merge_task_metadata

    async def _slow_merge(item, updates):
        await asyncio.sleep(0.2)
        return await _orig_merge(item, updates)

    svc._merge_task_metadata = _slow_merge

    try:

        async def _t():
            task = await svc.create_task("apply my completion")
            await wait_for_status(svc, task.id, "working")
            await runner.started.wait()

            shutdown_task = asyncio.create_task(svc.shutdown())
            await asyncio.sleep(0.05)
            runner.release.set()  # finish inside the grace window
            await shutdown_task

            assert runner.finished is True, "agent should have finished inside the grace window"
            row = await db.get_task(task.id)
            assert row.status != "working", (
                "a completion that landed inside the grace window must be applied, not dropped — "
                f"the row is still status={row.status!r}, a working-orphan the next start must resume"
            )
            assert task.id not in svc._in_flight, "the applied completion must be popped from _in_flight"

        run(_t())
    finally:
        run(db.close())


def test_dispatch_refused_while_draining(tmp_path, run):
    """While ``_accepting`` is False, a new dispatch is refused — the agent is
    never launched and the task is not tracked in-flight.

    Fails pre-fix: there is no ``_accepting`` guard, so the dispatch proceeds
    and the runner is called.
    """
    config = make_server_config(tmp_path)
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    runner = RecordingRunner()
    svc.runner = runner
    run(svc.start())
    try:

        async def _t():
            svc._accepting = False  # simulate a drain in progress
            task = await svc.create_task("late arrival")
            await asyncio.sleep(0.2)  # give any dispatch a chance to fire
            assert task.id not in svc._in_flight, "no task may enter in-flight while draining"
            assert runner.calls == [], "dispatch must be refused while draining"

        run(_t())
    finally:
        svc._accepting = True
        run(svc.shutdown())
        run(db.close())


def test_shutdown_grace_window_is_honoured_then_cancels_survivor(tmp_path, run):
    """An agent still running past the grace window is cancelled after the
    orchestrator waited (roughly) the configured ``shutdown_grace_seconds``.

    Fails pre-fix: ``shutdown()`` returns near-instantly (no wait) and the
    ``elapsed >= grace`` assertion bites.
    """
    config = make_server_config(tmp_path)
    config.shutdown_grace_seconds = 0.3
    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    runner = DrainRunner()
    svc.runner = runner
    run(svc.start())
    try:

        async def _t():
            task = await svc.create_task("slow survivor")
            await wait_for_status(svc, task.id, "working")
            await runner.started.wait()

            t0 = time.monotonic()
            await svc.shutdown()  # never released → wait the grace window, then cancel
            elapsed = time.monotonic() - t0

            assert elapsed >= 0.25, f"shutdown must wait ~grace before cancelling; waited {elapsed:.3f}s"
            assert runner.finished is False, "a survivor past the grace window must be cancelled, not completed"

        run(_t())
    finally:
        run(db.close())
