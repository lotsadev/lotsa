"""Tests for the posthook abstraction (ADR-024).

Three concerns, three sections:

1. **Registry surface** — ``lotsa.registry`` grows a posthook registry
   symmetric with the existing tool/engine registries
   (``register_posthook`` / ``get_posthook`` / ``is_posthook_registered`` /
   ``list_posthooks``), and ``snapshot``/``restore`` cover it for test
   isolation.
2. **Built-in ``commit`` posthook** — ``lotsa.posthooks`` registers a
   ``commit`` posthook that wraps ``lotsa.commit_step.execute_commit`` and
   returns a ``ToolResult`` (commit_sha / no-op / failure).
3. **Flow model** — ``flows.py`` parses + resolves a per-step ``posthooks``
   (and ``commit_prefix``) field and validates referenced names at
   build time.

The ``_isolated_registry`` autouse fixture (conftest.py) snapshots/restores
the global registry around every test, so registrations here are hermetic.
None of this exists yet; every test is expected to fail until it lands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ===========================================================================
# 1. Registry surface
# ===========================================================================


def test_registry_exports_posthook_api():
    from lotsa import registry

    assert hasattr(registry, "register_posthook")
    assert hasattr(registry, "get_posthook")
    assert hasattr(registry, "is_posthook_registered")
    assert hasattr(registry, "list_posthooks")


def test_register_posthook_makes_it_retrievable():
    from lotsa.registry import get_posthook, register_posthook

    async def my_hook(ctx, config):
        from lotsa.tools import ToolResult

        return ToolResult(success=True, output="ok")

    register_posthook("my_hook", my_hook)
    assert get_posthook("my_hook") is my_hook


def test_register_posthook_rejects_name_collision():
    from lotsa.registry import register_posthook

    async def h1(ctx, config): ...

    async def h2(ctx, config): ...

    register_posthook("dup_hook", h1)
    with pytest.raises(ValueError, match="dup_hook"):
        register_posthook("dup_hook", h2)


def test_get_posthook_unknown_name_raises_with_registered_list():
    from lotsa.registry import get_posthook, register_posthook

    async def known(ctx, config): ...

    register_posthook("known_hook", known)

    with pytest.raises(KeyError) as exc_info:
        get_posthook("nope_hook")
    msg = str(exc_info.value)
    assert "nope_hook" in msg
    assert "known_hook" in msg


def test_builtin_commit_posthook_registered_on_import():
    """Importing ``lotsa.posthooks`` registers the built-in ``commit`` posthook."""
    import lotsa.posthooks  # noqa: F401 — import side effect registers built-ins
    from lotsa.registry import get_posthook

    fn = get_posthook("commit")
    assert callable(fn)


def test_snapshot_restore_covers_posthooks():
    """``snapshot``/``restore`` round-trips the posthook registry too.

    A posthook registered after a snapshot must be dropped by restore, while
    the built-in baseline (``commit``) survives.
    """
    import lotsa.posthooks  # noqa: F401 — ensures the built-in baseline exists
    from lotsa import registry as reg

    snap = reg.snapshot()

    async def temp_hook(ctx, config): ...

    reg.register_posthook("temp_hook", temp_hook)
    assert reg.is_posthook_registered("temp_hook")

    reg.restore(snap)
    assert not reg.is_posthook_registered("temp_hook")
    assert reg.is_posthook_registered("commit")


# ===========================================================================
# 2. Built-in ``commit`` posthook (wraps execute_commit; real git repo)
# ===========================================================================


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Lotsa Test"], repo)
    (repo / "README.md").write_text("initial\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


def _make_ctx(worktree: Path, *, step_name: str = "code", task_id: str = "task-1"):
    """Build a TaskContext for a posthook. ``db`` is unused by ``commit``."""
    from lotsa.tools import TaskContext

    return TaskContext(
        task_id=task_id,
        worktree=worktree,
        metadata={},
        db=None,  # the commit posthook does not touch the DB
        process_name="full",
        flow_name="main",
        current_flow="main",
        last_run_step=step_name,
    )


async def test_commit_posthook_commits_dirty_tree(git_repo: Path):
    from lotsa.posthooks import commit_posthook

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo)
    result = await commit_posthook(ctx, {"task_title": "Add feature", "commit_prefix": "chore"})

    assert result.success is True
    assert result.metadata.get("commit_sha")
    # The commit landed and the worktree is clean.
    assert _git(["status", "--porcelain"], git_repo) == ""
    assert _git(["rev-parse", "HEAD"], git_repo) == result.metadata["commit_sha"]


async def test_commit_posthook_noop_on_clean_tree(git_repo: Path):
    from lotsa.posthooks import commit_posthook

    ctx = _make_ctx(git_repo)
    result = await commit_posthook(ctx, {"task_title": "Nothing to do"})

    assert result.success is True
    # No-op success carries no commit SHA; flagged as a no-op.
    assert not result.metadata.get("commit_sha")
    assert result.metadata.get("noop") is True


async def test_commit_posthook_failure_returns_unsuccessful(tmp_path: Path):
    """A git failure surfaces as ``success=False`` (the orchestrator blocks)."""
    from lotsa.posthooks import commit_posthook

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "f.py").write_text("x\n")

    ctx = _make_ctx(not_a_repo)
    result = await commit_posthook(ctx, {"task_title": "No repo"})

    assert result.success is False
    assert result.output  # a non-empty, operator-safe error message


# ===========================================================================
# 2b. Commit-and-publish — once a PR exists the posthook pushes HEAD too
#     (ADR-024 Addendum 2, issue #155)
# ===========================================================================


async def test_commit_posthook_publishes_when_pr_exists(git_repo: Path, monkeypatch):
    """With a ``pr_number`` in metadata, a committing round publishes HEAD to
    the existing PR branch in the same posthook."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    calls: list[dict] = []

    async def fake_execute_push(**kw):
        calls.append(kw)
        return kw["pr_number"], f"https://github.com/o/r/pull/{kw['pr_number']}", "o", "r"

    monkeypatch.setattr(push_step, "execute_push", fake_execute_push)

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo, task_id="abc123")
    ctx.metadata["pr_number"] = 143
    result = await commit_posthook(ctx, {"task_title": "Add feature"})

    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["pr_number"] == 143
    assert calls[0]["task_id"] == "abc123"
    # Re-push locates (does not create) the PR, so base_branch is unused here.
    assert calls[0]["base_branch"] is None
    assert result.metadata.get("commit_sha")
    assert result.metadata.get("pr_url") == "https://github.com/o/r/pull/143"
    assert "published to PR #143" in result.output


async def test_commit_posthook_publishes_even_on_noop(git_repo: Path, monkeypatch):
    """Drift convergence: a no-op-commit round still publishes HEAD, so commits
    stranded by an earlier round (a ``PR_FIX_SKIPPED`` round, a sync-merge)
    reach the PR branch on the next posthook run."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    calls: list[dict] = []

    async def fake_execute_push(**kw):
        calls.append(kw)
        return kw["pr_number"], "https://github.com/o/r/pull/9", "o", "r"

    monkeypatch.setattr(push_step, "execute_push", fake_execute_push)

    ctx = _make_ctx(git_repo)  # clean tree → the commit is a no-op
    ctx.metadata["pr_number"] = 9
    result = await commit_posthook(ctx, {"task_title": "nothing to commit"})

    assert result.success is True
    assert result.metadata.get("noop") is True
    assert len(calls) == 1, "publish must fire even when this round committed nothing"
    assert "published to PR #9" in result.output


async def test_commit_posthook_no_publish_before_pr(git_repo: Path, monkeypatch):
    """Pre-PR (no ``pr_number``) the posthook only commits; ``push_pr`` still
    owns the first push and PR creation."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    calls: list[dict] = []

    async def fake_execute_push(**kw):
        calls.append(kw)
        return 1, "u", "o", "r"

    monkeypatch.setattr(push_step, "execute_push", fake_execute_push)

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo)  # metadata carries no pr_number
    result = await commit_posthook(ctx, {"task_title": "Add feature"})

    assert result.success is True
    assert calls == [], "no publish before a PR exists"
    assert "published" not in result.output


async def test_commit_posthook_publish_failure_blocks(git_repo: Path, monkeypatch):
    """A NON_FAST_FORWARD whose reconcile can't help (here: no ``origin`` remote)
    surfaces as ``success=False`` so the orchestrator routes to ``blocked`` — the
    commit still landed, only the publish failed."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    async def boom(**kw):
        raise push_step.PushError("NON_FAST_FORWARD: remote moved")

    monkeypatch.setattr(push_step, "execute_push", boom)

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo)
    ctx.metadata["pr_number"] = 5
    result = await commit_posthook(ctx, {"task_title": "Add feature"})

    assert result.success is False
    assert result.metadata.get("error_kind") == "publish_failed"
    assert "PR #5" in result.output
    # The commit itself still landed — only the publish failed.
    assert _git(["log", "-1", "--format=%s"], git_repo) == "chore: Add feature (code)"


async def test_commit_posthook_reconciles_and_retries_on_non_fast_forward(git_repo: Path, monkeypatch):
    """A NON_FAST_FORWARD (operator pushed to the PR branch) triggers a reconcile
    + one push retry instead of blocking. Regression for an internal task."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    push_calls: list[dict] = []

    async def fake_execute_push(**kw):
        push_calls.append(kw)
        if len(push_calls) == 1:
            raise push_step.PushError("NON_FAST_FORWARD: remote moved")
        return kw["pr_number"], f"https://github.com/o/r/pull/{kw['pr_number']}", "o", "r"

    reconcile_calls: list[tuple] = []

    async def fake_reconcile(work_dir, task_id):
        reconcile_calls.append((work_dir, task_id))
        return True

    monkeypatch.setattr(push_step, "execute_push", fake_execute_push)
    monkeypatch.setattr(push_step, "reconcile_branch_with_remote", fake_reconcile)

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo, task_id="e0dd2fb4")
    ctx.metadata["pr_number"] = 162
    result = await commit_posthook(ctx, {"task_title": "Add feature"})

    assert result.success is True
    assert len(push_calls) == 2, "must retry the push after reconciling"
    assert reconcile_calls == [(git_repo, "e0dd2fb4")]
    assert "published to PR #162" in result.output


async def test_commit_posthook_blocks_on_reconcile_conflict(git_repo: Path, monkeypatch):
    """A real divergence-conflict during reconcile blocks the task
    (``publish_conflict``) so ``resolve_conflicts`` can handle it — no retry."""
    import lotsa.push_step as push_step
    from lotsa.posthooks import commit_posthook

    push_calls: list[dict] = []

    async def fake_execute_push(**kw):
        push_calls.append(kw)
        raise push_step.PushError("NON_FAST_FORWARD: remote moved")

    async def fake_reconcile(work_dir, task_id):
        raise push_step.ReconcileConflict("rebasing onto origin/lotsa/x conflicted")

    monkeypatch.setattr(push_step, "execute_push", fake_execute_push)
    monkeypatch.setattr(push_step, "reconcile_branch_with_remote", fake_reconcile)

    (git_repo / "feature.py").write_text("x\n")
    ctx = _make_ctx(git_repo)
    ctx.metadata["pr_number"] = 162
    result = await commit_posthook(ctx, {"task_title": "Add feature"})

    assert result.success is False
    assert result.metadata.get("error_kind") == "publish_conflict"
    assert len(push_calls) == 1, "must not retry the push when reconcile conflicts"


# ===========================================================================
# 3. Flow model — per-step posthooks + commit_prefix parsing/resolution
# ===========================================================================


def _build(tmp_path: Path, yaml_text: str):
    """Write a process.yaml and build it (no prompt files needed at build)."""
    from lotsa.flows import build_process

    path = tmp_path / "process.yaml"
    path.write_text(yaml_text)
    return build_process("custom", process_file=path)


def _job(process, name: str):
    return next(rj for rj in process.flows["main"].jobs if rj.name == name)


def test_posthooks_field_parsed_onto_resolved_job(tmp_path: Path):
    import lotsa.posthooks  # noqa: F401 — registers the built-in ``commit`` so validation passes

    process = _build(
        tmp_path,
        """
process: posthook_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [commit]
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").posthooks == ["commit"]


def test_no_posthooks_resolves_to_empty_list(tmp_path: Path):
    process = _build(
        tmp_path,
        """
process: no_posthook_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").posthooks == []


def test_per_binding_posthooks_overrides_job(tmp_path: Path):
    """A binding-level ``posthooks: []`` overrides the job's default."""
    import lotsa.posthooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: binding_override_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [commit]
flows:
  main:
    steps:
      - name: code
        posthooks: []
""",
    )
    assert _job(process, "code").posthooks == []


def test_per_binding_posthooks_can_add_when_job_has_none(tmp_path: Path):
    import lotsa.posthooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: binding_add_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - name: code
        posthooks: [commit]
""",
    )
    assert _job(process, "code").posthooks == ["commit"]


def test_commit_prefix_parsed_and_resolved(tmp_path: Path):
    import lotsa.posthooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: prefix_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [commit]
    commit_prefix: feat
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").commit_prefix == "feat"


def test_unknown_posthook_name_fails_at_build_time(tmp_path: Path):
    """An unregistered posthook name in YAML fails fast in build_process."""
    with pytest.raises(ValueError, match="does_not_exist"):
        _build(
            tmp_path,
            """
process: bad_posthook_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    posthooks: [does_not_exist]
flows:
  main:
    steps:
      - code
""",
        )


def test_posthooks_orthogonal_to_adr016_commit_field(tmp_path: Path):
    """``posthooks: [commit]`` (ADR-024) and ``commit: true`` (ADR-016) are
    independent fields and must not be conflated."""
    import lotsa.posthooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: disambiguation_test
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    output_file: out.txt
    commit: true
    posthooks: [commit]
flows:
  main:
    steps:
      - code
""",
    )
    job = _job(process, "code")
    # ADR-016 boolean (commit this step's output_file) stays True...
    assert job.commit is True
    # ...and is unrelated to the ADR-024 step-posthook list.
    assert job.posthooks == ["commit"]
