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
