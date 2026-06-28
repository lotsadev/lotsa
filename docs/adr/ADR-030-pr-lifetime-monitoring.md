# ADR-030: PR-lifetime monitoring — an opened PR is watched regardless of task state

**Status**: Implemented
**Date**: 2026-06-12

> **Implemented**: `list_waiting_pr_tasks` now discovers every non-terminal
> `pr_number`-bearing task (carrying `status`); `PrMonitor` defers a terminal
> signal landing on a `working` task (`MonitoredPr.terminal_pending`, consumed
> by the drainer via `take_terminal_pending`) and gates FEEDBACK on observed
> `status == "waiting_for_pr"`; `transition_task` no longer edge-gates terminal
> (`complete`/`abandoned`) outcomes, so a merge/close completes a task parked in
> any state (including the `state="blocked"` sink) with an audit row naming the
> parked status; and `StageBar` shows a `PR #{n}` badge whenever a PR is open.
**Related**: ADR-014 (jobs as unified primitive — the monitor job type this generalizes), ADR-015 (sync + conflict handling; removes one cause of the stranding this ADR fixes), ADR-019 (operator-acknowledged overrides — the guard that triggered the stranding incident). Triggered by task `<redacted>` / PR #116.

---

## Context

The `PrMonitor` only watches tasks whose status is `waiting_for_pr`
(`pr_monitor.py` — "tracks all tasks in `waiting_for_pr` state"). The
monitor is architecturally a *flow step* (`wait_for_pr_signal`,
ADR-014 `type: monitor`): a task is watched while it sits on that
step and unwatched everywhere else.

That scoping is correct for **feedback** signals — a task that is
`working` (agent in flight) or `blocked` (operator attention needed)
should not have pr-fix dispatched into it. But it is wrong for
**terminal** signals. The PR's merged/closed state is a fact about
the world that stays true no matter what the local task is doing —
and acting on it is what lets a task finish.

### The incident

Task `<redacted>` (PR #116):

1. Stale-base ghost reviews made pr-fix legitimately skip 4
   consecutive rounds ("already fixed" was true each time).
2. The `max_consecutive_skipped: 3` guard fired and blocked the task
   — the guard misread correct behaviour as suspicious.
3. **Blocked ⇒ unmonitored.** The operator reviewed the PR by hand,
   found it good, and merged it on GitHub.
4. The merge was invisible to Lotsa. The task sat `blocked` forever
   with its PR merged — a zombie the operator had to diagnose and
   clear by hand (SQL flip back to `waiting_for_pr`, after which the
   monitor completed it within one poll cycle).

Step 4 is the bug. The completion machinery worked perfectly the
moment the task re-entered the monitor's view; only the visibility
rule kept it stranded. ADR-015's implementation removes *this
incident's* root cause (ghost reviews → skip streak → guard), but
any future path that parks a PR-bearing task outside
`waiting_for_pr` — a new guard, a crash recovery, an operator stop —
re-creates the zombie.

## Decision

**Once a task has opened a PR, Lotsa monitors that PR until it
reaches a terminal state (merged or closed) — regardless of the
task's local status, state, or current step.**

Signal handling splits by class:

| Signal class | Acts when | Rationale |
|---|---|---|
| **Terminal** (`merged`, `closed`) | Always — any status, any step | The PR's fate is global truth; acting on it is completion, not dispatch. |
| **Feedback** (comments, review decisions, failing checks, `merge_conflict` mergeability) | Only in `waiting_for_pr`, unchanged | Dispatching pr-fix into a `working`/`blocked`/`needs_input` task would race the in-flight agent or bypass the operator's attention. |

### Mechanics

1. **Tracking set widens.** The monitor's task discovery changes from
   "status = `waiting_for_pr`" to "`metadata.pr_number` is set AND
   task not terminal (`complete`/`abandoned`/`archived`)". The
   per-task poll already classifies signals; only the discovery
   predicate moves.
2. **Terminal-signal handling becomes state-aware:**
   - Task `waiting_for_pr` → existing behaviour (complete/abandon).
   - Task `blocked` / `needs_input` / any parked state → same
     completion path: transition to `complete` (CAS from the actual
     observed status), audit row "PR #N merged while task was
     <status> — completing", worktree cleanup.
   - Task `working` (agent in flight) → **defer**: mark the
     MonitoredPr terminal-pending and act when the dispatch
     completes (the drainer checks the flag). Cancelling a running
     agent on merge would discard work mid-write; the agent's
     completion routing happens-before the deferred completion.
3. **Feedback-signal handling is unchanged** — the existing
   status gate stays.
4. **Poll cost**: tasks parked outside `waiting_for_pr` are polled at
   the same interval. The set is small (open PRs per Lotsa instance),
   and the per-poll `gh pr view` is one HTTP call. No new config.

### Why not "fix the guards instead"

Each guard that parks a task (`max_pr_fix_rounds`,
`max_consecutive_skipped`, `merge_conflict`-era states, future
guards) could individually learn to keep watching the PR — but that
is the same class-of-bug-fix-by-class-of-bug-fix ADR-018 warns
about. The invariant belongs in the monitor, stated once: *open PR ⇒
watched until terminal.* Guards then never need to know about
monitoring at all.

## Consequences

**Positive**
- Zombie PR-bearing tasks become impossible: any merge/close
  eventually completes/abandons the task, whatever local state it
  was parked in.
- Operator merges-by-hand (a legitimate override of the loop) become
  a first-class path instead of a stranding.
- Guards stay simple — they park tasks without owning PR visibility.

**Negative / risks**
- The completion CAS must handle every non-terminal status as a
  legal `from_status`, not just `waiting_for_pr` — a wider
  transition surface to test.
- Deferred completion for `working` tasks adds one flag and one
  drainer check — small but real coupling between monitor and
  drainer.
- A task the operator *wanted* parked (investigating something) can
  now complete out from under them if someone merges the PR. Judged
  acceptable: the PR merging is the stronger signal, and the audit
  row says exactly what happened.

## Implementation sketch

1. Widen `_list_waiting_tasks` (or its engine-path equivalent) to the
   new predicate; keep the per-monitor-state scoping for feedback
   dispatch only.
2. Terminal-signal handler: CAS from observed status → `complete` /
   `abandoned` with the new audit row; `working` defers via a
   `terminal_pending` flag the drainer consumes.
3. Tests: merged-while-blocked completes; closed-while-needs_input
   abandons; merged-while-working defers until dispatch completion;
   feedback-while-blocked still does NOT dispatch.
4. Frontend: task header shows the task ID and a PR badge
   (number + link) whenever `metadata.pr_number` is set — the
   operator-visible counterpart of "an opened PR is never invisible."
   (Today the header shows only the branch name.)
