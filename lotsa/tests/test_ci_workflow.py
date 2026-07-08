"""Acceptance tests for 'make CI actually run on task branches + broaden coverage'.

These tests encode the acceptance criteria from the implementation plan as
runnable assertions over the *content* of ``.github/workflows/ci.yml``. The
task is a single-file config change, so the executable specification is a
YAML-structure test rather than a unit test against a Python module.

Criteria verified:

  * The ``push`` trigger fires on ``lotsa/*`` task branches (and still ``main``)
    so CI runs on the branch push itself, independent of the PR-opening token.
  * A ``concurrency`` block scoped to the ref with ``cancel-in-progress: true``
    is present, so repeated pushes/rebases don't stack redundant runs.
  * The pipeline runs the checks CI currently skips: frontend ``lint``,
    ``typecheck``, ``test`` (vitest), and ``mypy lotsa/ rigg/``.
  * All existing steps (Python ``[dev]`` install, ``npm ci && npm run build``,
    git identity config, ruff check/format, ``pytest lotsa/tests rigg/tests``)
    are preserved.
  * The ``pull_request`` trigger is retained.

Tests in this file fail until ``ci.yml`` is updated: the current workflow
triggers only on ``push`` to ``main``, has no ``concurrency`` block, and runs
neither the frontend lint/typecheck/vitest checks nor ``mypy``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci_config() -> dict:
    """Parse ``.github/workflows/ci.yml`` into a dict.

    Fails loudly (not with a collection error) if the file is missing or the
    YAML is malformed — a broken workflow silently never runs.
    """
    assert CI_WORKFLOW.exists(), f"{CI_WORKFLOW} does not exist"
    parsed = yaml.safe_load(CI_WORKFLOW.read_text())
    assert isinstance(parsed, dict), "ci.yml did not parse to a mapping"
    return parsed


def _all_run_commands(ci_config: dict) -> list[str]:
    """Every ``run:`` command across every step of every job, concatenated."""
    commands: list[str] = []
    for job in ci_config.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                commands.append(run)
    return commands


def _on_section(ci_config: dict) -> dict:
    """The workflow's trigger section.

    PyYAML parses the bare key ``on:`` as the boolean ``True`` (YAML 1.1),
    so accept either spelling.
    """
    section = ci_config.get("on", ci_config.get(True))
    assert isinstance(section, dict), "ci.yml has no mapping-style `on:` section"
    return section


# ---------------------------------------------------------------------------
# The file is present and valid YAML
# ---------------------------------------------------------------------------


def test_ci_workflow_file_exists():
    """.github/workflows/ci.yml is present."""
    assert CI_WORKFLOW.exists()


def test_ci_workflow_is_valid_yaml(ci_config):
    """The workflow parses as a YAML mapping with a `jobs` section."""
    assert "jobs" in ci_config
    assert isinstance(ci_config["jobs"], dict) and ci_config["jobs"]


# ---------------------------------------------------------------------------
# Criterion 1 — push trigger fires on lotsa/* task branches (and main)
# ---------------------------------------------------------------------------


def test_push_trigger_includes_lotsa_task_branches(ci_config):
    """The `push` trigger matches `lotsa/<task_id>` branches.

    Task branches are `lotsa/<task_id>`; the push happens regardless of who
    opens the PR, so broadening `push` is the token-independent fix.
    """
    push = _on_section(ci_config).get("push")
    assert isinstance(push, dict), "`push` trigger missing or not a mapping"
    branches = push.get("branches")
    assert branches, "`push.branches` is missing or empty"
    assert any("lotsa/" in str(pattern) for pattern in branches), f"no `lotsa/*` pattern in push.branches: {branches!r}"


def test_push_trigger_still_includes_main(ci_config):
    """Broadening the trigger must not drop `main`."""
    branches = _on_section(ci_config)["push"]["branches"]
    assert any(str(pattern) == "main" for pattern in branches), f"`main` missing from push.branches: {branches!r}"


def test_pull_request_trigger_retained(ci_config):
    """The `pull_request` trigger is kept (belt-and-suspenders)."""
    on = _on_section(ci_config)
    assert "pull_request" in on, "`pull_request` trigger was dropped"


# ---------------------------------------------------------------------------
# Criterion 2 — ref-scoped concurrency with cancel-in-progress
# ---------------------------------------------------------------------------


def test_concurrency_block_present(ci_config):
    """A top-level `concurrency` block exists."""
    assert "concurrency" in ci_config, "no top-level `concurrency` block"
    assert isinstance(ci_config["concurrency"], dict)


def test_concurrency_group_scoped_to_ref(ci_config):
    """The concurrency group keys on the ref (`github.ref`)."""
    group = ci_config["concurrency"].get("group", "")
    assert "github.ref" in str(group), f"concurrency.group is not scoped to the ref: {group!r}"


def test_concurrency_cancels_in_progress(ci_config):
    """Redundant in-progress runs on the same ref are cancelled."""
    assert ci_config["concurrency"].get("cancel-in-progress") is True, "concurrency.cancel-in-progress must be true"


def test_same_repo_pull_request_run_is_deduped(ci_config):
    """A same-repo `lotsa/**` PR must not run CI twice (once via push, once via PR).

    For a `lotsa/**` task branch the `push` event already runs CI on the exact
    commit. The two events land in different `concurrency` groups
    (`refs/heads/...` vs `refs/pull/N/merge`), so `cancel-in-progress` can't
    dedupe them — the job itself must be guarded to skip the redundant
    `pull_request` run for same-repo heads while still running for forks (whose
    branch push never reaches the base repo).
    """
    guard = str(ci_config["jobs"]["test"].get("if", ""))
    assert "pull_request" in guard, f"test job has no pull_request dedup guard: {guard!r}"
    assert "head.repo.full_name" in guard and "github.repository" in guard, (
        f"test job guard doesn't compare PR head repo against the base repo: {guard!r}"
    )


def test_same_repo_pull_request_dedup_is_scoped_to_push_covered_branches(ci_config):
    """The dedup skip must be scoped to branches the `push` trigger covers.

    The `push` trigger only fires on `main` and `lotsa/**`. If the guard skips
    the `pull_request` run for *every* same-repo head, a same-repo PR from a
    branch outside that filter (e.g. a human `fix-ci` branch) gets neither a
    `push` run nor a `pull_request` run — no CI at all. The guard must therefore
    reference the `lotsa/`-branch scope (via `github.head_ref`) so only
    push-covered same-repo heads are skipped.
    """
    guard = str(ci_config["jobs"]["test"].get("if", ""))
    assert "head_ref" in guard and "lotsa/" in guard, (
        f"test job guard skips ALL same-repo PRs; it must scope the skip to push-covered "
        f"`lotsa/` branches so other same-repo PRs still run CI: {guard!r}"
    )


# ---------------------------------------------------------------------------
# Criterion 3 — the new checks run (frontend lint/typecheck/vitest + mypy)
# ---------------------------------------------------------------------------


def test_frontend_lint_runs(ci_config):
    """CI runs `npm run lint` for the frontend."""
    commands = _all_run_commands(ci_config)
    assert any("npm run lint" in cmd for cmd in commands), "no `npm run lint` step found"


def test_frontend_typecheck_runs(ci_config):
    """CI runs `npm run typecheck` for the frontend."""
    commands = _all_run_commands(ci_config)
    assert any("npm run typecheck" in cmd for cmd in commands), "no `npm run typecheck` step found"


def test_frontend_vitest_runs(ci_config):
    """CI runs the frontend unit tests (`npm run test` / vitest)."""
    commands = _all_run_commands(ci_config)
    assert any("npm run test" in cmd for cmd in commands), "no `npm run test` (vitest) step found"


def test_mypy_runs_over_lotsa_and_rigg(ci_config):
    """CI runs `mypy lotsa/ rigg/` (mirrors Makefile:58)."""
    commands = _all_run_commands(ci_config)
    assert any("mypy" in cmd and "lotsa/" in cmd and "rigg/" in cmd for cmd in commands), (
        "no `mypy lotsa/ rigg/` step found"
    )


# ---------------------------------------------------------------------------
# Criterion 4 — every existing step is preserved
# ---------------------------------------------------------------------------


def test_python_dev_install_preserved(ci_config):
    """The Python package + dev-deps install step is preserved."""
    commands = _all_run_commands(ci_config)
    assert any('pip install -e ".[dev]"' in cmd or "pip install -e '.[dev]'" in cmd for cmd in commands), (
        '`pip install -e ".[dev]"` step is missing'
    )


def test_frontend_build_preserved(ci_config):
    """The `npm ci && npm run build` dashboard-build step is preserved."""
    commands = _all_run_commands(ci_config)
    assert any("npm ci" in cmd for cmd in commands), "`npm ci` step is missing"
    assert any("npm run build" in cmd for cmd in commands), "`npm run build` step is missing"


def test_git_identity_config_preserved(ci_config):
    """The git identity config step (tests create commits) is preserved."""
    commands = _all_run_commands(ci_config)
    assert any("git config" in cmd and "user.email" in cmd for cmd in commands), "git identity config step is missing"


def test_ruff_checks_preserved(ci_config):
    """Both ruff lint and ruff format --check are preserved."""
    commands = _all_run_commands(ci_config)
    assert any("ruff check" in cmd for cmd in commands), "`ruff check` is missing"
    assert any("ruff format --check" in cmd for cmd in commands), "`ruff format --check` is missing"


def test_pytest_step_preserved(ci_config):
    """The `pytest lotsa/tests rigg/tests` step is preserved."""
    commands = _all_run_commands(ci_config)
    assert any("pytest" in cmd and "lotsa/tests" in cmd and "rigg/tests" in cmd for cmd in commands), (
        "`pytest lotsa/tests rigg/tests` step is missing"
    )
