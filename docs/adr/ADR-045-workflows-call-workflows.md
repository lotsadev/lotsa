# ADR-045 — Workflows call workflows

**Status:** Proposed — not implemented

**Scope:** CE

## Context

ADR-044 reframed the product around two primitives: **agents** (reusable,
process-independent prompt + declared properties + emittable outcomes) and
**workflows** (agents wired together by those outcomes). Routing moved from the
marker name onto the flow edge, and Phases 2/3 established the pattern of
*deriving* plumbing from declared properties rather than hand-wiring it.

The workflow half of that pair never got the same treatment. A workflow is
still a `process.yaml` containing one or more **flows**, where `main` is the
root and any sibling flow is a *sub-flow* reachable only through bespoke
dispatch wiring. That leaves three concrete problems.

**The `pr_fix` sub-flow is duplicated verbatim.** `build` and `fix` each
declare a `pr_fix` flow, and the two are parse-identical — same four steps,
same routes. Every job they reference is identical too, except the `review`
job's *default* routes, which both bindings override anyway. Two copies of the
same graph, guaranteed to drift.

**Sub-flows can't be extracted, because they reach into their caller.** The
duplication isn't accidental. `pr_fix`'s entry step routes
`SKIPPED: wait_for_pr_signal` — a step that lives in the *caller's* `main`
flow, not in `pr_fix`. A shared definition cannot name that, because it doesn't
know who called it. That single cross-flow edge is the coupling.

It barely works today, too. Generic cross-flow targets are not supported:
`flows.py:1312` documents that the drainer **special-cases** this one `SKIPPED`
route, and any other rule naming a sibling flow's job logs a warning and routes
to `blocked`.

**Sub-flows are not independently invocable.** A flow exists only inside its
parent process, so "watch this PR" cannot be pointed at a PR that Lotsa did not
create. `dispatch_sub_flow` already accepts an arbitrary `flow_name` and a
`target_job`, but `orchestrator.py:3914` rejects everything except `pr_fix`,
with a comment naming the missing work as *"Layer B, which will dispatch into
any named sub-flow."* This ADR is Layer B.

## Decision

**Drop the sub-flow concept entirely. Every workflow is top-level and
independently invocable. A workflow may *call* another workflow and wait for
its return.**

On return, the caller acts on the outcome, continues, or terminates. A "tail
call" is not a separate mechanism — it is a call whose caller has nothing after
it, which is why `chat → build` and `build → pr-monitor` are the same thing as
any other call.

### The call edge

A call target is **declared in YAML**, never chosen by an agent at runtime:

```yaml
# build — hands off to PR watching, automatically
routes: { COMPLETED: call pr-monitor }

# chat — starting real work needs a human
routes: { COMPLETED: call build }
gate: operator
```

`gate: operator` parks the task and waits for an explicit accept; without it
the call is automatic. This subsumes ADR-044 Phase 4c's hand-off as *"a call
edge that happens to be gated"* rather than a parallel mechanism.

Declared targets are the load-bearing choice. They keep the graph statically
analysable, so ADR-044 Phase 6's viewer can draw call edges, cycles are
rejected at build time, and an unknown target fails the build instead of a
task. An agent-chosen target (`AGENT_RESULT: CALL <workflow>`) would forfeit
all three and is explicitly rejected.

### Call state: a persisted stack

Caller and callee are **the same task** — same id, same branch, same worktree,
same audit trail. Only the active workflow changes. The task's single
`current_flow` metadata slot becomes a **call stack** whose top frame is the
active workflow.

The common path is already three deep — `chat → build → pr-monitor` — so this
is a real stack, not a single return slot.

### Termination propagates and can be caught

`terminate` is a reserved routing target that unwinds the stack. Each frame
either declares a route for it or lets it pass:

```yaml
# pr-monitor — merged; nothing left to do
routes: { COMPLETED: terminate }

# chat — catches the unwind rather than letting the task just end
routes: { terminate: chat_gate }
```

A frame with no `terminate` route propagates it upward; reaching the root with
no route ends the task. This is exception propagation expressed in the existing
`routes:` vocabulary — one reserved target, no new node type.

**Relationship to the existing `complete` target.** `flows.py` already reserves
`complete` (alongside `next`, `blocked`, `needs_input`). In a stacked world the
two are distinct and both are needed:

| Target | Meaning |
|---|---|
| `complete` | *this workflow* is done — pop one frame and return to the caller |
| `terminate` | *the task* is done — unwind every frame, subject to catches |

Today they coincide because the stack is always one deep, which is precisely why
the distinction has never had to exist. Migration must therefore be explicit
rather than mechanical: an existing `complete` target in a bundled or repo
workflow means "end the task" under the old model, and re-reading it as "return
to caller" changes behaviour for any workflow that gains a caller.

### Only merge or close may terminate

**Approval must not reach `terminate`.** ADR-030 is explicit that APPROVED is
not COMPLETE: reviewers and bots approve while leaving actionable feedback
inline, and only *merged* and *closed* are terminal. ADR-030 further guarantees
that an opened PR is watched until terminal regardless of task state.

If approval terminated and unwound the stack, both properties would be lost and
a comment arriving after approval would land on a dead task. Approval therefore
parks at an operator gate **inside** `pr-monitor`, which keeps watching. The
operator either accepts completion or sends it back for another round — a local
edge, no unwinding, PR still monitored throughout.

Re-entering a workflow at a named step (`call pr-monitor@pr-fix`) is what
`dispatch_sub_flow`'s already-reserved `target_job` parameter was left for.

### A terminal task may still talk, but may not call

Once a PR merges, the task's branch is merged and typically deleted — there is
nowhere for further commits to go. A follow-up build is therefore *necessarily*
a new task with a new branch. This is a structural fact, not a policy needing
enforcement.

But an operator asking "how do I test this?" should still get an answer. The
rule, derived from declared agent properties rather than naming `chat`:

> A terminal task may dispatch agents declaring `needs_worktree: false` **and**
> `produces_changes: false`. It may not call another workflow.

`chat` is currently the only agent satisfying both, which is exactly why it is
safe: it touches no git state on a task whose git state is finished. Starting
new work from a finished conversation spawns a **new task**, seeded through the
existing `promotion_inputs` / `draft_spec` channel.

The status model is untouched by this. `complete` stays genuinely terminal —
ADR-030's terminal CAS is global truth, and `CLAUDE.md`'s cross-cutting-refactor
rule warns that status semantics leak into CAS payloads, guards, and recovery
branches. Only *which agents may dispatch* on a terminal task is relaxed.

### `pr-monitor` becomes a workflow

`wait_for_pr_signal` moves out of `build`/`fix` and becomes `pr-monitor`'s first
step, closing the loop internally:

```
        ┌──▶ wait_for_pr_signal ──signal──┬──▶ pr-fix ──▶ review ──▶ push_pr ──┐
        │                                 ├──▶ resolve_conflicts ──────────────┤
        │                                 ├──▶ approved ──▶ operator_gate ─────┤
        │                                 └──▶ merged/closed ──▶ terminate     │
        └──────────────────────────────────────────────────────────────────────┘
```

Consequences of the move:

- Both duplicate `pr_fix` blocks are deleted.
- `SKIPPED: wait_for_pr_signal` becomes an **internal** edge. The drainer's
  cross-flow special case (`flows.py:1312`) and the cross-flow edge registrar
  are *removed*, not generalised.
- `build` and `fix` mains become symmetric — each ends at
  `push_pr → call pr-monitor`. Both reduce to "produce a PR."
- Pointing `pr-monitor` at an externally-created PR is simply a task that
  *starts* there, with a seeded `pr_number`.
- The name is now accurate: the component watches a PR and reacts, of which
  fixing is one branch. `pr_monitor.py` remains the engine that drives the
  monitor step; `pr-fix` remains the remediation agent.

### Metadata and variables are explicitly out of scope

A called workflow returns **an outcome only**, routed with today's `routes:`.
`promotion_inputs` is untouched and not generalised; artifacts
(`pr_description`, `draft_spec`, `handoff_suggestion`) remain the data channel
between workflows, as they are now. Nothing loses a capability it has today.

A metadata/variable layer — declared `inputs:`/`outputs:`, values bound into a
caller's scope, and **routing rules predicated on metadata** — is deferred to
its own ADR. Rules-over-metadata is the substantial half of that design and
deserves to be decided on its own terms rather than smuggled in here.

To keep that ADR *additive* rather than a migration, four constraints apply now:

1. **Stack frames are records, not strings.** Persist each frame as an object
   (`{workflow, step, called_from}`) even though a string suffices today, so a
   `vars` key is a pure addition with no schema change.
2. **Return is a structured result, not a bare enum.** Outcome-only in v1, but
   shaped so a payload slot appears beside it rather than changing the
   signature.
3. **Rule evaluation stays a seam.** Outcome matching resolves behind the same
   point a future metadata predicate would hook, so `when:`-style conditions
   slot in beside it instead of rewriting the router.
4. **Reserve the metadata namespace.** Frame-scoped variables will want a key
   on the task metadata JSON; name and reserve it now so nothing else takes it.

### Why not adopt an existing engine

This is the first question a reviewer will ask, and the self-hostable
dependency rule makes it legitimate — Temporal, Windmill and n8n all pass it.

The closest analogue is Temporal, not n8n: durable execution with a datastore as
the state of record is the hard part, and ADR-040 already built it here. What
none of them provide is the layer that *is* the product — orchestrator-owned git
(ADR-013), worktree-per-task, agents that never push, append-only audit,
CAS-guarded transitions (ADR-020), deterministic push. Those constraints are not
expressible as engine config; they would be rebuilt on top of any adopted
engine, which is most of the work, plus a new mandatory dependency in the
critical path.

The honest risk in the other direction is scope drift: a general workflow engine
is a deep, well-funded space, and Lotsa's differentiator was never the graph.
The mitigation is that this ADR's mechanism is scoped to two concrete drivers —
duplicated `pr_fix`, and `pr-monitor` needing to run standalone — and the
tempting generic layer (variables, expressions, conditionals) is deferred until
a real Lotsa workflow needs it. The signal that this went wrong is not having a
graph; it is the bundled catalog ceasing to be opinionated.

## Phasing

1. **Call/return + persisted stack** — the call edge, the stack, `terminate`
   propagation, and the `complete`-vs-`terminate` split. Extract `pr-monitor`;
   delete both duplicate `pr_fix` blocks; remove the cross-flow edge registrar
   and the drainer's `SKIPPED` special case. Resolves the duplication in full.
   Only `build`/`fix → pr-monitor` become calls in this phase, and both are
   automatic — `chat → build` keeps using ADR-027 promotion until Phase 2
   supplies the gate it needs. Phase 1 therefore ships no ungated call into a
   code-writing workflow.
2. **`gate: operator` on call edges** — reframe ADR-044 Phase 4c hand-off as a
   gated call; the `handoff_suggestion` artifact and its dashboard affordance
   become the gate's presentation.
3. **Standalone `pr-monitor`** — start a task directly in `pr-monitor` against
   an existing PR, including one Lotsa did not create.
4. **Viewer draws call edges** — extend `serialize_process_graph` so the
   ADR-044 Phase 6 board shows cross-workflow calls and gates.

Deferred to its own ADR: the metadata/variable layer and metadata-based routing
rules.

### Implementation notes (Phase 1)

- **Legacy `current_flow` rows are not migrated.** Phase 1 replaces the single
  `current_flow` metadata slot with a `call_stack`, but it does **not** convert
  rows persisted under the old slot onto a synthetic stack. Any task carrying a
  legacy `current_flow` slot (e.g. `"pr_fix"`, `"main"`) and no `call_stack`
  routes to `blocked` with the standard recovery message on the first surface
  that resolves its active workflow (`call_stack.is_legacy_flow_row`). This is a
  deliberate pre-alpha breaking change: reconstructing a faithful multi-frame
  stack for the new topology is user-visible churn we chose not to carry, and
  blocking is the honest, already-supported answer. The upshot is that
  restart-resume only ever reconstructs stacks *this* version wrote (which
  narrows the sharpest risk above), and the legacy contract is a single tested
  assertion (`test_legacy_current_flow_row_blocks_on_restart`) rather than a
  reconstruction path.
- **Changelog hint (for release notes).** Because in-flight tasks at upgrade
  time land in `blocked`, the eventual release notes must tell operators to
  **close or complete all in-flight tasks before upgrading**. Recorded here so
  it survives to the release: changelog authoring is out of this PR's scope, but
  the break is not.

## Consequences

### Positive
- One primitive. No sub-flows, no cross-flow edges, no special cases — a
  workflow is a workflow whatever invokes it.
- `pr_fix` exists once. The duplication that motivated this cannot recur,
  because there is no longer a construct that copies a graph.
- `pr-monitor` gains a capability it structurally could not have: adopting an
  externally-created PR.
- Removes code rather than adding a layer over it — the cross-flow registrar
  and the drainer special case both go.
- `handoff`, `promotion`, and the new automatic transfer collapse into one
  mechanism with different triggers, instead of three paths.

### Negative / risks
- **Restart-resume of a partially-unwound stack (ADR-040).** The stack is state
  of record and must be reconstructable from the DB, including a task
  interrupted mid-unwind. This is the sharpest implementation risk and needs
  explicit tests, not just idempotent steps.
- **Legacy rows.** Tasks persisted with a legacy `current_flow` slot (notably
  `current_flow="pr_fix"`, but also `"main"`) and no `call_stack` predate the
  stack. Per `CLAUDE.md`'s legacy-row contract, each surface comparing against
  flow names needs a defined answer. **Decided (Phase 1): route to `blocked`
  with the standard recovery message — no synthetic-stack migration.** See the
  *Implementation notes* below; this is a pre-alpha breaking change.
- **Cross-cutting rename.** `flow`/`sub-flow` appears in CAS payloads, audit
  fields, recovery branches, engine untrack checks, the viewer, and the API. Per
  `CLAUDE.md`, this sweep belongs in the implementation commit, not the review
  cycle.
- **Three ways to end a task.** `terminate`, ADR-043's `mark_complete`, and
  ADR-030's terminal PR signals now overlap. The implementation must state which
  the gate mechanism *replaces* versus sits beside, or operators will meet all
  three.
- **UI question left open.** A task showing `complete` while its `chat` frame is
  still answering questions needs an affordance for "done, still talking." That
  is a dashboard decision, but it is the first thing an operator will ask.

## Relationships

- **Amends/supersedes:** ADR-044 (workflow half of the agents/workflows pair;
  Phase 4c hand-off becomes a gated call edge), ADR-014 (flows as sub-graphs),
  ADR-021 (sub-flow dispatch — Layer B completes it).
- **Reuses/extends:** ADR-027 (promotion becomes the operator-gated call),
  ADR-030 (APPROVED-is-not-COMPLETE constrains where `terminate` is reachable),
  ADR-040 (the stack is state of record and must resume), ADR-020 (frame
  push/pop are CAS transitions), ADR-043 (`awaiting_operator` / `mark_complete`
  overlap to be reconciled).
- **Enables:** ADR-035 (cross-repo epic coordinator — a coordinator calling
  per-repo workflows is this mechanism), ADR-031 (runtime verification as a
  callable workflow).
- **Defers to a future ADR:** the metadata/variable layer and metadata-based
  routing rules.
