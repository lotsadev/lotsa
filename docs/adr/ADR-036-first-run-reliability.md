# ADR-036: First-run reliability — dashboard build + startup preflight

**Status**: Accepted — pre-launch (Layers 0 & 1 of the FTUE workstream). **Implemented**: Layer 0 (build self-heal + wheel bundle) and Layer 1 (`lotsa doctor` + `lotsa serve` startup gate).
**Date**: 2026-06-24
**Related**: ADR-029 (projects — the "valid repo" check), ADR-034 (chat-first — what a healthy server first shows), the README quickstart. **Forward reference**: ADR-037 moves auth and project setup into the dashboard, which will supersede parts of the CLI preflight below. Scope: CE.

---

## Context

The launch goal is **`pip install lotsa && lotsa serve` just works**. Two things break that today:

1. **The dashboard bundle is a gitignored build artifact.** A fresh `lotsa serve` with no built `lotsa/server/static/dist` renders a blank page, with no hint why.
2. **Missing prerequisites fail cryptically.** No `claude` CLI, no API key, no git repo to work in, no `GITHUB_TOKEN` — each surfaces as an obscure downstream error instead of an upfront, actionable message.

The first-run experience should be: the terminal does the *minimum* (and ideally nothing, for a packaged install), and the server either runs correctly or tells you exactly what to fix. This ADR covers the **CLI/packaging** layers; moving configuration into the web UI is ADR-037.

## Decision

### 1. Dashboard build — "just works", via make

- **`lotsa serve` self-heals the bundle.** At startup it checks for a present, non-stale `lotsa/server/static/dist`. If it's missing or older than `lotsa/frontend/src/`:
  - **Node available** → auto-build it (`npm run build`) before serving.
  - **Node absent and bundle missing** → fail fast with a one-line remedy (`make frontend`, or install Node). Never serve a blank or stale dashboard.
- **Packaged installs carry the bundle.** The wheel build runs the frontend build and includes `dist/` (a hatchling build hook), so `pip install lotsa` ships a ready dashboard and **needs no Node at all**. `dist/` stays gitignored; only the wheel carries it.
- **All of it is plain make.** `make setup` (install + build), `make frontend` (rebuild), `make build` (wheel, frontend bundled). No bespoke steps.

### 2. Startup preflight — `lotsa doctor`, run at boot

A `lotsa doctor` command runs a set of checks and prints pass/fail with remediation. **`lotsa serve` runs the same checks at startup and gates on them** — a misconfigured install should not limp along silently.

Check taxonomy:

- **FATAL (refuse to start):**
  - No agent auth **for the SDK runner** — the `claude-agent-sdk` runner (ADR-028) requires `ANTHROPIC_API_KEY`; missing it is fatal. *(Refinement during implementation: for the default native **CLI runner**, an absent `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` is only a **WARN**, not fatal — the `claude` CLI also authenticates via `claude login` keychain credentials, which are invisible from the environment, so a hard fail would lock out the most common setup. The original flat-FATAL rule did not account for keychain auth.)*
  - No agent auth **in docker mode** — a container can't see the host's `claude login` keychain, and the runner forwards only `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` via `-e`, so a missing env credential is FATAL (otherwise the container's `claude` fails mid-run with a generic exit 1).
  - The `claude` CLI is not on `PATH`.
  - No project resolves to a real git repository (ADR-029; the zero-config `default` from the launch directory usually satisfies this — it only fails when genuinely misconfigured).
  - The dashboard bundle is missing and cannot be built (§1).
  - **Docker mode only:** the Docker daemon is unreachable, or the agent image is missing. The default `lotsa-agent:latest` is a local-build tag, so a missing one is FATAL with a `lotsa build` remedy; a custom/registry image is a WARN (it will be pulled). This check is skipped entirely when docker mode is off.
- **CONFIRM (interactive acknowledgement):**
  - `GITHUB_TOKEN` is missing → push and pull-request features are disabled. **The operator must explicitly confirm continuing without it** at startup, rather than discovering it later. This is a deliberate choice over a passive warning: a silent "PR features quietly off" is a worse surprise than a one-time prompt.
- **WARN (note and continue):**
  - Soft/degraded conditions (e.g. git author identity unset — agent commits will need it, but chat-only use won't).

### 3. Headless / CI behaviour

The interactive CONFIRM must not hang a non-interactive run. A `--yes` flag (and a `LOTSA_ASSUME_YES` env equivalent) pre-acknowledges all CONFIRM prompts; CI sets it. **Without it, a CONFIRM condition in a non-TTY fails closed** (the server aborts) — so a misconfigured deployment surfaces the gap instead of running degraded forever.

## Consequences

### Positive
- `pip install lotsa && lotsa serve` works with no manual build and no Node, for packaged installs.
- No blank dashboard; every missing prerequisite produces an upfront, actionable message.
- The `GITHUB_TOKEN` confirmation makes "running without PR features" a conscious decision, not a silent default.

### Negative
- The from-source `serve` path gains a Node/build dependency (mitigated: only when the bundle is missing; packaged installs ship it).
- Startup gains a preflight pass and, occasionally, an interactive prompt (mitigated by `--yes`/env for headless).

### Migration
New behaviour; no data migration. Existing operators who launch with everything configured see no prompts.

## Out of scope (→ ADR-037)
- Moving auth/config and project setup into the dashboard UI.
- Secure storage of secrets (the constraint that credentials can't live in plaintext in `lotsa.db`, per CONSTITUTION §1.2).
- GitHub OAuth / cloning repos from the UI.
- In-dashboard onboarding ("teach" the chat→promote loop).
