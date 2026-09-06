# ADR-046: Standing monitors & the heartbeat protocol — with branch-freshness as the first consumer

**Status**: Implemented — `Monitor` base + `MonitorHeartbeat`/`MonitorRegistry` (`lotsa/monitors/`), `pr_monitor` migrated onto the base (behaviour-preserving), standing branch-freshness monitor (`lotsa/branch_monitor.py`) + orchestrator non-mutating probe + one-click `sync_branch`, `GET /api/monitors` liveness read, and the dashboard freshness badge + Sync button. Deferred: `lotsa doctor` monitor check + a dashboard liveness strip (registry + endpoint ship now).
**Date**: 2026-09-06
**Related**: ADR-013 (orchestrator owns git state — the probe/sync git logic lives on the orchestrator), ADR-015/ADR-018 (`_sync_branch_to_main` + the conflict-dispatch machinery the sync button reuses), ADR-030 (pr_monitor — the exemplar step-scoped monitor migrated onto the base), ADR-040 (DB is state-of-record; the heartbeat registry is a rebuildable cache), ADR-044 (event pre/posthooks — the *event* cousin of this *polling* pattern). Scope: CE (`lotsa/monitors/`, orchestrator, dashboard).

---

## Context

Lotsa already keeps task branches synced with upstream (ADR-015/ADR-018), and
the PR monitor (ADR-030) already computes — internally and destructively —
exactly the two facts an operator would want to *see*: how far a branch is
behind and whether it still merges. None of it is surfaced, and there is no
manual trigger for the automatic-only sync machinery.

Underneath, the codebase already carried ~70% of a monitor framework, unnamed:
an engine registry (`register_engine`/`get_engine`), a uniform lifecycle
(`(orchestrator, monitor_state, config)` → `async run()`, spawned via
`create_task`, cancelled/drained on shutdown), per-entity backoff, a bounded
concurrency semaphore, and the ADR-040 invariant. But `PrMonitor` is
**step-scoped** — bound to a `queue_state`, gating a flow edge. A
branch-freshness poller is a different animal: **standing** — always-on,
project-scoped, gating nothing. The framework had to name both lifetimes, and
we wanted a *liveness signal* so monitors become observable infrastructure
rather than opaque loops (restart-resilience visibility, `lotsa doctor`, stall
detection).

## Decision

### 1. A first-class `Monitor` base + heartbeat (`lotsa/monitors/`)

`Monitor(ABC)` owns the loop mechanics extracted from `PrMonitor`'s bespoke
`run()`:

- an interval-driven loop (`interval_seconds` supplied by the subclass);
- **exception isolation** — one failed `tick()` is logged and skipped, never
  killing the loop; `CancelledError` propagates so shutdown cancels cleanly;
- a per-tick **heartbeat** written into an optional `MonitorRegistry`;
- an `aclose()` teardown hook run in the loop's `finally`.

A subclass implements `tick()` (one poll cycle) + the `interval_seconds`
property and declares a `name` and a **`kind`**:

- `"standing"` — always-on, project-scoped, gates nothing (branch-freshness);
- `"step_scoped"` — bound to a `queue_state`, gates a flow edge (`pr_monitor`).

`MonitorHeartbeat` records `last_tick_at` / `next_due_at` / `last_ok` /
`consecutive_failures` / `last_error`. `MonitorRegistry` is an in-memory store
keyed by name; monitors update their heartbeat **in place**, so `snapshot()`
reflects live state with no copy-back. Per ADR-040 the registry is a
**rebuildable cache** — reconstructed at every `start()`, never persisted.

### 2. `pr_monitor` migrated onto the base (behaviour-preserving)

`PrMonitor(Monitor)`: the loop moves to the base; the class supplies
`interval_seconds` (→ `config.poll_interval_seconds`), `tick()` (= the former
`_poll_all`), and `aclose()` (= the former client-close `finally`). Every
existing invariant (per-entity backoff, `_MAX_POLL_CONCURRENCY`, debounce SHAs,
`comments_since` restore, deferred-terminal ADR-030) is untouched — gated by the
existing `test_pr_monitor*` suites (one pattern day one, per the operator's
call). The `PrMonitorEngine` wrapper threads the orchestrator's
`_monitor_registry` in so each poller records a heartbeat.

### 3. Branch-freshness monitor (first standing consumer)

`BranchMonitor(Monitor, kind="standing")` per tick:

1. fetches `origin/<default>` **once per project** — a project's task worktrees
   share its object store, so one fetch in the project root updates the
   remote-tracking ref for every worktree; a per-project fetch failure is
   isolated;
2. runs a **non-mutating** probe per non-terminal worktree-bearing task
   (bounded concurrency), persisting the result to task metadata.

All git logic lives on the orchestrator (ADR-013): `fetch_project_default`,
`list_branch_watch_tasks` (non-terminal + has-a-worktree; skips `chat`/terminal),
and `refresh_branch_status`. The monitor talks to it through the typed
`BranchMonitorOrchestrator` Protocol (mirroring `PrMonitorOrchestrator`).

**Non-mutating probe.** For a *status* display we must not merge into a worktree
that may have a live agent, so the probe uses `git rev-list --count
HEAD..origin/<default>` for the behind-count and **`git merge-tree --write-tree
--name-only HEAD origin/<default>`** (Git ≥2.38) for mergeability — it writes a
tree object but never touches the worktree or index. Exit 0 → mergeable; exit 1
→ conflict (the conflicting files are the lines after the tree OID up to the
first blank line); exit ≥2 → git error. Persisted keys: `branch_behind`,
`branch_mergeable` (bool | null), `branch_conflicts`, `branch_checked_at`,
`branch_checked_against_sha` (so staleness is visible).

*Git <2.38 fallback.* If `merge-tree --write-tree` is unavailable, `merge-tree`
exits non-clean and the probe records `branch_mergeable = null` ("unknown") while
still persisting `branch_behind`, with a logged warning — it never falls back to
a mutating merge for a status probe.

### 4. Surface + one-click sync

The dashboard reads the pre-computed metadata (no per-open network cost): a
sidebar "↓N behind" badge + conflict dot, and a Changes-tab freshness strip.
When a task is **behind > 0**, last-known **mergeable**, and **idle** (waiting /
awaiting_operator / needs_input / blocked / waiting_for_pr — never `working`),
a **Sync with default** button calls `POST /api/tasks/{id}/sync-branch` →
`OrchestratorService.sync_branch`. That reuses `_sync_branch_to_main` (now
parameterised with `push`): it **pushes** when the task has a PR and does a
**local merge, no push** pre-PR. A merge that races a conflict re-anchors the
task into the pr_fix sub-flow and routes through `_handle_conflict_dispatch` →
the process's `resolve_conflicts` agent (or blocks when the process has none),
exactly as `retry()` does. The server re-validates the idle gate.

### 5. Liveness endpoint

`GET /api/monitors` serializes the heartbeat registry (`monitor_heartbeats()`),
adding derived `healthy` / `stale` flags. The `lotsa doctor` check and a
dashboard liveness strip are deferred; the registry + endpoint ship now.

## Relationship to ADR-044 pre/posthooks

Both are "extensible orchestrator behaviour", but this is the **polling** cousin
(interval-driven, standing/step-scoped loops) and ADR-044 hooks are the **event**
cousin (fire on a step boundary). They stay separate mechanisms.

## Consequences

- One monitor pattern: a new monitor is "subclass `Monitor`, implement `tick()`,
  declare an interval + kind, register". Branch-freshness is consumer #1;
  `pr_monitor` the migrated exemplar.
- Operators see how stale each task is and can one-click sync idle tasks — with
  the full "take care of conflicts" behaviour when the merge races.
- Monitors are observable (`/api/monitors`), a foundation for `lotsa doctor` +
  a dashboard strip later.
- A configurable knob: `branch_monitor_interval_seconds` (default 300, rejected
  if non-positive) + `branch_monitor_enabled` (default true).
