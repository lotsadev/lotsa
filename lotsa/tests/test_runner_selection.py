"""Tests for global runner selection (ADR-028 Phase 1).

Covers ``LotsaConfig.runner`` and ``_build_runner``'s new SDK branch. The
SDK runner class is imported locally so this module still collects and each
test reports its own failure until Phase 1 lands.
"""

from __future__ import annotations

import pytest
import yaml

from lotsa.config import LotsaConfig
from lotsa.docker_runner import DockerAgentRunner
from lotsa.orchestrator import _build_runner
from rigg.agent_runner import ClaudeCodeRunner


def _sdk_runner_cls():
    from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

    return ClaudeAgentSDKRunner


# ---------------------------------------------------------------------------
# LotsaConfig.runner field
# ---------------------------------------------------------------------------


def test_runner_field_default_is_none():
    """Unset ``runner`` keeps today's CLI/Docker selection behaviour."""
    assert LotsaConfig().runner is None


def test_runner_loads_from_yaml(tmp_path):
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"runner": "claude-agent-sdk"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.runner == "claude-agent-sdk"


def test_runner_cli_override(tmp_path):
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"runner": None}))
    config = LotsaConfig.load(config_path=config_file, runner="claude-agent-sdk")
    assert config.runner == "claude-agent-sdk"


# ---------------------------------------------------------------------------
# _build_runner selection
# ---------------------------------------------------------------------------


def test_build_runner_defaults_to_claude_code():
    """Unset runner + no docker → today's CLI runner (non-regression)."""
    config = LotsaConfig(model="sonnet", budget=5.0)
    runner = _build_runner(config)
    assert isinstance(runner, ClaudeCodeRunner)


def test_build_runner_returns_docker_when_docker_set():
    """Docker selection is unchanged (non-regression)."""
    config = LotsaConfig(model="sonnet", budget=5.0, docker=True)
    runner = _build_runner(config)
    assert isinstance(runner, DockerAgentRunner)


def test_build_runner_returns_sdk_runner_when_selected():
    SDK = _sdk_runner_cls()
    config = LotsaConfig(model="sonnet", budget=5.0, runner="claude-agent-sdk")
    runner = _build_runner(config)
    assert isinstance(runner, SDK)


def test_build_runner_sdk_runner_overrides_docker():
    """--runner=claude-agent-sdk wins over --docker (documented override). If the
    runner/docker branch order in _build_runner were swapped, Docker would
    silently win — this pins the precedence."""
    SDK = _sdk_runner_cls()
    config = LotsaConfig(model="sonnet", budget=5.0, docker=True, runner="claude-agent-sdk")
    runner = _build_runner(config)
    assert isinstance(runner, SDK)
    assert not isinstance(runner, DockerAgentRunner)


def test_build_runner_passes_model_and_budget_to_sdk_runner():
    SDK = _sdk_runner_cls()
    config = LotsaConfig(model="opus", budget=12.0, max_output_tokens=4096, runner="claude-agent-sdk")
    runner = _build_runner(config)
    assert isinstance(runner, SDK)
    # A forwarding bug in _build_runner would otherwise pass silently — assert
    # the config values actually reach the runner, not just the runner type.
    assert runner._model == "opus"
    assert runner._budget_usd == 12.0
    assert runner._max_output_tokens == 4096


def test_build_runner_rejects_unknown_runner():
    """A typo'd ``--runner`` value must surface immediately, not silently
    fall through to the CLI runner."""
    config = LotsaConfig(model="sonnet", budget=5.0, runner="nope-not-a-runner")
    with pytest.raises(ValueError):
        _build_runner(config)
