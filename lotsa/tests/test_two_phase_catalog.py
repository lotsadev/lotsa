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


def test_build_commit_posthooks_derived_on_producing_steps():
    """test/code/pr-fix resolve to posthooks: [commit] — now DERIVED from each
    agent's ``produces_changes: true`` property (ADR-044 Phase 2), not from a
    hand-declared ``posthooks: [commit]`` in the YAML.

    Passes both pre- and post-Phase-2 for these three (they carried an explicit
    commit before, derive it after); the behaviour change is on ``verify``,
    pinned in ``test_build_verify_no_longer_commits`` below.
    """
    import lotsa.posthooks  # noqa: F401 — ensures the built-in ``commit`` exists

    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    for step in ("test", "code", "pr-fix"):
        assert by_name[step].posthooks == ["commit"], (
            f"{step} must run the commit posthook; got {by_name[step].posthooks!r}"
        )


def test_build_verify_no_longer_commits():
    """``verify`` is a gate (``produces_changes: false``): it observes and, on
    ``FAILED``, routes to ``code`` which commits. Under ADR-044 Phase 2 the
    contradictory ``posthooks: [commit]`` comes off it, so it resolves to no
    posthooks.

    RED pre-Phase-2: build's ``verify`` still declares ``posthooks: [commit]``,
    so it resolves to ``["commit"]``.
    """
    import lotsa.posthooks  # noqa: F401

    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    assert by_name["verify"].posthooks == [], (
        f"verify must not run the commit posthook (it observes, does not write); got {by_name['verify'].posthooks!r}"
    )


def test_bundled_effective_posthooks_preserved_except_verify():
    """Behaviour-preservation pin for the derive-and-drop migration (plan §Tests).

    Every bundled ``build``/``fix`` step resolves to the SAME effective posthook
    set as before Phase 2 — EXCEPT ``build``'s ``verify``, which loses commit.
    Encodes the whole migration in one place: producing agents derive
    ``[commit]``; non-producers / gates / action / monitor steps derive nothing.

    RED pre-Phase-2: ``build``'s ``verify`` resolves to ``["commit"]``, not ``[]``.
    """
    import lotsa.posthooks  # noqa: F401

    expected_build = {
        "plan": [],
        "test": ["commit"],
        "code": ["commit"],
        "review": [],
        "pr-fix": ["commit"],
        "verify": [],  # ← the one change
        "pr_summary": [],
        "push_pr": [],  # action
        "resolve_conflicts": ["commit"],
        "wait_for_pr_signal": [],  # monitor
    }
    expected_fix = {
        "code": ["commit"],
        "review": [],
        "pr-fix": ["commit"],
        "pr_summary": [],
        "push_pr": [],  # action
        "resolve_conflicts": ["commit"],
        "wait_for_pr_signal": [],  # monitor
    }

    build_jobs = {j.name: j for j in build_process("build").jobs}
    for name, expected in expected_build.items():
        assert build_jobs[name].posthooks == expected, (
            f"build/{name}: expected posthooks {expected!r}, got {build_jobs[name].posthooks!r}"
        )

    fix_jobs = {j.name: j for j in build_process("fix").jobs}
    for name, expected in expected_fix.items():
        assert fix_jobs[name].posthooks == expected, (
            f"fix/{name}: expected posthooks {expected!r}, got {fix_jobs[name].posthooks!r}"
        )


# ───────────────────────────────────────────────────────────────────────────
# ADR-044 Phase 3 — the ``worktree`` prehook is DERIVED from each agent's
# ``needs_worktree`` property (opt-OUT: worktree is the universal default; only
# a ``needs_worktree: false`` agent opts out).
# ───────────────────────────────────────────────────────────────────────────


def test_bundled_effective_prehooks_worktree_everywhere_except_monitor():
    """Every dispatched ``build``/``fix`` step (agent + action) derives the
    ``worktree`` prehook; monitor steps (which never created a worktree at
    dispatch) derive none. No bundled agent except ``chat`` sets
    ``needs_worktree: false``, and ``chat`` isn't in these two processes.

    RED pre-Phase-3: ``ResolvedJob`` has no ``prehooks`` attribute, so every
    access raises ``AttributeError``.
    """
    import lotsa.prehooks  # noqa: F401 — the built-in ``worktree`` must be registered

    expected_build = {
        "plan": ["worktree"],
        "test": ["worktree"],
        "code": ["worktree"],
        "review": ["worktree"],
        "pr-fix": ["worktree"],
        "verify": ["worktree"],
        "pr_summary": ["worktree"],
        "push_pr": ["worktree"],  # action — always got a worktree pre-Phase-3
        "resolve_conflicts": ["worktree"],
        "wait_for_pr_signal": [],  # monitor — never created one
    }
    expected_fix = {
        "code": ["worktree"],
        "review": ["worktree"],
        "pr-fix": ["worktree"],
        "pr_summary": ["worktree"],
        "push_pr": ["worktree"],  # action
        "resolve_conflicts": ["worktree"],
        "wait_for_pr_signal": [],  # monitor
    }

    build_jobs = {j.name: j for j in build_process("build").jobs}
    for name, expected in expected_build.items():
        assert build_jobs[name].prehooks == expected, (
            f"build/{name}: expected prehooks {expected!r}, got {build_jobs[name].prehooks!r}"
        )

    fix_jobs = {j.name: j for j in build_process("fix").jobs}
    for name, expected in expected_fix.items():
        assert fix_jobs[name].prehooks == expected, (
            f"fix/{name}: expected prehooks {expected!r}, got {fix_jobs[name].prehooks!r}"
        )


def test_chat_process_derives_no_worktree_prehook():
    """The ``chat`` process's sole step opts out of the worktree prehook — chat
    tasks stop creating a worktree they never use (ADR-044 Phase 3's payoff).

    RED pre-Phase-3: no ``prehooks`` attribute; worktree creation is
    unconditional for every step including chat.
    """
    process = build_process("chat")
    for j in process.jobs:
        assert j.prehooks == [], f"chat job {j.name!r} must derive no worktree prehook; got {j.prehooks!r}"


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
    assert ("^AGENT_RESULT: PASSED", "next") in targets
    assert ("^AGENT_RESULT: FAILED", "code") in targets


def test_build_verify_routing():
    """verify is a gate (ADR-044): PASSED→next, FAILED→code (the old two-way
    NEEDS_CODE/NEEDS_REVIEW failure collapses to FAILED→code)."""
    process = build_process("build")
    by_name = {j.name: j for j in process.jobs}
    targets = {(r.pattern, r.target) for r in by_name["verify"].rules}
    assert ("^AGENT_RESULT: PASSED", "next") in targets
    assert ("^AGENT_RESULT: FAILED", "code") in targets


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
    """fix's main flow is code→review→pr_summary→push_pr→wait_for_pr_signal.

    fix opens a human-facing PR, so it summarizes (pr_summary → pr_description
    artifact) before push — without it, the PR title falls back to the raw
    first-prompt commit subject. pr_summary lives in ``main`` only (not the
    ``pr_fix`` sub-flow, which must not regenerate PR text on a re-push).
    """
    main = build_process("fix").flows["main"]
    assert [b.name for b in main.bindings] == [
        "code",
        "review",
        "pr_summary",
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
    assert ("^AGENT_RESULT: PASSED", "next") in targets
    assert ("^AGENT_RESULT: FAILED", "code") in targets


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
    "pr_fix",
    "resolve_conflicts",
    "pr_summary",
)


def test_build_resolves_every_referenced_prompt():
    """Every prompt build references resolves (from its own dir) to non-empty text."""
    registry = build_process("build").registry
    for stem in _BUILD_PROMPT_STEMS:
        system = registry.load(f"{stem}-system")
        assert system.strip(), f"{stem}-system.md resolved empty for build"


def test_fix_resolves_its_distinct_coder_and_shared_agents_from_catalog():
    """ADR-044: fix references its distinctive ``fix_coding`` agent and shares
    ``review``/``pr_fix``/``resolve_conflicts`` with build — all resolved from the
    single agent catalog (the old fix→build prompt fallback is gone)."""
    registry = build_process("fix").registry
    # fix's distinctive coder.
    assert registry.load("fix_coding-system").strip()
    # Shared catalog agents.
    for stem in ("review", "pr_fix", "resolve_conflicts"):
        assert registry.load(f"{stem}-system").strip(), f"fix must resolve {stem}-system from the catalog"


# NOTE (ADR-044): prompt resolution moved to the shared agent catalog via
# ``AgentPromptRegistry``; the former ``_resolve_prompts_search_paths`` per-process
# fallback (and its two tests) were removed with the function. Catalog resolution
# is covered by ``test_fix_resolves_its_distinct_coder_and_shared_agents_from_catalog``
# and ``test_agent_catalog.py``.


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
