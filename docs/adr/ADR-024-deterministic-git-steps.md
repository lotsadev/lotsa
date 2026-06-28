# ADR-024: Commit joins push as a deterministic orchestrator step

**Status**: Implemented (see "Addendum: realized as a step posthook" and
"Addendum 2: commit publishes once a PR exists" below)
**Date**: 2026-06-07
**Related**: ADR-013 (orchestrator owns git state — this ADR completes
Rule 2/3 by removing agent-side commit responsibility), ADR-014 (the
`type: action` step that `push_pr` already uses), PR #57 (push-by-HEAD,
the first deterministic git step), PR #69 (prompt-level fix this ADR
replaces), `lotsa/push_step.py` (existing deterministic action),
`lotsa/prompts/full/{coding,testing,verify,pr-fix}-system.md` (where
commit instructions currently live).

---

## Context

Lotsa's full flow today mixes two ownership models for git operations:

- **`push_pr`** is a deterministic action step (`type: action`,
  `lotsa/push_step.py`). The orchestrator runs `git push` and creates the
  GitHub PR with no agent involvement. Pre-flight check at
  `lotsa/push_step.py:192` refuses to proceed if the worktree has
  uncommitted changes.
- **`commit`** is a *prompt-level instruction* repeated inside each
  producer agent's system prompt. `coding-system.md` Step 5, `testing-
  system.md` Step 4, and `pr-fix-system.md` Step 29 each tell their
  agent to `git add` + `git commit` before returning. Gate steps (`review`)
  are told not to commit.

This split was introduced by PR #69 ("full flow — producer steps own the
commit") after `push_pr` started failing with *"Uncommitted changes
detected in the working tree"* on real tasks. The diagnosis at the time
was: the agents that *wrote* code (test, code, pr-fix) weren't committing
it, so the next gate step ran against an uncommitted diff and the eventual
push failed. PR #69 fixed it by adding commit steps to those three
prompts.

It worked. Until it didn't.

On 2026-06-07 the user re-encountered the identical error on task
`lotsa/<redacted>`. Root cause:

- The `verify` step's "Fixing issues" path (`lotsa/prompts/full/verify-
  system.md:24-32`) lets the agent modify files and emit `NEEDS_REVIEW:`,
  but contains no commit instruction.
- Per `lotsa/prompts/full/process.yaml:82-84`, `NEEDS_REVIEW:` routes the
  flow back to `review`, a gate step that does not commit by design.
- Review passes against the uncommitted diff, the flow advances to
  `verify` again, the agent emits `VERIFIED:`, and `push_pr` fails on the
  uncommitted-files check.

PR #69 missed `verify` because it wasn't a producer step at the time
PR #69 was written. The class of bug PR #69 closed is *"a new agent step
that writes files but isn't told to commit"*. PR #69 closed three known
instances; the class itself is still open. Any new step that mutates
files — a future linting step, a future doc-generation step, a future
benchmarking step — will reopen the same hole.

The pattern is recognisable: a behavioural rule enforced only in prompt
text is a rule that depends on every author of every future prompt
remembering it. The orchestrator already enforces "do not push with
uncommitted changes" deterministically; the symmetric rule "stage and
commit before yielding control" lives only in prose.

## Decision

**Pull commit out of agent prompts and make it a deterministic
orchestrator step of `type: action`, alongside the existing `push_pr`
action.** Push and PR creation are already orchestrator-owned (via
`push_pr`); this ADR adds the missing third piece. Agents are then
responsible only for changes to the working tree; the orchestrator is
responsible for everything that touches refs.

Concretely:

### 1. Introduce a `commit` action step

A new `type: action` step, implemented alongside `push_step.py` (likely
`lotsa/commit_step.py`):

```python
async def commit_step(work_dir: Path, context: StepContext) -> CommitResult:
    """Stage all tracked changes plus any new files the agent created,
    then create a single commit with a deterministic message derived
    from the step that produced the changes.

    No-op (and reports success) when the worktree is clean.
    """
```

Behaviour:

- `git add -A` over `work_dir`, but with a deny-list for paths the
  orchestrator never wants in a commit (`.env*`, anything in `.gitignore`
  is already excluded by git, plus a small belt-and-braces list for
  patterns we've seen agents emit by accident).
- If `git diff --cached --quiet` shows nothing staged after the add,
  exit with a no-op success — there was nothing to commit, which is
  the legitimate path after a gate step or a pure-read agent.
- Otherwise `git commit -m "<message>"` with a deterministic message
  built from the producing step's name and the task slug, e.g. `code:
  <task title>` or `test: <task title> (red)`. The conventional-commits
  prefix is derived from a `commit_prefix:` field on the producing step,
  defaulting to `chore:`.
- Emit the resulting commit SHA into the step's output for audit.

### 2. Schedule the `commit` step after every producer agent

In `process.yaml`, insert `commit` immediately after every step whose
agent may write files, on **every outbound transition** from that step
— not just the "I made changes" branch. Initially: after `test`, after
`code`, after `verify` (both `NEEDS_REVIEW` and `VERIFIED` paths), and
after `pr-fix` (the `PR_FIX_DONE` path; the other pr-fix markers are
no-modification by contract but `commit`'s clean-worktree no-op makes
the extra step free).

The "every outbound transition" rule matters: `verify`'s prompt today
says *"Do NOT output `VERIFIED:` if you made code changes."* That is
exactly the prompt-level enforcement this ADR is moving away from. A
`commit` step on the `VERIFIED:` path is a no-op when the agent
followed the rule and a save when it didn't. The same logic applies
to any future agent step that gains a non-modification exit path —
inserting `commit` after the step, unconditionally, means a regressed
prompt cannot reintroduce the bug.

Routing tables get one new rule per `commit` insertion: success goes to
whatever the producer was previously chained to; failure goes to a new
`commit_failed` block reason that surfaces the offending paths to the
operator without retrying.

### 3. Strip commit instructions from agent prompts

Remove the commit steps from `coding-system.md`, `testing-system.md`,
`pr-fix-system.md`, and the implicit one from `verify-system.md`'s
"Fixing issues" path. Replace each with a single short line at the
appropriate location: *"You do not commit. Leave your changes staged
or unstaged; the orchestrator handles commit and push."*

The `Do not push` line already in `coding-system.md:121` becomes the
template for the broader rule.

### 4. Keep `push_pr` as it is

The current `push_pr` action is already on the right side of this line.
It stays. Its uncommitted-changes guard becomes a defence-in-depth
check rather than the primary user-visible error — by the time `push_pr`
runs, the `commit` step has already either committed everything or
explicitly failed.

## Why this over a prompt fix for verify

A prompt fix for `verify` closes today's specific manifestation. It does
not close the class. Three observations justify the structural fix
instead:

1. **The orchestrator already owns half the lifecycle.** Push happens in
   code; commit happens in prose. The split has no principled defence —
   it's an artefact of the order in which each problem first appeared
   and got patched.
2. **Every new agent step is a new prompt to remember to write
   correctly.** A multi-provider step (ADR-023), a linting step, a
   metrics step, a docs-generation step — each is a chance for the same
   bug to re-enter. The cost of "remember to add commit instructions"
   compounds with every flow extension.
3. **Agents are not the right enforcer.** Prompt instructions are
   advisory under best-effort interpretation. A subprocess call to `git
   commit` is not. The thing we want enforced is mechanical, so the
   mechanism should be mechanical.

## Why this complements rather than supersedes ADR-013

ADR-013 ("Orchestrator owns git state — agents commit only") shipped
Rule 1 (push by HEAD) via PR #57 and left Rules 2 and 3 unimplemented.
Its framing — *agents commit, orchestrator pushes* — was correct relative
to the situation it described, where the bug was push-related. This
ADR observes that the symmetric problem (commit-related) has now
materialised twice and proposes the next-most-restrictive split:
agents make file changes, orchestrator handles everything else.

ADR-013 stays in its Superseded-with-implemented-Rule-1 state. This ADR
is the natural continuation, not a contradiction.

## Consequences

### Positive

- The "uncommitted changes detected" class of bug is closed structurally,
  not patched per-step.
- New agent steps need no special git-related instructions; they
  inherit the commit/push lifecycle by virtue of being placed in the
  flow.
- Commit messages become consistent and grep-able across the audit
  trail (no more agent-authored variation in subjects).
- The reviewer-facing diff is whatever the orchestrator captured at
  commit time, not whatever the agent happened to remember to stage.
  Reproducibility improves.

### Negative

- The deterministic commit message loses the per-step nuance an agent
  could write. Mitigation: pass enough context (task title, step name,
  optional one-line summary the agent emits before yielding) for the
  message to be useful without being agent-authored prose.
- An agent that previously chose *not* to commit certain files (e.g. a
  scratch directory, a debug log) now has to either delete those files
  or add them to `.gitignore`. The deny-list in step 1 covers the
  obvious cases; the rest is a one-time cleanup task on adopting flows.
- One more step in the flow per producer agent — a small but real
  increase in orchestrator round-trips per task. Acceptable: each step
  is a single subprocess call, dwarfed by agent execution time.

### Migration

1. Land the `commit_step.py` action and its registration as an action
   type (mirrors `push_step.py` registration). Add unit tests covering:
   clean worktree no-op, single new file, mixed staged/unstaged, denied
   path rejection.
2. Insert `commit` steps in `process.yaml` after every producer agent.
   This is a non-breaking change because today's agents already commit;
   the orchestrator's commit will be a no-op on already-clean worktrees.
3. Strip commit instructions from the four producer prompts. After this
   point a regressed prompt cannot reintroduce the bug.
4. Remove `_has_uncommitted_changes` as a hard error from `push_pr` and
   downgrade it to a defensive log + abort (the case should now be
   unreachable; logging it tells us if a future bug bypasses the new
   commit step).

Steps 1 and 2 can ship in one PR. Step 3 ships once step 2 is on main
and the flow has run a handful of real tasks cleanly. Step 4 is an
opportunistic cleanup, not load-bearing.

## Out of scope

- Rebase, merge, branch-creation, and tag operations. Those remain
  outside the producer-agent path and outside this ADR.
- The "should lotsa use libgit2 instead of subprocessing git" question.
  This ADR keeps the existing subprocess-exec pattern from
  `push_step.py`.
- Multi-commit producer steps (an agent wanting to commit twice in one
  step). Not a current pattern; if it becomes one, the `commit` step
  is the right place to model it explicitly.

## Addendum: realized as a step posthook (Implemented)

The decision above frames commit as a repeated `type: action` step
inserted after every producer in `process.yaml`. The implementation
diverges on **mechanism** while keeping the principle (commit becomes
mechanical and orchestrator-owned):

- **A generic per-step posthook**, not a repeated action step. The flow
  model can't host the same job at multiple flow positions without a
  state collision (each job's state is derived from its name), so commit
  is declared as a per-step `posthooks: [commit]` field on the
  code-producing jobs (`test`, `code`, `verify`, `pr-fix`). The
  orchestrator runs a step's resolved posthooks after the **agent**
  completes successfully and before the success-state CAS, against the
  task's worktree. Action/monitor steps never run posthooks, so commit
  stays off non-code steps and off non-code processes (`simple`,
  `standard`) entirely — without per-process opt-out.
- **`commit` is a registered posthook** (`lotsa/posthooks/__init__.py`)
  wrapping a reusable, unit-testable `lotsa/commit_step.py`
  (`execute_commit` / `CommitResult` / `CommitError`), mirroring
  `push_step.py` subprocess discipline. Clean worktree → no-op success;
  git failure → the task is blocked with the error as the reason, no
  retry.
- The ADR's "after every producer, on every outbound transition" is
  realized by declaring the posthook on the producing job: a single
  insertion point in the completion drainer covers every outbound path
  (rule-route, conversational, gate, auto-advance), and the clean-tree
  no-op covers the non-modifying exits (`verify`'s `VERIFIED:`,
  `pr-fix`'s `SKIPPED`/`NEEDS_DECISION`).
- The posthook abstraction (registry + per-step field) is the new
  protocol surface; future hooks (lint, format, doc-gen) register a name
  and are listed in `posthooks:` with no orchestrator change.

Step 4 of the Migration (downgrade `push_pr`'s `_has_uncommitted_changes`
hard error to a defensive log + proceed) shipped with this change rather
than as a later opportunistic cleanup.

## Addendum 2: commit publishes once a PR exists (Implemented — issue #155)

The decision above keeps `commit` and `push_pr` as **separate** deterministic
steps (Decision §4). In practice that left them coupled only by flow *routing*,
and the flow has routes that reach `commit` but not `push_pr`:

- A `pr-fix` round that emits `PR_FIX_SKIPPED` routes `pr-fix →
  wait_for_pr_signal`, bypassing `push_pr`. The commit posthook still ran (it
  fires before the marker routes), so a commit could land in the worktree with
  no path to the PR branch.
- The ADR-015 pre-pr-fix sync pushes its merge commit **only when it performs a
  merge** (`HEAD..origin/main > 0`). Once the worktree has already absorbed
  `main`, the sync short-circuits at `behind == 0` and the merge commits from a
  prior round — plus any agent commit — stay unpushed.

The failure mode (task <redacted>): a worktree drifted 9 commits ahead of its
pushed PR branch (including a real fix). The pr-fix agent evaluated the bot's
finding against the *worktree* (already fixed → `SKIPPED`); the bot reviewed the
*pushed branch* (fix absent → re-flag) — a benign-skip loop that never
converged because the orchestrator never published the worktree.

**Resolution.** The principle "commit and push are paired deterministic git
operations" is now enforced structurally, not by routing: once the task carries
a `pr_number`, the `commit` posthook pushes HEAD to the PR branch in the same
step (`lotsa/posthooks/__init__.py`). Key properties:

- **Fires whenever a PR exists, not only when *this* step committed.** A
  no-op-commit round still publishes, so drift accumulated by an earlier round
  converges. Pushing an already-current branch is a cheap remote no-op.
- **Pre-PR is unchanged.** With `pr_number` unset the posthook only commits;
  the main pipeline's `pr_summary → push_pr` still owns the first push and PR
  creation (and thus the PR description). `push_pr` remains the sole PR-*creation*
  path and keeps its `NON_FAST_FORWARD → rebasing` handshake for the first push.
- **Failure contract mirrors the ADR-015 sync.** A `PushError` (incl.
  NON_FAST_FORWARD, which here means the PR branch moved underneath the
  orchestrator) surfaces as a posthook failure → task blocked; recovery is
  revise → pr-fix, which re-syncs before the next push. We intentionally do
  *not* thread the dedicated `rebasing` state through the posthook layer —
  orchestrator-internal pushes outside the `push_pr` action already block on
  failure (the sync does the same).

The ADR-015 sync's clean-merge push (step 5) is now redundant insurance rather
than the only publish path; it is left in place.
