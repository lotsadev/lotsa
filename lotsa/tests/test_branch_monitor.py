"""Failing tests for ADR-046 — the standing branch-freshness monitor.

Covers two layers:

1. Orchestrator git surface (ADR-013 keeps git logic in the orchestrator):
   - ``refresh_branch_status(task_id)`` — the **non-mutating** probe. Computes
     ``behind_count`` (``git rev-list --count HEAD..origin/<default>``) and
     mergeability via ``git merge-tree --write-tree`` (Git ≥2.38), persisting
     ``branch_behind`` / ``branch_mergeable`` / ``branch_conflicts`` /
     ``branch_checked_at`` to task metadata. It must NEVER mutate the worktree.
   - ``list_branch_watch_tasks()`` — discovery: non-terminal tasks that have a
     worktree; ``chat`` / terminal / worktree-less tasks are skipped.

2. ``lotsa.branch_monitor.BranchMonitor`` — a ``Monitor`` (``kind="standing"``)
   that per tick does one fetch per project then one probe per watched task,
   talking to the orchestrator through the ``BranchMonitorOrchestrator``
   Protocol.

Pre-fix failure shape:
- The ``lotsa.branch_monitor`` import raises ``ModuleNotFoundError`` (module
  absent) — the ``BranchMonitor`` tests are red at collection.
- The orchestrator-probe tests raise ``AttributeError`` — ``refresh_branch_status``
  / ``list_branch_watch_tasks`` don't exist yet.

Reuses the real-git scaffolding (``_setup_sync_worktree``) from
test_orchestrator, and ``full_service`` from conftest.
"""

from __future__ import annotations

import subprocess

from lotsa.branch_monitor import BranchMonitor, BranchMonitorOrchestrator  # noqa: F401
from lotsa.monitors.registry import MonitorRegistry
from lotsa.tests.test_orchestrator import _setup_sync_worktree

# full_service, _loop, run come from conftest.py.


# ---------------------------------------------------------------------------
# git helpers (local; the probe reads local refs — the monitor fetches first)
# ---------------------------------------------------------------------------


def _fetch_main(wt) -> None:
    """Simulate the monitor's per-project fetch so the local origin ref is current."""
    subprocess.run(["git", "-C", str(wt), "fetch", "origin", "main"], capture_output=True, check=True)


def _head(wt) -> str:
    return subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _porcelain(wt) -> str:
    return subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout


def _point_worktree(svc, task_id, wt) -> None:
    svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt


# ---------------------------------------------------------------------------
# refresh_branch_status — the non-mutating probe
# ---------------------------------------------------------------------------


class TestRefreshBranchStatus:
    def test_behind_computes_count_and_mergeable_without_mutating(self, full_service, tmp_path, run):
        svc = full_service
        task = run(svc.db.create_task("behind", state="coding"))
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        _fetch_main(wt)
        _point_worktree(svc, task.id, wt)
        head_before = _head(wt)

        run(svc.refresh_branch_status(task.id))

        row = run(svc.db.get_task(task.id))
        assert row.metadata["branch_behind"] == 1
        assert row.metadata["branch_mergeable"] is True
        assert row.metadata.get("branch_conflicts") == []
        assert row.metadata.get("branch_checked_at")
        # Non-mutating: the probe must not touch the worktree or its HEAD.
        assert _head(wt) == head_before, "the probe must not move HEAD"
        assert _porcelain(wt) == "", "the probe must leave a clean worktree"

    def test_conflict_reports_unmergeable_and_names_files_without_markers(self, full_service, tmp_path, run):
        svc = full_service
        task = run(svc.db.create_task("conflict", state="coding"))
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        _fetch_main(wt)
        _point_worktree(svc, task.id, wt)
        head_before = _head(wt)

        run(svc.refresh_branch_status(task.id))

        row = run(svc.db.get_task(task.id))
        assert row.metadata["branch_behind"] >= 1
        assert row.metadata["branch_mergeable"] is False
        assert "shared.txt" in (row.metadata.get("branch_conflicts") or [])
        # Non-mutating: unlike _sync_branch_to_main, the probe leaves NO markers.
        assert "<<<<<<<" not in (wt / "shared.txt").read_text(), "the probe must not write conflict markers"
        assert _head(wt) == head_before
        assert _porcelain(wt) == ""

    def test_current_reports_zero_behind_and_mergeable(self, full_service, tmp_path, run):
        svc = full_service
        task = run(svc.db.create_task("current", state="coding"))
        wt = _setup_sync_worktree(tmp_path, task.id, "current")
        _point_worktree(svc, task.id, wt)

        run(svc.refresh_branch_status(task.id))

        row = run(svc.db.get_task(task.id))
        assert row.metadata["branch_behind"] == 0
        assert row.metadata["branch_mergeable"] is True


# ---------------------------------------------------------------------------
# list_branch_watch_tasks — discovery predicate
# ---------------------------------------------------------------------------


class TestListBranchWatchTasks:
    def test_includes_only_nonterminal_worktree_bearing_tasks(self, full_service, tmp_path, run):
        svc = full_service

        with_wt = run(svc.db.create_task("watched", state="coding"))
        terminal = run(svc.db.create_task("done", state="coding"))
        no_wt = run(svc.db.create_task("chatlike", state="chat"))

        # A real worktree only for the watched task; the others resolve to None.
        wt = _setup_sync_worktree(tmp_path, with_wt.id, "current")
        paths = {with_wt.id: wt}
        svc._worktree_managers["default"].get_path = lambda tid: paths.get(tid)

        # Drive the terminal task to a terminal status so the SQL filter drops it.
        run(
            svc.db.claim_task_transition(
                terminal.id,
                from_status=terminal.status,
                from_state=terminal.state,
                to_state="complete",
                to_status="complete",
                to_current_step=None,
            )
        )

        watched = run(svc.list_branch_watch_tasks())
        ids = {t["id"] for t in watched}

        assert with_wt.id in ids, "a non-terminal task with a worktree must be watched"
        assert terminal.id not in ids, "a terminal task must be skipped"
        assert no_wt.id not in ids, "a worktree-less (chat) task must be skipped"


# ---------------------------------------------------------------------------
# BranchMonitor — the standing consumer
# ---------------------------------------------------------------------------


class _FakeBranchOrch:
    """Partial fake satisfying ``BranchMonitorOrchestrator`` for loop tests."""

    def __init__(self, project_ids, task_ids):
        self._project_ids = list(project_ids)
        self._task_ids = list(task_ids)
        self.fetched: list[str] = []
        self.refreshed: list[str] = []

    async def list_branch_watch_project_ids(self) -> list[str]:
        return list(self._project_ids)

    async def fetch_project_default(self, project_id: str) -> None:
        self.fetched.append(project_id)

    async def list_branch_watch_tasks(self) -> list[dict]:
        return [{"id": tid, "project_id": self._project_ids[0]} for tid in self._task_ids]

    async def refresh_branch_status(self, task_id: str) -> None:
        self.refreshed.append(task_id)


class TestBranchMonitorLoop:
    def test_tick_fetches_once_per_project_then_probes_each_task(self, run):
        async def _test():
            orch = _FakeBranchOrch(project_ids=["p1", "p2"], task_ids=["a", "b", "c"])
            reg = MonitorRegistry()
            mon = BranchMonitor(orch, interval_seconds=300, registry=reg)

            await mon.tick()

            assert sorted(orch.fetched) == ["p1", "p2"], "one fetch per project per tick"
            assert sorted(orch.refreshed) == ["a", "b", "c"], "one probe per watched task"

        run(_test())

    def test_registers_standing_heartbeat(self, run):
        async def _test():
            orch = _FakeBranchOrch(project_ids=["p1"], task_ids=["a"])
            reg = MonitorRegistry()
            BranchMonitor(orch, interval_seconds=300, registry=reg)
            beats = {hb.name: hb for hb in reg.snapshot()}
            assert beats, "the branch monitor must register a heartbeat on construction"
            assert any(hb.kind == "standing" for hb in beats.values())

        run(_test())

    def test_fetch_failure_for_one_project_does_not_starve_the_tick(self, run):
        async def _test():
            orch = _FakeBranchOrch(project_ids=["good", "bad"], task_ids=["a"])

            async def _boom(pid):
                orch.fetched.append(pid)
                if pid == "bad":
                    raise RuntimeError("network down")

            orch.fetch_project_default = _boom  # type: ignore[assignment]
            reg = MonitorRegistry()
            mon = BranchMonitor(orch, interval_seconds=300, registry=reg)

            await mon.tick()  # must not raise

            assert "good" in orch.fetched and "bad" in orch.fetched
            assert orch.refreshed == ["a"], "a per-project fetch failure must not skip the probe phase"

        run(_test())
