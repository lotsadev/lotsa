"""PR monitor — polls GitHub for signal changes on open pull requests.

Classifies signals (COMPLETE, ABANDONED, FEEDBACK, NONE), applies debounce
before dispatching feedback, and aggregates review comments into structured
payloads for the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol

import httpx

from lotsa.github_client import CheckStatus, GitHubClient, PrInfo, ReviewComment, compute_review_decision

if TYPE_CHECKING:
    from lotsa.db import TaskDB
    from lotsa.engines.pr_monitor import PrMonitorConfig as PrConfig
else:
    # Structural type for runtime — PrMonitor only reads attributes
    # (triggers, poll_interval_seconds, debounce_seconds, max_*).
    PrConfig = object  # type: ignore[misc, assignment]


class PrMonitorOrchestrator(Protocol):
    """Subset of OrchestratorService that PrMonitor depends on.

    Typing the constructor parameter against this Protocol lets the type
    checker catch renames/removals on OrchestratorService — without it,
    ``getattr(orch, "transition_task", None)`` silently turns the monitor
    into a no-op when the method goes away.

    The runtime ``getattr`` lookups in PrMonitor are kept as defensive
    no-ops for tests that pass partial fakes; static checking via this
    Protocol ensures real OrchestratorService still satisfies the contract.

    ``dispatch_sub_flow`` is the ADR-014 Layer A entry point that the
    ``pr_monitor`` engine (``lotsa.engines.pr_monitor``) uses directly when
    a future Layer B wiring instantiates it from ``OrchestratorService.start()``.
    Today the legacy ``PrMonitor`` class still calls ``dispatch_pr_fix`` — the
    ``_SubFlowAdapter`` in ``lotsa.engines.pr_monitor`` translates that to
    ``dispatch_sub_flow`` at runtime — so both methods are declared here. The
    ``dispatch_pr_fix`` entry will retire once ``PrMonitor`` migrates to call
    ``dispatch_sub_flow`` directly.
    """

    db: TaskDB

    async def transition_task(self, task_id: str, target_state: str) -> None: ...
    async def dispatch_pr_fix(self, task_id: str, feedback: str) -> bool: ...
    async def dispatch_sub_flow(
        self,
        task_id: str,
        flow_name: str,
        *,
        feedback: str | None = None,
        target_job: str | None = None,
    ) -> bool: ...
    async def list_waiting_pr_tasks(self) -> list[dict]: ...


logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Cap concurrent PR polls so a backlog of waiting_for_pr tasks can't open
# hundreds of simultaneous httpx requests against the GitHub API.
_MAX_POLL_CONCURRENCY = 8


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp, handling both 'Z' and '+00:00' suffixes."""
    if not ts:
        return _EPOCH
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Signal enum
# ---------------------------------------------------------------------------


class PrSignal(Enum):
    """Possible outcomes of a single PR poll cycle."""

    NONE = "none"
    COMPLETE = "complete"
    ABANDONED = "abandoned"
    FEEDBACK = "feedback"


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------


def classify_signals(
    pr: PrInfo,
    reviews: list[dict],
    comments: list[ReviewComment],
    checks: CheckStatus,
    config: PrConfig,
    push_timestamp: str | None = None,
    conflict_dispatched_sha: str | None = None,
) -> PrSignal:
    """Classify the current state of a PR into a single PrSignal.

    Evaluation order:
    1. COMPLETE — PR merged.
    2. ABANDONED — PR closed without merge.
    3. FEEDBACK — one or more configured triggers fires.
    4. NONE — no actionable change.

    APPROVED reviews are *not* a completion signal — bots and reviewers can
    mark a PR APPROVED while leaving actionable feedback inline, so treating
    APPROVED as "done" risks short-circuiting real work.  The PR is only
    considered complete once the merge actually happens.

    Args:
        pr: Current pull-request metadata.
        reviews: Raw review objects from the GitHub API.
        comments: Combined review + issue comments.
        checks: Aggregated check-run status for the head SHA.
        config: PR phase configuration (trigger list, debounce, etc.).
        push_timestamp: ISO-8601 cutoff timestamp (set at feedback dispatch
            time); reviews before this are filtered out to prevent stale
            feedback loops. Note: this is the dispatch time, not the actual
            push time — reviews during the fix cycle may be filtered.

    Returns:
        The most significant :class:`PrSignal` for this poll cycle.
    """
    # ── COMPLETE / ABANDONED — terminal signals from PR state alone ───────

    if pr.merged:
        return PrSignal.COMPLETE

    if pr.state == "closed" and not pr.merged:
        return PrSignal.ABANDONED

    # ── FEEDBACK triggers ─────────────────────────────────────────────────

    triggers = set(config.triggers)

    if "review_decision" in triggers:
        # Filter reviews to only those submitted after the last push timestamp.
        # This prevents stale CHANGES_REQUESTED reviews from before a fix push
        # from causing an infinite feedback loop, while still catching new
        # reviews submitted after the push (even without a SHA change).
        if push_timestamp:
            cutoff = _parse_ts(push_timestamp)
            recent_reviews = [r for r in reviews if _parse_ts(r.get("submitted_at", "")) > cutoff]
        else:
            recent_reviews = reviews

        # PR-level review_decision: only act if there are recent CHANGES_REQUESTED
        # reviews (or no push_timestamp filter is active). Using any-review semantics
        # would cause spurious feedback when a new APPROVED review arrives on a PR
        # that still has a stale pre-push CHANGES_REQUESTED from another reviewer.
        has_recent = not push_timestamp or any(r.get("state") == "CHANGES_REQUESTED" for r in recent_reviews)
        if pr.review_decision == "CHANGES_REQUESTED" and has_recent:
            return PrSignal.FEEDBACK

    if "human_comment" in triggers:
        for comment in comments:
            if not comment.author.endswith("[bot]"):
                return PrSignal.FEEDBACK
        # Also detect review bodies from non-bot reviewers (COMMENTED or
        # APPROVED reviews with substantive body text).  These don't appear
        # in the comments endpoints — only in the reviews endpoint.
        if push_timestamp:
            cutoff = _parse_ts(push_timestamp)
            body_reviews = [r for r in reviews if _parse_ts(r.get("submitted_at", "")) > cutoff]
        else:
            body_reviews = reviews
        for review in body_reviews:
            body = (review.get("body") or "").strip()
            author = (review.get("user") or {}).get("login", "")
            if body and author and not author.endswith("[bot]"):
                return PrSignal.FEEDBACK

    if "bot_comment" in triggers:
        for comment in comments:
            if comment.author.endswith("[bot]"):
                return PrSignal.FEEDBACK

    if "failing_check" in triggers and checks.failing > 0:
        return PrSignal.FEEDBACK

    if "merge_conflict" in triggers and pr.mergeable is False:
        # Debounce on head_sha: fire FEEDBACK once per distinct conflicting
        # commit, not every poll. Unlike the comment/review triggers (which
        # debounce via ``push_timestamp`` / ``comments_since``), a merge
        # conflict is timestamp-less — ``pr.mergeable is False`` is true on
        # every poll until the branch changes. Without this guard a CONFLICTING
        # PR re-dispatches pr-fix every poll; pr-fix is comment-driven, finds no
        # new feedback, benign-skips, never pushes — so the conflict persists,
        # the loop burns the round cap, and the task false-blocks as "budget
        # exhausted" (internal tasks / 04ee0735, PR #143 / #144). Re-attempt
        # only when a new commit (different ``head_sha``) could plausibly change
        # the outcome.
        if pr.head_sha and pr.head_sha == conflict_dispatched_sha:
            return PrSignal.NONE
        return PrSignal.FEEDBACK

    return PrSignal.NONE


# ---------------------------------------------------------------------------
# Tracked PR state
# ---------------------------------------------------------------------------


@dataclass
class MonitoredPr:
    """Per-PR monitoring state tracked by :class:`PrMonitor`."""

    task_id: str
    pr_number: int
    owner: str
    repo: str
    last_poll_at: float
    feedback_first_seen_at: float | None
    comments_since: str | None = None  # ISO-8601 timestamp for filtering old comments
    # Head SHA we last dispatched a merge_conflict FEEDBACK for. Debounces the
    # ``merge_conflict`` trigger so a persistently-CONFLICTING PR fires once per
    # conflicting commit instead of every poll. Persisted to task metadata
    # (``pr_conflict_dispatched_sha``) because ``_on_feedback`` pops the tracked
    # entry on every dispatch, so in-memory-only state would reset each cycle.
    conflict_dispatched_sha: str | None = None
    consecutive_failures: int = 0  # for exponential backoff on API errors
    # ADR-030: ``"complete"`` / ``"abandoned"`` set when a terminal PR signal
    # (merge / close / 404) lands while the task is ``working`` — an agent is
    # in flight, so completing now would discard work mid-write. The
    # orchestrator's drainer consumes this via ``take_terminal_pending`` AFTER
    # the agent's own completion routing has run (happens-before), then applies
    # the transition. In-memory only: on restart the recovery sweep flips
    # ``working``→``blocked`` and the widened discovery predicate re-polls and
    # completes the task anyway, so durability isn't required for correctness.
    terminal_pending: str | None = None
    # ``comment_id`` → last-seen ``updated_at``. Drives the ``pr_feedback``
    # audit-write path: a comment whose id is absent or whose stored
    # ``updated_at`` is older than the latest fetched value produces a new
    # audit row. In-memory only; resets on process restart (the GitHub
    # ``since=`` filter bounds the duplicate window after restart).
    last_updated_at_by_comment_id: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Feedback aggregation
# ---------------------------------------------------------------------------


def aggregate_feedback(
    reviews: list[dict],
    comments: list[ReviewComment],
    checks: CheckStatus,
) -> str:
    """Build a structured feedback payload from all available signals.

    Sections included (only when non-empty):
    - Review Decision (CHANGES_REQUESTED summary with review bodies)
    - Inline Comments (comments with a file path)
    - PR Comments (comments without a file path)
    - Failing Checks (names list)

    Sections are joined with ``---`` dividers.

    Returns:
        A multi-section Markdown string, or ``"No specific feedback found."``
        if no content is available.
    """
    sections: list[str] = []

    # ── Review Decision ───────────────────────────────────────────────────
    changes_reviews = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    if changes_reviews:
        lines = ["### Review Decision\n"]
        lines.append("Status: **CHANGES_REQUESTED**\n")
        for review in changes_reviews:
            author = (review.get("user") or {}).get("login", "unknown")
            body = (review.get("body") or "").strip()
            if body:
                lines.append(f"**{author}:** {body}")
            else:
                lines.append(f"**{author}** requested changes (no comment).")
        sections.append("\n".join(lines))

    # ── Review Comments (body text from COMMENTED/APPROVED reviews) ───────
    # Reviewers may submit substantive feedback as a COMMENTED or APPROVED
    # review body.  These do not appear in the comments endpoints.
    other_reviews = [
        r
        for r in reviews
        if r.get("state") not in ("CHANGES_REQUESTED", "DISMISSED", "PENDING") and (r.get("body") or "").strip()
    ]
    if other_reviews:
        lines = ["### Review Body Comments\n"]
        for review in other_reviews:
            author = (review.get("user") or {}).get("login", "unknown")
            state = review.get("state", "UNKNOWN")
            body = (review.get("body") or "").strip()
            lines.append(f"**{author}** ({state}): {body}")
        sections.append("\n".join(lines))

    # ── Inline Comments (diff-level) ──────────────────────────────────────
    inline = [c for c in comments if c.path is not None]
    if inline:
        lines = ["### Inline Comments\n"]
        for c in inline:
            lines.append(f"**{c.author}** on `{c.path}`" + (f" line {c.line}" if c.line else "") + f":\n> {c.body}")
        sections.append("\n".join(lines))

    # ── PR Comments (issue-level) ─────────────────────────────────────────
    general = [c for c in comments if c.path is None]
    if general:
        lines = ["### PR Comments\n"]
        for c in general:
            lines.append(f"**{c.author}:** {c.body}")
        sections.append("\n".join(lines))

    # ── Failing Checks ────────────────────────────────────────────────────
    if checks.failing > 0:
        lines = ["### Failing Checks\n"]
        for name in checks.failing_names:
            lines.append(f"- {name}")
        sections.append("\n".join(lines))

    if not sections:
        return "No specific feedback found."

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# pr_feedback audit-write helper
# ---------------------------------------------------------------------------


async def _record_pr_feedback(
    db: TaskDB,
    task_id: str,
    task_metadata: dict,
    comment: ReviewComment,
) -> None:
    """Append a ``pr_feedback`` row capturing this version of *comment*.

    Writes one message per comment-version. An edited comment produces a
    new row each time its ``updated_at`` advances; an unchanged comment is
    a no-op (handled at the call site by the ``last_updated_at_by_comment_id``
    fingerprint). The message body is the full markdown so the audit trail
    can reconstruct what the agent saw at a given ``pr_fix_round_count``.

    Phase 1 reads ``pr_fix_round_count`` with a ``0`` default — Task 5
    plumbs the counter. ``round`` in the metadata is therefore
    ``pr_fix_round_count + 1`` so the audit trail starts at 1 and
    back-fills correctly once Task 5 lands.
    """
    round_n = int(task_metadata.get("pr_fix_round_count", 0)) + 1
    await db.add_message(
        task_id,
        role="github",
        step_name="",
        content=comment.body,
        msg_type="pr_feedback",
        metadata={
            "comment_id": comment.id,
            "author": comment.author,
            "url": comment.html_url,
            "updated_at": comment.updated_at,
            "round": round_n,
        },
    )


# ---------------------------------------------------------------------------
# PrMonitor
# ---------------------------------------------------------------------------


class PrMonitor:
    """Background service that polls GitHub for PR signal changes.

    Lifecycle::

        monitor = PrMonitor(orchestrator, config)
        monitor.run()  # starts the polling loop (cancel to stop)

    The monitor polls every non-terminal task that has opened a PR (ADR-030:
    *open PR ⇒ watched until terminal*, regardless of the task's local status),
    fetching GitHub state for each. When a signal fires:

    - COMPLETE / ABANDONED — terminal; acts from any status. While the task is
      ``working`` the transition is DEFERRED (recorded on
      ``MonitoredPr.terminal_pending`` and applied by the orchestrator's drainer
      once the in-flight agent's routing has run) so an agent is never completed
      mid-write.
    - FEEDBACK — gated to observed ``status == "waiting_for_pr"`` (pr-fix must
      not be dispatched into a working/blocked/needs_input task); applies
      debounce, then delegates to the orchestrator.
    - NONE — no action.
    """

    def __init__(
        self,
        orchestrator: PrMonitorOrchestrator,
        config: PrConfig,
        monitor_state: str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._config = config
        # ADR-014 Layer A: the monitor-job state this poller owns (e.g.
        # ``wait_for_pr_signal``). ``None`` on the legacy direct-instantiation
        # path. Recorded for identity/introspection; ADR-030 moved the actual
        # feedback-dispatch scoping to the ``observed_status == "waiting_for_pr"``
        # gate in ``_poll_one`` (``_list_waiting_tasks`` no longer filters on
        # this so terminal handling can see tasks parked in any state).
        self._monitor_state = monitor_state
        self._tracked: dict[str, MonitoredPr] = {}  # task_id → MonitoredPr
        # Cache GitHubClient instances per (owner, repo) so polling reuses
        # connection pools across cycles.  The current token is stored
        # alongside; on token rotation the old client is closed before the
        # replacement is created so its httpx pool is released immediately.
        self._clients: dict[tuple[str, str], tuple[GitHubClient, str]] = {}

    def untrack(self, task_id: str) -> None:
        """Drop a task's in-memory tracking entry.

        Called by orchestrator paths that transition a task out of
        ``waiting_for_pr`` without going through one of the monitor's own
        cleanup callbacks (``block()``, ``jump_to_step()``).  Without this,
        the entry would linger in ``_tracked`` until process restart.
        """
        self._tracked.pop(task_id, None)

    def take_terminal_pending(self, task_id: str) -> str | None:
        """Return and clear a deferred terminal target for *task_id* (ADR-030).

        Called by the orchestrator's dispatch-completion path (the drainer)
        after the in-flight agent's own routing has run. When a terminal PR
        signal landed on a ``working`` task, ``_handle_terminal`` recorded
        ``"complete"`` / ``"abandoned"`` on the tracked entry instead of
        transitioning mid-dispatch; this hands that target back and drops the
        tracked entry (mirroring the untrack on a non-deferred terminal) so a
        second call returns ``None``. Returns ``None`` when nothing is pending
        or the task isn't tracked.
        """
        tracked = self._tracked.get(task_id)
        if tracked is None or tracked.terminal_pending is None:
            return None
        self._tracked.pop(task_id, None)
        return tracked.terminal_pending

    def snapshot_triggering_ids(self, task_id: str) -> list[int]:
        """Return the IDs of the comments the monitor has fingerprinted.

        Used by ``OrchestratorService._dispatch_pr_fix_locked`` to record
        which PR comments the agent is being asked to respond to in this
        round. The IDs land on the ``pr_decision`` audit row so an operator
        can cross-reference the decision with the ``pr_feedback`` rows it
        responded to. Returns an empty list when the task isn't tracked
        (e.g. revise-driven dispatch from a state the monitor doesn't
        manage).
        """
        tracked = self._tracked.get(task_id)
        if tracked is None:
            return []
        return list(tracked.last_updated_at_by_comment_id.keys())

    async def run(self) -> None:
        """Async polling loop.  Runs until cancelled."""
        try:
            while True:
                # Guard the body so a raise from _poll_all degrades to a logged,
                # skipped cycle instead of permanently killing PR monitoring. The
                # inner calls self-isolate today, but that is incidental, not
                # structural (finding #8). CancelledError must still propagate.
                try:
                    await self._poll_all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("PrMonitor: poll cycle failed; continuing")
                await asyncio.sleep(self._config.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.debug("PrMonitor cancelled")
            raise
        finally:
            # Close any cached clients on shutdown.  Cancellation propagates
            # after this; we still want pooled connections released.
            for client, _token in self._clients.values():
                try:
                    await client.close()
                except Exception:
                    logger.exception("PrMonitor: error closing cached GitHub client")
            self._clients.clear()

    async def _get_client(self, owner: str, repo: str, token: str) -> GitHubClient:
        """Return a cached GitHubClient, creating one on first use.

        If the cached token for ``(owner, repo)`` differs from *token* (e.g.
        the user rotated ``GITHUB_TOKEN`` mid-run), the old client is
        ``aclose()``'d before the replacement is created so the httpx pool
        is released immediately rather than waiting for shutdown.
        """
        key = (owner, repo)
        cached = self._clients.get(key)
        if cached is not None:
            client, cached_token = cached
            if cached_token == token:
                return client
            try:
                await client.close()
            except Exception:
                logger.exception("PrMonitor: error closing rotated GitHub client")
        client = GitHubClient(token=token, owner=owner, repo=repo)
        self._clients[key] = (client, token)
        return client

    async def _poll_all(self) -> None:
        """Poll every task currently waiting for a PR signal.

        Polls run concurrently across tasks (up to ``_MAX_POLL_CONCURRENCY``)
        because each task hits a different ``(owner, repo, pr)`` and benefits
        from independent latency budgets.  Per-task errors are isolated by
        ``_poll_one_with_backoff`` so one failing PR can't starve the rest.
        """
        tasks = await self._list_waiting_tasks()
        coros = []
        for task in tasks:
            pr_number = self._extract_pr_number(task)
            if pr_number is None:
                continue
            task_id = task.get("id", "")
            tracked = self._tracked.get(task_id)
            # Exponential backoff: skip polls if consecutive failures occurred
            if tracked and tracked.consecutive_failures > 0:
                backoff_multiplier = min(2**tracked.consecutive_failures, 10)
                effective_interval = self._config.poll_interval_seconds * backoff_multiplier
                if time.time() - tracked.last_poll_at < effective_interval:
                    continue
            coros.append(self._poll_one_with_backoff(task, pr_number, task_id))
        if not coros:
            return
        sem = asyncio.Semaphore(_MAX_POLL_CONCURRENCY)

        async def _bounded(coro):
            async with sem:
                await coro

        await asyncio.gather(*(_bounded(c) for c in coros), return_exceptions=False)

    async def _poll_one_with_backoff(self, task: dict, pr_number: int, task_id: str) -> None:
        """Poll one task and update consecutive_failures based on the outcome."""
        try:
            await self._poll_one(task, pr_number)
            tracked = self._tracked.get(task_id)
            if tracked:
                tracked.consecutive_failures = 0
        except Exception:
            logger.exception("PrMonitor: error polling task %s PR #%d", task_id, pr_number)
            tracked = self._tracked.get(task_id)
            if tracked:
                tracked.consecutive_failures += 1

    async def _poll_one(self, task: dict, pr_number: int) -> None:
        """Fetch current PR state and classify the signal.

        Args:
            task: Raw task dict with ``id`` and ``metadata`` keys.
            pr_number: The GitHub PR number to poll.
        """
        task_id: str = task["id"]
        metadata: dict = task.get("metadata", {})
        # ADR-030: the orchestrator's widened discovery now surfaces tasks in
        # any non-terminal status. Default to ``waiting_for_pr`` for callers
        # (and legacy tests) that don't supply one — pre-ADR-030 every polled
        # task WAS ``waiting_for_pr``, so this preserves their behaviour.
        observed_status: str = task.get("status", "waiting_for_pr")

        owner: str | None = metadata.get("github_owner")
        repo: str | None = metadata.get("github_repo")
        if not owner or not repo:
            logger.warning("PrMonitor: task %s missing github_owner/github_repo metadata", task_id)
            return

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.warning("PrMonitor: GITHUB_TOKEN not set, blocking task %s", task_id)
            transition_fn = getattr(self._orchestrator, "transition_task", None)
            if transition_fn is not None:
                await transition_fn(task_id, "blocked")
            return

        # First poll: establish baseline timestamp, skip signal classification
        tracked = self._tracked.get(task_id)
        is_first_poll = tracked is None
        if is_first_poll:
            now_ts = time.time()
            # Restore comments_since from task metadata if available.
            # Priority order:
            #   1. pr_comments_since — set after successful pr-fix dispatch,
            #      so subsequent fix cycles only see post-fix feedback.
            #   2. pr_pushed_at — set by the push step itself, so the very
            #      first poll cycle covers the window from push → first poll.
            #   3. now() — only when neither is present (e.g. legacy tasks).
            saved_since = (
                metadata.get("pr_comments_since") or metadata.get("pr_pushed_at") or datetime.now(UTC).isoformat()
            )
            tracked = MonitoredPr(
                task_id=task_id,
                pr_number=pr_number,
                owner=owner,
                repo=repo,
                last_poll_at=now_ts,
                feedback_first_seen_at=None,
                comments_since=saved_since,
                # Restore the merge_conflict debounce cursor — _on_feedback pops
                # the tracked entry on dispatch, so this survives only via metadata.
                conflict_dispatched_sha=metadata.get("pr_conflict_dispatched_sha"),
            )
            self._tracked[task_id] = tracked

        # Update poll timestamp before API calls so backoff windows
        # are measured from the attempt time, not the last success.
        tracked.last_poll_at = time.time()

        client = await self._get_client(owner, repo, token)
        # Capture the cutoff *before* the API fan-out.  When this poll
        # decides to dispatch feedback, this value is what we'll persist as
        # the next ``pr_comments_since`` — guaranteeing a comment whose
        # ``updated_at`` lands between fetch and write isn't silently
        # excluded from the next poll's ``since=`` filter.  Named to make
        # the semantic explicit: this is an updated_at-style cursor (the
        # GitHub ``since=`` filter is updated_at on comments), not a
        # created_at filter.
        fetch_updated_at_cutoff = datetime.now(UTC).isoformat()
        try:
            pr = await client.get_pr(pr_number)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # PR was deleted on GitHub — treat as ABANDONED so the task
                # doesn't loop forever on a missing PR.
                logger.info("PrMonitor: task %s PR #%d returned 404 — abandoning", task_id, pr_number)
                synthetic = PrInfo(
                    number=pr_number,
                    state="closed",
                    merged=False,
                    review_decision=None,
                    head_sha="",
                    base_branch="",
                )
                # ADR-030: a 404 is terminal too — defer if the agent is in
                # flight, else abandon immediately. ``_handle_terminal`` owns
                # the tracked-entry pop for the non-working path.
                await self._handle_terminal(task_id, "abandoned", synthetic, observed_status)
                return
            raise
        # Fan out the remaining four reads in parallel — they're independent
        # and dominate the per-poll latency budget.
        since = tracked.comments_since
        reviews, review_comments, issue_comments, checks = await asyncio.gather(
            client.get_reviews(pr_number),
            client.get_review_comments(pr_number, since=since),
            client.get_issue_comments(pr_number, since=since),
            client.get_check_status(pr.head_sha),
        )
        # The REST API does not return review_decision on the PR
        # object — compute it from the individual reviews.
        pr.review_decision = compute_review_decision(reviews)
        comments = review_comments + issue_comments

        # Persist PR status to task metadata for dashboard display.  Use
        # merge-style spread on the write (rather than mutating
        # ``task_row.metadata`` and writing it back wholesale) so the intent
        # is unambiguous: only the dashboard fields are being updated, every
        # other key the snapshot holds passes through unchanged.  A concurrent
        # writer (e.g. ``revise()`` finishing its GitHub round-trip and
        # writing ``pr_comments_since``) can still race the read-write window,
        # but spreading at write time documents the merge intent clearly.
        db = getattr(self._orchestrator, "db", None)
        if db:
            task_row = await db.get_task(task_id)
            if task_row:
                dashboard_fields = {
                    "pr_review_decision": pr.review_decision,
                    "pr_checks_passing": checks.passing,
                    "pr_checks_total": checks.total,
                    "pr_checks_failing": checks.failing,
                    "pr_last_polled": datetime.now(UTC).isoformat(),
                    # Count unaddressed comments (those after the last push)
                    "pr_feedback_count": len(comments),
                }
                await db.update_task(task_id, metadata={**task_row.metadata, **dashboard_fields})

            # R2: persist each captured comment-version as a pr_feedback
            # message. A first-seen comment_id and a comment whose
            # updated_at advanced both produce a new row; an unchanged
            # comment is a no-op. Re-read task metadata after the
            # dashboard write so the audit row's ``round`` field reflects
            # the latest ``pr_fix_round_count``.  Gated on ``comments`` so
            # an empty poll (no review or issue comments) doesn't pay for
            # a second ``get_task`` round-trip that would only feed an
            # empty loop.
            if comments:
                fresh = await db.get_task(task_id)
                fresh_meta = fresh.metadata if fresh else {}
                for comment in comments:
                    seen_at = tracked.last_updated_at_by_comment_id.get(comment.id)
                    if seen_at is None or comment.updated_at > seen_at:
                        await _record_pr_feedback(db, task_id, fresh_meta, comment)
                        tracked.last_updated_at_by_comment_id[comment.id] = comment.updated_at

        if is_first_poll:
            # First poll — only check completion/abandoned, skip feedback triggers.
            # Mirror the terminal-state checks from classify_signals: merged
            # → COMPLETE, closed-without-merge → ABANDONED.  APPROVED is never
            # a completion signal (see classify_signals docstring).
            #
            # ADR-030: route through ``_handle_terminal`` so a merge/close
            # observed while the task is ``working`` (e.g. a task mid-pr-fix
            # re-discovered untracked, hence a "first poll") DEFERS instead of
            # completing mid-dispatch. The tracked entry was created above, so
            # the deferral has somewhere to record ``terminal_pending``.
            if pr.merged:
                await self._handle_terminal(task_id, "complete", pr, observed_status)
            elif pr.state == "closed" and not pr.merged:
                await self._handle_terminal(task_id, "abandoned", pr, observed_status)
            return

        signal = classify_signals(
            pr,
            reviews,
            comments,
            checks,
            self._config,
            push_timestamp=tracked.comments_since,
            conflict_dispatched_sha=tracked.conflict_dispatched_sha,
        )
        now = time.time()

        if signal == PrSignal.COMPLETE:
            # ADR-030: terminal acts from any status (defers only for working).
            await self._handle_terminal(task_id, "complete", pr, observed_status)
        elif signal == PrSignal.ABANDONED:
            await self._handle_terminal(task_id, "abandoned", pr, observed_status)
        elif signal == PrSignal.FEEDBACK and observed_status == "waiting_for_pr":
            # ADR-030: feedback dispatch stays gated to ``waiting_for_pr``.
            # Dispatching pr-fix into a working/blocked/needs_input task would
            # race the in-flight agent or bypass the operator's attention — so
            # a task parked outside the monitor state is watched for terminal
            # signals only, never fed pr-fix. (Terminal handling above is NOT
            # gated; the PR's fate is global truth.)
            # Debounce: record first sighting, dispatch only after window
            if tracked.feedback_first_seen_at is None:
                tracked.feedback_first_seen_at = now
                logger.debug("PrMonitor: feedback first seen for task %s, starting debounce", task_id)
            elif (now - tracked.feedback_first_seen_at) >= self._config.debounce_seconds:
                # Filter reviews to those after the last push to avoid
                # feeding the agent feedback it already addressed.
                if tracked.comments_since:
                    cutoff_dt = datetime.fromisoformat(tracked.comments_since.replace("Z", "+00:00"))
                    recent_reviews = [r for r in reviews if _parse_ts(r.get("submitted_at", "")) > cutoff_dt]
                else:
                    recent_reviews = reviews
                feedback_text = aggregate_feedback(recent_reviews, comments, checks)
                await self._on_feedback(task_id, pr, feedback_text, since_cutoff=fetch_updated_at_cutoff)
        elif signal == PrSignal.NONE and tracked.feedback_first_seen_at is not None:
            # NONE — feedback isn't currently present.  Clear the debounce
            # timer so a later FEEDBACK signal starts a fresh window rather
            # than dispatching immediately on a timer that ticked through
            # an empty interval (e.g. comment posted then deleted, or a
            # transient failing check that recovered).
            #
            # ADR-030: a FEEDBACK signal on a task NOT in ``waiting_for_pr``
            # falls through with no branch — we neither dispatch nor touch the
            # debounce timer. When the task returns to ``waiting_for_pr`` the
            # debounce window resumes where it left off (a NONE signal while
            # parked would have reset it; persistent feedback throughout the
            # parked period carries the timer over, so dispatch can fire on the
            # first post-unblock poll).
            tracked.feedback_first_seen_at = None

    # ── Terminal handling (ADR-030) ─────────────────────────────────────

    async def _handle_terminal(
        self,
        task_id: str,
        target: str,
        pr: PrInfo,
        observed_status: str,
    ) -> None:
        """Act on a terminal PR signal (``"complete"`` / ``"abandoned"``).

        A merged/closed PR is global truth: it completes/abandons the task
        regardless of the task's local state. The one exception is a
        ``working`` task — an agent is in flight, so we DEFER (record
        ``terminal_pending`` on the tracked entry) and let the orchestrator's
        drainer apply the transition once the agent's own routing has run.
        Cancelling a running agent on merge would discard work mid-write.

        Called from all three terminal sites in ``_poll_one`` (first poll,
        steady state, 404) so the working-deferral rule stays symmetric.
        """
        tracked = self._tracked.get(task_id)
        if observed_status == "working":
            # Defer: keep the tracked entry, record the pending target. On the
            # first poll of a re-discovered working task ``_poll_one`` has
            # already created the tracked entry before reaching here.
            if tracked is not None:
                tracked.terminal_pending = target
            return
        # Non-working: act immediately. Drop the tracked entry first (mirrors
        # the pre-ADR-030 pop) so a later re-entry is treated as a fresh poll.
        self._tracked.pop(task_id, None)
        if target == "complete":
            await self._on_complete(task_id, pr)
        else:
            await self._on_abandoned(task_id, pr)

    # ── Orchestrator callbacks ──────────────────────────────────────────

    async def _on_complete(self, task_id: str, pr: PrInfo) -> None:
        """Called when the PR reaches the completion criterion."""
        logger.info("PrMonitor: task %s PR #%d complete (merged=%s)", task_id, pr.number, pr.merged)
        transition_fn = getattr(self._orchestrator, "transition_task", None)
        if transition_fn is not None:
            await transition_fn(task_id, "complete")

    async def _on_abandoned(self, task_id: str, pr: PrInfo) -> None:
        """Called when the PR is closed without merging."""
        logger.info("PrMonitor: task %s PR #%d abandoned", task_id, pr.number)
        transition_fn = getattr(self._orchestrator, "transition_task", None)
        if transition_fn is not None:
            await transition_fn(task_id, "abandoned")

    async def _on_feedback(self, task_id: str, pr: PrInfo, feedback: str, since_cutoff: str | None = None) -> None:
        """Called when debounced feedback is ready to dispatch.

        ``since_cutoff`` is the ISO-8601 timestamp captured *before* the
        GitHub fan-out for this poll cycle.  Persisting that (rather than a
        fresh ``now()``) as the next ``pr_comments_since`` means a comment
        whose ``updated_at`` lands between fetch and write isn't excluded
        from the next poll's filter.  Falls back to ``now()`` for callers
        that don't supply one (legacy behaviour).
        """
        logger.info("PrMonitor: task %s PR #%d feedback ready (%d chars)", task_id, pr.number, len(feedback))
        # No dispatch path → don't pop tracked or advance pr_comments_since.
        # Otherwise the feedback would be silently dropped *and* the next poll
        # would skip it via the since= filter.
        dispatch_fn = getattr(self._orchestrator, "dispatch_pr_fix", None)
        if dispatch_fn is None:
            return
        # Clear tracking after successful dispatch so re-entry to waiting_for_pr
        # after pr-fix push is treated as a fresh first poll.
        # If dispatch fails, re-insert so the feedback is retried on next poll cycle.
        tracked = self._tracked.pop(task_id, None)
        try:
            await dispatch_fn(task_id, feedback)
        except Exception:
            if tracked is not None:
                self._tracked[task_id] = tracked
            raise
        # Persist comments_since AFTER successful dispatch so a failed dispatch
        # followed by a server restart doesn't permanently skip the feedback.
        # This is a read-modify-write on metadata: between get_task and
        # update_task the event loop can yield to the just-scheduled pr-fix
        # agent task.  Safe in practice — the pr-fix subprocess takes minutes
        # to complete, so no realistic concurrent write path modifies these
        # metadata keys in the window between these two awaits.  The drainer's
        # writes also re-read via _merge_task_metadata, so they no longer
        # clobber pr_comments_since even if interleaving occurred.
        db = getattr(self._orchestrator, "db", None)
        if db and tracked:
            task = await db.get_task(task_id)
            if task:
                # Spread-merge on write so the intent is explicit: only the PR
                # monitoring keys below (pr_comments_since, and for a conflicting
                # PR pr_conflict_dispatched_sha) are added; every other key
                # passes through unchanged.
                cutoff = since_cutoff or datetime.now(UTC).isoformat()
                extra = {"pr_comments_since": cutoff}
                # Record the conflicting commit we just dispatched for so the
                # merge_conflict trigger debounces until a new commit lands.
                # (Only meaningful when the PR is actually conflicting; a
                # comment-driven dispatch on a mergeable PR leaves this untouched.)
                if pr.mergeable is False and pr.head_sha:
                    extra["pr_conflict_dispatched_sha"] = pr.head_sha
                merged = {**task.metadata, **extra}
                await db.update_task(task_id, metadata=merged)

    # ── Public surface for orchestrator (revise path) ────────────────────

    async def gather_pending_feedback(
        self,
        task_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        token: str,
        default_since: str | None = None,
    ) -> str | None:
        """Fetch and aggregate any debounced-but-not-yet-dispatched PR feedback.

        Used by the orchestrator's ``revise()`` path on a ``waiting_for_pr``
        task: the user's manual feedback should be combined with any review
        comments / failing checks the monitor has already seen but not yet
        dispatched (debounce window).  Drops the in-memory tracked entry as
        a side effect so the next poll cycle starts fresh.

        The ``since`` cutoff used for filtering reviews/comments is resolved
        internally — preferring the in-memory tracked entry's ``comments_since``
        (current debounce state) if one exists, otherwise falling back to
        ``default_since`` (typically the task's ``pr_pushed_at`` metadata).

        Returns:
            The aggregated feedback Markdown, or ``None`` if there is no
            actionable pending feedback to surface.
        """
        tracked = self._tracked.get(task_id)
        since = (tracked.comments_since if tracked else None) or default_since
        # Drop tracked first so a concurrent poll cycle won't classify this
        # task as in-debounce while we're fetching.  If the GitHub fan-out
        # raises, re-insert it so the next poll cycle continues with the
        # existing debounce state instead of restarting as a first-poll
        # (which would only check COMPLETE/ABANDONED and miss pending FEEDBACK).
        self._tracked.pop(task_id, None)
        try:
            client = await self._get_client(owner, repo, token)
            pr = await client.get_pr(pr_number)
            reviews, review_comments, issue_comments, checks = await asyncio.gather(
                client.get_reviews(pr_number),
                client.get_review_comments(pr_number, since=since),
                client.get_issue_comments(pr_number, since=since),
                client.get_check_status(pr.head_sha),
            )
        except Exception:
            if tracked is not None:
                self._tracked[task_id] = tracked
            raise
        pr.review_decision = compute_review_decision(reviews)
        if since:
            cutoff_dt = _parse_ts(since)
            recent_reviews = [r for r in reviews if _parse_ts(r.get("submitted_at", "")) > cutoff_dt]
        else:
            recent_reviews = reviews
        text = aggregate_feedback(recent_reviews, review_comments + issue_comments, checks)
        if text == "No specific feedback found.":
            return None
        return text

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _list_waiting_tasks(self) -> list[dict]:
        """Return every non-terminal PR-bearing task to poll (ADR-030).

        Delegates to the orchestrator's ``list_waiting_pr_tasks``, which now
        returns all non-terminal tasks carrying a ``pr_number`` (not just
        ``waiting_for_pr`` rows), each carrying ``status``. Returns an empty
        list when the orchestrator doesn't expose the method.

        The result is intentionally NOT scoped to ``self._monitor_state`` here:
        terminal handling (merge/close → complete/abandon) must see PR-bearing
        tasks parked in ANY non-terminal state, so a state filter would drop
        exactly the blocked/needs_input tasks this ADR exists to watch. The
        per-monitor-state scoping that used to live here now applies only to
        feedback dispatch, via the ``observed_status == "waiting_for_pr"`` gate
        in ``_poll_one`` (a feedback-eligible task is by definition sitting in
        the monitor's state). Terminal CAS is idempotent, so even a future
        multi-monitor topology can safely have every engine poll every task.
        """
        list_fn = getattr(self._orchestrator, "list_waiting_pr_tasks", None)
        if list_fn is None:
            return []
        try:
            return await list_fn()
        except Exception:
            logger.exception("PrMonitor: failed to list waiting PR tasks")
            return []

    @staticmethod
    def _extract_pr_number(task: dict) -> int | None:
        """Extract the PR number from task metadata."""
        meta = task.get("metadata", {})
        raw = meta.get("pr_number")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
