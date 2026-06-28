# ADR-039: Outcome-based step advancement — evaluate the result against the goal, not just a literal marker

**Status**: Proposed — post-launch. A stopgap (mandatory-marker prompt footer) ships now; the evaluator is the follow-up.
**Date**: 2026-06-27
**Related**: ADR-014 (job/flow model + `rules:` output markers), ADR-022 (per-step model), ADR-028 (SDK runner — programmatic control), ADR-031 (runtime verification). Scope: CE.

---

## Context

Steps advance via **stdout marker rules**: `process.yaml` declares `rules:` like `pattern: "^VERIFIED:" → target` and the orchestrator matches the agent's stdout against them (`evaluate_output_rules`). To advance, the agent must emit a **literal token** — `VERIFIED:`, `SPEC_COMPLETE:`, `REVIEW_PASS`, `PR_FIX_DONE:`, `CONFLICTS_RESOLVED:`, etc.

This is **brittle for a one-shot agent**. Observed in production: the `verify` step wrote a complete, correct confirmation ("All four spec requirements are implemented correctly… matches the spec exactly") but **never emitted `VERIFIED:`** — so no rule matched, no transition fired, and the task silently parked at `waiting`. Cheaper/chattier models do this often: they reason their way to the right conclusion and forget the structured token. The failure mode is the worst kind — **silent**: the work is done, the flow just stops, and an operator has to notice and jump it manually.

A **prompt footer** that makes the marker mandatory (derived from each step's own `rules`, so it can't drift) reduces this and ships now. But it's a band-aid: a sufficiently non-compliant reply still breaks it, and we're still betting task progress on the model formatting one line correctly.

## Decision (proposed)

Add an **AI outcome evaluator** as a fallback for marker-driven steps. Markers stay the fast, deterministic path; the evaluator catches the misses:

1. Run the step; match stdout against `rules` as today.
2. **If a marker matched** → advance as today (deterministic, free).
3. **If none matched** → instead of parking silently, a cheap **structured** LLM call reads:
   - the step's **goal / expected outcomes** (declared in `process.yaml`, or inferred from the rule target names + the prompt), and
   - the agent's actual output,
   and classifies the result into **exactly one of that step's declared rule targets** (or `needs_operator`). The choice is constrained to the real targets — no free text — the same robustness pattern as the review verifier.

Properties that keep it sane:
- **Only-on-miss** → most steps emit the marker, so the evaluator rarely runs (low cost/latency).
- **Cheap model** (e.g. haiku) for the adjudication.
- **Confidence floor** → low confidence routes to the operator (`needs_input`) rather than guessing.
- Markers remain **authoritative when present**, so the deterministic path is unchanged.

## Consequences

### Positive
- A correct-but-unmarked result no longer strands the task; advancement is robust across models.
- The silent-park failure mode is removed (worst case becomes an explicit operator decision).

### Negative / risks
- An extra LLM call on the miss path (cost/latency) and a second place that can be wrong — mitigated by structured choice among real targets + a confidence floor.
- Less determinism than pure marker matching for the no-marker case (acceptable: that case was a *stall* before).

## Alternatives
- **Prompt hardening only** (shipping now): cheapest, but can't guarantee compliance.
- **Enforced outcome via the agent** — the SDK runner (ADR-028) could require a final `outcome` tool call so the marker is structurally guaranteed; the CLI runner can't enforce this. Strong where available; pursue alongside.
- **Retry-with-nudge** — on no marker, re-dispatch with "emit a marker." Cheaper than a judge but re-runs the whole step and may still miss.

## Phasing
1. **Mandatory-marker prompt footer** (done) — derived from `step.rules`, applied to all marker steps.
2. **No silent park** — a no-marker result surfaces to the operator as a decision (cheap reliability win) rather than `waiting` with no affordance.
3. **AI outcome evaluator** (this ADR's core) — fallback classification into the step's targets.
4. **SDK-runner enforced outcome tool** where the runner supports it.

## Out of scope
- Full runtime verification (run the app and probe it) — ADR-031.
- Replacing markers entirely — they remain the deterministic fast path.
