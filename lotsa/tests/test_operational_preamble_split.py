"""Tests for the runner-aware operational preamble split (ADR-028 Phase 2).

After the split:
- ``OPERATIONAL_PREAMBLE`` keeps only the universal (task-shape) rules.
- The CLI-shape dispatch text moves into ``CLI_DISPATCH_SHAPE_FRAGMENT``.
- Every concrete runner exposes ``dispatch_shape_prompt()``.
- ``_build_system_prompt`` concatenates
  ``OPERATIONAL_PREAMBLE + runner.dispatch_shape_prompt() + base``.

Symbols that don't exist until Phase 2 are imported locally inside tests so
this module still collects and each test reports its own failure. The
no-CLI-text and integration tests rely only on existing symbols and fail
with a clean AssertionError until the split lands.
"""

from __future__ import annotations

import asyncio

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.docker_runner import DockerAgentRunner
from lotsa.orchestrator import OPERATIONAL_PREAMBLE, OrchestratorService
from rigg.agent_runner import ClaudeCodeRunner
from rigg.models import AgentResult


def _sdk_runner_cls():
    from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

    return ClaudeAgentSDKRunner


# CLI-shape markers that must move OUT of the universal preamble (acceptance #4).
CLI_DISPATCH_MARKERS = [
    "claude --print",
    "one-shot",
    "Monitor",
    "ScheduleWakeup",
    "BashOutput",
    "AskUserQuestion",
    "Bash background",
]


# ---------------------------------------------------------------------------
# The preamble partition (acceptance #4)
# ---------------------------------------------------------------------------


def test_preamble_no_longer_contains_cli_dispatch_text():
    """After the split, the universal preamble carries none of the CLI-shape
    dispatch text — it lives in CLI_DISPATCH_SHAPE_FRAGMENT instead."""
    for marker in CLI_DISPATCH_MARKERS:
        assert marker not in OPERATIONAL_PREAMBLE, f"{marker!r} should have moved into CLI_DISPATCH_SHAPE_FRAGMENT"


def test_preamble_keeps_universal_sections():
    """The task-shape rules stay universal across runner shapes."""
    assert "take precedence over project" in OPERATIONAL_PREAMBLE
    assert "Git authority" in OPERATIONAL_PREAMBLE
    assert "File scope" in OPERATIONAL_PREAMBLE
    assert "How to communicate with the operator" in OPERATIONAL_PREAMBLE
    assert "NEEDS_INPUT" in OPERATIONAL_PREAMBLE


# ---------------------------------------------------------------------------
# dispatch_shape_prompt() across every concrete runner (acceptance #5)
# ---------------------------------------------------------------------------


def test_all_runners_expose_dispatch_shape_prompt():
    """No runner raises AttributeError — the Protocol method is declared
    explicitly on each structural implementer."""
    SDK = _sdk_runner_cls()
    runners = [ClaudeCodeRunner(), DockerAgentRunner(), SDK()]
    for runner in runners:
        frag = runner.dispatch_shape_prompt()
        assert isinstance(frag, str)
        assert frag.strip()


def test_cli_and_docker_fragments_carry_cross_turn_restrictions():
    """CLI and Docker are both CLI-shaped — their fragment names the
    cross-turn tools that fail under ``--print``."""
    cli = ClaudeCodeRunner().dispatch_shape_prompt()
    docker = DockerAgentRunner().dispatch_shape_prompt()
    for frag in (cli, docker):
        assert "Monitor" in frag
        assert "ScheduleWakeup" in frag


def test_sdk_fragment_does_not_falsely_advertise_cross_turn_tools():
    """The SDK fragment must be honest to wired capability — interception
    isn't built in this cut (acceptance #5 / requirement #13)."""
    frag = _sdk_runner_cls()().dispatch_shape_prompt()
    for phrase in (
        "Lotsa routes it to the dashboard",
        "Lotsa polls and re-engages",
        "Lotsa fires it via SDK resume",
    ):
        assert phrase not in frag, f"SDK fragment falsely advertises un-wired capability: {phrase!r}"


# ---------------------------------------------------------------------------
# Assembled CLI prompt equivalence (acceptance #4)
# ---------------------------------------------------------------------------


def test_assembled_cli_system_prompt_preserves_all_sections():
    """The CLI runner's assembled prompt (universal preamble + CLI fragment)
    still contains every section the pre-refactor preamble had — no behaviour
    change for existing runners."""
    from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

    assembled = OPERATIONAL_PREAMBLE + "\n\n" + ClaudeCodeRunner().dispatch_shape_prompt()

    expected_sections = [
        "Lotsa Operational Rules",
        "How to communicate with the operator",
        "Git authority",
        "File scope",
        "Your environment",
        "Execution patterns",
        "NEEDS_INPUT",
        "claude --print",
        "Monitor",
        "AskUserQuestion",
    ]
    for section in expected_sections:
        assert section in assembled, f"assembled CLI prompt lost section/marker: {section!r}"

    # The runner fragment is appended after the universal preamble.
    assert assembled.endswith(CLI_DISPATCH_SHAPE_FRAGMENT)


# ---------------------------------------------------------------------------
# Integration: _build_system_prompt injects the runner's dispatch shape
# ---------------------------------------------------------------------------


class _ShapedFakeRunner:
    """A fake runner whose dispatch_shape_prompt() returns a unique sentinel,
    so we can assert the orchestrator injected it into the system prompt."""

    SENTINEL = "<<<DISPATCH-SHAPE-SENTINEL>>>"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, **kwargs})
        return AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            return_code=0,
            duration_ms=1,
            session_id="s",
        )

    def dispatch_shape_prompt(self) -> str:
        return self.SENTINEL


@pytest.mark.asyncio
async def test_build_system_prompt_injects_runner_dispatch_shape(tmp_path):
    """The dispatched agent's system prompt starts with the universal preamble
    and contains the active runner's dispatch-shape fragment."""
    data_dir = tmp_path / "tasks"
    data_dir.mkdir()
    flow_yaml = tmp_path / "test_flow.yaml"
    flow_yaml.write_text("name: test\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=data_dir.parent,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(data_dir / "lotsa.db")
    await db.initialize()

    svc = OrchestratorService(config, db)
    svc.runner = _ShapedFakeRunner()
    await svc.start()
    try:
        await svc.create_task("Build feature")
        await asyncio.sleep(0.1)  # let the drainer dispatch

        assert svc.runner.calls, "agent was not dispatched"
        system_prompt = svc.runner.calls[0]["system_prompt"]
        assert _ShapedFakeRunner.SENTINEL in system_prompt, (
            "orchestrator did not inject runner.dispatch_shape_prompt() into the system prompt"
        )
        assert system_prompt.startswith(OPERATIONAL_PREAMBLE)
    finally:
        await svc.shutdown()
        await db.close()
