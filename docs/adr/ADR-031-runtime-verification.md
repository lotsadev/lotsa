# ADR-031: Runtime verification — run the app and probe it, not just read the diff

**Status**: Proposed
**Date**: 2026-06-13
**Related**: ADR-041 (project toolchain & environment provisioning — this ADR's upstream *Layer 0*: the agent's workbench must be able to install deps, build, and test before the app can be run and probed), ADR-029 (multi-project — the project-setup lifecycle this hooks into and the project config it writes to), ADR-014 (jobs as the unified primitive), ADR-013/ADR-015 (orchestrator owns git state — this extends the same principle to *runtime* state), ADR-023 (multi-provider runner registry — the pluggable-backend-via-config precedent this reuses), the `verify` step (ADR-012-era flow), CLAUDE.md self-hostable + isolation rules.

---

## Context

Lotsa's `verify` step today is **prompt-and-diff only**: the agent reads the spec, the plan, and the worktree changes, then reports conversationally. It never *runs* the thing it just built. A change can pass review, pass verify, and ship a blank page — because nobody loaded the page.

The gap is not hypothetical. During this project's own development the operator repeatedly found shipped frontend changes that didn't work at runtime (theme not switching, an unscrollable diff panel, missing filenames) — none of which a diff review surfaces, all of which a single screenshot would have. The debugging that *did* find them was: bring the app up, point Playwright at it, screenshot, read console errors. That is exactly the loop `verify` should run automatically.

The constraints that make this non-trivial are real and already part of Lotsa's world:

- **It's not just Lotsa.** Lotsa CE is becoming multi-project (ADR-029); the target can be any web app, with its own build, its own services (db/cache), its own run command.
- **Agents run sandboxed.** Lotsa agents already execute in a Docker container against a per-task worktree. Whatever runs the app-under-test has to compose with that, not fight it.
- **Self-hostable is non-negotiable.** No hosted "agentic browser" SaaS; the browser must run on the operator's own infrastructure with no mandatory outbound calls.
- **Cloud and native are coming.** iOS/Android/macOS builds need specific toolchains (and, in cloud, Mac runners). The protocol must not assume "everything is a Linux web server," even though Phase 1 only implements that case.

## Decision

Runtime verification is built from five layers. Each is independently shippable; the phasing (below) sequences them.

### 1. The runtime profile — one per project, discovered then editable

A **runtime profile** declares how to bring an app up and what to probe:

```yaml
# .lotsa/verify.yaml  (illustrative — schema firmed up in implementation)
runtime:
  backend: compose                 # which runtime backend (see §2)
  compose_file: docker-compose.yml # backend-specific config
  app_service: web                 # the service to probe
  health:
    url: http://web:8000/healthz   # readiness gate before probing
    timeout_seconds: 60
probe:
  kind: browser                    # Phase 1 = browser (see §4)
  base_url: http://web:8000
  routes: ["/", "/tasks"]          # deterministic screenshot targets
```

**Resolution order:**

1. In-repo `.lotsa/verify.yaml` (committed) — the **default and primary** location.
2. Project config, Lotsa-side (the ADR-029 project record / DB) — a fallback/override for repos the operator does *not* want to mutate (e.g. a read-only external repo Lotsa only probes).
3. Neither → runtime verification is skipped; `verify` behaves exactly as it does today.

Rationale: a profile that travels *with the repo* is the only form that survives the move to cloud/Mac runners, is versioned, and is reviewable in the PR like any other change. Committing it is not "uninvited mutation" — a generated profile lands as a normal file change on Lotsa's existing PR path, so the operator sees and approves it before merge, exactly like generated code. The Lotsa-side config fallback exists only for the genuinely-don't-touch-this-repo case.

The profile is **created by an agent at project-setup time** (Phase 2, §6) and is freely operator-editable thereafter — the discovery agent's output is a starting draft, not an authority.

### 2. Runtime backend — pluggable, `compose` first

How the app is actually run is a **backend interface**, selected per-profile by `backend:`, resolved through a registry exactly like ADR-023's runner registry:

```
RuntimeBackend:
  bring_up(worktree, profile) -> RuntimeHandle   # endpoints, container ids
  health_check(handle, profile) -> bool
  tear_down(handle)                              # idempotent, always runs
```

Phase 1 ships **`compose`**: the profile points at a Compose file (the operator's own, or a Lotsa-generated one), Lotsa brings the project up on a **per-task Docker network**, and services are reachable by name. This reuses what most real apps already have (app + db + cache) and handles multi-service out of the box.

`single_container` (build+run one Dockerfile) and `dind` (nested Docker, `--privileged`; app containers maximally isolated from each other, at the cost of a privileged — i.e. weakly host-isolated — outer container) are designed-for but deferred — they implement the same interface and drop in via `backend:` with no orchestrator change. This is the "leave it open to change via configuration" requirement.

### 3. The orchestrator owns runtime state (not the agent)

ADR-013 established that the orchestrator owns git state because git mutations are deterministic command sequences an LLM shouldn't drive. **Runtime is the same shape.** `docker compose up`, network creation, health-check polling, and teardown are deterministic infrastructure operations — the orchestrator runs them; the agent never issues a `docker` command.

Concretely, runtime bring-up/tear-down attach to the `verify` step as orchestrator-owned lifecycle (a pre-step bring-up + an **always-runs** teardown, mirroring the commit posthook's determinism). The agent is handed ready endpoint URLs in its prompt context and does only the probing. No new job type in Phase 1; a future generalization to a first-class `type: service` job (a runtime whose lifecycle spans multiple steps) is noted but not built.

Teardown is unconditional — on success, on probe failure, on agent crash, on task archive, and reaped on server restart by task-id container labels. **No silent container/network leaks** — the same cleanup discipline ADR-013-era worktree management already follows.

### 4. The probe — Playwright as a sidecar, agent-driven

Phase 1's single probe modality is **browser** (your scoped choice). The "agentic browser" is **self-hosted Playwright running as another sidecar** on the per-task network — browsers bundled into the image at build time, no runtime download, no outbound calls (satisfies the self-hostable rule). The agent container stays lean and connects to the Playwright service (CDP / the same MCP surface this project already dogfoods).

The agent's probe combines:
- **Deterministic baseline**: screenshot each route in `probe.routes`, capture console errors and an accessibility snapshot per route.
- **Acceptance-driven exploration**: the agent drives the browser to reproduce the spec's acceptance criteria (click the thing the task was about, confirm it does the thing) — the part a fixed screenshot list can't express.

### 5. Evidence as artifacts; the human judges

Screenshots, console logs, and the a11y snapshots are captured as **task-scoped evidence artifacts** (written to a task evidence dir *outside* the worktree so they are never committed; served by the dashboard via a new static route; surfaced in a right-panel **Preview/Screenshots tab** alongside Artifacts and Changes). This is the **evidence half of ADR-016's artifact taxonomy**: design records (the spec) are committed in-repo; evidence (these) lives out-of-tree and is surfaced into the PR as uploaded comment attachments, never added to git history. The two ADRs share one out-of-tree standard location, and neither home leaks into the other — no committed screenshots, no out-of-tree specs.

Per your choice, this **augments the existing `verify` step rather than auto-judging**: `verify` boots the app, runs the probe, attaches the evidence, and reports conversationally with its existing markers (`VERIFIED:` / `NEEDS_CODE:` / `NEEDS_REVIEW:`). The operator looks at the screenshots and decides. An autonomous "the UI looks correct" judge is a new and fallible authority we deliberately do not grant in Phase 1.

### 6. Project-setup discovery (Phase 2)

ADR-029 introduces project registration. This adds a **setup step** that runs once when a project is first registered (or on demand): an agent reads the repo — Dockerfile, compose file, `package.json` scripts, Makefile, READMEs — and drafts a runtime profile. Convention-detection becomes *the agent's technique*, not a brittle hard-coded sniffer; the draft is persisted and editable.

Because the default storage is the committed in-repo file (§1) but project setup runs *outside* a task's worktree/PR flow, the generated draft needs a commit path. Setup **opens a small dedicated profile PR** (`.lotsa/verify.yaml` only) for the operator to review and merge — the same reviewable-change principle as everywhere else, not a silent write to `main`. The operator can also just hand-write the file. Until discovery ships, hand-writing is the only path and Phase 1 is fully usable that way.

**The setup agent is itself a new component to build, not a reuse of an existing one.** Phase 2 ships:
- a new prompt pair (`profile_discovery-system.md` / `-user.md`) instructing the agent to read the repo's build/run surface and emit a `.lotsa/verify.yaml` draft in the §1 schema (and *only* that — it writes no other files);
- a trigger that dispatches it on project registration (and on an operator-invoked "re-detect"), wired into ADR-029's project lifecycle;
- the profile-PR opener (branch, commit the single file, push, open PR) — distinct from the normal task push path since there is no task.

It is a genuinely new agent role alongside spec/plan/code/review/verify, with its own narrow contract; the discovery agent has no authority beyond drafting the profile file.

---

## Security & isolation

- **Host Docker socket.** The `compose` backend requires the orchestrator to reach a Docker daemon. For self-hosted single-tenant use this is acceptable (the operator already trusts their own machine). It is a real privilege-escalation surface; a shared or multi-tenant deployment would need a stronger sandbox — out of scope here.
- **The app-under-test runs AI-generated code.** It executes on an isolated per-task network with no host network and egress restricted to what the profile declares. Treated as untrusted.
- **Resource bounds.** Bring-up has a health-check timeout; containers carry CPU/memory limits and a hard task-level lifetime so a runaway compose project can't starve the host. Teardown is guaranteed (§3).

## Self-hostable footprint

Compose, Playwright, and the browser binaries are all self-hostable and run with no mandatory outbound calls (Playwright honours an offline/bundled browser path; the app-under-test's egress is profile-declared). No new dependency violates the constitution's rule; the Docker-daemon requirement is an infrastructure assumption, documented, not a network dependency.

## Consequences

**Positive**
- `verify` catches the entire class of runtime-only defects (blank page, crash-on-load, broken interaction, console errors) that diff review structurally cannot.
- The runtime profile + pluggable backend means the same protocol extends to API/CLI probes (more `probe.kind`s) and to native/cloud runners (more backends) without redesign.
- Orchestrator-owned runtime keeps the determinism guarantee ADR-013 bought; the agent gains no new infrastructure authority.

**Negative / risks**
- Real new infrastructure surface: Docker-daemon coupling, per-task networks, sidecar lifecycle, binary evidence storage + a dashboard route. The most leak-prone area is container/network cleanup — mitigated by labelled reaping but genuinely new operational risk.
- Verify gets slower and costlier (image builds, compose up, browser drive). Mitigation: runtime verification only fires when a profile resolves; profile-less projects are unchanged.
- Binary artifacts are a new storage shape (screenshots) the current text-only artifact path doesn't handle.

## Phasing

1. **Phase 1** — manual `.lotsa/verify.yaml`; `compose` backend; orchestrator bring-up/health-check/teardown attached to `verify`; Playwright sidecar; agent-driven screenshots + console + a11y captured as evidence artifacts; dashboard Preview tab; human judges. **Web apps only.**
2. **Phase 2** — agent-assisted profile discovery at ADR-029 project setup: a **new discovery agent** (prompt pair), its registration/​re-detect trigger, and the single-file profile-PR opener (see §6). Net-new agent role, not a reuse.
3. **Phase 3** — additional `probe.kind`s: HTTP API (declared requests + assertions), CLI/process (command + exit/stdout assertions).
4. **Phase 4** — additional runtime backends: `single_container`, `dind`.
5. **Deferred** — native targets (iOS/Android/macOS) and the toolchain/Mac-runner environment they need; cloud execution; any autonomous pass/fail judge; multi-tenant runtime isolation.

## Open questions (for review, not blocking the ADR)

- **Profile storage default** — *resolved (operator, 2026-06-13)*: committed in-repo `.lotsa/verify.yaml` is the default; Lotsa-side config is the don't-touch-this-repo fallback.
- **Multi-tenant runtime isolation** — the Docker-socket model suits single-tenant self-hosting; a shared or multi-tenant deployment needs a separate sandbox decision (its own ADR).
- **First-class `type: service` job** — whether runtime should generalize beyond verify into a flow primitive once a second consumer appears.
