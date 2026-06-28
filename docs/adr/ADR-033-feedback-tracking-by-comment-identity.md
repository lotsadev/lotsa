# ADR-033: PR feedback tracking by comment identity, not a wall-clock cursor

**Status**: Proposed — priority raised after a launch-hardening recurrence (2026-06-27); next in the pr-fix reliability line.
**Date**: 2026-06-16 (revised 2026-06-27)
**Related**: ADR-030 (PR-lifetime monitoring — the monitor this changes the bookkeeping of), ADR-040 (restart-resilient orchestration — the general "the DB is the state of record, in-memory is a cache" invariant this is one application of), the pr-fix loop and `PrMonitor`, #145 (retry re-fetch — whose cursor-bounded limitation this removes), tasks `<redacted>` / `<redacted>` / PR #144 and a later launch-hardening recurrence (the incidents).

---

## Context

Lotsa decides "is there new PR feedback for the agent to act on?" with a
**wall-clock timestamp cursor**, `pr_comments_since`. The pr-fix dispatch
feeds the agent only comments newer than that timestamp (so it doesn't
re-address handled feedback), and the cursor advances toward "now" as the
loop runs.

A timestamp cannot distinguish two cases that look identical to it:

1. **The agent saw a comment and skipped it** (correctly — not actionable).
2. **A comment finalized *after* the cursor advanced** (the `claude[bot]`
   reviewer posts a "working…" placeholder, then edits in the real review
   minutes later — same comment id, later `updated_at`).

Both leave a comment whose timestamp straddles the cursor. The timestamp
model is forced into a lose-lose:

- **Advance the cursor on every dispatch** (today's behaviour) → case 2's
  late-finalized review lands *behind* the cursor and is never delivered.
  This is exactly what stranded `<redacted>` and PR #144: the review finalized
  ~40s after a dispatch, and the agent kept seeing "no feedback." #145's
  retry re-fetch only helps when the feedback is *newer* than the cursor — it
  can't resurrect feedback the cursor already passed.
- **Don't advance on a skip** → the same non-actionable comment re-triggers
  every poll → a dispatch/skip loop (which is *why* the cursor advances, and
  why `max_consecutive_skipped` exists as a backstop).

There is no timestamp threshold that gets both right, because the missing
information isn't *when* — it's *which version of which comment have we
already shown the agent*.

Lotsa already computes that information: `PrMonitor` keeps
`last_updated_at_by_comment_id` (`comment_id → last-seen updated_at`). But it
is **in-memory only** — rebuilt empty on every server restart — and the
*durable* record (the thing persisted to task metadata, the thing
`gather_pending_feedback` and the drainer key off) is the timestamp cursor.
So the identity information exists transiently and is thrown away.

### Recurrence at launch (2026-06-27)

A task hit this again while hardening the launch deploy — a textbook instance
plus two compounding factors worth recording:

- The `claude[bot]` reviewer posted its review at 09:36–09:38 as **one comment
  edited in place** (`created_at` 09:36:33, `updated_at` 09:38:16) — exactly the
  "same id, later `updated_at`" shape above.
- The first pr-fix dispatch died in the sync step (an unrelated git-auth bug),
  so it never consumed the feedback.
- A server **restart** (to ship the auth fix) then wiped the in-memory
  `last_updated_at_by_comment_id`, leaving the wall-clock cursor as the only
  surviving state.
- Subsequent retries dispatched pr-fix with the cursor already advanced past
  09:38, so `aggregate_feedback` returned "No specific feedback found" on a PR
  whose review was sitting right there in the comments API.

Reproduced directly against the live PR: `since=pr_pushed_at` returns the
review and aggregates it; `since=pr_comments_since` returns nothing. The data
was never lost — the wall-clock *gate* in front of it moved. This is the third
independent occurrence, and the one that motivated raising the priority and
naming the underlying invariant (ADR-040).

## Decision

**Track delivered PR feedback by comment identity + version, persisted, and
make the delivery decision off that — not off a wall-clock cursor.**

### The delivered-versions map

A new task-metadata field:

```
pr_delivered: dict[str, str]   # str(comment_id) -> updated_at last delivered to the agent
```

A comment is **pending** (should be delivered to the next pr-fix dispatch)
iff `pr_delivered.get(str(comment.id)) != comment.updated_at` — i.e. it's new
(no entry) or edited since we last delivered it (entry differs). This is the
exact information the timestamp lacked:

- The late-finalized review: same id, *new* `updated_at` → pending → delivered.
  Case 2 fixed.
- The seen-and-skipped comment: same id, *same* `updated_at` → not pending →
  not re-delivered → no loop. Case 1 fixed.
- A genuinely new comment: no entry → pending → delivered.

The two failure modes the timestamp forced a choice between are now both
correct, because identity carries what the timestamp couldn't.

### When the map updates

`pr_delivered` is merged with the versions actually delivered **at dispatch
time** (the comments aggregated into that round's feedback), for *every*
pr-fix dispatch — monitor-driven, revise, answer, retry, jump. A skip records
the versions too: "I showed the agent X@v1; it wasn't actionable" — so X@v1
won't re-trigger, but X@v2 (an edit) will. This is what lets a skip be safe
*without* a wall-clock advance.

### The timestamp's reduced role

`pr_comments_since` is **not** removed — it survives as a coarse **fetch
bound** only: `GitHub get_comments(since=…)` to avoid pulling the PR's entire
comment history each poll. Correctness no longer depends on it; it's a
performance floor (set generously, e.g. `pr_pushed_at`). The *delivery*
decision is the identity map. (Keeping a too-tight `since` could still hide a
comment from the fetch, so the floor must be conservative — documented in the
implementation.)

### Restart durability (a bonus fix)

Because `pr_delivered` lives in task metadata, the monitor **restores** it on
restart instead of starting blind. Today a restart wipes
`last_updated_at_by_comment_id`, so the first post-restart poll re-treats
every comment as new and can re-dispatch already-handled feedback. Persisting
the map removes that restart-amnesia class too.

This is the feedback-tracking instance of **ADR-040**'s general rule: the DB is
the state of record, and `PrMonitor._tracked` (plus its debounce timers) is a
cache rebuildable from metadata at any poll. ADR-040 carries that invariant
across the rest of the orchestrator and adds resume-on-restart for in-flight
agents; this ADR and ADR-040 ship as a pair.

## Consequences

**Positive**
- The review-finalizes-after-dispatch race (the root of `<redacted>` / #144 and
  half the recent pr-fix incidents) is structurally fixed, not mitigated.
- Skips are safe without a wall-clock advance → no dispatch/skip loops *and*
  no missed late feedback. Combined with #145's benign-skip accounting and
  #149's one-button resume, the pr-fix loop's "stuck task" failure family is
  closed end to end.
- Restart no longer re-delivers handled feedback.
- The map is auditable (it's in metadata) — you can see exactly what version
  of which comment the agent has been shown.

**Negative / risks**
- This is the most incident-prone subsystem; the change touches
  `classify_signals` (FEEDBACK vs NONE now consults the map), the aggregate
  filter, the dispatch-time record, and the monitor's restore path. Staged
  carefully with tests for each: late-edit redelivers, same-version doesn't,
  restart restores, skip records without looping.
- `pr_delivered` grows with the PR's comment count. Bounded by comments-per-PR
  (tens, not thousands); prune on terminal if it ever matters.
- Migration: existing in-flight tasks have no `pr_delivered`. Treat absent as
  empty — the first poll re-derives it from the live comments (one possible
  redundant dispatch on upgrade, acceptable; pre-alpha anyway).

## Implementation sketch

1. Add `pr_delivered` to the metadata contract; absent ⇒ `{}`.
2. `aggregate_feedback` / `gather_pending_feedback`: filter fetched comments to
   those whose `(id, updated_at)` isn't in `pr_delivered` (replacing the
   `pr_comments_since` content filter). Keep `since=` as the fetch bound only.
3. At every pr-fix dispatch — **including a skip** — merge the `{id: updated_at}`
   of the comments shown to the agent that round into `pr_delivered` (the write
   that today advances `pr_comments_since`). The skip case is load-bearing (see
   "When the map updates"): recording a skipped comment's version is exactly what
   stops a non-actionable comment from re-triggering while still letting an edit
   to it (a new `updated_at`) trigger again.
4. `classify_signals`: FEEDBACK iff any fetched comment is pending by the map
   (plus the existing review-decision / failing-check signals).
5. `PrMonitor` restores `last_updated_at_by_comment_id` from `pr_delivered` on
   (re)start; the in-memory map becomes a cache of the persisted one.
6. Tests: late-edit→redeliver; same-version→no-deliver→no-loop;
   restart→restore (no re-deliver); skip records versions; new comment delivers.

> **Note (illustrative sketch).** This is the design intent, not a line-by-line
> plan — the exact plumbing is finalized at implementation. One known seam: the
> revise/answer path goes through `gather_pending_feedback`, which today returns
> `str | None` and so doesn't carry the `{id: updated_at}` map step 3 needs.
> Implementation will close that by either having `gather_pending_feedback`
> write `pr_delivered` internally before returning, or returning the map
> alongside the aggregated text. The monitor-driven path already holds the raw
> comment objects and can write the map directly.

## Out of scope

- Changing *what* counts as a signal (review decision, failing checks) — only
  *how delivered-ness is tracked*.
- Inline review-comment threading / resolution state (GitHub's
  `resolved`-thread API) — a richer future signal, separate ADR.
- Cross-task feedback sharing.
