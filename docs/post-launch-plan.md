# Post-launch implementation plan

The launch build is functional (see `launch-runbook.md`). This is the ordered
backlog of **Proposed / not-yet-implemented** ADRs to work after the OSS push,
grouped by theme and sequenced by dependency and priority. Each item links to
its ADR for the full design; this file is the *sequence*, not the rationale.

Status legend: 🔴 not started · 🟡 partially implemented · ✅ done (moves to the
ADR index, drops off here).

---

## Phase 1 — pr-fix loop reliability (highest priority)

The "stuck task" family that surfaced during launch hardening (PR feedback lost
behind a wall-clock cursor after a failed dispatch + restart).
These ship first because they protect every structured-flow task.

1. **ADR-040 — Restart-resilient orchestration.** 🔴
   - 1a. Invariant + idempotency audit of every step (sync / push / commit /
     agent); add CAS guards where re-run isn't already a no-op. *Low-risk,
     foundational — start here.*
   - 1b. Working tasks become *interrupted*, not *blocked*, on startup.
   - 1c. Resume dispatch (`--resume <session_id>` where supported, else
     idempotent re-run; `resume_count` cap → block).
   - 1d. Resumed-agent prompt note (ADR-025 layer).
   - 1e. Graceful drain on shutdown.
2. **ADR-033 — Feedback tracking by comment identity.** 🔴 *Ships paired with
   ADR-040* (the identity map is durable only once ADR-040's DB-is-truth /
   monitor-cache-rebuild work lands).
3. **ADR-039 — Outcome-based step advancement.** 🟡 (mandatory-marker prompt
   footer shipped as the stopgap.) Remaining: no-silent-park surfacing, then the
   AI outcome evaluator.

## Phase 2 — verification stack (in dependency order)

You cannot probe an app you cannot build, so Layer 0 lands before the probe.

4. **ADR-041 — Project toolchain & environment provisioning.** 🔴 *(ADR-031's
   Layer 0 — must precede it.)*
   - 4a. `env` block in project config (`setup`/`test`/`build`/`lint`);
     orchestrator runs `setup` once per worktree; surface commands to step
     prompts. Default `network: open`.
   - 4b. Per-project cache volume.
   - 4c. `image:` per-project override.
   - 4d. Discovery (extend ADR-031 §6 setup agent to draft the `env` block).
   - 4e. Restricted `network:` modes (mirror) — deferred.
5. **ADR-031 — Runtime verification (run the app, probe it).** 🔴 Phases 1–5
   per the ADR (compose backend → discovery → more probe kinds → more backends).
   Depends on ADR-041 for a buildable workbench.

## Phase 3 — onboarding & runner polish

6. **ADR-037 — Web-UI first-run** (UI-managed config, secure secrets, GitHub
   integration, onboarding). 🔴
7. **ADR-038 Phase 3 — SDK-runner sandbox parity.** 🟡 (Phases 0–2 shipped:
   native sandbox on macOS, Docker isolation on Linux, per-launch opt-out.)

## Phase 4 — larger bets (revisit when the above is solid)

8. **ADR-035 — Cross-repo coordinated changes (epic coordinator).** 🔴 Phased,
   post-launch.
9. **ADR-016 — Task artifact persistence & PR-inclusion.** 🔴 (Accepted; only
   schema slots exist, no write path.)
10. **ADR-026 — Orchestrator-managed background tasks.** 🔴 (Risky / undecided —
    re-evaluate, don't build on inertia.)

---

## Sequencing notes

- **Phase 1 before everything.** It's the reliability floor; the launch incident
  came from here, and ADR-040's idempotency audit (1a) is the cheapest, most
  broadly useful single piece of work in this plan.
- **ADR-041 strictly before ADR-031.** The probe assumes a buildable, testable
  workbench; that's exactly what ADR-041 provides.
- **033 and 040 are one unit of work**, not two — don't start 033 alone.
- Items drop off this file and move to "Implemented" in the `CLAUDE.md` ADR
  index as they land.
