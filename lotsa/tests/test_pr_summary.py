"""Failing-test spec for the diff-driven ``pr_summary`` step (red).

These tests pin the new behaviour described in the implementation plan:

* A dedicated ``pr_summary`` agent step in the ``full`` preset, sitting
  ``verify → pr_summary → push_pr`` and producing a ``pr_description`` artifact.
* ``lotsa.push_step`` gains diff/commit-driven PR-text generation:
  ``parse_pr_description``, ``append_lotsa_trailer``, ``build_pr_text``,
  ``_build_fallback_title`` / ``_build_fallback_body``, and a rewritten
  ``_detect_commit_type`` that takes (changed_files, subjects).
* ``execute_push`` becomes mechanical — it takes ``title`` / ``body`` and does
  no heuristic synthesis.
* ``push_pr`` reads ``pr_description`` (only when ``pr_number is None``), parses
  it, appends the Lotsa trailer, and passes ``title`` / ``body`` to
  ``execute_push``; on a missing/unparsable artifact it falls back deterministically.

Imports of the not-yet-existing symbols live INSIDE each test so every test
fails independently for its own reason (ImportError / TypeError / AssertionError)
rather than the whole module failing to collect.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

_HTTPS_REMOTE = "https://github.com/acme/my-repo.git"
_FAKE_HEAD_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


# ===========================================================================
# push_step — parse_pr_description (the artifact serialization contract)
# ===========================================================================


def test_parse_pr_description_splits_title_and_body():
    from lotsa.push_step import parse_pr_description

    title, body = parse_pr_description("feat: add the widget\n\nDoes the thing.")
    assert title == "feat: add the widget"
    assert body == "Does the thing."


def test_parse_pr_description_strips_markdown_header_from_title():
    from lotsa.push_step import parse_pr_description

    title, _ = parse_pr_description("# feat: add the widget\n\nbody")
    assert title == "feat: add the widget"


def test_parse_pr_description_title_only_yields_empty_body():
    from lotsa.push_step import parse_pr_description

    title, body = parse_pr_description("fix: stop the crash")
    assert title == "fix: stop the crash"
    assert body == ""


def test_parse_pr_description_empty_returns_none():
    from lotsa.push_step import parse_pr_description

    assert parse_pr_description("") is None
    assert parse_pr_description("   \n  ") is None


def test_parse_pr_description_separator_first_line_returns_none():
    """A spec/diff beginning with ``---`` must never become a title (the bug)."""
    from lotsa.push_step import parse_pr_description

    assert parse_pr_description("---\n\nsome body") is None
    assert parse_pr_description("***") is None
    assert parse_pr_description("___") is None


def test_parse_pr_description_skips_agent_preamble_before_cc_title():
    """Agent narration ahead of the title must never become the PR headline.

    Task ``c94e3ed9``: the pr_summary agent opened its final message with
    "I have enough to write the PR description." and that line shipped as
    the PR title. The parser scans for the first Conventional Commits line.
    """
    from lotsa.push_step import parse_pr_description

    artifact = (
        "I have enough to write the PR description.\n"
        "\n"
        "feat(changes-tab): render PR-style git diff with syntax highlighting\n"
        "\n"
        "Replace the Changes tab's raw-text view with a unified diff.\n"
    )
    title, body = parse_pr_description(artifact)
    assert title == "feat(changes-tab): render PR-style git diff with syntax highlighting"
    assert body == "Replace the Changes tab's raw-text view with a unified diff."
    assert "I have enough" not in title
    assert "I have enough" not in body


def test_parse_pr_description_multiline_preamble_and_scoped_breaking_title():
    """Multi-line preamble is skipped; scope and ``!`` still match."""
    from lotsa.push_step import parse_pr_description

    artifact = "Here's the summary.\nLet me write it now.\n\nfix(api)!: drop the v1 endpoint\n\nbody text"
    title, body = parse_pr_description(artifact)
    assert title == "fix(api)!: drop the v1 endpoint"
    assert body == "body text"


def test_parse_pr_description_no_cc_line_keeps_first_line_as_title():
    """Free-form artifacts (no CC-shaped line anywhere) keep legacy behaviour."""
    from lotsa.push_step import parse_pr_description

    title, body = parse_pr_description("Add the widget\n\nDoes the thing.")
    assert title == "Add the widget"
    assert body == "Does the thing."


def test_parse_pr_description_cc_first_line_unchanged():
    """A clean artifact (title on line 1) parses exactly as before."""
    from lotsa.push_step import parse_pr_description

    title, body = parse_pr_description("docs: revise ADR-015\n\nThe body.")
    assert title == "docs: revise ADR-015"
    assert body == "The body."


def test_parse_pr_description_cc_title_inside_markdown_header():
    """A ``#``-prefixed CC title after preamble is still found and stripped."""
    from lotsa.push_step import parse_pr_description

    title, _ = parse_pr_description("Some narration first.\n\n## feat: add the widget\n\nbody")
    assert title == "feat: add the widget"


# ===========================================================================
# orchestrator — _strip_artifact_narration (capture-time source fix)
# ===========================================================================


def test_strip_artifact_narration_drops_preamble_before_heading():
    from lotsa.orchestrator import _strip_artifact_narration

    text = "I have everything I need. Here is the implementation plan.\n\n---\n\n# Implementation Plan\n\nSteps."
    out = _strip_artifact_narration(text)
    assert out.startswith("# Implementation Plan")
    assert "I have everything" not in out


def test_strip_artifact_narration_drops_preamble_before_cc_title():
    from lotsa.orchestrator import _strip_artifact_narration

    text = "I have enough to write the PR description.\n\nfeat(x): do the thing\n\nBody."
    out = _strip_artifact_narration(text)
    assert out.startswith("feat(x): do the thing")


def test_strip_artifact_narration_clean_content_unchanged():
    from lotsa.orchestrator import _strip_artifact_narration

    assert _strip_artifact_narration("# Spec: widget\n\nBody.") == "# Spec: widget\n\nBody."
    assert _strip_artifact_narration("fix: stop crash\n\nBody.") == "fix: stop crash\n\nBody."


def test_strip_artifact_narration_no_anchor_returns_text_unchanged():
    """Free-form artifacts (custom processes) pass through untouched."""
    from lotsa.orchestrator import _strip_artifact_narration

    text = "Research findings: the market is large.\nMore detail here."
    assert _strip_artifact_narration(text) == text


# ===========================================================================
# push_step — append_lotsa_trailer
# ===========================================================================


def test_append_lotsa_trailer_adds_trailer_with_task_and_flow():
    from lotsa.push_step import append_lotsa_trailer

    out = append_lotsa_trailer("The body.", "task-9", "full")
    assert "The body." in out
    assert "Generated by Lotsa" in out
    assert "task task-9" in out
    assert "flow full" in out


def test_append_lotsa_trailer_omits_flow_when_empty():
    from lotsa.push_step import append_lotsa_trailer

    out = append_lotsa_trailer("Body", "task-9", "")
    assert "Generated by Lotsa" in out
    assert "flow" not in out


def test_append_lotsa_trailer_added_once():
    from lotsa.push_step import append_lotsa_trailer

    out = append_lotsa_trailer("Body", "task-1", "full")
    assert out.count("Generated by Lotsa") == 1


# ===========================================================================
# push_step — diff-driven _detect_commit_type (new signature)
# ===========================================================================


def test_detect_commit_type_docs_only_changes():
    from lotsa.push_step import _detect_commit_type

    assert _detect_commit_type(["README.md"], []) == "docs"
    assert _detect_commit_type(["docs/adr/ADR-027-foo.md"], []) == "docs"


def test_detect_commit_type_tests_only_changes():
    from lotsa.push_step import _detect_commit_type

    assert _detect_commit_type(["lotsa/tests/test_thing.py"], []) == "test"


def test_detect_commit_type_code_falls_back_to_subject_keywords():
    from lotsa.push_step import _detect_commit_type

    assert _detect_commit_type(["lotsa/foo.py"], ["Fix the login bug"]) == "fix"
    assert _detect_commit_type(["lotsa/foo.py"], ["Add a caching layer"]) == "feat"


# ===========================================================================
# push_step — fallback title never yields ``feat: ---``
# ===========================================================================


def test_build_fallback_title_never_emits_feat_dashes():
    """Even with a ``---`` spec and no git signal, the title must be valid."""
    from lotsa.push_step import _build_fallback_title

    title = _build_fallback_title([], [], "---")
    assert title != "feat: ---"
    assert ":" in title
    # The description portion must not be the separator.
    assert title.split(":", 1)[1].strip() not in ("---", "***", "")


def test_build_fallback_title_prefers_commit_subject_without_double_prefix():
    from lotsa.push_step import _build_fallback_title

    title = _build_fallback_title(["lotsa/foo.py"], ["feat(x): add the widget"], "ignored spec line")
    assert "add the widget" in title
    # The existing conventional prefix on the subject must be stripped, not stacked.
    assert "feat(x):" not in title


# ===========================================================================
# push_step — build_pr_text (parse path vs deterministic fallback)
# ===========================================================================


async def test_build_pr_text_uses_parsed_description_without_touching_git(tmp_path):
    """A valid ``pr_description`` is used verbatim; git collectors are NOT read."""
    from lotsa.push_step import build_pr_text

    with (
        patch("lotsa.push_step._resolve_base_ref", new=AsyncMock(return_value="origin/main")) as base_mock,
        patch("lotsa.push_step._collect_changed_files", new=AsyncMock(return_value=[])) as files_mock,
        patch("lotsa.push_step._collect_commit_subjects", new=AsyncMock(return_value=[])) as subj_mock,
    ):
        title, body = await build_pr_text(
            work_dir=tmp_path,
            task_id="task-1",
            base_branch="main",
            flow_name="full",
            pr_description="docs: update readme\n\nClarify the setup steps.",
            spec="anything",
        )

    assert title == "docs: update readme"
    assert "Clarify the setup steps." in body
    assert "Generated by Lotsa" in body  # trailer appended by build_pr_text
    # Parse path short-circuits the diff collection.
    base_mock.assert_not_awaited()
    files_mock.assert_not_awaited()
    subj_mock.assert_not_awaited()


async def test_build_pr_text_falls_back_to_diff_when_description_missing(tmp_path):
    from lotsa.push_step import build_pr_text

    with (
        patch("lotsa.push_step._resolve_base_ref", new=AsyncMock(return_value="origin/main")),
        patch("lotsa.push_step._collect_changed_files", new=AsyncMock(return_value=["README.md"])),
        patch("lotsa.push_step._collect_commit_subjects", new=AsyncMock(return_value=["Document the API"])),
    ):
        title, body = await build_pr_text(
            work_dir=tmp_path,
            task_id="task-1",
            base_branch="main",
            flow_name="full",
            pr_description="",
            spec="Add a thing",
        )

    # Diff is docs-only → docs type, never feat: ---.
    assert title.startswith("docs:")
    assert title != "feat: ---"
    assert "Generated by Lotsa" in body


async def test_build_pr_text_fallback_guards_dashes_spec(tmp_path):
    from lotsa.push_step import build_pr_text

    with (
        patch("lotsa.push_step._resolve_base_ref", new=AsyncMock(return_value="origin/main")),
        patch("lotsa.push_step._collect_changed_files", new=AsyncMock(return_value=[])),
        patch("lotsa.push_step._collect_commit_subjects", new=AsyncMock(return_value=[])),
    ):
        title, _ = await build_pr_text(
            work_dir=tmp_path,
            task_id="task-1",
            base_branch="main",
            flow_name="full",
            pr_description="---",
            spec="---",
        )

    assert title != "feat: ---"
    assert ":" in title


# ===========================================================================
# push_step — execute_push is mechanical (title/body params, no synthesis)
# ===========================================================================


def _make_proc_mock(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


async def test_execute_push_passes_title_and_body_verbatim_to_create_pr(monkeypatch, tmp_path):
    """``execute_push`` accepts ``title``/``body`` and forwards them unchanged.

    Pre-fix, ``execute_push`` took ``spec_content``/``plan_content`` and
    synthesized title/body internally — calling it with ``title=``/``body=``
    raises ``TypeError`` (the contract does not yet exist).
    """
    from lotsa.push_step import execute_push

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    proc = _make_proc_mock(returncode=0)
    mock_client = AsyncMock()
    mock_client.create_pr = AsyncMock(return_value=42)
    mock_client.get_default_branch = AsyncMock(return_value="main")
    mock_client.close = AsyncMock()

    with (
        patch("lotsa.push_step._get_remote_url", new=AsyncMock(return_value=_HTTPS_REMOTE)),
        patch("lotsa.push_step._has_uncommitted_changes", new=AsyncMock(return_value=False)),
        patch("lotsa.push_step._get_head_sha", new=AsyncMock(return_value=_FAKE_HEAD_SHA)),
        patch("lotsa.push_step._normalize_worktree_branch", new=AsyncMock(return_value=None)),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.sh")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        pr_num, _url, _owner, _repo = await execute_push(
            work_dir=tmp_path,
            task_id="task-1",
            pr_number=None,
            base_branch="main",
            title="docs: update readme",
            body="A concise body.\n\n_Generated by Lotsa · task task-1 · flow full_",
        )

    assert pr_num == 42
    kwargs = mock_client.create_pr.call_args.kwargs
    assert kwargs["title"] == "docs: update readme"
    assert kwargs["body"] == "A concise body.\n\n_Generated by Lotsa · task task-1 · flow full_"


async def test_execute_push_does_not_call_create_pr_when_pr_exists(monkeypatch, tmp_path):
    """Re-push (pr_number set) opens no PR — title/body may be ``None``."""
    from lotsa.push_step import execute_push

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    proc = _make_proc_mock(returncode=0)
    mock_client = AsyncMock()
    mock_client.close = AsyncMock()

    with (
        patch("lotsa.push_step._get_remote_url", new=AsyncMock(return_value=_HTTPS_REMOTE)),
        patch("lotsa.push_step._has_uncommitted_changes", new=AsyncMock(return_value=False)),
        patch("lotsa.push_step._get_head_sha", new=AsyncMock(return_value=_FAKE_HEAD_SHA)),
        patch("lotsa.push_step._normalize_worktree_branch", new=AsyncMock(return_value=None)),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("os.fdopen"),
        patch("os.chmod"),
        patch("os.unlink"),
        patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-askpass.sh")),
        patch("lotsa.push_step.GitHubClient", return_value=mock_client),
    ):
        pr_num, _url, _owner, _repo = await execute_push(
            work_dir=tmp_path,
            task_id="task-1",
            pr_number=99,
            base_branch="main",
            title=None,
            body=None,
        )

    assert pr_num == 99
    mock_client.create_pr.assert_not_called()


# ===========================================================================
# push_pr tool — consumes pr_description, mechanical pass-through
# ===========================================================================


def _make_ctx(worktree: Path, *, metadata: dict | None = None, artifacts: dict | None = None):
    """Minimal TaskContext with an in-memory artifact-answering DB stub."""
    from dataclasses import dataclass, field

    from lotsa.tools import TaskContext

    @dataclass
    class _FakeDB:
        artifacts: dict = field(default_factory=dict)

        async def get_messages(self, task_id, msg_type=None, step_name=None):
            @dataclass
            class _MsgRow:
                content: str
                metadata: dict

            if msg_type == "artifact":
                return [_MsgRow(content=v, metadata={"artifact_name": k}) for k, v in self.artifacts.items()]
            return []

    return TaskContext(
        task_id="task-001",
        worktree=worktree,
        metadata=metadata or {},
        db=_FakeDB(artifacts=artifacts or {}),
        process_name="software_process",
        flow_name="full",
        current_flow="main",
        last_run_step="pr_summary",
    )


async def test_push_pr_passes_parsed_pr_description_as_title_and_body(tmp_path):
    """On PR creation, push_pr parses ``pr_description`` and forwards title/body."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(
        tmp_path,
        metadata={},
        artifacts={"pr_description": "docs: update readme\n\nClarify setup.", "spec": "irrelevant"},
    )

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (5, "https://github.com/o/r/pull/5", "o", "r")
        result = await push_pr(ctx, {})

    assert result.success is True
    kwargs = mock_push.call_args.kwargs
    assert kwargs.get("title") == "docs: update readme"
    assert "Clarify setup." in (kwargs.get("body") or "")
    assert "Generated by Lotsa" in (kwargs.get("body") or "")
    # The mechanical contract: no spec/plan synthesis args leak into execute_push.
    assert "spec_content" not in kwargs
    assert "plan_content" not in kwargs


async def test_push_pr_does_not_regenerate_text_when_pr_exists(tmp_path):
    """A re-push (pr_number set) keeps the existing PR — title/body are None."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(
        tmp_path,
        metadata={"pr_number": 7},
        artifacts={"pr_description": "feat: x\n\nbody"},
    )

    with patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = (7, "https://github.com/o/r/pull/7", "o", "r")
        await push_pr(ctx, {})

    kwargs = mock_push.call_args.kwargs
    assert kwargs.get("pr_number") == 7
    assert "title" in kwargs and kwargs["title"] is None
    assert "body" in kwargs and kwargs["body"] is None


async def test_push_pr_falls_back_when_pr_description_missing(tmp_path):
    """No ``pr_description`` → push_pr still supplies a valid title and pushes."""
    from lotsa.tools.push_pr import push_pr

    ctx = _make_ctx(tmp_path, metadata={}, artifacts={"spec": "Add a thing"})

    # Mock the git collectors (as the build_pr_text fallback test does) so the
    # fallback path is exercised deterministically without spawning real git
    # subprocesses against the non-repo tmp_path.
    with (
        patch("lotsa.push_step._resolve_base_ref", new=AsyncMock(return_value="origin/main")),
        patch("lotsa.push_step._collect_changed_files", new=AsyncMock(return_value=["lotsa/foo.py"])),
        patch("lotsa.push_step._collect_commit_subjects", new=AsyncMock(return_value=["Add a thing"])),
        patch("lotsa.tools.push_pr.execute_push", new_callable=AsyncMock) as mock_push,
    ):
        mock_push.return_value = (1, "https://github.com/o/r/pull/1", "o", "r")
        result = await push_pr(ctx, {})

    assert result.success is True
    kwargs = mock_push.call_args.kwargs
    title = kwargs.get("title")
    assert isinstance(title, str) and title.strip()
    assert ":" in title
    assert title != "feat: ---"
    assert "spec_content" not in kwargs


# ===========================================================================
# flows / process.yaml — the pr_summary step is wired into ``full``
# ===========================================================================


def test_full_process_has_pr_summary_agent_step():
    from lotsa.flows import build_process

    process = build_process("build")
    job = next((j for j in process.jobs if j.name == "pr_summary"), None)
    assert job is not None, "full preset must declare a pr_summary job"
    assert job.type == "agent"
    assert job.output == "pr_description"


def test_pr_summary_declares_no_required_inputs():
    """pr_summary must not declare ``inputs`` — a missing spec must not block it."""
    from lotsa.flows import build_process

    process = build_process("build")
    job = next(j for j in process.jobs if j.name == "pr_summary")
    assert job.inputs == []


def test_pr_summary_sits_between_verify_and_push_pr_in_main():
    from lotsa.flows import build_process

    process = build_process("build")
    names = [b.name for b in process.flows["main"].bindings]
    assert "pr_summary" in names
    assert names.index("verify") < names.index("pr_summary") < names.index("push_pr")


def test_verify_routes_to_pr_summary_and_pr_summary_routes_to_push_pr():
    from lotsa.flows import build_process

    main = build_process("build").flows["main"]
    by_name = {rj.name: rj for rj in main.jobs}
    assert by_name["verify"].success_state == by_name["pr_summary"].queue_state
    assert by_name["pr_summary"].success_state == by_name["push_pr"].queue_state


def test_pr_fix_flow_has_no_pr_summary_step():
    from lotsa.flows import build_process

    pr_fix = build_process("build").flows["pr_fix"]
    assert not any(b.name == "pr_summary" for b in pr_fix.bindings)


def test_pr_summary_prompt_files_exist():
    from lotsa.flows import BUNDLED_PROMPTS

    assert (BUNDLED_PROMPTS / "build" / "pr_summary-system.md").is_file()
    assert (BUNDLED_PROMPTS / "build" / "pr_summary-user.md").is_file()
