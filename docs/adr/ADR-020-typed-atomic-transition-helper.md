# ADR-020: A typed atomic-transition helper for CAS state changes

**Status**: Implemented
**Date**: 2026-05-29
**Implementation**: PR #102 — ships all three phases. Phase 1
(helper + types in `lotsa/db.py` + 11 high-priority CAS site
migrations) merged in PR #90; PR #102 completes Phase 2 (remaining
24 `claim_task_transition` migrations in `lotsa/orchestrator.py`,
plus the CI-enforcement test
`test_adr020_enforcement.py::test_no_claim_task_transition_outside_db`)
and Phase 3 (typed transition constants — `PushTransition`,
`PrFixTransition`, `PUSH_START`, `PUSH_SUCCESS`, `PR_FIX_CAP_FIRE`
in `lotsa/db.py`; push and pr-fix CAS sites unpack those
constants).
**Related**: CONSTITUTION.md §3.1 (Atomic transitions — the rule this helper enforces structurally), ADR-014 (jobs as unified primitive — the refactor whose bot-review findings prompted this ADR), ADR-019 (operator-acknowledged overrides — Commitment 5 modifies five CAS call sites and would directly benefit). Triggered by repeated CAS-related findings on PR #72 and the prior PR #68.

---

## Context

> "Typed" here means *structurally* typed — the helper's signature enforces that callers supply each transition coordinate as a separate keyword argument and that the success branch always gates side effects on `result.won`. The `from_state`/`to_state` *values* stay strings through Phases 1 and 2 because CE's state names come from user-authored flow YAMLs and aren't `Literal`-tractable; Literal-level enforcement arrives only in Phase 3 for the built-in state machines (pr-fix, push, monitor) whose state names are stable. Readers expecting Phase 1 to catch a typo'd ``"plannned"`` at type-check time will be disappointed — that protection is Phase 3.

`claim_task_transition` (`lotsa/db.py:207`) is Lotsa's compare-and-swap primitive for task state changes. It exists because Constitution §3.1 requires it: any state change with a precondition ("only advance if currently in state X") must be a single atomic `UPDATE … WHERE` with a check on the return value before any side effect. The pattern is correct.

The problem is **adoption discipline at scale**. There are **31 `claim_task_transition` call sites in `lotsa/orchestrator.py` alone**, plus more in `lotsa/pr_monitor.py` and `lotsa/db.py` itself. Each site is correct in isolation but each carries the same set of implicit invariants the prose-based rule in CONSTITUTION.md §3.1 demands:

1. Specify exactly the right `from_status` and `from_state` — typo any one and the CAS silently loses every time, with no compile-time signal.
2. Check the `won` return value before any side effect (audit message, event emission, counter increment, dispatch).
3. Decide audit semantics for the CAS-loss branch: silent, or write a "concurrent action lost" row?
4. Decide ordering between the CAS, the audit row, and any state-changing follow-up (counter increment, dispatch).
5. Get the active state machine right — root flow's SM or the sub-flow's SM, post-ADR-014?

These are five separate disciplines per site. With 31+ sites and an ongoing refactor (ADR-014) reshaping what valid `from_state` values even mean, the bug rate has been measurable.

### What's actually happened in this session

The bot review on PR #72 (ADR-014 Layer A) flagged CAS-related issues across multiple rounds. The repair commits tell the story:

- **`b974ea0`** (round 2): Medium #2 — `_dispatch_step` did its CAS pre-check against `self.flow.state_machine.transitions` (root flow) instead of the *active* flow's state machine. Worked accidentally because `_register_cross_flow_edges` stitched the missing edges in, but the invariant was implicit and would silently no-op for any future sub-flow step that lacked cross-flow rule targets.
- **`4230d2b`** (round 3): sub-flow entry edge wasn't registered in both the host and destination state machines, breaking the active-flow CAS pre-check from a different angle.
- **`5ef019c` and earlier**: cross-flow step lookup gaps in `revise()`, `answer()`, `send_message()`, `retry()`, `jump_to_step()` — each is a CAS call site whose `from_state` resolution depends on finding the right step in the right flow's catalog.
- **The pr-fix budget cap path** has its own CAS shape (`_pr_fix_round_cap_blocked` calls `claim_task_transition` to move the task to `blocked` when the cap fires) and the same five-discipline burden. ADR-019 Commitment 5 removes five of the six cap-check sites — each is a CAS site that today does the wrong thing for operator-initiated paths.
- **Dead variables that look load-bearing**: `self._dispatching_push: set[str] = set()` was declared in `OrchestratorService` but never used; the real guard was the CAS at line 931. A future maintainer reads the dead variable, assumes it's load-bearing, and either preserves it forever or removes it and accidentally drops the wrong guard.

The pattern across these isn't "people are sloppy." Each individual author made a sensible CAS call. The pattern is that **a 5-parameter call with 4 implicit ordering rules and a typed-state-machine dependency is mistake-prone at 31+ sites, especially during a refactor**.

Intended outcome: a typed helper that encapsulates the full "CAS + audit row + on-win side-effect contract" so the five disciplines are enforced by the type system and one helper signature, not by 31 hand-maintained call sites. The Constitution rule stays the same; what changes is that the rule is structurally enforceable, not just prose-encoded.

---

## Decision

Introduce `db.atomic_transition` (`lotsa/db.py`) as a typed wrapper that encapsulates:

1. The CAS itself (delegates to today's `claim_task_transition`).
2. An audit row written atomically with the CAS via the same SQLite transaction — so an `atomic_transition` either writes both the state change AND the audit row, or neither. There is no "wrote the audit, lost the CAS" or vice versa.
3. A typed result that structurally separates `won` from raw rowcount.
4. An explicit audit-on-loss policy so the contract for CAS-loss is uniform per site, not silently varying.

### Public signature

```python
@dataclass
class TransitionResult:
    won: bool
    rowcount: int            # raw — almost never inspected; won is the contract

class AuditPolicy(Enum):
    SILENT = "silent"        # CAS-loss writes nothing. Default for races between
                             # equivalent actors (e.g. two operator clicks).
    LOG_LOSS = "log_loss"    # CAS-loss writes a system message recording the
                             # attempted transition coordinates. Use when
                             # diagnosing "what tried to advance but lost"
                             # matters. CE has no named-actor concept today; if
                             # a future ADR adds one, the helper can grow an
                             # optional ``actor_id`` parameter and include it
                             # in the log-loss row.

# Synthesised row when ``AuditPolicy.LOG_LOSS`` fires. The helper writes this
# row in the same transaction as the (no-op) UPDATE — the row is durable
# even though the CAS lost. Fields:
#
#   role       = "system"
#   step_name  = None  (the loser wasn't running a step — it was attempting
#                       a state transition)
#   msg_type   = "cas_loss"
#   content    = f"CAS lost: {from_status}/{from_state} → {to_status}/{to_state}"
#   metadata   = {
#       "from_status": from_status,
#       "from_state": from_state,
#       "to_status": to_status,
#       "to_state": to_state,
#       "to_current_step": to_current_step,
#   }
#
# Implementers should not vary this shape — uniformity is the point of
# defining it here. Future fields (e.g. ``actor_id``) extend metadata.

@dataclass
class AuditRow:
    role: Literal["system", "user", "agent", "github"]
    step_name: str | None                    # ``None`` for transitions not associated with a step
                                             # (top-level status changes, restart recovery, etc.).
                                             # The helper persists ``None`` as empty string in the
                                             # SQLite column; that's an internal detail callers
                                             # don't reproduce.
    content: str
    msg_type: str                            # e.g. "status_change", "stage_transition", "pr_decision"
    metadata: dict[str, Any] = field(default_factory=dict)

async def atomic_transition(
    self,
    task_id: str,
    *,
    from_status: TaskStatusLiteral,
    from_state: str,
    to_status: TaskStatusLiteral,
    to_state: str,
    to_current_step: str | None,
    audit_on_win: AuditRow | None,
    audit_on_loss: AuditPolicy = AuditPolicy.SILENT,
) -> TransitionResult:
    ...
```

The audit row, when supplied, is written in the same transaction as the state-mutation `UPDATE`. SQLite supports this via explicit `BEGIN`/`COMMIT` around the two statements. The current `TaskDB._open` already uses `isolation_level=None` (autocommit), so the helper opens a single explicit transaction around the CAS-and-audit pair for the duration of the call.

### Call-site shape (the contract this helper enforces)

```python
result = await self.db.atomic_transition(
    task_id,
    from_status="waiting",
    from_state="planned",
    to_status="working",
    to_state="testing",
    to_current_step="test",
    audit_on_win=AuditRow(
        role="system",
        step_name=None,
        content="✓ plan approved — test started",
        msg_type="stage_transition",
    ),
)
if not result.won:
    return

# Side effects after this point are guaranteed to follow a successful
# atomic state change + audit write.
await self._merge_task_metadata(item, {"last_run_step": "test"})
await self._dispatch_step(...)
```

What this enforces by construction:

- **The audit row cannot get out of sync with the state change.** Either both lands or neither does. Today the audit row is a separate `add_message` call after the CAS, and if anything fails between them (rare but possible) the audit trail drifts from reality.
- **`won` must be checked before any code after the call.** The current `won: bool` return is easy to use correctly but also easy to miss. Wrapping in a `TransitionResult` makes the check explicit (the lint rule "any unchecked `TransitionResult` is a bug" is tractable; "any unchecked bool" is not).
- **CAS-loss audit semantics are explicit.** `audit_on_loss=AuditPolicy.SILENT` is the default and matches today's behaviour. Sites that want to record the loss (e.g. for diagnosing concurrent operator clicks) opt in via `AuditPolicy.LOG_LOSS`.
- **The full transition is one call, not three.** Today a typical site does: CAS → check `won` → `add_message` for the audit row → optional `_merge_task_metadata`. Three calls, three failure modes, three places to forget. One helper, one failure mode.

### Typed state transitions (optional second layer)

`from_status` and `from_state` are still strings in the signature above because the set of valid state names in CE is not yet `Literal`-tractable — flow YAMLs are user-authored and add their own state names. But the helper makes it possible to layer typed transition shapes on top per *built-in* state machine:

```python
@dataclass(frozen=True)
class PrFixTransition:
    """Built-in transitions for the pr-fix cap mechanism."""
    name: str
    from_: tuple[TaskStatusLiteral, str]
    to_: tuple[TaskStatusLiteral, str, str | None]

    def kwargs(self) -> dict[str, Any]:
        """Expand into the kwargs ``atomic_transition`` expects.

        Field names like ``from_`` don't match the helper's parameter
        names (``from_status``, ``from_state``, ...); this method does
        the structural unpacking once per transition so call sites
        keep the typed source and the helper keeps its flat kwargs.
        """
        from_status, from_state = self.from_
        to_status, to_state, to_current_step = self.to_
        return {
            "from_status": from_status,
            "from_state": from_state,
            "to_status": to_status,
            "to_state": to_state,
            "to_current_step": to_current_step,
        }

PR_FIX_CAP_FIRE = PrFixTransition(
    name="pr_fix_cap_fire",
    from_=("working", "pr-fixing"),
    to_=("blocked", "blocked", "pr-fix"),
)

# Usage:
result = await self.db.atomic_transition(
    task_id,
    **PR_FIX_CAP_FIRE.kwargs(),  # expands to typed from_/to_ kwargs
    audit_on_win=AuditRow(
        role="agent",
        step_name="pr-fix",
        content=cap_reasoning,
        msg_type="pr_decision",
        metadata={"decision": "blocked", "round": round_n, ...},
    ),
)
```

A `dataclass` rather than `NamedTuple` because the call-site contract is "the transition object exposes a `kwargs()` method that produces helper-compatible kwargs" — NamedTuple's `_asdict()` would expose `name`/`from_`/`to_` directly, which don't match `atomic_transition`'s parameter names. The dataclass owns the unpacking once so every built-in transition stays a single declaration.

This layer is **optional and additive**. It catches typos in the transition pairs for the built-in flows (pr-fix, push, monitor) where the state names are stable. Sites that work with operator-defined flow YAMLs continue to pass raw strings. The typed-transition layer becomes most valuable on the highest-traffic CAS shapes — cap-fire, monitor-driven dispatch, push retry — where the same `(from, to)` pair recurs across many sites today.

### Migration policy

`claim_task_transition` is **not removed**. It stays as the underlying primitive. `atomic_transition` is layered on top and migrates call sites one at a time. After migration converges, `claim_task_transition` becomes an internal implementation detail of `atomic_transition` and stops being called from outside `db.py`. A lint rule (or a CI grep check) enforces this once migration is complete.

Phasing:

- **Phase 1** (this ADR's implementation PR): land `atomic_transition` + `AuditRow` + `AuditPolicy` + tests. Migrate the 6 pr-fix cap-fire sites (the highest-bug-density cluster) and the 5 main dispatch sites (`_dispatch_step`, `_dispatch_action`, `_dispatch_next_step`, push retry, monitor dispatch). ~11 sites, ~150-200 LOC of net change.
- **Phase 2** (separate PR): migrate the remaining ~20+ sites across action methods (approve, retry, revise, answer, send_message, jump_to_step, transition_task) and PR monitor paths.
- **Phase 3** (separate PR): add typed transition shapes for the built-in state machines and enforce them at call sites; deprecate raw `claim_task_transition` calls outside `db.py`.

Phases land in order, each independently shippable. Phase 1 is the inflection point — once it's in, the contract is in the codebase and the migration is a mechanical lift.

---

## Why this isn't a niche concern

Every backend with concurrent operator+system actors hits the same shape. The Constitution rule (atomic UPDATE + check return + ordered side effects) is correct in every codebase that has it; the discipline burden is the same. The standard patterns to reduce it:

- **Job queues** (Sidekiq, Celery, Oban) wrap state transitions in queue-driven primitives that enforce a single-actor model per job. Lotsa can't do this — operator actions and engine actions both legitimately race.
- **Actor models** (Erlang, Akka) enforce single-actor message handling per entity. Lotsa's architecture doesn't fit cleanly — operators are external to the actor system.
- **Workflow engines** (Temporal, Cadence, Step Functions) own state transitions explicitly via their state-machine DSL. Lotsa is itself a workflow engine for governed AI agents — the CAS pattern is what implements its state machine; we can't outsource it.
- **Typed state machines** (the rust-side `typestate` pattern; Java's `Enum` state machines) catch invalid transitions at compile time. Lotsa's flows are user-authored YAML — the state-name space isn't closed.

Lotsa's situation is unusual enough that none of the off-the-shelf patterns drop in. What's left is to make the CAS pattern *less mistake-prone per call site* via a typed helper. That's a smaller intervention than any of the above and stays within the existing architecture. The Constitution rule doesn't change — its enforcement does.

The pattern matters more as Lotsa grows. Today there are 31+ CAS sites in one file. ADR-014 multi-process support and the future engines beyond `pr_monitor` will add more. Without structural enforcement, the bug rate scales with site count.

---

## Tradeoffs

**Pros:**

- **One pattern, one call.** Audit row + state change become a single atomic operation; the "audit drift" failure mode disappears by construction.
- **Explicit audit-on-loss policy.** Today CAS-loss semantics vary across sites (mostly silent, sometimes a "concurrent action lost" row, sometimes neither). The enum makes the choice deliberate per call.
- **`won` check becomes structurally enforceable.** An unchecked `TransitionResult` is a one-rule lint check; an unchecked `bool` isn't.
- **Migration is incremental.** Each phase ships independently; no big-bang refactor that re-validates 31 sites at once.
- **The Constitution rule stays.** §3.1 is the canonical statement of the invariant; the helper is how the codebase implements compliance.
- **Bug-prone shapes get typed.** Phase 3's typed transition shapes catch typos in `from_state` / `to_state` for the built-in state machines, the highest-traffic call sites.

**Cons:**

- **31+ call sites to migrate.** Each touches a working code path. The migration is mechanical but it's not free — every site needs a test that the audit row is now atomic with the state change.
- **Coupling audit and state-change in one transaction.** Today they're separate. A bug in the audit-row write would now also block the state change. Mitigated by the audit row being a simple INSERT with no side effects, but it's worth noting.
- **A new abstraction surface.** Implementers reading the codebase have to learn `atomic_transition` in addition to `claim_task_transition`. The migration window has both APIs visible.
- **Not all CAS shapes fit the helper.** Sites that don't write an audit row (e.g. claim-only operations) get a marginal benefit from the typed result but pay the cost of using the helper. Worth keeping `claim_task_transition` available for these.
- **The typed transition layer (Phase 3) doesn't help operator-authored YAML flows.** Their states are still free strings. The Phase 3 typing benefits the built-in state machines; YAML-authored flows still rely on the runtime check in `claim_task_transition`.
- **SQLite explicit transaction handling.** Adds a `BEGIN`/`COMMIT` per call. Performance impact negligible at the low concurrency Lotsa runs at.

---

## Backward compatibility

- **No DB schema change.** `atomic_transition` uses the existing `tasks` and `messages` tables exactly as `claim_task_transition` + `add_message` do today.
- **`claim_task_transition` stays as the primitive.** Existing call sites continue working unchanged until migrated. The two APIs coexist during the migration window.
- **No flow YAML change.** State names and transitions in `process.yaml` files stay exactly as authored.
- **No external API change.** The new helper is internal to `lotsa/db.py` and `lotsa/orchestrator.py`. Endpoints, dashboard, and audit-trail format are unaffected.
- **Audit trail consumers** (chat panel, message-log readers, future analytics) see the same row format. The change is *when* the row gets written (now atomic with the state change), not *what* gets written.

---

## Scope

This ADR commits to:

1. The `atomic_transition` API (signature, transaction semantics, `AuditRow` + `AuditPolicy` shapes).
2. The migration policy (claim_task_transition stays; helper layers on top; one-phase-at-a-time migration).
3. Phase 1 in this ADR's implementation PR: land the helper + migrate ~11 highest-bug-density call sites (6 pr-fix cap-fire sites + 5 main dispatch sites). ~150-200 LOC.
4. The typed transition shape pattern (Phase 3) as a design direction; the concrete typed shapes for pr-fix, push, monitor are deferred to Phase 3's PR.

In scope for **Lotsa Community Edition** (`lotsa/` + `rigg/`).

---

## Out of scope

- **Phase 2 and Phase 3 migration PRs.** Each is its own change, planned in this ADR but executed in follow-up PRs once Phase 1's helper is in the codebase.
- **A lint rule enforcing "no `claim_task_transition` outside `db.py`".** Becomes useful after Phase 2; worth a small follow-up commit then.
- **Property-based concurrency tests** (hypothesis-style generators that produce interleaved operator+system actions to verify invariants). A complementary mechanism for the same goal of "make CAS-shape bugs impossible." Probably an ADR-021 candidate once this helper is in.
- **Migrating to a typed state-machine DSL** (Temporal, Cadence, custom Lotsa DSL). A much bigger architectural change. ADR-020 is the small intervention that stays inside the existing model; a DSL migration would supersede it.
- **Operator-identity audit fields.** Today the `role="user"` audit row doesn't identify *which* operator. Out of scope here; depends on Lotsa CE eventually growing a named-operator concept (mentioned in ADR-019's Out of scope as well).
