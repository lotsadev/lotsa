"""Tests for WorktreeManager — per-task git worktree management."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from rigg.git import WorktreeManager


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside *repo* and assert success."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Create a bare-minimum git repo with one commit on `main`."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # Force `main` as the initial branch so tests don't depend on the
    # caller's init.defaultBranch setting.
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        capture_output=True,
        check=True,
    )
    _git(repo_dir, "config", "user.email", "test@test.com")
    _git(repo_dir, "config", "user.name", "Test")
    # Need at least one commit for worktrees to work
    (repo_dir / "README.md").write_text("# Test")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "init")
    return repo_dir


@pytest.fixture()
def manager(repo: Path, tmp_path: Path) -> WorktreeManager:
    worktrees_dir = tmp_path / "worktrees"
    return WorktreeManager(repo, worktrees_dir)


class TestWorktreeManager:
    async def test_create_worktree(self, manager: WorktreeManager) -> None:
        path = await manager.create("task-1")
        assert path.exists()
        assert (path / "README.md").exists()
        # Branch should exist
        result = subprocess.run(
            ["git", "-C", str(manager._repo), "branch", "--list", "lotsa/task-1"],
            capture_output=True,
            text=True,
        )
        assert "lotsa/task-1" in result.stdout

    async def test_create_idempotent(self, manager: WorktreeManager) -> None:
        path1 = await manager.create("task-2")
        path2 = await manager.create("task-2")
        assert path1 == path2

    async def test_remove_worktree(self, manager: WorktreeManager) -> None:
        await manager.create("task-3")
        assert manager.exists("task-3")
        await manager.remove("task-3")
        assert not manager.exists("task-3")
        # Branch should be deleted too
        result = subprocess.run(
            ["git", "-C", str(manager._repo), "branch", "--list", "lotsa/task-3"],
            capture_output=True,
            text=True,
        )
        assert "lotsa/task-3" not in result.stdout

    async def test_remove_nonexistent(self, manager: WorktreeManager) -> None:
        # Should not raise
        await manager.remove("nonexistent")

    async def test_exists(self, manager: WorktreeManager) -> None:
        assert not manager.exists("task-4")
        await manager.create("task-4")
        assert manager.exists("task-4")
        await manager.remove("task-4")
        assert not manager.exists("task-4")

    async def test_get_path(self, manager: WorktreeManager) -> None:
        assert manager.get_path("task-5") is None
        path = await manager.create("task-5")
        assert manager.get_path("task-5") == path

    async def test_multiple_worktrees(self, manager: WorktreeManager) -> None:
        path_a = await manager.create("task-a")
        path_b = await manager.create("task-b")
        assert path_a != path_b
        assert path_a.exists()
        assert path_b.exists()
        # Both have repo content
        assert (path_a / "README.md").exists()
        assert (path_b / "README.md").exists()


class TestDefaultBranchAccessor:
    """The public `default_branch` accessor lets CE resolve the base ref the
    worktree was created from without reaching into private internals."""

    def test_defaults_to_main(self, manager: WorktreeManager) -> None:
        assert manager.default_branch == "main"

    def test_reflects_constructor_param(self, repo: Path, tmp_path: Path) -> None:
        mgr = WorktreeManager(repo, tmp_path / "worktrees", default_branch="trunk")
        assert mgr.default_branch == "trunk"


class TestValidateId:
    @pytest.mark.parametrize("bad_id", ["../escape", "foo/bar", "back\\slash", "..", ""])
    async def test_rejects_invalid_task_id(self, manager: WorktreeManager, bad_id: str) -> None:
        with pytest.raises(ValueError, match="Invalid task_id"):
            await manager.create(bad_id)

    @pytest.mark.parametrize("bad_id", ["../escape", "foo/bar", "back\\slash", "..", ""])
    async def test_rejects_invalid_id_on_remove(self, manager: WorktreeManager, bad_id: str) -> None:
        with pytest.raises(ValueError, match="Invalid task_id"):
            await manager.remove(bad_id)

    @pytest.mark.parametrize("bad_id", ["../escape", "foo/bar", "back\\slash", "..", ""])
    def test_rejects_invalid_id_on_exists(self, manager: WorktreeManager, bad_id: str) -> None:
        with pytest.raises(ValueError, match="Invalid task_id"):
            manager.exists(bad_id)

    @pytest.mark.parametrize("bad_id", ["../escape", "foo/bar", "back\\slash", "..", ""])
    def test_rejects_invalid_id_on_get_path(self, manager: WorktreeManager, bad_id: str) -> None:
        with pytest.raises(ValueError, match="Invalid task_id"):
            manager.get_path(bad_id)


@pytest.fixture()
def origin_repo(tmp_path: Path) -> Path:
    """A bare repo to serve as `origin` for the local repo under test."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        capture_output=True,
        check=True,
    )
    return bare


@pytest.fixture()
def repo_with_origin(repo: Path, origin_repo: Path) -> Path:
    """A local repo whose `origin` points at `origin_repo` with `main` published."""
    _git(repo, "remote", "add", "origin", str(origin_repo))
    _git(repo, "push", "origin", "main")
    # Configure upstream tracking so `git fetch origin main` updates
    # refs/remotes/origin/main as expected.
    _git(repo, "branch", "--set-upstream-to=origin/main", "main")
    return repo


def _advance_origin(origin_repo: Path, tmp_path: Path, message: str) -> str:
    """Make a new commit on `origin_repo`'s `main` via a scratch clone.

    Returns the new commit SHA — tests assert the worktree picks it up,
    which only happens if the fetch in WorktreeManager.create actually ran.
    """
    scratch = tmp_path / "origin-scratch"
    subprocess.run(
        ["git", "clone", str(origin_repo), str(scratch)],
        capture_output=True,
        check=True,
    )
    _git(scratch, "config", "user.email", "upstream@test.com")
    _git(scratch, "config", "user.name", "Upstream")
    (scratch / "NEW.md").write_text(message)
    _git(scratch, "add", "NEW.md")
    _git(scratch, "commit", "-m", message)
    sha = _git(scratch, "rev-parse", "HEAD").stdout.strip()
    _git(scratch, "push", "origin", "main")
    return sha


class TestCreateBaseRefResolution:
    """When base_ref=None, the manager fetches and bases off origin/<default_branch>."""

    async def test_defaults_to_origin_main_after_fetching(
        self,
        repo_with_origin: Path,
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """A new task should branch from the *current* origin/main, not stale local main."""
        new_sha = _advance_origin(origin_repo, tmp_path, "upstream-advance")
        # Local main is intentionally NOT pulled — this is exactly the
        # stale-local-main scenario the fix targets.

        worktrees_dir = tmp_path / "worktrees"
        mgr = WorktreeManager(repo_with_origin, worktrees_dir)
        wt_path = await mgr.create("task-fresh")

        head = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == new_sha, "worktree should be at origin/main's HEAD, not local main"
        # The upstream commit's file should be present in the worktree.
        assert (wt_path / "NEW.md").exists()

    async def test_explicit_base_ref_bypasses_fetch_and_resolution(
        self,
        repo_with_origin: Path,
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Passing base_ref explicitly skips the upstream sync."""
        _advance_origin(origin_repo, tmp_path, "upstream-advance")
        local_head = _git(repo_with_origin, "rev-parse", "HEAD").stdout.strip()

        worktrees_dir = tmp_path / "worktrees"
        mgr = WorktreeManager(repo_with_origin, worktrees_dir)
        wt_path = await mgr.create("task-explicit", base_ref="HEAD")

        head = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == local_head, "explicit base_ref should be honoured verbatim"

    async def test_falls_back_to_head_when_no_origin_configured(
        self,
        manager: WorktreeManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A repo with no `origin` remote still works — falls back to local HEAD."""
        with caplog.at_level(logging.WARNING):
            wt_path = await manager.create("task-no-origin")
        assert wt_path.exists()
        # Two warnings expected: fetch failure, then ref-not-found fallback.
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Could not fetch main from origin" in w for w in warnings)
        assert any("falling back to HEAD" in w for w in warnings)

    async def test_respects_default_branch_constructor_param(
        self,
        repo: Path,
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Repos that use a non-main default branch get the same fetch behaviour."""
        # Rename local default to `trunk` and publish it as the origin's default.
        _git(repo, "branch", "-m", "main", "trunk")
        _git(repo, "remote", "add", "origin", str(origin_repo))
        # Origin was initialised with `main`; reset its HEAD symbolic ref to trunk
        # after we push the trunk branch in.
        _git(repo, "push", "origin", "trunk")
        subprocess.run(
            ["git", "-C", str(origin_repo), "symbolic-ref", "HEAD", "refs/heads/trunk"],
            capture_output=True,
            check=True,
        )

        # Advance origin's trunk by one commit (via a fresh scratch clone).
        scratch = tmp_path / "trunk-scratch"
        subprocess.run(["git", "clone", str(origin_repo), str(scratch)], capture_output=True, check=True)
        _git(scratch, "config", "user.email", "upstream@test.com")
        _git(scratch, "config", "user.name", "Upstream")
        (scratch / "TRUNK_NEW.md").write_text("trunk-advance")
        _git(scratch, "add", "TRUNK_NEW.md")
        _git(scratch, "commit", "-m", "trunk-advance")
        new_sha = _git(scratch, "rev-parse", "HEAD").stdout.strip()
        _git(scratch, "push", "origin", "trunk")

        worktrees_dir = tmp_path / "worktrees"
        mgr = WorktreeManager(repo, worktrees_dir, default_branch="trunk")
        wt_path = await mgr.create("task-trunk")

        head = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == new_sha
        assert (wt_path / "TRUNK_NEW.md").exists()


async def test_remove_cleans_up_agent_home_sibling(manager, tmp_path):
    """remove() also deletes the Docker runner's per-task agent HOME sibling
    (.agent-home-<task>) so it doesn't accumulate after teardown."""
    task_id = "abc12345"
    await manager.create(task_id)
    agent_home = (tmp_path / "worktrees") / f".agent-home-{task_id}"
    (agent_home / ".claude").mkdir(parents=True)
    (agent_home / "marker").write_text("x")

    await manager.remove(task_id)

    assert not agent_home.exists(), "agent-home sibling should be removed with the worktree"
