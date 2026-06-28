# ADR-015: Orchestrator syncs the task branch to main before pr-fix dispatch

**Status**: Implemented (Phase 1 — deterministic sync — shipped; Phase 2 — conflict path / `resolve_conflicts` + `merge_conflict` trigger — shipped)
**Date**: 2026-05-19
**Related**: ADR-013 (orchestrator owns git state), ADR-014 (jobs as unified primitive), ADR-018 (sync principle across the lifecycle). Triggered by task `lotsa/<redacted>` getting stuck 9 commits behind main during a Phase 2 pr-fix cycle. Revision triggered by task `<redacted>` / PR #116.

---

## Implementation status (Phase 1 shipped)

The work is split into two phases:

- **Phase 1 (shipped) — the deterministic clean-path sync.**
  `OrchestratorService._sync_branch_to_main(task_id) -> SyncResult` runs
  inside the pr-fix dispatch funnel (`_dispatch_pr_fix_locked`, after the
  CAS win, before the round is consumed or the agent dispatched) and
  symmetrically on a pr-fix `retry()`. It fetches `origin/main`, measures
  divergence, and on a behind branch auto-merges `origin/main --no-edit`
  and pushes the merged ref to `origin/lotsa/<task_id>` via the existing
  deterministic `execute_push`. All git calls are orchestrator-owned
  (ADR-013) and use `asyncio.create_subprocess_exec`.

- **Interim conflict behaviour (Phase 1) — superseded by Phase 2.** On a
  merge conflict, Phase 1 aborts the merge (leaving the worktree clean) and
  routes the task to `blocked` with a message naming the conflicting files
  (`git diff --name-only --diff-filter=U`) and pointing to Phase 2. Fetch/
  push errors ride the same `blocked` path; operator Retry re-runs the sync.
  This interim block-on-conflict is **explicitly replaced** by Phase 2's
  `resolve_conflicts` agent step.

- **Phase 2 (pending) — the conflict path.** The `resolve_conflicts` agent
  step (+ `resolve_conflicts-system.md` / `-user.md` prompts), the
  `CONFLICTS_RESOLVED` marker (regex + dispatch handler), `NEEDS_INPUT`
  escalation for unjudgeable conflicts, and the `merge_conflict` monitor
  trigger in `pr_monitor` (read `mergeable`/`CONFLICTING` from the existing
  `gh pr view` poll, added to the bundled `wait_for_pr_signal` `triggers:`
  lists). The ADR-014 migration to a registered `sync_with_main` `action`
  tool is a mechanical later step.

---

## Revision note (2026-06-12)

The original decision routed merge conflicts straight to a human
(`blocked/merge_conflict`, "Conflict auto-resolution" explicitly out of
scope). Task `<redacted>` (PR #116) invalidated that position with a
second incident shape the original didn't anticipate:

- The branch fell 2 commits behind main and the merge conflicted on a
  single `CLAUDE.md` ADR-index hunk — a conflict any agent can resolve
  trivially.
- A `CONFLICTING` PR suppresses GitHub's `pull_request`-triggered
  workflows (no test-merge commit can be built), so the pushed fixes
  were **never re-reviewed** — the stale "ISSUES FOUND" comment stayed
  the PR's last word.
- The monitor's triggers (comments, review decisions, failing checks)
  don't include mergeability, so nothing dispatched; when comments did
  arrive, pr-fix correctly reported "already fixed" and skipped. The
  task burned 10 pr-fix rounds against a ghost review.

Three changes, revised in place because nothing had shipped:

1. **Conflicts dispatch a `resolve_conflicts` agent step**; for
   conflicts the agent cannot judge, it escalates via the standard
   `NEEDS_INPUT` channel (operator answers in the dashboard chat —
   **humans never edit the worktree**). This does NOT extend agent
   git authority — the orchestrator still runs the `git merge`; the
   agent only edits files containing conflict markers, which is its
   existing file-edit authority. The commit posthook completes the
   merge commit deterministically.
2. **The PR monitor gains a `merge_conflict` trigger** — it polls
   mergeability alongside comments and checks, so a conflicted PR
   wakes the sync path without waiting for a comment that CI
   suppression may never produce.
3. The sync remains exactly as originally designed (deterministic,
   orchestrator-owned, agent-free on the clean path).

---

## Context

Long-running tasks drift from main. A task that takes hours to reach
`waiting_for_pr` and then sits there receiving PR feedback can be
many commits behind main by the time `pr-fix` is dispatched. Today
the orchestrator never reconciles the branch with main — the gap
accumulates indefinitely.

On task `<redacted>` the branch was **9 commits behind main** when
pr-fix dispatched. The pr-fix agent identified the divergence,
correctly recognised that rebasing/merging is orchestrator-owned
(per ADR-013 and the project's memory note), and emitted
`PR_FIX_BLOCKED:`. The operator then ran `git merge origin/main`
in the worktree by hand — clean auto-merge, no real conflicts —
pushed, and the loop continued.

The blocking behaviour was correct. The need for manual operator
intervention was not. The orchestrator has all the information it
needs to do the merge itself:

- The worktree path (it created it).
- The remote and base branch (in task metadata / flow config).
- An agent-free moment immediately before pr-fix dispatch where a
  merge can run without racing the agent.

Without this, every task that idles long enough in `waiting_for_pr`
eventually hits the same wall.

---

## Decision

**The orchestrator syncs the task branch to main as a precondition
of every `pr-fix` dispatch.** Concretely, at the top of
`_dispatch_pr_fix_locked` (the funnel every pr-fix entry point
already routes through — see Scope below — or its successor under
ADR-014):

1. `git fetch origin main` from the task's worktree. Unconditional —
   without it the local `origin/main` tracking ref can be stale and
   the divergence count below silently reads zero, skipping the sync
   we're trying to guarantee.
2. Read divergence: `git rev-list --count HEAD..origin/main` from
   the task's worktree.
3. If zero, proceed unchanged. The branch is current.
4. If non-zero, `git merge origin/main --no-edit` from the worktree.
5. **On a clean merge:** push the resulting ref to
   `origin/lotsa/<task_id>` (CI re-runs; bot re-reviews on the new
   SHA — expected). Continue to pr-fix dispatch.
6. **On merge conflicts** (revised 2026-06-12): *do not roll back
   the merge.* Leave the conflicts in the worktree and dispatch the
   `resolve_conflicts` agent step (see the section below). For
   conflicts the agent cannot judge, it escalates with
   `NEEDS_INPUT:` — the operator answers in the dashboard chat and
   the agent applies the decision. There is no manual-resolution
   state: humans never edit the worktree.
7. **On fetch or push errors** (network failure, auth failure,
   remote ref rejected): do not wrap with bespoke retry logic.
   Propagate as an unhandled exception so the orchestrator's
   existing dispatch error handler treats it as a non-rule failure
   per ADR-014's retry semantics — the task lands in
   `status=blocked` with `last_run_step` unchanged. Operator clicks
   Retry to re-attempt the sync from the top of
   `_dispatch_pr_fix_locked`.
   Transient network issues recover via the generic retry path; no
   `sync_error` sub-state is introduced.

The agent never sees divergence. It always works on a branch that
is either up-to-date or already past the merge it would otherwise
have flagged.

### Why this is orchestrator work, not agent work

The agent's contract (ADR-013, memory note) is `stage + commit`
only. Merging main *is* a git operation; it rewrites the worktree's
HEAD ref. Inside the orchestrator, it's a deterministic command;
delegating it to an LLM is unnecessary indirection and re-opens
the determinism risk ADR-013 closed.

### The `resolve_conflicts` agent step (added 2026-06-12)

The division of labour that keeps ADR-013 intact: **the merge is
orchestrator-owned; conflicted files are just files.** The
orchestrator runs `git merge origin/main` and, on conflict, leaves
the markers in place. Resolving markers is file editing — the
agent's existing core authority. No new git capability is granted.

The step is a normal `type: agent` job:

- **Inputs**: the conflicting file list
  (`git diff --name-only --diff-filter=U`), rendered into the user
  prompt alongside the task title.
- **Prompt contract**: resolve every conflict marker preserving
  both sides' intent; touch ONLY files that contained markers; run
  the project's tests; do not commit (the `commit` posthook
  completes the merge commit deterministically, as it does for
  every agent step).
- **Rules**:
  - `^CONFLICTS_RESOLVED:` → `next` (continue to pr-fix dispatch;
    the eventual push carries merge + resolution + fixes in one
    update, and CI reviews the bundle).
  - `^NEEDS_INPUT:` → `needs_input` (the standard agent-question
    channel). The agent emits this when a conflict requires
    judgment it cannot ground in the repo — e.g. both sides rewrote
    the same function with incompatible intents and neither tests
    nor specs disambiguate. The operator's chat answer re-dispatches
    the step with the decision under `## Revision Feedback`.
- **Cost posture**: the step only ever runs after a real conflict,
  which the deterministic sync already established is the rare
  case (`<redacted>`'s 9-commit divergence auto-merged cleanly). The
  common path stays agent-free.

### The `merge_conflict` monitor trigger (added 2026-06-12)

`pr_monitor` adds `merge_conflict` to its trigger vocabulary
(joining `human_comment`, `bot_comment`, `review_decision`,
`failing_check`): when the PR's `mergeable` field reads
`CONFLICTING`, the monitor fires the pr-fix dispatch path, whose
pre-sync (this ADR) performs the merge and routes to
`resolve_conflicts`. Without this trigger, a conflicted PR with no
new comments sleeps forever — and because GitHub suppresses
`pull_request` workflows on conflicted PRs, the "new comment" that
would otherwise wake it may never arrive (the `<redacted>` deadlock).
Mergeability is read from the same `gh pr view` poll the monitor
already performs; no extra API call.

### Escalation for conflicts the agent cannot judge

The normal lifecycle of a conflict is fully automated — monitor
trigger → orchestrator merge → agent resolution — and never shows
the operator anything. When `resolve_conflicts` hits a conflict it
cannot ground in the repo (both sides rewrote the same function
with incompatible intents and neither tests nor specs
disambiguate), it does NOT park the task for manual worktree
surgery. **Humans never edit the worktree.** Instead it escalates
through the standard `NEEDS_INPUT` channel, exactly as the planning
step already does:

- The agent emits `NEEDS_INPUT:` with a concrete, answerable
  question naming the file, the two intents, and the options
  (e.g. *"`orchestrator.py::dispatch` — main rewired this through
  the registry; our branch inlined it. Keep the registry version
  and re-apply our null-check on top?"*).
- The orchestrator parks the task at `needs_input` and surfaces the
  question in the dashboard chat — the same affordance operators
  already use for planning questions.
- The operator answers in chat. `answer()` re-dispatches
  `resolve_conflicts` with the reply under `## Revision Feedback`;
  the agent applies the decision, runs the tests, and emits
  `CONFLICTS_RESOLVED:`. The loop repeats if a further judgment
  call surfaces.
- The question/answer pair is persisted in the message log, so the
  human decision is audited *as a decision* — no separate override
  row is needed (and ADR-019's earlier sketch of a `merge_conflict`
  OverrideHandler is superseded by this escalation path; see the
  cross-reference in ADR-019's Out of scope).

The resolved merge commit is pushed to `origin/lotsa/<task_id>` as
part of the pr-fix agent's final push, not as a standalone step —
step 5's push only fires on the clean (non-conflict) path. CI
therefore runs on the merge-plus-fix bundle, not on a merged-only
state.

This sharpens ADR-013's principle rather than bending it: the
orchestrator owns git state, the agent owns file edits, and the
human owns judgment — expressed through the dashboard, never
through the worktree.

### Migration onto ADR-014

When ADR-014's job-type model lands, the pre-sync becomes an
explicit `action` job in the pr-fix flow:

```yaml
flows:
  pr_fix:
    steps: [sync_with_main, resolve_conflicts, pr-fix, review, push_pr, wait_for_pr_signal]
```

`sync_with_main` is a `type: action` job bound to a `sync_with_main`
tool in the registry. Its output rules (revised 2026-06-12):

- Clean merge (or already current) → `target: pr-fix` explicitly —
  NOT `target: next`, which would route clean merges through
  `resolve_conflicts` now that it sits next in the step sequence.
  The skip-over is the common path.
- Conflicts → `target: resolve_conflicts` (the agent step above;
  its own rules route `CONFLICTS_RESOLVED` onward to pr-fix and
  `NEEDS_INPUT` to the standard operator-question state).
- Fetch/push errors → propagate as exception (same retry-from-
  blocked path as the pre-ADR-014 design; no per-tool error rule
  needed).

For now (pre-ADR-014), the same behaviour lives as a hardcoded step
inside `_dispatch_pr_fix_locked`. The migration is mechanical:
extract the sync logic into a tool, wire it as a flow step. The
user-visible behaviour is identical.

---

## Tradeoffs

**Pros:**

- Eliminates a class of "stuck task" operator interrupts. The
  manual `git merge && push` recovery becomes orchestrator behaviour.
- Conflicts are surfaced early (at sync time) with file-level
  detail, instead of late (during agent review) with vague "branch
  is behind main" guesses.
- Bot review of the PR always reflects the latest main state — no
  more "you fixed X but main has moved past Y" stale-review noise.
- Cleanly migrates onto ADR-014's tool registry as a first-party
  `sync_with_main` tool.

**Cons:**

- Every pr-fix dispatch on an idle task now does an extra `git
  fetch` round-trip. Cheap (one HTTP call to GitHub) but non-zero
  cost.
- Each non-empty merge produces a new push to the PR branch, which
  re-triggers CI and bot review. For pathological cases (very long
  idle, many small main moves) this means more CI runs. Mitigation:
  the merge is only triggered by pr-fix dispatch, not on every poll
  cycle — so the cost is amortised across feedback round-trips,
  not arbitrary time.
- Merge commits accumulate in the PR branch history. Final squash
  on merge collapses them — no permanent noise.
- `resolve_conflicts` questions surface through the existing
  `needs_input` affordance — no new state or frontend surface, but
  the question-quality bar is on the prompt (a vague question wastes
  an operator round-trip).

---

## Scope

This ADR proposes the architectural rule. Implementation lands as a
focused PR after approval:

1. Add `OrchestratorService._sync_branch_to_main(task_id) -> SyncResult`
   helper that does the fetch + merge + push and returns one of
   `{clean, conflicts, error}`.
2. Call it at the top of `_dispatch_pr_fix_locked` so all three
   pr-fix entry points pick it up via the existing funnel:
   - `dispatch_pr_fix` — PrMonitor's poll-driven path.
   - `revise()` `waiting_for_pr` branch — user feedback while the PR
     sits open.
   - `revise()` `rebasing` branch — recovery from the legacy
     `rebasing` state column.
   All three already gate on `_dispatching_pr_fix` and route through
   `_dispatch_pr_fix_locked`, so a single sync at the locked entry
   covers every dispatch.
3. On `conflicts`, dispatch the `resolve_conflicts` agent step:
   new prompt pair (`resolve_conflicts-system.md` / `-user.md`) in
   each bundled process that has a pr-fix flow, the job entry with
   `CONFLICTS_RESOLVED`/`NEEDS_INPUT` rules, and the conflict
   file list rendered into the user prompt. `NEEDS_INPUT` reuses
   the existing needs_input machinery (`answer()` re-dispatch) —
   no new state, no new frontend surface.
4. Add the `merge_conflict` trigger to `pr_monitor` (read
   `mergeable` from the existing poll; fire the pr-fix dispatch
   path on `CONFLICTING`). Add it to the bundled processes'
   `wait_for_pr_signal` `triggers:` lists.
5. Tests: `_sync_branch_to_main` happy path / behind / conflict /
   network-error cases; `resolve_conflicts` marker routing both
   ways (`CONFLICTS_RESOLVED` continues, `NEEDS_INPUT` parks and
   resumes via `answer()`); monitor fires on `CONFLICTING`
   mergeability.

In scope for **Lotsa Community Edition** (`lotsa/`).

---

## Out of scope

- **Periodic background sync.** Syncing only at pr-fix dispatch is
  enough. Continuous sync would waste CI on every main move.
- **Multi-branch base support.** Tasks branched from non-main bases
  (release branches, feature branches) need a richer source-of-truth
  for the base ref. ADR-014 moves the `base_branch:` field onto the
  monitor job's `config:` block (the `pr:` top-level block is
  removed under that ADR); that field becomes the merge source.
  Out of scope for this ADR as a separate-concern fix.
- ~~**Conflict auto-resolution.** No attempt to use the agent to
  resolve conflicts. Human-only, per ADR-013.~~ *Reversed by the
  2026-06-12 revision* — the `resolve_conflicts` agent step is now
  the conflict path, escalating to the operator via `NEEDS_INPUT`
  (chat judgment, not worktree edits) when it cannot decide. The
  ADR-013 principle survives because the merge itself stays
  orchestrator-owned; the agent only edits conflicted files, and
  the human contributes judgment through chat (`NEEDS_INPUT`),
  never edits in the worktree.
- **Squash vs merge in the resulting commit history.** The local
  merge uses default `merge --no-edit` (a merge commit). Final PR
  merge strategy is operator choice.
