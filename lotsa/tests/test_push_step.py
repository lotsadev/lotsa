"""Tests for push_step — precondition checks and PR text generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lotsa.push_step import (
    PushError,
    check_preconditions,
    execute_push,
)

# NOTE: PR title/body generation moved to the diff-driven ``pr_summary`` step.
# The Conventional Commits parsing/fallback helpers (``parse_pr_description``,
# ``build_pr_text``, ``_detect_commit_type`` with its new (files, subjects)
# signature, etc.) are covered by ``test_pr_summary.py``. This module now
# covers only the mechanical push: preconditions, the git push, and the
# ``title``/``body`` pass-through to ``create_pr``.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HTTPS_REMOTE = "https://github.com/acme/my-repo.git"
_SSH_REMOTE = "git@github.com:acme/my-repo.git"
_WORK_DIR = Path("/tmp/fake-work-dir")


def _patch_helpers(remote_url: str, has_changes: bool):
    """Context manager that patches async git helpers."""
    return (
        patch("lotsa.push_step._get_remote_url", new=AsyncMock(return_value=remote_url)),
        patch("lotsa.push_step._has_uncommitted_changes", new=AsyncMock(return_value=has_changes)),
    )


# ---------------------------------------------------------------------------
# check_preconditions — GITHUB_TOKEN missing
# ---------------------------------------------------------------------------


async def test_preconditions_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    # The ``NO_GITHUB:`` prefix is the machine-detectable contract the push
    # callers read to route a GitHub-less setup to ``awaiting_operator``
    # (ADR-043) rather than ``blocked``. Lock both the prefix and the human
    # text so a future reword can't silently break the escape-hatch routing.
    with remote_patch, changes_patch, pytest.raises(PushError, match="NO_GITHUB:.*GITHUB_TOKEN"):
        await check_preconditions(_WORK_DIR)


# ---------------------------------------------------------------------------
# check_preconditions — SSH remote rejected
# ---------------------------------------------------------------------------


async def test_preconditions_ssh_remote_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    remote_patch, changes_patch = _patch_helpers(_SSH_REMOTE, False)
    with remote_patch, changes_patch, pytest.raises(PushError, match="SSH"):
        await check_preconditions(_WORK_DIR)


async def test_preconditions_ssh_error_mentions_https(monkeypatch):
    """SSH error message should hint at switching to HTTPS."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    remote_patch, changes_patch = _patch_helpers(_SSH_REMOTE, False)
    with remote_patch, changes_patch, pytest.raises(PushError, match="HTTPS"):
        await check_preconditions(_WORK_DIR)


# ---------------------------------------------------------------------------
# check_preconditions — uncommitted changes (ADR-024: defence-in-depth)
# ---------------------------------------------------------------------------


async def test_preconditions_dirty_working_tree_warns_but_proceeds(monkeypatch, caplog):
    """ADR-024 R7: the clean-tree guard is now defence-in-depth.

    The ``commit`` posthook runs before ``push_pr``, so an uncommitted tree
    should be unreachable in normal flow. If it does happen, the push step no
    longer hard-errors — it logs a warning and proceeds (the push is by HEAD
    SHA, so uncommitted working-tree changes simply aren't part of the pushed
    commit). Pre-fix this raised ``PushError`` and was the primary
    user-visible failure.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, True)
    with remote_patch, changes_patch, caplog.at_level("WARNING", logger="lotsa.push_step"):
        token, owner, repo = await check_preconditions(_WORK_DIR)

    # Proceeds rather than raising.
    assert token == "ghp_test_token"
    assert owner == "acme"
    assert repo == "my-repo"
    # ...and warns so a bypassed commit posthook is observable.
    assert any("uncommitted" in r.getMessage().lower() for r in caplog.records), (
        "a dirty tree at push time must be logged as a warning, not silently swallowed"
    )


# ---------------------------------------------------------------------------
# check_preconditions — success path
# ---------------------------------------------------------------------------


async def test_preconditions_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    with remote_patch, changes_patch:
        token, owner, repo = await check_preconditions(_WORK_DIR)

    assert token == "ghp_test_token"
    assert owner == "acme"
    assert repo == "my-repo"


async def test_preconditions_success_no_git_suffix(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")

    remote_patch, changes_patch = _patch_helpers("https://github.com/org/project", False)
    with remote_patch, changes_patch:
        token, owner, repo = await check_preconditions(_WORK_DIR)

    assert token == "ghp_abc"
    assert owner == "org"
    assert repo == "project"


# ---------------------------------------------------------------------------
# execute_push — token scrubbing
# ---------------------------------------------------------------------------


def _make_proc_mock(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock subprocess with communicate()."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


_FAKE_HEAD_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _patch_git_helpers():
    """Patch the per-test git helpers that ``execute_push`` calls before/after push.

    ``_get_head_sha`` returns a constant SHA; ``_normalize_worktree_branch`` is
    a no-op AsyncMock so existing tests don't have to model post-push branch
    normalization.
    """
    return (
        patch("lotsa.push_step._get_head_sha", new=AsyncMock(return_value=_FAKE_HEAD_SHA)),
        patch("lotsa.push_step._normalize_worktree_branch", new=AsyncMock(return_value=None)),
    )


async def test_execute_push_scrubs_token_from_error(monkeypatch, tmp_path):
    """Token must be scrubbed from PushError message on git failure."""
    token = "ghp_secret_token_12345"
    monkeypatch.setenv("GITHUB_TOKEN", token)

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(
        returncode=1,
        stderr=f"fatal: unable to access 'https://{token}@github.com/acme/my-repo.git/': error".encode(),
    )

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        pytest.raises(PushError) as exc_info,
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    assert token not in str(exc_info.value)
    assert "***" in str(exc_info.value)


async def test_execute_push_non_fast_forward(monkeypatch, tmp_path):
    """Non-fast-forward rejection should raise PushError with NON_FAST_FORWARD."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(
        returncode=1,
        stderr=b"! [rejected] lotsa/task-1 -> lotsa/task-1 (non-fast-forward)",
    )

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        pytest.raises(PushError, match="NON_FAST_FORWARD"),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")


async def test_execute_push_creates_pr_when_no_pr_number(monkeypatch, tmp_path):
    """When pr_number is None, execute_push should call create_pr."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(returncode=0)

    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=42)
    mock_client.get_default_branch = AsyncMock(return_value="main")
    mock_client.close = AsyncMock()

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        pr_num, pr_url, owner, repo = await execute_push(
            tmp_path,
            "task-1",
            None,
            None,
            title="feat: build X",
            body="Plan for X",
        )

    assert pr_num == 42
    assert owner == "acme"
    mock_client.create_pr.assert_called_once()
    # execute_push is mechanical: it passes the given title/body to create_pr
    # verbatim, with no synthesis of its own.
    create_kwargs = mock_client.create_pr.call_args.kwargs
    assert create_kwargs["title"] == "feat: build X"
    assert create_kwargs["body"] == "Plan for X"


async def test_execute_push_skips_create_pr_when_pr_exists(monkeypatch, tmp_path):
    """When pr_number is provided, execute_push should NOT call create_pr."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(returncode=0)

    mock_client = AsyncMock()
    mock_client.close = AsyncMock()

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        pr_num, pr_url, owner, repo = await execute_push(
            tmp_path,
            "task-1",
            99,
            "main",
            title=None,
            body=None,
        )

    assert pr_num == 99
    mock_client.create_pr.assert_not_called()


# ---------------------------------------------------------------------------
# ADR-040 R1 — a resumed push must not open a duplicate PR
# ---------------------------------------------------------------------------


async def test_resumed_push_with_pr_number_opens_no_duplicate_pr(monkeypatch, tmp_path):
    """When an interrupted push is re-dispatched, the existing ``pr_number``
    (read from task metadata) is passed in, so ``execute_push`` re-pushes the
    branch idempotently and calls ``create_pr`` ZERO times — no duplicate PR
    (ADR-040 R1 / acceptance criterion 1).
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(returncode=0)

    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=1234)
    mock_client.close = AsyncMock()

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        # First (interrupted) push already created PR #77; the resume passes it back.
        pr_num, _url, _owner, _repo = await execute_push(
            tmp_path,
            "task-1",
            77,  # pr_number known from metadata
            "main",
            title=None,
            body=None,
        )

    assert pr_num == 77
    mock_client.create_pr.assert_not_called()


# ---------------------------------------------------------------------------
# execute_push — push-by-HEAD refspec
# ---------------------------------------------------------------------------


async def test_execute_push_uses_head_sha_in_refspec(monkeypatch, tmp_path):
    """Push refspec must use the worktree's actual HEAD SHA, not the local
    ``lotsa/<task_id>`` branch name. This is what makes the push step
    survive the agent leaving the worktree on a side branch (``feature/...``)
    instead of the orchestrator's pre-set branch.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(returncode=0)
    subprocess_mock = AsyncMock(return_value=proc)

    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=42)
    mock_client.get_default_branch = AsyncMock(return_value="main")
    mock_client.close = AsyncMock()

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=subprocess_mock),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    # The single subprocess call here is the push itself; positional args 2-3
    # are the URL and refspec.
    push_call = subprocess_mock.await_args
    assert push_call is not None
    args = push_call.args
    assert args[0] == "git"
    assert args[1] == "push"
    refspec = args[3]
    assert refspec == f"{_FAKE_HEAD_SHA}:refs/heads/lotsa/task-1", (
        f"Expected push to use HEAD SHA in refspec, got {refspec!r}"
    )


async def test_execute_push_normalizes_worktree_after_success(monkeypatch, tmp_path):
    """After a successful push the orchestrator should normalize the local
    worktree state: ``_normalize_worktree_branch`` is invoked with the
    canonical branch name and the just-pushed SHA so the next round starts
    on ``lotsa/<task_id>`` regardless of what the agent left it on.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_mock = AsyncMock(return_value=_FAKE_HEAD_SHA)
    normalize_mock = AsyncMock(return_value=None)
    proc = _make_proc_mock(returncode=0)

    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=42)
    mock_client.get_default_branch = AsyncMock(return_value="main")
    mock_client.close = AsyncMock()

    with (
        remote_patch,
        changes_patch,
        patch("lotsa.push_step._get_head_sha", new=head_mock),
        patch("lotsa.push_step._normalize_worktree_branch", new=normalize_mock),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    normalize_mock.assert_awaited_once_with(tmp_path, "lotsa/task-1", _FAKE_HEAD_SHA)


async def test_execute_push_skips_normalize_on_push_failure(monkeypatch, tmp_path):
    """When the push itself fails, the post-push normalize step must not
    run — the remote is not in the expected state, so leaving the local
    refs untouched is the conservative choice (the failure raises
    ``PushError`` and the orchestrator decides what to do next).
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_mock = AsyncMock(return_value=_FAKE_HEAD_SHA)
    normalize_mock = AsyncMock(return_value=None)
    proc = _make_proc_mock(returncode=1, stderr=b"some random push error")

    with (
        remote_patch,
        changes_patch,
        patch("lotsa.push_step._get_head_sha", new=head_mock),
        patch("lotsa.push_step._normalize_worktree_branch", new=normalize_mock),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.py")),
        pytest.raises(PushError),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    normalize_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _get_head_sha — direct unit tests
# ---------------------------------------------------------------------------


async def test_get_head_sha_returns_sha_from_stdout(tmp_path):
    """``_get_head_sha`` returns the trimmed SHA string from git's stdout."""
    from lotsa.push_step import _get_head_sha

    proc = _make_proc_mock(returncode=0, stdout=b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        sha = await _get_head_sha(tmp_path)

    assert sha == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


async def test_get_head_sha_raises_on_nonzero_return(tmp_path):
    """If ``git rev-parse HEAD`` fails, the helper surfaces a clear PushError."""
    from lotsa.push_step import _get_head_sha

    proc = _make_proc_mock(returncode=128, stderr=b"fatal: not a git repository")
    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        pytest.raises(PushError, match="rev-parse HEAD"),
    ):
        await _get_head_sha(tmp_path)


async def test_get_head_sha_raises_on_empty_output(tmp_path):
    """If git returns success but empty stdout, treat it as a hard error
    rather than pushing nothing to the remote ref."""
    from lotsa.push_step import _get_head_sha

    proc = _make_proc_mock(returncode=0, stdout=b"\n")
    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        pytest.raises(PushError, match="empty output"),
    ):
        await _get_head_sha(tmp_path)


async def test_execute_push_askpass_uses_x_access_token_username(monkeypatch, tmp_path):
    """The GIT_ASKPASS helper must return 'x-access-token' for Username prompts.

    Regression: returning the PAT for both Username and Password works today
    because GitHub ignores the username for PAT auth, but diverges from the
    canonical x-access-token:<PAT> form and could break under stricter configs.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    written: dict[str, str] = {}

    class FakeFile:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def write(self, content: str) -> None:
            written["content"] = content

    proc = _make_proc_mock(returncode=0)
    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=1)
    mock_client.get_default_branch = AsyncMock(return_value="main")
    mock_client.close = AsyncMock()

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen", return_value=FakeFile()),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.sh")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    body = written.get("content", "")
    assert "x-access-token" in body, "askpass script must echo x-access-token for Username prompts"
    assert "LOTSA_GIT_TOKEN" in body, "askpass script must still read the token from env"
    # Username branch must come before password fallback
    assert body.index("Username") < body.index("LOTSA_GIT_TOKEN")


async def test_execute_push_cleans_up_askpass_on_failure(monkeypatch, tmp_path):
    """The askpass temp file must be deleted even when git fails."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    remote_patch, changes_patch = _patch_helpers(_HTTPS_REMOTE, False)
    head_patch, normalize_patch = _patch_git_helpers()
    proc = _make_proc_mock(returncode=1, stderr=b"fatal: some error")

    unlink_mock = MagicMock()

    with (
        remote_patch,
        changes_patch,
        head_patch,
        normalize_patch,
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("pathlib.Path.unlink", unlink_mock),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/lotsa-askpass-xyz.py")),
        pytest.raises(PushError),
    ):
        await execute_push(tmp_path, "task-1", None, "main", title="t", body="b")

    # Cleanup uses Path(...).unlink(missing_ok=True) so the bound-method
    # call is on a Path instance and only takes the missing_ok kwarg.
    unlink_mock.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# _normalize_worktree_branch — direct unit tests
# ---------------------------------------------------------------------------


async def test_normalize_worktree_branch_runs_checkout_B(tmp_path):
    """Happy path: invokes ``git checkout -B <branch> <head_sha>``."""
    from lotsa.push_step import _normalize_worktree_branch

    proc = _make_proc_mock(returncode=0)
    subprocess_mock = AsyncMock(return_value=proc)
    with patch("asyncio.create_subprocess_exec", new=subprocess_mock):
        await _normalize_worktree_branch(tmp_path, "lotsa/abc", _FAKE_HEAD_SHA)

    # Exactly one subprocess call now (collapsed from the original two-step
    # branch -f + checkout to a single atomic ``checkout -B``).
    assert subprocess_mock.call_count == 1
    args = subprocess_mock.await_args.args
    assert args[:3] == ("git", "checkout", "-B")
    assert args[3] == "lotsa/abc"
    assert args[4] == _FAKE_HEAD_SHA


async def test_normalize_worktree_branch_succeeds_silently(tmp_path, caplog):
    """No warning is logged when the subprocess returns 0."""
    from lotsa.push_step import _normalize_worktree_branch

    proc = _make_proc_mock(returncode=0)
    with (
        caplog.at_level("WARNING", logger="lotsa.push_step"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
    ):
        await _normalize_worktree_branch(tmp_path, "lotsa/abc", _FAKE_HEAD_SHA)

    assert not caplog.records, f"Unexpected warnings on happy path: {caplog.records}"


async def test_normalize_worktree_branch_logs_warning_on_failure(tmp_path, caplog):
    """Subprocess failure logs a warning naming the branch + SHA, no raise."""
    from lotsa.push_step import _normalize_worktree_branch

    proc = _make_proc_mock(returncode=128, stderr=b"fatal: bad ref")
    with (
        caplog.at_level("WARNING", logger="lotsa.push_step"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
    ):
        # Must not raise — the remote is already correct; this is best-effort.
        await _normalize_worktree_branch(tmp_path, "lotsa/abc", _FAKE_HEAD_SHA)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "lotsa/abc" in msg
    assert _FAKE_HEAD_SHA in msg
    assert "bad ref" in msg


# ---------------------------------------------------------------------------
# reconcile_branch_with_remote — self-heal a PR branch an operator pushed to
# (e.g. GitHub's resolve-conflicts button). Regression for an internal task.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402

from lotsa.push_step import ReconcileConflict, reconcile_branch_with_remote  # noqa: E402


def _g(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _setup_diverged(tmp_path: Path, *, local_content: str, origin_content: str) -> Path:
    """Build a worktree whose ``lotsa/task-x`` diverged from its remote.

    Common ancestor A (file.txt='base'). The remote branch advanced to B
    (operator push, file.txt=origin_content); the local branch advanced to C
    (file.txt=local_content) — a non-fast-forward. Returns the work repo.
    """
    subprocess.run(["git", "init", "--bare", "origin.git"], cwd=tmp_path, check=True, capture_output=True)
    origin = tmp_path / "origin.git"

    subprocess.run(["git", "clone", str(origin), "work"], cwd=tmp_path, check=True, capture_output=True)
    work = tmp_path / "work"
    _g(["config", "user.email", "t@e.com"], work)
    _g(["config", "user.name", "T"], work)
    (work / "file.txt").write_text("base\n")
    _g(["add", "."], work)
    _g(["commit", "-m", "A"], work)
    _g(["checkout", "-b", "lotsa/task-x"], work)
    _g(["push", "-u", "origin", "lotsa/task-x"], work)  # origin = A

    # Operator advances the remote branch to B via a second clone.
    subprocess.run(["git", "clone", str(origin), "op"], cwd=tmp_path, check=True, capture_output=True)
    op = tmp_path / "op"
    _g(["config", "user.email", "op@e.com"], op)
    _g(["config", "user.name", "Op"], op)
    _g(["checkout", "lotsa/task-x"], op)
    (op / "file.txt").write_text(origin_content)
    _g(["add", "."], op)
    _g(["commit", "-m", "B (operator)"], op)
    _g(["push", "origin", "lotsa/task-x"], op)  # origin = A -> B

    # Local advances to C (without B) — now non-fast-forward against origin.
    (work / "file.txt").write_text(local_content)
    _g(["add", "."], work)
    _g(["commit", "-m", "C (local)"], work)
    return work


async def test_reconcile_drops_empty_commit_on_identical_divergence(tmp_path, monkeypatch):
    """The exact e0dd2fb4 shape: local and remote resolved to identical content.

    Reconcile rebases the local commit onto the remote tip; it becomes empty and
    drops, leaving the worktree == origin/lotsa/task-x — so the retried push is a
    plain fast-forward. Pre-fix the posthook just blocked on NON_FAST_FORWARD.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    work = _setup_diverged(tmp_path, local_content="resolved\n", origin_content="resolved\n")

    reconciled = await reconcile_branch_with_remote(work, "task-x")

    assert reconciled is True
    local_head = _g(["rev-parse", "HEAD"], work)
    remote_tip = _g(["rev-parse", "refs/heads/lotsa/task-x"], tmp_path / "origin.git")
    assert local_head == remote_tip, "worktree must now incorporate the remote tip"
    assert _g(["status", "--porcelain"], work) == ""


async def test_reconcile_leaves_merge_markers_on_incompatible_divergence(tmp_path, monkeypatch):
    """When local and remote changed the same line differently, reconcile can't
    rebase — it aborts the rebase, then MERGES the remote tip so the conflict is
    left in the worktree as markers (identical shape to _sync_branch_to_main's
    origin/main path), and raises ReconcileConflict carrying the unmerged paths.
    The orchestrator then dispatches resolve_conflicts against those markers."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    work = _setup_diverged(tmp_path, local_content="local version\n", origin_content="operator version\n")

    with pytest.raises(ReconcileConflict) as exc_info:
        await reconcile_branch_with_remote(work, "task-x")

    # The unmerged paths ride on the exception for the resolve_conflicts dispatch.
    assert exc_info.value.conflicting_files == ("file.txt",)
    # A merge (not a rebase) is left in progress with real conflict markers —
    # the exact worktree shape the resolve_conflicts agent + commit posthook
    # already handle for the origin/main conflict path.
    assert (work / ".git" / "MERGE_HEAD").exists()
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    status = _g(["status", "--porcelain"], work)
    assert "UU file.txt" in status, f"expected an unmerged file.txt, got: {status!r}"
    assert "<<<<<<<" in (work / "file.txt").read_text()


async def test_reconcile_returns_false_when_remote_branch_absent(tmp_path, monkeypatch):
    """No remote branch to reconcile against → return False so the caller surfaces
    the original push error unchanged."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    subprocess.run(["git", "init", "--bare", "origin.git"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), "work"], cwd=tmp_path, check=True, capture_output=True
    )
    work = tmp_path / "work"
    _g(["config", "user.email", "t@e.com"], work)
    _g(["config", "user.name", "T"], work)
    (work / "f").write_text("x\n")
    _g(["add", "."], work)
    _g(["commit", "-m", "A"], work)

    assert await reconcile_branch_with_remote(work, "task-x") is False
