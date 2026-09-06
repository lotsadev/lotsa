"""Failing tests for ADR-046 — the operator ``sync_branch`` action.

The one-click "Sync with default" entry point:

- runs the real merge (reusing ``_sync_branch_to_main``), pushing when the task
  has a PR and doing a **local merge, no push** pre-PR;
- is gated to idle tasks — a ``working`` task is rejected with
  ``SyncNotAllowed`` (an agent is live in the worktree);
- on a merge conflict (main moved since the last poll) routes through
  ``_handle_conflict_dispatch`` → the process's ``resolve_conflicts`` agent;
- on an already-current branch does nothing (``SyncNotNeeded``).

Pre-fix failure shape: importing ``sync_branch`` / ``SyncNotAllowed`` /
``SyncNotNeeded`` from ``lotsa.orchestrator`` fails (they don't exist), so the
module is red at collection — the intended "unimplemented" signal.

Reuses the real-git scaffolding + helpers from test_orchestrator, and
``full_service`` (started on the bundled ``build`` process, which carries the
``resolve_conflicts`` job) from conftest.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from lotsa.orchestrator import SyncNotAllowed, SyncNotNeeded
from lotsa.tests.test_orchestrator import (
    _patch_execute_push,
    _PushRecorder,
    _setup_sync_worktree,
    _stage_waiting_pr_task,
)

# full_service, _loop, run come from conftest.py.


def _point_worktree(svc, task_id, wt) -> None:
    svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt


class TestSyncBranchAction:
    def test_clean_merge_pushes_and_stays_parked(self, full_service, tmp_path, run, monkeypatch):
        """An idle behind task WITH a PR merges origin/main, pushes once, and
        stays parked (no agent dispatched, status unchanged)."""
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=41)
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        _point_worktree(svc, task.id, wt)
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.sync_branch(task.id))

        assert "main-change" in (wt / "file.txt").read_text(), "origin/main must be merged into the worktree"
        assert len(rec.calls) == 1, "a task with a PR must push the merged ref once"
        row = run(svc.db.get_task(task.id))
        assert row.status == "waiting_for_pr", "a clean sync leaves an idle task parked, not working"
        assert svc.runner.calls == [], "a clean merge dispatches no agent"

    def test_pre_pr_task_merges_locally_without_push(self, full_service, tmp_path, run, monkeypatch):
        """A behind idle task with NO PR merges locally and does not push."""
        svc = full_service
        task = run(svc.db.create_task("prepr", state="coding"))
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="coding",
                to_status="blocked",
                to_current_step="coding",
            )
        )
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        _point_worktree(svc, task.id, wt)
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.sync_branch(task.id))

        assert "main-change" in (wt / "file.txt").read_text(), "origin/main must be merged locally"
        assert rec.calls == [], "a pre-PR sync is a local merge — it must not push"

    def test_rejected_while_working(self, full_service, run):
        """Sync is disabled while an agent is live in the worktree."""
        svc = full_service
        task = run(svc.db.create_task("busy", state="coding"))
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="coding",
                to_status="working",
                to_current_step="coding",
            )
        )

        with pytest.raises(SyncNotAllowed):
            run(svc.sync_branch(task.id))

    def test_conflict_dispatches_resolve_conflicts(self, full_service, tmp_path, run, monkeypatch):
        """A merge that races a conflict routes to the resolve_conflicts agent."""
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=42)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        _point_worktree(svc, task.id, wt)
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.sync_branch(task.id))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "working", f"a conflict must dispatch resolve_conflicts, got status={row.status!r}"
        assert row.state == "resolving_conflicts"
        assert svc.runner.calls, "the resolve_conflicts agent must be dispatched on a conflict"
        assert rec.calls == [], "a conflicted sync must not push"

    def test_already_current_is_a_noop(self, full_service, tmp_path, run, monkeypatch):
        """A level branch performs no merge, no push, and no dispatch."""
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=43)
        wt = _setup_sync_worktree(tmp_path, task.id, "current")
        _point_worktree(svc, task.id, wt)
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        # An already-current branch signals SyncNotNeeded (or no-ops); either
        # way the safety invariants below must hold.
        with contextlib.suppress(SyncNotNeeded):
            run(svc.sync_branch(task.id))

        assert rec.calls == [], "an already-current branch must not push"
        assert svc.runner.calls == [], "an already-current branch must not dispatch an agent"
        assert (wt / "file.txt").read_text() == "base\n", "no merge should have touched the worktree"
