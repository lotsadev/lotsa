"""Tests for lotsa.pr_monitor — signal classification, feedback aggregation, debounce."""

from __future__ import annotations

import time

import pytest

from lotsa.engines.pr_monitor import PrMonitorConfig as PrConfig
from lotsa.github_client import CheckStatus, PrInfo, ReviewComment
from lotsa.pr_monitor import MonitoredPr, PrSignal, aggregate_feedback, classify_signals

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
    return PrInfo(
        number=1,
        state=state,
        merged=merged,
        review_decision=review_decision,
        head_sha=head_sha,
        base_branch=base_branch,
        mergeable=mergeable,
    )


def _comment(
    *,
    author: str = "alice",
    body: str = "looks good",
    path: str | None = None,
    line: int | None = None,
) -> ReviewComment:
    return ReviewComment(
        id=1,
        author=author,
        body=body,
        path=path,
        line=line,
        created_at="2024-01-01T00:00:00Z",
    )


def _checks(
    *, failing: int = 0, passing: int = 1, pending: int = 0, failing_names: list[str] | None = None
) -> CheckStatus:
    return CheckStatus(
        total=failing + passing + pending,
        passing=passing,
        failing=failing,
        pending=pending,
        failing_names=failing_names or [],
    )


def _config(**kwargs) -> PrConfig:
    defaults = dict(
        triggers=["human_comment", "bot_comment", "review_decision", "failing_check"],
    )
    defaults.update(kwargs)
    return PrConfig(**defaults)


# ---------------------------------------------------------------------------
# classify_signals — COMPLETE cases
# ---------------------------------------------------------------------------


def test_merged_pr_returns_complete():
    pr = _pr(merged=True, state="closed")
    signal = classify_signals(pr, [], [], _checks(), _config())
    assert signal == PrSignal.COMPLETE


def test_approved_pr_does_not_complete():
    """An APPROVED review must NOT mark the PR complete — only the merge does.

    Bots and reviewers routinely mark PRs APPROVED while leaving actionable
    inline feedback; treating APPROVED as terminal would short-circuit the
    feedback loop.
    """
    pr = _pr(review_decision="APPROVED")
    # With triggers=[] the result should be NONE — APPROVED alone is never COMPLETE.
    result = classify_signals(pr, [], [], _checks(), _config(triggers=[]))
    assert result == PrSignal.NONE


def test_approved_pr_with_failing_checks_is_feedback_not_complete():
    """APPROVED review + failing CI must produce FEEDBACK, not COMPLETE."""
    pr = _pr(review_decision="APPROVED")
    checks = _checks(failing=1, failing_names=["lint"])
    signal = classify_signals(pr, [], [], checks, _config(triggers=["failing_check"]))
    assert signal == PrSignal.FEEDBACK


# ---------------------------------------------------------------------------
# classify_signals — ABANDONED
# ---------------------------------------------------------------------------


def test_closed_not_merged_is_abandoned():
    pr = _pr(state="closed", merged=False)
    signal = classify_signals(pr, [], [], _checks(), _config())
    assert signal == PrSignal.ABANDONED


# ---------------------------------------------------------------------------
# classify_signals — FEEDBACK cases
# ---------------------------------------------------------------------------


def test_changes_requested_review_decision():
    pr = _pr(review_decision="CHANGES_REQUESTED")
    signal = classify_signals(pr, [], [], _checks(), _config())
    assert signal == PrSignal.FEEDBACK


def test_stale_review_filtered_by_push_timestamp():
    """Old CHANGES_REQUESTED before push_timestamp should not trigger FEEDBACK."""
    pr = _pr(review_decision="CHANGES_REQUESTED")
    old_review = {"state": "CHANGES_REQUESTED", "user": {"login": "bob"}, "submitted_at": "2024-01-01T00:00:00Z"}
    signal = classify_signals(
        pr,
        [old_review],
        [],
        _checks(),
        _config(),
        push_timestamp="2024-06-01T00:00:00Z",
    )
    assert signal == PrSignal.NONE


def test_recent_review_passes_push_timestamp_filter():
    """CHANGES_REQUESTED after push_timestamp should trigger FEEDBACK."""
    pr = _pr(review_decision="CHANGES_REQUESTED")
    new_review = {"state": "CHANGES_REQUESTED", "user": {"login": "bob"}, "submitted_at": "2024-07-01T00:00:00Z"}
    signal = classify_signals(
        pr,
        [new_review],
        [],
        _checks(),
        _config(),
        push_timestamp="2024-06-01T00:00:00Z",
    )
    assert signal == PrSignal.FEEDBACK


def test_human_comment_triggers_feedback():
    pr = _pr()
    comments = [_comment(author="alice")]
    signal = classify_signals(pr, [], comments, _checks(), _config())
    assert signal == PrSignal.FEEDBACK


def test_bot_comment_triggers_feedback():
    pr = _pr()
    comments = [_comment(author="dependabot[bot]")]
    signal = classify_signals(pr, [], comments, _checks(), _config())
    assert signal == PrSignal.FEEDBACK


def test_bot_comment_not_triggered_when_bot_comment_not_in_triggers():
    pr = _pr()
    comments = [_comment(author="dependabot[bot]")]
    signal = classify_signals(pr, [], comments, _checks(), _config(triggers=["human_comment"]))
    assert signal == PrSignal.NONE


def test_human_comment_not_triggered_when_not_in_triggers():
    pr = _pr()
    comments = [_comment(author="alice")]
    signal = classify_signals(pr, [], comments, _checks(), _config(triggers=["bot_comment"]))
    assert signal == PrSignal.NONE


def test_review_body_triggers_human_comment_feedback():
    """A COMMENTED review with a body should trigger human_comment feedback."""
    pr = _pr()
    reviews = [{"state": "COMMENTED", "user": {"login": "alice"}, "body": "I have concerns", "submitted_at": ""}]
    signal = classify_signals(pr, reviews, [], _checks(), _config(triggers=["human_comment"]))
    assert signal == PrSignal.FEEDBACK


def test_approved_review_body_triggers_human_comment_feedback():
    """An APPROVED review with a body should trigger human_comment feedback."""
    pr = _pr()
    reviews = [{"state": "APPROVED", "user": {"login": "alice"}, "body": "LGTM but fix the typo", "submitted_at": ""}]
    signal = classify_signals(pr, reviews, [], _checks(), _config(triggers=["human_comment"]))
    assert signal == PrSignal.FEEDBACK


def test_review_body_empty_does_not_trigger():
    """A review with an empty body should not trigger feedback."""
    pr = _pr()
    reviews = [{"state": "COMMENTED", "user": {"login": "alice"}, "body": "", "submitted_at": ""}]
    signal = classify_signals(pr, reviews, [], _checks(), _config(triggers=["human_comment"]))
    assert signal == PrSignal.NONE


def test_review_body_filtered_by_push_timestamp():
    """Review bodies older than push_timestamp should not trigger feedback."""
    pr = _pr()
    reviews = [
        {
            "state": "COMMENTED",
            "user": {"login": "alice"},
            "body": "Old feedback",
            "submitted_at": "2024-01-01T00:00:00Z",
        }
    ]
    signal = classify_signals(
        pr, reviews, [], _checks(), _config(triggers=["human_comment"]), push_timestamp="2024-06-01T00:00:00Z"
    )
    assert signal == PrSignal.NONE


def test_bot_review_body_does_not_trigger_human_comment():
    """A bot review body should not trigger human_comment feedback."""
    pr = _pr()
    reviews = [{"state": "COMMENTED", "user": {"login": "codecov[bot]"}, "body": "Coverage report", "submitted_at": ""}]
    signal = classify_signals(pr, reviews, [], _checks(), _config(triggers=["human_comment"]))
    assert signal == PrSignal.NONE


def test_failing_checks_trigger_feedback():
    pr = _pr()
    checks = _checks(failing=2, failing_names=["lint", "test"])
    signal = classify_signals(pr, [], [], checks, _config())
    assert signal == PrSignal.FEEDBACK


def test_failing_checks_not_triggered_when_not_in_triggers():
    pr = _pr()
    checks = _checks(failing=1, failing_names=["lint"])
    signal = classify_signals(pr, [], [], checks, _config(triggers=["human_comment"]))
    assert signal == PrSignal.NONE


# ---------------------------------------------------------------------------
# classify_signals — NONE
# ---------------------------------------------------------------------------


def test_open_pr_no_signals_returns_none():
    pr = _pr()
    signal = classify_signals(pr, [], [], _checks(failing=0), _config())
    assert signal == PrSignal.NONE


def test_empty_triggers_always_none():
    pr = _pr(review_decision="CHANGES_REQUESTED")
    # review_decision trigger not in list
    signal = classify_signals(pr, [], [], _checks(failing=1), _config(triggers=[]))
    assert signal == PrSignal.NONE


# ---------------------------------------------------------------------------
# classify_signals — merge_conflict debounce
# ---------------------------------------------------------------------------


def test_merge_conflict_fires_feedback_on_first_sighting():
    """A CONFLICTING PR with the merge_conflict trigger fires FEEDBACK so the
    sync-before-pr-fix path gets a chance to rebase/resolve."""
    pr = _pr(mergeable=False, head_sha="conflictsha")
    signal = classify_signals(pr, [], [], _checks(), _config(triggers=["merge_conflict"]))
    assert signal == PrSignal.FEEDBACK


def test_merge_conflict_debounced_on_unchanged_head_sha():
    """A persistent conflict must fire FEEDBACK once per conflicting commit —
    not every poll.

    Regression for internal tasks / 04ee0735 (PR #143 / #144): a PR stuck at
    ``mergeable=False`` re-classified as FEEDBACK on every ~3-min poll. Each
    dispatch ran a comment-driven pr-fix that found no new comments, benign-
    skipped, never pushed — so the conflict persisted and the loop burned the
    round cap into a false ``budget exhausted`` block. Once we've dispatched
    for a given conflicting ``head_sha``, the same unchanged commit must NOT
    re-fire; only a new commit (different head_sha) re-attempts.

    Pre-fix red: ``classify_signals`` had no ``conflict_dispatched_sha`` param
    and returned FEEDBACK unconditionally on ``mergeable is False``.
    """
    cfg = _config(triggers=["merge_conflict"])
    pr = _pr(mergeable=False, head_sha="conflictsha")

    # Already dispatched for THIS conflicting commit → suppressed (no re-loop).
    assert classify_signals(pr, [], [], _checks(), cfg, conflict_dispatched_sha="conflictsha") == PrSignal.NONE

    # A new commit on the branch → conflict re-evaluated → FEEDBACK again.
    pr_new = _pr(mergeable=False, head_sha="newsha")
    assert classify_signals(pr_new, [], [], _checks(), cfg, conflict_dispatched_sha="conflictsha") == PrSignal.FEEDBACK


@pytest.mark.asyncio
async def test_merge_conflict_debounce_round_trips_through_metadata(monkeypatch):
    """The conflict debounce must survive a dispatch cycle via task metadata.

    ``_on_feedback`` pops the tracked entry on every dispatch, so the debounce
    cursor only persists if it round-trips through ``pr_conflict_dispatched_sha``
    in task metadata and is restored on the next first-poll. This exercises the
    two links the ``classify_signals`` unit tests can't reach — the
    ``_on_feedback`` write and the ``_poll_one`` restore — i.e. the actual
    loop-prevention for internal tasks / 04ee0735.

    Pre-fix red: without the persist+restore, poll 3 re-dispatches (the loop).
    """
    from types import SimpleNamespace

    from lotsa.github_client import CheckStatus, PrInfo
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeDB:
        def __init__(self, metadata):
            self.metadata = dict(metadata)

        async def get_task(self, task_id):
            return SimpleNamespace(metadata=dict(self.metadata))

        async def update_task(self, task_id, metadata=None, **_):
            if metadata is not None:
                self.metadata = dict(metadata)

    class FakeOrchestrator:
        def __init__(self, db):
            self.db = db
            self.dispatched: list[tuple[str, str]] = []

        async def dispatch_pr_fix(self, task_id, feedback):
            self.dispatched.append((task_id, feedback))
            return True

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1,
                state="open",
                merged=False,
                review_decision=None,
                head_sha="conflictsha",
                base_branch="main",
                mergeable=False,
            )

        async def get_reviews(self, pr_number):
            return []

        async def get_review_comments(self, pr_number, since=None):
            return []

        async def get_issue_comments(self, pr_number, since=None):
            return []

        async def get_check_status(self, sha):
            return CheckStatus(total=1, passing=1, failing=0, pending=0)

        async def close(self):
            pass

    db = FakeDB({"pr_number": 1, "github_owner": "o", "github_repo": "r"})
    orch = FakeOrchestrator(db)
    monitor = PrMonitor(orch, _config(triggers=["merge_conflict"], debounce_seconds=0))

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    # Each poll re-reads the task from the DB (mirrors list_waiting_pr_tasks).
    def _task():
        return {"id": "t1", "metadata": dict(db.metadata)}

    # Drive the real dispatch cadence: first poll establishes the baseline
    # (terminal-only, no feedback), the next sets the debounce timer, the next
    # dispatches and persists the conflicting head_sha. Three polls → one
    # dispatch.
    for _ in range(3):
        await monitor._poll_one(_task(), 1)
    assert len(orch.dispatched) == 1, f"expected exactly one dispatch, got {len(orch.dispatched)}"
    assert db.metadata.get("pr_conflict_dispatched_sha") == "conflictsha", (
        f"_on_feedback must persist the conflicting head_sha; got {db.metadata.get('pr_conflict_dispatched_sha')!r}"
    )

    # Keep polling the SAME unchanged conflict. The dispatch popped the tracked
    # entry, so the next poll restores pr_conflict_dispatched_sha from metadata
    # and classify suppresses — there must be NO further dispatch. Pre-fix (no
    # persist/restore), these polls would re-dispatch the loop.
    for _ in range(4):
        await monitor._poll_one(_task(), 1)
    assert len(orch.dispatched) == 1, (
        f"merge_conflict must not re-dispatch for the same conflicting commit; got {len(orch.dispatched)} dispatches"
    )


# ---------------------------------------------------------------------------
# aggregate_feedback
# ---------------------------------------------------------------------------


def test_aggregate_feedback_empty_returns_fallback():
    _pr()
    result = aggregate_feedback([], [], _checks(failing=0))
    assert result == "No specific feedback found."


def test_aggregate_feedback_changes_requested_review():
    _pr(review_decision="CHANGES_REQUESTED")
    reviews = [
        {
            "state": "CHANGES_REQUESTED",
            "user": {"login": "bob"},
            "body": "Please fix the tests.",
        }
    ]
    result = aggregate_feedback(reviews, [], _checks(failing=0))
    assert "CHANGES_REQUESTED" in result
    assert "bob" in result
    assert "Please fix the tests." in result


def test_aggregate_feedback_inline_comments():
    _pr()
    comments = [_comment(author="carol", body="Nit: rename this.", path="src/foo.py", line=42)]
    result = aggregate_feedback([], comments, _checks(failing=0))
    assert "src/foo.py" in result
    assert "Nit: rename this." in result


def test_aggregate_feedback_pr_comments():
    _pr()
    comments = [_comment(author="dan", body="Overall LGTM but see inline.")]
    result = aggregate_feedback([], comments, _checks(failing=0))
    assert "Overall LGTM" in result
    assert "dan" in result


def test_aggregate_feedback_commented_review_body():
    """COMMENTED review bodies should appear in the aggregated feedback."""
    reviews = [{"state": "COMMENTED", "user": {"login": "carol"}, "body": "Consider using a different approach."}]
    result = aggregate_feedback(reviews, [], _checks(failing=0))
    assert "carol" in result
    assert "Consider using a different approach." in result
    assert "COMMENTED" in result


def test_aggregate_feedback_approved_review_body():
    """APPROVED review bodies should appear in the aggregated feedback."""
    reviews = [{"state": "APPROVED", "user": {"login": "dan"}, "body": "LGTM but rename the helper."}]
    result = aggregate_feedback(reviews, [], _checks(failing=0))
    assert "dan" in result
    assert "rename the helper" in result


def test_aggregate_feedback_empty_review_body_excluded():
    """Reviews with empty bodies should not create a section."""
    reviews = [{"state": "COMMENTED", "user": {"login": "eve"}, "body": ""}]
    result = aggregate_feedback(reviews, [], _checks(failing=0))
    assert result == "No specific feedback found."


def test_aggregate_feedback_dismissed_review_excluded():
    """DISMISSED reviews should not appear in feedback."""
    reviews = [{"state": "DISMISSED", "user": {"login": "frank"}, "body": "Stale review."}]
    result = aggregate_feedback(reviews, [], _checks(failing=0))
    assert result == "No specific feedback found."


def test_aggregate_feedback_failing_checks():
    _pr()
    checks = _checks(failing=2, failing_names=["ci/lint", "ci/test"])
    result = aggregate_feedback([], [], checks)
    assert "ci/lint" in result
    assert "ci/test" in result


def test_aggregate_feedback_combines_all_sections():
    _pr(review_decision="CHANGES_REQUESTED")
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "eve"}, "body": "Fix types."}]
    comments = [
        _comment(author="eve", body="Wrong type here.", path="main.py", line=10),
        _comment(author="frank", body="Looks okay overall."),
    ]
    checks = _checks(failing=1, failing_names=["ci/typecheck"])
    result = aggregate_feedback(reviews, comments, checks)
    # All sections present
    assert "Fix types." in result
    assert "Wrong type here." in result
    assert "Looks okay overall." in result
    assert "ci/typecheck" in result
    # Sections divided
    assert "---" in result


def test_aggregate_feedback_separates_inline_and_pr_comments():
    _pr()
    inline = _comment(author="g", body="inline note", path="x.py", line=1)
    general = _comment(author="h", body="general note")
    result = aggregate_feedback([], [inline, general], _checks(failing=0))
    assert "inline note" in result
    assert "general note" in result


# ---------------------------------------------------------------------------
# MonitoredPr dataclass
# ---------------------------------------------------------------------------


def test_monitored_pr_creation():
    now = time.time()
    monitored = MonitoredPr(
        task_id="task-1",
        pr_number=42,
        owner="acme",
        repo="my-repo",
        last_poll_at=now,
        feedback_first_seen_at=None,
    )
    assert monitored.task_id == "task-1"
    assert monitored.pr_number == 42
    assert monitored.feedback_first_seen_at is None


def test_monitored_pr_feedback_tracking():
    now = time.time()
    monitored = MonitoredPr(
        task_id="task-2",
        pr_number=7,
        owner="org",
        repo="repo",
        last_poll_at=now,
        feedback_first_seen_at=None,
    )
    # Simulate debounce: set feedback_first_seen_at
    monitored.feedback_first_seen_at = now
    assert monitored.feedback_first_seen_at == now

    # Simulate reset after feedback dispatched
    monitored.feedback_first_seen_at = None
    assert monitored.feedback_first_seen_at is None


# ---------------------------------------------------------------------------
# Debounce logic (via classify_signals + MonitoredPr state)
# ---------------------------------------------------------------------------


def test_feedback_first_seen_at_records_initial_feedback_time():
    """Simulates a single poll cycle where feedback is first observed."""
    pr = _pr(review_decision="CHANGES_REQUESTED")
    signal = classify_signals(pr, [], [], _checks(), _config())
    assert signal == PrSignal.FEEDBACK

    # First time seeing feedback — record timestamp
    now = time.time()
    monitored = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=now,
        feedback_first_seen_at=None,
    )
    if signal == PrSignal.FEEDBACK and monitored.feedback_first_seen_at is None:
        monitored.feedback_first_seen_at = now

    assert monitored.feedback_first_seen_at is not None


def test_debounce_does_not_dispatch_before_window():
    """If debounce window hasn't passed, feedback should not be dispatched."""
    debounce_seconds = 120
    now = time.time()
    feedback_first_seen_at = now - 30  # only 30s ago, window is 120s

    should_dispatch = (now - feedback_first_seen_at) >= debounce_seconds
    assert not should_dispatch


def test_debounce_dispatches_after_window():
    """If debounce window has passed, feedback should be dispatched."""
    debounce_seconds = 120
    now = time.time()
    feedback_first_seen_at = now - 150  # 150s ago, window is 120s

    should_dispatch = (now - feedback_first_seen_at) >= debounce_seconds
    assert should_dispatch


# ---------------------------------------------------------------------------
# _on_feedback dispatch failure re-inserts tracked entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_feedback_reinserts_tracked_on_dispatch_failure():
    """When dispatch_fn raises, the tracked entry should be re-inserted for retry."""
    from unittest.mock import AsyncMock

    from lotsa.pr_monitor import PrMonitor

    class FakeOrchestrator:
        dispatch_pr_fix = AsyncMock(side_effect=RuntimeError("DB error"))

    orchestrator = FakeOrchestrator()
    config = _config()
    monitor = PrMonitor(orchestrator, config)

    # Manually insert a tracked PR
    tracked = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=time.time() - 200,
    )
    monitor._tracked["t1"] = tracked

    pr = _pr()
    with pytest.raises(RuntimeError, match="DB error"):
        await monitor._on_feedback("t1", pr, "Fix the tests")

    # tracked should be re-inserted after failed dispatch
    assert "t1" in monitor._tracked
    assert monitor._tracked["t1"] is tracked


@pytest.mark.asyncio
async def test_on_feedback_no_dispatch_fn_preserves_tracked_and_since():
    """When dispatch_pr_fix isn't available, _on_feedback must not pop tracked
    nor advance pr_comments_since — otherwise feedback is silently dropped *and*
    the next poll's since= filter excludes it.
    """
    from unittest.mock import AsyncMock

    from lotsa.pr_monitor import PrMonitor

    update_task = AsyncMock()

    class FakeOrchestrator:
        # No dispatch_pr_fix attribute on purpose.
        db = type("DB", (), {"get_task": AsyncMock(), "update_task": update_task})()

    monitor = PrMonitor(FakeOrchestrator(), _config())
    tracked = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=time.time() - 200,
    )
    monitor._tracked["t1"] = tracked

    await monitor._on_feedback("t1", _pr(), "Fix the tests")

    assert "t1" in monitor._tracked
    assert monitor._tracked["t1"] is tracked
    update_task.assert_not_called()


@pytest.mark.asyncio
async def test_missing_token_blocks_task(monkeypatch):
    """When GITHUB_TOKEN is unset, _poll_one should block the task."""
    from unittest.mock import AsyncMock

    from lotsa.pr_monitor import PrMonitor

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class FakeOrchestrator:
        transition_task = AsyncMock()

    orchestrator = FakeOrchestrator()
    config = _config()
    monitor = PrMonitor(orchestrator, config)

    task = {
        "id": "task-no-token",
        "metadata": {"pr_number": 5, "github_owner": "acme", "github_repo": "repo"},
    }
    await monitor._poll_one(task, 5)

    orchestrator.transition_task.assert_called_once_with("task-no-token", "blocked")


# ---------------------------------------------------------------------------
# First-poll comments_since uses pr_pushed_at when available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_poll_uses_pr_pushed_at_as_cutoff(monkeypatch):
    """When pr_pushed_at is set, first poll's comments_since must equal it.

    Regression: without this, the first-poll cutoff was set to "now", which
    silently filtered out comments posted between push and first poll.
    """
    from unittest.mock import AsyncMock

    from lotsa.github_client import CheckStatus, PrInfo
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        transition_task = AsyncMock()
        db = None

    monitor = PrMonitor(FakeOrchestrator(), _config())

    push_time = "2024-06-01T12:00:00+00:00"

    captured_since: list[str | None] = []

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1, state="open", merged=False, review_decision=None, head_sha="sha", base_branch="main"
            )

        async def get_reviews(self, pr_number):
            return []

        async def get_review_comments(self, pr_number, since=None):
            captured_since.append(since)
            return []

        async def get_issue_comments(self, pr_number, since=None):
            return []

        async def get_check_status(self, sha):
            return CheckStatus(total=0, passing=0, failing=0, pending=0)

        async def close(self):
            pass

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {
        "id": "t-push",
        "metadata": {
            "pr_number": 1,
            "github_owner": "o",
            "github_repo": "r",
            "pr_pushed_at": push_time,
        },
    }
    await monitor._poll_one(task, 1)

    # First poll seeds comments_since from metadata
    assert monitor._tracked["t-push"].comments_since == push_time


# ---------------------------------------------------------------------------
# 404 on get_pr → ABANDONED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_404_transitions_to_abandoned(monkeypatch):
    """Deleted PR (404 from get_pr) should transition the task to abandoned."""
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        transition_task = AsyncMock()
        db = None

    monitor = PrMonitor(FakeOrchestrator(), _config())

    response = MagicMock()
    response.status_code = 404

    class FakeClient:
        async def get_pr(self, pr_number):
            raise httpx.HTTPStatusError("Not Found", request=MagicMock(), response=response)

        async def close(self):
            pass

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {"id": "t-gone", "metadata": {"pr_number": 99, "github_owner": "o", "github_repo": "r"}}
    await monitor._poll_one(task, 99)

    monitor._orchestrator.transition_task.assert_called_once_with("t-gone", "abandoned")


# ---------------------------------------------------------------------------
# Client cache: token rotation closes the old client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_client_token_rotation_closes_old_client():
    """Rotating GITHUB_TOKEN closes the previous cached client and replaces it."""
    from lotsa.pr_monitor import PrMonitor

    closed: list[str] = []

    class FakeClient:
        def __init__(self, token):
            self.token = token

        async def close(self):
            closed.append(self.token)

    def _make(token, owner, repo):
        return FakeClient(token)

    monitor = PrMonitor(object(), _config())
    # Patch the GitHubClient constructor used inside _get_client by
    # intercepting the cache directly with a fake instance.
    fake_v1 = FakeClient("tok-v1")
    monitor._clients[("o", "r")] = (fake_v1, "tok-v1")

    # Call with same token — returns cached, no close.
    result = await monitor._get_client("o", "r", "tok-v1")
    assert result is fake_v1
    assert closed == []

    # Call with rotated token — old client is closed, replacement created.
    new_client = await monitor._get_client("o", "r", "tok-v2")
    assert new_client is not fake_v1
    assert closed == ["tok-v1"]
    # New entry uses the new token.
    cached_client, cached_token = monitor._clients[("o", "r")]
    assert cached_client is new_client
    assert cached_token == "tok-v2"


# ---------------------------------------------------------------------------
# Debounce reset: NONE clears pending feedback timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_resets_when_signal_returns_to_none(monkeypatch):
    """If feedback transiently disappears (NONE), debounce restarts on next FEEDBACK."""
    from lotsa.github_client import CheckStatus, PrInfo
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        async def list_waiting_pr_tasks(self):
            return []

        db = None

    monitor = PrMonitor(FakeOrchestrator(), _config())

    # Pre-seed tracked state with a feedback timer well past the debounce
    # window — without the reset this would dispatch immediately on any
    # FEEDBACK signal.  With the reset, it should clear on NONE.
    monitor._tracked["t1"] = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=time.time() - 10_000,
        comments_since="2024-06-01T00:00:00+00:00",
    )

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1, state="open", merged=False, review_decision=None, head_sha="sha", base_branch="main"
            )

        async def get_reviews(self, pr_number):
            return []

        async def get_review_comments(self, pr_number, since=None):
            return []

        async def get_issue_comments(self, pr_number, since=None):
            return []

        async def get_check_status(self, sha):
            return CheckStatus(total=1, passing=1, failing=0, pending=0)

        async def close(self):
            pass

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}
    await monitor._poll_one(task, 1)

    # Signal was NONE (no reviews, no comments, no failing checks) — the
    # debounce timer should be cleared.
    assert monitor._tracked["t1"].feedback_first_seen_at is None


# ---------------------------------------------------------------------------
# gather_pending_feedback: aggregates pending PR feedback for revise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_pending_feedback_returns_none_when_empty():
    """gather_pending_feedback returns None when no actionable feedback exists."""
    from lotsa.github_client import CheckStatus, PrInfo
    from lotsa.pr_monitor import PrMonitor

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1, state="open", merged=False, review_decision=None, head_sha="sha", base_branch="main"
            )

        async def get_reviews(self, pr_number):
            return []

        async def get_review_comments(self, pr_number, since=None):
            return []

        async def get_issue_comments(self, pr_number, since=None):
            return []

        async def get_check_status(self, sha):
            return CheckStatus(total=1, passing=1, failing=0, pending=0)

        async def close(self):
            pass

    monitor = PrMonitor(object(), _config())

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]
    monitor._tracked["t1"] = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=None,
        comments_since=None,
    )

    result = await monitor.gather_pending_feedback(
        task_id="t1",
        owner="o",
        repo="r",
        pr_number=1,
        token="ghp_test",
        default_since=None,
    )
    assert result is None
    # tracked entry is dropped as a side effect
    assert "t1" not in monitor._tracked


@pytest.mark.asyncio
async def test_gather_pending_feedback_returns_aggregated_text():
    """gather_pending_feedback returns the aggregated payload when feedback exists."""
    from lotsa.github_client import CheckStatus, PrInfo, ReviewComment
    from lotsa.pr_monitor import PrMonitor

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1, state="open", merged=False, review_decision=None, head_sha="sha", base_branch="main"
            )

        async def get_reviews(self, pr_number):
            return [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}, "body": "Please fix this."}]

        async def get_review_comments(self, pr_number, since=None):
            return []

        async def get_issue_comments(self, pr_number, since=None):
            return [
                ReviewComment(
                    id=1,
                    author="bob",
                    body="LGTM but rename helper.",
                    path=None,
                    line=None,
                    created_at="2024-07-01T00:00:00Z",
                )
            ]

        async def get_check_status(self, sha):
            return CheckStatus(total=1, passing=1, failing=0, pending=0)

        async def close(self):
            pass

    monitor = PrMonitor(object(), _config())

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    result = await monitor.gather_pending_feedback(
        task_id="t1",
        owner="o",
        repo="r",
        pr_number=1,
        token="ghp_test",
        default_since=None,
    )
    assert result is not None
    assert "Please fix this." in result
    assert "LGTM but rename helper." in result


@pytest.mark.asyncio
async def test_gather_pending_feedback_reinserts_tracked_on_failure():
    """If the GitHub fan-out raises, the tracked entry must be restored.

    Regression: without re-insertion the next poll cycle would treat the
    task as first-poll, suppressing FEEDBACK detection until the next push.
    """
    from lotsa.pr_monitor import PrMonitor

    class FakeClient:
        async def get_pr(self, pr_number):
            raise RuntimeError("transient GitHub failure")

        async def close(self):
            pass

    monitor = PrMonitor(object(), _config())

    async def _fake_client(owner, repo, token):
        return FakeClient()

    monitor._get_client = _fake_client  # type: ignore[assignment]

    original = MonitoredPr(
        task_id="t1",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=time.time() - 60,
        comments_since="2024-06-01T00:00:00+00:00",
    )
    monitor._tracked["t1"] = original

    with pytest.raises(RuntimeError, match="transient GitHub failure"):
        await monitor.gather_pending_feedback(
            task_id="t1",
            owner="o",
            repo="r",
            pr_number=1,
            token="ghp_test",
            default_since=None,
        )

    # Tracked entry restored — next poll continues debounce instead of restarting.
    assert monitor._tracked.get("t1") is original


# ---------------------------------------------------------------------------
# Phase 1 — R2: pr_feedback audit writes on every captured/updated PR comment
# ---------------------------------------------------------------------------
#
# Every comment fetched by the monitor is persisted as a write-once-per-version
# pr_feedback message row (role="github", type="pr_feedback").  An edit-in-place
# (same comment_id, newer updated_at) produces a new row; an unchanged comment
# is a no-op on the second poll.
#
# The metadata payload required by the spec contains:
#     comment_id, author, url, updated_at, round
# where ``round`` is read from the task's ``pr_fix_round_count`` metadata + 1
# (defaulting to 0 → round=1 until Task 5 lands).


class _FakeMessageStore:
    """Minimal stand-in for TaskDB that captures add_message calls.

    The R2 helper needs ``get_task`` (to read pr_fix_round_count for the
    ``round`` metadata field) and ``add_message`` (to write the audit row).
    Provides just enough surface for those.  No real DB to keep the tests
    fast and isolated from migration state.
    """

    def __init__(self, task_metadata: dict | None = None):
        self.task_metadata = dict(task_metadata or {})
        self.add_message_calls: list[dict] = []

    async def get_task(self, task_id: str):
        # Return a simple object exposing .metadata for read access.
        class _Row:
            pass

        row = _Row()
        row.metadata = dict(self.task_metadata)
        return row

    async def update_task(self, task_id: str, **fields) -> None:
        # Captured but ignored — the dashboard write in _poll_one calls
        # update_task, which we don't assert against in these tests.
        if "metadata" in fields and isinstance(fields["metadata"], dict):
            self.task_metadata = dict(fields["metadata"])

    async def add_message(
        self,
        task_id: str,
        role: str,
        step_name: str,
        content: str,
        msg_type: str,
        metadata: dict | None = None,
    ):
        self.add_message_calls.append(
            {
                "task_id": task_id,
                "role": role,
                "step_name": step_name,
                "content": content,
                "type": msg_type,
                "metadata": dict(metadata or {}),
            }
        )


def _make_pr_feedback_client(comments_by_call: list[list]):
    """Build a FakeClient that returns a fresh list of comments per poll.

    ``comments_by_call`` is indexed by call number (0 → first poll, 1 →
    second poll, etc.).  Issue-comments fan-out gets the list; review-
    comments fan-out is empty in these tests.
    """
    from lotsa.github_client import CheckStatus, PrInfo

    state = {"call": 0}

    class FakeClient:
        async def get_pr(self, pr_number):
            return PrInfo(
                number=1,
                state="open",
                merged=False,
                review_decision=None,
                head_sha="sha",
                base_branch="main",
            )

        async def get_reviews(self, pr_number):
            return []

        async def get_review_comments(self, pr_number, since=None):
            return []

        async def get_issue_comments(self, pr_number, since=None):
            idx = state["call"]
            state["call"] += 1
            if idx < len(comments_by_call):
                return list(comments_by_call[idx])
            return []

        async def get_check_status(self, sha):
            return CheckStatus(total=0, passing=0, failing=0, pending=0)

        async def close(self):
            pass

    return FakeClient()


def _seed_monitor_tracked(monitor, task_id: str = "t1"):
    """Seed _tracked so _poll_one skips the first-poll early-exit."""
    monitor._tracked[task_id] = MonitoredPr(
        task_id=task_id,
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=None,
        comments_since="2024-06-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_pr_feedback_written_on_first_seen_comment(monkeypatch):
    """A comment fetched for the first time produces exactly one pr_feedback row.

    Metadata must include comment_id, author, url, updated_at, and round.
    role="github", type="pr_feedback".  step_name="".
    """
    from lotsa.github_client import ReviewComment
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    db = _FakeMessageStore(task_metadata={"pr_fix_round_count": 0})

    class FakeOrchestrator:
        pass

    orch = FakeOrchestrator()
    orch.db = db  # type: ignore[attr-defined]

    monitor = PrMonitor(orch, _config())
    _seed_monitor_tracked(monitor)

    comment = ReviewComment(
        id=4242,
        author="claude[bot]",
        body="Found a potential issue in api/foo.py line 12.",
        path=None,
        line=None,
        created_at="2024-07-01T12:00:00Z",
        updated_at="2024-07-01T12:00:00Z",
        html_url="https://github.com/o/r/pull/1#issuecomment-4242",
    )

    client = _make_pr_feedback_client([[comment]])

    async def _fake_client(owner, repo, token):
        return client

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}
    await monitor._poll_one(task, 1)

    pr_feedback_rows = [c for c in db.add_message_calls if c["type"] == "pr_feedback"]
    assert len(pr_feedback_rows) == 1, (
        f"Expected exactly one pr_feedback row, got {len(pr_feedback_rows)}: {pr_feedback_rows!r}"
    )

    row = pr_feedback_rows[0]
    assert row["role"] == "github"
    assert row["step_name"] == ""
    assert row["content"] == "Found a potential issue in api/foo.py line 12."
    assert row["task_id"] == "t1"

    meta = row["metadata"]
    assert meta["comment_id"] == 4242
    assert meta["author"] == "claude[bot]"
    assert meta["url"] == "https://github.com/o/r/pull/1#issuecomment-4242"
    assert meta["updated_at"] == "2024-07-01T12:00:00Z"
    # round = pr_fix_round_count (0) + 1 = 1
    assert meta["round"] == 1

    # The per-comment fingerprint is updated.
    assert monitor._tracked["t1"].last_updated_at_by_comment_id[4242] == "2024-07-01T12:00:00Z"


@pytest.mark.asyncio
async def test_pr_feedback_written_on_edit_in_place(monkeypatch):
    """Same comment_id with newer updated_at produces a second pr_feedback row.

    The claude PR-review bot edits its single comment in-place across
    rounds; without this behaviour the agent would never see the updated
    content.  Two polls, same comment_id, updated_at advances → two rows.
    """
    from lotsa.github_client import ReviewComment
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    db = _FakeMessageStore(task_metadata={"pr_fix_round_count": 0})

    class FakeOrchestrator:
        pass

    orch = FakeOrchestrator()
    orch.db = db  # type: ignore[attr-defined]

    monitor = PrMonitor(orch, _config())
    _seed_monitor_tracked(monitor)

    v1 = ReviewComment(
        id=99,
        author="claude[bot]",
        body="Round 1 findings.",
        path=None,
        line=None,
        created_at="2024-07-01T12:00:00Z",
        updated_at="2024-07-01T12:00:00Z",
        html_url="https://github.com/o/r/pull/1#issuecomment-99",
    )
    v2 = ReviewComment(
        id=99,
        author="claude[bot]",
        body="Round 2 findings (edited).",
        path=None,
        line=None,
        created_at="2024-07-01T12:00:00Z",
        updated_at="2024-07-01T13:30:00Z",
        html_url="https://github.com/o/r/pull/1#issuecomment-99",
    )

    client = _make_pr_feedback_client([[v1], [v2]])

    async def _fake_client(owner, repo, token):
        return client

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}

    await monitor._poll_one(task, 1)
    await monitor._poll_one(task, 1)

    pr_feedback_rows = [c for c in db.add_message_calls if c["type"] == "pr_feedback"]
    assert len(pr_feedback_rows) == 2, (
        f"Expected two pr_feedback rows (one per comment-version), got {len(pr_feedback_rows)}"
    )

    assert pr_feedback_rows[0]["content"] == "Round 1 findings."
    assert pr_feedback_rows[0]["metadata"]["updated_at"] == "2024-07-01T12:00:00Z"

    assert pr_feedback_rows[1]["content"] == "Round 2 findings (edited)."
    assert pr_feedback_rows[1]["metadata"]["updated_at"] == "2024-07-01T13:30:00Z"

    # Both rows share comment_id — append-only audit trail per version.
    assert pr_feedback_rows[0]["metadata"]["comment_id"] == 99
    assert pr_feedback_rows[1]["metadata"]["comment_id"] == 99


@pytest.mark.asyncio
async def test_pr_feedback_not_rewritten_when_updated_at_unchanged(monkeypatch):
    """Two consecutive polls with identical comment payloads → exactly one row.

    The per-comment fingerprint (last_updated_at_by_comment_id) must
    short-circuit the write when updated_at hasn't advanced.  Without
    this the audit trail would grow unbounded on a steady-state PR.
    """
    from lotsa.github_client import ReviewComment
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    db = _FakeMessageStore(task_metadata={"pr_fix_round_count": 0})

    class FakeOrchestrator:
        pass

    orch = FakeOrchestrator()
    orch.db = db  # type: ignore[attr-defined]

    monitor = PrMonitor(orch, _config())
    _seed_monitor_tracked(monitor)

    comment = ReviewComment(
        id=7,
        author="alice",
        body="Looks reasonable to me.",
        path=None,
        line=None,
        created_at="2024-07-01T12:00:00Z",
        updated_at="2024-07-01T12:00:00Z",
        html_url="https://github.com/o/r/pull/1#issuecomment-7",
    )

    # Same comment returned on both calls — updated_at unchanged.
    client = _make_pr_feedback_client([[comment], [comment]])

    async def _fake_client(owner, repo, token):
        return client

    monitor._get_client = _fake_client  # type: ignore[assignment]

    task = {"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}

    await monitor._poll_one(task, 1)
    await monitor._poll_one(task, 1)

    pr_feedback_rows = [c for c in db.add_message_calls if c["type"] == "pr_feedback"]
    assert len(pr_feedback_rows) == 1, (
        f"Expected idempotent write — one row across two unchanged polls, got {len(pr_feedback_rows)}"
    )


@pytest.mark.asyncio
async def test_pr_feedback_round_reflects_pr_fix_round_count(monkeypatch):
    """The ``round`` metadata field is read from pr_fix_round_count + 1.

    Phase 1 reads with a 0 default because Task 5 hasn't plumbed the
    counter yet, but the read path must be present so the audit trail
    back-fills correctly once Task 5 lands.  This test pins both:
        - default (no key set) → round=1
        - explicit value 3 → round=4
    """
    from lotsa.github_client import ReviewComment
    from lotsa.pr_monitor import PrMonitor

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    comment_factory = lambda cid: ReviewComment(  # noqa: E731
        id=cid,
        author="bot",
        body=f"comment {cid}",
        path=None,
        line=None,
        created_at="2024-07-01T12:00:00Z",
        updated_at="2024-07-01T12:00:00Z",
        html_url=f"https://github.com/o/r/pull/1#issuecomment-{cid}",
    )

    # Case A: pr_fix_round_count absent → round=1
    db_a = _FakeMessageStore(task_metadata={})

    class FakeOrchestratorA:
        pass

    orch_a = FakeOrchestratorA()
    orch_a.db = db_a  # type: ignore[attr-defined]

    monitor_a = PrMonitor(orch_a, _config())
    _seed_monitor_tracked(monitor_a)
    client_a = _make_pr_feedback_client([[comment_factory(1)]])

    async def _fc_a(owner, repo, token):
        return client_a

    monitor_a._get_client = _fc_a  # type: ignore[assignment]
    await monitor_a._poll_one({"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}, 1)

    rows_a = [c for c in db_a.add_message_calls if c["type"] == "pr_feedback"]
    assert rows_a and rows_a[0]["metadata"]["round"] == 1

    # Case B: pr_fix_round_count=3 → round=4
    db_b = _FakeMessageStore(task_metadata={"pr_fix_round_count": 3})

    class FakeOrchestratorB:
        pass

    orch_b = FakeOrchestratorB()
    orch_b.db = db_b  # type: ignore[attr-defined]

    monitor_b = PrMonitor(orch_b, _config())
    _seed_monitor_tracked(monitor_b)
    client_b = _make_pr_feedback_client([[comment_factory(2)]])

    async def _fc_b(owner, repo, token):
        return client_b

    monitor_b._get_client = _fc_b  # type: ignore[assignment]
    await monitor_b._poll_one({"id": "t1", "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"}}, 1)

    rows_b = [c for c in db_b.add_message_calls if c["type"] == "pr_feedback"]
    assert rows_b and rows_b[0]["metadata"]["round"] == 4


# ---------------------------------------------------------------------------
# Phase 1 — R2: MonitoredPr fingerprint dict default
# ---------------------------------------------------------------------------


def test_monitored_pr_has_last_updated_at_by_comment_id_default():
    """MonitoredPr must default last_updated_at_by_comment_id to {}.

    The per-comment fingerprint is initialised lazily on the dataclass —
    callers (including the orchestrator's reattach path) construct
    MonitoredPr without supplying this field, so a default_factory is
    required.  Without it, the first ``tracked.last_updated_at_by_comment_id.get(...)``
    in _poll_one raises AttributeError.
    """
    m = MonitoredPr(
        task_id="t",
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=0.0,
        feedback_first_seen_at=None,
    )
    assert m.last_updated_at_by_comment_id == {}


# ---------------------------------------------------------------------------
# Phase 1 — R3: fetch_cutoff renamed to fetch_updated_at_cutoff
# ---------------------------------------------------------------------------


def test_poll_one_uses_fetch_updated_at_cutoff_name():
    """The local variable in _poll_one is renamed for semantic clarity.

    R3 sweeps pr_monitor.py for created_at vs updated_at usage; the
    fetch-time cutoff variable was previously ``fetch_cutoff`` but its
    semantics are 'use as next poll's since= filter' which is an
    updated_at-style cursor on the GitHub side.  The rename pins the
    semantic; the old name must not survive.
    """
    import inspect

    from lotsa import pr_monitor as pm

    src = inspect.getsource(pm.PrMonitor._poll_one)
    assert "fetch_updated_at_cutoff" in src, "expected new name 'fetch_updated_at_cutoff' in _poll_one"
    # Old name must be gone — leaving both around silently breeds drift.
    assert "fetch_cutoff" not in src, "old name 'fetch_cutoff' should be removed in R3"


# ===========================================================================
# ADR-030 — PR-lifetime monitoring (monitor-side behaviour)
#
# These exercise the new branching inside ``PrMonitor`` / its discovery helper:
#   * ``_list_waiting_tasks`` no longer drops non-monitor-state tasks (terminal
#     handling must see every non-terminal PR-bearing task).
#   * a terminal signal on a ``working`` task DEFERS (sets ``terminal_pending``)
#     instead of transitioning mid-dispatch.
#   * ``take_terminal_pending`` is the drainer's consume hook.
#   * the FEEDBACK branch is gated on observed ``status == "waiting_for_pr"``.
#
# Each "red" test fails against pre-fix code for a documented reason recorded
# in its docstring (lotsa/CLAUDE.md regression-test discipline).
# ===========================================================================


class _AdrFakeClient:
    """Configurable GitHub client double for ADR-030 _poll_one tests."""

    def __init__(
        self,
        *,
        merged: bool = False,
        state: str = "open",
        comments: list[ReviewComment] | None = None,
    ) -> None:
        self._pr = PrInfo(
            number=1,
            state=state,
            merged=merged,
            review_decision=None,
            head_sha="sha",
            base_branch="main",
        )
        self._comments = comments or []

    async def get_pr(self, pr_number):
        return self._pr

    async def get_reviews(self, pr_number):
        return []

    async def get_review_comments(self, pr_number, since=None):
        return []

    async def get_issue_comments(self, pr_number, since=None):
        return list(self._comments)

    async def get_check_status(self, sha):
        return CheckStatus(total=0, passing=0, failing=0, pending=0)

    async def close(self):
        pass


def _adr_monitor(orchestrator, *, monitor_state: str | None = "wait_for_pr_signal"):
    """A PrMonitor wired to a fake orchestrator with the GitHub fan-out stubbed."""
    from lotsa.pr_monitor import PrMonitor

    monitor = PrMonitor(orchestrator, _config(), monitor_state=monitor_state)
    return monitor


def _seed_tracked(monitor, task_id, *, feedback_first_seen_at=None):
    """Pre-insert a tracked entry so _poll_one treats the next poll as steady-state."""
    tracked = MonitoredPr(
        task_id=task_id,
        pr_number=1,
        owner="o",
        repo="r",
        last_poll_at=time.time(),
        feedback_first_seen_at=feedback_first_seen_at,
        comments_since="2000-01-01T00:00:00+00:00",
    )
    monitor._tracked[task_id] = tracked
    return tracked


# ── _list_waiting_tasks no longer state-filters (terminal handling) ─────────


@pytest.mark.asyncio
async def test_list_waiting_tasks_returns_non_monitor_state_tasks():
    """The poller must surface PR-bearing tasks parked OUTSIDE the monitor state.

    Pre-fix: ``_list_waiting_tasks`` filters the orchestrator's list down to
    rows whose ``state == self._monitor_state``, so a ``blocked`` task (state
    ``"blocked"`` ≠ ``"wait_for_pr_signal"``) is dropped and never polled —
    exactly the zombie the ADR fixes. Post-fix the scoping moves to the
    feedback gate and the list is returned unfiltered.
    """

    class FakeOrchestrator:
        async def list_waiting_pr_tasks(self):
            return [
                {"id": "waiting", "state": "wait_for_pr_signal", "status": "waiting_for_pr", "metadata": {}},
                {"id": "blocked", "state": "blocked", "status": "blocked", "metadata": {}},
                {"id": "parked", "state": "pr-fixing", "status": "needs_input", "metadata": {}},
            ]

    monitor = _adr_monitor(FakeOrchestrator(), monitor_state="wait_for_pr_signal")
    tasks = await monitor._list_waiting_tasks()
    ids = {t["id"] for t in tasks}
    assert ids == {"waiting", "blocked", "parked"}, (
        "terminal handling needs every non-terminal PR task; the monitor-state "
        "filter must no longer exclude blocked/needs_input tasks"
    )


# ── Terminal signal on a working task DEFERS ────────────────────────────────


@pytest.mark.asyncio
async def test_merged_while_working_defers_instead_of_completing(monkeypatch):
    """A merge observed while the task is ``working`` must NOT transition.

    Pre-fix: ``_poll_one`` ignores status, classifies COMPLETE, pops the
    tracked entry and calls ``transition_task(task_id, "complete")`` — which
    would complete a task whose agent is mid-write. Post-fix it records
    ``terminal_pending="complete"`` on the tracked entry and leaves the
    transition to the drainer (after the agent's own routing).
    """
    from unittest.mock import AsyncMock

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        transition_task = AsyncMock()
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    _seed_tracked(monitor, "t-working")
    monitor._get_client = lambda owner, repo, token: _coro(_AdrFakeClient(merged=True))  # type: ignore[assignment]

    task = {
        "id": "t-working",
        "status": "working",
        "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"},
    }
    await monitor._poll_one(task, 1)

    monitor._orchestrator.transition_task.assert_not_called()
    assert "t-working" in monitor._tracked, "a deferred terminal must keep the tracked entry"
    assert monitor._tracked["t-working"].terminal_pending == "complete"


@pytest.mark.asyncio
async def test_first_poll_merged_while_working_defers(monkeypatch):
    """Deferral also applies on the FIRST poll of a re-discovered working task.

    A task mid-``pr-fix`` is untracked (``_on_feedback`` popped it at dispatch),
    so the widened predicate re-discovers it as a first poll. Pre-fix the
    first-poll terminal branch unconditionally pops + completes; post-fix it
    must defer because the observed status is ``working``.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        transition_task = AsyncMock()
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    monitor._get_client = lambda owner, repo, token: _coro(_AdrFakeClient(merged=True))  # type: ignore[assignment]

    task = {
        "id": "t-fresh-working",
        "status": "working",
        "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"},
    }
    await monitor._poll_one(task, 1)

    monitor._orchestrator.transition_task.assert_not_called()
    assert monitor._tracked["t-fresh-working"].terminal_pending == "complete"


@pytest.mark.asyncio
async def test_take_terminal_pending_returns_and_clears():
    """``take_terminal_pending`` hands the deferred target to the drainer once.

    Pre-fix: the method does not exist (``AttributeError``). Post-fix it
    returns the pending target and drops the tracked entry so a second call
    returns ``None``.
    """

    class FakeOrchestrator:
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    tracked = _seed_tracked(monitor, "t1")
    # Set via attribute (not constructor) so this test isolates the consume
    # hook from the dataclass-field change covered elsewhere.
    tracked.terminal_pending = "complete"

    assert monitor.take_terminal_pending("t1") == "complete"
    assert "t1" not in monitor._tracked, "consuming a deferred terminal must drop the tracked entry"
    assert monitor.take_terminal_pending("t1") is None
    assert monitor.take_terminal_pending("never-tracked") is None


# ── Terminal signal on a non-working parked task acts immediately ───────────


@pytest.mark.asyncio
async def test_closed_while_needs_input_abandons_immediately(monkeypatch):
    """A close observed while ``needs_input`` (agent NOT in flight) abandons now.

    Only ``working`` defers; every other parked status acts immediately. This
    guards against over-broadening the deferral.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        transition_task = AsyncMock()
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    _seed_tracked(monitor, "t-ni")
    monitor._get_client = lambda owner, repo, token: _coro(  # type: ignore[assignment]
        _AdrFakeClient(state="closed", merged=False)
    )

    task = {
        "id": "t-ni",
        "status": "needs_input",
        "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"},
    }
    await monitor._poll_one(task, 1)

    monitor._orchestrator.transition_task.assert_called_once_with("t-ni", "abandoned")
    assert "t-ni" not in monitor._tracked


# ── FEEDBACK is gated on observed status == waiting_for_pr ───────────────────


@pytest.mark.asyncio
async def test_feedback_while_blocked_does_not_dispatch(monkeypatch):
    """Feedback on a ``blocked`` task must NOT dispatch pr-fix.

    Pre-fix: with discovery widened, ``_poll_one`` would classify FEEDBACK and
    (past the debounce window) call ``dispatch_pr_fix`` into a task the
    operator deliberately parked. Post-fix the FEEDBACK branch is gated on
    observed ``status == "waiting_for_pr"`` and does nothing here.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        dispatch_pr_fix = AsyncMock(return_value=True)
        transition_task = AsyncMock()
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    # Debounce already elapsed so the only thing standing between the signal
    # and dispatch is the status gate.
    _seed_tracked(monitor, "t-blocked", feedback_first_seen_at=time.time() - 10_000)
    monitor._get_client = lambda owner, repo, token: _coro(  # type: ignore[assignment]
        _AdrFakeClient(comments=[_comment(author="alice", body="please change X")])
    )

    task = {
        "id": "t-blocked",
        "status": "blocked",
        "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"},
    }
    await monitor._poll_one(task, 1)

    monitor._orchestrator.dispatch_pr_fix.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_while_waiting_for_pr_still_dispatches(monkeypatch):
    """Companion guard: the status gate must not suppress legitimate feedback.

    A ``waiting_for_pr`` task with debounced feedback still dispatches pr-fix.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class FakeOrchestrator:
        dispatch_pr_fix = AsyncMock(return_value=True)
        transition_task = AsyncMock()
        db = None

    monitor = _adr_monitor(FakeOrchestrator())
    _seed_tracked(monitor, "t-wfp", feedback_first_seen_at=time.time() - 10_000)
    monitor._get_client = lambda owner, repo, token: _coro(  # type: ignore[assignment]
        _AdrFakeClient(comments=[_comment(author="alice", body="please change X")])
    )

    task = {
        "id": "t-wfp",
        "status": "waiting_for_pr",
        "metadata": {"pr_number": 1, "github_owner": "o", "github_repo": "r"},
    }
    await monitor._poll_one(task, 1)

    monitor._orchestrator.dispatch_pr_fix.assert_called_once()


def _coro(value):
    """Wrap a ready value in a coroutine for monkeypatched async factories."""

    async def _inner(*args, **kwargs):
        return value

    return _inner()
