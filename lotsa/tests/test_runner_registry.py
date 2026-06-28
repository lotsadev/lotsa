"""CE-side tests for the multi-provider AgentRunner registry (ADR-023).

Covers the edition seam the rigg unit tests can't: the ``runners:``
block on ``LotsaConfig``, the orchestrator resolving a runner per dispatch
(instead of one ``self.runner``), the ``agent_runner`` audit field, and the
ADR-028 reconciliation (``--docker`` / config-derived default still selects
the runner *shape*).

These are expected to FAIL until ADR-023 is implemented:
- ``LotsaConfig`` has no ``runners`` field yet (``AttributeError`` /
  YAML key dropped).
- ``rigg.register_runner`` / ``resolve_runner`` don't exist yet
  (``ImportError`` inside the integration tests).
- ``output_meta`` has no ``agent_runner`` key yet (``assert`` fail).

The audit test is a regression guard per CE discipline: it must fail
against pre-change code (no ``agent_runner`` key), which it does.

The orchestrator harness mirrors ``test_orchestrator.py``'s ``service``
fixture: a single-step ``evaluate`` flow, temp SQLite DB (never mocked),
driven through the ``run`` event-loop fixture from ``conftest.py``.
"""

from __future__ import annotations

import asyncio

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from rigg.models import AgentResult


class _FakeRunner:
    """Controllable AgentRunner test double routed in via the registry."""

    def __init__(self, tag: str = "agent output") -> None:
        self.tag = tag

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        return AgentResult(success=True, stdout=self.tag, stderr="", return_code=0, duration_ms=1)


class _UserRunner:
    """Stand-in for a third-party runner registered via ``lotsa.yaml``.

    The construction-arg contract (ADR-023 §3): user runner ctors must accept
    ``model`` / ``budget_usd`` / ``max_output_tokens`` passed through from
    config. Accepting them here exercises that contract through ``start()``.
    """

    def __init__(self, model=None, budget_usd=None, max_output_tokens=None, **kwargs) -> None:
        self.model = model

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        return AgentResult(success=True, stdout="user runner", stderr="", return_code=0, duration_ms=1)


def _make_config(tmp_path) -> tuple[LotsaConfig, TaskDB]:
    """Single-step (evaluate gate) flow + temp DB, mirroring the orchestrator
    ``service`` fixture in ``test_orchestrator.py``."""
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
    return config, db


# ---------------------------------------------------------------------------
# Config — ``runners:`` block (ADR-023 §5)
# ---------------------------------------------------------------------------


def test_lotsa_config_has_runners_field_defaulting_empty():
    """``LotsaConfig`` carries a ``runners`` dict (alongside tools/engines),
    defaulting to empty so a config with no block behaves as today."""
    config = LotsaConfig()
    assert config.runners == {}


def test_lotsa_config_loads_runners_block_from_yaml(tmp_path):
    """A ``runners:`` block in lotsa.yaml parses into ``config.runners``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    yaml_path = data_dir / "lotsa.yaml"
    yaml_path.write_text(
        "model: sonnet\n"
        "runners:\n"
        "  gpt:\n"
        "    handler: lotsa_runners.codex:CodexCliRunner\n"
        "    prefixes: [gpt-, openai/]\n"
    )

    config = LotsaConfig.load(config_path=yaml_path)

    assert config.runners["gpt"]["handler"] == "lotsa_runners.codex:CodexCliRunner"
    assert config.runners["gpt"]["prefixes"] == ["gpt-", "openai/"]


def test_bare_runners_line_does_not_clobber_default(tmp_path):
    """A bare ``runners:`` (YAML-null) must not clobber the empty-dict default
    — same null-normalisation that protects ``tools:`` / ``engines:``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    yaml_path = data_dir / "lotsa.yaml"
    yaml_path.write_text("model: sonnet\nrunners:\n")

    config = LotsaConfig.load(config_path=yaml_path)

    assert config.runners == {}


# ---------------------------------------------------------------------------
# Orchestrator wiring + audit (ADR-023 §3, §4)
# ---------------------------------------------------------------------------


def test_output_message_records_agent_runner(tmp_path, run):
    """Audit: a non-conversational step's output metadata records BOTH
    ``agent_model`` (existing) and ``agent_runner`` (new registered name).

    Regression guard — pre-change code writes ``agent_model`` only, so the
    ``agent_runner`` assertion fails against the current tree.
    """
    from rigg import agent_runner as ar
    from rigg import register_runner

    snap = ar.snapshot()
    config, db = _make_config(tmp_path)
    run(db.initialize())
    svc = OrchestratorService(config, db)
    try:
        run(svc.start())
        # Override the registry default so dispatch routes to a controllable
        # fake instead of execing the real ``claude`` CLI. ``"sonnet"`` matches
        # the default's prefix, so resolution records the name ``"default"``.
        register_runner(
            "default",
            _FakeRunner("agent output"),
            prefixes=["claude-", "sonnet", "opus", "haiku"],
            default=True,
        )
        run(svc.create_task("audit runner metadata"))
        run(asyncio.sleep(0.3))

        task = run(svc.list_tasks_async())[0]
        messages = run(svc.db.get_messages(task.id, msg_type="output"))
        assert messages, "no output message persisted"
        meta = messages[-1].metadata
        assert meta.get("agent_model") == "sonnet"
        assert meta.get("agent_runner") == "default"
    finally:
        run(svc.shutdown())
        run(db.close())
        ar.restore(snap)


def test_no_runners_block_keeps_default_runner(tmp_path, run):
    """No ``runners:`` block → the config-derived default answers every model
    (ADR-023 acceptance: existing lotsa.yaml files see no behaviour change)."""
    from rigg import ClaudeCodeRunner, resolve_runner
    from rigg import agent_runner as ar

    snap = ar.snapshot()
    config, db = _make_config(tmp_path)  # config.runners == {}
    run(db.initialize())
    svc = OrchestratorService(config, db)
    try:
        run(svc.start())
        resolved = resolve_runner("sonnet")
        assert resolved.name == "default"
        # No --docker / --runner → the default shape is the CLI ClaudeCodeRunner.
        assert isinstance(resolved.runner, ClaudeCodeRunner)
    finally:
        run(svc.shutdown())
        run(db.close())
        ar.restore(snap)


def test_user_runner_from_lotsa_yaml_routes_by_prefix(tmp_path, run):
    """A ``runners:`` entry is imported, constructed, and registered for its
    prefixes; resolution routes a matching model to it under its declared name."""
    from rigg import agent_runner as ar
    from rigg import resolve_runner

    snap = ar.snapshot()
    config, db = _make_config(tmp_path)
    config.runners = {
        "gpt": {
            "handler": "lotsa.tests.test_runner_registry:_UserRunner",
            "prefixes": ["gpt-", "openai/"],
        }
    }
    run(db.initialize())
    svc = OrchestratorService(config, db)
    try:
        run(svc.start())
        resolved = resolve_runner("gpt-5")
        assert resolved.name == "gpt"
        assert isinstance(resolved.runner, _UserRunner)
        # The built-in default still answers Claude models unchanged.
        assert resolve_runner("sonnet").name == "default"
    finally:
        run(svc.shutdown())
        run(db.close())
        ar.restore(snap)


def test_docker_default_runner_shape_preserved(tmp_path, run):
    """ADR-028 reconciliation: ``--docker`` still selects the default runner
    *shape* — ``start()`` re-registers ``_build_runner(config)`` as default."""
    from rigg import agent_runner as ar
    from rigg import resolve_runner

    snap = ar.snapshot()
    config, db = _make_config(tmp_path)
    config.docker = True
    run(db.initialize())
    svc = OrchestratorService(config, db)
    try:
        run(svc.start())
        resolved = resolve_runner("sonnet")
        assert resolved.name == "default"
        assert type(resolved.runner).__name__ == "DockerAgentRunner"
    finally:
        run(svc.shutdown())
        run(db.close())
        ar.restore(snap)
