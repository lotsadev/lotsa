# Lotsa Community Edition

Local runner for the [Rigg SDK](../rigg/). Scaffold a tasks directory, start the dashboard, and manage Claude Code work from your browser.

No GitHub, no external database, no infrastructure. Just a local SQLite store and a web UI.

## Quick start

```bash
# Install (from repo root)
pip install -e ".[dev]"

# Scaffold a Lotsa directory (defaults to ~/.lotsa)
lotsa init

# Start the dashboard (requires claude CLI + ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
lotsa serve
```

`lotsa serve` opens the dashboard in your browser. Create tasks, watch flow steps execute, approve gated steps, and review output — all from the web UI.

## How it works

1. You create **tasks** in the dashboard — each task has a title, body, priority, and runs against the active process (see [Processes](#processes) below)
2. The orchestrator picks the highest-priority backlog item, dispatches it to Claude Code
3. Task state and event history are persisted in a local SQLite database

Everything Lotsa owns lives in one directory (`--data-dir`, default `~/.lotsa`):

```
~/.lotsa/
  lotsa.yaml               ← config (created by `lotsa init`)
  lotsa.db                 ← SQLite store (tasks + messages + events)
  worktrees/               ← per-task git worktrees of your work_dir
```

### State machine

States depend on the active process (see [Processes](#processes) below).

- **backlog** — waiting to be picked up
- **coding** — agent is working on it
- **complete** — agent finished successfully
- **blocked** — agent failed, needs human attention

## Processes

A **process** is the full job catalog — every step a task can take, plus the flows that string them together. Lotsa ships three bundled processes on a two-phase **Think→Execute** axis (ADR-043); you can also define your own inline in `lotsa.yaml` or as standalone `process.yaml` files.

New tasks default to `chat` (ADR-034): a fresh `lotsa serve` opens a task as a conversation you grow from, then **hand off** into an Execute process (`build` / `fix`) when you know what you're building. The whole bundled catalog plus every inline process is always loaded, so each one is pickable per-task in the new-task mode switcher and a valid handoff target — `--process <name>` / `--flow <name>` (or `lotsa.yaml`'s `flow:` field, or an inline entry's `default: true`) only chooses **which process the picker pre-selects** as the default. Pass `--flow build` to make Build the default for an operator who always builds; drop the flag for chat-first.

The handoff from Think (`chat`) to Execute (`build`/`fix`) is one-way — the worktree and history carry over, but there's no return to chat — while a running Execute task stays steerable via revision feedback and `NEEDS_INPUT`.

### Bundled processes

#### `chat` (default) — Think: explore and triage

```
backlog → chatting (conversational) → hand off into an Execute process
```

A single conversational step: no completion marker, no commit pressure, and it never writes implementation code. New tasks start here out of the box: discuss the work with the agent — and, on request, have it distill a concise spec into the conversation — then, when you're ready, **hand off** the task into `build` or `fix`. The worktree and history carry over. This is the zero-config default.

#### `build` — Execute (full depth): plan, test, code, review, verify, PR loop

```
backlog → planning → testing → coding → reviewing → verifying → summarizing → push_pr → wait_for_pr_signal → complete | blocked
```

Full SDLC discipline, run autonomously — the task body (or a spec carried from chat) is the brief; there is no separate spec/plan gate (ADR-043 dropped it):

1. **Plan** — agent reads the codebase and writes an implementation plan. Ungated — it auto-advances to the next step (no human approval gate)
2. **Test** — agent writes failing tests (resumes same session)
3. **Code** — agent implements to make tests pass (resumes same session)
4. **Review** — agent reviews the diff independently (fresh session, no implementation bias)
5. **Verify** — conversational; agent walks through what was built before the PR
6. **PR summary** — agent writes the PR title/body from the branch diff
7. **Push & monitor** — the `push_pr` action job opens the PR, then the `wait_for_pr_signal` monitor polls GitHub. Reviewer comments and failing checks dispatch a `pr_fix` sub-flow that re-runs review and pushes again until merged

#### `fix` — Execute (shallow depth): execute a precise instruction

```
backlog → coding → reviewing → push_pr → wait_for_pr_signal → complete | blocked
```

For a mechanical change you've already decided on (status bumps, typo fixes, renames, config/dependency tweaks): the coder executes the instruction directly — no spec, no plan, no test-writing — and review checks the diff against that instruction. It then opens a PR and watches it to terminal, the same as `build`. A common handoff target from `chat` when the conversation lands on a small, well-defined edit.

#### No GitHub? The mark-complete escape hatch

Both Execute processes push to a remote and open a PR by default. When no GitHub token is configured, the push parks the task at **`awaiting_operator`** ("Awaiting you") with the code committed on `lotsa/<task_id>` — review the worktree and click **Mark complete** to close it out.

### Custom processes

Two ways to define your own:

#### Inline in `lotsa.yaml` (agent-only sequences)

Best for non-engineering processes (research, content review, anything that's just a sequence of agent calls). Each entry under `processes:` is its own process, named by its key:

```yaml
# lotsa.yaml
processes:
  marketing_research:
    default: true                    # picks this as the active process
    prompts_dir: ./prompts/mkt       # optional; relative to lotsa.yaml
    steps:
      - { name: research, prompt: research }
      - { name: synthesize, prompt: synthesize }

  support_triage:
    prompts_dir: ./prompts/support
    steps:
      - name: triage
        prompt: triage
        rules:
          - { source: stdout, pattern: ESCALATE, target: blocked }
```

Each step is an agent job. Step prompts load from `<basename>-system.md` and `<basename>-user.md` under the per-process `prompts_dir` (defaults to `./prompts`).

Run with `lotsa serve --process marketing_research` (or omit when `default: true` is set).

#### Standalone `process.yaml` (typed jobs + sub-flows)

For complex processes that need action jobs (run a tool), monitor jobs (poll an external source), or sub-flows (named pipelines invoked by monitors). Load via `--flow-file=<path>`.

```yaml
# my-process.yaml
process: my_process
jobs:
  - name: plan
    type: agent
    prompt: planning
    evaluate: true                   # human gate; queues until approved

  - name: code
    type: agent
    prompt: coding
    resume: true                     # reuse Claude session from previous step
    rules:
      - { source: stdout, pattern: "FAILED", target: plan }
      - { source: stdout, pattern: "passed", target: next }

  - name: review                     # also referenced from the pr_fix sub-flow
    type: agent
    prompt: review

  - name: pr-fix                     # invoked by the monitor on PR feedback
    type: agent
    prompt: pr-fix

  - name: push_pr
    type: action
    tool: push_pr                    # opens a GitHub PR

  - name: wait
    type: monitor
    engine: pr_monitor               # polls the PR for signals
    config:
      poll_interval_seconds: 30
      max_pr_fix_rounds: 10

flows:
  main:
    steps: [plan, code, review, push_pr, wait]

  pr_fix:                            # sub-flow the monitor dispatches into
    steps:
      - name: pr-fix
        rules:
          - { source: stdout, pattern: "^AGENT_RESULT: SKIPPED", target: wait }
      - name: review                 # the per-flow rules below override main's
        rules:
          - { source: stdout, pattern: "Critical|High", target: pr-fix }
          - { source: stdout, pattern: "LGTM", target: push_pr }
      - push_pr
```

Every job referenced from a `flows:` block (including sub-flows) must exist at the top-level `jobs:` list. Per-flow `rules:` overrides under a step entry shadow the job's default rules for that flow only — useful when a job like `review` participates in two flows with different routing.

**Job fields** (common to all types):

| Field | Default | Description |
|-------|---------|-------------|
| `name` | *(required)* | Job name — used for files, display, event logs |
| `type` | `agent` | One of `agent`, `action`, `monitor` |
| `config` | `{}` | Tool/engine-specific config; merged with per-flow binding overrides |

**Agent fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `prompt` | same as `name` | Prompt file prefix — loads `{prompt}-system.md` and `{prompt}-user.md` |
| `resume` | `false` | Resume from stored session ID |
| `evaluate` | `false` | Human gate — item waits for approval before advancing |
| `rules` | `[]` | Output-based routing rules (see below) |
| `conversational` | `false` | Chat-style iterative step |
| `output` | `null` | Artifact name this step produces |
| `inputs` | `[]` | Artifact names this step requires before dispatch |

**Action fields:** `tool: <name>` references a tool registered via `lotsa.registry.register_tool` (built-in: `push_pr`; or extend via `tools:` block, below).

**Monitor fields:** `engine: <name>` references an engine registered via `lotsa.registry.register_engine` (built-in: `pr_monitor`; or extend via `engines:` block, below).

**Output rules** match against agent output after completion. Rules are evaluated in order — first match wins:

| Field | Description |
|-------|-------------|
| `source` | `"stdout"` or a file path relative to work_dir (e.g. `.lotsa/plan.md`) |
| `pattern` | Regex to search for in the source content |
| `target` | `"next"` (default), `"blocked"`, or a job name to route to |

The bundled processes live at `lotsa/prompts/{name}/process.yaml`.

### Extending with tools and engines

Action and monitor jobs reference named tools and engines from a registry. Built-ins (`push_pr` tool, `pr_monitor` engine) are always available; register your own via `lotsa.yaml`:

```yaml
# lotsa.yaml
tools:
  notify_slack: my_package.tools:notify_slack       # action tool
engines:
  jira_monitor: my_package.engines:JiraMonitorEngine # monitor engine
```

Each value is `"dotted.module:callable_or_class"`. Tools are async callables `(TaskContext, config) -> ToolResult`. Engines are classes with `__init__(orchestrator, monitor_state, config)` plus `run()`, `untrack()`, and `snapshot_triggering_ids()` methods. See `lotsa/tools/push_pr.py` and `lotsa/engines/pr_monitor.py` for reference implementations.

### Multi-provider runners (ADR-023)

By default every model name dispatches through the built-in `ClaudeCodeRunner` (the `claude` CLI). To route some model names to a different agent runner — an OpenAI-shaped CLI, a local LiteLLM runner, etc. — register them under a `runners:` block. The built-in runner is always the default and handles `claude-*` / `sonnet` / `opus` / `haiku`, so existing configs need no entry.

```yaml
# lotsa.yaml — multi-provider is a sample, not the default. Leave this out for
# single-provider (Claude-only) setups; the bundled processes ship unchanged.
runners:
  gpt:
    handler: lotsa_runners.codex:CodexCliRunner
    prefixes: [gpt-, openai/]     # routes gpt-5, gpt-4.1, openai/o1, …
  local:
    handler: lotsa_runners.litellm_runner:LiteLLMRunner
    prefixes: [ollama:, llama-]
```

A model name resolves to a runner by: exact name → longest matching prefix → default. The handler class is constructed with `model` / `budget_usd` / `max_output_tokens` from config, so third-party runner constructors must accept those keyword arguments (the `ClaudeCodeRunner` signature is the contract). The audit trail records `agent_runner` (the registered name, e.g. `gpt`) alongside `agent_model`.

> Note: Lotsa does not ship any third-party runners — `runners:` only wires up runners you provide. Per-step model routing (a different runner per process step) is live: a job's per-step `model:` (ADR-022) selects the runner for that step, falling back to the global `model:` when a job sets none.

## Configuration

Settings can come from a config file, CLI flags, or both. CLI flags always win.

### Config file (`lotsa.yaml`)

Lives at `~/.lotsa/lotsa.yaml` by default (alongside `lotsa.db` and the per-task worktrees). `lotsa init [data_dir]` scaffolds one; `--data-dir <path>` and `--config <path>` override the location for one-off runs.

```yaml
# ~/.lotsa/lotsa.yaml
model: sonnet                 # claude model
budget: 5.0                   # max USD per agent run
# max_output_tokens: 128000   # cap per response — uncomment to raise the 32000 default
flow: chat                    # default-selected process: bundled name (chat/build/fix) or an inline process name. The full catalog always loads; this only sets the picker's pre-selected default
prompts_dir: prompts/         # custom prompt templates (optional)
# resume_cap: 2               # ADR-040 — max auto-resume attempts per task on restart before falling back to `blocked`
# shutdown_grace_seconds: 30  # ADR-040 — bounded window shutdown() waits for in-flight agents to drain before cancelling

# Projects — the git repos Lotsa can run tasks against. Each id must match
# [a-z0-9_-] and point at an existing git repository. The new-task picker and
# the sidebar project filter list these; a single project is shown without a
# picker. (~ is expanded.)
projects:
  lotsa:
    name: Lotsa
    path: /path/to/your/repo
  # myapp:
  #   name: My App
  #   path: ~/code/myapp

# work_dir: /path/to/your/repo  # DEPRECATED (ADR-029) — seeds a single
#                               # `default` project. Prefer `projects:` above.

# Optional — register custom action tools and monitor engines
# referenced from a process. Built-ins (push_pr, pr_monitor) are
# always available; only list third-party additions here.
tools:
  my_tool: my_package.tools:my_tool
engines:
  my_engine: my_package.engines:MyEngineClass

# Optional — define agent-only processes inline. See "Custom processes"
# above for the schema. Each entry is its own selectable process.
processes:
  marketing_research:
    default: true
    prompts_dir: ./prompts/mkt
    steps:
      - { name: research, prompt: research }
      - { name: synthesize, prompt: synthesize }
```

All fields are optional — missing fields use defaults. `lotsa init` writes a starter `lotsa.yaml` with the optional blocks commented out.

**Restart resilience (ADR-040).** Lotsa runs as a long-lived daemon, and a
deploy restarts it. On restart, tasks that were mid-run are **resumed** (the
agent reattaches via `--resume` where the runner supports it, or the step
re-runs idempotently), not dumped to `blocked`. On shutdown the service drains
in-flight agents for up to `shutdown_grace_seconds` (default 30s). If you run
Lotsa under systemd, set `TimeoutStopSec` **≥** `shutdown_grace_seconds` so
systemd doesn't SIGKILL the service mid-drain — otherwise the drain is cut short
and more tasks fall to the (still-safe) resume-on-next-start path.

### Projects (multi-repo)

Lotsa can drive tasks across several repos (ADR-029). Register each under `projects:` (see the example above): the `id` is a path/DB key (`[a-z0-9_-]`), `path` must be an existing git repository, and `name` is the display label. Restart `lotsa serve` after editing.

When you create a task, a **project picker** offers the registered repos (it remembers your last choice); the sidebar gains a per-project filter and badges. With only **one** project registered, those controls are hidden as noise — so if you "can't select a project," it means only one is registered. Add more to `projects:` to get the picker.

`work_dir:` is the deprecated single-project predecessor: it seeds one `default` project from that path. It still works, but prefer `projects:`. Note: keeping `work_dir:` *and* a `projects:` block registers a surprise extra `default` unless you declare `projects: default:` yourself — so once you migrate, remove `work_dir:`.

### Resolution order

1. CLI flags (highest priority)
2. `lotsa.yaml` config file
3. Built-in defaults

```bash
# Everything from config (reads ~/.lotsa/lotsa.yaml)
lotsa serve

# Override model from config
lotsa serve --model opus

# Override budget
lotsa serve --budget 10.0

# Use a portable project-local Lotsa directory
lotsa serve --data-dir ./lotsa-data

# Explicit config file (skip discovery)
lotsa serve --config /path/to/lotsa.yaml
```

## CLI reference

### `lotsa init [data_dir]`

Create a Lotsa directory with a config file. `data_dir` defaults to `~/.lotsa` — the single directory that holds `lotsa.yaml`, `lotsa.db`, and per-task worktrees.

```bash
lotsa init                          # creates ~/.lotsa/
lotsa init ./lotsa-data             # portable project-local data dir
```

Creates `lotsa.yaml` in the data directory. Idempotent — won't overwrite an existing `lotsa.yaml`.

### `lotsa serve`

Start the dashboard. Reads `lotsa.yaml` from `--data-dir` (default `~/.lotsa`). Uvicorn logs the bind URL on startup — open it in your browser. Errors out with a "run lotsa init" hint if no config is found.

```bash
lotsa serve                                # dashboard on 127.0.0.1:8420
lotsa serve --flow build                   # use the bundled Build (Execute) process
lotsa serve --process marketing            # use an inline process from lotsa.yaml
lotsa serve --flow-file my.yaml            # use a standalone process.yaml
lotsa serve --model opus                   # use a specific model
lotsa serve --budget 10.0                  # set budget per run
lotsa serve --work-dir /my/project         # agent works in this directory
lotsa serve --prompts-dir prompts/         # custom prompt templates
lotsa serve --docker                       # run agents in Docker containers
lotsa serve --runner claude-agent-sdk      # use the SDK-shaped runner (experimental, ADR-028)
lotsa serve --port 9000                    # custom port
lotsa serve --data-dir ./lotsa-data        # portable project-local data dir
lotsa serve --config /path/lotsa.yaml      # explicit config file
```

| Flag | Default | Description |
|------|---------|-------------|
| `--flow` | `chat` | Process the new-task picker pre-selects — a bundled name (`chat`/`build`/`fix`) or any inline name from `lotsa.yaml`'s `processes:` block. The full catalog always loads; this only sets the default, it doesn't restrict what's available |
| `--process` | — | Alias for `--flow`; either works |
| `--flow-file` | — | Standalone `process.yaml` file (highest priority — overrides `--flow`/`--process` and inline `default: true`) |
| `--model` | `sonnet` | Claude model name |
| `--budget` | `5.0` | Max USD per agent run |
| `--max-output-tokens` | — | Cap on tokens Claude Code may emit per response. Overrides the default 32000 ceiling that produces `"Claude's response exceeded the 32000 output token maximum"` errors. Unset inherits `$CLAUDE_CODE_MAX_OUTPUT_TOKENS` from the shell. |
| `--work-dir` | `.` | Working directory for the agent |
| `--prompts-dir` | — | Directory with custom prompt templates |
| `--docker` | off | Run agent inside a Docker container |
| `--docker-image` | `lotsa-agent:latest` | Docker image to use |
| `--runner` | — | Agent runner shape. Unset uses the default CLI runner (or Docker when `--docker` is set). Set `claude-agent-sdk` for the experimental SDK runner (ADR-028) — overrides `--docker` when both are given. See [Agent runners](#agent-runners). |
| `--data-dir` | `~/.lotsa` | Where Lotsa stores its data |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8420` | Dashboard port |
| `--config` | auto-discovered | Explicit path to `lotsa.yaml` |

### `lotsa build`

Build the Docker image for sandboxed agent execution.

```bash
lotsa build                    # builds lotsa-agent:latest
lotsa build --tag my-image:v1  # custom tag
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | one of these | API key for Claude |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these | OAuth token (Claude Pro/Team/Enterprise) |
| `CLAUDE_ACCOUNT_UUID` | with OAuth | Account UUID for OAuth auth |
| `CLAUDE_ORG_UUID` | with OAuth | Organization UUID for OAuth auth |

Set either `ANTHROPIC_API_KEY` *or* the `CLAUDE_CODE_OAUTH_TOKEN` group.

## Docker mode

Run agents inside a Docker container for sandboxed execution. The container has Claude Code CLI, Node.js, and git pre-installed.

### Setup

```bash
# Build the agent image (one-time)
lotsa build

# Start the dashboard with Docker enabled
lotsa serve --docker
```

### How it works

When `--docker` is set, Lotsa runs `docker run` instead of calling `claude` directly:
- Your `work_dir` is mounted as a volume at `/workspace` inside the container
- Claude auth credentials are forwarded as environment variables
- The container is removed after each run (`--rm`)

### Config file

```yaml
# lotsa.yaml
docker: true
docker_image: lotsa-agent:latest  # or your custom image
```

### Custom Docker args

For advanced use (network access, memory limits), use `DockerAgentRunner` directly in Python:

```python
from lotsa.docker_runner import DockerAgentRunner

runner = DockerAgentRunner(
    docker_args=["--network", "host", "--memory", "4g"],
)
```

## Agent runners

Lotsa dispatches each agent through a **runner**. Two runner *shapes* ship today:

- **CLI runner** (default) — drives the `claude` CLI as a one-shot
  `claude --print` subprocess. This is what you get with no `--runner` flag
  (and what `--docker` wraps in a container). Battle-tested; it backs every
  bundled process.
- **SDK runner** (`--runner claude-agent-sdk`, **experimental**) — drives
  Anthropic's [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)
  programmatically instead of shelling out to `claude --print`. Introduced by
  [ADR-028](../docs/adr/ADR-028-claude-agent-sdk-runner.md).

### SDK runner — what's wired, and the gaps

The SDK runner ships ADR-028 Phases 1, 2 & 3. Know these limits before
selecting it:

- **Not a pure-API / CLI-free runner.** `claude-agent-sdk` is not a thin HTTP
  client for `/v1/messages`; it drives the Claude Code CLI/Node runtime under
  the hood. Selecting it does **not** remove the Claude Code runtime
  requirement, and it does **not** reduce the self-hostable footprint versus
  the CLI runner. A genuinely CLI-free pure-API shape is a separate, unbuilt
  runner (the *API shape* in ADR-023).
- **Auth model is simpler.** It reads `ANTHROPIC_API_KEY` programmatically
  (and honours `ANTHROPIC_BASE_URL` for an enterprise-hosted proxy), with no
  keychain and no `--dangerously-skip-permissions` flag. `ANTHROPIC_API_KEY`
  is required when this runner is selected. Note this is an *auth*
  simplification, not a tighter *permission* posture: the runner sets
  `permission_mode="bypassPermissions"`, the SDK-equivalent of the CLI
  runner's `--dangerously-skip-permissions` (both bypass all permission
  prompts), which is required for headless operation. Real per-tool gating
  lands with the interception follow-up ADR.
- **Single-turn, no tool-interception surface yet.** The Lotsa-owned
  cross-turn lifecycle — dashboard-resolved `AskUserQuestion`, background
  `Bash`, subagent dispatch, `Monitor`/`ScheduleWakeup` via SDK resume — is
  **not** built in this cut. The runner therefore advertises only the
  capability it actually wires (programmatic, single-turn).
- **Per-step `runner:` field (Phase 3).** In addition to the global
  `--runner` flag, individual jobs in `process.yaml` can name a runner
  explicitly:
  ```yaml
  - name: code
    type: agent
    prompt: coding
    model: sonnet
    runner: claude-agent-sdk   # opt in for this step only
  ```
  The named runner must be registered (built-in `claude-agent-sdk`, or an
  operator entry in `lotsa.yaml`'s `runners:` block). A miss at
  `build_process` time raises a `ValueError` listing the registered names.
  Steps without `runner:` continue to resolve via `model:` as before.
- **`--budget` is not enforced yet.** The CLI runner caps per-run spend via
  `claude --print --max-budget-usd`; this cut wires no equivalent into the
  SDK (`claude-agent-sdk` exposes no USD ceiling), so `--budget` is advisory
  only for the SDK runner — surfaced in the run log but not capped. Bound SDK
  runs another way (e.g. `timeout_seconds`) until a cap lands.

The `claude-agent-sdk` dependency is install-time only and imported lazily, so
operators who don't select the SDK runner incur no runtime cost from it.

## Custom prompts

By default, Lotsa uses a generic coding prompt. For better results, create custom prompts:

```bash
mkdir prompts
```

```markdown
# prompts/coding-system.md
You are a senior Python developer working on the Acme project.
Follow PEP 8, write tests for all new code, and use type hints.
```

```markdown
# prompts/coding-user.md
Implement the following task. Write clean, well-tested code.
```

Then start the dashboard with:

```bash
lotsa serve --prompts-dir prompts/
```

The task title and body are appended to the user prompt automatically.

## Architecture

Lotsa is a thin product layer on top of the [Rigg SDK](../rigg/):

```
┌──────────────────────────┐
│     lotsa (this package)  │  CLI, config, SQLite store, dashboard, console output
├──────────────────────────┤
│     rigg (SDK)       │  StateMachine, OrchestrationEngine, ClaudeCodeRunner
└──────────────────────────┘
```

Lotsa provides the three infrastructure implementations that Rigg needs:

| Rigg Protocol | Lotsa Implementation | What it does |
|-------------------|---------------------|--------------|
| `ItemSource` | `SQLiteItemSource` | Reads tasks from the local SQLite store |
| `Notifier` | `ConsoleNotifier` | Prints blocking reasons to terminal |
| `AgentRunner` | `ClaudeCodeRunner` | Runs Claude Code CLI (from rigg) |

## Example

```bash
# Scaffold a tasks directory and start the dashboard
lotsa init
lotsa serve
```

Then open the dashboard in your browser and create your first task.

## Development

```bash
# Run lotsa tests
python -m pytest lotsa/tests/ -v

# Run all tests (rigg + lotsa)
python -m pytest rigg/tests/ lotsa/tests/ -v

# Lint
ruff check lotsa/
ruff format --check lotsa/
```
