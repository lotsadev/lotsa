# ADR-023: Multi-provider agent runners via a registry

**Status**: Implemented (2026-06-12)
**Date**: 2026-06-06
**Related**: ADR-014 (the tool/engine registry pattern this ADR mirrors),
ADR-022 (per-step `model:` field — this ADR makes those names route to
different providers), `rigg/agent_runner.py` (the `AgentRunner`
Protocol this ADR extends).

---

## Implementation note (2026-06-12)

Shipped the first-PR scope: the registry primitive (`register_runner` /
`resolve_runner` / `ResolvedRunner` / `RunnerNotFound` / `clear_registry` /
`snapshot` / `restore`) in `rigg/agent_runner.py`, the `runners:` block on
`LotsaConfig`, a `load_user_runners` loader in `lotsa/registry.py`, per-dispatch
resolution in `OrchestratorService`, and the `agent_runner` audit field at both
write sites.

Two divergences from the scope sketch below, both deliberate and documented in
the code: the registry stores **instances** (not the decorator-over-classes the
Scope section sketches), because runners carry config-derived construction
state; and collisions are handled **by registered name** — re-registering the
same name (the `default` slot at `start()`) is a silent refresh, while a
*cross-name* prefix collision warns and last-registration-wins.

**Per-step routing is live (ADR-022 landed).** ADR-022 shipped the per-step
`model:` field on `ResolvedJob`, so resolution now keys off `step.model or
config.model`: a job with `model: gpt-5` routes through whichever runner owns
the matching prefix, and jobs without a `model:` fall back to the global
`config.model` (and the `default` runner). Per-step provider diversity (the
`process.yaml` example below) is therefore active, not dormant.

---

## Context

ADR-022 introduces a per-step `model:` field on agent jobs in
`process.yaml`. Today every model name resolves to a single runner —
`ClaudeCodeRunner`, which subprocess-execs the `claude` CLI binary.
Names like `sonnet`, `opus`, and `haiku` map to `--model <name>` on
that CLI; everything else fails at dispatch.

The natural next step is letting different model names route to
different runners — `gpt-5` to an OpenAI-shaped runner, `gemini-2.5`
to a Google-shaped one, `ollama:llama3` to a local one — without the
orchestrator caring about which provider lives behind the name. That's
this ADR.

Two architectural shapes are available, both anchored on the existing
`AgentRunner` Protocol. **This ADR treats them as equally first-class
options for what registers behind a runner name** — the registry shape
doesn't prescribe which path operators must adopt. Each implementing
runner picks the shape that fits its provider best:

**CLI-shaped runners.** Each provider gets an `AgentRunner` impl that
subprocess-execs that provider's agent CLI. Mirrors today's shape:
`ClaudeCodeRunner` execs `claude`; an `OpenCodeRunner` would exec
`opencode`; a `CodexCliRunner` would exec OpenAI's `codex`; an
`AiderRunner` would exec `aider`. Each provider CLI owns the agent
loop (tool use, code editing, session resume); the runner just
orchestrates the subprocess call and adapts the CLI's flags and
output to the `AgentRunner` Protocol.

**API-shaped runners.** One runner uses a multi-provider Python
client (LiteLLM, AISuite) to make raw chat-completions calls against
the requested provider and implements the agent loop in our own
process — tool registration, tool execution, file editing, session
memory, the marker protocol. Same `AgentRunner` Protocol; different
internals.

The two shapes solve different problems and have different costs:

- CLI runners ship faster (the CLI does the agent-loop work) but
  inherit the CLI's quirks — flag conventions, session model, output
  format. Adaptation per CLI is mechanical but non-trivial.
- API runners give us more control (per-call routing, custom tool
  schemas, in-process error handling) but require building an
  agent harness — that's real software, not glue.

`ClaudeCodeRunner` is a CLI runner today. The registry stays
shape-agnostic: a CLI runner and an API runner can coexist in the same
`lotsa.yaml`, registered for different model prefixes, dispatching the
same way through the orchestrator.

### Survey of available agent CLIs

Surveyed for the CLI-shaped runner path. Each could become its own
`AgentRunner` impl registered against the relevant model prefixes:

- **Claude Code** (Anthropic) — current shape; `claude` CLI with
  tool use, session resume (`--resume <id>`), system/user prompt
  flags, structured JSON output, cost reporting. The reference
  contract the `AgentRunner` Protocol is shaped against.
- **opencode** — open-source agentic coding CLI; multi-provider
  routing built in. Has a non-interactive mode amenable to subprocess
  driving. Adaptation: flag conventions and output format differ from
  Claude Code's.
- **OpenAI Codex CLI** (`codex`) — official OpenAI agentic CLI;
  status fluctuated through mid-2026 but worth registering when
  stable. Native GPT-family routing.
- **Aider** — open-source, supports 50+ providers via a unified CLI.
  Most battle-tested option for a coding-agent CLI. Atomic git
  commits per change, can be driven non-interactively. Session model
  and command parsing don't match Claude Code's contract exactly —
  the implementing runner does the adaptation.
- **Cursor** — IDE-native; no headless CLI equivalent to Claude
  Code's `--print` mode at the time of writing. Not currently a
  viable target.
- **Continue CLI (`cn`)** — newer, has a headless mode, less mature
  than Aider for non-interactive subprocess use.

### Survey of multi-provider Python clients

For API-shaped runners — when in-process agent-loop control is worth
the build cost, or when no decent agent CLI exists for the desired
provider:

- **LiteLLM** (MIT, ~45k★, v1.88.x stable): 100+ providers (Anthropic,
  OpenAI, Gemini, Bedrock, Azure, Ollama, vLLM, …), tool-use schema
  translation across providers, streaming, cost accounting, retries.
  Proxy gateway mode. Provider-specific features (caching, computer
  use) don't all pass through cleanly. Self-hostable (no mandatory
  outbound calls except to LLM endpoints — satisfies the
  self-hostable dependency rule in the root CLAUDE.md).
- **AISuite** (Apache 2.0, ~12k★, Andrew Ng's project): unified
  interface across major providers, first-class function-calling /
  tool-use abstraction added in early 2026, cleaner than LiteLLM for
  agentic workflows. Less battle-tested.
- **LangChain provider layer**: usable in principle but pulls in the
  full LangChain framework — too much surface area for "I just want
  many providers".
- **OpenAI SDK + base_url**: works for OpenAI-compatible endpoints
  (Ollama, vLLM, LM Studio, OpenRouter, Portkey) but excludes
  Anthropic's native format, Gemini, etc. Not enough coverage for
  a single API-runner default; useful for narrow OpenAI-compatible
  cases.

When the first API runner lands, the library choice is LiteLLM by
default with AISuite as a worth-prototyping alternative.

---

## Decision

**Add an `AgentRunner` registry that maps a model name (or model-name
prefix) to a runner instance. The orchestrator looks the model name
up at dispatch time and calls the registered runner. Today's behaviour
(one `ClaudeCodeRunner` for every dispatch) becomes the default
registration; multi-provider is opt-in by registering more runners in
`lotsa.yaml`.**

The registry is a new primitive in `rigg/agent_runner.py`
(`register_runner` to add a runner, `resolve_runner` to look one up at
dispatch time). Registration follows the same shape as `register_tool`
/ `register_engine` from ADR-014 — `name → "dotted.module:callable"`
in `lotsa.yaml`, imported at startup.

### Resolution rules

1. **Exact name match wins.** A runner registered for `gpt-5` answers
   `model: gpt-5`. Used for explicit per-model routing.
2. **Prefix match (longest wins).** A runner registered for `claude-`
   answers `claude-opus-4.7`, `claude-sonnet-4`, etc. Lets one impl
   handle a whole family without enumerating every name.
3. **Default registration.** If no exact or prefix match, fall back to
   the default-registered runner. `ClaudeCodeRunner` registers as
   default at startup so existing lotsa.yaml files keep working
   unchanged.
4. **Unknown name with no default → loud error.** `RunnerNotFound` at
   dispatch time, naming both the model and the registered prefixes.

**Identical-prefix collisions.** When two runners declare the same
prefix (not a sub-prefix relationship — literally the same string),
the later registration wins for that prefix; `register_runner` emits
a startup WARNING naming both runners and the shared prefix so the
collision isn't silent. The operator disambiguates by editing
`lotsa.yaml` (give each runner a non-overlapping prefix, or remove
one). Resolution order itself (rules 1–4 above) is unchanged.

### What stays unchanged

- `AgentRunner` Protocol signature stays as-is. Existing impls
  (`ClaudeCodeRunner`, CE's `DockerAgentRunner`) work without change.
- Existing per-call kwargs from ADR-022 (`model=...`) still flow
  through; the registry just decides *which* runner gets the call.
- `output_meta["agent_model"]` still records the resolved model
  string; a new sibling field `output_meta["agent_runner"]` records
  which runner handled it (so an audit reader can tell `gpt-5` via
  LiteLLM apart from `gpt-5` via OpenAI Codex CLI if someone
  configures both).

### lotsa.yaml shape

```yaml
project_dir: /path/to/repo
model: sonnet                     # default (legacy global)
process: software_dev

# Optional: register additional runners. Built-in (ClaudeCodeRunner)
# is always available as the default and handles claude-* by default.
runners:
  gpt:
    handler: lotsa_runners.codex:CodexCliRunner
    prefixes: [gpt-, openai/]     # routes gpt-5, gpt-4.1, openai/o1, etc.
  aider:
    handler: lotsa_runners.aider:AiderRunner
    prefixes: [aider/]            # explicit opt-in via aider/<model>
  local:
    handler: lotsa_runners.litellm_runner:LiteLLMRunner
    prefixes: [ollama:, llama-, deepseek-]
```

Bare name `model: foo` means "look it up", same as ADR-014's
`tools:` / `engines:` blocks. No magic — registration is explicit.

### process.yaml example after ADR-022 + this ADR

```yaml
process: software_dev
jobs:
  - { name: planning, type: agent, prompt: planning, model: opus }
  - { name: coding,   type: agent, prompt: coding,   model: claude-sonnet-4.7 }
  - { name: review,   type: agent, prompt: review,   model: gpt-5 }
  - { name: verify,   type: agent, prompt: verify,   model: gemini-2.5-pro }
flows:
  main:
    steps: [planning, coding, review, verify]
```

Each step routes to a runner via the registry. The implementer
declares model diversity per step; the orchestrator dispatches without
caring whose CLI / API answers.

---

## Tradeoffs

**Pros:**

- Follows an established pattern (ADR-014 tool/engine registry) —
  contributors learn one mechanism, not three.
- Backwards-compatible. Existing lotsa.yaml files keep using
  `ClaudeCodeRunner` because it self-registers as default.
- Lets us ship multi-provider in stages — register one new runner at
  a time, validate it on a non-critical step, expand.
- The registry stays shape-agnostic: CLI runners and API runners
  coexist behind the same primitive. Adding a `LiteLLMRunner` (API
  shape) or an `OpenCodeRunner` (CLI shape) is one new registration
  in each case — neither requires revisiting the registry contract.
- Survives the CE → OSS split because the registry lives in
  `rigg/` and is a shared primitive the project depends on.

**Cons:**

- One more registration concept for operators to learn (tools,
  engines, now runners). Mitigated by reusing the same YAML shape.
- CLI-shaped runners inherit whatever oddities the provider's CLI has
  (Aider's commit semantics, Codex's argument parsing, opencode's
  output format, etc.). API-shaped runners take on the agent harness
  itself (tool registration, execution, sandboxing, session state) —
  real software, not glue. Either way, count on each new runner being
  non-trivial to write the first time.
- Two runners that nominally support the same model name (e.g. an
  Aider runner that can route to Anthropic, AND `ClaudeCodeRunner`)
  must declare different prefixes; if they don't, last-registered
  wins. The collision isn't silent — `register_runner` emits a
  startup WARNING (see the "Identical-prefix collisions" note in the
  Decision section) — but the operator still has to disambiguate in
  `lotsa.yaml`. Mitigation lives in the runtime; the duty to
  disambiguate is on whoever wrote the colliding registration.
- Routing by string prefix is a stringly-typed decision. A typo in
  `lotsa.yaml` (`gpt5-` vs `gpt-`) silently lands the dispatch on
  the default runner instead of the intended one. The startup INFO
  log enumerating "registered prefixes in priority order" (committed
  in Scope step 6 and Acceptance criteria) is the mitigation —
  misroutes are visible at startup without scrolling through traces.
  The con is residual: no compile-time check, just a log line the
  operator has to read.

---

## Scope

This ADR proposes the architectural rule. Implementation lands as a
focused PR after approval. The first PR ships just the registry plus
the `ClaudeCodeRunner` default registration:

1. Add to `rigg/agent_runner.py`:
   - `register_runner(name: str, *, prefixes: list[str] | None = None,
     default: bool = False) -> Callable` (decorator-style, matching
     `register_tool` from ADR-014).
   - `resolve_runner(model: str) -> AgentRunner` — exact → prefix →
     default → raise `RunnerNotFound`.
   - `clear_registry()`, `snapshot()`, `restore()` for tests (same
     shape as `lotsa.registry`).
2. Add `runners: dict[str, dict[str, Any]]` to `LotsaConfig`.
   YAML-null normalisation already covers it (the universal `if value
   is None: continue` from PR #82). Load at startup, same as
   `tools:` / `engines:`.
3. `ClaudeCodeRunner` self-registers as default at module-import
   time via a top-level `register_runner(...)` call (decorator-style,
   matching ADR-014's `register_tool` / `register_engine` pattern in
   `lotsa/tools/__init__.py` and `lotsa/engines/__init__.py`).
   Prefixes: `["claude-", "sonnet", "opus", "haiku"]`. No call from
   `OrchestratorService.start()` is needed — importing
   `rigg.agent_runner` is sufficient. Existing lotsa.yaml files
   route every model through this default — no behaviour change.
4. `OrchestratorService` swaps `self.runner` (one instance) for a
   resolver call at each dispatch: `runner =
   resolve_runner(step.model or self.config.model); await
   runner.run(...)`.
5. Audit trail: `output_meta["agent_runner"]` records the registered
   name; `chat_meta` mirror.
6. Startup logs: enumerate registered prefixes in priority order at
   INFO, warn on prefix collisions.
7. Tests:
   - exact-match wins over prefix
   - prefix-match (longest wins)
   - default fallback when no registered prefix applies
   - `RunnerNotFound` when no default *and* no match
   - prefix-collision warning fires
   - audit metadata records the resolved runner name

Subsequent PRs (one per new runner) add the actual third-party
runners — CLI-shaped or API-shaped, per provider — e.g.
`OpenCodeRunner`, `CodexCliRunner`, `AiderRunner`, or a
`LiteLLMRunner` for in-process API dispatch. Each is independent and
each picks its own shape; the registry doesn't care.

In scope for Lotsa (the registry lives in the shared `rigg/` SDK).

---

## Out of scope

- **Building any of the additional runners.** This ADR specifies the
  registry only. The first follow-up PR registers exactly one runner
  (`ClaudeCodeRunner`) the same way today's code constructs it.
  Additional runners — CLI-shaped (`OpenCodeRunner`,
  `CodexCliRunner`, `AiderRunner`) or API-shaped (`LiteLLMRunner`,
  `AISuiteRunner`) — get their own PRs / ADRs as needed. Each has
  provider-specific decisions to surface (credential handling,
  tool-call schema mapping, output parsing, agent-loop architecture
  for API-shaped runners).
- **Switching the default away from Claude Code.** Claude Code stays
  the default-registered runner. Operators opt into other runners
  explicitly via `lotsa.yaml` registration.
- **Cross-provider session resume.** Today `ClaudeCodeRunner` resumes
  a Claude Code session via `--resume <id>`. Sessions don't
  generalise across providers; the implementing runner is responsible
  for what "resume" means in its world. If `step.resume = true` and
  the resolved runner doesn't support resume, the runner raises a
  clear error rather than silently restarting fresh.
- **Per-step provider credentials.** Credential management is a
  separate concern; this ADR assumes credentials reach each runner
  via environment variables or a runner-specific config block. If
  cross-tenant credential isolation becomes a requirement, that's a separate ADR.

---

## Forward path: adding runners (CLI-shaped or API-shaped)

The registry doesn't change as more runners come online. New runner
follow-ups land independently — each is a single registration plus
that runner's own implementation work.

### Adding a CLI-shaped runner (e.g. opencode, codex, aider)

Each follows the `ClaudeCodeRunner` pattern:

1. Implement an `AgentRunner` impl in a new module
   (e.g. `lotsa/runners/opencode_runner.py`) that subprocess-execs
   the provider's CLI.
2. Map the `AgentRunner` Protocol surface (system / user prompt,
   allowed tools, session id, timeout) onto the CLI's flags and
   parse the CLI's stdout into `AgentResult`.
3. Register it for the desired model prefixes (e.g. `OpenCodeRunner`
   for opencode-supported prefixes, `CodexCliRunner` for
   `gpt-`/`openai/`, …).
4. Tests cover: argument construction, output parsing, session
   round-trip (or a clean unsupported-resume error), prefix routing.

Where each provider's CLI maps cleanly to the Protocol, this is the
cheapest path — the CLI does the agent-loop work and the runner is
adaptation glue.

### Adding an API-shaped runner (e.g. LiteLLM, AISuite)

When in-process control is worth the build cost — fine-grained
per-call routing, custom tool schemas, or a provider with no decent
agent CLI:

1. Pick the library (default: LiteLLM, MIT, mature, most providers
   covered; AISuite is the cleaner-API alternative). Audit licence
   and self-hostability one more time before adding the dependency.
2. Implement `LiteLLMRunner` — an `AgentRunner` impl that, per
   `run(...)` call, drives an in-process agent loop: tool
   registration, tool-call schema translation (the library handles
   the wire format; we plug into our existing tool registry from
   ADR-014), file editing via our existing tool surface, the marker
   protocol from `lotsa/orchestrator.py`.
3. Register it for the desired prefixes (`gpt-`, `gemini-`,
   `mistral-`, etc.).
4. Tests cover: tool-use schema round-trip, marker emission, session
   persistence in our DB (since the provider can't resume), and a
   smoke test against a local Ollama endpoint.

This work is non-trivial — building an agent loop is real software,
not glue. The point of this ADR is that **the registry doesn't change
when we do that work**. The new runner is a single registration that
follows the contract `ClaudeCodeRunner` already establishes.

### Mixing shapes

CLI and API runners coexist in the same `lotsa.yaml`. A typical
multi-provider setup might register `OpenCodeRunner` for one prefix,
`LiteLLMRunner` for another, with `ClaudeCodeRunner` still answering
the default. The orchestrator dispatches without knowing or caring
which shape answers any given request.

---

## Migration

Pure addition. Existing lotsa.yaml files have no `runners:` block;
the default `ClaudeCodeRunner` covers every dispatch. ADR-022's
per-step `model:` field continues to route through the default unless
a registered prefix matches. No behaviour change for operators who
keep using Claude Code only.

The implementing PR's audit-trail addition (`output_meta["agent_runner"]`)
is a new optional field on existing rows — readers that didn't ask
for it see no change.

---

## Acceptance criteria

- `AgentRunner` registry lives in `rigg/agent_runner.py` and
  mirrors the `lotsa.registry` shape (load via `lotsa.yaml`'s
  `runners:` block, decorator-style registration in Python, snapshot
  / restore for tests).
- `ClaudeCodeRunner` self-registers as default with prefixes
  covering today's accepted model names; existing `lotsa.yaml` files
  see no behaviour change.
- `OrchestratorService.start()` no longer constructs a single
  `self.runner`; every dispatch resolves the runner via
  `resolve_runner(step.model or config.model)`.
- Audit trail records both `agent_model` (existing) and
  `agent_runner` (new) on every relevant write site.
- Startup enumerates registered prefixes at INFO and warns on
  prefix collisions.
- Tests cover the resolution rules end to end.
- The bundled `full` process ships unchanged — model diversity
  becomes a sample in the README, not a default in the bundled
  preset (operators with single-provider setups shouldn't be forced
  to register more runners just to use the bundled flow).
- `CLAUDE.md` ADR index lists 023.
