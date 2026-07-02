"""Failing tests for the Execute-side of the two-phase model (ADR-043).

Covers the operator ``mark_complete`` terminal action + ``awaiting_operator``
parked status (plan §5, acceptance #8), the chat→build spec carry (plan §4/§6,
acceptance #9), and legacy-row routing on restart (plan §9, acceptance #10).

Written RED — before the implementation:

* ``OrchestratorService.mark_complete`` does not exist → ``AttributeError``.
* ``MarkCompleteNotAllowed`` is not importable → ``ImportError`` (imported
  lazily inside the one test that needs it, so it fails independently).
* ``awaiting_operator`` is not in the status enum.
* ``POST /tasks/{id}/mark-complete`` is unrouted → 404.
* A task persisted under ``full`` is not re-routed to ``blocked`` on restart
  today (``full`` still loads), so it stays ``waiting``.
* The ``build``/``chat`` spec-carry prompt surfaces don't exist yet.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import BUNDLED_PROMPTS, build_process
from lotsa.orchestrator import OrchestratorService
from lotsa.status import ALL_STATUSES, TaskStatus
from lotsa.tests.conftest import FakeRunner, wait_for_completion


def _catalog_service(tmp_path, run):
    """An OrchestratorService on the bundled catalog (no inline processes, no
    flow-file) with a FakeRunner. Mirrors the ADR-034 catalog-path helper."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        model="sonnet",
        budget=5.0,
        config_path=tmp_path / "lotsa.yaml",
    )
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    return svc


# ───────────────────────────────────────────────────────────────────────────
# awaiting_operator parked status (plan §5, acceptance #8)
# ───────────────────────────────────────────────────────────────────────────


def test_awaiting_operator_is_a_known_status():
    """The parked 'awaiting you' status must be a recognized enum member.

    Fails pre-impl: the status model is eight-valued; ``awaiting_operator``
    is absent.
    """
    assert "awaiting_operator" in ALL_STATUSES


def test_taskstatus_exposes_awaiting_operator_constant():
    """Fails pre-impl: ``TaskStatus.AWAITING_OPERATOR`` raises AttributeError."""
    assert TaskStatus.AWAITING_OPERATOR == "awaiting_operator"


# ───────────────────────────────────────────────────────────────────────────
# mark_complete terminal action (plan §5, acceptance #8)
# ───────────────────────────────────────────────────────────────────────────


def test_mark_complete_drives_non_terminal_task_to_complete(tmp_path, run):
    """The operator escape hatch: mark_complete moves a parked, non-terminal
    task to the terminal ``complete`` status via atomic_transition.

    Fails pre-impl: ``mark_complete`` does not exist → AttributeError.
    """
    svc = _catalog_service(tmp_path, run)
    run(svc.start())
    try:
        # Arrange a task parked at a non-terminal Execute state (the precondition,
        # not the post-bug outcome — the outcome under test is 'complete').
        task = run(
            svc.db.create_task(
                "Escape hatch",
                state="reviewing",
                status="waiting",
                current_step="review",
                metadata={"process_name": "build"},
            )
        )

        run(svc.mark_complete(task.id))

        row = run(svc.db.get_task(task.id))
        assert row.status == "complete"
        assert row.state == "complete"
        assert row.current_step is None
        # A terminal audit row naming the operator action is written on the win.
        msgs = run(svc.db.get_messages(task.id))
        assert any(
            m.type == "status_change" and ("complete" in m.content.lower() or "operator" in m.content.lower())
            for m in msgs
        ), "mark_complete must write a status_change audit row"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_mark_complete_rejects_already_terminal_task(tmp_path, run):
    """mark_complete on an already-terminal task raises MarkCompleteNotAllowed.

    Fails pre-impl: the exception type does not exist (ImportError), and the
    method does not exist.
    """
    from lotsa.orchestrator import MarkCompleteNotAllowed  # lazy: not yet defined

    svc = _catalog_service(tmp_path, run)
    run(svc.start())
    try:
        task = run(
            svc.db.create_task(
                "Already done",
                state="complete",
                status="complete",
                metadata={"process_name": "build"},
            )
        )
        with pytest.raises(MarkCompleteNotAllowed):
            run(svc.mark_complete(task.id))
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_mark_complete_api_route_completes_task(app_with_service, run):
    """POST /api/tasks/{id}/mark-complete drives the task terminal and returns
    the full task detail.

    Fails pre-impl: the route is unrouted → 404.
    """
    app, service = app_with_service

    async def _test():
        task = await service.create_task("API mark complete")
        await wait_for_completion(service, task.id)
        # Precondition: not already terminal.
        row = await service.db.get_task(task.id)
        assert row.status not in ("complete", "abandoned", "archived")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task.id}/mark-complete")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["task"]["status"] == "complete"

    run(_test())


# ───────────────────────────────────────────────────────────────────────────
# Legacy-row routing on restart (plan §9, acceptance #10)
# ───────────────────────────────────────────────────────────────────────────


def test_legacy_process_row_routes_to_blocked_on_restart(tmp_path, run):
    """A task persisted under a removed process name lands in ``blocked`` with a
    recovery message after restart (clean break — no aliasing).

    Fails pre-impl: ``full`` still loads, so the row is recognized and left at
    ``waiting`` rather than routed to ``blocked``.
    """
    # Seed the row BEFORE the service starts, so start()'s recovery sweep sees it.
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    legacy = run(
        db.create_task(
            "Persisted under full",
            state="planned",
            status="waiting",
            current_step="plan",
            metadata={"process_name": "full"},
        )
    )
    run(db.close())

    svc = _catalog_service(tmp_path, run)  # reuses the same data_dir/DB
    run(svc.start())
    try:
        row = run(svc.db.get_task(legacy.id))
        assert row.status == "blocked", (
            f"a row under the removed 'full' process must be routed to blocked; got {row.status!r}"
        )
        msgs = run(svc.db.get_messages(legacy.id))
        assert any("full" in m.content.lower() for m in msgs), "the recovery message must name the removed process"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


def test_non_legacy_row_is_untouched_by_restart_routing(tmp_path, run):
    """A row under a current process (``build``) parked at a gate is NOT flipped
    to blocked by the legacy-routing sweep."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    current = run(
        db.create_task(
            "Under build",
            state="reviewing",
            status="waiting",
            current_step="review",
            metadata={"process_name": "build"},
        )
    )
    run(db.close())

    svc = _catalog_service(tmp_path, run)
    run(svc.start())
    try:
        row = run(svc.db.get_task(current.id))
        assert row.status == "waiting", f"a current-process gate row must survive restart; got {row.status!r}"
    finally:
        run(svc.shutdown())
        run(svc.db.close())


# ───────────────────────────────────────────────────────────────────────────
# Chat→Build spec carry, prompt-level (plan §4/§6, acceptance #9)
# ───────────────────────────────────────────────────────────────────────────


def test_chat_prompt_offers_spec_distillation():
    """chat can, on request, distill a spec before handoff (acceptance #9).

    Fails pre-impl: chat-system.md carries no spec-distillation guidance today.
    """
    text = (BUNDLED_PROMPTS / "chat" / "chat-system.md").read_text().lower()
    assert "spec" in text, "chat prompt must describe distilling a spec on request"


def test_build_declares_draft_spec_promotion_input():
    """build declares the ``draft_spec`` promotion input so a carried spec is
    delivered via ADR-027's initial_artifacts on handoff.

    Fails pre-impl: build doesn't exist.
    """
    process = build_process("build")
    names = {pi.name for pi in process.promotion_inputs}
    assert "draft_spec" in names


def test_build_planning_prompt_injects_carried_spec():
    """build's planning user prompt reads the carried spec via the
    ``{artifact:draft_spec}`` injection, so it reaches the agent even though
    ``inputs`` gating was dropped.

    Fails pre-impl: the build planning prompt doesn't exist.
    """
    path = BUNDLED_PROMPTS / "build" / "planning-user.md"
    assert path.is_file(), "build/planning-user.md must exist"
    assert "{artifact:draft_spec}" in path.read_text()
