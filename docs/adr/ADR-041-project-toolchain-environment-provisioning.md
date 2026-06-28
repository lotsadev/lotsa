# ADR-041: Project toolchain & environment provisioning — give the agent a workbench that can build and test

**Status**: Proposed — post-launch; the "Layer 0" precondition for ADR-031.
**Date**: 2026-06-27
**Related**: ADR-031 (runtime verification — this is its upstream Layer 0: you cannot probe an app you cannot build), ADR-029 (multi-project — the project config this extends), ADR-038 (host-sandboxed execution — the container boundary; egress is the orthogonal axis), ADR-013 (orchestrator owns deterministic infra — provisioning is the same shape), ADR-039 (outcome evaluation — a declared `test` command gives it a concrete signal), ADR-023 (registry precedent for pluggable-via-config backends). Scope: CE.

---

## Context

The `code` / `test` / `verify` agent runs in a Docker container (ADR-038)
against a per-task worktree. The base image (`Dockerfile.agent`) carries only
what the Claude CLI itself needs — `git`, `curl`, `make`, `node`, `python` —
and **nothing project-specific**, and the worktree's own dependencies are never
installed.

The consequence is concrete and was observed at launch: a verification agent
**couldn't run `vitest`** (a project devDependency — nothing had run
`npm ci`) and fell back to *"by inspection, the assertions pass against the
current file."* That is exactly the diff-only verification ADR-031 exists to
eliminate — surfacing one layer earlier, in the agent's own workbench rather
than in the app probe.

Two structural gaps underlie it:

- **No declared toolchain.** `ProjectSpec` (ADR-029) carries only
  `path`/`name`/`id`. There is nowhere to say how to install a project's
  dependencies or how to run its tests.
- **A generic, un-provisioned environment.** The base image can't carry every
  ecosystem (a Rust or Go project has no toolchain at all), and even for a
  Node project the worktree's deps are absent.

This sits **upstream of ADR-031**. ADR-031 brings the whole *app* up (compose)
and probes it; it assumes a working build. Before any of that, the agent must
be able to install deps and run the project's own tests/build/lint. Call this
**ADR-031's Layer 0**.

The operator's framing — *"allow installation of custom software for the
servers to use"* — is precisely this: let each project provision whatever
toolchain it needs, run by Lotsa deterministically, with the network access
that installing requires.

## Decision

### 1. A project environment contract

Add an optional `env` block to the project config (ADR-029 `ProjectSpec`):

```yaml
# lotsa.yaml (illustrative — schema firmed up in implementation)
projects:
  ord:
    path: ~/code/ord
    env:
      setup:   npm ci             # run once per worktree to provision deps
      test:    npm test           # how to run the suite
      build:   npm run build      # optional
      lint:    npm run lint       # optional
      image:   lotsa-agent:latest # optional per-project image override (§3)
      network: open               # egress policy (§5); default: open
```

Every field is optional. Absent ⇒ today's behaviour (generic image, no
provisioning, the agent guesses commands). The block is a *starting* contract;
more fields (services, env vars) can follow without redesign.

### 2. Orchestrator-owned provisioning

Per ADR-013 and ADR-031 §3 — deterministic infrastructure is
**orchestrator-owned, not agent-driven** — the orchestrator runs `setup` once
when a worktree is first prepared. The agent does not issue ad-hoc installs as
the source of truth; provisioning is a deterministic, repeatable step.

`test` / `build` / `lint` are surfaced to the relevant step prompts as the
**known command to run**, replacing "figure out how to test this." That also
hands ADR-039's outcome evaluator a concrete signal — *did `npm test` exit
zero?* — instead of parsing prose.

### 3. Toolchain: hybrid base + optional per-project image

- The **base image** carries common runtimes (node, python), kept lean.
- The project's **`setup`** installs the rest of its dependencies into the
  worktree (cached, §4).
- For toolchains the base can't reasonably carry (Rust + system libraries, Go,
  native SDKs), **`image:`** selects a per-project image the operator builds —
  the literal "install custom software" lever. The base stays generic; heavy
  projects bring their own image.

### 4. Persistent per-project cache

A per-project cache volume (the package store — `node_modules`/pnpm store,
cargo registry, pip cache, …) is mounted into the container, mirroring the
agent-home mount, so `setup` does not re-download the world on every task.
Keyed by project id; project-scoped, no cross-project bleed.

### 5. Egress: Open by default, pluggable

Installing dependencies requires outbound network to package registries. **The
default network policy is `open`** — the agent container may reach the
internet, so `npm ci` / `cargo fetch` / `pip install` for *any* ecosystem just
work, with no registry or mirror infrastructure to stand up.

This is a deliberate, scoped decision:

- **It is not dependency-policing.** The operator already chose their
  dependencies in the project's own manifest. Lotsa gatekeeping *which*
  packages are permitted would be redundant with that choice and, across every
  package ecosystem, an open-ended maintenance burden. Predicting the universe
  of project types an operator may run is not a job Lotsa should take on.
- **Egress is orthogonal to host safety (ADR-038).** The container is the
  host-safety boundary regardless of network policy; ADR-038's guarantees are
  unchanged. Network policy governs only what the container can *reach*, not
  what it can do to the host.
- **The residual risk `open` accepts is exfiltration**, not bad packages:
  AI-generated code (or a malicious transitive dependency) running with network
  could send the worktree, environment, or tokens to an external host — the
  operator did not pick what the model writes at runtime. For a single-operator
  self-hosted install working on the operator's **own** repositories, this is
  an accepted risk: the operator already entrusts Lotsa with that code and
  those tokens and runs it on their own infrastructure. The standing
  mitigations apply — scope `GITHUB_TOKEN` to the registered repos, scrub
  secrets from captured output, and the container boundary.

**`network:` is a per-project knob, not a hardcoded assumption.** It defaults
to `open`; an operator with stricter requirements (air-gapped or regulated
self-hosting) can tighten it to reach only an internal mirror — without Lotsa
being redesigned. Tightening is an **operator opt-in, never a Lotsa mandate**.
Phase 1 implements `open`; restricted modes are designed-for and deferred (§Phasing).

### 6. Discovery

ADR-031 §6 already specs a setup agent that reads a repo's build surface
(`package.json` scripts, `Cargo.toml`, `Makefile`, README) to draft a runtime
profile. Extend its draft to also emit the `env` block
(`setup`/`test`/`build`/`lint`), persisted with the project record or the
in-repo profile. Hand-writing the block remains the always-available path;
discovery is a convenience, not an authority.

## Security & isolation

- The container remains the **host boundary** (ADR-038), independent of egress.
- `open` egress accepts an **exfiltration** surface, scoped to single-operator
  self-hosting on the operator's own repositories (§5). Mitigations: scoped
  token, secret scrubbing, host isolation. Tightenable via `network:`.
- `setup` runs **operator-declared** commands (from the project's manifest),
  deterministically by the orchestrator — not arbitrary agent-issued network
  activity beyond what the project's own install does.
- Cache volumes are project-scoped (keyed by project id); no cross-project bleed.

## Self-hostable footprint

Lotsa itself introduces **no new mandatory dependency and no mandatory outbound
call**: the outbound traffic is the *project's* own dependency installs, which
the operator controls. Default-`open` keeps Lotsa registry-agnostic; an
operator who needs zero-outbound can run a mirror and set `network:`
accordingly. Consistent with the constitution — Lotsa mandates no external
call; the project's build does what the project's build does.

## Consequences

**Positive**
- The agent can actually run tests / build / lint — verification stops
  degrading to "by inspection," one layer below ADR-031.
- Any project type is supported uniformly via `setup` + optional `image:`; no
  per-ecosystem special-casing inside Lotsa.
- A declared `test` command gives ADR-039's outcome evaluation a deterministic
  exit-code signal.
- Caches make repeat tasks fast.

**Negative / risks**
- `open` egress is a real (accepted, scoped, documented, tightenable)
  exfiltration surface.
- `setup` adds latency to first-worktree prep — mitigated by the cache.
- A per-project `image:` is operator work; the base image won't fit every
  toolchain.
- Cache volumes are new state to scope and reap (same discipline as
  worktrees/agent-home).

## Alternatives

- **Cache/mirror by default** — stronger supply-chain story, but forces every
  operator to run registry infrastructure just to test, and can't cover every
  ecosystem out of the box. Rejected as the *default*; available as the
  `network:` opt-in.
- **Pre-bake all deps into images** — no runtime egress, but a rebuild on every
  dependency change; clumsy for active development. Rejected as default;
  achievable for operators who want it via a locked `image:` with no `setup`.
- **Let the agent install ad-hoc with no declared contract** — works under
  `--dangerously-skip-permissions`, but is non-deterministic, uncached,
  unsurfaced to the test step, and invisible to outcome evaluation. Rejected:
  provisioning is deterministic infra (ADR-013).

## Phasing

1. `env` block in project config (`setup`/`test`/`build`/`lint`); orchestrator
   runs `setup` once per worktree; surface `test`/`build`/`lint` to step
   prompts. Default `network: open`.
2. Per-project cache volume.
3. `image:` per-project override.
4. Discovery: extend ADR-031 §6's setup agent to draft the `env` block.
5. Restricted `network:` modes (internal mirror) — designed-for, deferred.

## Out of scope

- The runtime/app probe itself (ADR-031 Layers 1–5).
- Multi-tenant / shared-deployment isolation — a separate decision; this ADR
  assumes single-operator self-hosting.
- Implementation of restricted egress modes (designed-for via `network:`,
  deferred).
