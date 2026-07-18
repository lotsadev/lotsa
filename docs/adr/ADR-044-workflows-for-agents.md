# ADR-044 — Workflows for agents

**Status:** Implemented (Phase 1) — Phases 2–6 Proposed

**Scope:** CE (with a shared-catalog concept that later reaches `rigg`)

## Context

Lotsa's process model (ADR-014, ADR-043) expresses each process as a state
machine of typed jobs whose prompts live per-process under
`prompts/{process}/`. Routing between steps is driven by **bespoke stdout
markers** — `REVIEW_PASS`/`REVIEW_FAIL`, the four `PR_FIX_*`, `VERIFIED:` /
`NEEDS_CODE:` / `NEEDS_REVIEW:`, `CONFLICTS_RESOLVED:`, `NEEDS_INPUT:`,
`SPEC_COMPLETE:`. The marker *name* carries the meaning, so an agent "knows"
which flow it belongs to, and the prompts drift between copies (`fix` reuses
`build`'s prompts only through a search-path fallback).

The operator's vision: reframe the product as **agents wired into workflows**.
An agent is a reusable, process-independent unit (prompt + declared properties
+ the fixed set of outcomes it may emit). A workflow is one or more agents
wired together by those outcomes. The tagline collapses to one line: *Lotsa
runs workflows; a workflow is agents wired together.*

## Decision

Re-architect around two primitives — **agents** and **workflows** — joined by a
**generic outcome vocabulary**. Semantics move from the marker name to the
flow *edge*: the orchestrator already evaluates output rules against the
*active step*, so the same outcome routes differently on `review` vs `pr-fix`.

### Outcome vocabulary (`AGENT_RESULT:`)

| Marker | Emitted by | Meaning | Default route |
|---|---|---|---|
| `AGENT_RESULT: COMPLETED` | workers | did the work, no verdict | next |
| `AGENT_RESULT: PASSED` | gates | evaluated, good | next |
| `AGENT_RESULT: FAILED` | gates | evaluated, not good | blocked (overridable) |
| `AGENT_RESULT: SKIPPED` | workers/monitors | nothing to do | next (overridable) |
| `AGENT_RESULT: INPUT` | any agent | blocking question (payload = the question) | pause — orchestrator-handled |

- **Payload format:** the outcome word, then any trailing same-line text is a
  free-text payload (question / reason). Mirrors the old `PR_FIX_DONE: <reason>`
  shape. The payload threads into the next agent via the existing
  needs-input-question and feedback channels.
- **`NEEDS_INPUT:` is retained** as a recognised alias for `AGENT_RESULT: INPUT`
  (operator muscle-memory, smaller blast radius).
- **Worker vs gate is a declared agent property.** Workers emit
  `COMPLETED` (+ optional `SKIPPED`); gates emit `PASSED`/`FAILED`. `INPUT` is
  orthogonal (any agent). This fixes the `COMPLETED`/`PASSED` ambiguity — an
  agent's emittable set is fixed and legible before it runs.
- **An agent may declare a wider closed subset than its class default.**
  `pr-fix` is the canonical worker declaring `{COMPLETED, SKIPPED, FAILED,
  INPUT}`.

Legacy → generic mapping (behaviour preserved):

| Old marker | Outcome | Agent (class) | Route |
|---|---|---|---|
| `REVIEW_PASS` | `PASSED` | review (gate) | next |
| `REVIEW_FAIL` | `FAILED` | review (gate) | build→`code`, pr_fix→`pr-fix` |
| `VERIFIED:` | `PASSED` | verify (gate) | next |
| `NEEDS_CODE:` / `NEEDS_REVIEW:` | `FAILED` | verify (gate) | `code` |
| `PR_FIX_DONE:` | `COMPLETED` | pr-fix (worker) | `review` |
| `PR_FIX_SKIPPED:` | `SKIPPED` | pr-fix | `wait_for_pr_signal` |
| `PR_FIX_BLOCKED:` | `FAILED` | pr-fix | blocked |
| `PR_FIX_NEEDS_DECISION:` | `INPUT` | pr-fix | needs_input |
| `CONFLICTS_RESOLVED:` | `COMPLETED` | resolve_conflicts (worker) | `pr-fix` |
| `NEEDS_INPUT:` | `INPUT` | any | needs_input |
| `SPEC_COMPLETE:` | `COMPLETED` | chat (operator-emitted) | — |

### Shared agent catalog

Prompts + properties are hoisted out of `prompts/{process}/` into a
process-independent catalog at `lotsa/prompts/agents/<agent>/` — one directory
per agent holding `agent.yaml` (declared properties) and `system.md` /
`user.md`. Workflows (`build`/`fix`/`chat`) reference agents by name. The old
`fix → build` prompt-fallback is removed; `fix` keeps its distinctive coder as
its own catalog agent (`fix_coding`).

Agent properties carry **two axes** — *what it does* × *who may set it*:

- `needs_worktree` (domain-declarable) — gates a worktree-setup prehook (Phase 3).
- `produces_changes` (Lotsa/operator-owned) — gates commit/push posthooks; a
  repo may **not** set it to opt out of review (Phase 2).
- `class` (worker | gate) — defines the emittable-outcome set.

In Phase 1 the property slots are declared and validated but only the outcome
vocabulary and catalog are wired; the hook derivations land in Phases 2–3.

### Two connection levels

- **Intra-workflow edges** — automatic, marker-routed (this ADR).
- **Inter-workflow transitions** — operator-gated promotions between whole
  workflows (`chat → build`), reusing ADR-027. `build`-vs-`fix` is a human
  depth choice, not a computed outcome — three workflows with gated promotion
  (Option B), not one forking graph.

### Provenance seam (designed now; built in Phase 5)

Repos may ship their own agents *and* workflows via a git-native convention
directory `<repo>/.lotsa/{agents,workflows}`. Namespaces: `lotsa:` (bundled)
and `repo:` (repo-local); an unqualified reference resolves `lotsa:`-first then
`repo:` (bundled names can't be silently shadowed). Resolution is against the
worktree under operation; **no cross-repo** resolution (copy to share; cross-repo
stays ADR-035's problem). **Mandatory rails** are injected regardless of what a
repo ships — the operational preamble, push/secrets discipline, and the
promotion gate — so a repo-authored workflow wires *within* the rails and cannot
route around review/push. The acknowledgment gate is dropped for v1 (operator
risk call); the pre-merge-branch exposure is noted and accepted.

## Phasing

1. **Marker vocabulary + agent catalog** (this PR) — closed `AGENT_RESULT:`
   vocab; hoist prompts into the catalog; migrate `rules:` patterns; rekey the
   orchestrator's marker parsing. No behaviour change.
2. **Agent properties + property-derived hooks** — `produces_changes` → commit/push
   (keep the per-workflow override seam).
3. **`needs_worktree` prehook** — move worktree creation off task-start.
4. **Workflow-model cleanup** — chat as a single-agent workflow; formalize the
   promotion payload (recommended-workflow + spec); optional `edges:` sugar.
5. **In-repo agents *and* workflows** — git-native `.lotsa/` discovery +
   namespaces + rails (one mechanism serves both).
6. **(Post-launch)** — visual graph editor.

## Consequences

### Positive
- One legible outcome contract; routing lives on the edge, so any compatible
  agent connects to any agent.
- Reusable agents in a single catalog; no prompt drift, no `fix → build` fallback.
- Property/provenance schema is stable from day one — Phases 2–6 are non-breaking.

### Negative / risks
- Large cross-cutting rename (every marker literal is a routing/parsing/display/
  test site). Mitigated by the sweep-guard test and the behaviour-preserving
  legacy→generic mapping.
- The vocabulary is intentionally small; `verify`'s old two-way failure collapses
  to `FAILED → code` (safe: build re-runs review after code) and `pr-fix`
  declares a wider set. Documented above.

## Relationships

- **Amends/supersedes:** ADR-043 (process model/catalog), ADR-039 (marker footer
  → real vocabulary), ADR-014 (job primitive framing).
- **Reuses/extends:** ADR-027 (promotion), ADR-024 (posthooks), ADR-025 (layered
  prompt authority + the repo-provenance trust boundary), ADR-020 (atomic
  routing transitions), ADR-034 (direct workflow selection).
- **Interacts (out of scope here):** ADR-035 (cross-repo) — sharing across repos
  is copy-based.
