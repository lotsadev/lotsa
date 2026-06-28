"""Tests for ``resolve_runner_by_name`` — ADR-028 Phase 3.

``resolve_runner_by_name`` is an exact-name-only resolver: it looks up a
name in the runner registry and raises ``RunnerNotFound`` on a miss, with
NO prefix or default fallback. An operator who names a runner explicitly
that doesn't resolve has a configuration error, not an invitation to fall
back to the default (the stringly-typed misroute ADR-023's tradeoffs call
out).

These tests FAIL until ``resolve_runner_by_name`` is implemented in
``rigg/agent_runner.py`` and exported from ``rigg/__init__.py``.
Pre-implementation the import fails (the name doesn't exist yet) — that is
the intended red.

Mirrors the existing ``test_runner_registry.py`` shape: uses a fake runner
satisfying the ``AgentRunner`` Protocol structurally, and wraps each test
in the module-level snapshot/restore fixture.
"""

from __future__ import annotations

import pytest

from rigg import (
    RunnerNotFound,
    register_runner,
    resolve_runner_by_name,  # the new export — NameError/ImportError until implemented
)
from rigg import agent_runner as ar
from rigg.models import AgentResult


class _FakeRunner:
    """Minimal AgentRunner test double (satisfies the Protocol structurally)."""

    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        return AgentResult(success=True, stdout=self.tag, stderr="", return_code=0, duration_ms=1)


@pytest.fixture(autouse=True)
def _isolated_runner_registry():
    """Snapshot/restore the process-global runner registry around every test."""
    snap = ar.snapshot()
    yield
    ar.restore(snap)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_resolve_runner_by_name_is_exported_from_rigg():
    """ADR-028 Phase 3 names ``resolve_runner_by_name`` on rigg's public surface."""
    import rigg

    assert "resolve_runner_by_name" in rigg.__all__, "resolve_runner_by_name missing from rigg.__all__"


# ---------------------------------------------------------------------------
# Exact-name hit
# ---------------------------------------------------------------------------


def test_resolve_runner_by_name_exact_hit_returns_resolved_runner():
    """An exact registered name resolves to the right (name, instance) pair."""
    ar.clear_registry()
    fake = _FakeRunner("named")
    register_runner("my-runner", fake)

    resolved = resolve_runner_by_name("my-runner")

    assert resolved.name == "my-runner"
    assert resolved.runner is fake


def test_resolve_runner_by_name_returns_most_recently_registered_instance():
    """Re-registering a name (refresh) makes the new instance the hit."""
    ar.clear_registry()
    old = _FakeRunner("old")
    new = _FakeRunner("new")
    register_runner("r", old)
    register_runner("r", new)

    resolved = resolve_runner_by_name("r")

    assert resolved.runner is new


# ---------------------------------------------------------------------------
# Miss → RunnerNotFound (no prefix fallback, no default fallback)
# ---------------------------------------------------------------------------


def test_resolve_runner_by_name_miss_raises_runner_not_found():
    """An unregistered name raises ``RunnerNotFound`` — not a silent default fallback."""
    ar.clear_registry()
    register_runner("default", _FakeRunner("default"), prefixes=["claude-"], default=True)

    with pytest.raises(RunnerNotFound):
        resolve_runner_by_name("typo-runner")


def test_resolve_runner_by_name_miss_message_lists_registered_names():
    """The ``RunnerNotFound`` message names the registered runners so an operator
    can immediately spot a typo without reading the registry source."""
    ar.clear_registry()
    register_runner("alpha", _FakeRunner("a"))
    register_runner("beta", _FakeRunner("b"))

    with pytest.raises(RunnerNotFound) as exc:
        resolve_runner_by_name("nonexistent")

    message = str(exc.value)
    assert "alpha" in message
    assert "beta" in message
    assert "nonexistent" in message


def test_resolve_runner_by_name_does_not_fall_back_to_default():
    """``resolve_runner_by_name`` must NOT fall back to the default registration
    on a miss — that would silently misroute a typo'd runner: field.

    ``resolve_runner`` (model-based) does fall back; ``resolve_runner_by_name``
    must not. This test pins that contract: if the implementation reused
    ``resolve_runner`` internally, the miss would silently return the default
    runner rather than raising.
    """
    ar.clear_registry()
    default_fake = _FakeRunner("default")
    register_runner("default", default_fake, default=True)

    with pytest.raises(RunnerNotFound):
        resolve_runner_by_name("not-registered")


def test_resolve_runner_by_name_does_not_use_prefix_matching():
    """``resolve_runner_by_name`` is exact-only: a name that is a PREFIX of a
    registered name is a miss, not a hit."""
    ar.clear_registry()
    register_runner("claude-agent-sdk", _FakeRunner("sdk"))

    with pytest.raises(RunnerNotFound):
        resolve_runner_by_name("claude-agent")  # prefix of "claude-agent-sdk", not exact


def test_resolve_runner_by_name_prefix_owner_by_name_is_an_exact_hit():
    """A name that was registered AND declared as a prefix owner resolves exactly."""
    ar.clear_registry()
    fake = _FakeRunner("p")
    register_runner("gpt-5", fake, prefixes=["gpt-"])

    resolved = resolve_runner_by_name("gpt-5")

    assert resolved.name == "gpt-5"
    assert resolved.runner is fake
