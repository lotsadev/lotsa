# ADR-043 — Two-phase Think→Execute task model

**Status:** Implemented

**Scope:** CE

## Context

Lotsa CE shipped five bundled processes (`simple`, `standard`, `full`, `chat`,
`quickfix`). They overlapped, misdescribed themselves, and one (`standard`'s
`coding` prompt) told the agent to `git checkout -b` / `git add` / `git commit`
— a direct violation of ADR-013 (orchestrator owns git state). The flat catalog
also gave the operator a five-way choice on the new-task picker with no clear
axis to reason about.

## Decision

Replace the flat five-preset catalog with a **two-phase Think→Execute model**
of exactly three processes on a single think→execute axis:

| Process | Phase | Pipeline |
|---------|-------|----------|
| `chat`  | **Think** — interactive, never writes code | one conversational REPL step; can distill a spec on request; ends by handoff or abandon |
| `build` | **Execute (full depth)** | `plan → test → code → review → verify → pr_summary → push_pr → wait_for_pr_signal` (+ `pr_fix` sub-flow) |
| `fix`   | **Execute (shallow depth)** | `code → review → push_pr → wait_for_pr_signal` (+ `pr_fix` sub-flow) |

- **`build` is `full` minus the spec/plan *gates*.** `plan` moves inside build
  as its ungated first step (no `gate_state: planned`, no `output` artifact —
  its reasoning propagates to `test`/`code` via `resume`). Every `inputs:
  [spec, plan]` reference is dropped: the task body — or a spec carried from
  chat via `promotion_inputs: draft_spec` — is the brief. Routing is preserved
  (review PASS→next/FAIL→code; verify VERIFIED→next/NEEDS_CODE→code/
  NEEDS_REVIEW→review).
- **`fix` is Execute at shallow depth** (not a peer mode of `build`). It now
  **pushes** and opens a PR (the former `quickfix` committed but never opened
  one). It keeps the mechanical "execute this instruction" coder framing.
- **`full` is dissolved** — removed as a process. Its capability is
  reconstituted as the *workflow* chat(design+spec)→build, not a *process*.
  `simple` is deleted outright.
- **The handoff (Think→Execute) is the sole pre-code gate.** The operator
  approves at chat→execute; the plan-review gate is dropped (a possible future
  toggle). The handoff is **irreversible** (no return to chat — generalizing
  ADR-027 §7) but the running Execute task stays **steerable** via revision
  feedback and `NEEDS_INPUT`.
- **Every Execute run ends in a push** (`push_pr → wait_for_pr_signal`,
  PR-primary), watched to terminal by `pr_monitor` (ADR-030). For GitHub-less
  setups a new operator **mark-complete** action (`awaiting_operator` parked
  status → `complete`, via a non-edge-gated `atomic_transition` shaped like
  ADR-030's terminal signals) is the escape hatch.

### Prompt resolution

`build/` carries every generic diff/PR/git-driven prompt (`review`, `pr-fix`,
`resolve_conflicts`, `pr_summary`) plus build-specific `planning`/`testing`/
`coding`/`verify`. `fix/` ships only its distinctive `coding` prompt and falls
back to `build/` for the generics (`_resolve_prompts_search_paths`, replacing
the old `quickfix → full` fallback). Inline/unknown processes fall back to
`build/`. No surviving prompt instructs the agent to branch/commit/push
(ADR-013 clean).

### Legacy handling

Clean break (pre-alpha posture): a task persisted under a removed process name
(`simple`/`standard`/`full`/`quickfix`) is routed to `blocked` on restart with a
recovery message naming the removed process — no aliasing to the new names.

## Consequences

- **Supersedes ADR-014's catalog** (the five-preset table). The typed-job flow
  primitive itself is unchanged.
- **Amends ADR-027** — "promotion" is reframed as "handoff" in the UI; the
  internals (`promote_task`, `PromoteNotAllowed`, `POST /promote`) are
  unchanged. "No demotion into chat" generalizes to "Think→Execute is the only
  transition, and it's irreversible."
- **Amends ADR-030** — mark-complete + the `awaiting_operator` parked status
  join the PR-lifecycle terminals as ways a task reaches a terminal state.
- **Amends ADR-034** — chat is the entry *mode*, not just the default
  *selection*; the new-task picker becomes a chat-default mode switcher
  (Chat / Build / Quick fix).
- **Intentionally lost:** `full`'s one-shot spec→code→PR run (for a chat-first
  tool the spec is exactly where a human should weigh in — the handoff is a
  feature, not friction) and the plan-review gate (candidate future toggle).

## Out of scope (fast-follow)

- Branch-only push (push a branch without opening a PR) — v1 is PR-primary +
  mark-complete only.
- Renaming the `promote_*` internals / API routes / ADR-027 title.
- A one-process depth-dial / agent-adaptive depth — rejected in favour of two
  deterministic processes presented under one handoff gesture.
