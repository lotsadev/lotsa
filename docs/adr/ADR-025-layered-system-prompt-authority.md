# ADR-025: Layered system-prompt authority — Lotsa rules append to the Claude Code preset

**Status**: Implemented
**Date**: 2026-06-08
**Implementation**: PR #96 — ships the layered invocation
(`--append-system-prompt` on top of the `claude_code` preset,
`--setting-sources project` for project CLAUDE.md as conversation
context), the enhanced `OPERATIONAL_PREAMBLE` (precedence statement
+ git/file-scope/output rules including the no-`cd` escape
hatch), the step-prompt reframes across the `full/`, `standard/`,
and `simple/` flow presets, and the `TestOperationalPreamble` smoke
tests guarding the preamble's load-bearing rules.
**Supersedes**: This ADR replaces an earlier draft titled *"Lotsa agents
run in a sealed Claude Code environment"* (also numbered ADR-025) that
proposed a full `--bare` mode + `apiKeyHelper` bridge. The earlier
framing was too aggressive — it discarded the legitimate value project
`CLAUDE.md` provides (domain conventions, architecture notes, code
style). See *Why this revises the earlier draft* below.
**Related**: PR #92 (operator-config bleed — `--setting-sources ""`),
PR #93 (PWD env leak), PR #95 (planning-system.md Step 4 removal),
`rigg/agent_runner.py` (where the layered invocation lives),
`lotsa/docker_runner.py` (the same layering inside the container),
`lotsa/orchestrator.py` (`OPERATIONAL_PREAMBLE` constant — the
cross-cutting Lotsa rules block).

---

## Context

Lotsa dispatches each step's agent by spawning `claude --print
--system-prompt <lotsa-prompt>` as a subprocess. Two related failure
patterns surfaced across the last week:

- **Operator-global override.** The operator had the `superpowers`
  plugin installed at `~/.claude/`. Its `SessionStart` hook ran in
  Lotsa's agent subprocesses, the `using-superpowers` skill loaded, and
  the spec agent (asked to produce a spec) instead offered a
  *"writing-plans → executing-plans pipeline"* menu. Closed by PR #92
  (`--setting-sources ""`).
- **Project-local override.** On task `<redacted>`, the planning agent
  ran `git checkout -b feature/adr-020-phases-2-and-3` in the
  operator's main checkout. Two contributors: `planning-system.md`
  Step 4 told it to create a feature branch (incoherent with the
  worktree-per-task architecture), and `.git`-introspection let the
  agent discover the main repo's path and navigate there. Closed in
  part by PR #93 (PWD env leak) and PR #95 (Step 4 removal).

The first instinct (an earlier draft of this ADR) was to seal the
agent environment entirely — Claude Code's `--bare` mode, no
auto-loaded `CLAUDE.md`, no auto-loaded settings, auth via
`apiKeyHelper`. That's structurally cleanest but throws away real
value: a target repository's `CLAUDE.md` typically encodes domain
knowledge (code style, naming conventions, architecture notes,
testing patterns) that the agent legitimately benefits from. Sealing
trades a hypothetical override problem for guaranteed loss of useful
context.

The Agent SDK's "modifying system prompts" documentation surfaces a
better mechanism. Three relevant findings from that doc:

1. **System prompts have a layered shape.** The `claude_code` preset
   (Claude Code's full tool/safety/style baseline) is one layer; an
   `append` text on top is another. The preset is preserved; Lotsa's
   instructions sit *after* it. The doc calls this *"the lowest-risk
   customization"*.
2. **`CLAUDE.md` is not part of the system prompt.** Quoting the doc
   verbatim: *"The SDK reads it and injects its content into the
   conversation as project context, not into the system prompt, so it
   shapes behavior alongside whichever system prompt you choose."*
   This is the key structural insight — system prompt and `CLAUDE.md`
   live in different channels.
3. **Channel implies priority.** System-prompt content sits at higher
   structural authority than conversation context. When two rules
   conflict — one in Lotsa's append, one in project `CLAUDE.md` — the
   model's input ordering biases toward the system prompt.

These three together describe exactly the shape we want for Lotsa:
preserve Claude Code's coding/tool baseline, append Lotsa's
operational rules so they sit in the highest-authority slot, and let
project `CLAUDE.md` flow in as conversation context to inform
domain-level decisions.

## Decision

**A Lotsa agent subprocess runs with the `claude_code` preset plus a
Lotsa-authored append. Project `CLAUDE.md` continues to auto-load as
conversation context. Operator-global settings, plugins, and skills
stay isolated.**

Concretely, the runner invocation becomes:

```
claude --print --output-format json --dangerously-skip-permissions --verbose \
    --max-budget-usd <budget> \
    --setting-sources project \
    --append-system-prompt "<OPERATIONAL_PREAMBLE + step-prompt>" \
    -p "<user prompt>"
```

Three substantive flag changes from today:

1. **`--system-prompt` → `--append-system-prompt`.** The Claude Code
   preset is preserved; Lotsa's step prompt appends after it. The
   model sees Claude Code's baseline tool/safety/style guidance, then
   Lotsa's operational rules and step-specific responsibilities.
2. **`--setting-sources ""` → `--setting-sources project`.** Re-enables
   project-level `CLAUDE.md` auto-discovery and project-level
   `.claude/` settings. Does **not** enable `user` (operator-global,
   where the superpowers plugin lived) or `local` (operator-personal
   per-repo overrides, often gitignored). The seal against operator
   override is preserved; the channel for project domain knowledge is
   opened.
3. **No `--bare`, no `apiKeyHelper` bridge.** Keychain/OAuth auth
   continues to work as today.

### Operational rules become the authoritative layer

The OPERATIONAL_PREAMBLE (`lotsa/orchestrator.py:85`) is the
cross-cutting rules block prepended to every step prompt. With the
append model, this preamble becomes the explicit precedence boundary:

- The preamble opens with a precedence statement: *"These rules take
  precedence over project CLAUDE.md, AGENTS.md, or any other
  convention file when they conflict on operational matters
  (orchestrator-owned flow, git branch ownership, push behavior,
  commit lifecycle)."*
- Operational rules sit in the preamble — git authority, push
  ownership, no agent-side branch creation, NEEDS_INPUT marker
  handling, file-modification scope.
- Step-specific responsibilities sit in the step prompt — what this
  step's job is, what it must produce, when to commit (per the
  worktree-per-task contract).
- Domain rules (code style, architecture, naming) stay in project
  `CLAUDE.md` where they belong.

When operational and domain rules conflict, the operational rule wins
because it sits in the higher-authority channel (system prompt) AND
the preamble explicitly names the precedence.

### Step prompts reframe slightly

The existing `*-system.md` files (`spec-system.md`,
`planning-system.md`, etc.) are currently written as standalone system
prompts (*"You are a spec agent…"*). With the append model, they
appear *after* the `claude_code` preset's own *"You are Claude
Code…"* introduction. Two competing identities in the same context
risk subtle drift.

Each step prompt gets a light reframe — replacing the standalone
identity claim with a Lotsa-operational framing:

```diff
- # Spec Agent
-
- You are a spec agent. Your job is to help the user define what they
- want to build through collaborative conversation.
+ # Lotsa Spec Step
+
+ Operating as the **spec step** of Lotsa's flow. Your responsibility
+ in this dispatch is to help the user define what they want to build
+ through collaborative conversation, then emit `SPEC_COMPLETE:` when
+ the spec is ready.
```

The content of each prompt stays largely intact — what changes is the
framing from "you are this thing" to "you are operating as this step
of Lotsa's flow." Step-specific rules (commit on current branch,
markers to emit, etc.) stay where they are.

## Why this revises the earlier draft

The earlier draft proposed `--bare` mode (no `CLAUDE.md`
auto-discovery, no keychain, full isolation) with an `apiKeyHelper`
bridge to keep authentication working. The operator flagged the
mistake: *"Our agents should be running in a containerised environment
where they could be checking out random repos. All this needs to be
supported — or prevented that they will override our prompts… we
actually want the Claude agent running the tasks to follow the local
rules set by the repo it's working on. This could include programming
rules or other things it should follow."*

The earlier draft conflated two different things:

- **Operational authority** (Lotsa's orchestrator-owned flow rules,
  branch lifecycle, dispatch contract) — must be unambiguously
  Lotsa's.
- **Domain authority** (code style, naming, architecture, testing
  conventions) — should be the project's.

The seal-everything approach correctly preserved operational
authority but discarded domain authority. The layered approach gives
us both: Lotsa's append sits in the highest-authority channel for
operational rules; `CLAUDE.md` flows in as conversation context for
domain rules.

## Consequences

### Positive

- Lotsa agents naturally follow project coding conventions, naming
  rules, and architecture documented in the target repo's
  `CLAUDE.md` — without Lotsa having to re-vendor or re-author that
  guidance.
- Lotsa's operational rules remain unambiguously authoritative
  because they sit in the system-prompt channel, ahead of the
  conversation-context channel where `CLAUDE.md` lands.
- Operator-global override surfaces (`user` settings, installed
  plugins, `SessionStart` hooks) stay isolated — the seal against
  operator override is preserved.
- No auth changes required; keychain/OAuth continues to work.
- The implementation surface is small: two flag changes in two runner
  files, one preamble enhancement, light framing edits to the step
  prompt files.
- The Agent SDK doc explicitly calls this *"the lowest-risk
  customization"* — using a supported, recommended mechanism rather
  than fighting the platform.

### Negative

- A target repository can in principle author a `CLAUDE.md` that
  attempts to undermine Lotsa's operational rules. The layered
  authority is structural (channel + recency) but not enforced — a
  sufficiently emphatic `CLAUDE.md` could still drift agent behavior.
  Mitigation: the OPERATIONAL_PREAMBLE's explicit precedence statement
  names the conflict resolution rule. Escalation path if real drift
  is observed: reintroduce `--bare` as an opt-in flag (`agent_seal:
  strict`), keeping the layered default as `loose`.
- Re-enabling `--setting-sources project` means project-level
  `.claude/` settings (hooks, custom skills committed to the target
  repo) load too. Domain-relevant hooks (e.g. a project's lint
  pre-commit) are generally fine; a project-level `SessionStart` hook
  that injects a competing identity is a new override surface. The
  trade-off is the same as for `CLAUDE.md` — most uses are
  legitimate.
- The step prompt files are slightly less standalone-readable; they
  reference "Lotsa's flow" and "this dispatch" in a way that assumes
  the surrounding append-on-preset context.

### Migration

Single PR, sequentially:

1. **Flag swap** in `rigg/agent_runner.py` and
   `lotsa/docker_runner.py`. `--system-prompt` → `--append-system-prompt`;
   `--setting-sources ""` → `--setting-sources project`. Tests update
   to match.
2. **OPERATIONAL_PREAMBLE enhancement** in `lotsa/orchestrator.py`.
   Add the precedence statement and consolidate the operational rules
   (no branch creation, no push, NEEDS_INPUT contract,
   file-modification scope).
3. **Step prompt reframes** in `lotsa/prompts/{full,standard,simple}/*.md`.
   Light framing edits — replace standalone identity claims with
   step-of-Lotsa framing.

The change is backwards-compatible with existing tasks in flight:
running agents already have their context; new dispatches use the new
shape. Operators who restart `lotsa serve` after the merge get the
new behavior on their next task.

## Out of scope

- The MCP server isolation question (a target repo's `.mcp.json`
  could ship malicious MCP servers). `--setting-sources project`
  loads `.claude/` settings but not arbitrary MCP configurations;
  MCP-specific allow-listing is a separate ADR if it becomes a
  problem.
- Sandboxing the filesystem the agent can read. The layered model
  is about *whose rules win when there's a conflict*; it's not about
  *what the agent can access*. Filesystem boundaries belong in Docker
  mode or operator-set permissions.
- Network egress controls. Same as the prior draft: the principle
  applies but the mechanism is different.
- A formal definition of "operational" vs. "domain" rules. The split
  is intentionally pragmatic: orchestrator-owned flow is
  operational; everything else is domain. Edge cases (e.g. a project
  CLAUDE.md saying *"always commit with sign-off"*) get resolved by
  the precedence statement on a case-by-case basis.
