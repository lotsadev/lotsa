"""Tests for the AgentRunner registry (ADR-023).

The registry maps a model name (or name prefix) to a runner *instance* and
resolves one per dispatch. Resolution order is: exact name → longest prefix
→ default → ``RunnerNotFound``. ``ClaudeCodeRunner`` self-registers as the
``default`` at import time so existing lotsa.yaml files keep working.

These tests are expected to FAIL until the registry is implemented in
``rigg/agent_runner.py`` (``register_runner``, ``resolve_runner``,
``ResolvedRunner``, ``RunnerNotFound``, ``clear_registry``, ``snapshot`` /
``restore``) and exported from ``rigg/__init__``. Pre-implementation
the module fails at import (the names below don't exist yet) — that is the
intended red.

Mirrors the ``lotsa.registry`` (ADR-014) test shape; uses a fake runner
that satisfies the ``AgentRunner`` Protocol structurally — no real
subprocesses (``rigg/CLAUDE.md`` testing rule).
"""

from __future__ import annotations

import logging

import pytest

from rigg import (
    ClaudeCodeRunner,
    ResolvedRunner,
    RunnerNotFound,
    register_runner,
    resolve_runner,
)
from rigg import agent_runner as ar
from rigg.models import AgentResult


class _FakeRunner:
    """Minimal AgentRunner test double (satisfies the Protocol structurally).

    Declares both Protocol members explicitly — ``run`` and
    ``dispatch_shape_prompt`` — because rigg runners are duck-typed and a
    missing method only surfaces at call time.
    """

    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        return AgentResult(success=True, stdout=self.tag, stderr="", return_code=0, duration_ms=1)


@pytest.fixture(autouse=True)
def _isolated_runner_registry():
    """Snapshot/restore the process-global runner registry around every test.

    The registry is module-global; without isolation a test that registers or
    clears runners would pollute later tests (including the import-time
    ``default``). Uses the public ``snapshot()`` / ``restore()`` surface, same
    rationale as ``lotsa.registry``'s test-isolation API.
    """
    snap = ar.snapshot()
    yield
    ar.restore(snap)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_registry_api_is_exported_from_rigg():
    """ADR-023 names ``register_runner`` / ``resolve_runner`` /
    ``RunnerNotFound`` / ``ResolvedRunner`` on rigg's public surface."""
    import rigg

    for name in ("register_runner", "resolve_runner", "RunnerNotFound", "ResolvedRunner"):
        assert name in rigg.__all__, f"{name} missing from rigg.__all__"


# ---------------------------------------------------------------------------
# Registration + resolution (ADR-023 "Resolution rules")
# ---------------------------------------------------------------------------


def test_register_instance_and_resolve_exact_returns_resolved_runner():
    """``resolve_runner`` returns a ``ResolvedRunner`` carrying the registered
    *name* (for audit) and the *instance* (ready to call)."""
    ar.clear_registry()
    fake = _FakeRunner("exact")
    register_runner("gpt-5", fake)

    resolved = resolve_runner("gpt-5")

    assert isinstance(resolved, ResolvedRunner)
    assert resolved.name == "gpt-5"
    # Registry stores the instance, not a class — identity is preserved.
    assert resolved.runner is fake


def test_exact_name_match_wins_over_prefix():
    """Rule 1: an exact name match beats a prefix that also matches."""
    ar.clear_registry()
    prefix_fake = _FakeRunner("prefix")
    exact_fake = _FakeRunner("exact")
    register_runner("gpt-family", prefix_fake, prefixes=["gpt-"])
    register_runner("gpt-5", exact_fake)

    resolved = resolve_runner("gpt-5")

    assert resolved.name == "gpt-5"
    assert resolved.runner is exact_fake


def test_longest_prefix_wins():
    """Rule 2: when two prefixes match, the longest one wins."""
    ar.clear_registry()
    broad = _FakeRunner("broad")
    narrow = _FakeRunner("narrow")
    register_runner("broad", broad, prefixes=["claude-"])
    register_runner("narrow", narrow, prefixes=["claude-opus-"])

    resolved = resolve_runner("claude-opus-4-8")

    assert resolved.name == "narrow"
    assert resolved.runner is narrow


def test_default_fallback_when_no_prefix_matches():
    """Rule 3: no exact and no prefix → the default registration answers."""
    ar.clear_registry()
    default = _FakeRunner("default")
    gpt = _FakeRunner("gpt")
    register_runner("default", default, default=True)
    register_runner("gpt", gpt, prefixes=["gpt-"])

    resolved = resolve_runner("mistral-large")

    assert resolved.name == "default"
    assert resolved.runner is default


def test_runner_not_found_when_no_default_and_no_match():
    """Rule 4: unknown model with no default → ``RunnerNotFound`` naming the
    model and the registered prefixes."""
    ar.clear_registry()
    register_runner("gpt", _FakeRunner("gpt"), prefixes=["gpt-"])

    with pytest.raises(RunnerNotFound) as exc:
        resolve_runner("mistral-large")

    message = str(exc.value)
    assert "mistral-large" in message
    assert "gpt-" in message


# ---------------------------------------------------------------------------
# Collision handling (ADR-023 "Identical-prefix collisions")
# ---------------------------------------------------------------------------


def test_prefix_collision_across_names_warns_and_later_wins(caplog):
    """Two *differently named* runners claiming the same prefix → a WARNING
    naming both runners and the shared prefix; later registration wins."""
    ar.clear_registry()
    register_runner("first", _FakeRunner("first"), prefixes=["gpt-"])

    with caplog.at_level(logging.WARNING, logger="rigg.agent_runner"):
        register_runner("second", _FakeRunner("second"), prefixes=["gpt-"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a collision WARNING for the shared prefix"
    blob = "\n".join(warnings)
    assert "gpt-" in blob
    assert "first" in blob and "second" in blob

    # Last-registered wins for the colliding prefix.
    assert resolve_runner("gpt-9").name == "second"


def test_default_override_same_name_does_not_warn(caplog):
    """Re-registering the ``default`` slot (same name, ADR-028 reconciliation
    path) overrides silently — it is NOT a prefix collision."""
    ar.clear_registry()
    register_runner("default", _FakeRunner("one"), prefixes=["claude-"], default=True)

    with caplog.at_level(logging.WARNING, logger="rigg.agent_runner"):
        second = _FakeRunner("two")
        register_runner("default", second, prefixes=["claude-"], default=True)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert resolve_runner("claude-opus-4").runner is second


def test_same_name_refresh_clears_stale_prefixes():
    """Re-registering an existing name refreshes its prefixes — the old prefix
    no longer routes to it (so the default re-registration is collision-free)."""
    ar.clear_registry()
    runner = _FakeRunner("r")
    register_runner("r", runner, prefixes=["a-"])
    register_runner("r", runner, prefixes=["b-"])

    assert resolve_runner("b-1").name == "r"
    with pytest.raises(RunnerNotFound):
        resolve_runner("a-1")


# ---------------------------------------------------------------------------
# ClaudeCodeRunner default self-registration (ADR-023 Scope step 3)
# ---------------------------------------------------------------------------


def test_claude_code_self_registers_as_default():
    """Import-only path: ``ClaudeCodeRunner`` answers as ``default`` for the
    accepted Claude model names with no ``start()`` call."""
    # No clear_registry() — assert the import-time baseline the fixture captured.
    for model in ("sonnet", "opus", "haiku", "claude-opus-4-8"):
        resolved = resolve_runner(model)
        assert resolved.name == "default", f"{model} did not resolve to the default registration"
        assert isinstance(resolved.runner, ClaudeCodeRunner)


# ---------------------------------------------------------------------------
# Snapshot / restore (test-isolation surface)
# ---------------------------------------------------------------------------


def test_snapshot_restore_roundtrip():
    """``snapshot`` then ``restore`` reinstates exact registry state — including
    prefixes and the default slot."""
    ar.clear_registry()
    runner = _FakeRunner("x")
    register_runner("x", runner, prefixes=["x-"])
    snap = ar.snapshot()

    ar.clear_registry()
    with pytest.raises(RunnerNotFound):
        resolve_runner("x-1")

    ar.restore(snap)
    resolved = resolve_runner("x-1")
    assert resolved.name == "x"
    assert resolved.runner is runner
