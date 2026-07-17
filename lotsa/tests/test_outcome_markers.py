"""RED tests for the generic outcome-marker vocabulary (ADR-044, Phase 1).

Phase 1 replaces every bespoke marker (``REVIEW_PASS/FAIL``, the four
``PR_FIX_*``, ``VERIFIED:``/``NEEDS_CODE:``/``NEEDS_REVIEW:``,
``CONFLICTS_RESOLVED:``, ``SPEC_COMPLETE:``) with one universal vocabulary::

    AGENT_RESULT: COMPLETED | PASSED | FAILED | SKIPPED | INPUT   [<payload>]

Routing semantics move from the marker *name* to the flow *edge*: the same
``AGENT_RESULT: FAILED`` routes differently on ``review`` vs ``pr-fix`` purely
by which step is active. ``NEEDS_INPUT:`` is retained as a recognised alias for
``AGENT_RESULT: INPUT``.

Contract these tests pin down (the coding step implements it in
``lotsa.orchestrator``):

* ``_extract_agent_outcome(stdout) -> (outcome, payload) | None`` — the LAST
  ``AGENT_RESULT`` marker; payload is the trailing same-line free text.
* ``_extract_needs_input(stdout)`` — extended to return the payload of the last
  ``AGENT_RESULT: INPUT`` **and** the ``NEEDS_INPUT:`` alias.
* ``_strip_agent_result_prefix(line)`` — strips a leading ``AGENT_RESULT:
  <OUTCOME>`` prefix (else returns the line unchanged), replacing
  ``_strip_pr_fix_marker_prefix``.

Plus: the bundled ``build``/``fix`` processes route on the generic vocabulary,
and no bundled ``process.yaml`` still carries a legacy marker literal.

Failures are RED because the new helpers don't exist yet and the bundled YAML
still uses the old markers. New symbols are imported inside each test.
"""

from __future__ import annotations

import re
from pathlib import Path

from rigg.models import AgentResult


def _result(stdout: str) -> AgentResult:
    return AgentResult(success=True, stdout=stdout, stderr="", return_code=0, duration_ms=1)


# ───────────────────────────────────────────────────────────────────────────
# _extract_agent_outcome — outcome + payload
# ───────────────────────────────────────────────────────────────────────────


def test_extract_bare_outcome_has_empty_payload():
    from lotsa.orchestrator import _extract_agent_outcome

    assert _extract_agent_outcome("AGENT_RESULT: COMPLETED") == ("COMPLETED", "")


def test_extract_outcome_with_trailing_payload():
    from lotsa.orchestrator import _extract_agent_outcome

    outcome, payload = _extract_agent_outcome("AGENT_RESULT: FAILED review found 3 correctness bugs")
    assert outcome == "FAILED"
    assert payload == "review found 3 correctness bugs"


def test_extract_tolerates_colon_after_outcome_word():
    from lotsa.orchestrator import _extract_agent_outcome

    outcome, payload = _extract_agent_outcome("AGENT_RESULT: INPUT: Which API version should I target?")
    assert outcome == "INPUT"
    assert payload == "Which API version should I target?"


def test_extract_returns_last_marker_when_several_present():
    from lotsa.orchestrator import _extract_agent_outcome

    stdout = "AGENT_RESULT: SKIPPED nothing actionable\ndoing more work\nAGENT_RESULT: COMPLETED done"
    assert _extract_agent_outcome(stdout) == ("COMPLETED", "done")


def test_extract_returns_none_without_a_marker():
    from lotsa.orchestrator import _extract_agent_outcome

    assert _extract_agent_outcome("just prose, no structured outcome here") is None


def test_extract_ignores_unknown_outcome_word():
    from lotsa.orchestrator import _extract_agent_outcome

    assert _extract_agent_outcome("AGENT_RESULT: MAYBE unsure") is None


# ───────────────────────────────────────────────────────────────────────────
# _extract_needs_input — INPUT outcome + NEEDS_INPUT alias
# ───────────────────────────────────────────────────────────────────────────


def test_needs_input_reads_the_input_outcome_payload():
    from lotsa.orchestrator import _extract_needs_input

    assert _extract_needs_input("AGENT_RESULT: INPUT Should I bump the major version?") == (
        "Should I bump the major version?"
    )


def test_needs_input_still_accepts_the_legacy_alias():
    from lotsa.orchestrator import _extract_needs_input

    assert _extract_needs_input("NEEDS_INPUT: legacy phrased question") == "legacy phrased question"


def test_needs_input_none_without_a_question():
    from lotsa.orchestrator import _extract_needs_input

    assert _extract_needs_input("AGENT_RESULT: COMPLETED all done") is None


# ───────────────────────────────────────────────────────────────────────────
# _strip_agent_result_prefix — generic prefix stripper for display/feedback
# ───────────────────────────────────────────────────────────────────────────


def test_strip_removes_the_outcome_prefix_leaving_payload():
    from lotsa.orchestrator import _strip_agent_result_prefix

    assert _strip_agent_result_prefix("AGENT_RESULT: COMPLETED addressed the lint comments") == (
        "addressed the lint comments"
    )


def test_strip_is_identity_when_no_marker_present():
    from lotsa.orchestrator import _strip_agent_result_prefix

    assert _strip_agent_result_prefix("a normal line of prose") == "a normal line of prose"


# ───────────────────────────────────────────────────────────────────────────
# Routing: the marker is agnostic; the active step's edges decide the target.
# Exercised through the real routing helper ``evaluate_output_rules``.
# ───────────────────────────────────────────────────────────────────────────


def test_generic_failed_routes_per_edge_not_per_marker(tmp_path):
    """The SAME ``AGENT_RESULT: FAILED`` output routes to different targets
    depending only on the edges of the step that emitted it."""
    from lotsa.flows import OutputRule, evaluate_output_rules

    review_edges = [OutputRule(source="stdout", pattern="^AGENT_RESULT: FAILED", target="code")]
    pr_fix_edges = [OutputRule(source="stdout", pattern="^AGENT_RESULT: FAILED", target="blocked")]
    result = _result("AGENT_RESULT: FAILED something is wrong")

    assert evaluate_output_rules(review_edges, result, tmp_path) == "code"
    assert evaluate_output_rules(pr_fix_edges, result, tmp_path) == "blocked"


def test_generic_marker_routes_through_markdown_wrappers(tmp_path):
    """A heading-wrapped marker still routes (reuses ``_match_marker``)."""
    from lotsa.flows import OutputRule, evaluate_output_rules

    edges = [OutputRule(source="stdout", pattern="^AGENT_RESULT: PASSED", target="next")]
    result = _result("## AGENT_RESULT: PASSED")
    assert evaluate_output_rules(edges, result, tmp_path) == "next"


# ───────────────────────────────────────────────────────────────────────────
# Bundled processes route on the generic vocabulary
# ───────────────────────────────────────────────────────────────────────────


def _has_edge(rules, outcome: str, target: str) -> bool:
    """True if some stdout rule with ``target`` matches ``AGENT_RESULT: <outcome>``.

    Matching by regex (not string equality) tolerates minor spacing variants in
    the migrated patterns while still failing on the legacy marker literals.
    """
    sample = f"AGENT_RESULT: {outcome}"
    return any(
        r.source == "stdout" and r.target == target and re.search(r.pattern, sample)
        for r in (rules or [])
    )


def test_build_main_review_routes_on_generic_pass_fail():
    from lotsa.flows import build_process

    main = build_process("build").flows["main"]
    rules = main.binding_for("review").rules
    assert _has_edge(rules, "PASSED", "next")
    assert _has_edge(rules, "FAILED", "code")


def test_build_verify_routes_on_generic_pass_fail():
    from lotsa.flows import build_process

    process = build_process("build")
    verify = {j.name: j for j in process.jobs}["verify"]
    assert _has_edge(verify.rules, "PASSED", "next")
    assert _has_edge(verify.rules, "FAILED", "code")


def test_build_pr_fix_routes_the_full_outcome_set():
    from lotsa.flows import build_process

    pr_fix_flow = build_process("build").flows["pr_fix"]
    rules = pr_fix_flow.binding_for("pr-fix").rules
    assert _has_edge(rules, "COMPLETED", "review")
    assert _has_edge(rules, "SKIPPED", "wait_for_pr_signal")
    assert _has_edge(rules, "FAILED", "blocked")
    assert _has_edge(rules, "INPUT", "needs_input")


def test_build_resolve_conflicts_completed_routes_to_pr_fix():
    from lotsa.flows import build_process

    pr_fix_flow = build_process("build").flows["pr_fix"]
    rules = pr_fix_flow.binding_for("resolve_conflicts").rules
    assert _has_edge(rules, "COMPLETED", "pr-fix")


def test_fix_main_review_routes_on_generic_pass_fail():
    from lotsa.flows import build_process

    main = build_process("fix").flows["main"]
    rules = main.binding_for("review").rules
    assert _has_edge(rules, "PASSED", "next")
    assert _has_edge(rules, "FAILED", "code")


# ───────────────────────────────────────────────────────────────────────────
# Sweep guard: no legacy marker literal survives in bundled process YAML
# ───────────────────────────────────────────────────────────────────────────

_LEGACY_MARKERS = (
    "REVIEW_PASS",
    "REVIEW_FAIL",
    "PR_FIX_DONE",
    "PR_FIX_SKIPPED",
    "PR_FIX_BLOCKED",
    "PR_FIX_NEEDS_DECISION",
    "VERIFIED:",
    "NEEDS_CODE:",
    "NEEDS_REVIEW:",
    "CONFLICTS_RESOLVED:",
    "SPEC_COMPLETE:",
)


def test_no_bundled_process_yaml_carries_a_legacy_marker():
    from lotsa.flows import BUNDLED_PROMPTS

    offenders: list[str] = []
    for yaml_path in BUNDLED_PROMPTS.rglob("process.yaml"):
        text = yaml_path.read_text()
        for marker in _LEGACY_MARKERS:
            if marker in text:
                offenders.append(f"{yaml_path.relative_to(BUNDLED_PROMPTS)}: {marker}")
    assert not offenders, "legacy markers still present in bundled YAML:\n" + "\n".join(offenders)


def test_no_bundled_agent_prompt_carries_a_legacy_marker():
    """Prompts must instruct the generic vocabulary once hoisted to the catalog."""
    from lotsa.agents import AGENTS_DIR

    offenders: list[str] = []
    for md_path in Path(AGENTS_DIR).rglob("*.md"):
        text = md_path.read_text()
        for marker in _LEGACY_MARKERS:
            if marker in text:
                offenders.append(f"{md_path.name}: {marker}")
    assert not offenders, "legacy markers still present in catalog prompts:\n" + "\n".join(offenders)
