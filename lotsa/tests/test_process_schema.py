"""Tests for the new ``process.yaml`` schema (ADR-014 Layer A).

These tests cover the substrate-level acceptance criteria:

* Typed jobs (``type: agent | action | monitor``) with their required fields.
* The ``flows:`` block with per-step rule overrides.
* State machine derivation with NO synthetic PR-phase states.
* Migration errors: ``pr:`` block rejection, ``commit: true`` without ``output_file:``.
* ``current_flow`` and ``last_run_step`` metadata semantics.

These import from a module that does not yet exist (``lotsa.flows.build_process``,
``Process``, ``FlowBinding``) — they fail at collection until the implementation
lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# build_process + Process / FlowBinding types
# ---------------------------------------------------------------------------


def test_build_process_exposes_canonical_entry_point():
    """``lotsa.flows`` exports ``build_process`` as the new canonical loader."""
    from lotsa.flows import build_process

    assert callable(build_process)


def test_process_dataclass_carries_jobs_and_flows():
    """``Process`` has ``name``, ``jobs``, ``flows`` keyed by flow name."""
    from lotsa.flows import Process

    # Field presence is the contract — instantiation may need more args; what
    # matters is that the dataclass declares the right shape.
    fields = {f.name for f in Process.__dataclass_fields__.values()}
    assert {"name", "jobs", "flows"}.issubset(fields)


def test_flow_binding_dataclass_carries_name_rules_config():
    """``FlowBinding`` is the per-flow step type with optional rule/config overrides."""
    from lotsa.flows import FlowBinding

    fields = {f.name for f in FlowBinding.__dataclass_fields__.values()}
    assert {"name", "rules", "config"}.issubset(fields)


# ---------------------------------------------------------------------------
# Typed jobs — type / tool / engine / config / output_file / commit fields
# ---------------------------------------------------------------------------


def test_job_dataclass_supports_type_field():
    from lotsa.flows import Job

    j = Job(name="x", type="action", tool="push_pr")
    assert j.type == "action"
    assert j.tool == "push_pr"


def test_job_dataclass_supports_engine_and_config_fields():
    from lotsa.flows import Job

    j = Job(name="m", type="monitor", engine="pr_monitor", config={"poll_interval_seconds": 30})
    assert j.engine == "pr_monitor"
    assert j.config == {"poll_interval_seconds": 30}


def test_job_default_type_is_agent():
    """Default ``type`` is ``agent`` so existing YAML without a ``type:`` line still parses."""
    from lotsa.flows import Job

    j = Job(name="agentish")
    assert j.type == "agent"


def test_job_dataclass_supports_output_file_and_commit_fields():
    """ADR-016 schema slots are present on Job (parser only; write path deferred)."""
    from lotsa.flows import Job

    j = Job(name="x", output_file="docs/foo.md", commit=True)
    assert j.output_file == "docs/foo.md"
    assert j.commit is True


def test_resolved_job_carries_type_tool_engine_fields():
    from lotsa.flows import ResolvedJob

    fields = {f.name for f in ResolvedJob.__dataclass_fields__.values()}
    assert {"type", "tool", "engine", "config"}.issubset(fields)


# ---------------------------------------------------------------------------
# Parser-level validation
# ---------------------------------------------------------------------------


def test_action_job_without_tool_rejected(tmp_path: Path):
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text("process: bad\njobs:\n  - { name: doit, type: action }\nflows:\n  main: { steps: [doit] }\n")
    with pytest.raises(ValueError, match="tool"):
        build_process("bad", process_file=yaml_path)


def test_monitor_job_without_engine_rejected(tmp_path: Path):
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: bad\njobs:\n  - { name: watch, type: monitor }\nflows:\n  main: { steps: [watch] }\n"
    )
    with pytest.raises(ValueError, match="engine"):
        build_process("bad", process_file=yaml_path)


def test_action_job_with_unknown_tool_rejected_at_build_time(tmp_path: Path):
    """A ``tool:`` typo fails at ``build_process`` time, not at dispatch time.

    Regression for the claude[bot] medium finding on PR #65: previously a
    typo like ``tool: pust_pr`` would parse cleanly, the orchestrator would
    happily start, and the operator would only learn about the typo after
    spec / plan / test / code / review had already run and the action step
    finally tried to ``get_tool("pust_pr")``.
    """
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: bad\njobs:\n  - { name: doit, type: action, tool: pust_pr }\nflows:\n  main: { steps: [doit] }\n"
    )
    with pytest.raises(ValueError, match="unknown tool 'pust_pr'"):
        build_process("bad", process_file=yaml_path)


def test_monitor_job_with_unknown_engine_rejected_at_build_time(tmp_path: Path):
    """An ``engine:`` typo fails at ``build_process`` time, not at startup time."""
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: bad\n"
        "jobs:\n"
        "  - { name: watch, type: monitor, engine: pr_monittor }\n"
        "flows:\n  main: { steps: [watch] }\n"
    )
    with pytest.raises(ValueError, match="unknown engine 'pr_monittor'"):
        build_process("bad", process_file=yaml_path)


def test_commit_true_without_output_file_rejected(tmp_path: Path):
    """ADR-016 acceptance criterion: ``commit: true`` requires ``output_file:``."""
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: bad\n"
        "jobs:\n"
        "  - { name: code, type: agent, prompt: coding, commit: true }\n"
        "flows:\n"
        "  main: { steps: [code] }\n"
    )
    with pytest.raises(ValueError, match="output_file"):
        build_process("bad", process_file=yaml_path)


def test_legacy_pr_block_raises_with_migration_message(tmp_path: Path):
    """A YAML file containing the old ``pr:`` block fails with a migration error."""
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: legacy\n"
        "jobs:\n"
        "  - { name: code, type: agent, prompt: coding }\n"
        "  - { name: pr-fix, type: agent, prompt: pr-fix }\n"
        "pr:\n"
        "  poll_interval_seconds: 30\n"
        "flows:\n"
        "  main: { steps: [code] }\n"
    )
    with pytest.raises(ValueError) as exc_info:
        build_process("legacy", process_file=yaml_path)
    # Migration text must mention the new monitor-job shape.
    msg = str(exc_info.value)
    assert "monitor" in msg.lower()
    assert "engine" in msg.lower() or "pr_monitor" in msg


# ---------------------------------------------------------------------------
# Flows block — per-step rule overrides
# ---------------------------------------------------------------------------


def test_bare_string_step_in_flow_is_binding_with_no_overrides(tmp_path: Path):
    """``steps: [name]`` is sugar for ``[{name: name}]`` with no rule override."""
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: sugar\njobs:\n  - { name: code, type: agent, prompt: coding }\nflows:\n  main: { steps: [code] }\n"
    )
    process = build_process("sugar", process_file=yaml_path)

    flow = process.flows["main"]
    assert len(flow.bindings) == 1
    assert flow.bindings[0].name == "code"
    # Bare-string sugar produces no rule overrides — None means "use job defaults"
    assert flow.bindings[0].rules is None


def test_flow_step_dict_form_overrides_job_rules(tmp_path: Path):
    """Dict-form steps with ``rules:`` override the job's default rules in this flow only."""
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "process": "override",
                "jobs": [
                    {
                        "name": "review",
                        "type": "agent",
                        "prompt": "review",
                        "rules": [
                            {"source": "stdout", "pattern": "^REVIEW_PASS", "target": "next"},
                            {"source": "stdout", "pattern": "^REVIEW_FAIL", "target": "blocked"},
                        ],
                    },
                    {"name": "code", "type": "agent", "prompt": "coding"},
                ],
                "flows": {
                    "main": {
                        "steps": [
                            "code",
                            {
                                "name": "review",
                                "rules": [{"source": "stdout", "pattern": "^REVIEW_FAIL", "target": "code"}],
                            },
                        ]
                    },
                },
            }
        )
    )
    process = build_process("override", process_file=yaml_path)

    main = process.flows["main"]
    review_binding = next(b for b in main.bindings if b.name == "review")
    assert review_binding.rules is not None
    # Per-flow override replaces the default REVIEW_FAIL → blocked with → code.
    fail_rules = [r for r in review_binding.rules if "REVIEW_FAIL" in r.pattern]
    assert len(fail_rules) == 1
    assert fail_rules[0].target == "code"


# ---------------------------------------------------------------------------
# State machine derivation — no synthetic PR-phase states
# ---------------------------------------------------------------------------


def test_full_process_has_no_synthetic_pr_phase_states():
    """The new full process must NOT register ``pushing`` / ``waiting_for_pr`` / ``rebasing``."""
    from lotsa.flows import build_process

    process = build_process("build")
    main = process.flows["main"]
    states = main.state_machine.states

    assert "pushing" not in states
    assert "waiting_for_pr" not in states
    assert "rebasing" not in states


def test_full_process_has_push_pr_action_state():
    """The ``push_pr`` action job contributes a state addressable by rule targets."""
    from lotsa.flows import build_process

    process = build_process("build")
    push_pr = next(j for j in process.jobs if j.name == "push_pr")
    assert push_pr.type == "action"
    assert push_pr.tool == "push_pr"


def test_full_process_has_wait_for_pr_signal_monitor_state():
    """The ``wait_for_pr_signal`` monitor job contributes a single state addressable
    by name."""
    from lotsa.flows import build_process

    process = build_process("build")
    monitor = next(j for j in process.jobs if j.name == "wait_for_pr_signal")
    assert monitor.type == "monitor"
    assert monitor.engine == "pr_monitor"
    main = process.flows["main"]
    assert "wait_for_pr_signal" in main.state_machine.states


def test_main_flow_review_fail_routes_to_code():
    """Per-flow override: in ``main``, REVIEW_FAIL routes to ``code``."""
    from lotsa.flows import build_process

    process = build_process("build")
    main = process.flows["main"]
    review_binding = next(b for b in main.bindings if b.name == "review")
    fail_rules = [r for r in (review_binding.rules or []) if "REVIEW_FAIL" in r.pattern]
    assert fail_rules, "main.review must override REVIEW_FAIL routing"
    assert fail_rules[0].target == "code"


def test_pr_fix_flow_review_fail_routes_to_pr_fix():
    """Per-flow override: in ``pr_fix``, REVIEW_FAIL routes back to ``pr-fix``."""
    from lotsa.flows import build_process

    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    review_binding = next(b for b in pr_fix.bindings if b.name == "review")
    fail_rules = [r for r in (review_binding.rules or []) if "REVIEW_FAIL" in r.pattern]
    assert fail_rules, "pr_fix.review must override REVIEW_FAIL routing"
    assert fail_rules[0].target == "pr-fix"


def test_pr_fix_flow_has_no_verify_step():
    """The pr_fix flow skips verify — supersedes the PR #62 stopgap heuristic."""
    from lotsa.flows import build_process

    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    assert not any(b.name == "verify" for b in pr_fix.bindings)


def test_cross_flow_rule_target_first_agent_uses_resolved_queue_state(tmp_path: Path):
    """A cross-flow rule target whose queue_state is derived (not declared)
    must register the transition against the target's ACTUAL queue_state
    (e.g. ``"backlog"`` for the first agent binding in its flow), not the
    raw job name.

    Pre-fix, ``_build_state_machine`` looked up cross-flow targets in the
    raw-Job catalog and used ``cross.queue_state or cross.name`` — for a
    target that is the first agent in its flow (deriving ``"backlog"``),
    this produced a stale ``(active_state, job.name)`` edge instead of
    ``(active_state, "backlog")``. The orchestrator's pre-CAS transition
    check would then reject the dispatch silently because the actual CAS
    target is ``"backlog"`` (the ResolvedJob queue_state).

    Bundled processes happen to dodge this — none of their cross-flow
    rule targets sit at first-binding position — so the bug is latent
    there. This test pins the contract for custom processes.
    """
    from lotsa.flows import build_process

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "process": "first_agent_cross_flow",
                "jobs": [
                    # ``alpha`` is the first agent of the ``side`` flow —
                    # _resolve_jobs derives its queue_state as ``"backlog"``.
                    {"name": "alpha", "type": "agent", "prompt": "alpha"},
                    {"name": "beta", "type": "agent", "prompt": "beta"},
                    {
                        "name": "gamma",
                        "type": "agent",
                        "prompt": "gamma",
                        # gamma's GO rule targets alpha (cross-flow).
                        "rules": [
                            {"source": "stdout", "pattern": "^GO:", "target": "alpha"},
                        ],
                    },
                ],
                "flows": {
                    "main": {"steps": ["beta", "gamma"]},
                    "side": {"steps": ["alpha"]},
                },
            }
        )
    )
    process = build_process("first_agent_cross_flow", process_file=yaml_path)

    # Alpha's actual queue_state under the ``side`` flow is ``"backlog"``.
    side = process.flows["side"]
    alpha_resolved = next(rj for rj in side.jobs if rj.name == "alpha")
    assert alpha_resolved.queue_state == "backlog"

    # The cross-flow edge from gamma.active to alpha's queue_state must
    # use the resolved value, not the raw job name. The edge is needed
    # in BOTH the target flow's SM (so the engine's CAS can validate the
    # destination) AND the source flow's SM (so the drainer's pre-CAS
    # check against the active flow accepts the routing).
    gamma_resolved = next(rj for rj in process.flows["main"].jobs if rj.name == "gamma")
    side_sm = side.state_machine
    main_sm = process.flows["main"].state_machine
    assert (gamma_resolved.active_state, "backlog") in side_sm.transitions
    assert (gamma_resolved.active_state, "backlog") in main_sm.transitions
    # And the stale edge keyed on the raw job name must NOT be there.
    assert (gamma_resolved.active_state, "alpha") not in main_sm.transitions


def test_pr_fix_flow_skipped_targets_wait_for_pr_signal():
    """``PR_FIX_SKIPPED:`` in the pr_fix flow targets the monitor by name."""
    from lotsa.flows import build_process

    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    pr_fix_binding = next(b for b in pr_fix.bindings if b.name == "pr-fix")
    skipped_rules = [r for r in (pr_fix_binding.rules or []) if "SKIPPED" in r.pattern]
    assert skipped_rules
    assert skipped_rules[0].target == "wait_for_pr_signal"


# ---------------------------------------------------------------------------
# resolve_output_target — no previous, current_flow scoped
# ---------------------------------------------------------------------------


def test_resolve_output_target_signature_drops_previous_step_name():
    """``resolve_output_target`` no longer accepts ``previous_step_name``."""
    import inspect

    from lotsa.flows import resolve_output_target

    sig = inspect.signature(resolve_output_target)
    assert "previous_step_name" not in sig.parameters


def test_target_previous_no_longer_a_recognized_value(tmp_path: Path):
    """``target: previous`` is no longer a valid rule target — it routes to blocked."""
    from lotsa.flows import build_process, resolve_output_target

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        "process: t\n"
        "jobs:\n"
        "  - { name: a, type: agent, prompt: a }\n"
        "  - { name: b, type: agent, prompt: b }\n"
        "flows:\n"
        "  main: { steps: [a, b] }\n"
    )
    process = build_process("t", process_file=yaml_path)
    main = process.flows["main"]
    job_a = next(j for j in process.jobs if j.name == "a")

    # "previous" is not a recognised special value anymore — must route to blocked.
    assert resolve_output_target("previous", job_a, main) == "blocked"


def test_resolve_output_target_next_uses_current_flow_order(tmp_path: Path):
    """``next`` walks the currently-active flow's bindings, not the global job list."""
    from lotsa.flows import build_process, resolve_output_target

    yaml_path = tmp_path / "process.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "process": "branchy",
                "jobs": [
                    {"name": "a", "type": "agent", "prompt": "a"},
                    {"name": "b", "type": "agent", "prompt": "b"},
                    {"name": "c", "type": "agent", "prompt": "c"},
                ],
                "flows": {
                    "main": {"steps": ["a", "b", "c"]},
                    "side": {"steps": ["c", "a"]},
                },
            }
        )
    )
    process = build_process("branchy", process_file=yaml_path)
    job_a = next(j for j in process.jobs if j.name == "a")

    main_next = resolve_output_target("next", job_a, process.flows["main"])
    side_next = resolve_output_target("next", job_a, process.flows["side"])

    # In main, "a → next" goes to b; in side, "a → next" goes to complete (last).
    job_b = next(j for j in process.jobs if j.name == "b")
    assert main_next == job_b.queue_state
    assert side_next == "complete"


# ---------------------------------------------------------------------------
# PrConfig removal
# ---------------------------------------------------------------------------


def test_pr_config_no_longer_importable():
    """``PrConfig`` is removed; importing it should fail."""
    with pytest.raises(ImportError):
        from lotsa.flows import PrConfig  # noqa: F401


def test_flow_config_has_no_pr_config_attribute():
    """``FlowConfig`` no longer carries a ``pr_config`` attribute."""
    from lotsa.flows import build_process

    process = build_process("build")
    main = process.flows["main"]
    assert not hasattr(main, "pr_config")


# ---------------------------------------------------------------------------
# Bundled process file rename
# ---------------------------------------------------------------------------


def test_bundled_build_process_file_exists():
    """The ``build`` preset is loaded from ``prompts/build/process.yaml`` (ADR-043)."""
    from lotsa.flows import BUNDLED_PROMPTS

    assert (BUNDLED_PROMPTS / "build" / "process.yaml").is_file()


def test_bundled_fix_process_file_exists():
    from lotsa.flows import BUNDLED_PROMPTS

    assert (BUNDLED_PROMPTS / "fix" / "process.yaml").is_file()


def test_bundled_chat_process_file_exists():
    from lotsa.flows import BUNDLED_PROMPTS

    assert (BUNDLED_PROMPTS / "chat" / "process.yaml").is_file()


def test_legacy_flow_yaml_files_are_removed():
    """The old ``flow.yaml`` files must be deleted to prevent split-brain loading."""
    from lotsa.flows import BUNDLED_PROMPTS

    for preset in ("chat", "build", "fix"):
        legacy = BUNDLED_PROMPTS / preset / "flow.yaml"
        assert not legacy.exists(), f"{legacy} should be deleted; replaced by process.yaml"
