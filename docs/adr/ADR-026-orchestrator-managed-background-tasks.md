# ADR-026: Orchestrator-managed background task support for agent dispatches

**Status**: Proposed — **RISKY / UNDECIDED**. This ADR documents the
design space and the case for/against shipping it. No path has been
selected. The proposal is captured so we don't re-discover the design
space from scratch the next time the question comes up.
**Date**: 2026-06-08
**Related**: PR #98 (the immediate workaround — `OPERATIONAL_PREAMBLE`
forbids background tasks in agent dispatches), ADR-025 (the layered
`--append-system-prompt` model that surfaced the underlying mismatch),
the failed task `<redacted>` on 2026-06-08 (the diagnostic source for
this ADR).

---

## Context

PR #98 forbids agents from backgrounding commands in their dispatches.
That's a workable rule but not a complete answer. It pays a real cost:
genuinely parallelizable work (long test suites, multi-target builds,
slow external commands) must now serialize, blocking the agent for the
duration. Operators who want their agents to fan out work in parallel
have no path.

The underlying issue isn't an agent choice — it's an architectural
mismatch:

- **Claude Code's Bash tool auto-promotes long-running commands to
  background mode** and returns a tool result that says *"Command
  running in background … You will be notified when it completes."*
- **The notification mechanism is REPL-specific.** Interactive Claude
  Code runs as a long-lived daemon that polls background tasks and
  injects completion notifications into the next turn.
- **Lotsa dispatches with `claude --print`** — one-shot, headless. No
  daemon, no next turn. The agent's turn ends with `stop_reason:
  end_turn`, the `claude` process exits, and the child subprocesses
  (the backgrounded commands) are reaped by the parent's exit. The
  output files (`/private/tmp/claude-501/.../tasks/<id>.output`) are
  left at 0 bytes.

The Bash tool's tool-result text is identical in both modes. The agent
has no way to know from the tool result which mode it's in, and its
training reasonably biases toward "wait for notification" because in
interactive mode that's the efficient choice.

We observed this on task `<redacted>` (2026-06-08):

| Step | Wall | Cost | Output tokens | Final visible output |
|---|---|---|---|---|
| code | 24.7 min | $9.53 | 30,126 | *"I'll wait for the notification that these background tasks complete rather than spawning more polls."* |
| review attempt 2 | 14.4 min | $2.40 | 17,825 | *"Still running. Waiting for the completion notification."* |

Four `.output` files for that task were all 0 bytes hours after the
dispatch. The backgrounded subprocesses had been reaped before they
could write anything useful.

PR #98 closes the immediate bleed by telling the agent (via
`OPERATIONAL_PREAMBLE`) that background mode doesn't work in this
dispatch and instructing it to poll synchronously or run foreground.
This ADR is about whether — and how — Lotsa should provide first-class
support for background tasks instead.

## The design space

There are several distinguishable approaches. Each has serious
trade-offs.

### Approach A — Foreground-only (PR #98 status quo)

The agent runs all commands foreground. No parallelism. Slow commands
block the turn linearly. If a command is genuinely too slow to fit in
one turn, the agent splits it (runs a subset) rather than
backgrounding it.

- **Pros:** Simple. Already shipped. No new failure modes.
- **Cons:** No parallelism at all. A 30-minute test suite blocks the
  whole turn. The agent can't fan out independent work-streams.
- **When sufficient:** Real-world workloads where the slowest single
  command fits within a reasonable turn budget. Most current Lotsa
  flows fit this.

### Approach B — In-turn polling

The agent is told (already in PR #98) that if the Bash tool
auto-promotes a command to background, it should poll the output file
via `Read` synchronously within the same turn until the command
completes.

- **Pros:** No orchestrator changes required. Agent can do other
  things between polls (read files, formulate plans).
- **Cons:** Still no cross-turn parallelism. Each `Read` poll costs
  tokens. The output file is 0 bytes because the subprocess is still
  alive — but it dies as soon as the agent's turn ends, so the agent
  *must* finish in one turn. If the command takes longer than the
  agent has budget for, the work is still lost.
- **When sufficient:** Commands long enough to want async, but short
  enough to wait for in one turn. Narrow band.

### Approach C — Tail-and-resume (the user's first instinct)

Lotsa detects pending background tasks in the transcript at turn-end,
tails the output files, and resumes the agent session via `claude
--print --resume <sid>` once the commands complete. The completion
result is injected as a synthetic user message.

- **Pros:** Conceptually clean. Agent's mental model ("I'll be
  notified") is preserved. The orchestrator simulates the REPL between
  turns.
- **Fatal flaw observed:** **The background subprocess is killed when
  the parent `claude --print` process exits.** The output file stays
  at 0 bytes. Tailing produces nothing. Tested empirically on task
  `<redacted>` — four 0-byte files hours after dispatch confirmed the
  reaping. The "you will be notified" promise is broken at the
  subprocess level, not just the messaging level.
- **What would unblock this:** A way to make the backgrounded
  subprocess survive the parent's exit. Possible mechanisms:
  `setsid`/`nohup` wrapping (might or might not work depending on how
  Claude Code spawns subprocesses), or a Lotsa-side intercept that
  wraps every Bash command in a `nohup`-equivalent before it reaches
  the OS. Neither is mechanically tested.
- **Risk class:** Even if the subprocess survives, `claude --print
  --resume <sid>` may not deliver the completion notification — that
  mechanism is REPL-specific and may not fire on resume. We'd have to
  inject the result synthetically via the resume's user message, and
  trust that the agent picks up where it left off coherently.

### Approach D — Hold the parent alive

Lotsa keeps the `claude --print` process alive past the agent's
`stop_reason: end_turn` so the subprocesses don't get reaped. The
orchestrator polls; once tasks complete, the orchestrator sends the
agent a follow-up message somehow.

- **Pros:** Doesn't require subprocess reparenting. Subprocesses
  survive because parent is still alive.
- **Cons:** Not clear `claude --print` supports being "held alive" —
  the CLI is designed to print and exit. We'd need to fork Claude
  Code or contract upstream for a new mode. Without that, this isn't
  buildable from outside.
- **Risk class:** Engineering risk (depends on Claude Code internals)
  and forward-compatibility risk (any upstream change breaks us).

### Approach E — Intercept the Bash tool

Lotsa intercepts every `Bash` tool call before Claude Code executes
it, runs the command itself as a detached process (e.g. via
`setsid`/`nohup` or a `subprocess.Popen` whose lifetime Lotsa
manages), captures output to a Lotsa-owned file, and synthesizes the
tool result back to Claude Code. When Lotsa's detached process
completes, Lotsa resumes the agent session with the result injected.

- **Pros:** Lotsa owns the subprocess lifecycle; reaping isn't an
  issue. The agent sees normal tool results.
- **Cons:** Requires intercepting tool calls, which `claude --print`
  doesn't expose. The natural place to intercept is inside Claude
  Code (impossible from outside) or by replacing the Bash tool with
  an MCP tool that Lotsa owns. The latter is possible (Claude Code
  supports MCP tools) but means **disabling the built-in Bash tool**
  in dispatch and routing through an MCP server — a large surface
  change with its own implications (loss of built-in safety hooks,
  permission model differences, etc.).
- **Risk class:** Large implementation surface, large change in how
  agents see the toolset, potential safety regressions.

### Approach F — Switch to the Anthropic Agent SDK

Stop using `claude --print` entirely. Drive the agent via the Agent
SDK (TypeScript or Python), which provides a programmatic interface
to a long-running agent session. Lotsa would run the agent in-process
(or as a managed subprocess) and have first-class access to tool calls
and lifecycle events.

- **Pros:** This is what the SDK is for. Likely the only path to
  feature-parity with interactive Claude Code's full capability set
  (background tasks, hooks, subagents, etc.). Closes this gap and
  others we haven't hit yet.
- **Cons:** Large architectural shift. Rewrite of `agent_runner.py`
  and `docker_runner.py`. New dependency on the SDK
  (`@anthropic-ai/claude-agent-sdk` or `claude-agent-sdk`).
  Auth/permission/credential plumbing changes. The Docker mode
  becomes complicated (SDK inside container vs. driving from
  outside). Many unknowns.
- **Risk class:** Engineering effort. Disruption to current users
  during migration. Possible upstream breakage as the SDK evolves.

## Why we haven't decided

Every approach has at least one of: insufficient power (A, B), a
mechanic that doesn't work today (C), an undocumented dependency on
internal behavior (D), a large blast radius (E), or a substantial
migration cost (F). None of them is obviously the right call.

There's also a category of evidence we don't have yet:

- **How much do real-world Lotsa flows actually need background
  parallelism?** PR #98's foreground-only rule is shipping; we don't
  yet have data on whether it's a real ceiling or a notional one.
  Until we observe specific tasks that genuinely can't fit in
  foreground-poll, we're optimising for a hypothetical workload.
- **Does Claude Code's `--resume` mechanism inject pending
  notifications?** We assumed not in PR #98's commit body, but
  haven't tested it. If yes, Approach C becomes potentially viable
  (the only remaining problem is keeping the subprocess alive).
- **Can `setsid`/`nohup` wrap the Bash tool's invocations
  externally?** Worth a 10-minute test. If yes, Approach C is alive.
  If no, Approach C is permanently blocked from outside `claude`.
- **What is upstream's roadmap?** If Claude Code is planning a
  background-survives-print mode, Approach D and the wait-and-see
  paths become attractive. If not, Approach F's pull gets stronger.

## What would change our mind

The decision flips toward shipping *something* when one or more of:

1. A specific user task genuinely cannot complete under PR #98's
   foreground-only rule. We need to see the workload, not assume it.
2. Multiple operators (more than one) ask for background support
   independently. One operator can be served by working around the
   limitation in their task descriptions.
3. A 10-minute experiment proves `setsid`-wrapping survives `claude
   --print` exit. That dramatically reduces Approach C's risk.
4. Upstream Claude Code ships native support for the dispatch shape
   we want (e.g. a `--print` mode that respects background tasks).
   Trivially solves the problem.

Absent any of these, the right call is to leave PR #98 in place,
collect real-world data, and revisit when we have evidence.

## Risks of shipping any of these

| Approach | Primary risk |
|---|---|
| A (current) | Operators hit a parallelism ceiling we can't see; tasks become impractically slow on large suites |
| B | Tokens spent polling; max-output-token cap kills in-turn polls; agent loses work if turn budget runs out mid-poll |
| C (with subprocess survival) | Notification injection on resume may not work; agent context drift between turns; partial outputs from killed-then-resurrected processes |
| D | Forking Claude Code or depending on undocumented internals; breakage on every upstream release |
| E | Replacing the Bash tool with an MCP wrapper changes the safety surface; agent sees a different toolset; edge cases in compatibility |
| F | Large migration; SDK churn; in-flight tasks broken during migration window; new categories of bugs we haven't anticipated |

## Decision

**No path selected. Status: Proposed, deferred indefinitely.**

The narrowing in PR #98 is the current operational answer. The right
follow-up is observation, not implementation:

1. Operate Lotsa under PR #98's foreground-only rule for an
   extended period.
2. Track tasks that fail or time out under the rule. Specifically,
   tasks where the foreground command would have legitimately
   benefited from background execution.
3. When we have ≥3 such tasks across ≥2 operators, re-open this ADR
   and choose a path with concrete evidence.

If the experiment in *"What would change our mind"* point 3
(`setsid`-wrapping) is run before then, document the result here.

## Out of scope

- Subagent dispatch via the `Task`/`Agent` tool. That's a related
  concurrency mechanism but a different one — subagents are managed
  by Claude Code's daemon and have their own lifecycle. If we want
  Lotsa agents to spawn subagents, that's a separate ADR.
- Non-`Bash`-tool long-running operations (e.g. an `mcp__*` tool that
  takes minutes). Same architectural class; same set of trade-offs.
  This ADR is written narrowly around the observed Bash failure
  mode but the conclusions generalize.
- Replacing `claude --print` with a different invocation shape
  (interactive PTY-driven, Agent SDK, etc.) just to fix this. That's
  Approach F above and would justify its own ADR if pursued.
