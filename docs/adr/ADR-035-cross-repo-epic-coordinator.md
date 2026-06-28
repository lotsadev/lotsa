# ADR-035: Cross-repo coordinated changes — the epic coordinator and contract-first fan-out

**Status**: Proposed — post-launch flagship; phased
**Date**: 2026-06-22
**Related**: ADR-027 (promotion — this generalizes 1→1 promotion into 1→N fan-out), ADR-034 (chat-first entry — the surface this starts from), ADR-029 (multi-project — the per-project worktrees children run in), ADR-026 (orchestrator-managed background tasks — the RISKY/UNDECIDED area this scopes and partially resolves), ADR-013 (orchestrator owns dispatch + git — the invariant the brokering preserves), ADR-021 (per-task process dispatch). Scope: CE, with an orchestrator-brokered read-only-scout primitive that may live in `rigg/`.

---

## Context

Real work on a stack spans repos: change an API in the backend and the
frontend and mobile app must follow; a shared type changes and every
consumer adapts. Today each repo is a separate task with no shared context,
so the operator manually decomposes the change, copies context between
tasks, and keeps the cross-repo design coherent in their head. At 15-20
repos that doesn't scale.

The pieces to do better already exist: ADR-029 gives a project registry and
per-project worktrees; ADR-034 makes chat the entry point; ADR-027 lets a
task grow into a process. What's missing is the layer **above** the task —
something that investigates a change across repos, decides one coherent
cross-repo design, and coordinates a family of per-repo tasks against it.

The naive version — "an agent that spawns agents that spawn tasks" — walks
straight into ADR-026's RISKY/UNDECIDED territory: the orchestrator loses
ownership of the dispatch tree, concurrency and cost run away, and git
authority leaks. This ADR adopts the capability while keeping ADR-013's
invariant (the orchestrator owns dispatch and git) intact.

Two design commitments shape everything below, settled during design:

1. **Contract-first.** Investigation is a *design* investigation, not a
   location search. Before any repo is touched, the system decides the
   shared solution — and it must be idiomatic to the existing stack
   (proposing gRPC to a JSON-REST backend is a failure, not a feature). A
   single **contract** artifact is agreed up front; each repo then
   implements against it independently, in parallel.
2. **A persistent epic coordinator.** The cross-repo unit is a long-lived
   meta-task that owns the contract, spawns and tracks the per-repo
   children, receives their escalations, and can revise the contract and
   re-issue fixes to affected children. It is the single authority for
   cross-repo consistency.

## Decision

Introduce a **cross-repo change** capability built from five roles, all
dispatched and bounded by the orchestrator.

### 1. Scope selection (per run)

The operator chooses, per change, which repos are in play:

- **Full-search** — every registered project (ADR-029) is a candidate;
  investigation discovers which are actually affected.
- **Group** — a named set of repos (a small config affordance, e.g.
  `groups: { web-stack: [backend, web, mobile] }`).
- **Hand-picked / isolated** — the operator names the repos directly.

Scope bounds cost and blast radius before any agent runs. The registry is
the universe investigation draws from.

### 2. Investigation — read-only scouts, orchestrator-brokered

For each in-scope repo, the orchestrator dispatches a **read-only scout**:
an ephemeral agent with no git authority and no worktree mutation that
reads the repo's conventions, relevant code, and constraints, and returns a
**structured finding** (where the change lands, how this repo idiomatically
solves the shared problem, what it needs from the contract).

Critically, the coordinator agent does **not** spawn scouts itself. It
*requests* a scout fan-out through a tool the orchestrator brokers; the
orchestrator runs the scouts, enforces the concurrency and token-budget
caps, and returns the findings. This keeps ADR-013 intact (orchestrator
owns dispatch) and keeps the ADR-026 risk contained — investigation is
read-only and orchestrator-governed, never an agent shelling out its own
sub-agents.

### 3. Synthesis → the contract

Scouts gather per-repo; a single **synthesis** step sees *all* findings
together and proposes the contract — the cross-repo design judgment lives
here, because no single-repo scout can choose a coherent shared approach.
Synthesis is explicitly instructed to be **migration-aware**: prefer
backward-compatible, independently-mergeable changes (API versioning,
expand-then-contract, feature flags) so that no repo's work hard-depends on
another's *merge*. Hard cross-PR ordering is a planning smell to design
away, not a runtime feature. The output is the contract plus a proposed
per-repo task breakdown.

### 4. Decompose + gate

The coordinator emits a structured plan artifact — a `PLAN_COMPLETE`-style
marker (sibling to ADR-027's `SPEC_COMPLETE`) carrying the contract and the
per-repo task list. The orchestrator consumes it to fan out, but **only
after operator approval**. For a change that will start 15-20 mutating
agents, the gate is non-negotiable: the operator reviews the contract and
the breakdown before anything writes code.

### 5. The epic coordinator (persistent meta-task)

Approval creates an **epic**: a persistent meta-task that

- **owns the shared artifacts** (the contract and the plan) — they belong
  to no single repo, so the epic is their home;
- is **project-less**, which resolves the ADR-029 `tasks.project_id NOT
  NULL` constraint cleanly — the epic carries no project; each **child task
  keeps its single `project_id`** and runs in that project's worktree;
- **spawns and tracks** one child task per affected repo, each seeded with
  the contract as spec context (the ADR-027 pre-seeded-artifact handover
  mechanism), each producing its own PR;
- **answers "is the stack change done?"** by tracking its children to
  terminal;
- **receives escalations and re-issues.** When a child discovers mid-flight
  that the contract is wrong or unworkable, it escalates to the epic with
  its reasoning. The epic — as the single authority — revises the contract
  and re-issues updated work to the affected children. Because one
  coordinator owns consistency, this is a fan-in/fan-out loop, not
  distributed consensus.

The epic *is* the "orchestrator-managed background task" ADR-026 left
undecided — now scoped: it is a supervised, gated, operator-visible
coordinator, not an autonomous agent loop.

This epic-as-entity choice resolves three problems at once (shared-artifact
home, the project-less coordinator, and family tracking), which is why it
wins over flat linked tasks on technical grounds, not just UX.

### 6. Entry paths — fresh, or promote an existing task

An epic has two on-ramps:

- **Fresh** — from a multi-repo chat (ADR-034), as described above:
  investigate → contract → gate → fan out.
- **Promote-to-epic** — a single-repo task that turns out to be
  cross-cutting ("this touches the API the frontend and mobile consume") is
  grown into an epic mid-flight.

Promote-to-epic is **not** an ADR-027 process switch. ADR-027 changes a
task's *process* (same entity, new pipeline); this changes a task's *scope*
(one repo → many) and is a change of **kind** — a worker is project-bound
and does the code; an epic is project-less and coordinates. So a worker
cannot morph into an epic. Instead:

1. **Trigger + gate.** The agent emits a `NEEDS_EPIC: <reasoning>` marker
   (sibling to `SPEC_COMPLETE` / `PR_FIX_NEEDS_DECISION`), or the operator
   acts directly. Either way the operator approves the scope before fan-out,
   and picks which other repos come in (§1; the originating repo is
   automatically in scope).
2. **Create above, adopt as child #1.** The orchestrator creates a new
   project-less epic and **re-parents the originating task as its first
   child** — preserving its `project_id`, worktree, history, and any open
   PR. (The project-less-epic / single-project-child split from §5 is what
   makes this consistent with ADR-029's `project_id NOT NULL`.)
3. **Adoption re-evaluates the originating task.** Folding a task into an
   epic is active, not passive. Its findings/spec/in-progress design seed
   the contract synthesis (§3) — it knows the most about the change — but
   the resulting shared contract then flows back: **child #1 is re-evaluated
   against it and updated if it diverged**, through the same reconciliation
   / re-issue loop (§5) that governs every child. The task that triggered
   the epic is not exempt from the contract it helped shape. If its
   in-progress direction already fits, the update is a no-op; if it made a
   choice the coherent cross-repo design overrides, the re-evaluation
   rewrites that task's plan to match.

**Decision: adopt-and-reconcile**, not re-investigate-from-scratch.
Preserving the originating task's worktree and momentum (and feeding its
design into the contract) is worth the cost that, if it had already
committed to an incompatible approach, the re-evaluation asks it to
course-correct — possibly some rework. That rework is inherent to starting
small and growing, not a defect of the mechanism.

Feasibility note: this reuses the machinery §§1-5 already define (entity
split, investigate → contract → gate → re-issue). The genuinely new parts
are the `NEEDS_EPIC` trigger, re-parenting an *in-flight* task under a new
epic, and the adoption-time re-evaluation of child #1.

### Invariants preserved

- **Orchestrator owns dispatch + git** (ADR-013): scouts and child tasks are
  orchestrator-dispatched; the coordinator only *requests* via brokered
  tools; children get worktrees and PRs through the existing deterministic
  push path.
- **Per-project isolation** (ADR-029): children are ordinary single-project
  tasks; nothing about them is special except their seeded contract and
  their epic parent.
- **Atomic transitions / append-only audit** (Constitution §3.1, §1.5): the
  epic and its children use the same `atomic_transition` + message-log
  machinery; escalation and re-issue are auditable events.

## Consequences

### Positive

- Whole-stack changes become one coherent operation: investigate → agree a
  contract → fan out → track to done, instead of manual decomposition and
  context-copying across 15-20 tasks.
- Contract-first + migration-aware planning makes repos proceed in
  *parallel* against an agreed interface, not a serial merge chain.
- The persistent epic gives a real answer to "is the stack change done?"
  and a single place to absorb mid-flight divergence.
- Investigation is independently valuable: even stopping after Phase 1 (the
  synthesized contract + plan) gives the operator a cross-repo design they
  can execute by hand.

### Negative

- This is the largest new surface in CE: a coordinator role, a persistent
  epic entity, read-only scout agents, brokered fan-out, and shared
  artifacts. It earns its weight only at multi-repo scale.
- Cost and concurrency are real at full-search scope (N scouts, then N
  execution agents). Mitigated by scope selection, the approval gate, and
  orchestrator-enforced caps — but it must be governed deliberately.
- The re-issue loop adds coordinator state and re-entrancy that the flat
  task model never had; getting the escalation→revise→re-issue transitions
  right is the hard implementation work.

### Migration

New capability, additive. No change to existing single-repo tasks. The
epic is a new entity alongside `tasks`; children are ordinary tasks with an
epic reference. Pre-alpha, so no data migration concern for existing
installs.

## Implementation phases

1. **Investigation (read-only, standalone value).** Scope selection +
   orchestrator-brokered read-only scouts + synthesis → a contract + plan
   artifact. No mutation, no epic yet; the operator can hand-create tasks
   from the plan. Safe, bounded, proves the novel part.
2. **Epic + gated fan-out.** The epic entity (project-less, owns contract,
   tracks children), the `PLAN_COMPLETE` gate, and spawning children seeded
   with the contract. Family tracking to terminal.
3. **The re-issue loop.** Child→epic escalation, epic contract revision, and
   re-issue to affected children. The data model reserves for this from
   Phase 2; Phase 3 activates it.
4. **Promote-to-epic (§6).** The `NEEDS_EPIC` trigger, re-parenting an
   in-flight task as child #1, and adoption-time re-evaluation. Lands on top
   of Phases 2-3 (it needs the epic entity and the re-issue loop), so it's
   the last on-ramp wired even though it's the most natural way an epic gets
   created in practice.

## Out of scope (v1)

- **Cross-PR merge-gating / ordering enforcement.** Migration-aware planning
  is expected to make repos independently mergeable; a "hold all merges
  until the family is green" enforcement is a later phase. The epic model
  leaves room for it.
- **Auto-discovery relevance ranking** at full-search scope beyond what
  synthesis already filters.
- **Named repo groups** config is a small affordance that can land with or
  after Phase 1.
- A general **agent-spawns-agent** primitive (ADR-026 in full). This ADR
  only takes the read-only, orchestrator-brokered slice.
