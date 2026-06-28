"""Tests for the guard-override registry, the ``pr_fix_budget`` handler, and
the ``acknowledge-override`` API surface (ADR-019).

These are RED tests written before the implementation exists. They fail today
for these reasons:

* ``lotsa.overrides`` does not exist yet → ``ImportError`` inside each test
  that imports it (registry + handler tests).
* ``OrchestratorService.acknowledge_override`` / ``AcknowledgeOverrideNotAllowed``
  do not exist → ``AttributeError`` / ``ImportError``.
* ``POST /api/tasks/{id}/acknowledge-override`` is not routed → HTTP 404.
* ``TaskDetailFullResponse.available_overrides`` is not a field → absent from
  the JSON detail response.

The registry tests reach into the private ``_HANDLERS`` dict only to
snapshot/restore it around a test that mutates it — the same isolation
pattern ``test_pr_flow_integration._stub_full_process_service`` uses with
``registry._TOOLS``. Built-in handler lookups go through the public API.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from lotsa.db import TaskDB
from lotsa.tests.conftest import wait_for_completion

# Phrase the cap emits at its single site (lotsa/orchestrator.py). The handler's
# ``detect`` substring-matches on this prefix (ADR-019 D4).
_CAP_FIRE_CONTENT = "PR-fix budget exhausted (10/10 rounds). Human review required."


def _make_db(tmp_path, run) -> TaskDB:
    db = TaskDB(tmp_path / "lotsa.db")
    run(db.initialize())
    return db


def _seed_cap_fire_row(db, run, task_id: str) -> None:
    """Append the ``pr_decision(blocked)`` row the cap writes today."""
    run(
        db.add_message(
            task_id,
            "agent",
            "pr-fix",
            _CAP_FIRE_CONTENT,
            "pr_decision",
            metadata={
                "decision": "blocked",
                "round": 10,
                "triggering_comment_ids": [],
                "commit_sha": None,
                "duration_ms": None,
                "cost_usd": None,
            },
        )
    )


_SKIP_CAP_FIRE_CONTENT = "Agent skipped 3 reviewer comments in a row (cap=3). Please verify the agent's reasoning."


def _seed_skip_cap_fire_row(db, run, task_id: str) -> None:
    """Append the ``pr_decision(blocked)`` row the consecutive-skip cap writes."""
    run(
        db.add_message(
            task_id,
            "agent",
            "pr-fix",
            _SKIP_CAP_FIRE_CONTENT,
            "pr_decision",
            metadata={
                "decision": "blocked",
                "round": 1,
                "triggering_comment_ids": [],
                "commit_sha": None,
                "duration_ms": None,
                "cost_usd": None,
            },
        )
    )


class _FakeOverride:
    """Minimal handler satisfying the OverrideHandler protocol for registry tests."""

    def __init__(self, guard_name: str = "fake_guard", *, detects: bool = False):
        self.guard_name = guard_name
        self.label = "Fake label"
        self.description = "Fake description"
        self._detects = detects

    async def detect(self, task, db) -> bool:
        return self._detects

    async def acknowledge(self, task, operator_reason, db) -> None:
        return None


# ---------------------------------------------------------------------------
# R1 — registry contract (AC1)
# ---------------------------------------------------------------------------


class TestOverrideRegistry:
    def test_register_and_get_roundtrip(self):
        from lotsa import overrides

        saved = dict(overrides._HANDLERS)
        try:
            handler = _FakeOverride("rt_guard")
            overrides.register_override(handler)
            assert overrides.get_override("rt_guard") is handler
            assert overrides.is_override_registered("rt_guard") is True
            assert "rt_guard" in overrides.list_overrides()
        finally:
            overrides._HANDLERS.clear()
            overrides._HANDLERS.update(saved)

    def test_register_duplicate_raises_value_error(self):
        from lotsa import overrides

        saved = dict(overrides._HANDLERS)
        try:
            overrides.register_override(_FakeOverride("dup_guard"))
            with pytest.raises(ValueError):
                overrides.register_override(_FakeOverride("dup_guard"))
        finally:
            overrides._HANDLERS.clear()
            overrides._HANDLERS.update(saved)

    def test_get_missing_raises_key_error_listing_registered(self):
        from lotsa import overrides

        with pytest.raises(KeyError) as excinfo:
            overrides.get_override("definitely_not_registered")
        # Message names the registered set so an operator can spot a typo —
        # mirrors registry.get_tool's KeyError contract.
        assert "registered" in str(excinfo.value).lower()

    def test_is_override_registered_false_for_unknown(self):
        from lotsa import overrides

        assert overrides.is_override_registered("definitely_not_registered") is False

    def test_builtin_pr_fix_budget_registered_at_import(self):
        from lotsa import overrides

        # The built-in self-registers at module import (guarded by
        # is_override_registered so re-import is a no-op).
        assert overrides.is_override_registered("pr_fix_budget") is True
        handler = overrides.get_override("pr_fix_budget")
        assert handler.guard_name == "pr_fix_budget"
        assert handler.label == "Acknowledge & continue"
        assert isinstance(handler.description, str) and handler.description

    def test_reimport_is_idempotent(self):
        # A second import must not raise (the is_override_registered guard
        # swallows the would-be duplicate registration).
        import importlib

        from lotsa import overrides

        importlib.import_module("lotsa.overrides")
        importlib.reload(overrides)
        assert overrides.is_override_registered("pr_fix_budget") is True

    def test_list_available_for_matches_cap_blocked_task(self, tmp_path, run):
        from lotsa import overrides

        db = _make_db(tmp_path, run)
        try:
            blocked = run(db.create_task("Blocked", state="pr-fixing", metadata={"pr_fix_round_count": 10}))
            _seed_cap_fire_row(db, run, blocked.id)
            normal = run(db.create_task("Normal"))

            blocked_row = run(db.get_task(blocked.id))
            normal_row = run(db.get_task(normal.id))

            available = run(overrides.list_available_for(blocked_row, db))
            assert [h.guard_name for h in available] == ["pr_fix_budget"]

            assert run(overrides.list_available_for(normal_row, db)) == []
        finally:
            run(db.close())


# ---------------------------------------------------------------------------
# R2 — pr_fix_budget handler detect (AC2)
# ---------------------------------------------------------------------------


class TestPrFixBudgetDetect:
    def _handler(self):
        from lotsa import overrides

        return overrides.get_override("pr_fix_budget")

    def test_detect_true_for_cap_fire_row(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("Blocked", state="pr-fixing"))
            _seed_cap_fire_row(db, run, task.id)
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is True
        finally:
            run(db.close())

    def test_detect_false_when_no_pr_decision_rows(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("Fresh"))
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is False
        finally:
            run(db.close())

    def test_detect_false_for_non_blocked_latest_decision(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("Done"))
            run(
                db.add_message(
                    task.id,
                    "agent",
                    "pr-fix",
                    "addressed the comments",
                    "pr_decision",
                    metadata={"decision": "done", "round": 3},
                )
            )
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is False
        finally:
            run(db.close())

    def test_detect_false_for_blocked_but_different_reasoning(self, tmp_path, run):
        """A ``decision="blocked"`` row whose content is an agent-emitted BLOCKED
        (not the cap-fire phrase) must NOT match — the cap override is specific
        to the budget cap, not to every blocked outcome.
        """
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("AgentBlocked"))
            run(
                db.add_message(
                    task.id,
                    "agent",
                    "pr-fix",
                    "cannot resolve the merge conflict",
                    "pr_decision",
                    metadata={"decision": "blocked", "round": 4},
                )
            )
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is False
        finally:
            run(db.close())

    def test_detect_false_when_cap_fire_is_not_the_latest_row(self, tmp_path, run):
        """A cap-fire row followed by a newer non-blocked decision must not match —
        only the MOST RECENT pr_decision row governs detection.
        """
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("Recovered"))
            _seed_cap_fire_row(db, run, task.id)
            run(
                db.add_message(
                    task.id,
                    "agent",
                    "pr-fix",
                    "addressed the comments",
                    "pr_decision",
                    metadata={"decision": "done", "round": 11},
                )
            )
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is False
        finally:
            run(db.close())

    def test_detect_true_for_skip_cap_fire_row(self, tmp_path, run):
        """The override also covers the consecutive-skip cap block — without it a
        skip-cap-blocked task has no acknowledge path at all (internal tasks /
        04ee0735 blocked on the skip cap, not the round cap)."""
        db = _make_db(tmp_path, run)
        try:
            task = run(db.create_task("SkipBlocked", state="pr-fixing"))
            _seed_skip_cap_fire_row(db, run, task.id)
            row = run(db.get_task(task.id))
            assert run(self._handler().detect(row, db)) is True
        finally:
            run(db.close())


# ---------------------------------------------------------------------------
# R2 — pr_fix_budget handler acknowledge (AC3)
# ---------------------------------------------------------------------------


class TestPrFixBudgetAcknowledge:
    def _handler(self):
        from lotsa import overrides

        return overrides.get_override("pr_fix_budget")

    def _blocked_task(self, db, run):
        task = run(
            db.create_task(
                "Cap blocked",
                state="pr-fixing",
                status="blocked",
                current_step="pr-fix",
                metadata={"pr_fix_round_count": 10, "current_flow": "pr_fix"},
            )
        )
        _seed_cap_fire_row(db, run, task.id)
        return run(db.get_task(task.id))

    def test_acknowledge_resets_round_counter_to_zero(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = self._blocked_task(db, run)
            run(self._handler().acknowledge(task, "looks good, continue", db))
            after = run(db.get_task(task.id))
            assert after.metadata.get("pr_fix_round_count") == 0
        finally:
            run(db.close())

    def test_acknowledge_resets_both_cap_counters(self, tmp_path, run):
        """Acknowledge clears BOTH budget counters — round AND consecutive-skip —
        so the task doesn't immediately re-block on the other cap after the
        operator says continue (the 04ee0735 failure: round override left
        consecutive_skipped at 2, so one more skip re-blocked)."""
        db = _make_db(tmp_path, run)
        try:
            task = run(
                db.create_task(
                    "Cap blocked",
                    state="pr-fixing",
                    status="blocked",
                    current_step="pr-fix",
                    metadata={
                        "pr_fix_round_count": 10,
                        "pr_fix_consecutive_skipped": 3,
                        "current_flow": "pr_fix",
                    },
                )
            )
            _seed_cap_fire_row(db, run, task.id)
            row = run(db.get_task(task.id))
            run(self._handler().acknowledge(row, "continue", db))
            after = run(db.get_task(task.id))
            assert after.metadata.get("pr_fix_round_count") == 0
            assert after.metadata.get("pr_fix_consecutive_skipped") == 0
        finally:
            run(db.close())

    def test_acknowledge_appends_exactly_one_overridden_row(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = self._blocked_task(db, run)
            before = run(db.get_messages(task.id, msg_type="pr_decision"))
            run(self._handler().acknowledge(task, "reviewed by me", db))
            after = run(db.get_messages(task.id, msg_type="pr_decision"))
            new_rows = [m for m in after if m.metadata.get("decision") == "overridden"]
            assert len(after) == len(before) + 1
            assert len(new_rows) == 1
            row = new_rows[0]
            assert row.role == "user"
            assert row.type == "pr_decision"
            assert row.step_name == "pr-fix"
            # Cap-firing round preserved on the override row.
            assert row.metadata.get("round") == 10
            assert row.metadata.get("triggering_comment_ids") == []
            assert row.metadata.get("commit_sha") is None
            assert row.metadata.get("duration_ms") is None
            assert row.metadata.get("cost_usd") is None
        finally:
            run(db.close())

    def test_acknowledge_includes_reason_in_content(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = self._blocked_task(db, run)
            run(self._handler().acknowledge(task, "bot already approved", db))
            rows = run(db.get_messages(task.id, msg_type="pr_decision"))
            row = [m for m in rows if m.metadata.get("decision") == "overridden"][0]
            assert row.content == "Operator acknowledged budget cap — bot already approved"
        finally:
            run(db.close())

    def test_acknowledge_without_reason_omits_suffix(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = self._blocked_task(db, run)
            run(self._handler().acknowledge(task, None, db))
            rows = run(db.get_messages(task.id, msg_type="pr_decision"))
            row = [m for m in rows if m.metadata.get("decision") == "overridden"][0]
            assert row.content == "Operator acknowledged budget cap"
        finally:
            run(db.close())

    def test_acknowledge_does_not_transition_task(self, tmp_path, run):
        db = _make_db(tmp_path, run)
        try:
            task = self._blocked_task(db, run)
            run(self._handler().acknowledge(task, None, db))
            after = run(db.get_task(task.id))
            # Handler does not transition the task — acknowledge_override calls retry() downstream.
            assert after.status == "blocked"
            assert after.state == "pr-fixing"
        finally:
            run(db.close())


# ---------------------------------------------------------------------------
# R4 — endpoint (AC4)  +  R5 — available_overrides on the detail response (AC5)
# ---------------------------------------------------------------------------


class TestAcknowledgeOverrideEndpoint:
    @staticmethod
    async def _cap_blocked_task(service):
        """Async seeding helper — awaited inside the running ``_test()`` loop."""
        task = await service.db.create_task(
            "Cap blocked",
            state="pr-fixing",
            status="blocked",
            current_step="pr-fix",
            metadata={"pr_fix_round_count": 10},
        )
        await service.db.add_message(
            task.id,
            "agent",
            "pr-fix",
            _CAP_FIRE_CONTENT,
            "pr_decision",
            metadata={
                "decision": "blocked",
                "round": 10,
                "triggering_comment_ids": [],
                "commit_sha": None,
                "duration_ms": None,
                "cost_usd": None,
            },
        )
        return task

    def test_acknowledge_override_returns_200_and_writes_override_row(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/acknowledge-override",
                    json={"guard_name": "pr_fix_budget", "reason": "reviewed"},
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()
                # The new overridden pr_decision row is visible in the chat log.
                overridden = [
                    m
                    for m in data["messages"]
                    if m["type"] == "pr_decision" and m["metadata"].get("decision") == "overridden"
                ]
                assert len(overridden) == 1
                assert overridden[0]["role"] == "user"
                # detect() now returns False (latest decision is "overridden"),
                # so the override button disappears.
                assert data["available_overrides"] == []

            after = await service.db.get_task(task.id)
            assert after.metadata.get("pr_fix_round_count") == 0

        run(_test())

    def test_acknowledge_override_resumes_via_retry(self, app_with_service, run):
        """ADR-019 revised (2026-06-16): the override resets the guard AND
        resumes the blocked step in one action. acknowledge_override must
        invoke retry() (the resume) after the reset — not leave the operator
        to click Retry separately. The full retry→dispatch is covered by the
        retry tests; here we assert the wiring + that the reset still happened.
        """
        from unittest.mock import AsyncMock, patch

        _app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            with patch.object(service, "retry", new=AsyncMock()) as mock_retry:
                await service.acknowledge_override(task.id, "pr_fix_budget", "reviewed")
                mock_retry.assert_awaited_once_with(task.id)
            after = await service.db.get_task(task.id)
            assert after.metadata.get("pr_fix_round_count") == 0, "the acknowledge reset must run before the resume"

        run(_test())

    def test_acknowledge_override_before_start_does_not_partially_mutate(self, app_with_service, run):
        """acknowledge_override is state-mutating (it resumes via retry() →
        atomic_transition), so it carries the flow-not-loaded guard. Without it,
        a pre-start() call would commit the handler's counter reset + overridden
        audit row and only THEN have retry() raise — leaving counters reset, the
        override affordance gone, and a 500.

        Pre-fix red: the guard is absent, so the mutation lands before retry()
        raises (counter→0, overridden row written) and the assertions below fail.
        """
        _app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            original_flow = service.flow
            service.flow = None  # simulate pre-start()
            try:
                with pytest.raises(RuntimeError, match="not started"):
                    await service.acknowledge_override(task.id, "pr_fix_budget", "reviewed")
            finally:
                service.flow = original_flow
            # No partial mutation: counter untouched and no overridden row.
            after = await service.db.get_task(task.id)
            assert after.metadata.get("pr_fix_round_count") == 10, "guard must raise before the counter reset"
            rows = await service.db.get_messages(task.id, msg_type="pr_decision")
            assert not [m for m in rows if m.metadata.get("decision") == "overridden"], (
                "guard must raise before the overridden audit row is written"
            )

        run(_test())

    def test_concurrent_acknowledge_writes_exactly_one_override_row(self, app_with_service, run):
        """Regression: detect→acknowledge must be serialised per task.

        Two concurrent ``acknowledge_override`` calls would both pass
        ``detect()`` and both write an ``overridden`` audit row; the second
        re-reads ``pr_fix_round_count`` *after* the first reset it to 0, so it
        records a misleading ``round=0`` row. The ``_acknowledging_override``
        guard makes the second caller bail cleanly between awaits.

        ``TaskDB`` wraps a synchronous ``sqlite3`` connection, so its ``async``
        methods never yield to the event loop — two service calls can only
        interleave at a real suspension point. We register a handler whose
        ``detect`` yields (``asyncio.sleep(0)``) so both callers reach the
        detect→acknowledge window concurrently, exercising the race from inside
        the code under test rather than pre-seeding the post-bug state.

        Against the pre-fix code (no guard) this fails: both callers' detect
        yields, both acknowledge, and the second writes a second overridden row
        carrying ``round == 0``.
        """
        from unittest.mock import AsyncMock, patch

        from lotsa import overrides
        from lotsa.overrides import PrFixBudgetOverride

        class _YieldingBudgetOverride(PrFixBudgetOverride):
            guard_name = "pr_fix_budget_yielding"

            async def detect(self, task, db) -> bool:
                # Real suspension point so a competing caller can interleave.
                await asyncio.sleep(0)
                return await super().detect(task, db)

        overrides.register_override(_YieldingBudgetOverride())

        app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            # Mock retry() so this test isolates the concurrency guard (exactly
            # one overridden row) from the downstream resume. Otherwise the
            # round_count assertion would depend on _sync_branch_to_main happening
            # to fail in the test env before retry() bumps the counter — fragile.
            # The real resume is covered by test_acknowledge_override_resumes_via_retry.
            with patch.object(service, "retry", new=AsyncMock()):
                results = await asyncio.gather(
                    service.acknowledge_override(task.id, "pr_fix_budget_yielding", "a"),
                    service.acknowledge_override(task.id, "pr_fix_budget_yielding", "b"),
                    return_exceptions=True,
                )
            # Neither call raises — the loser bails cleanly via the guard.
            assert all(not isinstance(r, Exception) for r in results), results
            rows = await service.db.get_messages(task.id, msg_type="pr_decision")
            overridden = [m for m in rows if m.metadata.get("decision") == "overridden"]
            assert len(overridden) == 1
            # The single row preserves the cap-fire round, never round=0.
            assert overridden[0].metadata.get("round") == 10
            # Reset ran exactly once; retry is mocked so nothing bumps it back up.
            assert (await service.db.get_task(task.id)).metadata.get("pr_fix_round_count") == 0

        run(_test())

    def test_acknowledge_override_unregistered_guard_returns_400(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/acknowledge-override",
                    json={"guard_name": "no_such_guard"},
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "ACKNOWLEDGE_OVERRIDE_NOT_ALLOWED"

        run(_test())

    def test_acknowledge_override_not_applicable_returns_400(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            # A normal task with no cap-fire row → detect() is False.
            task = await service.db.create_task("Normal")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/acknowledge-override",
                    json={"guard_name": "pr_fix_budget"},
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "ACKNOWLEDGE_OVERRIDE_NOT_ALLOWED"

        run(_test())

    def test_detail_response_populates_available_overrides_for_cap_blocked(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._cap_blocked_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}")
                assert resp.status_code == 200
                data = resp.json()
                assert "available_overrides" in data
                assert len(data["available_overrides"]) == 1
                entry = data["available_overrides"][0]
                assert entry["guard_name"] == "pr_fix_budget"
                assert entry["label"] == "Acknowledge & continue"
                assert isinstance(entry["description"], str) and entry["description"]

        run(_test())

    def test_detail_response_available_overrides_empty_for_normal_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Normal task")
            await wait_for_completion(service, task.id)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}")
                assert resp.status_code == 200
                assert resp.json()["available_overrides"] == []

        run(_test())

    def test_summary_response_omits_available_overrides(self, app_with_service, run):
        """D6 — ``available_overrides`` lives only on the full detail response,
        never on the summary/sidebar list, so per-handler detect() runs once per
        detail load rather than per sidebar render.
        """
        app, service = app_with_service

        async def _test():
            await self._cap_blocked_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/tasks")
                assert resp.status_code == 200
                for item in resp.json():
                    assert "available_overrides" not in item

        run(_test())
