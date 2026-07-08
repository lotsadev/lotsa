"""Tests for ADR-028 Phase 3 — per-step runner selection.

Covers:
- ``Job`` / ``ResolvedJob`` ``runner`` field threading through the YAML
  parser and ``_resolve_jobs``.
- ``_validate_runner_references`` build-time rejection of unknown runner names.
- ``_resolve_runner`` precedence:
    ``_runner_override`` > ``step.runner`` > ``step.model``/``config.model``
- Built-in ``claude-agent-sdk`` registration at ``start()``.
- Model passthrough on the ``step.runner`` path.
- ``agent_runner_name`` reflects the registered name on per-step-routed dispatch.
- Bundled presets unchanged (``runner`` is ``None`` on every job).

These tests FAIL until the implementation lands. Specific failure modes:
- ``AttributeError: 'Job' object has no attribute 'runner'``  (field missing)
- ``AttributeError: 'ResolvedJob' object has no attribute 'runner'``
- ``resolve_runner_by_name`` ``ImportError`` / ``NameError``
- ``build_process`` succeeds with an unknown ``runner:`` instead of raising
  (validation missing)
- ``_resolve_runner`` ignores ``step.runner`` and falls through to model-based
  resolution (precedence branch missing)
- ``claude-agent-sdk`` not in registry after ``start()`` (built-in registration
  missing)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import Job, ResolvedJob, build_process
from lotsa.orchestrator import OrchestratorService
from rigg import agent_runner as ar
from rigg.agent_runner import RunnerNotFound, register_runner
from rigg.models import AgentResult

# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Minimal AgentRunner test double (satisfies the Protocol structurally)."""

    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "work_dir": work_dir,
                **kwargs,
            }
        )
        return AgentResult(
            success=True,
            stdout=f"output from {self.tag}",
            stderr="",
            return_code=0,
            duration_ms=1,
        )


@pytest.fixture(autouse=True)
def _isolated_runner_registry():
    """Snapshot/restore the process-global runner registry around every test."""
    snap = ar.snapshot()
    yield
    ar.restore(snap)


# ---------------------------------------------------------------------------
# Job / ResolvedJob ``runner`` field
# ---------------------------------------------------------------------------


def test_job_dataclass_has_runner_field():
    """``Job`` must expose a ``runner`` field (defaults to None).

    Fails pre-implementation with:
        AttributeError: 'Job' object has no attribute 'runner'
    """
    j = Job(name="code")
    assert j.runner is None


def test_job_runner_field_can_be_set():
    """``Job`` accepts a ``runner`` string value.

    Fails pre-implementation with:
        TypeError: Job.__init__() got an unexpected keyword argument 'runner'
    """
    j = Job(name="code", runner="claude-agent-sdk")
    assert j.runner == "claude-agent-sdk"


def test_resolved_job_dataclass_has_runner_field():
    """``ResolvedJob`` must expose a ``runner`` field (defaults to None).

    Fails pre-implementation with:
        AttributeError: 'ResolvedJob' object has no attribute 'runner'
    """
    rj = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
    )
    assert rj.runner is None


def test_resolved_job_runner_field_can_be_set():
    """``ResolvedJob`` accepts a ``runner`` string value.

    Fails pre-implementation with:
        TypeError: ResolvedJob.__init__() got an unexpected keyword argument 'runner'
    """
    rj = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="claude-agent-sdk",
    )
    assert rj.runner == "claude-agent-sdk"


# ---------------------------------------------------------------------------
# YAML parser threading
# ---------------------------------------------------------------------------


def _write_min_prompts(prompts_dir: Path, names: list[str]) -> None:
    prompts_dir.mkdir(exist_ok=True)
    for name in names:
        for kind in ("system", "user"):
            (prompts_dir / f"{name}-{kind}.md").write_text(f"Prompt: {name}\n{{title}}\n{{body}}")


def test_runner_field_parsed_from_yaml_into_resolved_job(tmp_path):
    """A ``runner:`` key on a YAML job entry must reach ``ResolvedJob.runner``.

    Fails pre-implementation: ``build_process`` silently discards the key
    (``_parse_job`` never reads it), so the resulting ``ResolvedJob.runner``
    is ``None`` instead of ``"my-runner"``.
    """
    register_runner("my-runner", _FakeRunner("r"))

    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "myproc",
                "jobs": [{"name": "code", "type": "agent", "runner": "my-runner"}],
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])
    process = build_process("myproc", prompts_dir=tmp_path / "prompts", process_file=process_file)

    code_job = next(j for j in process.jobs if j.name == "code")
    assert code_job.runner == "my-runner"


def test_runner_none_when_yaml_omits_field(tmp_path):
    """A job with no ``runner:`` key resolves to ``runner=None`` (non-regression).

    Fails pre-implementation because ``ResolvedJob`` has no ``runner`` attribute
    at all (AttributeError) — once the field exists this test will pass.
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(yaml.dump({"process": "myproc", "jobs": [{"name": "code", "type": "agent"}]}))
    _write_min_prompts(tmp_path / "prompts", ["code"])
    process = build_process("myproc", prompts_dir=tmp_path / "prompts", process_file=process_file)

    code_job = next(j for j in process.jobs if j.name == "code")
    assert code_job.runner is None


# ---------------------------------------------------------------------------
# Build-time validation — unknown runner name fails at startup
# ---------------------------------------------------------------------------


def test_build_process_rejects_unknown_runner_name(tmp_path):
    """A ``runner: bad-name`` that is not in the registry must raise
    ``ValueError`` from ``build_process`` (the build-time validation).

    Fails pre-implementation: ``build_process`` currently succeeds with any
    ``runner:`` value because there is no ``_validate_runner_references`` call.
    After implementation the test passes because the validator raises.
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "myproc",
                "jobs": [{"name": "code", "type": "agent", "runner": "typo-runner"}],
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])

    with pytest.raises(ValueError) as exc:
        build_process("myproc", prompts_dir=tmp_path / "prompts", process_file=process_file)

    assert "typo-runner" in str(exc.value)


def test_build_process_rejects_unknown_runner_lists_registered_names(tmp_path):
    """The ValueError for an unknown runner lists registered runner names so the
    operator can immediately spot the typo.

    Fails pre-implementation for the same reason as the test above.
    """
    register_runner("good-runner", _FakeRunner("good"))

    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "myproc",
                "jobs": [{"name": "code", "type": "agent", "runner": "bad-runner"}],
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])

    with pytest.raises(ValueError) as exc:
        build_process("myproc", prompts_dir=tmp_path / "prompts", process_file=process_file)

    assert "good-runner" in str(exc.value)


def test_build_process_accepts_registered_runner_name(tmp_path):
    """A ``runner:`` name that IS registered passes validation and the field is
    set on the resolved job.

    Fails pre-implementation because ``_parse_job`` never reads the field, so
    the resolved job has ``runner=None`` (AttributeError first, then None≠name).
    """
    register_runner("my-runner", _FakeRunner("mine"))

    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        yaml.dump(
            {
                "process": "myproc",
                "jobs": [{"name": "code", "type": "agent", "runner": "my-runner"}],
            }
        )
    )
    _write_min_prompts(tmp_path / "prompts", ["code"])
    process = build_process("myproc", prompts_dir=tmp_path / "prompts", process_file=process_file)

    code_job = next(j for j in process.jobs if j.name == "code")
    assert code_job.runner == "my-runner"


# ---------------------------------------------------------------------------
# Bundled presets unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["chat", "build", "fix"])
def test_bundled_preset_jobs_have_no_runner_field(preset):
    """Bundled presets must ship no ``runner:`` field — all jobs have
    ``runner=None``.

    Fails pre-implementation because ``ResolvedJob`` has no ``runner`` attribute
    (AttributeError) — once the field exists this test verifies the presets are
    unmodified.
    """
    process = build_process(preset)
    for job in process.jobs:
        assert job.runner is None, (
            f"Preset {preset!r} job {job.name!r} has runner={job.runner!r}; bundled presets must not declare a runner"
        )


# ---------------------------------------------------------------------------
# _resolve_runner precedence
# ---------------------------------------------------------------------------


def _make_minimal_service(tmp_path: Path, flow: str = "build") -> OrchestratorService:
    """Create an OrchestratorService without calling start()."""
    db = TaskDB(tmp_path / "lotsa.db")
    config = LotsaConfig(
        model="sonnet",
        budget=5.0,
        data_dir=tmp_path,
        flow=flow,
    )
    return OrchestratorService(config, db)


def test_resolve_runner_override_wins_over_step_runner(tmp_path):
    """``_runner_override`` beats a per-step ``step.runner``.

    Fails pre-implementation because ``_resolve_runner`` has no ``step.runner``
    branch; this test proves the override stays the top-priority path.
    Before implementation the test fails because the override path returns an
    instance that IS the override (coincidentally correct) but the missing
    ``step.runner`` branch means the second runner never gets involved — the
    test may pass vacuously. After implementation BOTH branches exist and the
    test pins the override as higher priority.
    """
    svc = _make_minimal_service(tmp_path)

    override = _FakeRunner("override")
    named = _FakeRunner("named")
    register_runner("named-runner", named)

    svc._runner_override = override

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="named-runner",
        model="sonnet",
    )

    resolved = svc._resolve_runner(step)

    assert resolved.runner is override
    assert resolved.name == "default"


def test_resolve_runner_step_runner_beats_step_model(tmp_path):
    """``step.runner`` takes precedence over ``step.model`` prefix routing.

    Fails pre-implementation: ``_resolve_runner`` has no ``step.runner`` branch
    and always routes via ``resolve_runner(step.model or config.model)``, so a
    step with ``runner="my-sdk"`` and ``model="gpt-5"`` routes to the "gpt"
    prefix-owner runner instead of "my-sdk".
    """
    svc = _make_minimal_service(tmp_path)

    cli_runner = _FakeRunner("cli")
    named_runner = _FakeRunner("named")
    register_runner("gpt", cli_runner, prefixes=["gpt-"])
    register_runner("my-sdk", named_runner)

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="my-sdk",
        model="gpt-5",
    )

    resolved = svc._resolve_runner(step)

    assert resolved.name == "my-sdk"
    assert resolved.runner is named_runner


def test_resolve_runner_falls_back_to_model_routing_when_no_step_runner(tmp_path):
    """When ``step.runner is None``, ``_resolve_runner`` falls back to
    ``step.model``/``config.model`` routing — today's behaviour unchanged.

    Fails pre-implementation because ``ResolvedJob`` has no ``runner`` attribute
    (the test can't construct a step with ``runner=None`` explicitly), so
    accessing ``step.runner`` inside ``_resolve_runner`` would raise AttributeError.
    After implementation the attribute exists and this path remains correct.
    """
    svc = _make_minimal_service(tmp_path)

    gpt_runner = _FakeRunner("gpt")
    register_runner("gpt", gpt_runner, prefixes=["gpt-"])

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner=None,
        model="gpt-5",
    )

    resolved = svc._resolve_runner(step)

    assert resolved.name == "gpt"
    assert resolved.runner is gpt_runner


def test_resolve_runner_step_runner_mistyped_raises_runner_not_found(tmp_path):
    """``_resolve_runner`` must raise ``RunnerNotFound`` for a ``step.runner``
    name that is not registered — NOT fall back to the default runner.

    Fails pre-implementation: the ``step.runner`` branch doesn't exist, so the
    step routes via model-based resolution and silently lands on the default.
    """
    svc = _make_minimal_service(tmp_path)

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="does-not-exist",
        model="sonnet",
    )

    with pytest.raises(RunnerNotFound):
        svc._resolve_runner(step)


# ---------------------------------------------------------------------------
# Model passthrough on the step.runner path
# ---------------------------------------------------------------------------


def test_resolve_runner_step_runner_resolves_instance_independently_of_model(tmp_path):
    """When ``step.runner`` picks the runner, the runner instance is resolved by
    name; the model for ``run()`` is determined separately at the dispatch site.

    Verifies the contract: ``_resolve_runner`` returns the named runner
    instance, and the model formula ``step.model or config.model`` is unchanged.

    Fails pre-implementation: the ``step.runner`` branch doesn't exist so
    ``_resolve_runner`` routes by model, returning the wrong runner.
    """
    svc = _make_minimal_service(tmp_path)

    recording_runner = _FakeRunner("recording")
    register_runner("my-runner", recording_runner)

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="my-runner",
        model="opus",
    )

    resolved = svc._resolve_runner(step)

    assert resolved.name == "my-runner"
    assert resolved.runner is recording_runner
    # The model the dispatch site will pass to run() is still step.model or config.model.
    effective_model = step.model or svc.config.model
    assert effective_model == "opus"


# ---------------------------------------------------------------------------
# agent_runner_name reflects registered name on per-step-routed dispatch
# ---------------------------------------------------------------------------


def test_resolve_runner_name_is_registered_name_not_default(tmp_path):
    """When ``step.runner`` resolves to a named runner, ``ResolvedRunner.name``
    is that registered name, not "default".

    This is the contract that flows into ``info.agent_runner_name`` and the
    audit trail (``orchestrator.py:3835``).

    Fails pre-implementation: the ``step.runner`` branch doesn't exist, so
    all dispatches route through ``resolve_runner(model)`` and the name is
    whatever the model-based resolution returns (often "default").
    """
    svc = _make_minimal_service(tmp_path)

    named_runner = _FakeRunner("sdk-like")
    register_runner("my-named-runner", named_runner)

    step = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="code",
        success_state="complete",
        runner="my-named-runner",
    )

    resolved = svc._resolve_runner(step)

    assert resolved.name == "my-named-runner"


# ---------------------------------------------------------------------------
# Built-in ``claude-agent-sdk`` registration at start()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_registers_claude_agent_sdk_built_in(tmp_path):
    """``OrchestratorService.start()`` registers ``ClaudeAgentSDKRunner`` under
    the name ``claude-agent-sdk`` so ``runner: claude-agent-sdk`` works with no
    ``runners:`` block in lotsa.yaml.

    Fails pre-implementation: ``start()`` never registers this name, so
    ``resolve_runner_by_name("claude-agent-sdk")`` raises ``RunnerNotFound``.
    """
    from rigg import resolve_runner_by_name
    from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

    db = TaskDB(tmp_path / "lotsa.db")
    await db.initialize()
    config = LotsaConfig(
        model="sonnet",
        budget=5.0,
        data_dir=tmp_path,
        flow="build",
    )
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner("fake")
    await svc.start()

    try:
        resolved = resolve_runner_by_name("claude-agent-sdk")
        assert isinstance(resolved.runner, ClaudeAgentSDKRunner)
        assert resolved.name == "claude-agent-sdk"
    finally:
        await svc.shutdown()


@pytest.mark.asyncio
async def test_start_claude_agent_sdk_builtin_not_default(tmp_path):
    """The built-in ``claude-agent-sdk`` registration must NOT become the
    default runner — only reachable by an explicit name, not model fallback.

    Fails pre-implementation: ``start()`` never registers "claude-agent-sdk"
    so importing ``resolve_runner_by_name`` fails (NameError/ImportError).
    After implementation the name IS registered but is NOT the default, so
    resolving an unknown model still falls to "default", not "claude-agent-sdk".
    """
    from rigg import (
        resolve_runner,
        resolve_runner_by_name,  # must exist after start() registration
    )

    db = TaskDB(tmp_path / "lotsa.db")
    await db.initialize()
    config = LotsaConfig(
        model="sonnet",
        budget=5.0,
        data_dir=tmp_path,
        flow="build",
    )
    svc = OrchestratorService(config, db)
    svc.runner = _FakeRunner("fake")
    await svc.start()

    try:
        # Confirm the name is registered (will raise RunnerNotFound pre-implementation).
        resolve_runner_by_name("claude-agent-sdk")

        # Confirm it did not steal the default slot.
        resolved = resolve_runner("completely-unknown-model-xyz")
        assert resolved.name != "claude-agent-sdk", (
            "claude-agent-sdk became the default — it must only be reachable "
            "via resolve_runner_by_name, not as the model-fallback default"
        )
    finally:
        await svc.shutdown()


# ---------------------------------------------------------------------------
# Mixed-runner: _resolve_runner dispatches two steps to different runners
# ---------------------------------------------------------------------------


def test_mixed_runner_two_steps_route_to_different_runners(tmp_path):
    """A process with two agent steps — one with ``runner: my-sdk`` and one with
    no ``runner:`` — resolves each to a different runner via ``_resolve_runner``.

    This is the core per-step routing contract. Drives ``_resolve_runner``
    directly with two ``ResolvedJob``s; no full dispatch cycle needed.

    Fails pre-implementation: both steps hit the same ``resolve_runner(model)``
    path and land on the same (default) runner.
    """
    svc = _make_minimal_service(tmp_path)

    cli_runner = _FakeRunner("cli")
    sdk_runner = _FakeRunner("sdk")
    # Refresh the default slot with our fake so the default path is observable.
    register_runner("default", cli_runner, prefixes=["claude-", "sonnet", "haiku", "opus"], default=True)
    register_runner("my-sdk", sdk_runner)

    # Step 1: no runner → falls through to model-based default resolution.
    step_cli = ResolvedJob(
        name="plan",
        prompt_name="plan",
        resume_session=False,
        evaluate=False,
        queue_state="backlog",
        active_state="plan",
        success_state="code",
        runner=None,
        model=None,
    )
    # Step 2: explicit runner name.
    step_sdk = ResolvedJob(
        name="code",
        prompt_name="code",
        resume_session=False,
        evaluate=False,
        queue_state="code",
        active_state="code",
        success_state="complete",
        runner="my-sdk",
        model=None,
    )

    resolved_cli = svc._resolve_runner(step_cli)
    resolved_sdk = svc._resolve_runner(step_sdk)

    assert resolved_cli.runner is cli_runner, "plan step should use the default CLI runner"
    assert resolved_sdk.runner is sdk_runner, "code step should use the named SDK runner"
    assert resolved_cli.name == "default"
    assert resolved_sdk.name == "my-sdk"
