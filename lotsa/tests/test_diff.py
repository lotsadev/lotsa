"""Tests for lotsa.diff.compute_branch_diff — PR-style worktree diff.

These exercise the backend half of the "PR-style git diff in the Changes
tab" feature: a merge-base diff of the task's branch against the branch it
was created from, capturing committed + staged + unstaged tracked changes
plus untracked/new files, computed strictly read-only (ADR-013).

The module builds real git repositories (no mocking — matching the
``rigg/tests/test_worktree.py`` convention) and runs the real
``git`` binary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lotsa.diff import compute_branch_diff


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside *repo* and assert success."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit; return the new HEAD sha."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit on `main` and no remote configured.

    Mirrors the real CE worktree layout closely enough for the diff: the
    task branch is ``lotsa/<id>`` and the base branch is the local
    ``main`` it diverged from.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], capture_output=True, check=True)
    _git(repo_dir, "config", "user.email", "test@test.com")
    _git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "README.md").write_text("# Test\n")
    _commit_all(repo_dir, "init")
    return repo_dir


def _branch_off(repo: Path, name: str = "lotsa/task-1") -> None:
    """Create and check out a fresh task branch from the current HEAD."""
    _git(repo, "checkout", "-b", name)


# ---------------------------------------------------------------------------
# Committed changes (the GitHub-PR baseline)
# ---------------------------------------------------------------------------


async def test_committed_branch_changes_appear_as_patch(repo: Path) -> None:
    """A commit on the task branch shows up as a real unified-diff hunk."""
    _branch_off(repo)
    (repo / "app.py").write_text("print('hello')\n")
    _commit_all(repo, "add app.py")

    diff = await compute_branch_diff(repo, "main")

    assert "diff --git" in diff
    assert "app.py" in diff
    assert "@@" in diff  # real hunk header, not a --stat summary
    assert "print('hello')" in diff


async def test_modification_to_existing_file_shows_added_and_removed_lines(repo: Path) -> None:
    """Editing a tracked file yields +/- hunk lines, PR-style."""
    _branch_off(repo)
    (repo / "README.md").write_text("# Test\nsecond line\n")
    _commit_all(repo, "edit readme")

    diff = await compute_branch_diff(repo, "main")

    assert "README.md" in diff
    assert "+second line" in diff


# ---------------------------------------------------------------------------
# Staged + unstaged tracked edits (agent mid-edit)
# ---------------------------------------------------------------------------


async def test_staged_and_unstaged_tracked_edits_appear(repo: Path) -> None:
    """Committed, staged, and unstaged tracked changes all appear together."""
    _branch_off(repo)
    # Committed change on the branch.
    (repo / "committed.py").write_text("committed = 1\n")
    _commit_all(repo, "committed change")
    # Staged-but-not-committed change.
    (repo / "staged.py").write_text("staged = 2\n")
    _git(repo, "add", "staged.py")
    # Unstaged edit to a tracked file.
    (repo / "README.md").write_text("# Test\nunstaged edit\n")

    diff = await compute_branch_diff(repo, "main")

    assert "committed.py" in diff
    assert "staged.py" in diff
    assert "unstaged edit" in diff


# ---------------------------------------------------------------------------
# Untracked / newly-created files
# ---------------------------------------------------------------------------


async def test_untracked_file_appears_as_new_file_addition(repo: Path) -> None:
    """An untracked file is surfaced as a new-file addition (read-only)."""
    _branch_off(repo)
    (repo / "brand_new.py").write_text("x = 42\n")

    diff = await compute_branch_diff(repo, "main")

    assert "brand_new.py" in diff
    assert "x = 42" in diff
    # New-file addition is rendered against /dev/null.
    assert "/dev/null" in diff
    # Standard a/ b/ prefixes (not --no-index's 1/ 2/) so the frontend patch
    # parser recognises it as a git diff and shows the real filename.
    assert "diff --git a/brand_new.py b/brand_new.py" in diff
    assert "+++ b/brand_new.py" in diff
    assert "1/brand_new.py" not in diff


async def test_committed_and_untracked_changes_both_present(repo: Path) -> None:
    """Tracked patch and untracked new-file patch are concatenated together."""
    _branch_off(repo)
    (repo / "tracked.py").write_text("tracked = True\n")
    _commit_all(repo, "tracked")
    (repo / "untracked.py").write_text("untracked = True\n")

    diff = await compute_branch_diff(repo, "main")

    assert "tracked.py" in diff
    assert "untracked.py" in diff


async def test_gitignored_untracked_files_are_excluded(repo: Path) -> None:
    """`.gitignore`d files stay hidden (listing uses --exclude-standard)."""
    # Commit the .gitignore on `main` *before* branching so it isn't part of
    # the branch diff — the only thing that could appear is the ignored file.
    (repo / ".gitignore").write_text("secret.env\n")
    _commit_all(repo, "add gitignore")
    _branch_off(repo)
    (repo / "secret.env").write_text("TOKEN=shh\n")

    diff = await compute_branch_diff(repo, "main")

    assert "secret.env" not in diff
    assert "TOKEN=shh" not in diff


# ---------------------------------------------------------------------------
# Merge-base semantics — upstream advancing must not pollute the diff
# ---------------------------------------------------------------------------


async def test_upstream_commits_after_divergence_are_excluded(repo: Path) -> None:
    """origin/main advancing past the branch point must not enter the diff.

    Simulates the PR-equivalent scenario: the task branched at A, then
    origin/main moved on to C. The diff must be computed from
    merge-base(origin/main, HEAD) == A, so C's files never appear.
    """
    # A = current main HEAD.
    a_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Publish A as origin/main without any network (a local remote-tracking ref).
    _git(repo, "update-ref", "refs/remotes/origin/main", a_sha)

    # Task branch diverges at A and adds its own file.
    _branch_off(repo)
    (repo / "branch_work.py").write_text("branch = True\n")
    _commit_all(repo, "branch work")

    # Upstream advances: main gains a commit, and origin/main is moved to it.
    _git(repo, "checkout", "main")
    (repo / "upstream_only.py").write_text("upstream = True\n")
    c_sha = _commit_all(repo, "upstream advance")
    _git(repo, "update-ref", "refs/remotes/origin/main", c_sha)
    # Back onto the task branch (the worktree's checked-out branch).
    _git(repo, "checkout", "lotsa/task-1")

    diff = await compute_branch_diff(repo, "main")

    assert "branch_work.py" in diff
    assert "upstream_only.py" not in diff, "merge-base diff must exclude upstream-only commits"


# ---------------------------------------------------------------------------
# Fallbacks — never error, never empty when changes exist
# ---------------------------------------------------------------------------


async def test_no_base_branch_falls_back_to_working_tree_diff(repo: Path) -> None:
    """When the base branch can't be resolved, fall back without erroring."""
    # Rename main away so neither origin/main nor main resolves.
    _git(repo, "branch", "-m", "main", "scratch")
    (repo / "README.md").write_text("# Test\nlocal edit\n")

    diff = await compute_branch_diff(repo, "main")

    # Must not raise and must still surface the uncommitted edit.
    assert "local edit" in diff


async def test_no_changes_returns_empty_string(repo: Path) -> None:
    """A branch identical to its base produces a clean empty diff."""
    _branch_off(repo)

    diff = await compute_branch_diff(repo, "main")

    assert diff == ""


async def test_missing_work_dir_does_not_raise(tmp_path: Path) -> None:
    """A non-repo / nonexistent path returns empty rather than raising."""
    diff = await compute_branch_diff(tmp_path / "does-not-exist", "main")
    assert diff == ""


# ---------------------------------------------------------------------------
# Read-only guarantee (ADR-013 / AC9)
# ---------------------------------------------------------------------------


async def test_diff_path_performs_zero_git_mutations(repo: Path) -> None:
    """Computing the diff must not commit, stage, or otherwise mutate state."""
    _branch_off(repo)
    (repo / "committed.py").write_text("committed = 1\n")
    _commit_all(repo, "committed change")
    # Leave a staged change and an untracked file in the tree.
    (repo / "staged.py").write_text("staged = 2\n")
    _git(repo, "add", "staged.py")
    (repo / "untracked.py").write_text("untracked = 3\n")

    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    status_before = _git(repo, "status", "--porcelain").stdout
    staged_before = _git(repo, "diff", "--cached", "--name-only").stdout

    await compute_branch_diff(repo, "main")

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before, "no new commit"
    assert _git(repo, "status", "--porcelain").stdout == status_before, "working tree unchanged"
    assert _git(repo, "diff", "--cached", "--name-only").stdout == staged_before, "index unchanged"


# ---------------------------------------------------------------------------
# Binary files must not crash the diff
# ---------------------------------------------------------------------------


async def test_untracked_binary_file_does_not_crash(repo: Path) -> None:
    """A binary untracked file is summarised, not rendered, and never raises."""
    _branch_off(repo)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03\xff\xfe")

    diff = await compute_branch_diff(repo, "main")

    # git emits a "Binary files ... differ" line rather than a textual hunk.
    assert "Binary" in diff


async def test_mnemonic_prefix_config_does_not_leak_into_diff(repo: Path) -> None:
    """``diff.mnemonicPrefix`` in the operator's gitconfig must not change
    the patch headers — the frontend parser (@pierre/diffs) only accepts
    ``a/``/``b/`` and silently drops every file header on ``c/``/``w/``
    (the Changes tab showed no filenames for any operator with the setting).
    """
    _git(repo, "config", "diff.mnemonicPrefix", "true")
    _git(repo, "checkout", "-b", "lotsa/task-prefix")
    (repo / "README.md").write_text("# Test\nchanged\n")

    diff = await compute_branch_diff(repo, "main")

    assert "diff --git a/README.md b/README.md" in diff
    assert "c/README.md" not in diff
    assert "w/README.md" not in diff
