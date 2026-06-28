# ADR-021: Per-task process dispatch

**Status**: Implemented
**Date**: 2026-06-01 (Accepted) → 2026-06-10 (Implemented)
**Implementation**: PR #108 — lands all seven scope steps in one PR: per-task dispatch helpers + per-process state collections (`_process_for`/`_flow_for`, `_action_states_by_process`/`_monitor_states_by_process`) and the sweep of dispatch sites in `lotsa/orchestrator.py`, per-process monitor engine instances, recovery sweep with legacy-row fallback, `create_task` accepting any loaded process (`lotsa/server/api_routes.py`), cross-process target rejection (`lotsa/flows.py`), and the frontend process picker (`lotsa/frontend/src/components/process-picker.tsx`). Closes the per-task dispatch deferral noted in ADR-014's header.
**Related**: ADR-014 (jobs as unified primitive), PR #78 (multi-process
catalog landing without per-task dispatch), `lotsa/orchestrator.py`,
`lotsa/flows.py`

---

## Context

ADR-014 introduced the `Process` primitive and PR #78 wired a catalog
of processes through `lotsa.yaml`'s `processes:` block. `lotsa serve`
loads every defined process at startup; the API surfaces them; tasks
record `process_name` in metadata. But one architectural rule from
the original orchestrator survived: **`OrchestratorService` holds one
"active" process at a time** and every dispatch site reads from the
singletons `self.process` / `self.flow`. Picking a different process
today means restarting Lotsa with `--process <name>`.

That singleton is the right shape when there is exactly one process.
It becomes friction the moment a user defines two processes
(`bug_fix_flow`, `feature_flow`) intending to use both in the same
Lotsa instance — the natural workflow for software-development teams
who already think in terms of branching flows for different task
types.

The orchestrator was built around the singleton in several places:

- `self.process: Process` and `self.flow: FlowConfig` are read by ~50
  call sites — action methods, dispatcher paths, drainer branches,
  recovery sweep, transition guards, prompt loading.
- `self._action_states: set[str]` and `self._monitor_state: str` are
  derived from the active process at `start()` time. The restart
  recovery sweep, retry-routing, and `untrack` guards read them.
- `self._pr_monitor` (singular) is a single engine instance bound to
  the active process's monitor job.
- Cross-flow edges (`_register_cross_flow_edges`) stitch sub-flow
  entry/exit transitions into both flows' state machines, assuming
  there is exactly one process's worth of flows in scope.

The singletons are not wrong by themselves — they're an
*observability bias* that makes single-process the cheap path and
multi-process expensive. The cost is real because the failure mode is
silent: a state-machine check against the wrong process's transitions
table returns the wrong answer without raising. The bug manifests as
tasks landing in unexpected states, not as crashes.

The reason this didn't surface in PR #78 is that the catalog loads
multiple processes but the dispatch still uses singletons — so today
every task in flight is the active process by construction. Lifting
that constraint is the work this ADR proposes.

---

## Decision

**The orchestrator dispatches each task against the process the task
declares in its metadata.** `self.process` / `self.flow` stop being
load-bearing for routing; they become a default that's read only at
task-creation time and only when the caller didn't specify a process.

Concretely:

1. **Per-task lookup.** Every site that reads `self.process` or
   `self.flow` to make a routing decision (step name → job, state →
   transition validity, prompt loading) reads from the task's
   process instead. Introduce `_process_for(task) -> Process` and
   `_flow_for(task) -> FlowConfig` helpers that read
   `metadata['process_name']` / `metadata['current_flow']` and fall
   back to the active process for legacy tasks (those persisted
   before this ADR landed).

2. **Decompose the singletons.** Promote the per-process derived state
   from singletons to per-process collections:

   - `self._action_states: set[str]` →
     `self._action_states_by_process: dict[str, set[str]]`. The
     recovery sweep walks every task, looks up its process, checks
     against that process's action states.
   - `self._monitor_state: str | None` →
     `self._monitor_states_by_process: dict[str, str | None]`.
     Read by `block()`, `jump_to_step()`, `untrack` guards, all
     keyed on the task's process.
   - `self._pr_monitor` (singular engine instance) → a dict of
     engine instances, one per monitor-bearing process. Each
     engine's poll loop runs concurrently; each scopes its
     waiting-task query to its own state (the `monitor_state=`
     plumbing PR #77 added becomes load-bearing).
   - `self._pr_monitor_config` (singular) → per-process configs
     read by the pr-fix-specific cap logic.

3. **Cross-flow edges stay within-process.** A sub-flow only refers
   to flows within its own process. The cross-flow edge registrar
   stitches entry/exit edges in BOTH host and destination flow's
   SMs *within the same process*. Cross-process dispatch is not
   supported — a sub-flow rule target that names a job in a
   different process is a parse-time error.

4. **The active-process concept survives as a default.** When a
   task is created without an explicit `process` field, it gets
   the active process's name written into its metadata. The
   active process is still selected at startup (the inline
   `default: true` entry, or `--process <name>`, or the bundled
   fallback). Tasks created against any other loaded process
   record that process's name. From that moment on, the task's
   process is its own — switching the orchestrator's "active"
   default later doesn't move existing tasks.

5. **`create_task` accepts any loaded process.** PR #78's
   "rejects non-active processes" error message is removed; any
   name in `self._processes` is a valid target. The error path
   becomes "unknown process" only — for names not in the catalog
   at all.

6. **API and UI follow.** The `GET /api/processes` shape stays
   the same; `is_active` is retained as "this is the configured
   default" rather than "this is the only one that works".
   `POST /api/tasks` accepts `process: <name>` for any loaded
   process. The new-task UI's dropdown becomes a real picker
   rather than an informational display.

### The legacy-row contract

Tasks persisted before this ADR have no `process_name` in metadata.
The lookup must fall back to the active process — `_process_for(task)`
returns `self.process` when metadata is missing the field. This is
the same shape PR #78 already established; the helper is already
written. No migration is required; the fallback covers the upgrade
case.

Where this matters specifically: the restart recovery sweep walks
every persisted task row. For pre-ADR-021 rows, the sweep uses the
active process's action states. For post-ADR-021 rows, the sweep
uses the row's own process's action states. The mixed-population
case (some legacy rows, some new) must work without operator
intervention.

### Why not the multi-instance-per-process alternative

The alternative considered (and ruled out per operator discussion) is
running one Lotsa instance per process — `lotsa serve --process A
--port 8420` plus `lotsa serve --process B --port 8421`. Each
instance keeps its singleton; there's no refactor.

This was rejected for three reasons:

1. **Unified task list.** Software teams running multiple flows
   (bug-fix, feature, refactor) want all their tasks in one place.
   Per-instance isolation forks the task list per process.
2. **Shared infrastructure.** Each instance would need its own DB
   and worktree dir, or careful coordination to share them.
   Single-instance is simpler.
3. **Operator overhead.** N processes means N services to manage,
   N ports, N browser tabs. For the bundled CE single-developer
   workflow this is wrong friction.

The redesign in this ADR keeps the single-instance UX while removing
the singleton constraint.

---

## Tradeoffs

**Pros:**

- Multiple processes coexist in one instance. The natural
  software-team workflow (one process per task type) works without
  restart.
- The orchestrator's state model matches its data model.
  `task.metadata.process_name` becomes a load-bearing field rather
  than a forward-compatibility marker.
- Custom engines (delivered by PR #77) compose with multi-process
  cleanly — each engine instance is scoped to its monitor job's
  process.
- The dropdown UI becomes a real picker rather than an
  "informational display + restart instructions" affordance.

**Cons:**

- ~30-50 call sites in `orchestrator.py` change. The silent-failure
  mode (wrong SM, wrong action states) means partial coverage is
  dangerous. Test infrastructure that pins per-task routing under
  multi-process load is non-trivial.
- More concurrent state — multiple engine poll loops running
  simultaneously, each with its own debounce/cursor bookkeeping.
  No new race classes (each engine scoped to its own state) but
  more in-flight asyncio tasks.
- The cross-process boundary is a new invariant to enforce. Today
  the YAML parser doesn't reject "rule targeting a job in another
  process" because the concept didn't exist; the parser update is
  a small but real schema change.
- Legacy-row testing requires fixtures that seed both pre- and
  post-ADR-021 metadata shapes and confirms the recovery sweep
  routes both correctly.

---

## Scope

This ADR proposes the architectural rule. Implementation lands in a
focused PR series after approval. Splitting suggested:

1. **Helpers and per-process collections.** Introduce
   `_process_for` / `_flow_for` (already present from PR #78,
   currently unused at call sites). Promote `_action_states`,
   `_monitor_state`, and the engine instance to per-process
   collections. No call-site changes yet — the singletons stay
   alongside the collections as `@property`-style backward-compat
   accessors that return the active process's values.
2. **Sweep dispatch sites.** Replace `self.process` / `self.flow`
   reads in the action methods, dispatcher paths, and drainer
   branches with `_process_for(task)` / `_flow_for(task)`. The
   ordering and won-check patterns from
   `.claude/review-checklist.md` apply throughout — every CAS site
   keeps its existing guard shape, only the SM read changes.
3. **Recovery sweep + restart.** Update the `start()` recovery
   sweep to look up each persisted task's process before deciding
   on action-state vs working-state routing. Pin the legacy-row
   fallback with a regression test that mixes both populations.
4. **Engine concurrency.** Start one engine task per
   monitor-bearing process. Each engine's `monitor_state=` filter
   (PR #77) becomes the routing key.
5. **`create_task` accepts any loaded process.** Remove the
   "non-active rejected" error path; the only remaining error is
   "unknown process name".
6. **Cross-process target rejection.** `_validate_registry_references`
   gains a check that rule targets resolve within the source
   process. Parse-time error message names the offending rule and
   suggests either inlining the step or restructuring the process.
7. **Frontend.** The dropdown UI on the new-task surface becomes a
   real picker. API contract is unchanged from PR #78's shape.

Each step is independently verifiable. Step 2 is the
highest-blast-radius one and benefits most from the
`lotsa/CLAUDE.md` discipline (every site that reads SM internals
must consult the active flow; symmetric handling across sibling
branches is itself an invariant).

In scope for **Lotsa Community Edition** (`lotsa/`) as the proving
ground.

---

## Out of scope

- **Cross-process task references.** A task in process A cannot
  reference or dispatch into a job in process B. Sub-flows are
  within-process only. If a future workflow needs cross-process
  composition, that's a separate ADR.
- **Per-process worktree isolation.** Today every task gets a
  worktree under `~/.lotsa/worktrees/<task_id>` regardless of
  process. No change here — tasks of different processes share
  the worktree namespace; the per-task-id keying is sufficient.
- **Per-process budgets.** The `budget:` config field is
  orchestrator-wide. Per-process budget caps would need a separate
  bookkeeping mechanism; not required for the workflow this ADR
  enables.
- **Process hot-reload.** A change to `lotsa.yaml`'s `processes:`
  block requires a Lotsa restart. In-flight tasks against a removed
  process would orphan; the recovery sweep would catch them on
  restart but mid-session reload is out of scope.
- **Re-routing existing tasks.** Once a task is created against
  process A, it stays on A. There is no "change this task's
  process" operation. The user creates a new task if they want a
  different flow.
- **Frontend dropdown UI design.** This ADR specifies the
  behavioural contract (dropdown becomes a real picker); the
  visual design and interaction model are deferred to the
  implementation PR.
