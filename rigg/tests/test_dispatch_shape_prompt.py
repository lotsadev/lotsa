"""Tests for the runner-aware preamble seam in rigg (ADR-028 Phase 2).

Covers the rigg-side pieces:
- ``CLI_DISPATCH_SHAPE_FRAGMENT`` constant (public, crosses the lotsa↔rigg
  boundary so it must be on ``__all__``).
- ``ClaudeCodeRunner.dispatch_shape_prompt()`` returning that fragment.
- ``ClaudeAgentSDKRunner`` / ``CLI_DISPATCH_SHAPE_FRAGMENT`` re-exports.

References to not-yet-existing symbols go through local imports so the
module still collects and each test reports its own failure until Phase 2
lands. The assembled-prompt equivalence test that needs ``OPERATIONAL_PREAMBLE``
lives in ``lotsa/tests`` (rigg tests stay edition-agnostic).
"""

from __future__ import annotations

import rigg


def _fragment():
    from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

    return CLI_DISPATCH_SHAPE_FRAGMENT


def test_cli_fragment_is_nonempty_str():
    frag = _fragment()
    assert isinstance(frag, str)
    assert frag.strip()


def test_cli_fragment_exported_from_rigg():
    """The constant crosses the lotsa↔rigg boundary, so it must be on
    the public surface (``__all__``)."""
    from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

    assert hasattr(rigg, "CLI_DISPATCH_SHAPE_FRAGMENT")
    assert rigg.CLI_DISPATCH_SHAPE_FRAGMENT is CLI_DISPATCH_SHAPE_FRAGMENT
    assert "CLI_DISPATCH_SHAPE_FRAGMENT" in rigg.__all__


def test_cli_fragment_names_cross_turn_tools():
    """The CLI-shape restrictions (cross-turn tools that fail under ``--print``)
    must live in the fragment now that they're out of the universal preamble."""
    frag = _fragment()
    for tool in ("Monitor", "ScheduleWakeup", "Task", "BashOutput", "AskUserQuestion"):
        assert tool in frag, f"{tool} not named in CLI dispatch-shape fragment"


def test_cli_fragment_carries_dispatch_shape_text():
    frag = _fragment()
    assert "claude --print" in frag
    assert "one-shot" in frag


def test_claude_code_runner_dispatch_shape_prompt_returns_cli_fragment():
    from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT, ClaudeCodeRunner

    assert ClaudeCodeRunner().dispatch_shape_prompt() == CLI_DISPATCH_SHAPE_FRAGMENT


def test_claude_agent_sdk_runner_exported():
    from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

    assert hasattr(rigg, "ClaudeAgentSDKRunner")
    assert rigg.ClaudeAgentSDKRunner is ClaudeAgentSDKRunner
    assert "ClaudeAgentSDKRunner" in rigg.__all__
