# CLAUDE.md — Lotsa (Community Edition)

Lotsa CE is a local task runner and web dashboard for Claude Code: a local
SQLite store, a worktree-per-task workspace, and a FastAPI/React dashboard —
no hosted infrastructure and no mandatory outbound calls.

The repo carries a `CLAUDE.md` in each main directory. **Read this file plus
the `CLAUDE.md` for the directory you're touching.**

Always also read [CONSTITUTION.md](./CONSTITUTION.md). It captures the
non-negotiable rules (security, secrets, audit-trail integrity, concurrency,
performance). The constitution is *what to never do*; per-directory
`CLAUDE.md` files are *how this area is organised*.

---

## Repository map

```
lotsa/       — Community Edition: CLI, config, SQLite store, dashboard,
               orchestrator, push step, PR monitor. Most development is here.
               see lotsa/CLAUDE.md

rigg/   — Shared SDK that CE consumes (StateMachine,
               OrchestrationEngine, AgentRunner, models, git utilities).
               see rigg/CLAUDE.md

docs/        — Documentation and ADRs.
```

Per-edition tests live in `lotsa/tests/` and `rigg/tests/`.

---

## Workflow

### Branches and PRs

- One branch per change; never push directly to `main`.
- PRs require CI to pass — lint, typecheck, tests.
- Migrations go in their own PR or are the first commit of a feature PR.
  Never mix schema changes and application logic in a way that makes
  rollback difficult.

### When to plan vs. build directly

Plan first (brainstorm → write a plan → execute) when **all** of these hold:

- Task spans multiple files across different areas (e.g. the CE orchestrator
  and the shared `rigg` SDK).
- Task introduces new architecture or changes existing patterns.
- Task will take more than ~30 minutes of implementation.

Otherwise (single file/small set, known-location bug fix, test/utility, or
"just build it"), write code directly.

### Action bias

When the task is clear, start producing output early. Exploration serves
implementation — once you've read the target file and understand the change,
start writing. Read more files as questions arise during implementation.

### What to flag rather than decide

- New top-level dependencies (Python packages, services).
- Changes to the contract between `lotsa/` and `rigg/` (the SDK
  boundary).

---

## Self-hostable dependency rule

**Every dependency must be self-hostable with no mandatory outbound calls, or
have a documented self-hosted alternative.**

Before adding a dependency, confirm:

- A self-hosted deployment option exists.
- It runs in a Docker container with no mandatory external calls.
- It is compatible with EU-only infrastructure.

Dependencies that violate this require an ADR.

**Footnote — `claude-agent-sdk` (ADR-028):** the SDK-shaped agent runner's
dependency is *not* a thin pure-API client; it drives the Claude Code
CLI/Node runtime under the hood, so it carries a transitive CLI-runtime
requirement rather than reducing the self-hostable footprint. It honours
`ANTHROPIC_BASE_URL` (no mandatory outbound calls beyond the configured
endpoint) and is imported lazily, so operators who don't select that runner
incur no cost. See ADR-028's *Self-hostable footprint* note.

---

## ADR index

Architecture Decision Records live in `docs/adr/`. Write an ADR before making
a significant architectural decision — anything that changes a protocol
contract or introduces a new top-level dependency.

| ADR | Title | Scope |
|-----|-------|-------|
| 012 | shadcn/ui for the dashboard (**Implemented**) | CE |
| 013 | Orchestrator owns git state | **Superseded** — Rule 1 shipped; unimplemented Rules 2/3 continued in ADR-024 |
| 014 | Jobs as the unified flow primitive (**Implemented**) | CE |
| 015 | Orchestrator syncs task branch to main before pr-fix (**Implemented**) | CE |
| 016 | Task artifact persistence & PR-inclusion policy (**Accepted** — not implemented; `output_file`/`commit` schema slots only, no write path) | CE |
| 017 | In-app agent activity visibility (**Implemented** — `AgentRunner.read_activity` + shared JSONL parser, `agent-activity` endpoint, dashboard Activity tab, soft-timeout dot, `lotsa inspect`) | CE |
| 018 | Task branches stay synced with upstream across the lifecycle (**Implemented**) | CE |
| 019 | Operator-acknowledged overrides for guard conditions (**Implemented**) | CE |
| 020 | A typed atomic-transition helper for CAS state changes (**Implemented**) | CE |
| 021 | Per-task process dispatch — lifts the singleton-process constraint (**Implemented**) | CE |
| 022 | Per-step model selection (and provider) in process.yaml (**Implemented**) | CE |
| 023 | Multi-provider agent runners via a registry (**Implemented** — registry primitive; per-step routing live via ADR-022's `step.model`) | Shared (rigg) |
| 024 | Commit joins push as a deterministic orchestrator step (**Implemented** — realized as a per-step `commit` posthook) | CE |
| 025 | Layered system-prompt authority — Lotsa append + project CLAUDE.md as context (**Implemented**) | Shared (rigg) |
| 026 | Orchestrator-managed background task support (**RISKY / UNDECIDED** — deferred) | Shared (rigg) |
| 027 | Operator-driven process promotion — mid-life process switch for tasks (**Implemented**) | CE |
| 028 | Claude Agent SDK runner — third runner shape alongside CLI and API (**Partially implemented** — Phases 1, 2 & 3 shipped; Phase 4 deferred) | Shared (rigg) |
| 029 | Multi-project support — tasks carry a project, the server doesn't (**Implemented** — pre-alpha breaking schema change: `tasks` recreated with `project_id`, flat worktrees tree cleared) | CE |
| 030 | PR-lifetime monitoring — an opened PR is watched regardless of task state (**Implemented**) | CE |
| 031 | Runtime verification — run the app and probe it, not just read the diff (**Proposed** — not implemented) | CE |
| 033 | PR feedback tracking by comment identity, not a wall-clock cursor (**Proposed** — not implemented; priority raised after a third recurrence 2026-06-27; ships paired with ADR-040) | CE |
| 034 | Chat-first task creation — load the full process catalog, default to chat (**Implemented** — `start()` loads the full bundled catalog; new-task default is `chat`; `--flow` selects the picker default rather than gating what loads) | CE |
| 035 | Cross-repo coordinated changes — epic coordinator + contract-first fan-out (**Proposed** — post-launch, phased) | CE |
| 036 | First-run reliability — dashboard build (auto-build + wheel bundle) + startup preflight (`lotsa doctor`) (**Implemented** — Layer 0 build self-heal + wheel bundle; Layer 1 `lotsa doctor` + `lotsa serve` preflight gate with FATAL/CONFIRM/WARN taxonomy) | CE |
| 037 | Web-UI first-run — UI-managed config, secure secrets, GitHub integration, onboarding (**Proposed** — post-launch, phased) | CE |
| 038 | Host-sandboxed agent execution — native runs use the OS sandbox + `dontAsk` and drop `--dangerously-skip-permissions`; bypass only via container or an explicit per-launch flag (**Implemented** — Phases 0–2: native sandbox (macOS) + per-launch opt-out; Linux isolation via Docker (native sandbox doesn't start on Linux servers); Phase 3 SDK parity pending) | CE |
| 039 | Outcome-based step advancement — evaluate the agent's result against the step goal when no stdout marker matched, instead of silently parking (**Proposed** — post-launch; mandatory-marker prompt footer shipped as the stopgap) | CE |
| 040 | Restart-resilient orchestration — the DB is the state of record; resume in-flight agents (and idempotent step re-runs) instead of blocking on restart (**Implemented** — phases 1–5: idempotency audit, interrupted-not-blocked startup, auto-resume dispatch via runner `supports_resume`, resumed-agent prompt note, graceful drain; phase 6 / ADR-033 deferred) | CE |
| 041 | Project toolchain & environment provisioning — declared `setup`/`test`/`build`/`lint` + per-project cache + optional per-project `image:`; egress Open by default and pluggable (ADR-031's upstream Layer 0) (**Proposed** — post-launch) | CE |
| 042 | `lotsa deploy` CLI — bundle the deploy assets in the wheel, ship + run the installer over ssh from a declarative `deploy.yaml`; PyPI install by default; Debian/Ubuntu + systemd target with a fail-fast platform preflight (**Implemented**) | CE |
| 043 | Two-phase Think→Execute task model — three-process catalog (`chat`/`build`/`fix`) replaces the flat five presets; ungated build, PR-primary Execute, operator mark-complete + `awaiting_operator` escape hatch; fixes the ADR-013 `standard` violation (**Implemented** — supersedes ADR-014's catalog; amends ADR-027/030/034) | CE |
| 044 | Workflows for agents — generic `AGENT_RESULT:` outcome vocabulary (`COMPLETED`/`PASSED`/`FAILED`/`SKIPPED`/`INPUT`) + a shared, process-independent agent catalog (`lotsa/prompts/agents/`); routing moves from marker name to flow edge (**Implemented — Phase 1**; Phases 2–6 proposed: property-derived hooks, `needs_worktree` prehook, workflow-model cleanup, git-native `.lotsa/` provenance, visual editor). Amends ADR-043/039/014 | CE |

Design rationale for a specific area belongs in that directory's `CLAUDE.md`
or in an ADR. This index is the registry.
