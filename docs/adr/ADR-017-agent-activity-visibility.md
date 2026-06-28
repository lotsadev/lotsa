# ADR-017: In-Lotsa agent activity visibility

**Status**: Implemented
**Date**: 2026-05-24
**Related**: ADR-013 (orchestrator owns git state), ADR-014 (jobs as unified primitive — `AgentRunner` is the abstraction extended here), `rigg/agent_runner.py`, `lotsa/orchestrator.py`, `lotsa/server/api_routes.py`

---

## Context

Lotsa's value proposition includes a dashboard that operators use to supervise long-running agent tasks. The dashboard surfaces task state, messages, artifacts, and the diff — but it does **not** surface what the agent is *currently doing*. As soon as an agent dispatch starts, Lotsa waits for the subprocess to return its final JSON; nothing about the in-flight execution (tools called, files read, bash commands issued, time spent thinking) is visible from the UI.

When an agent stalls or runs unusually long today, the diagnosis flow is:

1. `ps aux | grep claude` to confirm the agent process is alive.
2. `lsof -p <pid>` to inspect blocked pipes and identify what file/socket the process is waiting on.
3. Manual inspection of the session JSONL at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` via Claude Code on the side, or `jq` from the terminal.
4. Cross-reference with the worktree's file mtimes to confirm forward progress.

This shipped concretely as a real incident on 2026-05-24: a coding agent on task `<redacted>` hung for 44 minutes on a deadlocked pytest test. Diagnosis happened entirely outside Lotsa — `ps`, `lsof`, manual JSONL parsing. The fix (kill the deadlocked pytest, let the agent see EOF and continue) was equally external.

The product cost of this is twofold:

- **Operators without that toolchain are blocked.** Lotsa's positioning includes non-engineering users (per ADR-014's "this isn't a niche concern" framing — research, content, ops processes). An ops manager triaging a stuck agent has no business `lsof`-ing a Python PID.
- **Engineering operators waste cycles** doing forensic work the dashboard could have shown in one click. Each incident burns 5–30 minutes that should be 30 seconds.

The pieces needed to fix this already exist in the codebase:

- Claude Code persists every event — tool use, tool result, thinking block, text — **incrementally** to its session JSONL file, even when invoked with `--print --output-format json`. The file is written and `fsync`ed as the agent works.
- Lotsa already extracts the agent's `session_id` from the parsed CLI output (`rigg/parsing.py:84`, surfaced via `AgentResult.session_id` at `rigg/models.py:36`) and persists it into `TaskRow.metadata["session_id"]` (`lotsa/orchestrator.py:1933-1934`). It also already reads that field back on subsequent dispatches to pass `--resume` (`lotsa/orchestrator.py:1842-1844`).
- The dashboard's right panel has a tab-based layout with two tabs today (Artifacts, Changes) at `lotsa/frontend/src/components/right-panel/right-panel.tsx:51-56` — directly extensible.
- Per-step elapsed time is already tracked: `InFlightStep.started_at` at `lotsa/orchestrator.py:213`, surfaced as `elapsed_s` on `TaskSummary` and `TaskDetail` at lines 371 and 397.

The data is there. Lotsa just doesn't read it back from the agent runner and doesn't surface it to the operator.

---

## Decision

Add a per-task **Agent Activity** view to Lotsa, backed by a new protocol method on `AgentRunner` that returns recent activity events from the runner's native persistence. Pair it with a **soft timeout indicator** on each task summary so operators see "this task has been running 30 min, looks abnormal" at a glance.

The decision is made of six concrete commitments. They ship together as one feature ("see and triage in-flight agents from the dashboard"); item-by-item phasing is unnecessary at this scope.

### 1. `AgentRunner.read_activity` protocol method

Extend the `AgentRunner` Protocol in `rigg/agent_runner.py:26`:

```python
class AgentRunner(Protocol):
    async def run(self, ...) -> AgentResult: ...

    async def read_activity(
        self,
        session_id: str,
        work_dir: Path,
        since_index: int = 0,
        limit: int = 200,
    ) -> ActivityResult: ...
```

Returns the agent's activity events from `since_index` onwards, up to `limit` events. Read-only — never mutates session state. Safe to call against an in-flight session (the JSONL is being appended-to, not rewritten).

Default implementation (Protocol-level): returns `ActivityResult(events=[], supported=False)`. Runners that implement the method override this. A dashboard receiving `supported=False` shows the "Activity unavailable for this runner" empty state.

### 2. `ActivityResult` and `ActivityEvent` models

New dataclasses in `rigg/models.py`:

```python
@dataclass
class ActivityEvent:
    index: int                    # monotonic per-session; client passes as since_index
    timestamp: datetime
    kind: Literal[
        "thinking",               # the model's reasoning trace
        "tool_use",               # the agent invoked a tool
        "tool_result",            # tool returned (success or error)
        "text",                   # the agent emitted assistant text
        "system",                 # session metadata, errors, lifecycle events
    ]
    summary: str                  # one-line human-readable label
    detail: dict[str, Any] | None # kind-specific structured detail; truncated

@dataclass
class ActivityResult:
    events: list[ActivityEvent]
    supported: bool               # False = runner has no implementation
    session_complete: bool        # True when the session JSONL stopped growing
    next_index: int               # value to pass as since_index on the next poll
```

Detail truncation policy (in the runner implementation, not the protocol):

- **`tool_use`**: tool name (full); first 200 chars of input args, with a `truncated: true` flag if more.
- **`tool_result`**: success/error flag; first 500 chars of result content.
- **`text`** and **`thinking`**: first 1000 chars; `truncated: true` flag.

Truncation matters because session JSONL records carry full prompts and full file reads — surfacing the raw bytes would blow out the API response size. Full content remains in the JSONL on disk; a future "expand event" endpoint can read a single event's full record if needed (not in this ADR).

### 3. `ClaudeCodeRunner.read_activity` implementation

`ClaudeCodeRunner` in `rigg/agent_runner.py` implements `read_activity` by reading the Claude Code session JSONL.

**Path resolution.** Claude Code writes session files to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, where `encoded-cwd` is the agent's working directory with `/` replaced by `-` and a leading `-`. The runner computes this path from `work_dir` (already a parameter the runner accepts on `run()` calls).

**Read strategy.** Open the file read-only, `seek` to the position implied by `since_index`, read appended lines, parse each JSONL record, project into `ActivityEvent`. Use `aiofiles` (already a dependency or trivially added) to keep the read off the event loop's thread. Bound by `limit` events per call; the client polls again with the new `next_index` if there are more.

**Mapping JSONL → ActivityEvent.** Claude Code session records have a `type` field (`assistant`, `user`, `summary`, etc.) and a `message.content` array of typed blocks (`text`, `thinking`, `tool_use`, `tool_result`). The mapping is straightforward:

| JSONL block type | ActivityEvent.kind | summary |
|---|---|---|
| `thinking` | `thinking` | first line of `thinking` field, trimmed |
| `tool_use` (`name: "Bash"`) | `tool_use` | `Bash: <first 80 chars of command>` |
| `tool_use` (`name: "Read"`) | `tool_use` | `Read: <file_path>` |
| `tool_use` (other) | `tool_use` | `<tool_name>: <best-effort first input>` |
| `tool_result` | `tool_result` | `← <ok\|error>` |
| `text` | `text` | first line, trimmed |
| `summary` (session-level) | `system` | the summary text |

The runner is responsible for picking what makes a useful summary per tool — operators reading the activity feed should be able to grok "what is the agent doing" without expanding every event.

**Session-completion detection.** The session JSONL gains a final `summary` record when the CLI exits cleanly. The runner uses presence of that record to set `session_complete=True`. For sessions that crashed or were killed, the file simply stops growing; the orchestrator's existing dispatch result (success / failure / timeout) is the source of truth for "did this dispatch succeed."

**`DockerAgentRunner` inheritance.** The Docker runner at `lotsa/docker_runner.py` wraps the same `claude` CLI inside a container; the agent inside the container writes to the host-mounted Claude config directory (which Lotsa mounts so `--resume` works across runs). The host path is the same `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. `DockerAgentRunner.read_activity` can delegate to a module-level helper shared with `ClaudeCodeRunner` rather than duplicate the parser.

### 4. New endpoint: `GET /api/tasks/{task_id}/agent-activity`

New route in `lotsa/server/api_routes.py`. Query params:

- `since` (int, default 0) — return events with `index >= since`.
- `limit` (int, default 100, max 500) — pagination cap.

Response shape (Pydantic in `lotsa/server/schemas.py`):

```python
class AgentActivityResponse(BaseModel):
    session_id: str | None         # None if the task hasn't dispatched yet
    runner_supports_activity: bool
    session_complete: bool
    events: list[AgentActivityEventResponse]
    next_index: int

class AgentActivityEventResponse(BaseModel):
    index: int
    timestamp: datetime
    kind: str                      # mirrors ActivityEvent.kind
    summary: str
    detail: dict[str, Any] | None
    truncated: bool
```

Lotsa loads the task, pulls `session_id` from `task.metadata`, looks up the current `AgentRunner` from `OrchestratorService`, calls `runner.read_activity(...)`, and projects to the response. Errors (file not found, parse failure on one record) return `events=[]` and a `session_id` if known — the dashboard surfaces "no activity yet" rather than a 500.

Polling, not streaming. Matches every other endpoint in `api_routes.py`. SSE/WebSocket is deferred — see **Out of scope**.

### 5. Dashboard Activity tab

New `ActivityTab` component in `lotsa/frontend/src/components/right-panel/activity-tab.tsx`. Inserted into `right-panel.tsx` as a third tab after Artifacts and Changes (insertion point: the `TabsList` block at lines 51–56 today).

UI behaviour:

- **Timeline-style list** of events, newest at the top, with relative timestamps ("12s ago", "3m ago").
- **Polls every 2s** while `task.status === "working"` (matches the dashboard's existing live-update cadence); every 30s otherwise.
- **Incremental fetch:** stores the last `next_index` returned and passes it as `since` on the next call. Eliminates re-transferring events the client already has.
- **Empty state for `runner_supports_activity: false`** — explains that this runner does not expose activity.
- **Empty state for `session_id: null`** — "Agent has not dispatched yet."
- **"Agent looks idle" badge** when `(now - last_event.timestamp) > 120s` and `task.status === "working"`. Operator-facing signal that the agent might be stuck.
- **Expand-on-click** for `detail` content. Truncated previews by default; the detail dict from `ActivityEvent` displays as a syntax-highlighted JSON block on expand.

The auto-switch logic in `right-panel.tsx:20-27` (currently switches to Changes when a task starts running, Artifacts when it finishes) extends to switch to Activity if Changes is empty but Activity has new events. Refinement is post-merge feedback territory.

### 6. Soft timeout indicator

Flow job definitions in `lotsa/flows.py` gain optional fields:

```yaml
- name: code
  type: agent
  prompt: code
  timeout_warn_seconds: 1200      # 20 min — surface yellow badge
  timeout_kill_seconds: 3600      # 60 min — surface red badge (Phase 1: informational only)
```

Both default to `None` (no badge). Per-flow defaults (a `timeouts:` block at the flow level) can be added later if patterns emerge; not in this ADR.

The orchestrator already computes `elapsed_s` against `InFlightStep.started_at` at `lotsa/orchestrator.py:371` (for summaries) and `397` (for details). Extend the response with a `timeout_status` enum:

```python
timeout_status: Literal["ok", "warn", "over"] = "ok"
```

Computed at response-build time from `elapsed_s` against the active step's thresholds. Frontend renders a yellow dot next to the task name at `warn`, red at `over`, no dot at `ok`.

**Phase 1 ships the indicator only — no auto-kill.** `timeout_kill_seconds` is informational. Auto-kill semantics (recovery flow, retry vs. block, message-trail expectations) are the subject of a separate future ADR.

### 7. `lotsa inspect <task-id>` CLI

New click command in `lotsa/cli.py`. Opens the SQLite DB, reads `task.metadata["session_id"]`, calls the same `read_activity` helper, prints the last N events to stdout in a terminal-friendly format. For:

- Operators debugging from a server without a browser session.
- CI and automation that wants to grep agent activity programmatically.
- Local development of new runners (test `read_activity` without booting the dashboard).

Flags: `--task-id <id>`, `--limit <n>` (default 50), `--since <index>` (default 0), `--watch` (re-poll every 2s like the dashboard).

---

## Why this isn't a niche concern

Lotsa's "in-dashboard visibility into running agents" gap is not unique to Lotsa. **Every agent-orchestration platform has the same blind spot** when wrapping CLIs that return final results only:

- **LangGraph** users routinely write custom callback handlers to mirror agent state into a separate observability store, because the framework itself returns the final result.
- **AutoGen** has a similar pattern — `register_reply` hooks that operators wire up to log activity externally.
- **Microsoft Copilot agent mode**, Cursor's agent mode, Aider — all surface "running" status without exposing real-time tool calls.

The shape that scales across these tools is **persistence-based read, not in-process streaming**:

| Approach | Trade-offs |
|---|---|
| In-process streaming (subscribe to stdout) | Tightest latency. Requires running agent and supervisor in the same process; falls over on container/process boundaries; protocol-coupled per CLI. |
| Webhook / callback pushed *from* the agent | Agent runtime must know it has a supervisor. Adds an integration surface to every agent CLI. |
| **Persistence-based read** | Agent persists for its own reasons (resume, audit). Supervisor reads when needed. Decoupled. Latency = polling interval. |

Persistence is what every responsible agent runtime is moving toward: Claude Code has session JSONL; OpenCode persists conversations; Codex CLI maintains history; chat APIs persist via `--continue` or `/resume` flags. Lotsa piggybacking on that persistence is the **portable shape**.

The corollary: this ADR's `AgentRunner.read_activity` is the seam. Each runner Lotsa adds (OpenCode, Codex, future first-party runners) implements `read_activity` by reading its own persistence. The dashboard and CLI don't change.

---

## Tradeoffs

**Pros:**

- **Operators get visibility without leaving the dashboard.** The 44-minute `lsof` incident becomes a 30-second tab check.
- **Reuses Lotsa's existing data.** `session_id` is already persisted, `elapsed_s` is already tracked, the right-panel tab structure already exists. The ADR adds wiring, not new persistence.
- **Polling fits Lotsa's existing API style.** No new transport (SSE, WebSocket) introduced; all the existing dashboard endpoints poll, and the front-end already has the cadence machinery.
- **Generalises across runners.** `AgentRunner` is the established abstraction (ADR-014). `read_activity` is one more protocol method — runners that don't implement it return `supported=False` and the dashboard degrades gracefully.
- **The soft timeout indicator surfaces "this is weird" without committing to enforcement.** Operators can investigate stuck agents proactively; auto-kill semantics get their own ADR.
- **The CLI inspector covers headless and CI use cases** without parallel implementation work — same protocol method.

**Cons:**

- **Reads files Lotsa does not own.** Claude Code's session JSONL location and format are an internal contract of the Claude Code CLI, not a public API. If Claude Code changes the path or schema, `ClaudeCodeRunner.read_activity` breaks. Mitigation: version-check `claude --version` at runner construction and log a warning if the version is outside a tested range; tests pin a known-good fixture format. The breakage mode is "Activity tab shows empty"; not silent corruption.
- **Polling latency.** 2 seconds of staleness is fine for human supervision but not for tight integration loops. If a future use case needs sub-second freshness, SSE is the upgrade path.
- **Per-runner parser cost.** Every new `AgentRunner` implementation that wants Activity support has to write its own JSONL → ActivityEvent projection. Acceptable — the alternative (a generic event schema across CLIs) is worse, because each CLI's event vocabulary is genuinely different (Claude Code thinks in tool blocks; OpenCode in messages; Codex in patches).
- **Doesn't catch CPU-bound infinite loops.** An agent caught in a JSON-parsing loop or model-side hallucination loop generates events the whole time; "looks busy" isn't the same as "making progress." The soft timeout indicator is the complementary signal for that case; process-tree CPU monitoring (separate ADR) is a third signal if needed.
- **Truncation hides relevant detail.** A `Bash` command longer than 80 chars in the summary, or a tool result longer than 500 chars in the detail, requires the operator to expand. Acceptable trade-off; un-truncated payloads would make the API response unbounded.
- **New surface in three places** (protocol, API, frontend tab). Three implementations means three places to keep in sync when the activity schema evolves. The dataclass + Pydantic mirroring is the standard pattern in this codebase; no new pattern needed.

---

## Backward compatibility

**No DB schema changes.** `session_id` is already in `TaskRow.metadata` (a JSON column). `timeout_warn_seconds` and `timeout_kill_seconds` are new optional fields on `Job` — existing flow YAML files that don't set them get the default (`None`, no badge).

**No breaking changes to `AgentRunner`.** The protocol gets one new method with a Protocol-level default implementation that returns `ActivityResult(events=[], supported=False)`. Existing runners (`ClaudeCodeRunner` and `DockerAgentRunner` ship with `read_activity`; third-party runners without it continue working — the dashboard shows the "unavailable" empty state).

**No breaking changes to the API.** The new endpoint is additive. Existing dashboard polls (`useTask`, etc.) are untouched.

**Dashboard rollout is additive.** The Activity tab appears alongside Artifacts and Changes; users who don't open it lose nothing.

---

## Scope

This ADR proposes the architectural direction. The implementation PR(s) will:

1. Add `ActivityEvent` and `ActivityResult` dataclasses to `rigg/models.py`.
2. Extend the `AgentRunner` Protocol in `rigg/agent_runner.py` with `read_activity` and a Protocol-default implementation.
3. Implement `ClaudeCodeRunner.read_activity` (session JSONL reader, mapping, truncation). Add fixtures of recorded session JSONL records for parser tests.
4. Implement `DockerAgentRunner.read_activity` (delegates to the shared parser, computes the host-mounted path).
5. Add `timeout_warn_seconds` and `timeout_kill_seconds` fields to `Job` / `ResolvedJob` in `lotsa/flows.py`.
6. Extend the orchestrator's `_summary_for` / `_detail_for` helpers (the existing `elapsed_s`-attaching code at `orchestrator.py:369-371, 395-397`) to compute and attach `timeout_status`.
7. Add `GET /api/tasks/{task_id}/agent-activity` to `lotsa/server/api_routes.py` and the matching Pydantic models in `schemas.py`.
8. Add `ActivityTab` to `lotsa/frontend/src/components/right-panel/`. Wire into `right-panel.tsx` as the third tab.
9. Render the soft timeout badge in the sidebar task list (`lotsa/frontend/src/components/sidebar/`).
10. Add `lotsa inspect <task-id>` to `lotsa/cli.py`.

In scope for **Lotsa Community Edition** (`lotsa/` + `rigg/`).

---

## Out of scope

- **Auto-killing stuck agents at `timeout_kill_seconds`.** Phase 2. Needs its own ADR covering recovery semantics: does the task move to `blocked` with a "killed by timeout" message? Does it auto-retry? How does the operator distinguish a timeout-kill from a genuine agent failure? These are policy questions, not implementation details.
- **Streaming activity via SSE or WebSocket.** Only if 2-second polling latency proves insufficient in practice. The polling endpoint shipped here is forward-compatible — the same response shape can be event-pushed by SSE without changing the schema.
- **Process-tree CPU monitoring** of agent subprocesses. Complementary signal to the activity feed and the soft timeout — catches "agent is producing events but making no real progress" and "agent is producing no events but is CPU-pegged on something." Separate ADR; not blocking this one.
- **Activity reading for non-Claude-Code agent runners** (OpenCode, Codex CLI, others). The `AgentRunner.read_activity` Protocol is the seam; each runner implements when it ships. No upfront design work needed.
- **Full-content expansion endpoint.** The truncated detail in `ActivityEvent` is sufficient for triage. If a future use case needs the un-truncated bytes (security review of an agent's bash command, full debug of a tool result), a separate endpoint reads a single event's raw record from the JSONL. Wait for the use case before designing the API.
- **Activity persistence across server restarts.** The session JSONL persists on disk, so `read_activity` works after restart for any task whose `session_id` is still in `task.metadata`. There is no in-Lotsa mirror table; one isn't needed because the JSONL is durable. (If the JSONL is deleted out-of-band, the Activity tab shows the "no activity available" empty state.)
- **A general agent-observability framework.** This ADR ships a focused supervisor view, not a metrics/tracing/OpenTelemetry surface. The latter is a real product question; out of scope here.
