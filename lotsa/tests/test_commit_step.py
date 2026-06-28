"""Tests for ``lotsa.commit_step`` — the deterministic commit core (ADR-024).

These exercise the reusable git-commit logic that the ``commit`` posthook
wraps. The module does not exist yet; every test here is expected to fail
(ImportError / assertion) until ``lotsa/commit_step.py`` lands.

The module mirrors ``lotsa.push_step`` discipline: no agent invocation, all
git via ``asyncio.create_subprocess_exec`` (never ``subprocess.run`` /
``shell=True``). The tests below run against a *real* temporary git repo so
the behaviour — staging, no-op-on-clean, deny-list exclusion, deterministic
message, SHA return — is verified end to end rather than mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Real-git helpers (test scaffolding — subprocess.run is fine in test code)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A temp git repo with one initial commit and a configured identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Lotsa Test"], repo)
    (repo / "README.md").write_text("initial\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo)


def _porcelain(repo: Path) -> str:
    return _git(["status", "--porcelain"], repo)


def _last_subject(repo: Path) -> str:
    return _git(["log", "-1", "--format=%s"], repo)


def _files_in_head(repo: Path) -> set[str]:
    out = _git(["show", "--name-only", "--format=", "HEAD"], repo)
    return {line.strip() for line in out.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# Clean worktree → no-op success
# ---------------------------------------------------------------------------


async def test_clean_worktree_is_noop(git_repo: Path):
    """A clean worktree commits nothing and reports a no-op success."""
    from lotsa.commit_step import execute_commit

    before = _head_sha(git_repo)
    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="Some task",
        step_name="code",
    )

    assert result.committed is False
    assert result.sha is None
    # HEAD must not have moved — nothing was committed.
    assert _head_sha(git_repo) == before


# ---------------------------------------------------------------------------
# Single new file → one commit
# ---------------------------------------------------------------------------


async def test_single_new_file_is_committed(git_repo: Path):
    from lotsa.commit_step import execute_commit

    (git_repo / "feature.py").write_text("print('hi')\n")

    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="Add feature",
        step_name="code",
    )

    assert result.committed is True
    assert result.sha == _head_sha(git_repo)
    assert "feature.py" in _files_in_head(git_repo)
    # Worktree is clean afterwards.
    assert _porcelain(git_repo) == ""


# ---------------------------------------------------------------------------
# Mixed staged + unstaged → all captured in one commit
# ---------------------------------------------------------------------------


async def test_mixed_staged_and_unstaged_all_committed(git_repo: Path):
    """``git add -A`` captures both a pre-staged file and an unstaged one."""
    from lotsa.commit_step import execute_commit

    (git_repo / "staged.py").write_text("a\n")
    _git(["add", "staged.py"], git_repo)
    (git_repo / "unstaged.py").write_text("b\n")  # left unstaged

    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="Mixed",
        step_name="code",
    )

    assert result.committed is True
    files = _files_in_head(git_repo)
    assert "staged.py" in files
    assert "unstaged.py" in files
    assert _porcelain(git_repo) == ""


# ---------------------------------------------------------------------------
# Deny-list → secrets excluded from the commit, left on disk
# ---------------------------------------------------------------------------


async def test_denied_path_excluded_from_commit(git_repo: Path):
    """A ``.env`` file is excluded from the commit (but not deleted)."""
    from lotsa.commit_step import execute_commit

    (git_repo / ".env").write_text("SECRET=abc\n")
    (git_repo / "app.py").write_text("x\n")

    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="With secret",
        step_name="code",
    )

    assert result.committed is True
    files = _files_in_head(git_repo)
    assert "app.py" in files
    assert ".env" not in files, "deny-listed .env must never enter a commit"
    # The file is still present on disk — exclusion is non-destructive.
    assert (git_repo / ".env").exists()


# ---------------------------------------------------------------------------
# Deterministic commit message
# ---------------------------------------------------------------------------


async def test_deterministic_message_format(git_repo: Path):
    """Subject is ``<prefix>: <task title> (<step name>)``."""
    from lotsa.commit_step import execute_commit

    (git_repo / "f.py").write_text("x\n")
    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="My Task",
        step_name="code",
        commit_prefix="feat",
    )

    assert result.committed is True
    assert _last_subject(git_repo) == "feat: My Task (code)"
    assert result.message == "feat: My Task (code)"


async def test_commit_prefix_defaults_to_chore(git_repo: Path):
    from lotsa.commit_step import execute_commit

    (git_repo / "f.py").write_text("x\n")
    await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="Default prefix",
        step_name="test",
    )

    assert _last_subject(git_repo) == "chore: Default prefix (test)"


async def test_empty_title_falls_back_to_task_id(git_repo: Path):
    """An empty task title must not produce a degenerate ``: ()`` subject."""
    from lotsa.commit_step import execute_commit

    (git_repo / "f.py").write_text("x\n")
    await execute_commit(
        work_dir=git_repo,
        task_id="abc123",
        task_title="",
        step_name="code",
    )

    assert "abc123" in _last_subject(git_repo)


# ---------------------------------------------------------------------------
# SHA returned
# ---------------------------------------------------------------------------


async def test_returns_new_head_sha(git_repo: Path):
    from lotsa.commit_step import execute_commit

    before = _head_sha(git_repo)
    (git_repo / "f.py").write_text("x\n")
    result = await execute_commit(
        work_dir=git_repo,
        task_id="task-1",
        task_title="New commit",
        step_name="code",
    )

    assert result.sha is not None
    assert result.sha != before
    assert result.sha == _head_sha(git_repo)


# ---------------------------------------------------------------------------
# Git failure → typed CommitError
# ---------------------------------------------------------------------------


async def test_git_failure_raises_commit_error(tmp_path: Path):
    """Running against a non-repo surfaces a typed, message-safe error."""
    from lotsa.commit_step import CommitError, execute_commit

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "f.py").write_text("x\n")

    with pytest.raises(CommitError):
        await execute_commit(
            work_dir=not_a_repo,
            task_id="task-1",
            task_title="No repo",
            step_name="code",
        )


# ---------------------------------------------------------------------------
# Git-subprocess discipline (Constitution §1.1 / §2.1)
# ---------------------------------------------------------------------------


def test_module_uses_async_subprocess_not_blocking():
    """The module must not use ``subprocess.run`` or a shell string for git."""
    from lotsa import commit_step

    src = Path(commit_step.__file__).read_text()
    assert "subprocess.run" not in src, "commit_step must use asyncio.create_subprocess_exec, not subprocess.run"
    assert "shell=True" not in src, "commit_step must never invoke git via a shell"
    assert "create_subprocess_exec" in src
