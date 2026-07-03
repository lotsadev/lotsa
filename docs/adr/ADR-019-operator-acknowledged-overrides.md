# ADR-019: Operator-acknowledged overrides for Lotsa guard conditions

**Status**: Implemented (revised 2026-06-16 — override now resumes; revised 2026-07-02 — reason field removed; see Revision notes)
**Date**: 2026-05-26
**Related**: ADR-014 (jobs as unified primitive — establishes the action-vocabulary the new override action joins), ADR-015 (merge-conflict handling — formerly a planned instantiation; its 2026-06-12 revision escalates via `NEEDS_INPUT` instead, see Out of scope), ADR-017 (soft timeout indicator — a second future instantiation), PR #72 / task `<redacted>` (the prompting incident).

---

## Revision note (2026-07-02)

The operator-**reason** coupling is removed. The original design (Commitment 2
step 2, Commitment 4) let the operator type an optional free-text reason that
was appended to the `overridden` decision row (`"Operator acknowledged budget
cap — <reason>"`) and surfaced in the UI as a collapsed "Add reason (optional)"
textarea next to the override button.

In practice this was a confusing two-field pattern: one action button sitting
next to an optional free-text field whose only effect was an audit-row suffix.
This revision drops it:

- The override is now a **bare reset-and-resume**. Clicking "Acknowledge &
  continue" resets the guard counters and resumes the step in one action, with
  no intermediate reason entry.
- The `overridden` audit row content is always the bare `"Operator
  acknowledged budget cap"` — no `" — <reason>"` suffix.
- The `reason`/`operator_reason` parameter is removed end-to-end: the
  `OverrideHandler.acknowledge` protocol, the `pr_fix_budget` handler,
  `OrchestratorService.acknowledge_override`, the
  `POST /acknowledge-override` request body, and the frontend
  `acknowledgeOverride` client all drop it.
- Operators who want to record *why* they overrode a guard type a normal chat
  message (a `user` audit row). The tight coupling of the reason to the
  `overridden` decision row is intentionally dropped — audit-via-chat is
  accepted as sufficient for this version of the product.

This changes, not preserves, this ADR's original Commitment 2 (reason appended
to the decision row) and Commitment 4 (the reason field). The sections below
are annotated to point here.

---

## Revision note (2026-06-16)

The original implementation **decoupled** the override into two operator
clicks: "Acknowledge" reset the guard counters and wrote the audit row, then
a separate "Retry" re-dispatched. The rationale was audit tidiness — one
action, one decision, one row.

In practice it was a UX trap. The button was even labelled *"Acknowledge &
continue,"* but clicking it reset the counter and **did nothing visible** —
the task stayed `blocked`. Operators reasonably concluded the override hadn't
worked and asked "do I need to hit Retry too?" (yes — but the separate Retry
had its own failure modes). Three tasks (`<redacted>`, `<redacted>`, `<redacted>`)
stranded on this confusion in one week.

The decouple also contradicted this ADR's own **stated intent** (Context: *"a
single in-Lotsa action — 'Acknowledge & continue' — that the operator clicks
to clear the block"*). This revision restores that intent:

- `acknowledge_override` now **resumes the blocked step** in the same action
  (re-dispatch via `retry()`; for pr-fix, re-fetches PR feedback per #145).
- The audit trail still records **two rows** — the `overridden` pr_decision
  and the `Retrying` row — so override and dispatch stay independently
  auditable. Only the click count collapses from two to one.
- The dashboard hides the bare **Retry** button when an override is available
  (the override's "Acknowledge & continue" now covers both); Retry remains for
  plain blocks (crash, sync failure, agent error).

---

## Context

Lotsa has guard conditions that fire to invoke human judgment. The `max_pr_fix_rounds` budget cap is the canonical example today: when a task hits `10/10` rounds without the pr-fix loop converging, the cap writes a `pr_decision` audit row with `decision="blocked"`, transitions the task to `status="blocked"`, and surfaces "PR-fix budget exhausted. Human review required." in the chat. The cap is doing its job — it stops the loop and demands a human look.

The pattern recurs across the codebase:

- **ADR-015** originally sketched a `merge_conflict` blocked state for conflicts an automated `git merge origin/main` produced during a pre-pr-fix sync. Its 2026-06-12 revision dropped the manual-resolution state entirely: the `resolve_conflicts` agent step escalates judgment calls via `NEEDS_INPUT` and the operator answers in chat — humans never edit the worktree, so there is no guard state left to override.
- **ADR-017** (in flight on PR #67) introduces a soft-timeout indicator on tasks that have been running suspiciously long. The dashboard surfaces a "Looks idle" badge once the warn threshold is crossed.
- **Future guards** will fire on conditions we haven't named yet — third-party engine cap-fires, anything an operator might need to acknowledge.

The gap, common to every one of these: **Lotsa raises the issue, but the operator has no in-app affordance to express their decision.** Today, when the pr-fix cap fires, the operator's options are:

1. Edit the SQLite DB by hand: `UPDATE tasks SET metadata = json_set(metadata, '$.pr_fix_round_count', 0)…`. Works, but it's under-the-hood surgery — not auditable as a decision, no record of who did it or why.
2. Edit `process.yaml` and restart the server. Changes the rule globally for every task and every future task — wrong granularity for a per-task decision.
3. Bypass Lotsa and finish the work by hand. Loses the loop's audit trail entirely.

All three are operator-hostile in the framing ADR-014 set: Lotsa is positioned to host non-engineering processes (research, ops, content), with operators who don't have SQL or YAML reflexes. The "Human review required" message is correct intent and broken UX — it asks for a decision the dashboard provides no way to act on.

The concrete trigger for this ADR: task `<redacted>` (PR #72, ADR-014 Layer A implementation) hit the cap on 2026-05-26 after 9 rounds of legitimate fixes. The bot reviewer had already said "APPROVED WITH COMMENTS" on the May 25 review pass. The right operator move was "I've reviewed the situation, override the cap and let it continue." Lotsa offered no UI for it; the operator (an engineer with DB access) ran the SQL update by hand. A non-engineer operator would have been stuck.

Intended outcome: a single in-Lotsa action — "Acknowledge & continue" — that the operator clicks to clear the block. The action produces a structured audit row recording the override decision. The principle generalises so future guards (timeout dismiss, conflict acknowledgement, anything Lotsa adds later) inherit the same UX and audit shape rather than each reinventing the operator surface.

---

## Decision

**Principle.** When Lotsa raises a guard that requires human judgment, the operator expresses that judgment via a single audited action in the dashboard. The guard's *detection mechanism* does not change; the operator's *response to it* becomes first-class UI with a first-class audit row.

**Corollary.** Autonomous-action budget caps apply *only* to autonomous actions. Operator-initiated paths (answering a question, sending a message, revising, retrying, jumping) represent explicit human supervision and bypass the cap by design — caps protect against runaway loops, not against operator dialogue.

Five concrete commitments.

### 1. Guard-override registry

A new abstraction — a small module (`lotsa/overrides.py`) — defines an `OverrideHandler` protocol:

```python
class OverrideHandler(Protocol):
    guard_name: str                # stable identifier, snake_case
    label: str                     # button label in the UI
    description: str               # one-line explanation for hover/expand

    async def detect(self, task: TaskRow, db: TaskDB) -> bool:
        """Is this task currently blocked by this guard?"""

    async def acknowledge(
        self,
        task: TaskRow,
        db: TaskDB,
    ) -> None:
        """Clear the block. Write the audit row. No state-transition
        side effects beyond what is necessary to clear the specific
        block this guard imposes."""
        # Note: the ``operator_reason`` parameter was removed 2026-07-02;
        # see the 2026-07-02 Revision note.
```

A module-level `_handlers: dict[str, OverrideHandler] = {}` plus `register_override(handler)` / `get_override(name)` / `list_available_for(task, db)` give the orchestrator and the API layer a uniform lookup. `list_available_for` iterates registered handlers, calls each one's `detect(task, db)`, and returns the matches — so the `db: TaskDB` argument has to flow through to make `detect()` callable.

This mirrors the tool / engine registry pattern from ADR-014. Built-in handlers register at module import; third-party guards (custom engines) register at startup the same way.

### 2. First instantiation — pr-fix budget override

A built-in handler with `guard_name = "pr_fix_budget"`:

- **Detect**: the task's most recent `pr_decision` audit row has `decision="blocked"` AND `reasoning` starts with the cap-fire phrase Lotsa emits today (`"PR-fix budget exhausted"`). Substring match is acceptable for the first version because the phrase is emitted at exactly one site (`lotsa/orchestrator.py:2682`). When a follow-up adds a structured `block_reason` field to the audit row, the handler switches to the structural check.
- **Acknowledge**:
  1. Reset `task.metadata.pr_fix_round_count` to `0` via a read-merge-write against the DB directly: `fresh = await db.get_task(task_id)` → mutate `fresh.metadata["pr_fix_round_count"] = 0` → `await db.update_task(task_id, metadata=fresh.metadata)`. The read-then-write is safe here because acknowledgement only fires on a `status="blocked"` task, and the orchestrator only writes metadata from drainer callbacks for tasks it has just dispatched (which `_in_flight` prevents during blocked status). The existing `_merge_task_metadata` helper takes an `Item` rather than a `task_id`, so it's not directly callable from the handler protocol — inlining the read-merge-write keeps the handler signature `(task: TaskRow, db: TaskDB)` clean.
  2. Write a `pr_decision` audit row via `db.add_message`:
     - `role="user"` (operator action — consistent with how `feedback`, `answer`, `chat` are recorded)
     - `step_name="pr-fix"`
     - `type="pr_decision"` (same shape as the cap-fire row at `lotsa/orchestrator.py:2806`)
     - `content="Operator acknowledged budget cap"` (bare — the reason suffix was removed 2026-07-02; see the Revision note)
     - `metadata = {"decision": "overridden", "round": <round_at_which_cap_fired>, "triggering_comment_ids": [], "commit_sha": None, "duration_ms": None, "cost_usd": None}`
  3. **Resumes the blocked step in the same action** (revised 2026-06-16). After the reset + audit row, `acknowledge_override` re-dispatches via the normal `retry()` path (for pr-fix, this re-fetches PR feedback per #145). The audit trail still records two distinct rows — the `overridden` pr_decision here and the `Retrying` row from the resume — so override and dispatch remain separately auditable; only the operator's click count collapses to one. See the Revision note for why the original decouple-into-two-clicks design was reversed.

### 3. New API endpoint

A new action endpoint joining the existing family in `lotsa/server/api_routes.py` (the pattern at lines 216-282):

```
POST /api/tasks/{task_id}/acknowledge-override
Request:  {"guard_name": str}
Response: TaskDetailFullResponse
```

(The request body originally carried an optional `"reason": str | None`; it was
removed 2026-07-02 — see the Revision note.)

Status guard: HTTP 400 via a new `AcknowledgeOverrideNotAllowed` exception (parallel to `ApproveNotAllowed` / `RetryNotAllowed` defined at `lotsa/orchestrator.py:43-56`), raised when the named guard's `detect()` returns False for this task — preventing the operator from triggering an override action that doesn't apply. The same exception (and same 400 response) covers the case where `guard_name` is not in the registry at all; treating both as "this override isn't applicable to this task" keeps the client-side error handling uniform and avoids leaking registry contents through differentiated status codes.

The endpoint calls a new `OrchestratorService.acknowledge_override(task_id, guard_name)` that looks up the handler in the registry and invokes its `acknowledge()`. (Originally `acknowledge_override(task_id, guard_name, reason)`; the `reason` parameter was removed 2026-07-02 — see the Revision note.) Standard request body model added to `lotsa/server/api_routes.py` (next to `FeedbackRequest`, `AnswerRequest`, etc. — sibling request models at lines 45-59).

The response shape — `TaskDetailFullResponse` — is the existing uniform action response. Frontend re-fetches the task detail on success and the new audit row appears in the chat immediately, same way `answer` and `feedback` updates appear today.

### 4. Dashboard UI

The chat input panel (`lotsa/frontend/src/components/chat/chat-input.tsx`) is the existing action surface — it's where Approve, Retry, Send, and Answer all live today. Override actions join it.

Concretely:

- **Backend** enriches `TaskDetailFullResponse` (or `TaskSummaryResponse`, depending on cost) with an `available_overrides: list[AvailableOverride]` field, where `AvailableOverride = {guard_name: str, label: str, description: str}`. Populated by iterating registered handlers and calling each one's `detect()`. For most tasks, the list is empty (zero-cost).
- **Frontend**: when `available_overrides` is non-empty, render one button per override in `ChatInput`'s action button row; the bare **Retry** is hidden in that case (the override's "Acknowledge & continue" now resets *and* resumes — revised 2026-06-16). Retry still renders for plain blocks with no override. Default label: "Acknowledge & continue" (or the handler's custom `label`).
- **Reason field**: ~~an inline expandable textarea — collapsed by default with a "Add reason (optional)" affordance.~~ **Removed 2026-07-02** (see the Revision note). The override is a single bare button with no textarea; the override request carries only the guard name. Operators who want to record rationale type a normal chat message (a `user` audit row) — the chat input remains available on a blocked task.
- **On success**: standard React Query invalidation pattern at `chat-input.tsx:24` — the task detail refetches, the new `pr_decision` row is visible in the chat alongside the original cap-fire row, the override button disappears (because `detect()` now returns False for this guard).

### 5. Operator-initiated paths bypass autonomous-action budget caps

The `max_pr_fix_rounds` cap exists to limit *autonomous* re-dispatch loops where no human is supervising each round. Today the cap check fires uniformly at all six call sites in `lotsa/orchestrator.py`:

| Line | Call site | Initiated by | Cap check today | Cap check under this ADR |
|------|-----------|--------------|-----------------|--------------------------|
| 764  | `revise()` | operator revising | applies | **removed** |
| 875  | `answer()` | operator answering NEEDS_DECISION | applies | **removed** |
| 946  | `send_message()` | operator chat | applies | **removed** |
| 1104 | `retry()` | operator clicked Retry | applies | **removed** |
| 1213 | `jump_to_step("pr-fix")` | operator manual jump | applies | **removed** |
| 1443 | `_dispatch_pr_fix_locked` | PR monitor (autonomous) | applies | **stays** |

The bug this fixes: today, an operator answering a `PR_FIX_NEEDS_DECISION:` question when the task has previously hit the cap gets their answer silently rejected by the cap check — the very dialogue the agent requested can't be delivered. Same for sending a message or revising. The cap was never intended to gate operator dialogue; it's there to limit autonomous loops.

**Concretely**:

- The five operator-initiated cap checks are removed from `lotsa/orchestrator.py`.
- `_dispatch_pr_fix_locked` (the one autonomous site) keeps its cap check unchanged. This is the path the cap is designed to limit.
- The `pr_fix_round_count` metadata field still **increments** on every dispatch regardless of source. Counting all dispatches preserves audit-trail completeness — operators reading the message history can still see how many total rounds occurred. The cap *enforcement* is what's scoped to autonomous dispatches.
- The override action from Commitment 2 is still the right tool when an *autonomous* cap fire blocks the loop and the operator wants to give it more headroom. Commitment 5 is the complementary direction: even before any acknowledgement, operator-initiated paths never get caught by the cap to begin with.

**This generalises**: any future "autonomous action budget" cap (agent retry limit, tool-call budget, recursion ceiling) applies only to autonomous dispatches. Operator-initiated paths are by definition supervised and don't need the same protection. The Corollary above is the rule; this commitment is the first instantiation.

---

## Why this isn't a niche concern

Every agent-orchestration platform that hosts non-engineer operators eventually surfaces guard conditions whose right resolution is "human judgment + override," not "kill the process" and not "raise the limit globally." The shape recurs:

- **LangGraph** / **AutoGen** / **CrewAI** all let workflow authors specify per-step retry limits, recursion depth caps, budget ceilings. None ship a generic operator-acknowledge UI; each integration builds its own surface.
- **Microsoft Copilot agent mode** and **Cursor** treat cap conditions as terminal failures, recoverable only by re-running from scratch.
- **AWS Step Functions** has a `human approval` task type — but it's a *pre-approval* gate, not a post-hoc override of a fired guard.

The missing piece across these systems is: "Lotsa detected a condition. Lotsa reported it. The operator made a decision. The decision is recorded as a discrete event in the audit trail." That's the gap ADR-019 closes for Lotsa.

The pattern matters more as Lotsa broadens beyond engineering: a research process's guard ("model returned no citations after 5 retries") needs the same operator-decision surface as a software process's pr-fix cap. Building it once, with one registry and one UI, is the difference between Lotsa being a tool for engineers and a tool for anyone running governed AI processes.

---

## Tradeoffs

**Pros:**

- **One pattern, many guards.** ADR-017's timeout dismiss and any future Lotsa guard inherit the same UI surface and audit shape. Each handler is ~30 LOC + a registration line.
- **First-class audit trail.** The override is its own `pr_decision` row, recording the round it overrode. Reviewers reading the message history see the cap fire, the override, and the subsequent retry as three distinct events. (The row originally also preserved an operator-supplied reason; that coupling was removed 2026-07-02 — rationale, when the operator wants it, is now a normal chat message. See the Revision note.)
- **Reuses existing patterns.** The action endpoint, the request schema shape, the exception-on-precondition-failure, the React Query invalidation pattern — all already established. Nothing new to learn for an implementer.
- **Doesn't change guard semantics.** The cap still fires the same way at the same thresholds. The override is purely a *response* mechanism. Operators who don't want to override (or who set `max_pr_fix_rounds=0` to disable the cap globally) see no behaviour change.
- **One action, two audit rows** (revised 2026-06-16). "Acknowledge & continue" resets the guard *and* resumes the blocked step in a single operator click — matching this ADR's own stated intent (Context: "a single in-Lotsa action … that the operator clicks to clear the block"). The audit trail still carries both the `overridden` row and the `Retrying` row, so the two events stay independently explainable; the operator just doesn't perform two clicks for one decision. (The original design decoupled them into Acknowledge-then-Retry for audit tidiness; in practice that read as "I acknowledged and nothing happened," and three separate tasks stranded on it — see the Revision note.)

**Cons:**

- **A new abstraction surface.** `OverrideHandler` is a contract third-party guard authors will need to honor. Small contract (`guard_name`, `label`, `description`, `detect`, `acknowledge`) but it's one more thing to document.
- **A resumed dispatch can immediately re-block** if the underlying problem is unresolved (e.g. the PR genuinely has no actionable feedback and pr-fix skips again). That's acceptable — the guard re-fires honestly — and #145's benign-skip accounting means an empty re-skip no longer counts toward the cap.
- **Per-fire, not per-task.** If the cap fires again later on the same task, the operator decides again. There's no "permanently disable cap for this task" mode. That's by design (such a mode is a different shape — a per-task config override — and would be a separate ADR) but worth flagging because operators may expect it.
- **`detect()` is called on every task-detail fetch.** Cost is one DB query per registered handler per task-detail load. With the current 1–3 handlers in scope, negligible. If the registry grows past ~20 handlers, batching or caching becomes worth doing.
- **Substring-matched detection for the cap-fire reasoning.** Brittle if the cap-fire message text changes. Mitigated by emitting from exactly one site today; a follow-up adds a structured `block_reason` field to the audit row's metadata to make detection structural.

---

## Backward compatibility

- **No DB schema change.** The override audit row uses the existing `messages` table and `pr_decision` type. `decision="overridden"` is a new enum value added to the existing four (`done`, `skipped`, `needs_decision`, `blocked`). Reader code paths that branch on `decision` need a new case but don't regress on the existing four.
- **`_record_pr_decision` signature unchanged.** Its `decision` parameter stays `Literal["done", "skipped", "needs_decision", "blocked"]` (`lotsa/orchestrator.py:2756`). The override row is written via a direct `db.add_message` call (see Commitment 2) precisely so the new `"overridden"` value bypasses the helper's typed enum — callers of `_record_pr_decision` continue to use the four-value Literal with no migration.
- **Existing Retry behaviour changes** (Commitment 5). Today Retry calls `_pr_fix_round_cap_blocked` and re-fires the cap. Under this ADR, Retry no longer checks the cap — operator-initiated retry is supervised, not autonomous. The behaviour change is intentional and resolves a current bug: today an operator who Retries on a cap-blocked task gets immediately blocked again, with no way forward except the override (Commitment 2) or DB surgery. Under this ADR, Retry alone is sufficient when the operator has reviewed and wants to continue; the override is for resetting the *counter* itself (so subsequent autonomous rounds get full budget again).
- **New endpoint and frontend button are additive.** Older Lotsa clients without the UI changes can call the endpoint directly. Older Lotsa servers without the endpoint return 404 to new clients; the frontend handles 404 gracefully by hiding the override buttons. Acceptable for Lotsa CE pre-1.0.
- **Existing tests for the cap mechanism unchanged.** Override is a new code path; it adds tests, it doesn't modify existing ones.

---

## Scope

This ADR commits to:

1. The principle (operator-acknowledged overrides as first-class UI + audit events) and its corollary (autonomous-action caps apply only to autonomous actions).
2. The `OverrideHandler` registry contract.
3. The first concrete instantiation: `pr_fix_budget` handler.
4. The new `POST /api/tasks/{task_id}/acknowledge-override` endpoint and supporting orchestrator method.
5. The dashboard UI affordance in `ChatInput`.
6. The audit row shape (`decision="overridden"` on a `pr_decision` row with `role="user"`).
7. The operator-path cap-check carve-out (remove the five operator-initiated cap-check call sites; keep only the one autonomous site).

In scope for **Lotsa Community Edition** (`lotsa/` + `rigg/`).

---

## Out of scope

- **ADR-017 timeout dismiss as the second instantiation.** When ADR-017's soft-timeout indicator implementation lands, it registers an `OverrideHandler` against this ADR's contract (the "Dismiss warning" action). The handler code lives in the ADR-017 implementation PR; the contract it conforms to lives here.
- **ADR-015 merge-conflict handling.** An earlier sketch had the merge-conflict UI register an `OverrideHandler` ("I've resolved the conflicts, continue"). Superseded: ADR-015's 2026-06-12 revision resolves conflicts with a `resolve_conflicts` agent step and escalates judgment calls via `NEEDS_INPUT` — the operator's chat answer is the audited decision, and no blocked-state override exists to acknowledge.
- **+N rounds bounded extension** rather than reset-to-zero. Reset-to-zero is simpler and matches the cap's "fire fresh, decide fresh" intent. Operators wanting a bounded extension (e.g. "give it 5 more rounds, not unlimited") can be supported later by a richer reason-input schema; not in scope here.
- **Per-task config override** (e.g. set `max_pr_fix_rounds=20` for one specific task forever). Different shape — persistent task-scoped config rather than one-shot acknowledgement. Separate ADR if a real need emerges.
- **Multi-operator approval.** Requiring two operators to acknowledge before override takes effect. Out of scope for now.
- **Operator identity beyond `role="user"`.** Lotsa CE today has no concept of named operators. When it does, the override audit row's metadata gains an `acknowledged_by` field; until then, `role="user"` is the only signal.
- **Structured `block_reason` field on the audit row.** A follow-up improvement to make handler `detect()` structural rather than substring-matched. Small change to `_record_pr_decision` plus a backfill consideration; deserves its own PR.
- **A "dismiss permanently" option per guard per task.** If operators want to silence a specific guard for the rest of a task's life, that's a different shape. Out of scope.
