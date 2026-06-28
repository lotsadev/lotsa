# ADR-027: Operator-driven process promotion — mid-life process switch for tasks

**Status**: Implemented
**Date**: 2026-06-08

> **Implementation note (2026-06-11).** Shipped in full. PR 1 (mechanism) added
> the `process_promotion` / `artifact_seeded` audit events, the
> `promote_task` action method (CAS via `atomic_transition`'s new `to_metadata`
> param), `POST /api/tasks/{id}/promote` + the `lotsa promote` CLI, the catalog
> `description` / `promotion_inputs` schema fields, the `spec-system.md` /
> `spec-user.md` verify-instead-of-elicit handover, and the dashboard
> Promote button/modal. PR 2 added the bundled `chat` (one conversational REPL
> step with the rendered available-processes block) and `quickfix`
> (`code → review`) processes, plus `description`/`promotion_inputs` on the
> bundled catalog. Deviation from §7's "→ push" sketch: `quickfix` is
> `code → review` (review terminal), matching the actual minimal shape of the
> bundled `simple`/`standard` presets, which do not push either; a push
> pipeline can be added later if needed.
**Related**: ADR-014 (jobs as unified flow primitive — defines what
processes/flows/jobs are), ADR-021 (per-task process dispatch — made
each task's process its own; promotion is the next move that
becomes natural once that lands), ADR-016 (worktree-persisted task
artifacts — the spec-handover mechanism re-uses this pattern),
`lotsa/orchestrator.py` (where the new action method lives),
`lotsa/prompts/full/spec-system.md` (the prompt that grows a
verify-instead-of-elicit branch).

---

## Context

ADR-021 made each task carry its own process: `metadata.process_name`
selects the state machine, the job catalog, and the prompt set the
orchestrator dispatches against. From that moment on, two tasks
running concurrently can be on different processes — one on `full`,
one on a hypothetical `chat` process — without conflict.

That gets us most of the way to a real workflow story. The remaining
gap is *changing a task's process mid-life*.

The motivating shape is **start exploratory, decide where to take
it from there**:

1. Operator opens a task in `chat` mode. The chat process has one
   conversational step that runs as a REPL — no markers required,
   no commit pressure, just a structured conversation.
2. The operator and agent explore an idea — discussing scope,
   reviewing relevant code, sketching design — for as long as it
   takes.
3. Once the chat reaches a decision point, the operator wants the
   task to *continue* — same worktree, same audit log, same
   conversation history reachable — but now under whatever process
   matches where the idea is going.

That destination is genuinely arbitrary:

- **Build it** — promote to `full` SDLC (spec → plan → test → code →
  review → verify → push). The canonical case.
- **Research it deeper** — promote to a `deep-research` process that
  fans out web/code searches and produces a structured report.
- **Audit existing code** — promote to a `qa-review` process that
  walks the worktree against a checklist and surfaces findings.
- **Spec-only** — promote to a process that produces a spec
  artifact and stops, without implementation. (Useful when the
  operator wants to draft a spec for someone else to build.)
- **Anything else operators define.** ADR-014 + ADR-021 made
  processes catalogable; this ADR makes the *destination* of a
  promotion a free choice from that catalog.

Today, the operator must copy the chat output, create a new task on
the destination process, paste context in, and the chat task becomes
a stranded record. The vision is one task with one history,
switching processes when ready. The operator should not have to
choose between "exploratory chat" and "structured execution" at
task creation — they should be able to start one and grow into the
other, in whichever direction the conversation pointed.

ADR-021 explicitly left this unaddressed: it lifted the
singleton-process constraint but didn't introduce a mechanism for a
task's process to change at runtime. The orchestrator reads
`metadata.process_name` at dispatch time; nothing stops it from
reading a *different* name on the next dispatch — what's missing is
the controlled, observable transition.

## Decision

**A task's process can be changed mid-life by an operator-driven
action. The new process takes over from its first step, with a
clean state-machine transition. The worktree, the audit log, and
the task identity are unchanged. Agent-driven sub-flow switching
(within the same process) stays a separate concept.**

Concretely:

### 1. New action method: `promote_task`

```python
async def promote_task(
    self,
    task_id: str,
    to_process: str,
    initial_artifacts: dict[str, str] | None = None,
) -> None:
    """Switch task to a different loaded process.

    Atomic transition: update metadata.process_name and transition
    state to the destination process's first job's queue_state.
    Worktree continues unchanged. Audit log records the promotion
    with the prior process name and the destination's first step.
    """
```

Invariants:

- **Operator-only.** No agent marker triggers this. Agents may
  *suggest* promotion in their output ("we're ready to build —
  promote to `full` when you want"), but the orchestrator does not
  parse such suggestions. Only `POST /api/tasks/{id}/promote`
  (dashboard button, CLI command) calls `promote_task`.
- **Destination process must be loaded.** The error path is
  identical to `create_task`'s unknown-process error: a name not
  in `self._processes` is rejected.
- **Always enters at the destination's first step.** The simplest
  contract. Operators wanting to skip the first step (e.g. *"skip
  spec, start at planning"*) instead provide a pre-filled artifact
  via `initial_artifacts` (see §4 below), and the destination's
  first step recognizes and short-circuits.
- **Same task, same worktree, same audit log.** Promotion is *not*
  task creation. The `messages` table accumulates; the worktree
  stays on `lotsa/<task_id>`; no parent/child relationships are
  introduced.

### 2. Sub-flow switching stays separate

Sub-flow switching (e.g. `review → pr-fix` within `full`) is an
agent-driven, orchestrator-routed state machine edge defined by an
output rule in `process.yaml`. It's about *orchestration decisions
within an existing flow contract* — the agent emits `REVIEW_FAIL`,
the orchestrator routes to `pr-fix`, the contract (which process
applies, what steps exist) hasn't changed.

Process promotion is *changing the contract*. The mechanism is
different (action call, not output rule), the authority is different
(operator, not agent), the audit signal is different (`promotion`
event, not state transition), and the destination is in a different
state machine entirely.

These two should not share implementation. They should not share
audit framing. The dashboard surfaces them differently:

| Concept | Trigger | Surface | Audit |
|---|---|---|---|
| **Sub-flow switch** | Agent marker | State transition in the same SM | `state_change` event |
| **Process promotion** | Operator action | Cross-SM transition | `process_promotion` event with old/new process names |

### 3. Chat-process triage — the planning-mode analogue

Process promotion only matters if there's a sensible way to
*decide* which destination to promote to. The chat process is
where that decision happens. Its job isn't just open-ended
conversation; it's reaching a *decision point* where the right
destination becomes selectable — the same shape as Claude Code's
plan-mode exit: the agent proposes, the operator confirms.

For the chat agent to triage, it needs to know what processes are
available and what each is for. The process catalog grows a new
**optional `description:` field at the root** alongside the
existing `process:` name:

```yaml
process: quickfix
description: |
  Execute a precisely-scoped mechanical change the operator has
  already decided on. No design conversation, no test writing.
  Use for: ADR status bumps, typo fixes, dependency bumps,
  rename operations, config tweaks. Includes a review step
  for safety but reviews against the operator's instruction,
  not the broad architectural checklist.
promotion_inputs:
  - name: instruction
    description: The mechanical change the operator wants applied.
jobs: [...]
flows: [...]
```

The orchestrator reads all loaded processes' descriptions at
startup. The chat agent's system prompt includes those descriptions
(rendered as an *available processes* block), and the chat agent
uses them to match the operator's intent against the catalog.

#### Why descriptions, not hardcoded heuristics in the prompt

An earlier draft of this ADR considered baking the triage rules
into `chat-system.md`:

> *"if the operator names specific files and the change is
> mechanical, suggest `quickfix`; if they describe behavior to add,
> suggest `full`; …"*

That approach hardcodes Lotsa's bundled flow taxonomy into a prompt.
Operators who define their own processes (`qa-review`,
`deep-research`, internal flow shapes) get no help from chat — the
chat agent doesn't know what to do with a flow whose name it has
never seen unless we update the system prompt.

Description-based triage scales:

- Operators ship a new process with a description; chat reads it
  and can route to it without prompt changes.
- Lotsa's bundled processes each carry their own description.
- The chat agent's behavior is data-driven, not prompt-driven.

This is the **`SKILL.md` / skills pattern in Claude Code** applied
to flows. Skills carry a `description:` frontmatter that the model
uses to decide when to invoke them. Lotsa processes are richer than
skills — multi-step orchestrated flows, not single operations — but
the triage mechanism is structurally identical.

#### The bundled process catalog after this ADR

| Process | Shape | Description (rendered to chat agent) |
|---|---|---|
| `chat` | One conversational step | Exploration and triage. Talk through an idea with the operator until the next step is clear, then suggest promoting to the matching process. |
| `quickfix` | code → review | Mechanical change the operator has already decided on (status bumps, typo fixes, renames, config tweaks). Review checks the diff against the instruction. |
| `simple` | coding | Small build where the operator knows what they want but the agent should think through the implementation. Differs from quickfix in coder-agent framing: "build this" vs "execute this." |
| `standard` | coding | Substantive change with clean commit and review, but no spec/plan/test ceremony. |
| `full` | spec → plan → test → code → review → verify → push | New feature or substantive change. Full SDLC discipline including operator-approved spec, failing-tests-first, and post-implementation verify. |

The quickfix/simple distinction is the load-bearing one for chat's
triage decisions. Both are short flows; the difference is *prompt
framing* — quickfix's coder agent reads "execute this instruction,"
simple's reads "build this thing." Review is included in both;
review is fundamental to any code change.

#### Chat-agent triage behavior

When the conversation reaches a concrete decision (operator and
agent have agreed on what to do):

1. Match the operator's intent against the loaded processes'
   descriptions.
2. Surface the top match plus any close runners-up via the
   dashboard: *"This sounds like a `quickfix`. Want to promote and
   execute? (Other options: `simple`, `standard`, …)"*
3. Do not promote on the agent's own authority — per §1, promotion
   is operator-only. The chat agent suggests; the operator clicks.

If the conversation hasn't reached a concrete decision, keep
chatting. The triage moment is recognizable: scope, files affected,
and intended behavior change are all clear enough that the matching
description is obvious.

### 4. Handover via pre-seeded artifacts

When a task is promoted, the work that already happened — typically
a chat conversation, sometimes other context — should not be lost.
The mechanism for carrying that work into the new process is
**generic, destination-agnostic, and re-uses ADR-016's artifact
pattern.**

Mechanism:

- The `promote_task` caller optionally passes `initial_artifacts`,
  a dict of artifact-name → content. The dashboard captures the
  promoting-side context (chat conversation, search results,
  whatever the source process produced) and passes it under
  whatever artifact name(s) the destination's first step is
  prepared to read.
- The orchestrator writes these to the standard artifact storage
  (same path as ADR-016's per-job `output_file` would) and records
  them in the audit log as `artifact_seeded` events with the
  source `"promotion"`.
- The destination process's first step's prompt is responsible for
  recognizing pre-seeded artifacts and adapting its behavior —
  verify-instead-of-elicit, skip-the-intake-conversation, jump-
  straight-to-analysis, whatever fits that process's first step.

#### How the dashboard knows which artifact name(s) to seed

The dashboard's promotion modal collects content from the operator
and needs to key it into `initial_artifacts` under whatever names
the destination process is expecting. The process catalog gains a
new optional field, `promotion_inputs`, on the process root:

```yaml
process: full
promotion_inputs:
  - name: draft_spec
    description: A discussed-and-agreed spec the operator wants verified
                 rather than re-elicited.
flows:
  main:
    ...
```

The dashboard reads `promotion_inputs` when rendering the promotion
modal: each declared input gets a field in the form. The operator
fills them; the dashboard passes the resulting dict as
`initial_artifacts`. If a process omits `promotion_inputs`, the
dashboard falls back to a single generic field keyed
`promotion_context` — the destination's first step can read that
under the same name if it chooses to participate in promotion at all.

The convention is declared in the catalog (machine-readable for the
dashboard) and honored in the prompt (where the agent reads it).
Both must agree per process, but the orchestrator does not enforce
the agreement — a process whose `promotion_inputs` declares
`research_question` while its first step's prompt only reads
`draft_spec` will receive content under the wrong name and ignore
it. Process authors own that contract per ADR-014's process-catalog
philosophy.

**The mechanism is destination-agnostic.** Promotion does not
hardcode SDLC, or any other process shape. Each destination process
chooses what artifact names its first step recognizes; promotion
just delivers whatever the caller put in `initial_artifacts`.
Concrete examples (none of which are special-cased in the runner):

| Destination | Artifact convention | First-step behavior |
|---|---|---|
| `full` (SDLC) | `draft_spec` | Spec step verifies the draft instead of eliciting |
| `deep-research` | `research_question` | Research step launches the fan-out using the chat-distilled question instead of asking the operator |
| `qa-review` | `review_brief` | Review step uses the chat-derived focus areas instead of running the generic checklist |
| `spec-only` | `draft_spec` | Same as `full`'s spec step (same prompt may be reused) |
| Custom operator process | Whatever the operator declared | Whatever the operator wrote into the first-step prompt |

The convention lives in the destination's first-step prompt, not
in the runner or the orchestrator. The runner has no knowledge of
"spec" or "research" — it just writes artifacts and dispatches.

#### Worked example: chat → `full`

`full`'s spec step gets a small prompt modification — one paragraph
near the top of `spec-system.md`:

> **Spec already present?** If the conversation history (or a
> `draft_spec` artifact) already contains a discussed-and-agreed
> spec — for example, this task was promoted from chat mode, or
> the operator provided a spec in the task body — your job is to
> **verify and finalize** rather than re-elicit. Read the existing
> draft, check it covers the goal, ask follow-ups only for
> genuine gaps, and emit `SPEC_COMPLETE:` with the validated
> content. Do not start the conversation from scratch.

Same prompt now serves both flows: fresh spec from scratch (no
draft present) and spec verification post-promotion (draft present).
The promotion path doesn't need a separate "skip spec" mechanism —
the spec step shortcuts itself when the artifact is already there.

Other destination processes follow the same shape with whatever
artifact name and first-step semantics make sense for them.

### 5. State-machine transition mechanics

`promote_task` is a CAS-guarded state transition like any other —
it uses `atomic_transition` (ADR-020) to update the row with:

- `metadata.process_name = to_process`
- `metadata.current_flow = main` (resets to the destination's main
  flow; sub-flow context from the prior process is discarded)
- `state = <destination's first job's queue_state>`
- `current_step = <destination's first job's name>`
- audit: `audit_on_win=AuditRow(message="Promoted from <prior> to <new>")`

Pre-conditions checked at the action site:
- Task exists.
- Destination process is loaded.
- Task is in a non-terminal state (not `complete`, not `abandoned`).

The transition is not state-aware on the source side — the operator
explicitly chose to promote; the orchestrator doesn't second-guess
which prior step was "okay to leave from." Any non-terminal state
is a valid source.

### 6. Isolation guarantees inherited from ADR-025

A chat-style process is just another Lotsa dispatch. It goes through
the same `agent_runner.py` with the same flags as `spec`, `plan`,
`code`, etc. — and inherits the same isolation stack:

- `--setting-sources project` (PR #96): operator-level `~/.claude/`
  settings, plugins, skills, and SessionStart hooks do **not** load
  into the chat agent. If the operator has the `superpowers` plugin
  installed (or any other personal customization), it stays out of
  the dispatch. Chat does not become a vector for the
  operator-config bleed that PR #92 closed.
- `--append-system-prompt` (PR #96): the chat step's prompt sits in
  the highest-authority slot, on top of the `claude_code` preset.
  Project `CLAUDE.md` flows in as conversation context — same as
  every other step.
- `LOTSA_DEFAULT_ALLOWED_TOOLS` (PR #100): `Monitor`,
  `ScheduleWakeup`, `Task`, `BashOutput`, `KillShell` are excluded.
  The chat agent cannot fall into the background-mode trap that
  cost tasks `<redacted>` and `<redacted>` the better part of two
  dispatches.
- OPERATIONAL_PREAMBLE: the no-branch, no-push, no-`cd`,
  no-cross-turn-deferral rules apply uniformly. Chat is still in a
  worktree under orchestrator git ownership.

What chat-style processes *can* do that SDLC-style processes
typically don't:

- **Stay conversational indefinitely.** No marker pressure to
  finalize an artifact. The step runs as a REPL until the operator
  promotes (this ADR) or abandons.
- **Accept additional synchronous tools via per-step opt-in.**
  ADR-021's per-step `allowed_tools` (currently kept on the runner
  API for future step opt-ins) is how a chat process would request
  `WebSearch`, `WebFetch`, `NotebookEdit`, or other sync-safe
  tools without modifying the Lotsa default. The opt-in mechanism
  *adds* to the default — it does not allow opting back into the
  cross-turn class, and it does not allow opting back into
  operator-level skills.

What chat-style processes should **not** do:

- **Re-enable operator-level skill loading.** A chat-process author
  who wants chat to feel like a brainstorming partner should bake
  that into the chat process's system prompt, not pull from the
  operator's installed `~/.claude/skills/`. The seal that ADR-025
  established is uniform across processes.
- **Relax the background-task ban.** `claude --print` is still
  one-shot for chat dispatches. The agent's "I'll wait for the
  notification" failure mode would re-appear identically.

This section is explicit because the question is reasonable to
raise. The answer is "chat inherits everything ADR-025 set up,
nothing relaxes." But the inheritance happens by virtue of going
through the same runner, not by any chat-specific guard — so it's
worth recording the property here rather than leaving it implicit.

### 7. What promotion does NOT do

- **No demotion.** A task that's been promoted to `full` cannot be
  demoted back to `chat`. The motivation: post-build iteration
  happens in the `full` flow's `verify` step (already
  conversational; already supports modify-and-re-route-to-review).
  Demotion would muddy what the task represents. If the operator
  wants to start a fresh conversation on related work, that's a new
  task — possibly linked to the previous via `task body: "see
  task abc123 for context"`.
- **No multi-hop with state preservation.** Each promotion enters
  the destination at step 1 and discards the source's flow context.
  An operator who promotes A → B → A would be re-entering A at A's
  step 1; the prior A-state is gone (though the audit log
  preserves the journey).
- **No magic translation of artifact names across processes.**
  Promotion delivers whatever `initial_artifacts` the caller passed;
  the destination's first-step prompt either recognizes those names
  or ignores them. A `draft_spec` artifact passed to a process
  whose first step expects `research_question` is a no-op — the
  prompt won't look for `draft_spec`. The orchestrator does not
  rename, alias, or transform artifacts between source and
  destination conventions. Each process's first step declares what
  it reads (§4); promotion just delivers.

## Implementation order

1. **Audit event type.** Add `process_promotion` to the message
   types the orchestrator emits. Surface it in the dashboard
   message list (visually distinct from state transitions).
2. **`promote_task` action method** in `OrchestratorService`,
   using `atomic_transition` per the §5 sketch. Tests cover:
   non-terminal source, unknown destination, unloaded destination,
   terminal-state source (rejected), audit row presence.
3. **`POST /api/tasks/{id}/promote`** route — body is
   `{to_process: str, initial_artifacts: dict | None}`. CLI mirror:
   `lotsa promote <task-id> <process>`.
4. **`spec-system.md` modification** — the verify-instead-of-elicit
   paragraph. Smoke test asserts the paragraph is present (joins
   the `TestOperationalPreamble`-style guardrails).
5. **Dashboard "Promote" button** — appears on tasks in
   non-terminal states. Modal lets the operator pick the
   destination process, with a confirmation message that names what
   stays (worktree, audit log) and what changes (active process,
   prompt set, state machine). Default copy: *"Promoting to
   `<destination>`. The conversation so far is captured as context
   for the new process. The current step ends; the new process
   starts at its first step."* Destination-specific copy (e.g. *"as
   the spec draft"* for `full`, *"as the research question"* for
   `deep-research`) can be supplied per process via a future
   catalog field; the default stays destination-agnostic to match §3.
6. **Process catalog gains `description:` and `promotion_inputs:`
   fields** — both optional, both at the process root. Loader and
   schema validation in `lotsa/flows.py`. Existing processes
   continue to work without these fields; the chat-process triage
   simply doesn't surface processes that lack a description.
7. **Chat-process catalog entry** — ship a bundled `chat` process
   with its own description, plus a `chat-system.md` prompt that
   includes the *available processes* block (rendered from the
   catalog at dispatch time) and the §3 triage behavior.
8. **Quickfix-process catalog entry** — bundled `quickfix` process
   matching the table in §3. Two steps: `code` and `review`, with
   prompts framed for *executing an instruction* rather than
   *building a thing.* (Same `review` job as `simple`/`standard` —
   the review prompt is generic; quickfix's narrowness comes from
   the coder prompt and the smaller diff size, not from a different
   reviewer.)

Steps 6, 7, and 8 can ship in one PR or be sequenced. The
description-field schema change (step 6) is the strict prerequisite
for the chat triage to work end-to-end; steps 7 and 8 are the
visible behavior on top.

## Consequences

### Positive

- Operators can start exploratory and grow into structured without
  losing context. The chat→build flow is one task, one worktree,
  one audit trail.
- The chat-mode capability (already buildable via a one-step
  conversational process) becomes substantially more useful, because
  the "exit ramp" exists.
- Sub-flow switching's semantics stay clean — it doesn't accumulate
  the user-driven "change the contract" use case as a confusing
  variant.
- The spec-handover pattern (artifact-seeded, prompt-aware) is
  reusable: future processes whose first step is a conversational
  spec step inherit the affordance for free.

### Negative

- A new action that mutates `metadata.process_name` is a class of
  state change the orchestrator hasn't done before. It needs
  careful audit-log framing and dashboard visibility so it doesn't
  look like silent corruption when reading task history.
- The `spec-system.md` change makes the spec prompt slightly more
  complex — two branches (elicit vs. verify) instead of one. Mitigated
  by keeping the elicit path unchanged and adding the verify branch
  as an opt-in detected by artifact presence.
- Operators who promote and then realize they wanted a different
  destination have no undo path — the task is now in the new
  process's flow. They can abandon and re-promote a new task, but
  the abandoned task's audit log fragments the history.

### Migration

This is additive. No existing tasks change behavior; no existing
processes change. Operators who don't promote anything see no
difference. The dashboard "Promote" button can ship hidden behind
a feature flag for a release if a phased rollout is desirable.

## Out of scope

- **Demotion** (full → chat). See §7; rejected on simplicity
  grounds, with the verify-step REPL as the in-flow iteration
  path.
- **Agent-initiated promotion** — agents may suggest in their
  output, but the orchestrator does not parse such suggestions
  as triggers. Adding it would re-introduce the "agent vs.
  operator authority" question that ADR-025's layered model
  resolved in operator-favor for operational decisions.
- **Cross-process sub-flow edges** — a sub-flow rule target that
  names a job in a different process remains a parse-time error
  (per ADR-021 §3). Promotion is the only way to cross processes;
  sub-flows are within-process.
- **Multi-task linkage** — formal parent/child relationships
  between tasks (e.g. "this full-flow task was promoted from
  chat task X"). The audit log records the promotion within a
  single task; cross-task linkage stays informal ("see task
  abc123" in task body) until a real need surfaces.
- **State-aware promotion preconditions** — refusing to promote
  from certain source steps. The current design accepts any
  non-terminal source; if a real failure mode surfaces (e.g.
  promoting mid-test is destructive), we add it then.
- **Initial-artifact validation** — the `draft_spec` content is
  trusted from the operator. The spec agent will verify it
  semantically. If we later want format validation (e.g. spec
  must include certain sections), that's an orthogonal concern.
