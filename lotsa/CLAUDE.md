# lotsa/CLAUDE.md — Lotsa Community Edition

This directory is **Lotsa Community Edition** — a local task runner and web
dashboard for Claude Code. No GitHub-required, no external database, no
hosted infrastructure: a local SQLite store, a worktree-per-task workspace,
and a FastAPI/React dashboard. Most active development in this repo happens
here.

Read this together with the root [CLAUDE.md](../CLAUDE.md) (cross-cutting
workflow) and [CONSTITUTION.md](../CONSTITUTION.md) (non-negotiable rules).

CE depends on [`rigg/`](../rigg/CLAUDE.md) for the shared
StateMachine, OrchestrationEngine, AgentRunner, and git utilities — that SDK
boundary is real, so keep CE product concerns out of `rigg/`.

---

## What CE is

Lotsa CE is a thin product layer on top of the [Rigg SDK](../rigg/):

```
┌─────────────────────────────────────────┐
│  lotsa/                                  │  CLI, config, SQLite store,
│  (this package)                          │  dashboard, console output,
│                                          │  orchestrator, push step,
│                                          │  PR monitor
├─────────────────────────────────────────┤
│  rigg/                              │  StateMachine,
│  (SDK)                                   │  OrchestrationEngine,
│                                          │  ClaudeCodeRunner,
│                                          │  WorktreeManager,
│                                          │  models, git, blocking
└─────────────────────────────────────────┘
```

CE supplies the three concrete implementations the rigg SDK needs:

| Rigg protocol | CE implementation       | Module                          |
|--------------------|-------------------------|----------------------------------|
| `ItemSource`       | `SQLiteItemSource`      | `lotsa/db.py`                    |
| `Notifier`         | `ConsoleNotifier`       | `lotsa/console_notifier.py`      |
| `AgentRunner`      | `ClaudeCodeRunner`      | re-exported from `rigg/`    |

Everything else in this directory is product surface (CLI, dashboard, flow
definitions, push step, PR monitor) built on top of those primitives.

---

## Directory layout

```
lotsa/
├── cli.py              — click CLI: lotsa init, lotsa serve, lotsa build
├── config.py           — LotsaConfig loader (reads lotsa.yaml from
│                         data_dir, default ~/.lotsa)
├── db.py               — SQLite store: TaskDB + SQLiteItemSource
├── migrations.py       — schema migrations applied at open time
├── orchestrator.py     — OrchestratorService: dispatches agents, owns
│                         state transitions, owns git
├── flows.py            — YAML-driven Job/Flow model + state machine
│                         derivation
├── push_step.py        — deterministic git push + GitHub PR open
├── pr_monitor.py       — polls open PRs, classifies signals, dispatches
│                         pr-fix
├── github_client.py    — minimal GitHub REST client
├── docker_runner.py    — sandboxed agent execution via docker run
├── console_notifier.py — Notifier protocol impl that prints to stdout
├── status.py           — TaskStatusLiteral enum + helpers
├── server/             — FastAPI dashboard backend
│   ├── app.py          — app factory, mounts static frontend
│   ├── api_routes.py   — /api/* routes (tasks, messages, events, SSE)
│   ├── schemas.py      — Pydantic models for dashboard requests
│   └── static/dist/    — built frontend output (**gitignored** — run `npm run build` in `lotsa/frontend/` before `lotsa serve`)
├── frontend/           — Vite + React + shadcn/ui dashboard (ADR-012)
├── prompts/            — bundled process presets and standalone prompts:
│                         simple/, standard/, full/ — each carries a
│                         process.yaml + {step}-system.md + {step}-user.md
│                         (ADR-014 process catalog). chat/ holds the
│                         task-creation prompt; review/ holds the
│                         /review skill (SKILL.md + checklist.md), not
│                         a process.
├── tests/              — pytest, mirrors module layout
├── Dockerfile.agent    — base image for --docker mode
└── README.md           — user-facing quickstart and CLI reference
```

---

## Core conventions

### Orchestrator owns git state (ADR-013)

The orchestrator is the only component that mutates git state at the
worktree level: it creates worktrees, switches branches, commits, pushes,
and pulls. **Agents only stage and commit within the worktree they're
given** — they never push, never branch, never resolve conflicts.

This was a real bug: an agent following an old CLAUDE.md created a side
branch, broke the push silently, and the loss was only caught later. The
rule is enforced both at the prompt level (operational preamble) and
structurally (push step is deterministic, no agent involvement).

When adding a flow step that touches git, the question to answer first is
"is this deterministic enough to be a non-agent step?" If yes, make it an
`action` job backed by a tool (see ADR-014). If no, put the agent's git
authority on the shortest possible leash and add a deterministic step
after it to push.

### Orchestrator keeps the task branch synced with upstream (ADR-018)

Owning git state (ADR-013) includes keeping it *current*. **Stale local
refs never determine task behaviour.** Every lifecycle event that resolves
a branch state from local git first reconciles with
`origin/<default_branch>` (the task's project
`WorktreeManager.default_branch`, resolved via `_worktree_manager_for`; `main`
by default):

- **Worktree creation** — base the new worktree off `origin/<default_branch>` (PR #70).
- **Pre-pr-fix dispatch** — fetch + merge before the pr-fix agent runs (ADR-015).
- **Pre-retry-from-blocked** and **pre-rebase-after-restart** — `retry()`'s
  push-retry branch re-runs `_sync_branch_to_main` before re-pushing. A task
  blocked after a push failure funnels here. (Since ADR-040, a modern mid-push
  task is no longer flipped to `blocked` by restart recovery — it is
  *resumed*: the push action re-runs idempotently, and the push step's own
  `NON_FAST_FORWARD → rebasing` handling covers divergence. Only a
  cap-exceeded or legacy push-state row reaches `blocked` + `retry()`.)

Sync is deterministic and orchestrator-owned — never delegated to an agent.
Failure degrades best-effort: a fetch/merge error blocks the task with a
warning (the worst case is "ran on slightly stale upstream", never worse
than the pre-fix status quo); a merge conflict dispatches the
`resolve_conflicts` agent step (or blocks when the process has none). The
**push step itself is excluded** — the first forward push stays unsynced and
owns its own `NON_FAST_FORWARD → rebasing` handling. When adding a new
git-touching lifecycle event, ask "is this event in ADR-018's table?" and
wire `_sync_branch_to_main` in if so.

The above syncs against `origin/<default_branch>`. The **commit posthook's
publish** also reconciles against the task's *own* remote branch
(`origin/lotsa/<task_id>`): when an operator pushes to the PR branch directly
(e.g. GitHub's resolve-conflicts button), the next publish hits
`NON_FAST_FORWARD`, so `reconcile_branch_with_remote` (`lotsa/push_step.py`)
fetches and rebases the worktree onto the remote tip and retries once — local
commits whose content the remote already has drop out as empty. A real
content-divergence conflict can't rebase, so it falls back to `git merge` —
leaving the conflict in the worktree as markers (the same shape
`_sync_branch_to_main` leaves for the origin/main case) — and raises
`ReconcileConflict` carrying the unmerged paths. The drainer's posthook-failure
handler routes that `publish_conflict` to `_handle_conflict_dispatch`, so the
`resolve_conflicts` agent edits the markers and the `commit` posthook completes
the merge — instead of dead-ending at `blocked`, where Retry/Revise would just
re-trigger the same non-fast-forward and loop. (Regressions: tasks `<redacted>`,
`c79aaf7f`.)

### State transitions are atomic CAS (Constitution §3.1)

`TaskDB.atomic_transition()` is the named CAS helper (ADR-020). Every
transition with a precondition ("only advance if currently waiting") goes
through it. It wraps the CAS UPDATE and any paired audit-row INSERT in a
single transaction, so the audit trail can never drift from the state
change. It returns a `TransitionResult`; check `result.won` before any side
effect.

```python
result = await db.atomic_transition(
    task_id,
    from_status="waiting",
    from_state="planned",
    to_state="testing",
    to_status="working",
    to_current_step="test",
    audit_on_win=AuditRow(
        role="system",
        step_name="test",
        content="Advancing to testing",
        msg_type="status_change",
    ),  # or audit_on_win=None when no message is paired with the win
)
if not result.won:
    return  # Lost the race — another caller already advanced this task
```

The CAS-loser writes nothing (`audit_on_win` is only written when the CAS
wins). Check `result.won` before any side effect (dispatch, event emission,
branch switch).

`atomic_transition` delegates to the lower-level `claim_task_transition`
primitive, which stays defined in `lotsa/db.py` only. Production code must
never call `claim_task_transition` directly — that reintroduces the
audit-drift failure mode ADR-020 closed. `lotsa/tests/test_adr020_enforcement.py`
fences it: it fails if any production module outside `lotsa/db.py` names it.

### Single-process dispatch guards (Constitution §3.3)

Within `OrchestratorService` there are three `set`-based guards:
`_dispatching_pr_fix`, `_dispatching_push`, `_dispatching_jump`. Each
prevents re-entrant dispatch between awaits for the same task. Add to the
set **before** any await that could yield to a competing call; discard in
`finally`.

### Messages are append-only

The `messages` table is the audit trail. Application code calls
`TaskDB.add_message(...)` (INSERT only). There is no `update_message`,
no `delete_message`, and there should never be. This includes "cleanup",
"fixup", and "compaction" — the row written yesterday is the row that's
there today.

Tables and their mutability:

| Table         | Mutability                                              |
|---------------|---------------------------------------------------------|
| `tasks`       | mutable (state, status, current_step, metadata, etc.)   |
| `messages`    | append-only (no UPDATE, no DELETE)                      |

This instantiates Constitution §1.5 and §4.1 for the CE schema.

### Git subprocess discipline

Every git call uses the async subprocess API with arguments passed as
separate positional tokens. Never `subprocess.run`, never a shell string,
never an f-string interpolated into a command line. `lotsa/push_step.py`
is the canonical reference:

```python
proc = await asyncio.create_subprocess_exec(
    "git", "rev-parse", "HEAD",
    cwd=work_dir,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
```

This satisfies both injection prevention (Constitution §1.1) and async
hygiene (§2.1) in one rule.

### Agent output markers (the orchestrator's structured-output contract)

Agents communicate exit conditions to the orchestrator via single-line
markers in stdout. The orchestrator scans for the **last** matching marker.

| Marker                          | Meaning                                                                       |
|---------------------------------|-------------------------------------------------------------------------------|
| `NEEDS_INPUT: <question>`       | Agent paused; orchestrator collects the answer and resumes the same session   |
| `SPEC_COMPLETE: <title>`        | Conversational spec step done; persist artifact (orchestrator strips marker)  |
| `PR_FIX_DONE: <reasoning>`      | pr-fix made changes; advance to push                                          |
| `PR_FIX_SKIPPED: <reasoning>`   | pr-fix declined feedback as non-actionable                                    |
| `PR_FIX_BLOCKED: <reasoning>`   | pr-fix cannot proceed; mark task blocked                                      |
| `PR_FIX_NEEDS_DECISION: <q>`    | pr-fix needs an operator answer before continuing                             |
| `CONFLICTS_RESOLVED: <summary>` | resolve_conflicts resolved all markers; advance through output rule to pr-fix |

Parser regexes live at module scope in `lotsa/orchestrator.py`. If you add
a marker, add the regex next to its sibling and update the dispatch logic
in one PR — never let an emitter ship without a parser.

### Conversational rules vs. structured markers

A subset of flow steps run "conversational" (the agent emits a single
`{MARKER}: <result>` line on completion rather than producing a separate
artifact file). The `flows.py` `check_conversational_rules` function
matches these. When adding a conversational step, the marker pattern is
part of its job definition, not orchestrator code.

---

## Flow model (ADR-014)

A flow is an ordered list of `Job`s with derived state-machine semantics.
A `Job` has a `type`:

- **`agent`** — dispatch a Claude Code subprocess with a prompt template.
  Default. Existing behaviour.
- **`action`** — call a registered tool function (e.g. `push_pr`). No
  agent involvement.
- **`monitor`** — park the task in a state; an engine (e.g. `pr_monitor`)
  drives the transition out.

The state machine is derived purely from the jobs list — no synthetic
states, no special-case branches in the dispatcher. `action` and `agent`
jobs contribute `queue_state` + `active_state`; `monitor` jobs contribute
a single state.

### Bundled process presets

| Preset      | Pipeline                                                           |
|-------------|--------------------------------------------------------------------|
| `simple`    | `coding → complete \| blocked`                                     |
| `standard`  | `coding → complete \| blocked` (with branching + validation + commit) |
| `full`      | `speccing → planning → planned (gate) → testing → coding → reviewing → verifying → complete` |

`full` additionally includes the PR pipeline (pr_summary → push →
wait_for_pr_signal), with `pr_fix` as a sub-flow that handles inbound
feedback.

The `pr_summary` step (state `summarizing`) is an agent step that runs
immediately before `push_pr`. It reads the **branch diff** (ground truth for
what changed) plus the branch commit messages, with spec/plan as optional
*intent* context, and writes a **`pr_description`** artifact: a Conventional
Commits v1.0.0 title on line 1, a blank line, then a concise Markdown body. It
declares no `inputs` (a missing spec must never block it) and is only present
in `main`, not the `pr_fix` sub-flow — re-pushes keep the existing PR and do
not regenerate.

### Output rules

Each step can declare `OutputRule`s for automatic routing:

```yaml
- name: test
  prompt: testing
  resume: true
  rules:
    - source: stdout
      pattern: "FAILED"
      target: code
    - source: stdout
      pattern: "passed"
      target: next
```

Rules are evaluated in order, first-match wins. `target` can be `next`
(default success), `blocked`, or a sibling job name.

### Per-flow rule overrides

When the same job appears in two flows with different routing (e.g.
`review` in `main` vs. `pr_fix`), the `FlowStep` carries per-flow rule
overrides that take precedence over the job's defaults. Implemented as
lookup-then-fallback at evaluation time — not a YAML merge at load time.

---

## Push step

`lotsa/push_step.py` is the deterministic push: validate clean tree, push
branch (with retries for non-fast-forward), open or update PR via
`lotsa/github_client.py`. **No agent invocation.** All git invocations go
through the async subprocess API.

PR title/body are **not** synthesized here. `execute_push` takes a ready
`title`/`body` and only opens the PR (on creation). `push_pr` resolves them via
`build_pr_text`: parse the `pr_description` artifact (the `pr_summary` agent's
Conventional Commits summary) when present, else a deterministic diff/commit-driven
fallback (`parse_pr_description` → `_build_fallback_title`/`_build_fallback_body`),
then append the `_Generated by Lotsa · task <id> · flow <name>_` trailer. The
fallback is diff/commit-driven (not the spec's first line) and guards the
historical `feat: ---` bug. Generation happens only at PR creation
(`pr_number is None`); a re-push passes `title=None`/`body=None`.

When push fails with NON_FAST_FORWARD, the task is moved to `rebasing` —
the orchestrator rebases against the base branch (ADR-015) and re-attempts
the push. The retry path is observable as state transitions in the
dashboard, not buried in a loop.

---

## PR monitor

`lotsa/pr_monitor.py` polls open PRs on a schedule, classifies the signal
(NONE / COMPLETE / ABANDONED / FEEDBACK), and on FEEDBACK calls back into
the orchestrator to dispatch a `pr_fix` sub-flow. Capped at
`_MAX_POLL_CONCURRENCY = 8` concurrent polls so a backlog of waiting tasks
can't fan out hundreds of GitHub API calls.

Monitor → orchestrator callbacks are typed against the
`PrMonitorOrchestrator` Protocol (`db`, `transition_task`,
`dispatch_pr_fix`, `list_waiting_pr_tasks`). The contract is named and
public — do not regress to duck-typed `getattr` lookups.

### What counts as "FEEDBACK"

Bot comments are included by default. The `pr_fix` agent triages — it can
emit `PR_FIX_SKIPPED:` to decline non-actionable bot chatter without
producing a push. Operators opt **out** of bot comments via `lotsa.yaml`
only if they want the old narrow behaviour.

### APPROVED is not COMPLETE

Bots and reviewers can mark a PR APPROVED while leaving actionable
feedback inline. The only terminal signals are: **PR merged** (→ complete)
and **PR closed without merge** (→ abandoned). Treating APPROVED as
"done" risks short-circuiting real work — don't.

### An opened PR is watched until terminal (ADR-030)

`list_waiting_pr_tasks` discovers **every** non-terminal task carrying a
`pr_number` — not just `waiting_for_pr` ones — so a PR-bearing task parked
anywhere (blocked, needs_input, crash-recovered) still gets polled and an
operator's hand-merge completes it instead of stranding it. The split:
**terminal** signals (merge/close/404) act from any status; **feedback**
stays gated to `waiting_for_pr` (never dispatch pr-fix into a working/blocked
task). A terminal signal on a `working` task is **deferred** —
`MonitoredPr.terminal_pending` is set and the orchestrator's drainer applies
it via `take_terminal_pending` after the in-flight agent's own routing, so an
agent is never completed mid-write. `transition_task` does **not** edge-gate
terminal outcomes (a merge is global truth, not a flow transition), which is
how completion works from the `blocked` SM sink; the audit row names the
parked status.

---

## Server, frontend, and the dashboard

`lotsa/server/` is a FastAPI app that exposes the dashboard:

- **API:** `/api/tasks`, `/api/tasks/{id}/messages`, `/api/tasks/{id}/events`
  (SSE), plus action endpoints (approve, retry, revise, etc.).
- **Static frontend:** `lotsa/server/static/dist/` holds the built React
  app — **gitignored build artifact**, not committed. Run `npm run build`
  in `lotsa/frontend/` before `lotsa serve` (see *Dev workflow* below).
  The frontend source is in `lotsa/frontend/`.
- **Bind defaults:** `127.0.0.1:8420`. Localhost-only by default; opening
  it to a network requires explicit `--host`.

### Frontend (`lotsa/frontend/`)

- React + Vite SPA, **shadcn/ui** component library (ADR-012).
- Tailwind for styling — design tokens in `tailwind.config.ts`, never
  inline colour/spacing values.
- The build output lives at `lotsa/server/static/dist/` and is
  **gitignored** (`.gitignore:21`). It is not committed.

If you change the frontend's contract with `lotsa/server/api_routes.py`,
update both in the same PR.

### Dev workflow — rebuild the frontend before `lotsa serve`

The dashboard serves files from `lotsa/server/static/dist/`. Because
the bundle is gitignored, **`pip install -e '.[dev]'` does not produce
it** — `pip` only installs Python deps. If you edit anything under
`lotsa/frontend/src/` and restart `lotsa serve` without rebuilding,
the dashboard keeps serving the previous build's compiled JS and your
changes appear invisible.

Always rebuild before relaunching `lotsa serve` after a frontend
change:

```bash
cd lotsa/frontend && npm run build
cd ../.. && lotsa serve --process full --budget 50
```

Or — equivalently — wrap the two in a shell alias / one-liner.
Open question (worth its own PR): should `lotsa serve` detect a
stale or missing bundle at startup and auto-build (or at least warn)?
That'd close the gap structurally.

Note for agents: when you modify files under `lotsa/frontend/src/`,
running `npm run build` is part of the same logical change — the
operator's next `lotsa serve` will otherwise serve stale JS. But the
build output stays gitignored; nothing to commit.

---

## Operational preamble (injected into every agent dispatch)

The orchestrator prepends this preamble to every agent prompt. Agents
must respect it. If you change it (in `lotsa/orchestrator.py`'s
`OPERATIONAL_PREAMBLE` constant), audit every consumer — including
this section.

The preamble (per ADR-025) is the authoritative layer in the
layered-system-prompt model: it sits in the highest-authority slot
(appended to the `claude_code` preset via `--append-system-prompt`)
and explicitly names its precedence over project `CLAUDE.md` /
`AGENTS.md` on operational matters. Project `CLAUDE.md` still flows
in as conversation context and informs domain decisions (code style,
naming, architecture); the preamble wins on orchestrator-owned flow.

### Universal preamble vs. runner-specific dispatch fragment (ADR-028)

The system prompt the orchestrator assembles is **two pieces**, concatenated
in `_build_system_prompt`:

```
OPERATIONAL_PREAMBLE  +  runner.dispatch_shape_prompt()  +  step prompt
```

- **`OPERATIONAL_PREAMBLE`** (`lotsa/orchestrator.py`) holds only the
  *task-shape* rules that are true regardless of how the agent is
  dispatched: the precedence statement, *How to communicate with the
  operator* (final stdout → audit log, `NEEDS_INPUT:`, non-blocking
  judgment calls), *Git authority*, and *File scope*.
- **`runner.dispatch_shape_prompt()`** contributes the *dispatch-shape*
  text, which differs per runner. The CLI runners (`ClaudeCodeRunner`,
  `DockerAgentRunner`) return `CLI_DISPATCH_SHAPE_FRAGMENT`
  (`rigg/agent_runner.py`) — the `claude --print` one-shot
  *Your environment* / *Execution patterns* sections (no daemon, no UI,
  `Monitor`/`ScheduleWakeup`/`Task`/`BashOutput`/`AskUserQuestion` /
  background `Bash` all fail). The SDK runner (`ClaudeAgentSDKRunner`)
  returns its own honest fragment instead.

Universal shape (see `lotsa/orchestrator.py` for canonical content):

```
## Lotsa Operational Rules (authoritative)

[Precedence statement — Lotsa wins over project CLAUDE.md on
operational matters; project conventions still inform domain.]

### How to communicate with the operator
- Final stdout becomes a message in the audit log (read async via
  dashboard). State decisions + rationale explicitly.
- Blocking questions: emit `NEEDS_INPUT: <question>` as final
  line and stop.
- Non-blocking judgment calls: state decision + reasoning and
  proceed; operator can redirect via dashboard chat (arrives on
  next dispatch under `## Revision Feedback`).

### Git authority
- Orchestrator owns git state; worktree is on lotsa/<task_id>.
- No branch create/switch/rebase/reset; no push.

### File scope
- Modify only files inside the worktree.
- No `cd`, `pushd`, `os.chdir`, or `git -C <other-path>` — stay in
  the worktree the orchestrator placed you in.
```

When adding a new operational rule, decide where it belongs: a rule that is
true for *every* dispatch shape goes in `OPERATIONAL_PREAMBLE`; a rule that
only holds for a particular dispatch shape (CLI one-shot vs. SDK
programmatic) goes in that runner's `dispatch_shape_prompt()` fragment. As
before, cross-step rules go in the preamble/fragment, not in a per-step
prompt; per-step responsibilities belong in the step prompt.

---

## CLI

```bash
lotsa init [data_dir]               # Scaffold the Lotsa directory (default ~/.lotsa)
lotsa serve                         # Start the dashboard (reads data_dir/lotsa.yaml)
lotsa build                         # Build the agent Docker image
```

Common `lotsa serve` flags:

| Flag              | Default                | Description                              |
|-------------------|------------------------|------------------------------------------|
| `--flow`          | `chat`                 | Default-selected process for new tasks (ADR-034) — a bundled name (`chat`/`simple`/`standard`/`full`/`quickfix`) or any inline name from `lotsa.yaml`'s `processes:` block. The full catalog always loads; this only picks the picker's pre-selected default, not what loads |
| `--process`       | —                      | Alias for `--flow`; either works         |
| `--flow-file`     | —                      | Standalone `process.yaml` file (highest priority — overrides `--flow`/`--process` and inline `default: true`) |
| `--model`         | `sonnet`               | Claude model name                        |
| `--budget`        | `5.0`                  | Max USD per agent run                    |
| `--max-output-tokens` | —                  | Cap on tokens Claude Code may emit per response (overrides the 32000 default) |
| `--work-dir`      | `.`                    | **Deprecated** (ADR-029) — seeds a single `default` project; prefer a `projects:` block |
| `--prompts-dir`   | —                      | Custom prompt templates                  |
| `--docker`        | off                    | Run agents inside a Docker container     |
| `--docker-image`  | `lotsa-agent:latest`   | Docker image to use                      |
| `--runner`        | —                      | Agent runner shape (ADR-028). Unset = default CLI runner (or Docker via `--docker`); `claude-agent-sdk` = experimental SDK runner (overrides `--docker`, requires `ANTHROPIC_API_KEY`) |
| `--data-dir`      | `~/.lotsa`             | The single Lotsa directory (config + DB + worktrees) |
| `--config`        | `<data-dir>/lotsa.yaml`| Explicit `lotsa.yaml` path (overrides discovery) |
| `--host`          | `127.0.0.1`            | Dashboard bind address                   |
| `--port`          | `8420`                 | Dashboard port                           |

### Required env vars

`ANTHROPIC_API_KEY` **or** the `CLAUDE_CODE_OAUTH_TOKEN` group
(`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_ACCOUNT_UUID`, `CLAUDE_ORG_UUID`).

---

## Worktree-per-task

Tasks get an isolated git worktree at
`{data_dir}/worktrees/{project_id}/{task_id}/` (namespaced per project since
ADR-029). Created lazily on first dispatch by `WorktreeManager`
(`rigg.git.WorktreeManager`). The orchestrator switches into it for
agent dispatch, push, and rebase operations.

### Restart is resumptive, not destructive (ADR-040)

**The DB is the state of record; in-memory dicts/timers are caches only;
every flow step is idempotent.** Any in-memory structure the orchestrator or
monitor relies on for correctness must be reconstructable from the DB — dicts
and timers are permitted only as caches of persisted state, never as the sole
record of a decision. Every flow step is safe to execute at-least-once (a step
that can't be made naturally idempotent is guarded by a persisted CAS marker,
ADR-020), which is what makes at-least-once dispatch — resume / retry / crash
recovery — safe in general.

Restart recovery (deploy now always restarts the daemon, so this is routine):
on startup a `status='working'` task is treated as **interrupted**, not failed.
The sweep records `interrupted_at` + `resume_count` in the task `metadata` JSON
(no new columns, no migration) and auto-dispatches a continuation:

- **Resume the agent** — an agent step with a persisted `session_id` and a
  runner that reports `supports_resume` → re-dispatch with `--resume
  <session_id>`. `session_id` is a single global metadata slot, so the resume
  is gated on `session_step` (persisted alongside `session_id` when a step
  completes): we only reattach when the persisted session belongs to the step
  being resumed — otherwise a step interrupted before producing its own session
  (e.g. `plan`/`review`/`pr_summary`, none `resume: true`) would `--resume`
  into the previous step's unrelated conversation. A mismatch falls through to
  the re-run path.
- **Idempotent re-run** — no `session_id`, a stale/foreign `session_step`, a
  non-resume runner, or a deterministic/action step → re-dispatch the current
  step from its start; step idempotency makes already-done work a no-op.
- **Cap → block** — bounded by `resume_count` (default `resume_cap=2`,
  `lotsa.yaml`-configurable). The count bounds repeated failure at a *single*
  interruption: it is cleared (`_clear_interruption_markers`) the moment a task
  makes forward progress past the interrupted step — advancing into a new step's
  active state, an action step completing, **or** entering a monitor state (all
  three sibling forward-progress edges clear the markers) — so routine deploys
  that each make real forward progress don't accumulate toward the cap. Past the
  cap, fall back to `blocked` with a "couldn't resume after N attempts" message.

Resume-vs-re-run is selected via the runner's `supports_resume` capability
(read defensively, never `isinstance`). A resumed dispatch appends a
resumed-agent note to the layered system prompt (ADR-025) so the agent checks
what it already did before redoing work. Tasks already at `status='blocked'` /
`'archived'` are skipped. Non-`working` push-state rows are pre-ADR-014 legacy
shapes and keep today's block-with-recovery-message contract (a modern mid-push
task is `status='working'` and takes the resume path).

**Idempotency audit (ADR-040 phase 1):** commit (`commit_step.execute_commit`
no-ops on a clean staged tree), push (`push_step.execute_push` guards PR
creation on `pr_number is None`; push-by-SHA is a no-op if already pushed), and
sync (`_sync_branch_to_main` returns `already_current` when `behind==0`) are all
**naturally idempotent** — confirmed by regression tests; no new CAS marker was
required.

**Graceful drain:** on `shutdown()` the service sets `_accepting=False` (refusing
new dispatches), awaits in-flight agents up to `shutdown_grace_seconds`
(default 30s), then — before cancelling the completion drainer — waits (briefly,
bounded) on `_completions.join()` so completions that landed inside the window are
*applied* (state CAS + `_in_flight` pop), not just dequeued. Agents that finish
cleanly in-window therefore commit their transition instead of being left
`status='working'` and re-resumed pointlessly on the next start. Survivors past
the window are cancelled and recovered by the next start()'s resume sweep.
Operators should set systemd `TimeoutStopSec` ≥ `shutdown_grace_seconds` so
systemd doesn't SIGKILL mid-drain.

---

## Docker mode

`--docker` runs agents inside `lotsa-agent:latest` (built by
`lotsa build`). The task's worktree is mounted at `/workspace`; Claude auth
flows through environment variables; the container is removed after each
run (`--rm`).

For advanced use, instantiate `DockerAgentRunner` directly:

```python
from lotsa.docker_runner import DockerAgentRunner
runner = DockerAgentRunner(docker_args=["--network", "host", "--memory", "4g"])
```

---

## Testing

```bash
python -m pytest lotsa/tests/ -v        # CE only
python -m pytest rigg/tests/ lotsa/tests/ -v   # CE + shared
ruff check lotsa/
ruff format --check lotsa/
```

Test conventions:
- One test module per CE module (`lotsa/orchestrator.py` →
  `lotsa/tests/test_orchestrator.py`).
- Async tests use `pytest-asyncio`; CE's test DB is a temp SQLite file
  per test, **never** mocked. The SQLite migration path itself is
  exercised by tests.
- PR-monitor tests use partial fakes that satisfy the
  `PrMonitorOrchestrator` Protocol; integration tests exercise the real
  `OrchestratorService` against a recorded GitHub REST fixture.

---

## Refactor and review discipline

The orchestrator and the flow / state-machine layer carry concerns that
surface in many call sites: task state names, state-machine transition
checks, active-vs-root flow resolution, legacy-row handling on restart.
When a change renames or replaces one of those concepts, or changes how
the state machine is built, the sweep for affected sites belongs in the
*implementation commit*, not in the review cycle.

### Cross-cutting refactors — sweep before the PR opens

Before opening a PR that touches state names, state-machine shape, or how
the orchestrator resolves the active flow for a task, walk these surfaces
explicitly and decide for each one what the new behaviour is.

**Every literal occurrence of the old name.** State names appear as
strings in CAS payloads, audit-message fields, conditional guards on the
row's current state, recovery branches, and engine untrack checks. A
rename that misses any of these strands tasks silently — the bug surfaces
only when a task happens to traverse the missed site. Read every literal
and decide what it becomes (often: parameterise on the configured state
instead of hardcoding the name).

**Every read of the state machine that assumes the root flow.** When the
change introduces a notion of "active flow" that can differ from the
root, every read of state-machine transitions, job lists, or queue states
needs to consult the active flow rather than the root. The legitimate
exceptions are the cross-flow edge registrar (which writes into the root
SM at process-build time) and a small number of root-only operations
such as task creation and top-level process loading. Everywhere else,
ask "if this task is mid-sub-flow, does this read still make sense?".

**Every user-facing action method and internal dispatcher path.** The
state surface is consumed from a known set of code paths: the action
methods (the public entry points that mutate task state), the
auto-advance branches of the completion drainer, the dispatch helpers,
the restart recovery sweep, and the engine untrack call sites. When the
rule changes for one of them, the same change usually applies to the
others. Symmetric behaviour across sibling paths is itself an invariant
in this codebase — asymmetric handling of the same scenario across two
similar branches is a bug class.

**The legacy-row contract.** "Recreate tasks on upgrade" covers the
new-deployment story. On any restart of an existing deployment, the
restart recovery sweep, `transition_task`, the `block()` action, the
`retry()` action, and engine `untrack` paths all encounter rows
persisted under the prior schema. Each surface that compares against
state names needs a defined answer for old names — even if the answer is
"route to blocked with the standard recovery message". Silent failure
here manifests as tasks that never re-enter the active flow after
restart.

### After fixing a bug surfaced by review

Name the anti-pattern in one sentence (the *class* of bug, not the single
line). Search the touched module and any sibling modules in the same
layer for the same shape. Fix every instance in the same commit.

Reviewers surface one site at a time. The cycle that follows from fixing
one site at a time is avoidable, and historically expensive in this
codebase — past PRs have absorbed many rounds of feedback in which most
of the rounds were the same class of finding hitting different call
sites. A partial fix is a guarantee the same class will come back in the
next round.

The same rule applies to comments and docstrings within roughly ten lines
of any changed code: re-read them and update anything that now describes
pre-change behaviour. Stale prose has driven entire rounds of feedback in
past PRs.

### Regression-test discipline

A regression test only protects against the bug it was written for if it
fails against the pre-fix code. Two patterns have slipped through
multiple times and shipped trivially-passing tests:

- Tests that pre-flip a task row into the post-bug state *before*
  dispatching the code under test. The dispatcher's own entry CAS loses
  first, the failure branch never runs, and the test passes against both
  buggy and fixed code.
- Tests that assert on database fields a pre-dispatch CAS populates even
  when the subsequent dispatch silently no-ops. The assertion sees the
  populated fields and concludes the agent ran when in fact it never
  did.

For every regression test added with a bug fix, run it against the
pre-fix code (a temporary revert is fine), observe the actual failure
message, and record it in the commit body. A test that does not fail
against the code it claims to protect from does not protect anything.
Exercise the failure from inside the code under test (for example via a
stub that flips state mid-execution) rather than by setting up the
post-bug state externally.

### Scope: the task body wins over the artefact's internal phasing

When an agent (planning, coding, or otherwise) implements an ADR, an
issue, or any artefact that documents its own Scope / Phases / Layers
sections, the implementing PR ships the full Scope unless the
operator's task body explicitly carved it down. The phasing inside the
artefact describes how the original author imagined the work breaking
down — it is *not* an implicit PR boundary.

Past failure mode: an operator asked for ADR-X "in one PR, don't split
it", the planning agent read ADR-X's "Phase 1 / Phase 2 / Phase 3"
Scope section and planned only Phase 1, the coding agent followed the
plan, and the operator got a partial delivery that silently dropped
two-thirds of the requested scope. The planner re-decomposed scope the
operator had already framed.

If the full scope is genuinely unworkable in one session, the right
move is to surface that via `NEEDS_INPUT` (planner) or report a
scope gap (coder), not to ship a quietly narrowed result. See
`lotsa/prompts/full/planning-system.md` Step 1-2 and
`lotsa/prompts/full/coding-system.md` Step 4.5 for the prompt-level
rules.

---

## What to flag rather than decide in CE

- Adding a flow `Job.type` beyond `agent | action | monitor` — that's a
  protocol change, write an ADR.
- Mutating the `messages` table after insert (UPDATE or DELETE) — never
  do this without an explicit product decision and an ADR.
- Bypassing `atomic_transition` for a state change with a precondition, or
  calling the `claim_task_transition` primitive directly from production code.
- Adding git calls via blocking subprocess APIs or shell strings.
- Granting agents direct push, branch-creation, or rebase authority.
- Changing the `OPERATIONAL_PREAMBLE` (every agent prompt depends on it).
- Adding a new agent output marker without also adding its parser
  regex and dispatch handler in the same PR.

---

## CE-applicable ADRs

- ADR-009 — Bot orchestrator as reference implementation (**Superseded** —
  bot retired; patterns live here and in `rigg/`).
- ADR-012 — shadcn/ui for CE dashboard.
- ADR-013 — Orchestrator owns git state.
- ADR-014 — Jobs as the unified flow primitive.
- ADR-015 — Orchestrator syncs task branch to main before pr-fix
  (**Implemented** — Phase 1: deterministic sync; Phase 2: conflict
  path + `resolve_conflicts` + `merge_conflict` trigger).
- ADR-018 — Task branches stay synced with upstream across the lifecycle
  (**Implemented** — pre-retry-from-blocked + pre-rebase-after-restart wired
  into `retry()`'s push branch; creation via PR #70, pre-pr-fix via ADR-015).
- ADR-016 — Optional worktree-persisted task artifacts.
- ADR-022 — Per-step model selection in process.yaml (**Implemented**).
- ADR-028 — Claude Agent SDK runner — Phases 1, 2 & 3 shipped. Phase 3 adds a
  per-step `runner:` field in `process.yaml` (alongside the existing `model:`).
  Jobs can name any registered runner explicitly; resolution uses
  `resolve_runner_by_name` (exact-name-only, no prefix fallback). The built-in
  name `claude-agent-sdk` is registered at `start()` if no operator entry
  already claims it.
- ADR-029 — Multi-project support (**Implemented**). Tasks carry a
  `project_id` FK; a `projects:` block in `lotsa.yaml` registers repos
  (validated + normalized by `config.resolve_project_specs`). The
  orchestrator resolves a `WorktreeManager` **per project** lazily
  (`_worktree_manager_for` / `_worktree_managers`, keyed by project id) —
  the former `self.worktree_manager` singleton is gone. Worktrees are
  namespaced `worktrees/<project_id>/<task_id>`. `work_dir:` still seeds a
  `default` project (single-project configs keep working; deprecation
  warning). Shipped as a pre-alpha clean break: migration `_m004` recreates
  the `tasks` table with `project_id` (dropping old rows) and startup
  clears the old flat `worktrees/` tree.
- ADR-034 — Chat-first task creation (**Implemented**).
  `start()` loads the full bundled catalog (`PRESET_NAMES`) plus inline
  `processes:`, not just the active preset, so every process is a pickable
  new-task option and a valid promotion target. New-task default is `chat`
  (`LotsaConfig.flow` defaults to `"chat"`); `--flow`/`--process` selects the
  picker's pre-selected process rather than gating what loads.
- ADR-035 — Cross-repo coordinated changes (**Proposed** — post-launch,
  phased). A persistent project-less **epic** coordinator investigates a
  change across selected repos (read-only, orchestrator-brokered scouts),
  synthesizes a migration-aware **contract**, and after an operator gate
  fans out one child task per repo (each single-project, seeded with the
  contract). The epic owns the shared artifacts, tracks children to
  terminal, and absorbs mid-flight divergence by revising the contract and
  re-issuing to affected children. Scopes the read-only slice of ADR-026.
