# ADR-014: Jobs as the unified flow primitive

**Status**: Implemented
**Date**: 2026-05-18 (Accepted) → 2026-06-07 (Implemented)
**Implementation**: PR #72 (Layer A spec + typed-job dispatch), PR #76 / #77 (monitor engine via registry), PR #82 (multi-process catalog + API + CLI). Per-task simultaneous dispatch (originally Layer B) deferred to ADR-021.
**Related**: ADR-013 (orchestrator owns git state), `lotsa/flows.py`, `lotsa/orchestrator.py`, `lotsa/pr_monitor.py`

---

## Context

Lotsa's flow abstraction has two parallel concepts that mostly do the
same thing — they're nodes in a per-task state machine — but with
different rules for how they're declared, how the orchestrator
dispatches them, and how rule targets can address them.

**Jobs** are declarative. They live in `flow.yaml` under `jobs:`. Each
has a name, prompt, output rules, queue/active states. Examples:
`spec`, `code`, `review`, `pr-fix`, `verify`. Every job dispatches an
LLM agent subprocess.

**Synthetic states** are imperative. They are conjured into the state
machine by `lotsa/flows.py:_build_state_machine` *only* when a `pr:`
block is present in the flow YAML. Examples: `pushing`,
`waiting_for_pr`, `rebasing`, `abandoned`, `complete`. Some get
hardcoded dispatch shapes (the orchestrator has an `if item.state ==
"pushing":` branch that runs `_execute_push` instead of dispatching an
agent); some are passive (`waiting_for_pr` is fall-through-do-nothing
while `PrMonitor` polls externally).

This asymmetry leaks through the system:

- `resolve_output_target` accepts `next`, `previous`, `blocked`, or a
  *job name*. Synthetic states cannot be named in rule targets — they
  exist only as transition endpoints synthesised by `_build_state_machine`.
- The orchestrator dispatch loop special-cases `pushing` ahead of the
  generic job lookup.
- The PR monitor lives outside the dispatch loop and reaches into the
  orchestrator via duck-typed `getattr` for its transitions.
- New action-style steps (push, future "merge", "tag-release",
  "deploy-stage") would each require a new special-case branch.
- New monitor steps (waiting for a webhook, polling an external API,
  waiting for human approval) would each require a new bespoke
  integration point.

We've also outgrown the implicit "Lotsa is a software-engineering
pipeline" framing. Lotsa's value is **governed AI execution** —
running LLM-driven work through approval gates, audit trails, and
deterministic glue. The work doesn't have to be code-shaped: research
processes synthesise findings; outreach processes draft + review +
send messages; ops processes triage + investigate + remediate. The
state machine + audit log + governance model are domain-agnostic; the
*specific job mix* is what makes a process about code vs. research vs.
operations.

If jobs are the only flow primitive and the YAML language admits
multiple job types, users can compose processes for any domain
without asking us to special-case their use case.

---

## Decision

**A process is a state machine whose states are jobs.** A job
declares *how it dispatches* via a `type:` field with three values:

| Type | What it does | How the orchestrator runs it |
|---|---|---|
| `agent` | LLM-driven step with a system+user prompt and a tool set | `_dispatch_step` spawns a Claude subprocess (or future agent runners) |
| `action` | Pure Python tool — one-shot, no LLM | Looks up `tool: <name>` in the tool registry, calls the bound callable with the task context |
| `monitor` | Passive wait for an external signal | An engine (e.g. `PrMonitor`, future `WebhookMonitor`, `SchedulerMonitor`) drives transitions; orchestrator does not actively dispatch |

Each type slot has uniform interfaces so jobs of any type can appear
anywhere in a process's flow sequence, route via the same output-rule
mechanism, and contribute states to the same state machine.

**State contribution per type.** Each job contributes one or more
states to the state machine. The orchestrator's dispatch loop reads
the task's current state and decides what (if anything) to do:

- **`agent` and `action` jobs** contribute a `queue_state` (the task
  is parked here pending dispatch) and an `active_state` (the task
  is here while the agent/handler executes). The dispatch loop
  recognises the `queue_state` and triggers execution; it recognises
  the `active_state` and waits for the in-flight task to complete.
  Both names default to the job's name when not set explicitly.
- **`monitor` jobs** contribute a single state (their `queue_state`,
  equal to the job name). There is no `active_state` because the
  job does not execute — the engine drives transitions from
  outside. When a task enters a monitor's state the dispatch loop
  does **nothing**: the task parks until the engine calls the
  orchestrator's `transition_task` API. The engine is responsible
  for polling, debouncing, and routing into the next flow step (or
  back to the parent flow per the sub-flow exit rule below).

This pins the contract the original draft hand-waved: the orchestrator
treats `monitor` states as inert; only registered engines may move
tasks out of them.

The `pr:` config block is removed. Its contents (poll interval,
triggers, base branch, budget caps) become attributes of a monitor
job (`type: monitor`, `engine: pr_monitor`, config block underneath).
Equivalently, push becomes a `type: action` job whose `tool:` resolves
to a registered `push_pr` handler. The synthetic-state generation in
`_build_state_machine` goes away — the state machine is *fully
derived* from the jobs list.

### Vocabulary

| Term | Meaning |
|---|---|
| **process** | The top-level YAML file's content. A self-contained domain definition (software_process, research_process, content_process). Holds jobs, flows, and any process-specific tool registrations. |
| **flow** | A named sequence of jobs within a process. A process can have multiple flows that share jobs — e.g. the SE process has `main` (spec → ... → push) and `pr_fix` (pr-fix → review → push). |
| **job** | A node in a flow's state machine. Has a `type:` (agent / action / monitor) that determines how the orchestrator dispatches it. |
| **tool** | A named binding to a Python callable, used by `action` jobs to do their work. Built-in tools ship with Lotsa; custom tools register at startup. |

### Naming conventions

- **`agent` job names** follow the existing codebase pattern:
  kebab-case or single words (`spec`, `pr-fix`, `verify`). The name
  must match the prompt-file stem (`spec-system.md`, `pr-fix-system.md`)
  so the prompt registry resolves correctly. Job names are reused as
  state names in the state machine, so they double as `queue_state` /
  `active_state` keys.
- **`action` and `monitor` job names** use snake_case, matching the
  tool or engine registry entry they bind to (`push_pr`,
  `wait_for_pr_signal`). The visual mapping `tool: push_pr` →
  `name: push_pr` is intentional; mismatching them would be a source
  of confusion. Like agent jobs, these names become state-machine
  keys.
- **Flow names** use snake_case (`main`, `pr_fix`). The `pr-fix` job
  and the `pr_fix` flow are deliberately distinct names — they refer
  to different entities and live in different YAML namespaces. The
  two-character spelling difference is the disambiguator.
- **Process names** use snake_case (`software_process`,
  `research_process`).
- **Tool names** use snake_case (`push_pr`, `set_pr_label`).
- **Engine names** use snake_case (`pr_monitor`).
- **Synthesised state names** (when an `action` or `monitor` job's
  `queue_state` isn't set explicitly) default to the job name — so
  a job named `pr-fix` produces a `pr-fix` state. Kebab-case states
  coexist with snake_case states (`waiting_for_pr`) today; no
  forced rename.

### YAML shape (sketch)

```yaml
process: software_process

jobs:
  - name: spec
    type: agent
    prompt: spec
    rules:
      - source: stdout
        pattern: "^SPEC_COMPLETE:"
        target: next
  - name: code
    type: agent
    prompt: code
    rules: [...]
  - name: review
    type: agent
    prompt: review
    rules:                       # safe defaults; flows may override
      - source: stdout
        pattern: "^REVIEW_PASS"
        target: next             # resolves within the current flow
      - source: stdout
        pattern: "^REVIEW_FAIL"
        target: blocked          # flows override to their rework step
  # ... plan, test, verify, pr-fix omitted for brevity; all are
  # existing agent jobs in lotsa/prompts/full/flow.yaml ...
  - name: push_pr
    type: action
    tool: push_pr               # name from the tool registry
  - name: wait_for_pr_signal
    type: monitor
    engine: pr_monitor          # name from the engine registry
    config:
      poll_interval_seconds: 30
      debounce_seconds: 120
      triggers: [human_comment, bot_comment, review_decision, failing_check]
      # max_pr_fix_rounds and max_consecutive_skipped are planned in
      # docs/superpowers/plans/2026-05-12-autonomous-pr-fix-loop.md
      # (originally as additions to PrConfig). This ADR supersedes
      # that config path — when the refactor lands, these caps live
      # on the monitor job's config block rather than in PrConfig.
      max_pr_fix_rounds: 10
      max_consecutive_skipped: 3

flows:
  main:
    steps: [spec, plan, test, code, review, verify, push_pr, wait_for_pr_signal]
  pr_fix:
    steps: [pr-fix, review, push_pr, wait_for_pr_signal]
```

### Tool registry

`action` jobs reference tools by name, not by Python import path.
This makes process YAML refactor-stable (a tool's module moves, the
YAML doesn't change) and friendlier to operators authoring processes
without writing Python.

A tool is a named binding to a Python callable, registered at
process-load time. Lotsa ships a starter set of built-in tools:

- `push_pr` — git push + PR creation; what `lotsa.push_step.execute_push` does today.
- `set_pr_label` — apply a GitHub label to the task's PR.
- `set_branch_status` — write a commit status on the task's branch (e.g. "lotsa/in-review").

These are the SE-process primitives that already exist or are
imminent. Generic tools (`post_slack`, `send_email`, `http_get`)
ship as concrete use cases land — the mechanism is the same, the
catalogue grows.

Custom tools register at startup via one of:

- A `tools:` block in `lotsa.yaml` mapping names to import paths
  (`my_tool: my_package.tools.do_thing`).
- A `--tools-dir` config option pointing at a directory of Python
  modules that register tools on import.

The choice between these is an implementation detail covered by the
follow-on plan. The contract a tool must satisfy is:

```python
async def my_tool(task: TaskContext, config: dict[str, Any]) -> ToolResult: ...
```

with these types:

```python
@dataclass
class TaskContext:
    task_id: str
    worktree: Path
    metadata: dict[str, Any]    # fresh-read from DB at job entry
    db: TaskDB                  # for writing audit messages
    process_name: str
    flow_name: str              # root flow (e.g. "main")
    current_flow: str           # may differ during sub-flows
    last_run_step: str          # most recently dispatched step

@dataclass
class ToolResult:
    success: bool
    output: str
    metadata: dict[str, Any]    # same shape AgentResult has today,
                                # minus the LLM-specific fields
```

`TaskContext` exposes only what a tool needs to do its work and
write audit trail. It does **not** expose the full ORM session or
secrets/credentials — those live behind narrower interfaces.

The `dict[str, Any]` signature is the minimum contract. **Built-in
tools should define a typed config dataclass and validate the raw
dict against it at call time** — e.g. `PostSlackConfig(channel: str,
template: str)` for the `post_slack` tool. Custom third-party tools
may use the raw dict if their config is small or fully optional, but
typed configs catch YAML typos and document the schema in one place.

**Exception path.** A tool that raises instead of returning a
`ToolResult` is treated as a failure: the dispatch loop catches the
exception, synthesises `ToolResult(success=False, output=str(exc),
metadata={"exception_type": type(exc).__name__})`, writes a system
error message to the task's audit log, and transitions the task to
`blocked` (the same terminal state any `action` job's
`success=False` outcome routes to). The implementation plan should
not invent its own exception semantics; this is the contract.

Each tool documents its config schema. The `action` job's YAML
carries the tool name + tool-specific config:

```yaml
- name: post_release_note
  type: action
  tool: post_slack
  config:
    channel: "#releases"
    template: "task-complete"
```

Long-term, the tool registry could be shared with agent tool-use —
so an agent could call `push_pr` mid-response the same way an
`action` job does. The shapes are compatible; only the dispatch
point differs (orchestrator vs. LLM). Unification is noted as future
direction but not required by this ADR.

### Sub-flow support

Job definitions are scoped to their process; `flows:` declares named
sequences over those jobs. `target: next` resolves *within the
currently active flow* — the orchestrator tracks
`task.metadata.current_flow` and the resolver reads it.

This is what makes context-aware routing work: `review`'s `REVIEW_PASS
→ target: next` resolves to `verify` in the `main` flow and to
`push_pr` in the `pr_fix` flow, **with the same rule and the same
job definition**. No `target_by_origin` lookup table; no duplicated
review job.

**Stateless = declarative, not acyclic.** A flow is stateless when
every transition is declared in YAML. The DAG may contain cycles
(`code → review → code`) as long as each edge has an explicit,
named target. The orchestrator never consults dispatch history to
decide where to send a task next — the rule says exactly. The
earlier `target: previous` keyword is removed; the autonomous
code↔review rework loop now spells its target by name (`code` in
`main`, `pr-fix` in `pr_fix`).

What disappears with this change:

- `target: previous` keyword and any resolver branch for it.
- `task.metadata.previous_step` tracking.
- Any positional resolution at rule-fire or retry time.

What stays:

- Cycles. `review.REVIEW_FAIL → code` in `main` and
  `review.REVIEW_FAIL → pr-fix` in `pr_fix` are both fine: explicit,
  declarative, named.

**Per-flow rule overrides for shared jobs.** `review` is used in
both `main` and `pr_fix` with different REVIEW_FAIL targets. Rules
attached to a job are *defaults*; rules attached to a flow's step
binding override them for that flow:

```yaml
jobs:
  - name: review
    type: agent
    prompt: review
    rules:                          # safe defaults
      - source: stdout
        pattern: "^REVIEW_PASS"
        target: next
      - source: stdout
        pattern: "^REVIEW_FAIL"
        target: blocked

flows:
  main:
    steps:
      - name: spec
      - name: plan
      - name: code
      - name: review
        rules:                      # override for this flow
          - source: stdout
            pattern: "^REVIEW_PASS"
            target: next
          - source: stdout
            pattern: "^REVIEW_FAIL"
            target: code            # autonomous rework loop
      - name: verify
      - name: push_pr
      - name: wait_for_pr_signal
  pr_fix:
    steps:
      - name: pr-fix
      - name: review
        rules:
          - source: stdout
            pattern: "^REVIEW_PASS"
            target: next
          - source: stdout
            pattern: "^REVIEW_FAIL"
            target: pr-fix          # rework loop within pr_fix
      - name: push_pr
```

Resolution order: when the orchestrator evaluates a step's output,
it uses the step's flow-binding rules if present; otherwise it
falls back to the job's defaults. The bare-list form
`steps: [spec, plan, code, ...]` remains valid sugar for flows that
need no per-step overrides.

Two consequences worth calling out:

1. **The `pr_fix` sub-flow simply omits `verify`.** PR #62 (commit
   `db8400c`, merged on main) added a stopgap that detects pr-fix
   context via `task.metadata.pr_number` and auto-advances `verify`
   past its conversational gate in that context. The sub-flow form
   above supersedes that heuristic: `pr-fix → review → push_pr`.
   No `pr_number` check needed once this ADR's implementation lands.
2. **`verify.NEEDS_REVIEW → review` is already a named target** —
   it stays as-is. The only rule that changes form under this ADR is
   `review.REVIEW_FAIL`.

**Retry semantics.** When a task lands in `status=blocked` for any
reason — a rule with `target: blocked`, an agent crash, a budget
exit, a push failure — retry-from-blocked re-dispatches
`task.metadata.last_run_step` in `task.metadata.current_flow`.
There is no special-case branch for "review failed" vs. "subprocess
crashed"; one code path serves both. Pre-retry context changes
(e.g. manually re-dispatching `code` to refresh its inputs) are
operator actions.

The two metadata fields involved are:

- `current_flow: str` — the flow the task is currently in. Set at
  task creation (root flow) or at sub-flow dispatch.
- `last_run_step: str` — the step the orchestrator most recently
  dispatched. Updated on every dispatch.

That's the entire retry-relevant state surface. No `previous_step`,
no dispatch history.

**Failure-reason handoff.** The autonomous code↔review loop requires
that `code` sees why `review` rejected the prior output. The
stateless-flows design itself is agnostic to *where* that data
lives — rules describe transitions, not data flow.

*For now* (this ADR): the existing audit/message log is the carrier.
Every job's stdout is persisted to the task's message history
before any downstream rule transition fires; resumed subprocesses
(`code` is `resume: true`) load recent messages into their context.

*Forward direction*: worktree-persisted artifacts
(`docs/tasks/<task-id>/spec.md`, `review.md`, ...) are the likely
canonical channel long-term — versioned in git, reviewable on the
PR, no DB query at agent runtime. A separate ADR will pin
task-scoped paths (to avoid post-merge collisions), per-job commit
policy (some artifacts ship with the PR, others stay scratch), and
a sensitive-content allow-list. Forward-pointer here so reviewers
understand the current carrier is a deliberate interim choice.

**The monitor job owns sub-flow context.** When a monitor's engine
dispatches a task into a sub-flow (e.g. `PrMonitor.dispatch_pr_fix`
routing into the `pr_fix` flow's entry), the engine sets
`task.metadata.current_flow` to the sub-flow's name. When the task
re-enters the monitor's state at the end of the sub-flow (the
sub-flow's terminal step transitions to the monitor's queue state),
the monitor resets `current_flow` to the parent flow's name.
Monitors are the natural rendezvous between flows — making them the
gatekeeper avoids needing a `return_to_flow:` annotation on every
terminal job.

### Multiple processes in one running app

The running app holds a registry of N processes, loaded at startup
from a `--processes-dir` config option (a directory of YAML files)
or an explicit list in `lotsa.yaml`. A single Lotsa install can host
a software process, a research process, and a content process
simultaneously.

Concretely:

- Each task records both `task.metadata.process_name` and
  `task.metadata.flow_name` so dispatch can resolve the right
  flow within the right process.
- `OrchestratorService.flow` (singleton today) becomes
  `OrchestratorService.processes` (dict-of-dicts, keyed by process
  name then flow name).
- Jobs are scoped to their containing process. The
  `software_process`'s `review` is a different job from
  `research_process`'s `review` — they may use different prompts,
  rules, and tool sets. There is no global job namespace and no
  cross-process job references; if reuse is needed, copy the job
  definition (the copy is small, the coupling cost is large).
- The React UI gains a process selector on the new-task form.
  Existing tasks remain pinned to whatever process created them.

This unblocks (but does not deliver) a future **project** concept,
where each project has its own default process, working directory,
and credentials. Projects are tracked in a separate ADR.

---

## Why this isn't a niche concern

Lotsa today is used for software processes, but the substrate is
generic. The three job types map cleanly onto domains that have
nothing to do with code:

| Process (domain) | `agent` jobs | `action` jobs | `monitor` jobs |
|---|---|---|---|
| **Software engineering** | spec, plan, code, review, verify, pr-fix | push_pr, merge, tag_release, deploy_stage | wait_for_pr_signal, wait_for_ci, wait_for_oncall_ack |
| **Research synthesis** | literature_scan, summarise, generate_hypothesis, write_findings | save_to_notion, post_to_slack | wait_for_stakeholder_approval, wait_for_new_papers |
| **Outreach / sales** | draft_message, qualify_lead, follow_up | send_email, log_to_crm, schedule_meeting | wait_for_reply, wait_for_meeting_outcome |
| **Operations / incident response** | triage_alert, investigate, propose_remediation | apply_runbook_step, page_oncall, post_status_update | wait_for_human_decision, wait_for_metric_recovery |
| **Content production** | research, draft, edit, fact_check | publish_to_cms, schedule_post | wait_for_editor_approval, wait_for_publication_window |

A process author picks the job mix that suits their domain. Lotsa's
governance model (approval gates, audit log, append-only message
history, sandboxed execution, budget caps) applies uniformly across
all three job types. The platform doesn't have to know whether the
process is making software, drafting newsletters, or running an
oncall rotation.

The corollary: **tools and monitor engines are extension points**.
Lotsa ships first-party tools and engines for the shapes we use today
(`push_pr`, `pr_monitor`) and first-party agent runners
(`ClaudeCodeRunner`). Operators running a non-engineering process
register their own tools and engines at startup; the orchestrator
doesn't need to know about them.

---

## Tradeoffs

**Pros:**

- One mental model: a process is a sequence of jobs. No "is this a
  job or a synthetic state?" question for authors.
- The state machine becomes fully declarative — every state is a
  job's queue_state, active_state, or terminal state.
- Future capabilities (new action tools, new monitor engines) plug
  in without changes to the orchestrator's dispatch loop.
- The platform genuinely generalises beyond software engineering
  with no architectural cost.
- Rule wiring is uniform. Every job's `target:` accepts the same set
  of values; no special syntax for "route to the push state."
- Multi-flow context-aware routing (`target: next` resolving
  per-flow) handles the pr-fix-skips-verify case and any future
  similar case with the same primitive.
- Multiple processes per running app are first-class — the same
  install can host SE, research, and content processes
  simultaneously, with per-task process selection.

**Cons:**

- A real refactor of working code. Estimated ~400 LOC across
  `flows.py`, `orchestrator.py`, `pr_monitor.py`, `push_step.py`, plus
  meaningful test changes.
- Breaking change to flow YAML schema. The `pr:` block is removed in
  favour of a monitor job. The in-tree flows under `lotsa/prompts/`
  are rewritten as part of the implementation. See **Backward
  compatibility** below for why no migration path is required.
- Monitor jobs introduce a new dispatch path: their engines run
  *outside* the orchestrator's dispatch loop (because monitors don't
  "execute" in a one-shot sense). The contract is now spelled out
  in the Decision section's state-contribution paragraph — engines
  call the orchestrator's existing `transition_task` API to move
  tasks out of the monitor's state. `PrMonitor` already follows this
  shape today; the refactor formalises it as the monitor-engine
  contract.
- The tool and engine registries add a small extension surface. Easy
  to get right, but a new contract the docs need to cover.

---

## Backward compatibility

**No backward-compatibility work is required.** Lotsa is in closed
beta. There are no production deployments running the current `pr:`
block schema and no live tasks in flight on the synthetic `pushing`
/ `waiting_for_pr` / `rebasing` states that would need migration.

Concretely, this means:

- **No parser shim.** The `pr:` block parser is removed outright when
  the new schema lands. Old YAML files are rewritten in the same PR
  series; no dual-parse phase, no deprecation window.
- **No `lotsa migrate-flow` CLI helper.** Nothing for operators to
  migrate. The in-tree flow files under `lotsa/prompts/` are
  rewritten by the implementation PR.
- **No in-flight task backfill.** Closed-beta tasks that exist today
  can be re-created against the new schema if they're still relevant;
  forcing a backfill script onto a non-shipped system is wasted work.

If/when Lotsa exits closed beta, a future ADR will revisit the
deprecation and migration story for any schema changes that follow.
The "no migration needed" call here is bounded to this specific
schema change at this specific point in the product's life — it is
not a precedent for future breaking changes.

---

## Scope

This ADR proposes the architectural direction. It does **not** ship
the implementation. A subsequent implementation plan and PR series
will:

1. Extend `Job`/`ResolvedJob` with `type`, `tool`, `engine`, `config`
   fields and the `flows:` block parser in `flows.py`. Introduce a
   `Process` container holding jobs, flows, and any
   process-specific tool registrations.
2. Rewrite the orchestrator dispatch loop as a dispatch-table over
   `job.type`. Eliminate the `if state == "pushing":` special-case.
3. Build the tool registry. Migrate `_execute_push` into a registered
   `push_pr` tool.
4. Refactor `PrMonitor` to be a monitor-job engine driven by config
   rather than the `pr:` block. Move budget caps and triggers from
   `PrConfig` to monitor job attributes.
5. Eliminate the synthetic-state generation in
   `_build_state_machine`. The state machine is purely derived from
   jobs.
6. Introduce `task.metadata.process_name` + `task.metadata.flow_name`
   + `task.metadata.current_flow` and update `resolve_output_target`
   to use them.
7. Make `OrchestratorService` host multiple processes; load from
   `--processes-dir` or `lotsa.yaml`.
8. Update the React UI to surface process selection on the new-task
   form.
9. Rewrite the existing flow files under `lotsa/prompts/full/`,
   `lotsa/prompts/standard/`, `lotsa/prompts/simple/` as processes
   in the new schema.

In scope for **Lotsa Community Edition** (`lotsa/`) as the proving
ground.

---

## Out of scope

- **First-party non-engineering processes.** This ADR makes them
  *possible*; it doesn't ship a `research_process` or
  `outreach_process` template. Templates are a follow-on; the design
  here is purely the primitives.
- **Cross-task workflows.** Today a task is the unit of execution.
  Multi-task workflows ("research project comprising 12 sub-tasks")
  remain a separate concern.
- **Schema versioning for the process YAML.** Adding a
  `schema_version:` field is worth doing once external authors
  start writing processes, but not before.
- **Project concept.** Multi-process infrastructure unblocks it; the
  project layer itself (per-project default process, working
  directory, credentials) is its own ADR when we tackle it.
- **Unifying flow tools with agent tools.** Interesting long-term —
  the shapes are compatible enough that an agent could call the same
  `push_pr` tool an action job does. Not required by this ADR.
