"""A failed agent step must not leave uncommitted work behind.

Prod incident, task ``f22e232b`` (2026-08-26): a ``pr-fix`` agent was 60
minutes into implementing a PR review response when the dispatch was killed at
the wall-clock timeout. The ``commit`` posthook only runs after a step
*succeeds*, so 671 lines across three files stayed uncommitted in the worktree
with nothing owning them.

Every later lifecycle event then hit a precondition it silently assumes — a
clean tree:

    Branch sync to main failed: git merge origin/main failed (no conflicts):
    error: Your local changes to the following files would be overwritten by
    merge: lotsa/orchestrator.py

That is not a content conflict, so ADR-015's ``resolve_conflicts`` path cannot
apply (there are no unmerged paths). ``_sync_branch_to_main`` routes it to a
fatal ``RuntimeError`` → ``blocked``, and Retry re-runs the same sync against
the same dirty tree — a permanent loop needing an operator with ssh.

The fix restores the invariant at its source: when an agent step fails, any
work it left is committed to the task branch, so the tree is clean for whatever
runs next and the work survives into the next round.

**The dangerous case is a worktree-less step.** ``chat`` (``needs_worktree:
false``) runs in the *project root* — the operator's own checkout. A blind
``git add -A`` there would commit their working tree. Preservation must fire
only for a step running in the task's own worktree.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from rigg.models import AgentResult


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd is not None else None, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", str(origin))
    _git("config", "user.email", "o@test.com", cwd=origin)
    _git("config", "user.name", "O", cwd=origin)
    (origin / "README.md").write_text("# upstream\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-m", "init", cwd=origin)
    _git("branch", "-M", "main", cwd=origin)

    local = tmp_path / "local"
    _git("clone", str(origin), str(local))
    _git("config", "user.email", "l@test.com", cwd=local)
    _git("config", "user.name", "L", cwd=local)
    return local


class DyingRunner:
    """An agent that edits files and then dies — the incident's shape.

    Used against ``build``'s first step (``plan``), whose agent declares
    ``produces_changes: false`` and so derives NO commit posthook. Preservation
    therefore cannot be confused with the normal posthook firing.
    """

    def __init__(self, writes: dict[str, str] | None = None) -> None:
        self.writes = writes if writes is not None else {"lotsa/edited.py": "# half-finished work\n"}
        self.work_dirs: list[Path] = []

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.work_dirs.append(Path(work_dir))
        for rel, content in self.writes.items():
            target = Path(work_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return AgentResult(
            success=False,
            stdout="",
            stderr="Docker container timed out after 3600s",
            return_code=-1,
            duration_ms=3600105,
        )


def _settle(run, svc, db, task_id: str, timeout: float = 8.0) -> None:
    """Wait until the dispatch has been fully drained (status left ``working``).

    A fixed sleep races the worktree prehook (a real ``git worktree add``) plus
    the completion drainer; poll the DB — the state of record — instead.
    """

    async def _poll() -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            row = await db.get_task(task_id)
            if row is not None and row.status != "working":
                return
            await asyncio.sleep(0.05)

    run(_poll())


def _service(tmp_path: Path, run, repo: Path, runner, flow: str = "build"):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    config = LotsaConfig(data_dir=data_dir, work_dir=repo, flow=flow, model="sonnet", budget=5.0)
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = runner
    run(svc.start())
    return svc, db


# ===========================================================================
# 1. Work is preserved, and the tree is left clean
# ===========================================================================


class TestFailedStepLeavesACleanTree:
    @pytest.fixture
    def kit(self, tmp_path, _loop, run):
        repo = _make_repo(tmp_path)
        runner = DyingRunner()
        svc, db = _service(tmp_path, run, repo, runner)
        yield svc, db, runner, repo
        run(svc.shutdown())
        run(db.close())

    def test_a_killed_agents_work_is_committed(self, kit, run):
        svc, db, runner, _repo = kit

        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)

        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        assert wt.exists(), "precondition: the step ran in a task worktree"
        assert (wt / "lotsa" / "edited.py").exists(), "precondition: the agent wrote before dying"

        status = _git("status", "--porcelain", cwd=wt)
        assert status == "", (
            f"a failed step must leave a clean tree — otherwise the next branch sync "
            f"refuses to merge and the task blocks forever; got:\n{status}"
        )

    def test_the_work_survives_in_a_commit(self, kit, run):
        svc, db, _runner, _repo = kit

        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)

        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        committed = _git("show", "--name-only", "--format=", "HEAD", cwd=wt)
        assert "lotsa/edited.py" in committed, (
            f"the agent's work must be preserved, not discarded; HEAD touched:\n{committed}"
        )

    def test_the_operator_is_told_the_work_was_preserved(self, kit, run):
        svc, db, _runner, _repo = kit

        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)

        messages = run(db.get_messages(task.id))
        assert any("preserv" in m.content.lower() for m in messages), (
            "a recovery commit the operator didn't ask for must be visible in the audit trail"
        )

    def test_the_task_still_blocks_on_the_agent_failure(self, kit, run):
        """Preserving work must not swallow the failure."""
        svc, db, _runner, _repo = kit

        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)

        row = run(db.get_task(task.id))
        assert row.status == "blocked"
        messages = run(db.get_messages(task.id))
        assert any("timed out after 3600s" in m.content for m in messages), (
            "the original agent error must still reach the audit trail"
        )


# ===========================================================================
# 2. The guard — never commit in the operator's own checkout
# ===========================================================================


def test_a_worktree_less_step_never_commits_the_project_root(tmp_path, _loop, run):
    """``chat`` runs in the project root. ``git add -A`` there would commit the
    operator's working tree — strictly worse than the bug being fixed."""
    repo = _make_repo(tmp_path)
    # Operator's own uncommitted work, sitting in their checkout.
    (repo / "my_local_notes.txt").write_text("operator's scratch\n")
    (repo / "README.md").write_text("# operator edited this\n")
    before = _git("rev-parse", "HEAD", cwd=repo)

    runner = DyingRunner(writes={})
    svc, db = _service(tmp_path, run, repo, runner, flow="chat")
    try:
        chat_task = run(svc.create_task("talk to me", process_name="chat"))
        _settle(run, svc, db, chat_task.id)
    finally:
        run(svc.shutdown())
        run(db.close())

    assert _git("rev-parse", "HEAD", cwd=repo) == before, "the project root's HEAD must never move"
    status = _git("status", "--porcelain", cwd=repo)
    assert "my_local_notes.txt" in status, "the operator's untracked file must still be untracked"
    assert "README.md" in status, "the operator's edit must still be uncommitted"


# ===========================================================================
# 3. Degradation
# ===========================================================================


def test_a_clean_tree_produces_no_recovery_commit(tmp_path, _loop, run):
    """A gate step that fails without writing anything must not manufacture a commit."""
    repo = _make_repo(tmp_path)
    runner = DyingRunner(writes={})
    svc, db = _service(tmp_path, run, repo, runner)
    try:
        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)
        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        count = _git("rev-list", "--count", "HEAD", cwd=wt)
        messages = run(db.get_messages(task.id))
    finally:
        run(svc.shutdown())
        run(db.close())

    assert count == "1", f"no new commit should exist on a clean tree; got {count} commits"
    assert not any("preserv" in m.content.lower() for m in messages), "nothing was preserved — say nothing"


def test_secrets_are_not_swept_into_the_recovery_commit(tmp_path, _loop, run):
    """Reuses the commit step's deny-list rather than a bespoke ``git add -A``."""
    repo = _make_repo(tmp_path)
    runner = DyingRunner(writes={"real_change.py": "x = 1\n", ".env": "SECRET=hunter2\n"})
    svc, db = _service(tmp_path, run, repo, runner)
    try:
        task = run(svc.create_task("do the thing", process_name="build"))
        _settle(run, svc, db, task.id)
        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        committed = _git("show", "--name-only", "--format=", "HEAD", cwd=wt)
    finally:
        run(svc.shutdown())
        run(db.close())

    assert "real_change.py" in committed
    assert ".env" not in committed, "the deny-list must still exclude secrets from a recovery commit"
    assert (wt / ".env").exists(), "excluded, not deleted"


# ===========================================================================
# 4. Every path that interrupts a running step, not just the failure drainer
# ===========================================================================
#
# PR #48 review, Medium: the first cut preserved work only in the completion
# drainer's ``not result.success`` branch. ``stop()`` cancels through
# ``_cancel_in_flight``, which never reaches that branch (``_run_agent``
# re-raises ``CancelledError`` without queuing a completion) — so an operator
# clicking Stop mid-edit parked the task at ``blocked`` with a dirty worktree,
# and a later Retry re-entered exactly the "local changes would be overwritten"
# deadlock this PR exists to fix.
#
# The fix goes in ``_cancel_in_flight`` rather than in ``stop()``: it is the one
# place that interrupts a running agent (shared by ``stop``/``archive``/
# ``jump_to_step``/promotion), so no sibling path can drift out of sync — the
# repo's "symmetric behaviour across sibling paths" rule.


class BlockingRunner:
    """Writes files, then blocks until cancelled — a step caught mid-edit."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        target = Path(work_dir) / "lotsa" / "half_written.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# operator hit Stop while this was being written\n")
        self.started.set()
        await asyncio.sleep(3600)  # cancelled by stop()
        raise AssertionError("unreachable")


def test_stop_preserves_the_work_it_interrupts(tmp_path, _loop, run):
    """Stop → blocked must not leave a dirty tree for the next Retry to deadlock on."""
    repo = _make_repo(tmp_path)
    runner = BlockingRunner()
    svc, db = _service(tmp_path, run, repo, runner)
    try:
        task = run(svc.create_task("do the thing", process_name="build"))

        async def _stop_once_running() -> None:
            await asyncio.wait_for(runner.started.wait(), timeout=10)
            await asyncio.sleep(0.1)  # let the write land
            await svc.stop(task.id)

        run(_stop_once_running())
        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        status = _git("status", "--porcelain", cwd=wt)
        committed = _git("show", "--name-only", "--format=", "HEAD", cwd=wt)
        row = run(db.get_task(task.id))
    finally:
        run(svc.shutdown())
        run(db.close())

    assert status == "", f"Stop must leave a clean tree, else Retry deadlocks on the sync; got:\n{status}"
    assert "lotsa/half_written.py" in committed, "the interrupted work must be preserved, not discarded"
    assert row.status == "blocked", "Stop must still park the task"


def test_preservation_lives_in_the_shared_cancel_primitive(tmp_path, _loop, run):
    """Structural guard: every interrupt path inherits preservation.

    ``stop``/``archive``/``jump_to_step``/promotion all cancel through
    ``_cancel_in_flight``. Preserving inside ``stop()`` alone would leave the
    siblings free to drift — the bug class this repo calls out explicitly.
    """
    import inspect

    from lotsa.orchestrator import OrchestratorService

    src = inspect.getsource(OrchestratorService._cancel_in_flight)
    assert "_preserve_failed_step_work" in src, (
        "preservation belongs in the shared cancel primitive, not bolted onto one caller"
    )


# ===========================================================================
# 5. Two ways preservation could make things worse (PR #48 review, round 2)
# ===========================================================================


def _dirty_worktree_service(tmp_path, run):
    """A started service with one task whose worktree has uncommitted changes."""
    repo = _make_repo(tmp_path)
    runner = DyingRunner()
    svc, db = _service(tmp_path, run, repo, runner)
    task = run(svc.create_task("do the thing", process_name="build"))
    _settle(run, svc, db, task.id)
    wt = svc.config.data_dir / "worktrees" / "default" / task.id
    # The first dispatch already preserved; re-dirty it for these cases.
    (wt / "lotsa" / "later_edit.py").write_text("# written after the recovery commit\n")
    return svc, db, task, wt


def test_a_failed_audit_write_never_escapes_preservation(tmp_path, _loop, run):
    """The "never raises" guarantee has to cover the DB write too.

    ``jump_to_step`` CASes the *new* step to ``working`` **before** calling
    ``_cancel_in_flight``. If preservation raises there — a transient SQLite
    lock on the audit INSERT is enough — the jump aborts before dispatching,
    stranding the task at ``status=working`` with no in-flight agent: precisely
    the failure this function's docstring promises it cannot cause.
    """
    from lotsa.orchestrator import InFlightStep, Item

    svc, db, task, wt = _dirty_worktree_service(tmp_path, run)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    fake_item = Item(id=task.id, state="planned", title="t", body="", metadata={})
    flow_step = svc._resolve_flow(fake_item).steps[0]
    info = InFlightStep(item=fake_item, step=flow_step, step_work_dir=wt)

    original = svc.db.add_message
    svc.db.add_message = boom  # type: ignore[method-assign]
    try:
        run(svc._preserve_failed_step_work(fake_item, info))  # must not raise
    finally:
        svc.db.add_message = original  # type: ignore[method-assign]
        run(svc.shutdown())
        run(db.close())

    assert _git("status", "--porcelain", cwd=wt) == "", "the commit itself must still have landed"


def test_cancelling_an_already_finished_step_does_not_claim_it_failed(tmp_path, _loop, run):
    """A completion sits in ``_in_flight`` until the drainer picks it up.

    ``_run_agent`` sets ``info.agent_result`` and enqueues, but the DB status
    stays ``working`` and the ``_in_flight`` entry stays present until the
    drainer — which processes serially and may be busy with another task —
    dequeues it. A Stop landing in that window would otherwise commit a
    *successful* step's work and write an audit row saying the step "did not
    finish". Pre-PR that window was harmless; preservation put real side
    effects in it.
    """
    from lotsa.orchestrator import InFlightStep, Item
    from rigg.models import AgentResult

    svc, db, task, wt = _dirty_worktree_service(tmp_path, run)
    head_before = _git("rev-parse", "HEAD", cwd=wt)
    try:
        fake_item = Item(id=task.id, state="planned", title="t", body="", metadata={})
        flow_step = svc._resolve_flow(fake_item).steps[0]
        info = InFlightStep(item=fake_item, step=flow_step, step_work_dir=wt)
        # The agent already finished successfully — just not drained yet.
        info.agent_result = AgentResult(success=True, stdout="all good", stderr="", return_code=0, duration_ms=10)
        svc._in_flight[task.id] = info

        run(svc._cancel_in_flight(task.id))
        messages = run(db.get_messages(task.id))
    finally:
        run(svc.shutdown())
        run(db.close())

    assert _git("rev-parse", "HEAD", cwd=wt) == head_before, (
        "a step that already produced a result is not interrupted work — the drainer owns it"
    )
    assert not any("did not finish" in m.content for m in messages), (
        "must not tell the operator a successful step failed"
    )
