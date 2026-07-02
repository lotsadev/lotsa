"""Failing tests for the two-phase Think→Execute catalog (ADR-043).

These specify the new three-process catalog (``chat``/``build``/``fix``) that
replaces the flat five-preset catalog (``simple``/``standard``/``full``/
``chat``/``quickfix``). They are written RED — before the implementation:

* ``build_process("build")`` / ``build_process("fix")`` raise ``ValueError``
  ("Unknown process") today, so every flow-shape test fails at ``build_process``.
* ``build_process("full")`` still succeeds today, so the "full is dissolved"
  test's ``pytest.raises`` does not fire and the test fails.
* The ``build/`` and ``fix/`` prompt directories don't exist yet, so the
  prompt-resolution and git-authority tests fail on the up-front directory
  assertions.
* ``PRESET_NAMES`` is still the old five-tuple.

Covers plan §1–§4 and acceptance criteria #1, #2, #3, #4, #5.
"""

from __future__ import annotations

import re

import pytest

from lotsa.flows import (
    BUNDLED_PROMPTS,
    PRESET_NAMES,
    _resolve_prompts_search_paths,
    build_process,
)

# ───────────────────────────────────────────────────────────────────────────
# Catalog membership (acceptance #1)
# ───────────────────────────────────────────────────────────────────────────


def test_preset_names_are_exactly_chat_build_fix():
    """``PRESET_NAMES`` is the three-process think→execute catalog.

    Fails pre-impl: it is still ``("simple","standard","full","chat","quickfix")``.
    """
    assert PRESET_NAMES == ("chat", "build", "fix")


@pytest.mark.parametrize("removed", ["simple", "standard", "full", "quickfix"])
def test_removed_presets_are_not_in_catalog(removed):
    assert removed not in PRESET_NAMES


@pytest.mark.parametrize("removed", ["simple", "standard", "full", "quickfix"])
def test_removed_preset_directories_are_gone(removed):
    """The deleted process directories (and their orphaned ``pr_summary-*.md``)
    must no longer exist on disk.

    Fails pre-impl: the directories are still present.
    """
    assert not (BUNDLED_PROMPTS / removed).exists(), (
        f"{removed}/ prompt directory must be deleted in the two-phase catalog"
    )


@pytest.mark.parametrize("removed", ["simple", "standard", "full", "quickfix"])
def test_removed_presets_no_longer_build(removed):
    """A deleted preset name is unknown to ``build_process``.

    Fails pre-impl: ``full``/``standard``/etc. still load, so no ValueError.
    """
    with pytest.raises(ValueError, match="Unknown process"):
        build_process(removed)


def test_no_orphaned_pr_summary_files_remain():
    """No ``pr_summary-*.md`` may survive under a removed preset dir.

    (build carries pr_summary legitimately; the orphans were the standalone
    copies under simple/ and standard/.)
    """
    for removed in ("simple", "standard"):
        d = BUNDLED_PROMPTS / removed
        assert not d.exists() or not list(d.glob("pr_summary-*.md")), (
            f"orphaned pr_summary prompt still present under {removed}/"
        )


# ───────────────────────────────────────────────────────────────────────────
# build process shape (plan §2, acceptance #3)
# ───────────────────────────────────────────────────────────────────────────


def test_build_process_loads_with_main_and_pr_fix_flows():
    process = build_process("build")
    assert "main" in process.flows
    assert "pr_fix" in process.flows


def test_build_main_flow_step_order():
    """build's main flow is plan→test→code→review→verify→pr_summary→push_pr→wait."""
    main = build_process("build").flows["main"]
    names = [b.name for b in main.bindings]
    assert names == [
        "plan",
        "test",
        "code",
        "review",
        "verify",
        "pr_summary",
        "push_pr",
        "wait_for_pr_signal",
    ]


def test_build_has_no_spec_step():
    """The spec step is dissolved into chat — build never speccs.

    Fails pre-impl: build doesn't exist; post-impl it must not carry ``spec``.
    """
    process = build_process("build")
    names = {j.name for j in process.jobs}
    assert "spec" not in names
    # And no ``speccing`` state leaks in via any flow's state machine.
    for flow in process.flows.values():
        assert "speccing" not in flow.state_machine.states


def test_build_plan_is_first_and_ungated():
    """plan moves inside build as an ungated first step (no gate_state: planned)."""
    main = build_process("build").flows["main"]
    by_name = {j.name: j for j in main.jobs}
    plan = by_name["plan"]
    # First step: agent job derives queue_state "backlog".
    assert main.bindings[0].name == "plan"
    assert plan.queue_state == "backlog"
    # Ungated: not an approval gate, and no "planned" gate state anywhere.
    assert plan.is_approval_gate is False, "plan must not pause for operator Accept"
    assert "planned" not in main.gate_states
    assert "planned" not in main.state_machine.states


def test_build_commit_posthooks_on_test_code_verify_prfix():
    """test/code/verify/pr-fix keep posthooks: [commit] (plan §2)."""
    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    for step in ("test", "code", "verify", "pr-fix"):
        assert by_name[step].posthooks == ["commit"], (
            f"{step} must run the commit posthook; got {by_name[step].posthooks!r}"
        )


def test_build_drops_all_spec_and_plan_inputs():
    """No build job may declare inputs: [spec] / [plan] — the task body/carried
    spec is the source of truth (plan §2)."""
    process = build_process("build")
    for j in process.jobs:
        assert not j.inputs, f"build job {j.name!r} must not gate on inputs; got {j.inputs!r}"


def test_build_review_routing_pass_next_fail_code():
    """main-flow review: PASS→next, FAIL→code (not blocked)."""
    main = build_process("build").flows["main"]
    binding = main.binding_for("review")
    targets = {(r.pattern, r.target) for r in (binding.rules or [])}
    assert ("^REVIEW_PASS", "next") in targets
    assert ("^REVIEW_FAIL", "code") in targets


def test_build_verify_routing():
    """verify: VERIFIED→next, NEEDS_CODE→code, NEEDS_REVIEW→review."""
    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    targets = {(r.pattern, r.target) for r in by_name["verify"].rules}
    assert ("^VERIFIED:", "next") in targets
    assert ("^NEEDS_CODE:", "code") in targets
    assert ("^NEEDS_REVIEW:", "review") in targets


def test_build_pr_fix_subflow_shape():
    """pr_fix sub-flow preserved: pr-fix → resolve_conflicts → review → push_pr."""
    pr_fix = build_process("build").flows["pr_fix"]
    assert [b.name for b in pr_fix.bindings] == [
        "pr-fix",
        "resolve_conflicts",
        "review",
        "push_pr",
    ]


def test_build_ends_in_push_and_pr_watch():
    """Execute ends in push_pr (action) → wait_for_pr_signal (monitor)."""
    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    assert by_name["push_pr"].type == "action"
    assert by_name["push_pr"].tool == "push_pr"
    assert by_name["wait_for_pr_signal"].type == "monitor"
    assert by_name["wait_for_pr_signal"].engine == "pr_monitor"


# ───────────────────────────────────────────────────────────────────────────
# fix process shape (plan §3, acceptance #4)
# ───────────────────────────────────────────────────────────────────────────


def test_fix_process_loads_with_main_and_pr_fix_flows():
    process = build_process("fix")
    assert "main" in process.flows
    assert "pr_fix" in process.flows


def test_fix_main_flow_step_order():
    """fix's main flow is code→review→push_pr→wait_for_pr_signal (it now pushes)."""
    main = build_process("fix").flows["main"]
    assert [b.name for b in main.bindings] == [
        "code",
        "review",
        "push_pr",
        "wait_for_pr_signal",
    ]


def test_fix_opens_a_pr_and_enters_pr_watch():
    """fix now pushes: a push_pr action + pr_monitor wait must be present.

    (Today quickfix commits but never opens a PR.)
    """
    process = build_process("fix")
    by_name = {j.name: j for j in process.jobs}
    assert by_name["push_pr"].type == "action"
    assert by_name["push_pr"].tool == "push_pr"
    assert by_name["wait_for_pr_signal"].type == "monitor"
    assert by_name["wait_for_pr_signal"].engine == "pr_monitor"


def test_fix_code_step_has_commit_posthook():
    process = build_process("fix")
    by_name = {j.name: j for j in process.jobs}
    assert by_name["code"].posthooks == ["commit"]


def test_fix_review_routing_pass_next_fail_code():
    main = build_process("fix").flows["main"]
    binding = main.binding_for("review")
    targets = {(r.pattern, r.target) for r in (binding.rules or [])}
    assert ("^REVIEW_PASS", "next") in targets
    assert ("^REVIEW_FAIL", "code") in targets


def test_fix_pr_fix_subflow_shape():
    pr_fix = build_process("fix").flows["pr_fix"]
    assert [b.name for b in pr_fix.bindings] == [
        "pr-fix",
        "resolve_conflicts",
        "review",
        "push_pr",
    ]


# ───────────────────────────────────────────────────────────────────────────
# Prompt resolution & fallback (plan §1/§4, acceptance #2)
# ───────────────────────────────────────────────────────────────────────────

_BUILD_PROMPT_STEMS = (
    "planning",
    "testing",
    "coding",
    "review",
    "verify",
    "pr-fix",
    "resolve_conflicts",
    "pr_summary",
)


def test_build_resolves_every_referenced_prompt():
    """Every prompt build references resolves (from its own dir) to non-empty text."""
    registry = build_process("build").registry
    for stem in _BUILD_PROMPT_STEMS:
        system = registry.load(f"{stem}-system")
        assert system.strip(), f"{stem}-system.md resolved empty for build"


def test_fix_coding_from_own_dir_generics_fall_back_to_build():
    """fix ships only its distinctive coding prompt; review/pr-fix/resolve_conflicts
    resolve via the build fallback (acceptance #2)."""
    registry = build_process("fix").registry
    # Own dir.
    assert registry.load("coding-system").strip()
    # Fallback to build/.
    for stem in ("review", "pr-fix", "resolve_conflicts"):
        assert registry.load(f"{stem}-system").strip(), f"fix must resolve {stem}-system via the build fallback"


def test_resolve_prompts_search_paths_fix_falls_back_to_build():
    """The fallback branch routes ``fix`` → its own dir + ``build`` (was
    ``quickfix`` → ``full``)."""
    paths = _resolve_prompts_search_paths("fix", None)
    assert (BUNDLED_PROMPTS / "fix") in paths
    assert (BUNDLED_PROMPTS / "build") in paths


def test_unknown_process_default_fallback_is_build_not_standard():
    """A non-preset flow name falls back to the ``build`` generic prompts,
    since ``standard`` is deleted."""
    paths = _resolve_prompts_search_paths("some_inline_process", None)
    assert (BUNDLED_PROMPTS / "build") in paths
    assert (BUNDLED_PROMPTS / "standard") not in paths


# ───────────────────────────────────────────────────────────────────────────
# ADR-013 clean: no surviving prompt instructs branch/commit/push (acceptance #5)
# ───────────────────────────────────────────────────────────────────────────

# Matches an *imperative* git-write command at line start (the shape of the
# deleted standard/coding-system.md violation: ``git checkout -b …``,
# ``git add <files>``, ``git commit -m "…"``). Deliberately anchored to line
# start so the legitimate *negative* prose in resolve_conflicts/verify/pr-fix
# ("Do not run `git merge`, `git rebase`, `git commit`, `git push`") — where the
# commands sit mid-sentence inside backticks — does not false-match.
_GIT_WRITE_IMPERATIVE = re.compile(
    r"^\s*git\s+(checkout\s+-b|add|commit|push|branch|rebase|merge)\b",
    re.MULTILINE,
)


@pytest.mark.parametrize("process_dir", ["build", "fix", "chat"])
def test_no_surviving_prompt_instructs_git_writes(process_dir):
    """No prompt in any surviving process may tell the agent to branch/commit/push.

    Fails pre-impl for build/fix: the directories don't exist yet, so the
    up-front existence assertion fires. Guards against re-introducing the
    ADR-013 violation that lived in standard/coding-system.md.
    """
    d = BUNDLED_PROMPTS / process_dir
    assert d.is_dir(), f"{process_dir}/ prompt directory must exist"
    for md in d.glob("*.md"):
        text = md.read_text()
        hits = _GIT_WRITE_IMPERATIVE.findall(text)
        assert not hits, f"{md} instructs a git write operation (ADR-013 violation): {hits}"
