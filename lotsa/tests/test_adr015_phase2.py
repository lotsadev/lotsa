"""Failing tests for ADR-015 Phase 2 — Conflict Auto-Resolution.

Tests target the following spec requirements:

1. ``classify_signals`` fires on ``merge_conflict`` trigger when
   ``pr.mergeable is False`` (and not on None/True).
2. ``parse_config`` accepts ``merge_conflict`` as a valid trigger.
3. ``PrInfo`` carries a ``mergeable`` field; ``get_pr`` populates it.
4. ``_sync_branch_to_main`` on a conflict leaves markers in place (no
   ``git merge --abort``), so the agent can resolve them.
5. A conflicted sync dispatches ``resolve_conflicts`` instead of blocking.
6. ``CONFLICTS_RESOLVED:`` advances the task through to ``pr-fix``.
7. ``NEEDS_INPUT:`` from ``resolve_conflicts`` parks at ``needs_input``.
8. ``answer()`` re-dispatches ``resolve_conflicts`` with the operator's
   decision under ``## Revision Feedback``.
9. A process without a ``resolve_conflicts`` job still blocks on conflict
   (backward compatibility).
10. Cross-flow edge: ``(wait_for_pr_signal, resolving_conflicts)`` exists
    in both main's and pr_fix's state-machine transitions.
11. ``full`` process.yaml declares ``resolve_conflicts`` in the job catalog
    and the ``pr_fix`` sub-flow.
12. ``full`` process.yaml's ``wait_for_pr_signal`` monitor carries
    ``merge_conflict`` in its trigger list.

Pre-fix failure shape:
- Tests 1–3 fail because ``classify_signals`` / ``parse_config`` /
  ``PrInfo`` don't know about ``merge_conflict`` yet.
- Tests 4–9 fail because Phase 2 dispatch logic is absent; the conflict
  path still calls ``git merge --abort`` and blocks.
- Tests 10–12 fail because the job, flow step, and trigger are not yet
  declared.

The real git worktree helpers (``_setup_sync_worktree``, ``_PushRecorder``,
``full_service``, ``_stage_waiting_pr_task``, ``_RecordingHangRunner``,
``_patch_execute_push``) are imported from test_orchestrator where they live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.engines.pr_monitor import PrMonitorConfig as PrConfig
from lotsa.engines.pr_monitor import parse_config
from lotsa.flows import build_process
from lotsa.github_client import CheckStatus, PrInfo
from lotsa.orchestrator import OrchestratorService
from lotsa.pr_monitor import PrSignal, classify_signals

# ---------------------------------------------------------------------------
# Re-use the git scaffolding from test_orchestrator (same module, tests imported
# via direct import of the helper functions). They're module-level functions, so
# we can import them directly.
# ---------------------------------------------------------------------------
from lotsa.tests.test_orchestrator import (
    _patch_execute_push,
    _PushRecorder,
    _RecordingHangRunner,
    _setup_sync_worktree,
    _stage_waiting_pr_task,
)
from rigg.models import AgentResult

# full_service, _loop, run come from conftest.py (auto-discovered by pytest).


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr(
    *,
    merged: bool = False,
    state: str = "open",
    review_decision: str | None = None,
    head_sha: str = "abc123",
    base_branch: str = "main",
    mergeable: bool | None = None,
) -> PrInfo:
    """Build a ``PrInfo`` with an optional ``mergeable`` field."""
    return PrInfo(
        number=1,
        state=state,
        merged=merged,
        review_decision=review_decision,
        head_sha=head_sha,
        base_branch=base_branch,
        mergeable=mergeable,  # Phase 2 — new field
    )


def _checks(*, failing: int = 0, passing: int = 1, pending: int = 0) -> CheckStatus:
    return CheckStatus(
        total=failing + passing + pending,
        passing=passing,
        failing=failing,
        pending=pending,
    )


def _config(**kwargs) -> PrConfig:
    defaults = dict(
        triggers=["human_comment", "bot_comment", "review_decision", "failing_check"],
    )
    defaults.update(kwargs)
    return PrConfig(**defaults)


# ---------------------------------------------------------------------------
# 1. classify_signals — merge_conflict trigger
# ---------------------------------------------------------------------------


def test_merge_conflict_trigger_fires_when_pr_is_conflicting():
    """``mergeable is False`` (GitHub's CONFLICTING) + ``merge_conflict`` → FEEDBACK.

    Pre-fix failure: ``classify_signals`` has no ``merge_conflict`` branch, so
    the function falls through and returns ``PrSignal.NONE``.
    """
    pr = _pr(mergeable=False)
    config = _config(triggers=["merge_conflict"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.FEEDBACK


def test_merge_conflict_trigger_does_not_fire_when_mergeable_is_none():
    """``mergeable is None`` (GitHub hasn't computed mergeability yet) must NOT
    fire the trigger — that would produce a spurious feedback dispatch right
    after a push while GitHub is still calculating the merge state.

    Pre-fix failure: even if the branch were added, a naive ``not pr.mergeable``
    check would fire on ``None``; we require ``is False``.
    """
    pr = _pr(mergeable=None)
    config = _config(triggers=["merge_conflict"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.NONE


def test_merge_conflict_trigger_does_not_fire_when_mergeable_is_true():
    """A cleanly mergeable PR must not fire the ``merge_conflict`` trigger."""
    pr = _pr(mergeable=True)
    config = _config(triggers=["merge_conflict"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.NONE


def test_merge_conflict_trigger_not_active_without_configuration():
    """A CONFLICTING PR does not dispatch if the trigger isn't in the config."""
    pr = _pr(mergeable=False)
    # Omit merge_conflict from triggers — should not fire
    config = _config(triggers=["human_comment", "bot_comment", "review_decision", "failing_check"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.NONE


def test_complete_takes_precedence_over_merge_conflict_trigger():
    """A merged PR returns COMPLETE regardless of ``mergeable`` field value."""
    pr = _pr(merged=True, state="closed", mergeable=False)
    config = _config(triggers=["merge_conflict"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.COMPLETE


def test_abandoned_takes_precedence_over_merge_conflict_trigger():
    """A closed-without-merge PR returns ABANDONED regardless of ``mergeable``."""
    pr = _pr(state="closed", merged=False, mergeable=False)
    config = _config(triggers=["merge_conflict"])
    signal = classify_signals(pr, [], [], _checks(), config)
    assert signal == PrSignal.ABANDONED


# ---------------------------------------------------------------------------
# 2. parse_config accepts merge_conflict as a valid trigger
# ---------------------------------------------------------------------------


def test_parse_config_accepts_merge_conflict_trigger():
    """``merge_conflict`` must be in the validated trigger vocabulary.

    Pre-fix failure: ``_VALID_TRIGGERS`` only contains the four original
    triggers, so ``parse_config`` raises ValueError:
    ``"Invalid pr_monitor trigger(s): {'merge_conflict'}"``
    """
    raw = {
        "triggers": ["human_comment", "merge_conflict"],
        "poll_interval_seconds": 30,
        "debounce_seconds": 120,
    }
    # Must not raise — merge_conflict is now a valid trigger.
    config = parse_config(raw)
    assert "merge_conflict" in config.triggers


def test_parse_config_still_rejects_unknown_triggers():
    """Adding ``merge_conflict`` to the vocabulary must not relax validation for
    truly unknown triggers."""
    raw = {
        "triggers": ["merge_conflict", "totally_made_up_trigger"],
        "poll_interval_seconds": 30,
        "debounce_seconds": 120,
    }
    with pytest.raises(ValueError, match="totally_made_up_trigger"):
        parse_config(raw)


# ---------------------------------------------------------------------------
# 3. PrInfo.mergeable field and get_pr population
# ---------------------------------------------------------------------------


def test_prinfo_has_mergeable_field():
    """``PrInfo`` must expose a ``mergeable`` field (new in Phase 2).

    Pre-fix failure: ``PrInfo`` has no ``mergeable`` attribute, so the
    constructor call in ``_pr()`` above raises TypeError.
    """
    info = PrInfo(
        number=1,
        state="open",
        merged=False,
        review_decision=None,
        head_sha="abc",
        base_branch="main",
        mergeable=False,
    )
    assert info.mergeable is False


def test_prinfo_mergeable_defaults_to_none():
    """Existing callers that don't pass ``mergeable`` must still work.

    Pre-fix failure: if ``mergeable`` is added without a default, every
    existing test that constructs ``PrInfo`` without the argument would break.
    The field must have a default of ``None``.
    """
    info = PrInfo(
        number=1,
        state="open",
        merged=False,
        review_decision=None,
        head_sha="abc",
        base_branch="main",
    )
    assert info.mergeable is None


# ---------------------------------------------------------------------------
# 4. _sync_branch_to_main leaves conflict markers in place (no abort)
# ---------------------------------------------------------------------------


class TestSyncBranchConflictLeavesMarkers:
    """Phase 2 changes ``_sync_branch_to_main``'s conflict path: the merge is
    left in place (markers remain in the worktree) instead of being aborted.

    Pre-fix failure: the conflict branch calls ``git merge --abort``, so the
    worktree is clean after the call. The assertion below checks that markers
    ARE present, which fails against the Phase-1 code.
    """

    def test_conflict_leaves_merge_markers_in_worktree(self, full_service, tmp_path, run, monkeypatch):
        """After a conflicting auto-merge ``_sync_branch_to_main`` must NOT run
        ``git merge --abort`` — the conflict markers must remain in the
        worktree for the ``resolve_conflicts`` agent to edit.

        Pre-fix failure: Phase 1 runs ``git merge --abort``, so
        ``shared.txt`` is clean and contains no ``<<<<<<<`` markers. The
        assertion below then fails.
        """
        svc = full_service
        task = run(svc.db.create_task("leave-markers", state="wait_for_pr_signal", metadata={"pr_number": 99}))
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        result = run(svc._sync_branch_to_main(task.id))

        assert result.status == "conflicts"
        assert "shared.txt" in result.conflicting_files

        # The markers must still be present — the merge was NOT aborted.
        content = (wt / "shared.txt").read_text()
        assert "<<<<<<<" in content, (
            f"Conflict markers must remain in shared.txt after Phase 2 sync; "
            f"found: {content!r}. "
            "Phase 1 ran 'git merge --abort' (worktree clean). "
            "Phase 2 must leave markers for resolve_conflicts."
        )

    def test_conflict_merge_head_still_present(self, full_service, tmp_path, run, monkeypatch):
        """``MERGE_HEAD`` must exist after a conflict — it is git's marker
        that a merge is in progress. If the merge were aborted,
        ``MERGE_HEAD`` would not exist.

        Pre-fix failure: ``git merge --abort`` removes ``MERGE_HEAD``.
        """
        svc = full_service
        task = run(svc.db.create_task("merge-head", state="wait_for_pr_signal", metadata={"pr_number": 100}))
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc._sync_branch_to_main(task.id))

        merge_head = Path(wt) / ".git" / "MERGE_HEAD"
        assert merge_head.exists(), (
            "MERGE_HEAD must be present after a conflict — it proves the merge "
            "was left in progress for the resolve_conflicts agent. "
            "Phase 1 ran 'git merge --abort' which removes it."
        )


# ---------------------------------------------------------------------------
# 5. Conflicted sync dispatches resolve_conflicts (not block)
# ---------------------------------------------------------------------------


class TestConflictDispatchesResolveConflicts:
    """Phase 2 wires the conflict path to dispatch ``resolve_conflicts`` instead
    of blocking the task with a Phase 1 message.

    Pre-fix failure: the conflict path calls ``_block_after_sync``, leaving
    the task at ``status=blocked``. The assertions below check for
    ``status=working`` / ``state=resolving_conflicts``.
    """

    def test_conflict_dispatches_resolve_conflicts_not_blocked(self, full_service, tmp_path, run, monkeypatch):
        """A conflicted sync must land the task at
        ``(state=resolving_conflicts, status=working)`` with one agent
        dispatched — not at ``status=blocked``.

        Pre-fix failure: task lands at ``status=blocked`` (Phase 1 block
        path). ``svc.runner.calls`` is empty because the agent was never
        dispatched.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=30)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        dispatched = run(svc.dispatch_pr_fix(task.id, "fix the conflicts"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "working", (
            f"A conflict must dispatch resolve_conflicts (status=working), "
            f"got status={row.status!r}. Phase 1 blocked here."
        )
        assert row.state == "resolving_conflicts", (
            f"Expected state=resolving_conflicts, got state={row.state!r}. "
            "resolve_conflicts must be dispatched instead of pr-fix."
        )
        assert dispatched is True
        assert len(svc.runner.calls) == 1, "resolve_conflicts agent must be dispatched"
        assert rec.calls == [], "conflicted sync must not push"

    def test_conflict_dispatch_consumes_one_round(self, full_service, tmp_path, run, monkeypatch):
        """The conflict dispatch consumes one pr_fix_round_count — resolve +
        subsequent pr-fix are one cohesive round.

        Pre-fix failure: Phase 1 does NOT increment the round counter on a
        conflict (it blocks before doing so). Phase 2 must increment it when
        dispatching resolve_conflicts.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=31)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.dispatch_pr_fix(task.id, "fix the conflicts"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert int(row.metadata.get("pr_fix_round_count", 0)) == 1, (
            "A conflict dispatch must increment pr_fix_round_count. "
            "Phase 1 blocked before doing so, so the count stayed at 0."
        )

    def test_conflict_dispatch_includes_file_list_in_prompt(self, full_service, tmp_path, run, monkeypatch):
        """The conflicting file list must appear in the dispatched agent's
        prompt (user_prompt or system_prompt) so the agent knows which files
        to resolve.

        Pre-fix failure: resolve_conflicts is never dispatched; runner.calls
        is empty.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=32)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.dispatch_pr_fix(task.id, "fix the conflicts"))
        run(asyncio.sleep(0.1))

        assert svc.runner.calls, "resolve_conflicts agent must be dispatched"
        # The conflict file list is injected into the user_prompt via the
        # ``feedback`` kwarg → ``## Revision Feedback`` section in _run_agent.
        call = svc.runner.calls[0]
        combined_prompt = call.get("user_prompt", "") + call.get("system_prompt", "")
        assert "shared.txt" in combined_prompt, (
            f"Conflicting file 'shared.txt' must appear in the agent prompt. Prompt excerpt: {combined_prompt[:400]!r}"
        )

    def test_current_flow_set_to_pr_fix_on_conflict_dispatch(self, full_service, tmp_path, run, monkeypatch):
        """``current_flow`` must be set to ``pr_fix`` when dispatching
        resolve_conflicts — the drainer resolves rules against the pr_fix
        sub-flow's SM.

        Pre-fix failure: Phase 1 blocks before setting current_flow; the
        task row either lacks the key or has the wrong value.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=33)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.dispatch_pr_fix(task.id, "fix the conflicts"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.metadata.get("current_flow") == "pr_fix", (
            f"current_flow must be 'pr_fix' after conflict dispatch, got {row.metadata.get('current_flow')!r}"
        )


# ---------------------------------------------------------------------------
# 6. CONFLICTS_RESOLVED: advances to pr-fix
# ---------------------------------------------------------------------------


class TestConflictsResolvedMarkerRouting:
    """``CONFLICTS_RESOLVED:`` from the ``resolve_conflicts`` step must route
    the task forward to ``pr-fix``.

    Tests flow through ``dispatch_pr_fix`` on a conflicted worktree so the full
    path is exercised: conflict detected → resolve_conflicts dispatched → agent
    emits CONFLICTS_RESOLVED → pr-fix dispatched.

    Pre-fix failure shape: Phase 1 blocks the task at ``dispatch_pr_fix`` before
    any agent runs, so the task lands in ``blocked``, the runner records zero
    calls, and the state assertions below fail.
    """

    def _make_svc_with_conflict(self, tmp_path, run, *, resolve_stdout: str):
        """Build a full-process service with a conflict worktree and a sequential
        runner.

        Call 0 (resolve_conflicts): returns ``resolve_stdout`` immediately.
        Call 1+ (pr-fix and beyond): hangs until shutdown.

        Returns ``(svc, db, push_recorder)``.
        """
        from lotsa import registry as reg
        from lotsa.registry import register_tool
        from lotsa.tools import ToolResult

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()

        async def _stub_push_pr(ctx, cfg):
            await hang_event.wait()
            return ToolResult(success=True, output="stub", metadata={})

        register_tool("push_pr", _stub_push_pr)

        (tmp_path / "data").mkdir(exist_ok=True)
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        class _SequentialRunner:
            def __init__(self):
                self.calls: list[dict] = []
                self._gate = asyncio.Event()

            def dispatch_shape_prompt(self) -> str:
                return ""

            async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
                idx = len(self.calls)
                self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
                if idx == 0:
                    # First call: resolve_conflicts — return immediately.
                    return AgentResult(
                        success=True,
                        stdout=resolve_stdout,
                        stderr="",
                        return_code=0,
                        duration_ms=10,
                    )
                # Subsequent calls: pr-fix and beyond hang.
                await self._gate.wait()
                return AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=1)

        svc = OrchestratorService(config, db)
        svc.runner = _SequentialRunner()
        return svc, db

    def test_conflicts_resolved_advances_to_pr_fixing(self, tmp_path, run, monkeypatch):
        """A full-path test: conflicted sync → resolve_conflicts dispatched →
        agent emits ``CONFLICTS_RESOLVED:`` → task advances to pr-fixing.

        Pre-fix failure: Phase 1 blocks the task at ``dispatch_pr_fix``; the
        runner records 0 calls and ``state='blocked'`` — not ``pr-fixing``.
        """
        svc, db = self._make_svc_with_conflict(tmp_path, run, resolve_stdout="CONFLICTS_RESOLVED: all markers resolved")
        rec = _PushRecorder()
        try:
            run(svc.start())
            try:
                task = _stage_waiting_pr_task(svc, run, pr_number=50)
                wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
                svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

                async def _fake_create_50(task_id, _wt=wt):
                    return _wt

                svc._worktree_managers["default"].create = _fake_create_50
                _patch_execute_push(monkeypatch, rec)

                run(svc.dispatch_pr_fix(task.id, "fix conflicts"))
                run(asyncio.sleep(0.5))

                row = run(db.get_task(task.id))
                assert row.state == "pr-fixing", (
                    f"CONFLICTS_RESOLVED must advance task to pr-fixing, got state={row.state!r}. "
                    "Pre-fix: Phase 1 blocks at dispatch_pr_fix; state='blocked'."
                )
                assert row.status == "working"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise

    def test_conflicts_resolved_dispatches_pr_fix_agent(self, tmp_path, run, monkeypatch):
        """After CONFLICTS_RESOLVED routes to pr-fixing, the pr-fix agent must be
        dispatched (runner receives a second call).

        Pre-fix failure: Phase 1 blocks; runner records 0 calls.
        """
        svc, db = self._make_svc_with_conflict(tmp_path, run, resolve_stdout="CONFLICTS_RESOLVED: all markers resolved")
        rec = _PushRecorder()
        try:
            run(svc.start())
            try:
                task = _stage_waiting_pr_task(svc, run, pr_number=51)
                wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
                svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

                async def _fake_create_51(task_id, _wt=wt):
                    return _wt

                svc._worktree_managers["default"].create = _fake_create_51
                _patch_execute_push(monkeypatch, rec)

                run(svc.dispatch_pr_fix(task.id, "fix conflicts"))
                run(asyncio.sleep(0.5))

                # Call 0: resolve_conflicts; Call 1: pr-fix.
                assert len(svc.runner.calls) >= 2, (
                    f"Expected ≥2 runner calls (resolve_conflicts + pr-fix), "
                    f"got {len(svc.runner.calls)}. "
                    "Pre-fix: Phase 1 blocks; runner records 0 calls."
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise


# ---------------------------------------------------------------------------
# 7. NEEDS_INPUT: from resolve_conflicts parks at needs_input
# ---------------------------------------------------------------------------


class TestResolveConflictsNeedsInputEscalation:
    """``NEEDS_INPUT:`` emitted by ``resolve_conflicts`` must park the task at
    ``(status=needs_input)`` with the question persisted.

    Tests flow through ``dispatch_pr_fix`` on a conflicted worktree so the full
    dispatch path is exercised.

    Pre-fix failure shape: Phase 1 blocks the task; the runner records 0 calls;
    state/status assertions fail because the task is blocked before any agent runs.
    """

    def _make_needs_input_svc(self, tmp_path, run):
        """Build a full-process service where resolve_conflicts emits NEEDS_INPUT.

        Returns ``(svc, db, push_recorder)``.
        """
        from lotsa import registry as reg
        from lotsa.registry import register_tool
        from lotsa.tools import ToolResult

        reg._TOOLS.pop("push_pr", None)
        hang_event = asyncio.Event()

        async def _stub_push_pr(ctx, cfg):
            await hang_event.wait()
            return ToolResult(success=True, output="stub", metadata={})

        register_tool("push_pr", _stub_push_pr)

        (tmp_path / "data").mkdir(exist_ok=True)
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        class _NeedsInputRunner:
            def __init__(self):
                self.calls: list[dict] = []
                self._gate = asyncio.Event()

            def dispatch_shape_prompt(self) -> str:
                return ""

            async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
                self.calls.append({"user_prompt": user_prompt})
                if len(self.calls) == 1:
                    # First call (resolve_conflicts) — escalate with NEEDS_INPUT.
                    return AgentResult(
                        success=True,
                        stdout="NEEDS_INPUT: keep registry or inline — which intent wins?",
                        stderr="",
                        return_code=0,
                        duration_ms=10,
                    )
                # Subsequent calls hang (shouldn't reach here before the assertions).
                await self._gate.wait()
                return AgentResult(
                    success=True, stdout="CONFLICTS_RESOLVED: done", stderr="", return_code=0, duration_ms=1
                )

        svc = OrchestratorService(config, db)
        svc.runner = _NeedsInputRunner()
        return svc, db

    def test_needs_input_parks_task(self, tmp_path, run, monkeypatch):
        """``NEEDS_INPUT:`` from resolve_conflicts must produce
        ``status=needs_input``, not advance or block.

        Pre-fix failure: Phase 1 blocks the task at ``dispatch_pr_fix``; the
        runner records 0 calls and ``status='blocked'``.
        """
        svc, db = self._make_needs_input_svc(tmp_path, run)
        rec = _PushRecorder()
        try:
            run(svc.start())
            try:
                task = _stage_waiting_pr_task(svc, run, pr_number=60)
                wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
                svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

                async def _fake_create_60(task_id, _wt=wt):
                    return _wt

                svc._worktree_managers["default"].create = _fake_create_60
                _patch_execute_push(monkeypatch, rec)

                run(svc.dispatch_pr_fix(task.id, "fix conflicts"))
                run(asyncio.sleep(0.5))

                row = run(db.get_task(task.id))
                assert row.status == "needs_input", (
                    f"NEEDS_INPUT from resolve_conflicts must park at needs_input, "
                    f"got status={row.status!r}. "
                    "Pre-fix: Phase 1 blocks; status='blocked'."
                )
                assert row.current_step == "resolve_conflicts", (
                    f"current_step must be resolve_conflicts, got {row.current_step!r}"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise

    def test_needs_input_does_not_commit_conflict_markers(self, tmp_path, run, monkeypatch):
        """``NEEDS_INPUT:`` from resolve_conflicts must NOT run the ``commit``
        posthook — the merge stays in progress with markers uncommitted.

        The drainer runs step posthooks before extracting ``NEEDS_INPUT:``. If
        the ``commit`` posthook is not gated on the agent finishing, its
        ``git add -A`` stages the still-unresolved conflict paths at stage 0 and
        commits raw ``<<<<<<<`` markers into a merge commit — leaving the
        worktree on a marker-filled HEAD with no MERGE_HEAD, a corrupted base
        for the operator's answer round.

        Pre-fix failure: HEAD's committed ``shared.txt`` contains ``<<<<<<<``
        markers (the posthook completed the conflicted merge) and MERGE_HEAD is
        gone. Post-fix: posthooks are skipped while a question is pending, so
        HEAD is unchanged and the merge is still in progress.
        """
        import subprocess

        svc, db = self._make_needs_input_svc(tmp_path, run)
        rec = _PushRecorder()
        try:
            run(svc.start())
            try:
                task = _stage_waiting_pr_task(svc, run, pr_number=62)
                wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
                svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

                async def _fake_create_62(task_id, _wt=wt):
                    return _wt

                svc._worktree_managers["default"].create = _fake_create_62
                _patch_execute_push(monkeypatch, rec)

                run(svc.dispatch_pr_fix(task.id, "fix conflicts"))
                run(asyncio.sleep(0.5))

                row = run(db.get_task(task.id))
                assert row.status == "needs_input", f"precondition: expected needs_input, got {row.status!r}"

                head_shared = subprocess.run(
                    ["git", "-C", str(wt), "show", "HEAD:shared.txt"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert "<<<<<<<" not in head_shared and ">>>>>>>" not in head_shared, (
                    "commit posthook committed conflict markers into HEAD on a NEEDS_INPUT escalation. "
                    f"HEAD:shared.txt was:\n{head_shared!r}"
                )

                merge_head = subprocess.run(
                    ["git", "-C", str(wt), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                    capture_output=True,
                    text=True,
                )
                assert merge_head.returncode == 0, (
                    "MERGE_HEAD must still exist after NEEDS_INPUT — the merge must not be "
                    "completed by a posthook before the agent has resolved the conflicts."
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise

    def test_needs_input_persists_question(self, tmp_path, run, monkeypatch):
        """The NEEDS_INPUT question must be stored as a ``type='question'`` message.

        Pre-fix failure: Phase 1 blocks; runner never runs; no question row written.
        """
        svc, db = self._make_needs_input_svc(tmp_path, run)
        rec = _PushRecorder()
        try:
            run(svc.start())
            try:
                task = _stage_waiting_pr_task(svc, run, pr_number=61)
                wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
                svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

                async def _fake_create_61(task_id, _wt=wt):
                    return _wt

                svc._worktree_managers["default"].create = _fake_create_61
                _patch_execute_push(monkeypatch, rec)

                run(svc.dispatch_pr_fix(task.id, "fix conflicts"))
                run(asyncio.sleep(0.5))

                questions = run(db.get_messages(task.id, msg_type="question"))
                assert questions, (
                    "A NEEDS_INPUT from resolve_conflicts must produce a type='question' message. "
                    "Pre-fix: no question row because Phase 1 blocked before agent ran."
                )
                joined = " ".join(m.content for m in questions)
                assert "registry" in joined or "inline" in joined, (
                    f"Question content must include the agent's question text; got: {joined!r}"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise


# ---------------------------------------------------------------------------
# 8. answer() re-dispatches resolve_conflicts with operator decision
# ---------------------------------------------------------------------------


class TestAnswerRedispatchesResolveConflicts:
    """``answer()`` on a ``needs_input`` resolve_conflicts task must re-dispatch
    resolve_conflicts with the operator's answer under ``## Revision Feedback``.

    Pre-fix failure: ``answer()`` looks up ``current_step`` in the process
    catalog (with the sub-flow catalog fallback). Since ``resolve_conflicts``
    doesn't exist in the catalog, it raises ``AnswerNotAllowed``.
    """

    def _make_answer_svc(self, tmp_path, run):
        from lotsa import registry as reg
        from lotsa.registry import register_tool
        from lotsa.tools import ToolResult

        reg._TOOLS.pop("push_pr", None)

        async def _stub_push_pr(ctx, cfg):
            return ToolResult(success=True, output="stub", metadata={})

        register_tool("push_pr", _stub_push_pr)

        (tmp_path / "data").mkdir(exist_ok=True)
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        class _RecordingRunner:
            def __init__(self):
                self.calls: list[dict] = []
                self._gate = asyncio.Event()

            def dispatch_shape_prompt(self) -> str:
                return ""

            async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
                self.calls.append({"user_prompt": user_prompt})
                await self._gate.wait()
                return AgentResult(
                    success=True, stdout="CONFLICTS_RESOLVED: done", stderr="", return_code=0, duration_ms=1
                )

        svc = OrchestratorService(config, db)
        svc.runner = _RecordingRunner()
        return svc, db

    def test_answer_redispatches_resolve_conflicts(self, tmp_path, run):
        """``answer()`` on a parked resolve_conflicts task must transition back
        to ``(status=working, state=resolving_conflicts)`` and queue a new agent.

        Pre-fix failure: ``answer()`` cannot find resolve_conflicts in the
        catalog, raises ``AnswerNotAllowed``.
        """
        from lotsa.orchestrator import AnswerNotAllowed

        svc, db = self._make_answer_svc(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = run(
                    db.create_task(
                        "answer-test",
                        state="resolving_conflicts",
                        metadata={"pr_number": 70, "current_flow": "pr_fix"},
                    )
                )
                run(db.add_message(task.id, "agent", "spec", "spec", "artifact", metadata={"artifact_name": "spec"}))
                run(db.add_message(task.id, "agent", "plan", "plan", "artifact", metadata={"artifact_name": "plan"}))
                # Park the task at needs_input manually to simulate the state
                # that would follow NEEDS_INPUT: from resolve_conflicts.
                run(
                    db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="resolving_conflicts",
                        to_status="needs_input",
                        to_current_step="resolve_conflicts",
                    )
                )

                # answer() must not raise
                try:
                    run(svc.answer(task.id, "keep the registry version and re-apply our null-check"))
                except AnswerNotAllowed as exc:
                    pytest.fail(
                        f"answer() raised AnswerNotAllowed for resolve_conflicts: {exc}. "
                        "Pre-fix: resolve_conflicts not in catalog."
                    )

                run(asyncio.sleep(0.1))

                row = run(db.get_task(task.id))
                assert row.status == "working", f"answer() must flip status back to working, got {row.status!r}"
                assert row.state == "resolving_conflicts"
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise

    def test_answer_injects_decision_under_revision_feedback(self, tmp_path, run):
        """The operator's answer must appear under ``## Revision Feedback`` in
        the re-dispatched agent's prompt.

        Pre-fix failure: answer() raises before any runner call happens.
        """
        svc, db = self._make_answer_svc(tmp_path, run)
        try:
            run(svc.start())
            try:
                task = run(
                    db.create_task(
                        "feedback-injection",
                        state="resolving_conflicts",
                        metadata={"pr_number": 71, "current_flow": "pr_fix"},
                    )
                )
                run(db.add_message(task.id, "agent", "spec", "spec", "artifact", metadata={"artifact_name": "spec"}))
                run(db.add_message(task.id, "agent", "plan", "plan", "artifact", metadata={"artifact_name": "plan"}))
                run(
                    db.claim_task_transition(
                        task.id,
                        from_status=task.status,
                        from_state=task.state,
                        to_state="resolving_conflicts",
                        to_status="needs_input",
                        to_current_step="resolve_conflicts",
                    )
                )

                answer_text = "keep the registry version, discard inline"
                run(svc.answer(task.id, answer_text))
                run(asyncio.sleep(0.1))

                assert svc.runner.calls, "agent must have been dispatched by answer()"
                last_call = svc.runner.calls[-1]
                assert "## Revision Feedback" in last_call["user_prompt"], (
                    "answer() must inject under '## Revision Feedback'"
                )
                assert answer_text in last_call["user_prompt"], (
                    f"Operator decision must appear in the prompt; prompt: {last_call['user_prompt'][:300]!r}"
                )
            finally:
                run(svc.shutdown())
                run(db.close())
        except Exception:
            run(db.close())
            raise


# ---------------------------------------------------------------------------
# 9. Backward compat — process without resolve_conflicts still blocks on conflict
# ---------------------------------------------------------------------------


def test_conflict_blocks_when_process_has_no_resolve_conflicts_step(tmp_path, run, monkeypatch):
    """When the active process has no ``resolve_conflicts`` job, the conflict
    path must fall back to the Phase-1 block (no crash, no silent no-op).

    Pre-fix: the conflict path always blocks; this test exists to document and
    protect the backward-compat guarantee so adding the Phase-2 dispatch doesn't
    break custom processes.

    This test runs against a *custom* process (not ``full``) that has a pr-fix
    flow but no resolve_conflicts job. After Phase 2 ships the custom process
    must still block on conflict.

    Pre-fix failure shape: the custom process doesn't have resolve_conflicts;
    after Phase 2 the dispatch funnel must detect its absence and fall back to
    blocking. If it crashes or silently dispatches the wrong step the assertion
    fails.
    """
    import yaml

    from lotsa import registry as reg
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    reg._TOOLS.pop("push_pr", None)

    async def _push_stub(ctx, cfg):
        return ToolResult(success=True, output="stub", metadata={})

    register_tool("push_pr", _push_stub)

    # Minimal custom process with pr-fix flow but WITHOUT resolve_conflicts
    process_yaml = tmp_path / "custom_process.yaml"
    process_yaml.write_text(
        yaml.dump(
            {
                "process": "custom",
                "jobs": [
                    {"name": "coding", "type": "agent", "prompt": "coding"},
                    {
                        "name": "pr-fix",
                        "type": "agent",
                        "prompt": "pr-fix",
                        "model": "sonnet",
                        "rules": [
                            {"source": "stdout", "pattern": "^PR_FIX_DONE:", "target": "next"},
                            {"source": "stdout", "pattern": "^PR_FIX_SKIPPED:", "target": "wait_for_pr_signal"},
                            {"source": "stdout", "pattern": "^PR_FIX_BLOCKED:", "target": "blocked"},
                        ],
                    },
                    {"name": "push_pr", "type": "action", "tool": "push_pr"},
                    {
                        "name": "wait_for_pr_signal",
                        "type": "monitor",
                        "engine": "pr_monitor",
                        "config": {"poll_interval_seconds": 30, "debounce_seconds": 0},
                    },
                ],
                "flows": {
                    "main": {"steps": ["coding", "push_pr", "wait_for_pr_signal"]},
                    "pr_fix": {
                        "steps": [
                            {
                                "name": "pr-fix",
                                "rules": [
                                    {"source": "stdout", "pattern": "^PR_FIX_DONE:", "target": "next"},
                                    {"source": "stdout", "pattern": "^PR_FIX_SKIPPED:", "target": "wait_for_pr_signal"},
                                    {"source": "stdout", "pattern": "^PR_FIX_BLOCKED:", "target": "blocked"},
                                ],
                            },
                            "push_pr",
                        ]
                    },
                },
            }
        )
    )

    (tmp_path / "data").mkdir(exist_ok=True)
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="custom",
        flow_file=process_yaml,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())

    svc = OrchestratorService(config, db)
    svc.runner = _RecordingHangRunner()
    run(svc.start())

    task = run(db.create_task("custom-conflict", state="wait_for_pr_signal", metadata={"pr_number": 80}))
    run(db.add_message(task.id, "agent", "spec", "s", "artifact", metadata={"artifact_name": "spec"}))
    run(db.add_message(task.id, "agent", "plan", "p", "artifact", metadata={"artifact_name": "plan"}))
    run(
        db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state="wait_for_pr_signal",
            to_status="waiting_for_pr",
            to_current_step="wait_for_pr_signal",
        )
    )

    wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
    svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
    rec = _PushRecorder()
    _patch_execute_push(monkeypatch, rec)

    try:
        run(svc.dispatch_pr_fix(task.id, "feedback"))
        run(asyncio.sleep(0.1))

        row = run(db.get_task(task.id))
        # The process has no resolve_conflicts job → must fall back to block.
        assert row.status == "blocked", (
            f"A custom process without resolve_conflicts must still block on conflict, "
            f"got status={row.status!r}. Phase 2 must detect the missing step and fall back."
        )
        assert svc.runner.calls == [], "no agent should be dispatched for a blocked conflict"
    finally:
        run(svc.shutdown())
        run(db.close())


# ---------------------------------------------------------------------------
# 10. Cross-flow SM edges for resolve_conflicts
# ---------------------------------------------------------------------------


def test_resolve_conflicts_monitor_entry_edge_in_main_sm():
    """``(wait_for_pr_signal, resolving_conflicts)`` must be registered in
    main's SM so ``_dispatch_pr_fix_locked`` can CAS the task into the
    resolve_conflicts queue state from the monitor state.

    Pre-fix failure: ``resolve_conflicts`` job doesn't exist in the process,
    so ``resolving_conflicts`` is not a known state; the edge is absent.
    """
    process = build_process("build")
    main_sm = process.flows["main"].state_machine
    resolve_step = next((rj for rj in process.flows["pr_fix"].jobs if rj.name == "resolve_conflicts"), None)
    assert resolve_step is not None, (
        "resolve_conflicts must be a job in the pr_fix sub-flow of the full process. Add it to full/process.yaml."
    )
    assert ("wait_for_pr_signal", resolve_step.queue_state) in main_sm.transitions, (
        f"main's SM must contain the sub-flow entry edge "
        f"(wait_for_pr_signal, {resolve_step.queue_state!r}). "
        "The cross-flow registrar must register all sub-flow step entries, not only bindings[0]."
    )


def test_resolve_conflicts_monitor_entry_edge_in_pr_fix_sm():
    """The entry edge must also be in pr_fix's SM, because ``_dispatch_step``
    resolves ``active_flow`` to pr_fix (via metadata.current_flow) and validates
    the pre-CAS transition against that SM.

    Pre-fix failure: even if main's SM carries the edge, pr_fix's SM won't,
    so the transition guard silently no-ops.
    """
    process = build_process("build")
    pr_fix_sm = process.flows["pr_fix"].state_machine
    resolve_step = next((rj for rj in process.flows["pr_fix"].jobs if rj.name == "resolve_conflicts"), None)
    assert resolve_step is not None, "resolve_conflicts must be in pr_fix sub-flow"
    assert ("wait_for_pr_signal", resolve_step.queue_state) in pr_fix_sm.transitions, (
        f"pr_fix's SM must also contain (wait_for_pr_signal, {resolve_step.queue_state!r}). "
        "_dispatch_step resolves active_flow=pr_fix via current_flow metadata before CAS."
    )


def test_pr_fixing_to_resolving_conflicts_edge_in_pr_fix_sm():
    """The retry() conflict path dispatches resolve_conflicts from state
    ``pr-fixing`` (the task was mid pr-fix when a new sync found conflicts).
    ``_dispatch_step`` validates ``(item.state, resolve_step.active_state)``
    against the active flow's SM before CAS — without this edge it silently
    no-ops, leaving the task stuck at ``working/pr-fixing`` with no agent.

    This edge is implicitly derived from resolve_conflicts sitting immediately
    after pr-fix in ``pr_fix.steps`` (pr-fix's success_state). Inserting any
    step between them would change pr-fix's success_state and drop this edge.
    This test protects that invariant.
    """
    process = build_process("build")
    pr_fix_sm = process.flows["pr_fix"].state_machine
    assert ("pr-fixing", "resolving_conflicts") in pr_fix_sm.transitions, (
        "pr_fix SM must have ('pr-fixing', 'resolving_conflicts'). "
        "The retry() conflict path dispatches resolve_conflicts from pr-fixing state; "
        "without this edge _dispatch_step silently no-ops."
    )


def test_conflicts_resolved_rule_edge_in_sm():
    """The ``CONFLICTS_RESOLVED → pr-fix`` rule-target edge must be registered
    in both pr_fix's and main's SM so the drainer can CAS the forward step.

    Pre-fix failure: no rule → no edge registered by _build_state_machine.
    """
    process = build_process("build")
    resolve_step = next((rj for rj in process.flows["pr_fix"].jobs if rj.name == "resolve_conflicts"), None)
    assert resolve_step is not None, "resolve_conflicts must exist in pr_fix"

    pr_fix_job = next(rj for rj in process.flows["pr_fix"].jobs if rj.name == "pr-fix")

    # The rule target is "pr-fix" (job name); its queue_state is "pr-fixing".
    pr_fix_sm = process.flows["pr_fix"].state_machine
    assert (resolve_step.active_state, pr_fix_job.queue_state) in pr_fix_sm.transitions, (
        f"pr_fix SM must have ({resolve_step.active_state!r}, {pr_fix_job.queue_state!r}) "
        "for the CONFLICTS_RESOLVED → pr-fix rule routing."
    )


# ---------------------------------------------------------------------------
# 11. full process.yaml — resolve_conflicts job and flow binding
# ---------------------------------------------------------------------------


def test_full_process_has_resolve_conflicts_job():
    """``resolve_conflicts`` must be in the full process's job catalog.

    Pre-fix failure: it's not in full/process.yaml.
    """
    process = build_process("build")
    names = [j.name for j in process.jobs]
    assert "resolve_conflicts" in names, f"resolve_conflicts must be in full process jobs. Got: {names}"


def test_full_process_resolve_conflicts_is_agent_type():
    process = build_process("build")
    job = next(j for j in process.jobs if j.name == "resolve_conflicts")
    assert job.type == "agent", f"resolve_conflicts must be type=agent, got {job.type!r}"


def test_full_process_resolve_conflicts_has_commit_posthook():
    """The ``commit`` posthook must be declared so the orchestrator
    completes the merge commit deterministically after the agent edits."""
    process = build_process("build")
    # The job-level posthooks are on the raw Job; resolve through process.jobs.
    job = next(j for j in process.jobs if j.name == "resolve_conflicts")
    assert "commit" in (job.posthooks or []), (
        f"resolve_conflicts must declare posthooks: [commit]. Got: {job.posthooks!r}"
    )


def test_full_process_resolve_conflicts_has_no_model():
    process = build_process("build")
    job = next(j for j in process.jobs if j.name == "resolve_conflicts")
    assert job.model is None, (
        f"resolve_conflicts must not pin a model (bundled process ships no per-step models — "
        f"PR #131 policy); got {job.model!r}"
    )


def test_full_process_resolve_conflicts_has_resume():
    process = build_process("build")
    job = next(j for j in process.jobs if j.name == "resolve_conflicts")
    assert job.resume_session is True, (
        "resolve_conflicts must have resume=true so the agent can continue a session "
        "across operator answer() re-dispatch."
    )


def test_full_process_pr_fix_flow_contains_resolve_conflicts():
    """``resolve_conflicts`` must appear in the ``pr_fix`` sub-flow's bindings."""
    process = build_process("build")
    pr_fix = process.flows["pr_fix"]
    names = [b.name for b in pr_fix.bindings]
    assert "resolve_conflicts" in names, (
        f"pr_fix sub-flow must contain resolve_conflicts binding. Got bindings: {names}"
    )


def test_full_process_resolve_conflicts_has_conflicts_resolved_rule():
    """``resolve_conflicts`` must declare a ``CONFLICTS_RESOLVED:`` output rule
    routing to ``pr-fix``."""
    process = build_process("build")
    # The per-flow rule override in the pr_fix binding takes precedence, but the
    # job-level default is also acceptable. Check the resolved FlowStep via the
    # pr_fix flow's job list.
    resolve_step = next((rj for rj in process.flows["pr_fix"].jobs if rj.name == "resolve_conflicts"), None)
    assert resolve_step is not None
    rule_targets = {r.target for r in resolve_step.rules}
    assert "pr-fix" in rule_targets, (
        f"resolve_conflicts must have a rule with target='pr-fix' (CONFLICTS_RESOLVED path). "
        f"Got rule targets: {rule_targets}"
    )
    patterns = {r.pattern for r in resolve_step.rules}
    assert any("CONFLICTS_RESOLVED" in p for p in patterns), (
        f"resolve_conflicts must have a rule matching CONFLICTS_RESOLVED. Patterns: {patterns}"
    )


def test_full_process_pr_fix_binding_is_still_first():
    """``pr-fix`` must remain bindings[0] of the pr_fix sub-flow so the existing
    cross-flow entry edge (monitor → pr-fixing) is preserved by the registrar's
    bindings[0] registration.

    Adding resolve_conflicts must NOT reorder pr-fix away from position 0.
    """
    process = build_process("build")
    first_binding = process.flows["pr_fix"].bindings[0]
    assert first_binding.name == "pr-fix", (
        f"pr-fix must remain bindings[0] of pr_fix. Got bindings[0].name={first_binding.name!r}. "
        "Moving pr-fix from position 0 breaks the cross-flow entry-edge registration for "
        "the existing clean-path dispatch."
    )


# ---------------------------------------------------------------------------
# 12. full process.yaml — wait_for_pr_signal carries merge_conflict trigger
# ---------------------------------------------------------------------------


def test_full_process_wait_for_pr_signal_has_merge_conflict_trigger():
    """The bundled ``full`` process's monitor job must declare ``merge_conflict``
    in its ``triggers:`` list so a CONFLICTING PR wakes the pr-fix dispatch path.

    Pre-fix failure: the trigger isn't in full/process.yaml's monitor config.
    """
    process = build_process("build")
    monitor_job = next(j for j in process.jobs if j.name == "wait_for_pr_signal")
    triggers = monitor_job.config.get("triggers", []) if hasattr(monitor_job, "config") else []
    assert "merge_conflict" in triggers, (
        f"wait_for_pr_signal must include 'merge_conflict' in its triggers. Got: {triggers}"
    )


def test_full_process_monitor_config_parses_merge_conflict_trigger():
    """The monitor config's trigger list (including merge_conflict) must pass
    ``parse_config`` validation — the trigger must be whitelisted in
    ``_VALID_TRIGGERS``.

    Pre-fix failure: parse_config raises ValueError on the unknown trigger.
    """
    process = build_process("build")
    monitor_job = next(j for j in process.jobs if j.name == "wait_for_pr_signal")
    raw_config = dict(monitor_job.config) if hasattr(monitor_job, "config") else {}
    # Should not raise.
    parsed = parse_config(raw_config)
    assert "merge_conflict" in parsed.triggers


# ---------------------------------------------------------------------------
# 13. retry() on a blocked pr-fix task uses Phase 2 conflict path
# ---------------------------------------------------------------------------


def test_retry_conflict_dispatches_resolve_conflicts_not_blocked(tmp_path, run, full_service, monkeypatch):
    """``retry()`` on a pr-fix task must use the Phase-2 conflict path —
    dispatch ``resolve_conflicts`` — not block via the stale Phase-1
    ``_sync_or_block`` helper.

    Regression for the asymmetric-dispatcher bug: ``_dispatch_pr_fix_locked``
    was migrated to Phase 2 but ``retry()`` still called ``_sync_or_block``
    which (a) routes conflicts to ``blocked`` instead of dispatching
    ``resolve_conflicts``, and (b) no longer runs ``git merge --abort`` (that
    line was removed in Phase 2), so a retry-path conflict leaves markers and
    MERGE_HEAD in the worktree — a permanent stuck loop.

    Pre-fix failure: ``retry()`` calls ``_sync_or_block``, which routes to
    ``blocked``. The assertion on ``status=working`` / ``state=resolving_conflicts``
    fails and the test also catches the stuck-loop regression (a second
    ``retry()`` would re-hit a dirty worktree).
    """
    svc = full_service
    # Stage a blocked pr-fix task (simulates a task that was blocked by e.g.
    # a fetch error during a prior pr-fix round, and has since drifted).
    task = run(
        svc.db.create_task(
            "retry-conflict",
            state="pr-fixing",
            metadata={"pr_number": 50, "current_flow": "pr_fix"},
        )
    )
    run(svc.db.add_message(task.id, "agent", "spec", "spec", "artifact", metadata={"artifact_name": "spec"}))
    run(svc.db.add_message(task.id, "agent", "plan", "plan", "artifact", metadata={"artifact_name": "plan"}))
    # Simulate the task already at blocked (pr-fix was previously blocked).
    run(
        svc.db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state="pr-fixing",
            to_status="blocked",
            to_current_step="pr-fix",
        )
    )

    # Set up a worktree that will conflict on sync.
    wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
    svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
    rec = _PushRecorder()
    _patch_execute_push(monkeypatch, rec)

    run(svc.retry(task.id))
    run(asyncio.sleep(0.1))

    row = run(svc.db.get_task(task.id))
    assert row.status == "working", (
        f"retry() on a conflicting sync must dispatch resolve_conflicts "
        f"(status=working), got status={row.status!r}. "
        "Pre-fix: _sync_or_block routes to status=blocked."
    )
    assert row.state == "resolving_conflicts", (
        f"Expected state=resolving_conflicts after retry() conflict, got state={row.state!r}."
    )
    assert len(svc.runner.calls) == 1, "retry() conflict path must dispatch exactly one agent (resolve_conflicts)"
    assert rec.calls == [], "conflicted retry sync must not push"
