"""Worktree-less steps (chat) must run against a fresh project root.

The bug: ADR-044 Phase 3 gave ``chat`` (``needs_worktree: false``) no prehooks
at all, so ``_run_step_prehooks`` falls through to ``_fallback_work_dir`` — the
project's own checkout. Nothing in the codebase ever updates that checkout's
working tree: ``WorktreeManager._resolve_default_base_ref`` fetches with
``git -C <project root>`` but only moves remote-tracking refs, and the ADR-018
branch sync explicitly bails when there is no worktree. So every chat task ran
against whatever commit the operator's clone was last left on — frozen forever,
which is why chat reports that files "don't exist".

The fix: a ``sync_root`` prehook, derived for exactly the steps that opt out of
the worktree, that fetches origin and **fast-forwards the project root** — but
only when doing so cannot destroy operator work:

* HEAD must be on the project's default branch, and
* the tree must be clean (tracked files; untracked are fine).

Otherwise it skips, warns, and leaves the checkout alone. ``git merge
--ff-only`` is the merge verb precisely because it cannot clobber by
construction.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_repo_with_origin(tmp_path: Path, default_branch: str = "main") -> tuple[Path, Path]:
    """An ``origin`` repo on *default_branch* plus a clone of it.

    Cloning is what sets the clone's ``refs/remotes/origin/HEAD``, which is the
    ref ``_detect_default_branch`` reads — so the clone's detected default
    branch is *default_branch* without any extra wiring.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", str(origin))
    _git("config", "user.email", "origin@test.com", cwd=origin)
    _git("config", "user.name", "Origin", cwd=origin)
    (origin / "README.md").write_text("# upstream\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-m", "init", cwd=origin)
    _git("branch", "-M", default_branch, cwd=origin)

    local = tmp_path / "local"
    _git("clone", str(origin), str(local))
    _git("config", "user.email", "local@test.com", cwd=local)
    _git("config", "user.name", "Local", cwd=local)
    return origin, local


def _advance_origin(origin: Path, filename: str = "NEW_FILE.md") -> str:
    """Add one commit to *origin*; return the new upstream HEAD sha."""
    (origin / filename).write_text("added upstream\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-m", f"add {filename}", cwd=origin)
    return _git("rev-parse", "HEAD", cwd=origin)


def _ctx(tmp_path: Path, repo: Path, db=None, default_branch: str = "main"):
    """A prehook-shaped TaskContext for *repo* (worktree = the project root)."""
    from lotsa.tools import TaskContext
    from rigg.git import WorktreeManager

    return TaskContext(
        task_id="t1",
        worktree=repo,
        metadata={},
        db=db,
        process_name="chat",
        flow_name="main",
        current_flow="main",
        last_run_step="chat",
        worktree_manager=WorktreeManager(repo, tmp_path / "worktrees", default_branch=default_branch),
    )


# ===========================================================================
# 1. Registration + derivation
# ===========================================================================


def test_sync_root_prehook_is_registered():
    import lotsa.prehooks  # noqa: F401 — registration side effect
    from lotsa.registry import is_prehook_registered

    assert is_prehook_registered("sync_root")


def test_worktree_less_step_derives_sync_root():
    """A ``needs_worktree: false`` agent gets ``sync_root`` in place of ``worktree``."""
    import lotsa.prehooks  # noqa: F401 — the built-in must be registered to validate
    from lotsa.flows import build_process

    process = build_process("chat")
    job = process.flows["main"].jobs[0]
    assert "worktree" not in job.prehooks
    assert "sync_root" in job.prehooks


def test_worktree_step_does_not_derive_sync_root():
    """A worktree step already syncs via ``origin/<default>`` — no root touch."""
    import lotsa.prehooks  # noqa: F401
    from lotsa.flows import build_process

    process = build_process("build")
    for job in process.flows["main"].jobs:
        if "worktree" in job.prehooks:
            assert "sync_root" not in job.prehooks


# ===========================================================================
# 2. The happy path — a clean root on the default branch fast-forwards
# ===========================================================================


@pytest.mark.asyncio
async def test_clean_root_fast_forwards_to_origin(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    upstream_sha = _advance_origin(origin)
    assert not (local / "NEW_FILE.md").exists(), "precondition: the clone is behind"

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert result.success, result.output
    assert _git("rev-parse", "HEAD", cwd=local) == upstream_sha
    assert (local / "NEW_FILE.md").exists(), "the file chat claimed didn't exist must now be on disk"


@pytest.mark.asyncio
async def test_sync_root_never_reports_a_worktree(tmp_path):
    """The orchestrator reads ``metadata['worktree']`` to set the step work_dir.

    ``sync_root`` creates no worktree, so it must not report one — otherwise the
    step's work_dir would point at a path that does not exist.
    """
    from lotsa.prehooks import sync_root_prehook

    _origin, local = _make_repo_with_origin(tmp_path)
    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert "worktree" not in (result.metadata or {})


@pytest.mark.asyncio
async def test_already_current_root_is_a_silent_success(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    _origin, local = _make_repo_with_origin(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=local)

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert result.success
    assert _git("rev-parse", "HEAD", cwd=local) == before


@pytest.mark.asyncio
async def test_non_main_default_branch_is_honoured(tmp_path):
    """The rails compare HEAD against the *project's* default branch, not ``main``."""
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path, default_branch="trunk")
    upstream_sha = _advance_origin(origin)

    result = await sync_root_prehook(_ctx(tmp_path, local, default_branch="trunk"), {})

    assert result.success, result.output
    assert _git("rev-parse", "HEAD", cwd=local) == upstream_sha


# ===========================================================================
# 3. The rails — never destroy operator work
# ===========================================================================


@pytest.mark.asyncio
async def test_dirty_root_is_skipped_not_clobbered(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    _advance_origin(origin)
    (local / "README.md").write_text("# local work in progress\n")
    before = _git("rev-parse", "HEAD", cwd=local)

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert not result.success
    assert result.metadata.get("error_kind") == "dirty_worktree"
    assert _git("rev-parse", "HEAD", cwd=local) == before, "HEAD must not move under a dirty tree"
    assert (local / "README.md").read_text() == "# local work in progress\n", "operator edit destroyed"


@pytest.mark.asyncio
async def test_untracked_files_do_not_block_the_sync(tmp_path):
    """Untracked files survive a fast-forward — they must not veto it."""
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    upstream_sha = _advance_origin(origin)
    (local / "scratch.txt").write_text("notes\n")

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert result.success, result.output
    assert _git("rev-parse", "HEAD", cwd=local) == upstream_sha
    assert (local / "scratch.txt").exists()


@pytest.mark.asyncio
async def test_root_on_another_branch_is_skipped(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    _advance_origin(origin)
    _git("checkout", "-b", "my-feature", cwd=local)
    before = _git("rev-parse", "HEAD", cwd=local)

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert not result.success
    assert result.metadata.get("error_kind") == "not_on_default_branch"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=local) == "my-feature"
    assert _git("rev-parse", "HEAD", cwd=local) == before


@pytest.mark.asyncio
async def test_diverged_root_is_skipped(tmp_path):
    """A root carrying local commits can't fast-forward — never rewrite it."""
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    _advance_origin(origin)
    (local / "local_only.md").write_text("local commit\n")
    _git("add", ".", cwd=local)
    _git("commit", "-m", "local only", cwd=local)
    before = _git("rev-parse", "HEAD", cwd=local)

    result = await sync_root_prehook(_ctx(tmp_path, local), {})

    assert not result.success
    assert _git("rev-parse", "HEAD", cwd=local) == before
    assert (local / "local_only.md").exists()


# ===========================================================================
# 4. Degradation — never raise, never block a task
# ===========================================================================


@pytest.mark.asyncio
async def test_repo_without_origin_degrades_quietly(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    repo = tmp_path / "solo"
    repo.mkdir()
    _git("init", str(repo))
    _git("config", "user.email", "solo@test.com", cwd=repo)
    _git("config", "user.name", "Solo", cwd=repo)
    (repo / "a.txt").write_text("a\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)

    result = await sync_root_prehook(_ctx(tmp_path, repo), {})

    assert result.metadata.get("error_kind") == "no_upstream_ref"


@pytest.mark.asyncio
async def test_non_git_directory_degrades_quietly(tmp_path):
    from lotsa.prehooks import sync_root_prehook

    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    result = await sync_root_prehook(_ctx(tmp_path, plain), {})

    assert not result.success  # and, crucially, no exception


@pytest.mark.asyncio
async def test_missing_worktree_manager_degrades_quietly(tmp_path):
    from lotsa.prehooks import sync_root_prehook
    from lotsa.tools import TaskContext

    ctx = TaskContext(
        task_id="t1",
        worktree=tmp_path,
        metadata={},
        db=None,
        process_name="chat",
        flow_name="main",
        current_flow="main",
        last_run_step="chat",
    )
    result = await sync_root_prehook(ctx, {})

    assert not result.success
    assert result.metadata.get("error_kind") == "no_worktree_manager"


# ===========================================================================
# 5. Operator visibility — a skipped sync that actually mattered is surfaced
# ===========================================================================


@pytest.mark.asyncio
async def test_skipped_sync_while_behind_posts_an_operator_message(tmp_path):
    """Silence would leave chat confidently reading a stale tree. Say so."""
    from lotsa.db import TaskDB
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    _advance_origin(origin)
    (local / "README.md").write_text("# dirty\n")

    db = TaskDB(tmp_path / "lotsa.db")
    await db.initialize()
    try:
        await sync_root_prehook(_ctx(tmp_path, local, db=db), {})
        messages = await db.get_messages("t1")
        assert any("behind" in m.content.lower() for m in messages), (
            "operator must be told the project root could not be synced"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_successful_sync_posts_no_message(tmp_path):
    """A chat REPL re-dispatches per turn — a healthy sync must stay silent."""
    from lotsa.db import TaskDB
    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    _advance_origin(origin)

    db = TaskDB(tmp_path / "lotsa.db")
    await db.initialize()
    try:
        await sync_root_prehook(_ctx(tmp_path, local, db=db), {})
        assert await db.get_messages("t1") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_syncs_on_one_repo_do_not_collide(tmp_path):
    """Two chat tasks on the same project share one checkout — serialize them.

    Without a per-repo lock the two ``git merge`` calls race on ``index.lock``;
    the loser fails and reports a spurious "couldn't be fast-forwarded" notice.
    """
    import asyncio

    from lotsa.prehooks import sync_root_prehook

    origin, local = _make_repo_with_origin(tmp_path)
    upstream_sha = _advance_origin(origin)

    results = await asyncio.gather(
        sync_root_prehook(_ctx(tmp_path, local), {}),
        sync_root_prehook(_ctx(tmp_path, local), {}),
    )

    assert all(r.success for r in results), [r.output for r in results]
    assert _git("rev-parse", "HEAD", cwd=local) == upstream_sha


# ===========================================================================
# 6. End to end — the wiring the bug was actually made of
# ===========================================================================


class TestChatDispatchSyncsProjectRoot:
    """A dispatched chat task must run against a fast-forwarded project root.

    The unit tests above prove the prehook works and that ``chat`` derives it.
    This closes the loop the bug lived in: the prehook has to actually run on a
    real dispatch, and it must not accidentally start materializing a worktree
    for a ``needs_worktree: false`` step.
    """

    @pytest.fixture()
    def kit(self, tmp_path, _loop, run):
        from lotsa.config import LotsaConfig
        from lotsa.db import TaskDB
        from lotsa.orchestrator import OrchestratorService
        from lotsa.tests.conftest import FakeRunner

        origin, local = _make_repo_with_origin(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config = LotsaConfig(data_dir=data_dir, work_dir=local, flow="chat", model="sonnet", budget=5.0)
        db = TaskDB(data_dir / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc, origin, local
        run(svc.shutdown())
        run(db.close())

    def test_chat_dispatch_fast_forwards_the_project_root(self, kit, run):
        import asyncio

        svc, origin, local = kit
        upstream_sha = _advance_origin(origin)
        assert not (local / "NEW_FILE.md").exists(), "precondition: the project root is behind"

        task = run(svc.create_task("what does NEW_FILE.md say?", process_name="chat"))
        run(asyncio.sleep(0.3))

        assert _git("rev-parse", "HEAD", cwd=local) == upstream_sha
        assert (local / "NEW_FILE.md").exists(), "chat must be able to read the file it was asked about"
        wt = svc.config.data_dir / "worktrees" / "default" / task.id
        assert not wt.exists(), "chat (needs_worktree: false) must still materialize no worktree"
