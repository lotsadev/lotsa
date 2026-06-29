"""Tests for flow loading — state machines, steps, and prompt loading.

Rewritten for ADR-014 Layer A: ``PrConfig``, the ``pr:`` block, and the
``target: previous`` shorthand are removed. The full process file is now
``process.yaml`` with typed jobs (``agent`` / ``action`` / ``monitor``)
and a ``flows:`` block. This file covers the substrate behaviors that
survived the refactor.
"""

from __future__ import annotations

import pytest
import yaml

from lotsa.flows import (
    FlowStep,
    OutputRule,
    ResolvedJob,
    build_dispatch_rules,
    build_flow,
    build_process,
    evaluate_output_rules,
    find_step,
    find_step_for_gate,
    next_dispatchable_state,
    resolve_output_target,
)

# ---------------------------------------------------------------------------
# Basic loading
# ---------------------------------------------------------------------------


def test_simple_process_loads():
    """The simple preset loads and exposes a single-step main flow."""
    process = build_process("simple")
    assert process.name == "simple"
    main = process.flows["main"]
    assert len(main.bindings) == 1
    assert main.bindings[0].name == "coding"


def test_standard_process_loads():
    process = build_process("standard")
    assert process.name == "standard"
    main = process.flows["main"]
    assert len(main.bindings) == 1


def test_full_process_loads_with_two_flows():
    process = build_process("full")
    assert "main" in process.flows
    assert "pr_fix" in process.flows


def test_unknown_process_raises():
    with pytest.raises(ValueError, match="Unknown process"):
        build_process("nonexistent")


def test_build_flow_returns_main_flow_for_backward_compat():
    """``build_flow`` (legacy entry point) returns the root flow as a FlowConfig."""
    flow = build_flow("full")
    assert flow.name == "main"


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def test_state_derivation_simple():
    """Simple flow derives: backlog → coding → complete."""
    process = build_process("simple")
    main = process.flows["main"]
    step = main.jobs[0]
    assert step.queue_state == "backlog"
    assert step.active_state == "coding"
    assert step.success_state == "complete"


def test_state_derivation_full_main():
    process = build_process("full")
    main = process.flows["main"]
    by_name = {rj.name: rj for rj in main.jobs}

    # spec is conversational with a distinct queue_state
    assert by_name["spec"].queue_state == "speccing"
    assert by_name["spec"].active_state == "spec"
    # plan has an evaluate gate
    assert by_name["plan"].success_state == "planned"
    # The last step in main is wait_for_pr_signal (monitor)
    assert main.bindings[-1].name == "wait_for_pr_signal"
    assert by_name["wait_for_pr_signal"].type == "monitor"


def test_gate_states():
    process = build_process("full")
    main = process.flows["main"]
    assert "planned" in main.gate_states


def test_revision_self_loop_on_active_states():
    """Active states have self-loops for revision dispatch."""
    process = build_process("simple")
    main = process.flows["main"]
    assert ("coding", "coding") in main.state_machine.transitions


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def test_prompt_loading_standard():
    process = build_process("standard")
    system = process.registry.load("coding-system")
    assert "Coding Agent" in system or len(system) > 50


def test_prompt_loading_full():
    process = build_process("full")
    for name in ["planning-system", "testing-system", "coding-system", "review-system", "verify-system"]:
        content = process.registry.load(name)
        assert len(content) > 50


def test_user_override_prompts(tmp_path):
    """User prompts_dir overrides bundled defaults."""
    custom = tmp_path / "prompts"
    custom.mkdir()
    (custom / "coding-system.md").write_text("Custom system prompt.")
    (custom / "coding-user.md").write_text("Custom: {title}\n{body}")

    process = build_process("standard", prompts_dir=custom)
    assert process.registry.load("coding-system") == "Custom system prompt."


# ---------------------------------------------------------------------------
# Custom YAML loading
# ---------------------------------------------------------------------------


def test_yaml_process_loading(tmp_path):
    process_file = tmp_path / "custom.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "custom",
                "jobs": [
                    {"name": "analyze", "type": "agent", "prompt": "analyze", "evaluate": True},
                    {"name": "implement", "type": "agent", "prompt": "implement"},
                ],
                "flows": {"main": {"steps": ["analyze", "implement"]}},
            }
        )
    )

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ["analyze-system", "analyze-user", "implement-system", "implement-user"]:
        (prompts / f"{name}.md").write_text(f"Prompt: {name}\n{{title}}\n{{body}}")

    process = build_process("custom", prompts_dir=prompts, process_file=process_file)
    main = process.flows["main"]
    assert main.jobs[0].name == "analyze"
    assert main.jobs[0].success_state == "analyzed"
    assert "analyzed" in main.gate_states


def test_yaml_process_with_rules(tmp_path):
    process_file = tmp_path / "rules.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "ruled",
                "jobs": [
                    {
                        "name": "test",
                        "type": "agent",
                        "prompt": "test",
                        "rules": [
                            {"source": "stdout", "pattern": "FAILED", "target": "blocked"},
                            {"source": ".lotsa/test.md", "pattern": "passed", "target": "next"},
                        ],
                    }
                ],
                "flows": {"main": {"steps": ["test"]}},
            }
        )
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ["test-system", "test-user"]:
        (prompts / f"{name}.md").write_text(f"Prompt: {name}\n{{title}}\n{{body}}")

    process = build_process("ruled", prompts_dir=prompts, process_file=process_file)
    main = process.flows["main"]
    assert len(main.jobs[0].rules) == 2
    assert main.jobs[0].rules[0].source == "stdout"
    assert main.jobs[0].rules[0].target == "blocked"


# ---------------------------------------------------------------------------
# Cross-process target rejection at parse time (ADR-021 R6 / AC-7)
# ---------------------------------------------------------------------------


def test_build_process_rejects_rule_target_outside_process(tmp_path):
    """A rule whose ``target`` names a job not in this process fails at
    ``build_process`` time — cross-process dispatch is unsupported.

    Fails pre-fix: ``build_process`` succeeds with the dangling target
    (``resolve_output_target`` only warns at evaluation time, and the
    state-machine builders silently skip unknown targets at build time).
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: alpha\n"
        "jobs:\n"
        "  - name: review\n"
        "    type: agent\n"
        "    rules:\n"
        "      - { source: stdout, pattern: NOPE, target: job_in_another_process }\n"
        "  - { name: code, type: agent }\n"
        "flows:\n"
        "  main: { steps: [review, code] }\n"
    )
    with pytest.raises(ValueError) as exc_info:
        build_process("alpha", process_file=process_file)
    message = str(exc_info.value)
    # The error names the offending target and the rule's owning job.
    assert "job_in_another_process" in message, (
        f"Cross-process target error must name the offending target; got {message!r}"
    )
    assert "review" in message, f"Cross-process target error must name the rule's owning job; got {message!r}"


def test_build_process_accepts_within_process_rule_target(tmp_path):
    """A rule target that resolves to a job within the same process still
    loads (positive control for the cross-process validator)."""
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: beta\n"
        "jobs:\n"
        "  - name: review\n"
        "    type: agent\n"
        "    rules:\n"
        "      - { source: stdout, pattern: FAIL, target: code }\n"
        "  - { name: code, type: agent }\n"
        "flows:\n"
        "  main: { steps: [review, code] }\n"
    )
    process = build_process("beta", process_file=process_file)
    assert "main" in process.flows
    assert [j.name for j in process.flows["main"].jobs] == ["review", "code"]


def test_build_process_rejects_subflow_binding_override_target_outside_process(tmp_path):
    """A *per-flow binding override* rule whose ``target`` names a job not in
    this process fails at ``build_process`` time.

    Sub-flow routing (e.g. ``pr_fix.review.REVIEW_FAIL → pr-fix``) lives in the
    binding-level ``rules:`` override, not the job's default ``rules:`` — so a
    validator that only checked ``Job.rules`` would let a cross-process target
    in a sub-flow override slip straight through to the runtime ``blocked``
    fallback. This is precisely the "sub-flow rule" surface ADR-021 R6 names.

    Fails pre-fix: ``build_process`` succeeds with the dangling override target
    (the original ``_validate_rule_targets`` iterated only the job defaults,
    never the per-flow binding overrides — verified by running against the
    installed pre-fix package, which returns a Process with ``main`` + ``sub``
    flows and no error).
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: gamma\n"
        "jobs:\n"
        "  - { name: review, type: agent }\n"
        "  - { name: code, type: agent }\n"
        "flows:\n"
        "  main: { steps: [review, code] }\n"
        "  sub:\n"
        "    steps:\n"
        "      - name: review\n"
        "        rules:\n"
        "          - { source: stdout, pattern: FAIL, target: job_in_another_process }\n"
    )
    with pytest.raises(ValueError) as exc_info:
        build_process("gamma", process_file=process_file)
    message = str(exc_info.value)
    assert "job_in_another_process" in message, f"Override-target error must name the offending target; got {message!r}"
    # The error names the offending flow so the operator knows which sub-flow.
    assert "sub" in message, f"Override-target error must name the owning flow; got {message!r}"


@pytest.mark.parametrize("preset", ["simple", "standard", "full"])
def test_bundled_presets_pass_cross_process_validator(preset):
    """Every bundled preset's rule targets resolve within their own process —
    the new validator must not reject them (guards against false positives,
    e.g. the ``full`` process's pr_fix sub-flow routing)."""
    process = build_process(preset)
    assert "main" in process.flows


# ---------------------------------------------------------------------------
# ADR-027 — process catalog ``description`` / ``promotion_inputs`` (PR 1 R4)
# ---------------------------------------------------------------------------


def _write_min_prompts(prompts_dir, names):
    prompts_dir.mkdir(exist_ok=True)
    for name in names:
        for kind in ("system", "user"):
            (prompts_dir / f"{name}-{kind}.md").write_text(f"Prompt: {name}\n{{title}}\n{{body}}")


def test_process_description_parses(tmp_path):
    """A root-level ``description:`` is parsed onto the ``Process`` (ADR-027 §3).

    Fails pre-fix: ``Process`` has no ``description`` attribute (AttributeError)."""
    process_file = tmp_path / "described.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "described",
                "description": "Exploration and triage process.",
                "jobs": [{"name": "code", "type": "agent", "prompt": "code"}],
                "flows": {"main": {"steps": ["code"]}},
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])
    process = build_process("described", prompts_dir=tmp_path / "prompts", process_file=process_file)
    assert process.description == "Exploration and triage process."


def test_process_promotion_inputs_parse(tmp_path):
    """Root-level ``promotion_inputs:`` parse into typed ``PromotionInput``s
    (ADR-027 §4). Fails pre-fix: ``PromotionInput`` is not importable and
    ``Process`` has no ``promotion_inputs`` attribute."""
    from lotsa.flows import PromotionInput

    process_file = tmp_path / "withinputs.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "withinputs",
                "promotion_inputs": [
                    {"name": "draft_spec", "description": "A discussed-and-agreed spec to verify."},
                ],
                "jobs": [{"name": "spec", "type": "agent", "prompt": "spec"}],
                "flows": {"main": {"steps": ["spec"]}},
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["spec"])
    process = build_process("withinputs", prompts_dir=tmp_path / "prompts", process_file=process_file)
    assert process.promotion_inputs == [
        PromotionInput(name="draft_spec", description="A discussed-and-agreed spec to verify.")
    ]


def test_process_without_catalog_fields_still_loads(tmp_path):
    """Existing processes that omit ``description``/``promotion_inputs`` load
    unchanged with empty defaults (additive — ADR-027 Migration)."""
    process_file = tmp_path / "plain.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "plain",
                "jobs": [{"name": "code", "type": "agent", "prompt": "code"}],
                "flows": {"main": {"steps": ["code"]}},
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])
    process = build_process("plain", prompts_dir=tmp_path / "prompts", process_file=process_file)
    assert process.description is None
    assert process.promotion_inputs == []


# ---------------------------------------------------------------------------
# ADR-027 — bundled ``chat`` and ``quickfix`` processes (PR 2 R8/R9)
# ---------------------------------------------------------------------------


def test_chat_process_loads_as_single_conversational_step():
    """The bundled ``chat`` process is one conversational REPL step with no
    completion marker (ADR-027 §3 / R8).

    Fails pre-fix: ``chat`` is not a bundled preset (ValueError 'Unknown
    process')."""
    process = build_process("chat")
    main = process.flows["main"]
    assert len(main.bindings) == 1
    step = main.steps[0]
    assert step.conversational is True
    # A REPL step has no auto-completion output rule — it runs until promoted.
    assert step.rules == []


def test_chat_process_has_description():
    """The chat process ships its own description so triage can surface it."""
    process = build_process("chat")
    assert process.description is not None
    assert process.description.strip() != ""


def test_quickfix_process_is_code_then_review():
    """The bundled ``quickfix`` process starts ``code → review`` (ADR-027 §3 /
    R9). Fails pre-fix: ``quickfix`` is not a bundled preset (ValueError)."""
    process = build_process("quickfix")
    main = process.flows["main"]
    step_names = [s.name for s in main.steps]
    assert step_names[:2] == ["code", "review"]


def test_quickfix_process_has_description_and_promotion_inputs():
    """Quickfix carries a description (for triage) and declares its
    promotion input (the operator's instruction)."""
    process = build_process("quickfix")
    assert process.description is not None
    assert [pi.name for pi in process.promotion_inputs] == ["instruction"]


# ---------------------------------------------------------------------------
# Backward compat aliases
# ---------------------------------------------------------------------------


def test_backward_compat_flow_step_alias():
    assert FlowStep is ResolvedJob


def test_flow_config_steps_alias_returns_resolved_jobs_in_binding_order():
    process = build_process("full")
    main = process.flows["main"]
    assert [s.name for s in main.steps] == [b.name for b in main.bindings]


# ---------------------------------------------------------------------------
# Output rule evaluation
# ---------------------------------------------------------------------------


def test_evaluate_output_rules_stdout_match(tmp_path):
    from rigg.models import AgentResult

    rules = [
        OutputRule(source="stdout", pattern="FAILED", target="blocked"),
        OutputRule(source="stdout", pattern="passed", target="next"),
    ]
    result = AgentResult(success=True, stdout="All tests passed", stderr="", return_code=0, duration_ms=100)
    assert evaluate_output_rules(rules, result, tmp_path) == "next"


def test_evaluate_output_rules_first_match_wins(tmp_path):
    from rigg.models import AgentResult

    rules = [
        OutputRule(source="stdout", pattern="error", target="blocked"),
        OutputRule(source="stdout", pattern=".*", target="next"),
    ]
    result = AgentResult(success=True, stdout="found error in output", stderr="", return_code=0, duration_ms=100)
    assert evaluate_output_rules(rules, result, tmp_path) == "blocked"


def test_evaluate_output_rules_file_match(tmp_path):
    from rigg.models import AgentResult

    (tmp_path / ".lotsa").mkdir()
    (tmp_path / ".lotsa" / "plan.md").write_text("## Plan\nDo the thing.")
    rules = [OutputRule(source=".lotsa/plan.md", pattern="## Plan", target="next")]
    result = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=100)
    assert evaluate_output_rules(rules, result, tmp_path) == "next"


def test_evaluate_output_rules_no_match(tmp_path):
    from rigg.models import AgentResult

    rules = [OutputRule(source="stdout", pattern="SPECIFIC_PATTERN", target="blocked")]
    result = AgentResult(success=True, stdout="nothing here", stderr="", return_code=0, duration_ms=100)
    assert evaluate_output_rules(rules, result, tmp_path) is None


def test_evaluate_output_rules_marker_in_backticks_matches(tmp_path):
    """A line-anchored marker wrapped in inline code still routes.

    Task ``a2c62ca2`` stranded at spec because the agent emitted
    ```` `SPEC_COMPLETE:` … ```` and ``^SPEC_COMPLETE:`` never matched.
    """
    from rigg.models import AgentResult

    rules = [OutputRule(source="stdout", pattern="^REVIEW_PASS", target="next")]
    result = AgentResult(
        success=True, stdout="All good.\n`REVIEW_PASS` clean run", stderr="", return_code=0, duration_ms=100
    )
    assert evaluate_output_rules(rules, result, tmp_path) == "next"


def test_evaluate_output_rules_marker_in_bold_matches(tmp_path):
    from rigg.models import AgentResult

    rules = [OutputRule(source="stdout", pattern="^REVIEW_FAIL", target="blocked")]
    result = AgentResult(
        success=True, stdout="**REVIEW_FAIL** two issues found", stderr="", return_code=0, duration_ms=100
    )
    assert evaluate_output_rules(rules, result, tmp_path) == "blocked"


def test_evaluate_output_rules_bullet_quoting_marker_does_not_match(tmp_path):
    """A bullet *mentioning* a marker mid-document must not route — the
    wrapper-strip is deliberately tight (no whitespace between wrapper
    and text), so "* `SPEC_COMPLETE:` is emitted when…" stays prose.
    """
    from rigg.models import AgentResult

    rules = [OutputRule(source="stdout", pattern="^SPEC_COMPLETE:", target="next")]
    result = AgentResult(
        success=True,
        stdout="Plan:\n* `SPEC_COMPLETE:` is emitted when the spec is done\nStill working.",
        stderr="",
        return_code=0,
        duration_ms=100,
    )
    assert evaluate_output_rules(rules, result, tmp_path) is None


def test_check_conversational_rules_backtick_marker_returns_from_marker_line():
    """The conversational matcher returns content from the marker line on,
    even when the marker is backtick-wrapped (a2c62ca2 regression)."""
    from lotsa.flows import check_conversational_rules

    step = ResolvedJob(
        name="spec",
        prompt_name="spec",
        resume_session=False,
        evaluate=False,
        conversational=True,
        rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:", target="next")],
        queue_state="speccing",
        active_state="spec",
        success_state="planned",
    )
    stdout = "Great, writing the spec now.\n`SPEC_COMPLETE:` widget support\n\n# Spec\n\nBody."
    captured = check_conversational_rules(step, stdout)
    assert captured is not None
    assert captured.startswith("`SPEC_COMPLETE:`")
    assert "# Spec" in captured


def test_check_conversational_rules_heading_marker_returns_from_marker_line():
    """A marker written as a Markdown heading (`## SPEC_COMPLETE:`) routes —
    an internal task: the spec agent emitted `## SPEC_COMPLETE: …` and the
    `^SPEC_COMPLETE:` rule missed because the heading prefix wasn't stripped.
    """
    from lotsa.flows import check_conversational_rules

    step = ResolvedJob(
        name="spec",
        prompt_name="spec",
        resume_session=False,
        evaluate=False,
        conversational=True,
        rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:", target="next")],
        queue_state="speccing",
        active_state="spec",
        success_state="planned",
    )
    stdout = "Now the plan.\n\n## SPEC_COMPLETE: multi-project support\n\nBody."
    captured = check_conversational_rules(step, stdout)
    assert captured is not None
    assert captured.startswith("## SPEC_COMPLETE:")
    assert "Body." in captured


def test_every_bundled_marker_survives_markdown_wrapping():
    """Regression sweep: EVERY stdout rule in EVERY bundled process must
    route when its marker is emitted plain, backtick-wrapped, or
    bold-wrapped.

    This is the future-proof counterpart of the targeted tests above —
    a marker added to any bundled process.yaml is covered automatically,
    nobody has to remember to write its wrapping test. (Task a2c62ca2:
    the agent emitted `` `SPEC_COMPLETE:` `` and the spec was never
    captured.)
    """
    from pathlib import Path

    from rigg.models import AgentResult

    bundled = ("simple", "standard", "full", "chat", "quickfix")
    swept = 0
    for process_name in bundled:
        process = build_process(process_name)
        for flow in process.flows.values():
            for job in flow.jobs:
                for rule in job.rules:
                    if rule.source != "stdout":
                        continue
                    pattern = rule.pattern
                    literal = pattern.removeprefix("^")
                    # The sweep synthesizes an emission from the pattern, which
                    # only works for plain literals. A future non-literal
                    # pattern must extend this test, not silently skip it.
                    assert not any(ch in literal for ch in r".*+?[](){}|\\$"), (
                        f"{process_name}/{job.name}: pattern {pattern!r} is not a "
                        "plain literal — extend the wrapping sweep to cover it."
                    )
                    for emission in (
                        f"{literal} summary text",
                        f"`{literal}` summary text",
                        f"**{literal}** summary text",
                        f"## {literal} summary text",  # heading prefix (an internal task)
                        f"### `{literal}` summary text",  # heading + inline code
                        f"preamble line\n{literal} summary text",
                        f"preamble line\n`{literal}` summary text",
                        f"preamble line\n## {literal} summary text",
                    ):
                        result = AgentResult(success=True, stdout=emission, stderr="", return_code=0, duration_ms=1)
                        target = evaluate_output_rules([rule], result, Path("/tmp"))
                        assert target == rule.target, (
                            f"{process_name}/{job.name}: {pattern!r} failed to route "
                            f"for emission {emission!r} (got {target!r})"
                        )
                    swept += 1
    # Guard against the sweep silently going hollow (e.g. a refactor that
    # empties job.rules) — full+quickfix carry 14 stdout rules today.
    assert swept >= 10, f"wrapping sweep only covered {swept} rules — sweep broken?"


def test_check_conversational_rules_plain_marker_unchanged():
    from lotsa.flows import check_conversational_rules

    step = ResolvedJob(
        name="spec",
        prompt_name="spec",
        resume_session=False,
        evaluate=False,
        conversational=True,
        rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:", target="next")],
        queue_state="speccing",
        active_state="spec",
        success_state="planned",
    )
    stdout = "SPEC_COMPLETE: widget support\n\nBody."
    captured = check_conversational_rules(step, stdout)
    assert captured is not None
    assert captured.startswith("SPEC_COMPLETE:")


def test_evaluate_output_rules_missing_file(tmp_path):
    from rigg.models import AgentResult

    rules = [
        OutputRule(source="nonexistent.md", pattern=".*", target="blocked"),
        OutputRule(source="stdout", pattern="ok", target="next"),
    ]
    result = AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=100)
    assert evaluate_output_rules(rules, result, tmp_path) == "next"


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def test_find_step():
    process = build_process("full")
    main = process.flows["main"]
    assert find_step(main, "planning").name == "plan"
    assert find_step(main, "nonexistent") is None


def test_find_step_for_gate():
    process = build_process("full")
    main = process.flows["main"]
    assert find_step_for_gate(main, "planned").name == "plan"
    assert find_step_for_gate(main, "nonexistent") is None


def test_next_dispatchable_state_gated():
    process = build_process("full")
    main = process.flows["main"]
    assert next_dispatchable_state(main, "planned") == "testing"


def test_next_dispatchable_state_already_dispatchable():
    process = build_process("full")
    main = process.flows["main"]
    assert next_dispatchable_state(main, "backlog") == "backlog"


def test_next_dispatchable_state_complete():
    process = build_process("full")
    main = process.flows["main"]
    assert next_dispatchable_state(main, "complete") is None


# ---------------------------------------------------------------------------
# Dispatch rules
# ---------------------------------------------------------------------------


def test_build_dispatch_rules_skips_non_agent_jobs(tmp_path):
    """Only agent jobs produce DispatchRules — action/monitor jobs are
    driven through other paths."""
    process = build_process("full")
    main = process.flows["main"]
    rules = build_dispatch_rules(main, work_dir=tmp_path)
    # The full main flow has 7 agent jobs + 1 action + 1 monitor; only agents
    # produce dispatch rules.
    rule_names = {r.job_type for r in rules}
    assert "push_pr" not in rule_names
    assert "wait_for_pr_signal" not in rule_names


def test_build_dispatch_rules_standard(tmp_path):
    process = build_process("standard")
    main = process.flows["main"]
    rules = build_dispatch_rules(main, work_dir=tmp_path)
    assert len(rules) == 1
    assert rules[0].job_type == "coding"


# ---------------------------------------------------------------------------
# Output rule routing
# ---------------------------------------------------------------------------


def test_resolve_output_target_next_returns_success_state():
    process = build_process("full")
    main = process.flows["main"]
    plan = next(rj for rj in main.jobs if rj.name == "plan")
    # plan's success_state is "planned" (gate)
    assert resolve_output_target("next", plan, main) == "planned"


def test_resolve_output_target_blocked():
    process = build_process("full")
    main = process.flows["main"]
    plan = next(rj for rj in main.jobs if rj.name == "plan")
    assert resolve_output_target("blocked", plan, main) == "blocked"


def test_resolve_output_target_named_job():
    process = build_process("full")
    main = process.flows["main"]
    review = next(rj for rj in main.jobs if rj.name == "review")
    code = next(rj for rj in main.jobs if rj.name == "code")
    # main flow override: REVIEW_FAIL targets "code" → resolves to code's queue_state
    assert resolve_output_target("code", review, main) == code.queue_state


def test_resolve_output_target_unknown_routes_to_blocked():
    process = build_process("full")
    main = process.flows["main"]
    plan = next(rj for rj in main.jobs if rj.name == "plan")
    assert resolve_output_target("nonexistent_job", plan, main) == "blocked"


# ---------------------------------------------------------------------------
# pr-fix output rules in full process (semantic preservation)
# ---------------------------------------------------------------------------


def test_full_pr_fix_needs_decision_routes_to_needs_input():
    process = build_process("full")
    pr_fix = process.flows["pr_fix"]
    pr_fix_binding = next(b for b in pr_fix.bindings if b.name == "pr-fix")
    needs = next(r for r in (pr_fix_binding.rules or []) if "NEEDS_DECISION" in r.pattern)
    assert needs.target == "needs_input"


def test_full_pr_fix_needs_decision_precedes_blocked():
    process = build_process("full")
    pr_fix = process.flows["pr_fix"]
    pr_fix_binding = next(b for b in pr_fix.bindings if b.name == "pr-fix")
    patterns = [r.pattern for r in (pr_fix_binding.rules or [])]
    needs_idx = next(i for i, p in enumerate(patterns) if "NEEDS_DECISION" in p)
    blocked_idx = next(i for i, p in enumerate(patterns) if "BLOCKED" in p)
    assert needs_idx < blocked_idx


# ---------------------------------------------------------------------------
# Conversational step parsing
# ---------------------------------------------------------------------------


def test_conversational_job_parsed(tmp_path):
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: t\njobs:\n  - { name: spec, type: agent, conversational: true }\n"
        "  - { name: code, type: agent }\nflows:\n  main: { steps: [spec, code] }\n"
    )
    process = build_process("t", process_file=process_file)
    main = process.flows["main"]
    assert main.jobs[0].conversational is True
    assert main.jobs[1].conversational is False


def test_output_and_inputs_parsed(tmp_path):
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: t\njobs:\n"
        "  - { name: spec, type: agent, output: spec }\n"
        "  - { name: plan, type: agent, inputs: [spec], output: plan }\n"
        "  - { name: code, type: agent, inputs: [spec, plan] }\n"
        "flows:\n  main: { steps: [spec, plan, code] }\n"
    )
    process = build_process("t", process_file=process_file)
    main = process.flows["main"]
    assert main.jobs[0].output == "spec"
    assert main.jobs[1].inputs == ["spec"]
    assert main.jobs[2].inputs == ["spec", "plan"]


def test_full_process_artifact_config():
    process = build_process("full")
    main = process.flows["main"]
    by_name = {s.name: s for s in main.jobs}
    assert by_name["spec"].output == "spec"
    assert by_name["plan"].output == "plan"
    assert by_name["plan"].inputs == ["spec"]
    assert by_name["code"].inputs == ["spec", "plan"]


# ---------------------------------------------------------------------------
# build_process_from_inline — lotsa.yaml ``processes:`` block loader
# ---------------------------------------------------------------------------


def test_build_process_from_inline_minimal(tmp_path):
    """Minimal inline process: two agent steps with bare prompts."""
    from lotsa.flows import build_process_from_inline

    process = build_process_from_inline(
        "marketing_research",
        {
            "steps": [
                {"name": "research", "prompt": "research"},
                {"name": "synthesize", "prompt": "synthesize"},
            ]
        },
        base_dir=tmp_path,
    )
    assert process.name == "marketing_research"
    assert list(process.flows) == ["main"]
    main = process.flows["main"]
    assert [j.name for j in main.jobs] == ["research", "synthesize"]
    assert all(j.type == "agent" for j in main.jobs)


def test_build_process_from_inline_resolves_relative_prompts_dir(tmp_path):
    """``prompts_dir`` resolves against ``base_dir`` when given as a relative path."""
    from lotsa.flows import build_process_from_inline

    process = build_process_from_inline(
        "mkt",
        {
            "prompts_dir": "./prompts/mkt",
            "steps": [{"name": "research", "prompt": "research"}],
        },
        base_dir=tmp_path,
    )
    main = process.flows["main"]
    expected = (tmp_path / "prompts" / "mkt").resolve()
    # The registry's first search path is the resolved prompts dir.
    assert any(p.resolve() == expected for p in main.registry._search_paths)


def test_build_process_from_inline_defaults_prompts_dir_to_prompts(tmp_path):
    """Omitted ``prompts_dir`` defaults to ``<base_dir>/prompts``."""
    from lotsa.flows import build_process_from_inline

    process = build_process_from_inline(
        "p",
        {"steps": [{"name": "step", "prompt": "step"}]},
        base_dir=tmp_path,
    )
    expected = (tmp_path / "prompts").resolve()
    assert any(p.resolve() == expected for p in process.flows["main"].registry._search_paths)


def test_build_process_from_inline_absolute_prompts_dir(tmp_path):
    """An absolute ``prompts_dir`` is used as-is, ignoring ``base_dir``."""
    from lotsa.flows import build_process_from_inline

    absolute_dir = tmp_path / "elsewhere" / "prompts"
    process = build_process_from_inline(
        "p",
        {
            "prompts_dir": str(absolute_dir),
            "steps": [{"name": "step", "prompt": "step"}],
        },
        base_dir=tmp_path / "other-base",
    )
    assert any(p.resolve() == absolute_dir.resolve() for p in process.flows["main"].registry._search_paths)


def test_build_process_from_inline_rejects_empty_steps(tmp_path):
    """Empty or missing ``steps:`` raises a clear error."""
    from lotsa.flows import build_process_from_inline

    for invalid in ({}, {"steps": []}, {"steps": None}):
        try:
            build_process_from_inline("p", invalid, base_dir=tmp_path)
        except ValueError as exc:
            assert "steps" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {invalid!r}")


def test_build_process_from_inline_rejects_non_agent_step_type(tmp_path):
    """Inline processes are agent-only; action/monitor types must use a process.yaml."""
    from lotsa.flows import build_process_from_inline

    try:
        build_process_from_inline(
            "p",
            {"steps": [{"name": "push", "type": "action", "tool": "push_pr"}]},
            base_dir=tmp_path,
        )
    except ValueError as exc:
        msg = str(exc)
        assert "action" in msg
        assert "process.yaml" in msg  # suggests the fallback path
    else:
        raise AssertionError("expected ValueError for non-agent inline step")


def test_build_process_from_inline_rejects_missing_step_name(tmp_path):
    """Each step needs a ``name:`` — the error mentions which step is bad."""
    from lotsa.flows import build_process_from_inline

    try:
        build_process_from_inline(
            "p",
            {"steps": [{"prompt": "foo"}]},
            base_dir=tmp_path,
        )
    except ValueError as exc:
        assert "name" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing step name")


def test_build_process_from_inline_accepts_string_inputs_shorthand(tmp_path):
    """``inputs:`` accepts a single bare string as sugar for ``[<name>]``.

    Mirrors the convenience shim in ``_parse_job`` (the standalone YAML
    process parser). Without it, ``inputs: spec`` would be passed straight
    to ``list(...)`` and silently become ``["s", "p", "e", "c"]`` — broken
    artifact lookup downstream.
    """
    from lotsa.flows import build_process_from_inline

    process = build_process_from_inline(
        "p",
        {
            "steps": [
                {"name": "code", "prompt": "code", "inputs": "spec"},
            ]
        },
        base_dir=tmp_path,
    )
    main = process.flows["main"]
    code = next(j for j in main.jobs if j.name == "code")
    assert code.inputs == ["spec"], f"inputs as a bare string should be wrapped to [<name>]; got {code.inputs!r}"


def test_build_process_from_inline_carries_rules(tmp_path):
    """Per-step ``rules:`` parse into OutputRule lists on the job."""
    from lotsa.flows import build_process_from_inline

    process = build_process_from_inline(
        "p",
        {
            "steps": [
                {
                    "name": "code",
                    "prompt": "code",
                    "rules": [{"source": "stdout", "pattern": "FAIL", "target": "code"}],
                },
                {"name": "review", "prompt": "review"},
            ]
        },
        base_dir=tmp_path,
    )
    main = process.flows["main"]
    code = next(j for j in main.jobs if j.name == "code")
    assert len(code.rules) == 1
    assert code.rules[0].source == "stdout"
    assert code.rules[0].pattern == "FAIL"
    assert code.rules[0].target == "code"


# ---------------------------------------------------------------------------
# ADR-022 — per-step model selection in process.yaml
# ---------------------------------------------------------------------------


def _write_process(tmp_path, body: str):
    """Write a process.yaml to tmp_path and build it via the custom loader."""
    path = tmp_path / "model_process.yaml"
    path.write_text(body)
    return build_process("custom", process_file=path)


def test_agent_job_carries_model_field(tmp_path):
    """ADR-022 step 1/2: ``model:`` on an agent job threads to the resolved step."""
    process = _write_process(
        tmp_path,
        "name: model-test\n"
        "jobs:\n"
        "  - name: code\n"
        "    prompt: coding\n"
        "    model: opus\n"
        "    queue_state: coding\n"
        "    active_state: coding\n"
        "flows:\n"
        "  main:\n"
        "    steps: [code]\n",
    )
    code = process.flows["main"].steps[0]
    assert code.model == "opus"


def test_agent_job_without_model_resolves_to_none(tmp_path):
    """A job that declares no ``model:`` resolves to ``None`` (global fallback)."""
    process = _write_process(
        tmp_path,
        "name: no-model-test\n"
        "jobs:\n"
        "  - name: code\n"
        "    prompt: coding\n"
        "    queue_state: coding\n"
        "    active_state: coding\n"
        "flows:\n"
        "  main:\n"
        "    steps: [code]\n",
    )
    code = process.flows["main"].steps[0]
    assert code.model is None


def test_model_accepted_on_action_job(tmp_path):
    """ADR-022 step 1: ``model:`` is accepted on a non-agent (action) job —
    parsed and silently ignored, never a schema error at load time."""
    process = _write_process(
        tmp_path,
        "name: action-model-test\n"
        "jobs:\n"
        "  - name: code\n"
        "    prompt: coding\n"
        "    queue_state: coding\n"
        "    active_state: coding\n"
        "  - name: push_pr\n"
        "    type: action\n"
        "    tool: push_pr\n"
        "    model: opus\n"
        "flows:\n"
        "  main:\n"
        "    steps: [code, push_pr]\n",
    )
    push = next(s for s in process.flows["main"].steps if s.name == "push_pr")
    # The field round-trips even though the action dispatch path never reads it.
    assert push.model == "opus"


def test_model_threaded_through_catalog_fallback(tmp_path):
    """ADR-022 step 2: a job declared but not wired into any flow still carries
    its ``model:`` in the process catalog (the line-1003 fallback constructor).

    Omitting the field here would silently drop the override when the job is
    later wired into a flow — exactly the ADR-014 silent-drop class.
    """
    process = _write_process(
        tmp_path,
        "name: catalog-fallback-test\n"
        "jobs:\n"
        "  - name: code\n"
        "    prompt: coding\n"
        "    queue_state: coding\n"
        "    active_state: coding\n"
        "  - name: orphan\n"
        "    prompt: orphan\n"
        "    model: opus\n"
        "flows:\n"
        "  main:\n"
        "    steps: [code]\n",
    )
    orphan = next(j for j in process.jobs if j.name == "orphan")
    assert orphan.model == "opus"


def test_model_resolved_in_subflow_step(tmp_path):
    """ADR-022 step 6: a job that lives in a sub-flow resolves its ``model:``
    the same way as one in main — there is no per-flow special path."""
    process = _write_process(
        tmp_path,
        "name: subflow-model-test\n"
        "jobs:\n"
        "  - name: code\n"
        "    prompt: coding\n"
        "    queue_state: coding\n"
        "    active_state: coding\n"
        "  - name: review\n"
        "    prompt: review\n"
        "    model: opus\n"
        "    queue_state: reviewing\n"
        "    active_state: reviewing\n"
        "flows:\n"
        "  main:\n"
        "    steps: [code]\n"
        "  pr_fix:\n"
        "    steps: [review]\n",
    )
    review = process.flows["pr_fix"].steps[0]
    assert review.model == "opus"


def _gate_job(**kw: object) -> ResolvedJob:
    base = dict(
        name="x",
        prompt_name="x",
        resume_session=False,
        evaluate=False,
        queue_state="q",
        active_state="a",
        success_state="s",
    )
    base.update(kw)
    return ResolvedJob(**base)  # type: ignore[arg-type]


def test_is_approval_gate():
    """The operator-Accept predicate: an output artifact, an evaluate gate, OR a
    conversational step with a forward (next) rule (verify). A rule-less chat REPL
    and a non-conversational auto-routing step are NOT gates. Regression guard for
    the Accept-on-chat narrowing that dropped verify's Accept button."""
    nxt = OutputRule(source="stdout", pattern="^VERIFIED:", target="next")
    back = OutputRule(source="stdout", pattern="^NEEDS_CODE:", target="code")
    # verify: conversational + forward rule, no output, not evaluate
    assert _gate_job(conversational=True, rules=[nxt, back]).is_approval_gate is True
    # spec: produces an output artifact
    assert _gate_job(conversational=True, output="spec", rules=[nxt]).is_approval_gate is True
    # plan: evaluate gate
    assert _gate_job(evaluate=True).is_approval_gate is True
    # chat REPL: conversational, no rules → not a gate
    assert _gate_job(conversational=True, rules=[]).is_approval_gate is False
    # non-conversational agent step (code/review): auto-routes on its rule, not an
    # operator gate even though it has a next-rule
    assert _gate_job(conversational=False, rules=[nxt]).is_approval_gate is False
    # conversational step whose only rule routes backward → no forward accept
    assert _gate_job(conversational=True, rules=[back]).is_approval_gate is False
