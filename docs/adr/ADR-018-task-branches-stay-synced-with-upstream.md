# ADR-018: Task branches stay synced with upstream across the lifecycle

**Status**: Accepted
**Date**: 2026-05-25
**Related**: ADR-013 (orchestrator owns git state), ADR-015 (orchestrator syncs branch to main before pr-fix). PR #70 instantiates this principle at task creation.

---

## Context

Lotsa has fixed the same class of bug twice in two different lifecycle events:

- **PR #70** — `WorktreeManager.create()` didn't fetch from origin before creating a worktree. Tasks branched from whatever stale commit the local default branch was sitting on. The concrete failure: task `<redacted>` was created against a 3-week-old base, the agent built ADR-014 against that snapshot, and 36+ commits of pr-fix loop phase 2 work on `origin/main` couldn't be cleanly reconciled. Closed PR (#68) was the result.
- **ADR-015** — the orchestrator didn't sync the task branch with main before pr-fix dispatch. Tasks that idled in `waiting_for_pr` for hours diverged from main; the pr-fix agent correctly emitted `PR_FIX_BLOCKED:` when the divergence made the worktree incoherent. The operator merged main into the worktree by hand. Concrete failure: task `<redacted>` was 9 commits behind main when pr-fix dispatched.

These are two instances of the same gap: **stale local refs were allowed to determine task behaviour**. ADR-013 says the orchestrator owns git state but doesn't say the orchestrator owns *keeping git state current*. Without a named principle, every new lifecycle event has to remember to sync on its own — and the cost of forgetting is a class of bug that's hard to diagnose (the symptom is "the agent produced wrong work" or "the merge is unmergeable," not "the orchestrator didn't fetch").

This ADR names the principle. It does not ship code — both instantiations exist (PR #70, ADR-015). The point is to make the rule explicit so future lifecycle events inherit it by default rather than by accident.

---

## Decision

**The orchestrator keeps the task branch synced with the canonical upstream branch across the task lifecycle. Stale local refs never determine task behaviour.**

This is a property of the orchestrator's git ownership (ADR-013), not a per-step concern. Every lifecycle event that *resolves* a branch state from local git — creation, dispatch, retry, sub-flow entry — first reconciles with upstream.

### Lifecycle events the principle covers

| Event | Sync action | Implementation |
|-------|-------------|----------------|
| **Worktree creation** | `git fetch origin <default_branch>`; base new worktree from `origin/<default_branch>` | PR #70 |
| **Pre-pr-fix dispatch** | `git fetch origin <default_branch>`; `git merge origin/<default_branch>` on the task branch; conflicts → `resolve_conflicts` agent step, `NEEDS_INPUT` escalation for judgment calls (ADR-015 as revised 2026-06-12) | ADR-015 |
| **Pre-retry from blocked** when the block was a push failure or any other state where local divergence is plausible | `git fetch origin <default_branch>`; `git merge origin/<default_branch>` before re-dispatching | Implemented — wired into `retry()`'s push-retry branch (`_sync_branch_to_main`) |
| **Pre-rebase after server restart** when a task was `working` and got flipped to `blocked` for recovery | Same as pre-retry | Implemented — restart recovery preserves `state="pushing"`, so it funnels through the same `retry()` push-retry branch |

The pre-retry and pre-rebase-after-restart rows were shipped together: they collapse to a single call site (`retry()`'s push-retry branch), so wiring `_sync_branch_to_main` there satisfied both — "wire the sync helper into one more call site," not a new design, exactly as anticipated.

### Lifecycle events the principle does NOT cover

| Event | Why excluded |
|-------|--------------|
| **Code / test / review agent dispatches** mid-task | The agent's spec and plan were produced against the branch state at task creation. A mid-task sync could invalidate the plan and produce internally inconsistent commits. The branch syncs at creation; it stays stable until the operator-visible boundaries below. |
| **Push step** | The push step's own pre-flight (clean tree, push, retry on NON_FAST_FORWARD) is sufficient. Pre-push syncing would be a separate decision about whether to auto-merge before push; today push failures surface to the operator, which is the desired behaviour. |
| **Operator-triggered actions** that explicitly target the task's existing branch state (e.g. `revise` from `needs_input`) | These reuse the branch as-is by intent. If the operator wants a sync, they trigger pr-fix or retry, which both run through the synced paths. |

### Contract for new instantiations

When a new lifecycle event needs to instantiate this principle:

1. **Use `git fetch`** to update the remote-tracking ref. Always `origin` + `<default_branch>` explicitly — never `git fetch` without args (slow, fetches everything).
2. **Best-effort failure handling**: log a warning and proceed with the last-known state. Never block a lifecycle event on a transient network failure. The fallback is "use stale local state" — which is the pre-fix status quo, not a regression.
3. **For "branch from upstream" events** (create, fresh worktree): base off `origin/<default_branch>` directly. Don't merge into local — there's no local state worth preserving.
4. **For "rebase onto upstream" events** (pr-fix, retry-after-divergence): `git merge origin/<default_branch> --no-edit` on the task branch.
   - **Clean merge**: continue the lifecycle event; push the new HEAD (CI re-runs, bot re-reviews on the new SHA — expected).
   - **Conflicts**: do not rollback the merge. Dispatch the `resolve_conflicts` agent step (ADR-015 as revised 2026-06-12); for judgment calls the agent escalates via `NEEDS_INPUT:` and the operator answers in the dashboard chat — humans never edit the worktree. ADR-015 details the step contract and the escalation path.
5. **Default branch is configurable** at the `WorktreeManager` / `OrchestratorService` level. Defaults to `main`. Repositories using `master`, `trunk`, etc. configure once; lifecycle events read the configured value.

### Why this is orchestrator work, not agent work

ADR-013 establishes that agents only stage and commit; the orchestrator owns push, branch, and rebase. Syncing with upstream is git mutation (fetch updates refs, merge rewrites HEAD), so by ADR-013 it belongs to the orchestrator. Delegating to an LLM would re-open the determinism risk ADR-013 closed, and the LLM has no information the orchestrator lacks — sync is a deterministic command sequence.

The pr-fix agent in the `<redacted>` incident recognised this correctly: it emitted `PR_FIX_BLOCKED:` rather than attempting the merge itself. That was the right behaviour for the agent — and the right signal that the orchestrator needed to own the sync.

---

## Why this isn't a niche concern

Every agent-orchestration system that runs long-lived workflows on git branches hits this. The shape is universal:

- A workflow forks from some upstream ref at moment T₀.
- The agent works for hours or days.
- Upstream advances independently — bug fixes, dependency bumps, schema migrations.
- By the time the workflow tries to merge back, the gap is wide enough that the merge is non-trivial.

Most systems handle this reactively: the merge fails, someone resolves it by hand. That's tolerable when "someone" is a human engineer who understands both sides. It's not tolerable when "someone" is an LLM whose context is the branch state at T₀, or when "someone" is an operator who's never seen the code.

The proactive shape (sync at every lifecycle event where divergence matters) keeps the gap small enough that auto-merge usually works. When it doesn't, the failure is visible and actionable inside the orchestrator's blocked-task UI rather than buried in `git status` on a worktree the operator may not know exists.

Lotsa is positioning to run non-engineering processes (research, ops, content — see ADR-014). Those operators won't have `git rebase` reflexes. The principle has to be enforced by the orchestrator or the platform fails them.

---

## Tradeoffs

**Pros:**

- **One rule, many call sites.** New lifecycle events that touch git state inherit the principle by default. No more class-of-bug-fix-by-class-of-bug-fix.
- **Operator-friendly degradation.** Sync failures are warnings, not blocks. Network outages or auth issues don't take Lotsa down — the worst case is "task ran on slightly stale upstream," which is no worse than the pre-fix status quo.
- **Composable with ADR-013.** Orchestrator owns git, and the orchestrator's git ownership includes keeping it current.
- **Makes the unimplemented call sites trivially identifiable.** Once the principle is named, "is this lifecycle event in the table?" is a yes/no question for any new orchestrator code.

**Cons:**

- **Adds latency to lifecycle events.** Every `WorktreeManager.create()` now does a `git fetch` (≤30s with timeout). Every pre-pr-fix dispatch does a fetch + merge. For pathological networks this is observable; for normal operation it's tens of milliseconds.
- **CI re-runs on the merge commit.** Pre-pr-fix sync that produces a new merge commit triggers CI again. Expected and desired (the merged state is what gets shipped), but worth noting for cost tracking.
- **Bot re-review on the merge commit.** Same shape as CI — the bot reviews the new SHA. Usually a no-op review; occasionally surfaces a real interaction between upstream changes and the task's diff.
- **Doesn't fix bad merges, only narrows the window.** A task that diverges sufficiently will still hit conflicts. The point is to make divergence rare and small, not to eliminate it.
- **Conflict escalation rides the existing `needs_input` affordance.** No new UI state (ADR-015's 2026-06-12 revision routes judgment calls through `NEEDS_INPUT`); the cost moved into the `resolve_conflicts` prompt's question quality instead.

---

## Backward compatibility

**No backward-compatibility work is required.** This ADR formalises a principle that's already partially implemented (ADR-015 specifies the pr-fix path; PR #70 ships the creation path). It doesn't change semantics — it captures intent that's already been committed to.

The "not yet implemented" rows in the lifecycle table (pre-retry-from-blocked, pre-rebase-after-restart) are new behaviour, but the orchestrator's existing retry paths already handle the "no sync happened" case (the task continues from where it left off). Adding a sync there is strictly additive — it fails more visibly when divergence is high but never regresses behaviour for tasks that aren't divergent.

---

## Scope

This ADR captures the principle. Concretely it commits to:

1. **Documenting** the principle in the constitution (CONSTITUTION.md §3 or §4) or in `lotsa/CLAUDE.md` once PR #66 merges. The principle becomes part of the durable architectural rule set, not just an oral tradition.
2. **Auditing existing lifecycle events** against the table above. Where an event needs the sync and doesn't have it (pre-retry-from-blocked, pre-rebase-after-restart), file follow-up issues or extend ADR-015's implementation.
3. **Establishing the contract** future PRs reference when adding new git-touching lifecycle events. The contract section above is the canonical specification.

This ADR is **Accepted**: both acceptance conditions are now met. The principle is encoded in `lotsa/CLAUDE.md` (the "Orchestrator keeps the task branch synced with upstream (ADR-018)" subsection), and both remaining lifecycle rows — pre-retry-from-blocked and pre-rebase-after-restart — are wired up against the contract above (`retry()`'s push-retry branch calls `_sync_branch_to_main`, with conflicts routed to `resolve_conflicts` and fetch errors degrading to a best-effort block).

---

## Out of scope

- ~~**Implementing the unimplemented rows** (pre-retry-from-blocked, pre-rebase-after-restart).~~ **Now shipped** — both rows landed together (they collapse to a single call site, `retry()`'s push-retry branch), delivered in one pass rather than the per-row PRs originally envisaged here.
- **Auto-merging on push failure** (NON_FAST_FORWARD → fetch + merge + retry push). Adjacent concern, currently handled by the push step's own retry logic. If we decide to fold this into ADR-018's umbrella, it's a follow-up.
- **Continuous-sync mid-task** (e.g. a daemon that periodically rebases idle tasks). Adds operational complexity (conflicting with in-flight agent dispatches, debounce semantics) without clear benefit over sync-at-lifecycle-event. Deferred unless evidence accumulates.
- **Cross-repository sync** (worktrees that span multiple repos). Not a current shape; would need its own ADR.
- **Sync against a non-default branch** (tasks that explicitly target a feature branch as their base). The contract above assumes the default branch; if a future use case needs per-task base branches, this ADR extends naturally — the configured `default_branch` becomes `task.metadata.base_branch` at lookup time.
