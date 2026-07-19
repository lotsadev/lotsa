"""Shared fixtures for lotsa tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService
from lotsa.server.app import create_app
from lotsa.tests.test_orchestrator import full_service  # noqa: F401 — re-exported fixture
from rigg.models import AgentResult


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot/restore the global tool/engine/override registry around every test.

    The registry is process-global; without isolation, a test that registers
    or overrides a tool would pollute every subsequent test in the suite.
    Per-test (function-scoped) snapshot keeps the cost negligible (a dict
    copy of ~5 entries) and ensures hermetic isolation for any test that
    touches the registry — directly or transitively via ``OrchestratorService``.

    Importing ``lotsa.tools`` / ``lotsa.engines`` BEFORE the snapshot ensures
    built-ins are part of the baseline; otherwise the first test in a fresh
    session would capture an empty registry and restoration would permanently
    strip the built-ins from every later test. Imports are cached after the
    first call so this is a no-op on subsequent fixture entries.

    Uses the public ``snapshot()`` / ``restore()`` API so a future rename of
    the underlying ``_TOOLS`` / ``_ENGINES`` storage dicts doesn't break the
    fixture silently — the public surface is the API contract.
    """
    import lotsa.engines  # noqa: F401 — registers built-ins
    import lotsa.overrides  # noqa: F401 — registers built-in override handlers (ADR-019)
    import lotsa.posthooks  # noqa: F401 — registers built-in posthooks (commit)
    import lotsa.prehooks  # noqa: F401 — registers built-in prehooks (worktree, ADR-044 P3)
    import lotsa.tools  # noqa: F401 — registers built-ins
    from lotsa import overrides as ovr
    from lotsa import registry as reg
    from rigg import agent_runner as ar  # import-time default runner registration

    snap = reg.snapshot()
    ovr_snap = ovr.snapshot()
    # ADR-023 — the agent-runner registry (rigg) is a second process-global
    # registry. ``OrchestratorService.start()`` re-registers its ``default`` slot,
    # so isolate it too or one test's runner leaks into the next.
    runner_snap = ar.snapshot()
    yield
    reg.restore(snap)
    ovr.restore(ovr_snap)
    ar.restore(runner_snap)


@pytest.fixture(autouse=True)
def _fast_shutdown_drain(request, monkeypatch):
    """Shrink the ADR-040 graceful-drain window for the test suite.

    ``shutdown()`` now awaits in-flight agents up to
    ``config.shutdown_grace_seconds`` (default 30s in production) before
    cancelling them. Many tests deliberately freeze an agent in-flight (a
    never-set ``asyncio.Event``) and rely on teardown cancelling it — with the
    30s default each such teardown would stall the full window. Patching the
    module-level default to a tiny value keeps teardown fast without weakening
    any assertion. ``test_config`` is exempt: it verifies the shipped 30s
    default. Tests that assert on the drain window itself set
    ``config.shutdown_grace_seconds`` explicitly on the instance, which wins
    over this default.
    """
    if not request.module.__name__.endswith("test_config"):
        import lotsa.config as _cfg

        monkeypatch.setattr(_cfg, "_DEFAULT_SHUTDOWN_GRACE_SECONDS", 0.05, raising=False)
    yield


@pytest.fixture
def tasks_dir(tmp_path: Path) -> Path:
    """Legacy fixture name retained for tests that still use it as a workspace.

    No longer reflects a LotsaConfig field — lotsa.yaml lives next to the
    DB under ``data_dir`` now. Tests that need a workspace path should
    prefer ``tmp_path`` directly.
    """
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def sample_task(tasks_dir: Path) -> Path:
    """Create a sample task YAML file in the test workspace."""
    task_file = tasks_dir / "sample.yaml"
    task_file.write_text(
        yaml.dump(
            {
                "title": "Sample task",
                "state": "backlog",
                "priority": 1,
                "body": "Do the thing.\n",
            },
            default_flow_style=False,
            sort_keys=False,
        )
    )
    return task_file


async def wait_for_completion(service: OrchestratorService, task_id: str, timeout: float = 2.0):
    """Wait until a task leaves the in-flight state."""
    for _ in range(int(timeout / 0.05)):
        if task_id not in service._in_flight:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Task {task_id} still in-flight after {timeout}s")


async def wait_for_status(service: OrchestratorService, task_id: str, expected: str, timeout: float = 2.0):
    """Wait until task.status equals *expected* in the DB."""
    for _ in range(int(timeout / 0.05)):
        row = await service.db.get_task(task_id)
        if row is not None and row.status == expected:
            return
        await asyncio.sleep(0.05)
    final = await service.db.get_task(task_id)
    raise TimeoutError(
        f"Task {task_id} status={final.status if final else None!r} never reached {expected!r} within {timeout}s"
    )


class FakeRunner:
    """Mock agent runner that returns immediately."""

    def __init__(self):
        self.result = AgentResult(success=True, stdout="Agent output", stderr="", return_code=0, duration_ms=500)

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        return self.result


def make_server_config(tmp_path: Path) -> LotsaConfig:
    """Create a LotsaConfig for server/integration tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    flow_yaml = tmp_path / "test_flow.yaml"
    # The generic ``coding`` step is just "an agent step" for these
    # orchestrator/server tests — not the real coding agent's commit behaviour.
    # Under ADR-044 Phase 2 ``coding`` (``produces_changes: true``) would derive
    # a ``commit`` posthook that fails against the non-git test work_dir; the
    # binding-level ``posthooks: []`` override suppresses it (the documented
    # seam). Commit-posthook behaviour is covered by test_commit_posthook_*.
    flow_yaml.write_text(
        "name: test\n"
        "jobs:\n"
        "  - name: coding\n"
        "    evaluate: true\n"
        "flows:\n"
        "  main:\n"
        "    steps:\n"
        "      - name: coding\n"
        "        posthooks: []\n"
    )
    return LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )


@pytest.fixture()
def _loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def run(_loop):
    return _loop.run_until_complete


@pytest.fixture()
def app_with_service(tmp_path, _loop, run):
    """Create app with service manually initialized (bypassing lifespan)."""
    config = make_server_config(tmp_path)
    app = create_app(config)

    db = TaskDB(config.data_dir / "lotsa.db")
    run(db.initialize())

    service = OrchestratorService(config, db)
    service.runner = FakeRunner()
    run(service.start())

    app.state.service = service
    app.state.db = db

    yield app, service

    run(service.shutdown())
    run(db.close())
