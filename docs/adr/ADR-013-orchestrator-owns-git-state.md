# ADR-013: Orchestrator owns git state — agents commit only

**Status**: Superseded by PR #57 (partial implementation).
**Date**: 2026-05-15
**Resolved**: 2026-05-21
**Related**: PR #57 (push step pushes worktree HEAD — implements Rule 1),
`docs/superpowers/issues/2026-05-12-orchestrator-must-own-git-operations.md`,
memory note `project_git_operations_orchestrator_owned.md` (where the
principle lives operationally), `CLAUDE.md`.

---

## Resolution

This ADR proposed three rules under one principle. After review, the
focused fix for Rule 1 shipped as a smaller scoped change in PR #57;
Rules 2 and 3 remain valid but unshipped. The principle itself
survived in the project memory note — and is doing real work there:
agents on later tasks (e.g. `lotsa/<redacted>` on 2026-05-19) cited the
memory note as the reason they refused to rebase autonomously.

The ADR is preserved as the historical decision record. It is **not**
the source of truth for the principle (the memory note is) and it is
**not** a commitment to ship Rules 2 and 3.

### Status of each rule

- **Rule 1 — push by HEAD, not branch name.** ✅ Implemented in PR #57
  (commit `72a2c3c` + follow-up `6609405`). The push step resolves
  `git rev-parse HEAD` and pushes the SHA to `refs/heads/lotsa/<task_id>`.
  Bug A is closed.
- **Rule 2 — state-driven dispatch decisions (8-cell HEAD-vs-base × marker
  table).** ❌ Not implemented. Bug B (SKIPPED with unpushed commits)
  is still a latent risk. Mitigated in practice by the Phase-2 prompt
  changes that tell the agent never to emit SKIPPED when there are
  unpushed commits — fragile, prompt-only enforcement. Future work
  if the bug reappears.
- **Rule 3 — detect-and-fail on agent git overreach.** ❌ Not
  implemented. The pattern in question (agent runs `git checkout -b`,
  `git reset`, `git rebase`) is still possible. Rule 1's fix makes
  most of these benign (push pushes HEAD regardless of which branch
  the agent left it on), so the urgency is reduced.

### What this ADR is good for

- The Context section below (a useful narrative of the two
  underlying bugs) — keep as reference.
- The principle statement: *git state is observable truth, not a
  marker the agent might misinterpret*.
- A pointer back to PR #57 for the implementation of Rule 1.

ADR-014 and ADR-015 build on the same principle but reference it
through the memory note, not through this ADR.

---

## Context

Two production bugs hit the same task (`<redacted>`, Phase-1 autonomous PR-fix
loop) within 72 hours, both rooted in the same architectural assumption:
that the agent and the orchestrator share authority over the worktree's
git state.

**Bug A — wrong-branch push (2026-05-12).** The code-step agent, following
CLAUDE.md's contributor guidance (`"One branch per issue:
feature/issue-{number}-..."`), ran `git checkout -b
feature/autonomous-pr-fix-loop-phase1` in the worktree and committed
there. The push step ran `git push origin lotsa/<task_id>` and silently
no-op'd because the local `lotsa/<task_id>` ref was unchanged. The
subsequent `POST /pulls` returned 422 (head and base identical SHA on
remote). The orchestrator's push step assumed the agent would commit to
the worktree's pre-set branch. The agent assumed it should follow the
project's branch-naming convention. Both assumptions are individually
reasonable; together they break the pipeline.

**Bug B — `PR_FIX_SKIPPED` with unpushed commits (2026-05-15).** In an
earlier pr-fix dispatch (round N), the agent correctly addressed the
bot's review feedback and committed `8851c1d`. In a subsequent dispatch
(round N+1) the agent saw the commit already in the worktree, judged
*"no new work this round,"* and emitted `PR_FIX_SKIPPED`. The
orchestrator's SKIPPED handler is designed as a no-op exit — it routes
back to `waiting_for_pr` without pushing. Commit `8851c1d` sat unpushed
indefinitely. The agent's reasoning even explicitly cited a memory note
that said *"git operations are orchestrator-owned"* as justification for
emitting SKIPPED — the agent interpreted that note as *"I shouldn't
push; the orchestrator will pick this up,"* which is the wrong
direction.

The two bugs share one root cause: **the orchestrator's "when to push"
decision is marker-driven (it reads the agent's emitted marker and
trusts the implied semantics), when it should be state-driven (it should
inspect the worktree directly and act on observed git state).**

The agent's marker should be advisory — *"I think I made a fix"* — and
the orchestrator should verify by reading the git state. Right now it
delegates to the marker and treats the worktree as opaque.

---

## Decision

**The orchestrator is the sole authority on git state.** Agents may stage
and commit; nothing else. Every other git operation — push, branch
create, branch switch, fetch, pull, merge, rebase, reset, tag, remote
manipulation — belongs to the orchestrator. The orchestrator inspects
the worktree directly to make decisions; it does not trust agent-emitted
markers as ground truth for git facts.

Operationally, this is one principle with three implementation rules:

### Rule 1 — Push by HEAD, not by branch name

The push step resolves the worktree's actual HEAD ref via
`git rev-parse --abbrev-ref HEAD` (or `git rev-parse HEAD` for the SHA)
and pushes *that* to `origin/lotsa/<task_id>`. The agent's local branch
name is irrelevant to the orchestrator. The canonical contract is the
*remote* branch name; the local name is the agent's working space and
may be anything.

This kills Bug A.

### Rule 2 — State-driven dispatch decisions

On every pr-fix outcome (regardless of marker), the orchestrator:

1. Records `origin/lotsa/<task_id>`'s SHA at dispatch time as the round's
   *base SHA*.
2. At completion, reads the worktree's HEAD SHA.
3. Combines (HEAD vs base) × (marker) into the actual transition:

   | HEAD vs base       | Marker            | Action |
   |---|---|---|
   | HEAD == base       | DONE              | **Block.** DONE without commits is agent confusion. Surface clearly. |
   | HEAD == base       | SKIPPED           | Trust the agent — return to `waiting_for_pr`. ✅ |
   | HEAD == base       | BLOCKED           | Block — agent said so, worktree agrees. ✅ |
   | HEAD == base       | NEEDS_DECISION    | `needs_input` (Phase 2). |
   | HEAD > base        | DONE              | Push, transition to `review`. ✅ (expected case) |
   | HEAD > base        | SKIPPED           | **Push anyway.** Agent emitted SKIPPED with unpushed commits — likely confused. Log a warning, push, return to `waiting_for_pr`. Defensive over strict because the work was real. |
   | HEAD > base        | BLOCKED           | Block, but push first so the partial work is preserved on the PR for human inspection. |
   | HEAD > base        | NEEDS_DECISION    | Push, then `needs_input`. The work the agent did before asking the question is preserved. |

   `HEAD > base` is "HEAD has commits beyond base" — i.e. `git
   rev-list --count base..HEAD > 0`.

This kills Bug B and replaces the marker-as-authority model with a
state-as-authority model. Markers become hints about agent *intent*,
useful for audit messages and UX, but no longer drive the git decisions.

### Rule 3 — Detect-and-fail on agent git overreach

If the orchestrator detects post-dispatch worktree state that suggests
the agent ran a non-commit git operation, it logs a warning and routes
to `blocked` with a clear error. Concretely:

- Local branch differs from the worktree's pre-set branch (Bug A
  signature) → log, but tolerate (Rule 1 handles the push correctly).
- Working tree dirty (uncommitted changes) at dispatch completion → log
  and proceed; the agent should have committed before finishing.
- Worktree HEAD detached → log warning, treat HEAD SHA as authoritative
  for push.
- Worktree HEAD points at a different SHA than where dispatch began *and*
  intermediate commits are absent (i.e. the agent rebased or reset) →
  block with `"agent rewrote history in worktree; investigate"`.

Long-term, these checks justify scoping the agent's allowed git
commands at the sandbox level (only `git add`, `git commit`, `git
status`, `git diff`, `git log` — no `checkout`, `push`, `pull`, `reset`,
`rebase`). That's a follow-up; the runtime audits in Rule 3 are the
interim defense.

---

## Tradeoffs

**Pros:**
- Eliminates a class of agent-vs-orchestrator authority conflicts.
- The pipeline becomes more deterministic — git state is observable
  truth, not a marker the agent might misinterpret.
- Future agent prompt changes (or model upgrades that reinterpret
  prompts) cannot break the pipeline by misreading git semantics.
- Agents become simpler — fewer responsibilities, narrower contract.
- Recovery from agent-side mistakes (wrong branch, missed push) becomes
  free instead of a manual intervention each time.

**Cons:**
- The orchestrator does more work per dispatch (two extra `git`
  subprocess calls — `rev-parse` and `rev-list --count`). Negligible
  given pr-fix dispatches already take seconds-to-minutes.
- The decision table in Rule 2 adds branches to the completion drainer.
  Need careful tests for each of the eight combinations.
- The agent prompt has to be updated to teach the new contract — the
  marker no longer "promises" a push, it only signals intent.
- Disambiguates a real ambiguity: today an agent can validly say "I did
  the work in a previous round, nothing more this round." That utterance
  no longer maps cleanly to SKIPPED because the orchestrator will
  detect the unpushed commits and act anyway. The agent can say it,
  but the orchestrator overrides. This is fine — the orchestrator is
  the one with the global view.

---

## Scope

This ADR applies to the **Lotsa Community Edition** orchestrator
(`lotsa/orchestrator.py`, `lotsa/push_step.py`, `lotsa/pr_monitor.py`)
and the agents it dispatches (currently: `code`, `review`, `verify`,
`pr-fix`, `spec`, `plan`, `test`).

Out of scope:
- Sandbox-level git command filtering (Rule 3 long-term). Captured as a
  separate follow-up.
- Refactoring the agent's allowed-tools list in
  `rigg/agent_runner.py`. The agent still runs in a sandbox that
  accepts arbitrary shell — Rule 3 audits are the boundary for now.

---

## Implementation surface (historical — see Resolution section)

The original plan was a single task implementing Rules 1 and 2:

- `lotsa/push_step.py` — resolve HEAD via `rev-parse`. ✅ Shipped in
  PR #57.
- `lotsa/orchestrator.py` — record `pr_fix_base_sha`, compute the
  8-cell decision table at pr-fix completion. ❌ Not shipped.
- `lotsa/prompts/full/pr-fix-system.md` — update the marker contract
  to be advisory rather than authoritative. ❌ Not shipped (the
  prompt was instead tuned via Phase-2's "never SKIPPED with unpushed
  commits" instruction — a less robust mitigation).
- Memory note — revised in the same window to clarify the
  agent-vs-orchestrator contract. ✅ Done; the note is in active
  use and was cited by an agent on `lotsa/<redacted>` (2026-05-19) as
  the rationale for refusing a rebase.
