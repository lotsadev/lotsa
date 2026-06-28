# ADR-040: Restart-resilient orchestration — the DB is the state of record; resume in-flight agents instead of blocking

**Status**: Proposed — post-launch, high priority (every deploy now restarts the service, so this is a routine event, not a rare crash).
**Date**: 2026-06-27
**Related**: ADR-014 (jobs/flow steps — the units that must be idempotent), ADR-020 (typed atomic transitions / CAS — the no-op-on-rerun mechanism), ADR-025 (layered system prompt — where the resume note attaches), ADR-028 (runner shapes — CLI/docker/SDK; resume support varies by runner), ADR-030 (PR-lifetime monitoring), ADR-033 (feedback tracking by comment identity — one application of the invariant below), and the docker agent-home mount (session-JSONL persistence). Scope: CE (orchestrator + `rigg` runner contract).

---

## Context

Lotsa runs as a long-lived daemon (systemd unit). It is redeployed, and the
deploy now **always restarts the service** (the `install.sh` `enable --now` →
explicit `restart` fix). So a restart is a routine, frequent event — not the
rare crash the current design assumed.

Today `OrchestratorService.start()` is **deliberately destructive**. Its own
docstring:

> *"any task with status='working' was mid-execution when the server died. We
> mark it blocked … and let the user explicitly retry. We do NOT try to
> reconstruct in-memory state from the DB — restart is destructive on
> purpose."*

The consequences:

- **Every deploy interrupts every in-flight task** and dumps it to `blocked`,
  requiring a manual Retry. During launch hardening — where we redeploy often —
  this is operationally hostile and reads to a user as "it stopped working
  after I deployed."
- **In-memory orchestration state is lost.** `PrMonitor._tracked`, its debounce
  timers, and the feedback seen-map (`last_updated_at_by_comment_id`) all live
  only in memory. ADR-033 is already moving the feedback map into the DB to fix
  one symptom (lost PR feedback after a restart — see ADR-033's 2026-06-27
  recurrence). The same principle deserves to be a **named invariant**, not a
  per-bug patch.

Two facts make a better design reachable:

1. **The killed agent is resumable.** The CLI session id is persisted in task
   metadata (`session_id`), and the session JSONL survives restart — on disk
   under the worktree's agent-home (native) or the mounted agent-home (docker,
   already persisted by the runner). `claude --resume <session_id>` reattaches
   to that conversation.
2. **Most steps are already close to idempotent.** The deterministic posthooks
   (commit / push / sync) are written to be safe to re-run; agent steps operate
   on a worktree whose current state they can observe.

The operator framing that motivated this ADR: *"There shouldn't be any
in-memory data. Every call should be near-idempotent. Agents may have started
working when the server restarts — I'm hoping they resume, maybe make it
explicit in the prompts too."*

## Decision

Two coupled commitments.

### 1. The database is the state of record; in-memory is a cache

- Any in-memory structure the orchestrator or monitor relies on **for
  correctness** must be reconstructable from the DB at any time. In-memory
  dicts and timers are permitted only as *caches* of persisted state, never as
  the sole record of a decision.
- **Every flow step is idempotent** — safe to execute at-least-once. A step
  that cannot be made naturally idempotent must be guarded by a persisted CAS
  marker (ADR-020) so a re-run is a no-op. This is what makes
  at-least-once dispatch (and therefore resume / retry / crash recovery) safe
  in general, not just at restart.

### 2. Restart resumes in-flight work instead of destroying it

On startup, a task with `status='working'` is treated as **interrupted**, not
**failed**. The orchestrator attempts to continue it:

- **Resume the agent** when the interrupted step ran an agent, a `session_id`
  exists, and the runner supports resume → re-dispatch the step with
  `--resume <session_id>`, reattaching to the conversation where it left off.
- **Idempotent re-run** when resume isn't available (no `session_id`, a runner
  without resume support, or a deterministic/posthook step) → re-run the step
  from its start; step idempotency makes already-completed work a no-op.
- **Bounded** by a per-task `resume_count` cap (e.g. 2). On exceeding the cap,
  fall back to today's behaviour — `blocked` with an explicit "couldn't resume
  after N attempts" message — so a genuinely crash-looping agent does not
  resume forever. This is what converts an otherwise-undiagnosable
  working-orphan into an explicit operator decision.

**Resumed agents are told so.** Via the layered prompt (ADR-025), a resumed
dispatch appends a note: *"You may have been interrupted and resumed. Before
continuing, check what you have already done (git status, the worktree,
existing files, prior tool output) and do not redo completed work."*
Prompt-only is **not** sufficient — the orchestrator choosing resume over block
is the load-bearing change — but the note materially reduces double-work on the
resume path.

### Graceful drain (complement, not substitute)

On `systemctl stop`/restart, the service first stops accepting new dispatches
and waits a bounded grace period for in-flight agents to finish, then exits.
Only agents still running past the grace window need resuming. This minimises
how many tasks hit the resume path at all (systemd `TimeoutStopSec` + an
orchestrator shutdown handler).

## Consequences

### Positive
- Deploys (now always a restart) no longer interrupt running work — tasks
  resume or no-op-rerun. The "it broke after I deployed" failure class
  disappears.
- The DB-is-truth invariant removes the in-memory-amnesia bug **family** at the
  design level; ADR-033's lost-feedback incident is one instance of it.
- Idempotent steps make at-least-once execution safe everywhere — which is also
  what makes retries and crash recovery robust, not just restart.

### Negative / risks
- **Session durability.** Resume depends on the session JSONL surviving
  restart. Native: on disk under the worktree agent-home. Docker: the mounted
  agent-home (already persisted by the runner). A runner/mode that can't
  persist the session falls back to idempotent re-run-from-start (still safe) —
  documented per runner.
- **Mid-side-effect kills.** An agent killed just after `git push` but before
  the result is recorded could double-apply on resume/re-run. Mitigated by
  idempotent steps + persisted CAS markers + the resumed-agent prompt. Each of
  push / commit / sync must be audited for re-run safety as part of Phase 1.
- **Interrupted vs. crashed are indistinguishable** at startup (both present as
  `status='working'`). The `resume_count` cap is what bounds the blast radius
  of a self-crashing agent.
- **Startup is the most safety-critical moment** and gains moving parts. Staged
  and tested: resume happy-path, no-session fallback, cap→block, drain timeout,
  idempotent re-run no-ops.
- **Runner variance.** SDK runner (ADR-028) resume semantics differ from the
  CLI; the rollout notes which runners support resume.

## Alternatives

- **Keep destructive restart, make Retry one-click** (status quo + UX polish).
  Rejected: deploys still interrupt work, just with easier manual recovery —
  and it ignores the "near-idempotent, no in-memory state" directive.
- **Drain-only** (wait for all in-flight to finish before exit, no resume).
  Rejected as the whole answer: agents take minutes; an unbounded drain blocks
  deploys and a forced-timeout drain still strands survivors. Adopted as a
  *complement*.
- **Full durable-workflow engine** (e.g. Temporal). Rejected for now: large
  dependency + self-hostable-rule scrutiny. DB-as-truth + idempotent steps +
  resume captures most of the benefit at a fraction of the cost. Revisit only
  if step orchestration outgrows it.

## Phasing

1. **Invariant + audit.** Document "DB is the state of record; steps are
   idempotent." Audit each step (sync, push, commit, agent) for re-run safety;
   add CAS guards (ADR-020) where missing.
2. **Interrupted, not failed.** On startup, mark working tasks with a distinct
   interrupted condition (`interrupted_at`, `resume_count`) rather than flipping
   straight to `blocked`.
3. **Resume dispatch.** Re-dispatch interrupted steps with
   `--resume <session_id>` where supported; otherwise idempotent re-run; cap →
   block. (Extends the `rigg` runner contract to expose resume capability +
   session continuation.)
4. **Resumed-agent prompt note** (ADR-025 layer).
5. **Graceful drain on shutdown** (systemd `TimeoutStopSec` + shutdown handler).
6. **Monitor cache rebuild.** `PrMonitor` reconstructs `_tracked` (and
   ADR-033's `pr_delivered`) purely from metadata on start; no correctness
   depends on in-memory-only fields (debounce timers become best-effort, not
   correctness-bearing).

## Out of scope

- Marker robustness / outcome evaluation (ADR-039).
- The feedback-identity map itself (ADR-033) — this ADR generalises the
  invariant that ADR-033 relies on and adds agent resume; the two ship as a
  pair.
- Distributed / multi-node orchestration — the single-daemon assumption holds.
