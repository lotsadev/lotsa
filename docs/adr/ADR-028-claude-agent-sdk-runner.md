# ADR-028: Claude Agent SDK runner — a third runner shape alongside CLI and API

**Status**: Partially implemented (Phases 1, 2 & 3 shipped)
**Date**: 2026-06-09
**Related**: ADR-023 (multi-provider runner registry — this ADR
adds a third *shape* to the taxonomy that ADR-023 established for
CLI- and API-shaped runners), ADR-014 (jobs as unified flow
primitive — what each runner dispatches against), ADR-022
(per-step model selection — the operator-facing knob that picks
which runner handles each step), ADR-026 (orchestrator-managed
background tasks — the SDK shape this ADR proposes makes
ADR-026's approaches reachable; see *Relationship to ADR-026*
below), `rigg/agent_runner.py` (the `AgentRunner` Protocol
the SDK runner implements), `docs/notes/
2026-06-09-dont-build-your-own-ai-harness.md` (the lessons that
motivated this ADR).

---

## Implementation status

| Phase | Scope | State |
|---|---|---|
| **1** | `ClaudeAgentSDKRunner` in `rigg/`, the `claude-agent-sdk` dependency, `__all__` export, and a minimal global `--runner` / `LotsaConfig.runner` selector wired into `_build_runner` | **Shipped** |
| **2** | Runner-aware preamble: `CLI_DISPATCH_SHAPE_FRAGMENT` constant, `dispatch_shape_prompt()` on every concrete runner, the `OPERATIONAL_PREAMBLE` split, and orchestrator concatenation | **Shipped** |
| **3** | Per-step `runner:` field in `process.yaml` | **Shipped** |
| **4** | Flip the default runner CLI→SDK | **Deferred** per this ADR — gated on operational data, not on this cut. |

Out of scope for the shipped phases (lands in follow-up ADRs): the
Lotsa-side **tool-interception surface** — a Lotsa-owned `AskUserQuestion`
that resolves via the dashboard, background-`Bash` lifecycle, subagent
dispatch, and `Monitor`/`ScheduleWakeup` via SDK resume.

### Two refinements made during implementation

1. **The SDK-shape fragment is honest to *wired* capability.** This ADR's
   Phase-2 sketch (below) illustrates an SDK fragment that advertises
   `Monitor` / `ScheduleWakeup` / `AskUserQuestion` / background `Bash` as
   usable. Because the interception surface is **not** built in this cut,
   advertising those tools would reintroduce exactly the abstraction leak
   the SDK runner exists to fix. The shipped `SDK_DISPATCH_SHAPE_FRAGMENT`
   therefore describes the SDK environment truthfully — programmatic,
   single-turn for now — and does **not** advertise un-wired cross-turn
   tools. The illustrative "does not forbid those tools" smoke test in
   *Phase 2* below is superseded by this honesty rule.

2. **Self-hostable footprint, stated accurately.** The Python
   `claude-agent-sdk` is not a pure HTTP wrapper around `/v1/messages`; it
   drives the Claude Code CLI/Node runtime under the hood. The auth win
   still holds — it reads `ANTHROPIC_API_KEY` programmatically (and honours
   `ANTHROPIC_BASE_URL` for an enterprise-hosted proxy), with no keychain
   and no `--dangerously-skip-permissions` *login trick* — but the dependency
   still carries a transitive CLI-runtime requirement rather than being a bare
   API client. (The win is the *auth* flow; the *permission* posture is
   unchanged in this cut: the runner sets `permission_mode="bypassPermissions"`,
   the SDK-equivalent bypass, required for headless operation. Real per-tool
   gating is the interception surface below, out of scope here.) A genuinely CLI-free pure-API shape is the *API-shaped*
   runner from ADR-023, out of scope here. The dependency is install-time
   only and imported lazily inside `run()`, so operators who don't select
   the SDK runner incur no runtime cost.

---

## Context

ADR-023 established that Lotsa should support **multiple agent
runners** dispatched via a registry: `ClaudeCodeRunner` for Claude
Code, `OpenCodeRunner` for opencode, `AiderRunner` for Aider, etc.
It introduced two distinct *shapes* for those runners:

- **CLI-shaped** — the runner subprocess-execs a provider's agent
  CLI (`claude --print`, `aider --no-stream`, etc.). The CLI owns
  the agent loop; the runner is a glue layer.
- **API-shaped** — the runner uses a multi-provider client like
  LiteLLM to make chat-completions calls and implements the agent
  loop (tool use, session memory, marker protocol) in Lotsa's own
  process.

This ADR proposes a **third shape**: **SDK-shaped** runners that
use a provider's official Agent SDK programmatically. The
motivating instance is Anthropic's
[`@anthropic-ai/claude-agent-sdk`](https://docs.anthropic.com/en/api/agent-sdk)
(JS) / `claude-agent-sdk` (Python). The same shape could be applied
to other providers' SDKs as they mature (e.g. OpenAI Responses SDK
with agent extensions).

### Why a third shape is needed

Throughout late May and early June 2026 we burned roughly two days
debugging Lotsa's `ClaudeCodeRunner` (CLI-shaped). The story is in
`docs/notes/2026-06-09-dont-build-your-own-ai-harness.md`. The
short version:

The Claude Code agent's behaviour is built for an interactive REPL
with a long-lived daemon. Lotsa wraps it with `--print` (one-shot
headless). The agent reaches for cross-turn patterns
(`Monitor`, `ScheduleWakeup`, `Task` subagent delegation,
`AskUserQuestion`, Bash background mode) that work in the REPL and
silently fail in `--print`. We've shipped:

- **PR #92** — operator-config bleed isolation
- **PR #93** — `PWD` env leak
- **PR #95** — planning prompt no-branch
- **PR #96** — layered system prompt (preset + append)
- **PR #98** — no Bash background in OPERATIONAL_PREAMBLE
- **PR #100** — `--allowedTools` allowlist (which turned out to be
  a placebo — `--dangerously-skip-permissions` bypasses it)
- **PR #103** — environment-context preamble + drop the placebo

Each fix was specific to a way the CLI shape leaks abstractions the
agent thinks it has access to. Every new tool Claude Code ships
extends the surface we have to plug. The CLI shape is *fundamentally
mismatched* with our dispatch model.

The Agent SDK is the primitive Anthropic publishes **specifically
for products like Lotsa**. Its docs say:

> Use a custom prompt when your agent's surface, identity, or
> permission model differs from Claude Code's.

That's us. Our surface is a dashboard; our identity is "Lotsa's
spec step"; our permission model is operator-via-dashboard. The
SDK exposes:

- Programmatic tool-call interception (we decide what each tool
  call resolves to)
- Session lifecycle we control (we decide what happens between
  turns — Lotsa can wake the agent up when an answer arrives,
  inject a synthetic tool result, etc.)
- Cross-turn state we manage
- An auth model defined by env vars / API keys, not the keychain
  trick `--dangerously-skip-permissions` requires
- Tool registration where Lotsa can supply *its own* tool
  implementations (a Lotsa-controlled `AskUserQuestion` that resolves
  via the dashboard, a Lotsa-controlled background-runner that
  resumes the agent when done, etc.)

The CLI shape can never expose those primitives because the CLI is
designed to be self-contained — it spawns, runs to completion,
exits. Anything that requires the orchestrator to participate in
the agent's lifecycle isn't reachable through `--print`.

ADR-023 anticipated this: it explicitly notes that *"API runners
give us more control… but require building an agent harness."* The
SDK shape is the *third* option: someone else's agent harness, with
the control surface API runners promise.

## Decision

**Add a `ClaudeAgentSDKRunner` as a new `AgentRunner`
implementation, registered in the ADR-023 registry alongside
`ClaudeCodeRunner`. The SDK runner is a peer to the CLI runner, not
a replacement.** Operators choose which to use per task or per step
via `process.yaml`'s existing `model:` field (ADR-022) and the
runner-name resolution that ADR-023 introduces.

The ADR-023 taxonomy gets a third entry:

| Shape | Examples | Lotsa owns | Provider owns |
|---|---|---|---|
| **CLI** | `ClaudeCodeRunner`, `OpenCodeRunner`, `AiderRunner`, `CodexCliRunner` | Subprocess invocation, flag mapping, output parsing | Agent loop, tool execution, session resume |
| **API** | LiteLLM / AISuite-based runner | Agent loop, tool execution, session memory, marker protocol | Just the chat-completions API |
| **SDK** *(this ADR)* | `ClaudeAgentSDKRunner`; future `OpenAIAgentSDKRunner`, etc. | Session orchestration, tool interception, lifecycle control | Agent loop + tool plumbing exposed programmatically |

Each shape has different trade-offs. The SDK shape sits between
CLI and API: more control than CLI (because the SDK exposes
interception points), less work than API (because the SDK provides
the agent loop and tool plumbing). For providers that publish a
mature Agent SDK, this shape is the right default.

### Implementation

#### Phase 1 — Add `ClaudeAgentSDKRunner` to rigg/

New module `rigg/claude_agent_sdk_runner.py`:

```python
class ClaudeAgentSDKRunner:
    """AgentRunner backed by Anthropic's Claude Agent SDK.

    Implements the same AgentRunner Protocol as ClaudeCodeRunner.
    The system prompt, user prompt, work_dir, session_id semantics
    are identical. Cost reporting, output capture, and error
    classification follow ADR-023's contract.

    What this runner provides that the CLI runner cannot:

    - Cross-turn lifecycle: when the agent's turn ends with pending
      asynchronous work (background commands, scheduled wakeups),
      Lotsa can resume the session via the SDK with the result
      injected, rather than the subprocess exiting and the work
      being reaped.
    - Tool-call interception: Lotsa registers Lotsa-owned
      implementations for tools that need to participate in
      orchestration (e.g. ``AskUserQuestion`` resolves via the
      dashboard's NEEDS_INPUT path; long-running ``Bash`` invocations
      survive the turn boundary).
    - Programmatic permission grants: no
      ``--dangerously-skip-permissions`` needed. Lotsa configures
      which tools the agent can call directly via the SDK options.
    """
```

The runner registers itself in the ADR-023 registry under names like
`claude-agent-sdk`, `opus-sdk`, `sonnet-sdk` (or whatever the
operator picks in `lotsa.yaml`). Default-runner-name resolution for
`opus` / `sonnet` / `haiku` stays pointed at `ClaudeCodeRunner` for
now; operators opt in to the SDK runner explicitly.

#### Phase 2 — `OPERATIONAL_PREAMBLE` becomes runner-aware

The current preamble's *Your environment* section describes the
`claude --print` dispatch shape: one-shot, no daemon, no UI,
no cross-turn work. **Those constraints apply only to the CLI
runner.** Under the SDK runner, the agent legitimately can:

- Background commands (Lotsa's wrapper keeps them alive across turns)
- Use `Monitor` (Lotsa polls and re-engages the agent)
- Use `ScheduleWakeup` (Lotsa fires it via SDK resume)
- Use `AskUserQuestion` (Lotsa routes it to the dashboard)
- Use `Task` / subagent delegation (the SDK can spawn another agent
  session)

Telling an SDK-runner agent it can't do those things would be
incorrect. The preamble needs runner-specific fragments.

The mechanism: each runner provides a `dispatch_shape_prompt()`
method whose output the orchestrator appends after the OPERATIONAL_
PREAMBLE before sending. `ClaudeCodeRunner.dispatch_shape_prompt()`
returns today's *Your environment* / *How to communicate* /
*Execution patterns* sections. `ClaudeAgentSDKRunner.
dispatch_shape_prompt()` returns a different version that says the
agent's environment supports cross-turn work.

The git-authority, file-scope, and Lotsa-precedence sections of the
preamble stay universal — they're about the *task* shape, not the
*dispatch* shape.

##### Protocol membership

`dispatch_shape_prompt()` is added to the `AgentRunner` Protocol
in `rigg/agent_runner.py`. `rigg/CLAUDE.md`'s stability
contract states: *"Don't add a protocol method without a
default-or-fallback path. Mid-rollout, some implementations will
not yet provide it."* This ADR satisfies the *intent* of that
contract — no runtime hole during rollout — but does so via a
**simultaneous-update strategy** rather than the
default-or-fallback pattern the contract describes literally. See
*Why not a Protocol default with body?* below for the technical
reason the default-fallback pattern doesn't work here. The
simultaneous-update strategy is a deliberate alternative.

Concretely: every concrete runner declares the method explicitly,
and a public module-level constant carries the CLI-shaped body so
each declaration is one line:

```python
# rigg/agent_runner.py
CLI_DISPATCH_SHAPE_FRAGMENT = """..."""

class AgentRunner(Protocol):
    ...
    def dispatch_shape_prompt(self) -> str: ...

class ClaudeCodeRunner:
    ...
    def dispatch_shape_prompt(self) -> str:
        return CLI_DISPATCH_SHAPE_FRAGMENT
```

```python
# lotsa/docker_runner.py
from rigg import CLI_DISPATCH_SHAPE_FRAGMENT

class DockerAgentRunner:
    ...
    def dispatch_shape_prompt(self) -> str:
        return CLI_DISPATCH_SHAPE_FRAGMENT
```

`CLI_DISPATCH_SHAPE_FRAGMENT` is added to `rigg/__init__.py`'s
`__all__` as part of Phase 2 — it crosses the lotsa↔rigg
boundary and must therefore be on the public surface per
`rigg/CLAUDE.md`'s *"Don't import [private things] from
outside rigg/"* rule. `ClaudeAgentSDKRunner` ships its own
SDK-shaped fragment via the same method (no need for the constant —
its body is the SDK-shape preamble, defined within the SDK runner
module).

**Why not a Protocol default with body?** Python `Protocol` classes
in `rigg/` are used for *structural* typing — `ClaudeCodeRunner`
and `DockerAgentRunner` satisfy `AgentRunner` via duck typing, not
via `class ClaudeCodeRunner(AgentRunner)` inheritance. A default
method body on the Protocol is reachable only by classes that
*explicitly* subclass the Protocol (and Protocol subclassing has
its own quirks). For structural implementers the default is
silently absent: calling `runner.dispatch_shape_prompt()` raises
`AttributeError` at runtime. The one-line shim per concrete
runner is the safe path. It also keeps the contract enforced at
the implementation site, where future implementers will look
when adding new runners.

The wiring is small enough that each runner declares it
explicitly in the same PR that introduces the Protocol method —
no migration window where existing runners silently lack the
method.

The fragment constant (`CLI_DISPATCH_SHAPE_FRAGMENT`) is the
load-bearing piece; today's `OPERATIONAL_PREAMBLE` is split so the
CLI-specific portions live in this constant while the universal
portions stay in the preamble. The orchestrator concatenates them
at dispatch:

```python
preamble = OPERATIONAL_PREAMBLE  # universal rules
shape = runner.dispatch_shape_prompt()  # runner-specific
full_prompt = preamble + "\n\n" + shape + "\n\n" + step_prompt
```

Smoke tests assert that `ClaudeCodeRunner.dispatch_shape_prompt()`
returns a fragment containing the cross-turn-tool list, and that
`ClaudeAgentSDKRunner.dispatch_shape_prompt()` returns a fragment
that does *not* forbid those tools.

#### Phase 3 — Operators opt in per-step

`process.yaml` gains a `runner:` field on agent jobs (in addition
to the existing `model:`):

```yaml
- name: code
  type: agent
  prompt: coding
  model: sonnet
  runner: claude-agent-sdk     # opt into SDK runner for this step
```

Without `runner:`, the registry resolves the runner from `model:`
via ADR-023's existing rules. With `runner:`, the named runner is
used and the model is passed to it. Operators choose per step,
because some steps benefit from SDK control (e.g. `verify`'s
conversational REPL fits SDK lifecycle better) while others may stay
on the CLI for cost or familiarity reasons.

#### Phase 4 — Default-runner migration (deferred)

After the SDK runner has run a meaningful number of real tasks and
operators trust it, the registry default for `opus` / `sonnet` /
`haiku` model names flips from CLI to SDK. This is a separate
decision, not gated by this ADR. The migration is a one-line change
in the registry; the data point that justifies it is operational
observation.

### Auth model

The Agent SDK does not read the macOS keychain or the
`~/.claude/.credentials.json` file. Auth is supplied programmatically
via `ANTHROPIC_API_KEY` env var (or via a custom auth provider
configured at SDK construction time). Lotsa already plumbs this:
the `AgentRunner` constructors accept a `credentials: dict[str, str]`
kwarg, and `agent_runner.py` merges those into the subprocess
`env` alongside `os.environ` (`agent_runner.py:127`). Operators
set `ANTHROPIC_API_KEY` in their shell or in `lotsa.yaml` and the
runner forwards it. `rigg/git.CredentialStrategy` is a
separate mechanism that handles git repository auth (GIT_ASKPASS
pattern) — not API keys — and is unrelated.

This is a slightly tighter auth model than Claude Code's keychain
flow — but Lotsa is a headless server; explicit API keys are the
right fit. Docker mode already requires this.

### Additional dependency

`claude-agent-sdk` (Python) becomes a new top-level dependency of
`rigg/`. Per the self-hostable dependency rule in the root
CLAUDE.md: the SDK is a thin Python wrapper around the public
Anthropic API; no servers to host, no outbound calls beyond the
configured Anthropic endpoint (which can point at an
enterprise-hosted proxy if needed). Acceptable.

Bundle is install-time only; operators who don't use the SDK runner
don't pay any runtime cost.

## Why not just switch entirely to SDK?

Three reasons to keep `ClaudeCodeRunner` alongside:

1. **Stability hedge.** The SDK is a separate code path; bugs there
   should not block existing operators who are happy with the CLI
   shape.
2. **Cost.** The SDK runner needs Lotsa-side tool implementations
   for the interception points. That's real software. Letting CLI
   stay default while SDK proves its value avoids forcing the
   migration on operators who aren't seeing the bugs we've been
   fighting.
3. **Provider diversity.** The CLI shape extends naturally to other
   provider CLIs (Aider, OpenCode, Codex). If we drop CLI entirely,
   we'd have to re-implement those providers' integrations as
   API-shaped runners — substantial work for unclear benefit.

The right framing is: each shape solves a different problem class.
The registry is the durable mechanism; specific runners come and go.

## Relationship to ADR-026

ADR-026 (orchestrator-managed background task support — RISKY /
UNDECIDED, deferred) catalogued six approaches to making background
tasks survive Lotsa's headless dispatch model: foreground-only
(Approach A, the current state), in-turn polling (B), tail-and-
resume (C, killed because subprocesses are reaped at parent exit),
hold-the-parent-alive (D), Bash-tool interception (E), and Agent
SDK migration (F).

This ADR is essentially **Approach F**, with one important
clarification: it's framed not as a *migration* but as a *peer
runner*. Adding `ClaudeAgentSDKRunner` to the registry doesn't
require dropping `ClaudeCodeRunner`. Operators who want
background tasks reachable per ADR-026's design space pick the SDK
runner; operators who don't can stay on the CLI runner.

If this ADR is accepted and the SDK runner ships, ADR-026's
Approaches A–E become less interesting — A/B as fallbacks for
operators on the CLI runner; C is still mechanically blocked; D
and E are unnecessary work. ADR-026 stays as a useful design-
space catalogue and as a record of why "tail-and-resume" doesn't
work, but its active proposals are largely subsumed. Whether to
mark ADR-026 Superseded at that point is a separate decision; the
catalogue value persists.

## Consequences

### Positive

- The dispatch shape mismatch documented in the *"Don't build your
  own AI harness"* brief stops biting under the SDK runner.
  Cross-turn patterns work as the agent expects.
- Operators get a real choice: stay on CLI (simpler, works today),
  move to SDK (better for Lotsa's headless shape).
- Future Anthropic features (Claude Code skills, agents, mcp
  configuration) are accessible to Lotsa via the SDK's programmatic
  surface, without us reverse-engineering how the CLI flags them.
- The taxonomy in ADR-023 grows cleanly. Other providers' SDK
  runners follow the same pattern when those SDKs mature.

### Negative

- Two code paths to maintain. Each runner needs its own tests,
  error classification, output adaptation. The `AgentRunner`
  Protocol absorbs most of the duplication, but per-runner bugs
  are now per-runner.
- The runner-aware OPERATIONAL_PREAMBLE adds a layer of indirection
  to a piece of code we touch frequently. Test coverage on which
  fragment gets injected matters.
- Operators have to *choose*. We can default sensibly (CLI stays
  default until the SDK runner is mature) but the choice is now
  surfaced. Some operators won't want to think about it.

### Migration

- Phase 1 (add the runner) is non-breaking. Operators see no
  difference until they opt in.
- Phase 2 (runner-aware preamble) is a refactor with smoke-test
  coverage for both fragments. Default fragment matches today's
  preamble. Existing concrete runners (`ClaudeCodeRunner`,
  `DockerAgentRunner`) each gain a one-line
  `dispatch_shape_prompt()` returning the CLI fragment, in the
  same PR that introduces the Protocol method — there is no
  migration window where structural implementers silently lack
  the method (see *Protocol membership* above for why structural
  Protocols don't auto-inherit defaults).
- Phase 3 (per-step opt-in) is additive to `process.yaml`. Old
  flows continue to work.
- Phase 4 (default flip) is operator-observable and deferred until
  there's real-world data.

## Out of scope

- **Adding other provider SDKs.** OpenAI's agent SDK, Google's
  agent SDK if/when, etc. Each gets its own ADR if the maturity is
  there. The shape this ADR establishes is reusable.
- **Migrating from CLI runner to SDK runner**. Phase 4 (default
  flip) is deferred to its own decision; this ADR doesn't commit
  to it.
- **Deprecating `ClaudeCodeRunner`**. The CLI runner stays
  supported indefinitely. CLI is the right shape for some operators
  (simpler, no API key required if they're using keychain auth, no
  in-process Python SDK).
- **The full Lotsa-side tool-implementation surface for SDK
  interception.** That's substantial design work — what
  `AskUserQuestion` resolves to, how background-Bash gets a Lotsa-
  owned lifecycle, what the subagent dispatch looks like. Captured
  here in *Phase 1's docstring* but expanded as part of the
  implementation work, possibly in follow-up ADRs per interception
  surface.
- **Streaming output to the dashboard.** The SDK supports streaming;
  Lotsa's dashboard could surface partial agent output as it
  arrives. Useful but orthogonal to the runner shape decision.
