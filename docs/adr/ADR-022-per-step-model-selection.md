# ADR-022: Per-step model selection (and provider) in process.yaml

**Status**: Implemented
**Date**: 2026-06-06
**Related**: ADR-014 (jobs as the unified flow primitive — adds the field
this ADR proposes), `lotsa/config.py` (current single `model:`),
`rigg/agent_runner.py` (the runner abstraction multi-provider would
extend).

---

## Context

Today every agent step in every process runs against a single model:
`LotsaConfig.model` (default `sonnet`). The value flows from
`lotsa.yaml` → `LotsaConfig.model` → `ClaudeCodeRunner(model=…)`
at startup, and `OrchestratorService.runner` is shared across every
dispatch. The dispatcher (`lotsa/orchestrator.py`) has no per-job hook
for model selection — `runner.run(…)` is called the same way for
`coding`, `review`, `verify`, `pr_fix`, and any custom step.

Steps differ enough that a single global model is the wrong default:

- **Implementation steps** (`coding`, `pr_fix`) benefit from a strong
  coding model and the longest context window. They edit code, run
  tests, and resume sessions across rounds.
- **Review and verification steps** (`review`, `verify`) benefit from an
  *independent* perspective on the same code. Using the same model that
  wrote the code re-uses the same blind spots; a different model (often
  smaller or faster, sometimes from a different provider) catches
  classes of problems the implementer missed. This is the discipline
  ADR-014 already encodes in the `full` process (review runs in a fresh
  session "no implementation bias") — model diversity is the next step
  of the same principle.
- **Planning and spec steps** (`speccing`, `planning`) benefit from
  reasoning-heavy models; the output is short and prose-y, so context
  window matters less.
- **Conversational / chat steps** (`spec`, marker-emitting) can use a
  cheaper model — they produce a single line of output.

Today an operator who wants any of this has two choices:

1. Run the whole process against an oversized model and pay the cost
   for every step.
2. Fork the process, hard-code logic somewhere, or skip the discipline.

Neither is good. Per-step model selection is the missing knob.

Future-state: model selection extends naturally to **provider**
selection (Claude vs GPT vs local). That's out of scope for the
implementation work this ADR triggers — see *Future direction* below —
but the schema and dispatch wiring should not preclude it.

---

## Decision

**A `Job` may declare an optional `model:` field. The orchestrator
resolves the per-dispatch model as `job.model or self.config.model` and
passes it to the runner per call.** The global `model:` in `lotsa.yaml`
stays as the catalog-wide default for any job that doesn't override.

Schema additions (changes to `lotsa/flows.py`):

- `Job` (dataclass) gains `model: str | None = None`.
- `process.yaml` validator accepts `model:` on any job entry. Any
  string value passes the validator; the runner is the source of truth
  for what's a valid model name. An unknown name surfaces as a runner
  error at dispatch time, not a schema error at load time. This keeps
  the schema stable when new model names ship.

Dispatch additions (changes to `lotsa/orchestrator.py` +
`rigg/agent_runner.py`):

- `AgentRunner.run(…)` gains an optional `model: str | None` keyword
  argument. When `None`, the runner uses the model it was constructed
  with (today's behaviour). When provided, the call overrides it for
  that one invocation.
- Both `AgentRunner` Protocol implementations absorb the new kwarg —
  `ClaudeCodeRunner.run(…)` and `DockerAgentRunner.run(…)` — by
  passing `--model` per invocation. The existing CLI subprocess
  already accepts the flag per call in both runners; the change is
  per-call CLI flag selection, not a new client. Failing to update
  either impl would break Protocol conformance and raise `TypeError`
  on a docker-mode dispatch with a per-step override.
- The sole `runner.run(…)` callsite — `_run_agent` in
  `lotsa/orchestrator.py` (handles both fresh-session and
  `session_id`-resumed dispatches) — picks up `step.model` from the
  resolved `FlowStep` (= `ResolvedJob`) and threads it through.
  `_execute_action_step` dispatches `type: action` tool callables and
  doesn't call `runner.run`, so it's not on this list. The `model:`
  field has to be on both `Job` (parsed) and `ResolvedJob` (runtime),
  with a pass-through in `_resolve_jobs`; see Scope step 2 for the
  exact sites.
- Audit metadata: `output_meta["agent_model"]` (already written) records
  the *resolved* model (after override), so the dashboard and audit
  trail show which model actually ran — not the global default. Tests
  pin this for both override and fallback cases.

Example `process.yaml` after this ADR lands:

```yaml
process: software_dev
jobs:
  - { name: planning, type: agent, prompt: planning, model: opus }
  - { name: coding,   type: agent, prompt: coding,   model: sonnet }
  - { name: review,   type: agent, prompt: review,   model: opus }
  - { name: verify,   type: agent, prompt: verify }              # falls back to global
flows:
  main:
    steps: [planning, coding, review, verify]
```

Example `lotsa.yaml`:

```yaml
project_dir: /path/to/repo
model: sonnet         # global default; jobs without `model:` use this
process: software_dev
```

CLI override: no new flag. `lotsa serve --model X` (existing) overrides
the **global default** only; per-step overrides in `process.yaml` still
win for the jobs that declare them. Operators who want to swap one
step's model on the fly do it by editing the process YAML — consistent
with how every other per-step setting (prompt, rules, queue_state) is
configured.

---

## Tradeoffs

**Pros:**

- Cheap to spec, mechanically small to implement (one new field, one
  resolver, one runner kwarg, audit-trail field already records the
  right thing).
- No breaking change for existing `lotsa.yaml` files — `model:` at the
  top level still works exactly as today; the new field is opt-in.
- Encodes the model-diversity discipline (different model for review vs.
  implementation) in the same place the rest of the per-step
  configuration lives.
- Makes per-step cost optimization possible without forking the bundled
  presets — the bundled `full` process can ship with sensible defaults
  and users can override one job at a time in their own
  `process.yaml`.
- Cleanly forward-compatible with multi-provider (`model: gpt-5` would
  just route to a different `AgentRunner` impl when the registry
  exists).

**Cons:**

- Adds one moving part to the `Job` schema. Future schema additions
  should be done with awareness that `model:` already lives there.
- Increases the per-step config surface that `lotsa init`'s template
  comment has to mention — keep the template's commented block short
  so the file stays scannable.
- The "different model for review" discipline becomes easier to forget
  to apply when authoring a custom process (the field is optional). The
  bundled `full` preset should lead by example — i.e. its
  `process.yaml` should declare per-step models for the review/verify
  steps.

---

## Scope

This ADR proposes the architectural rule. Implementation lands as a
focused PR after approval:

1. Add `model: str | None = None` to `lotsa/flows.py`'s `Job`
   dataclass. Schema validator accepts the key on **any job entry**
   (consistent with the Decision section's "schema stays stable"
   rationale) — the field is silently ignored on `type: action` and
   `type: monitor` jobs because the dispatch path for those types
   doesn't call `runner.run`. Erroring at parse time on non-agent
   jobs would force operators to drop and re-add the field whenever
   they switch a step between types; silent-ignore is the kinder
   surface.
2. Add the same `model: str | None = None` field to `ResolvedJob`
   (the runtime form the dispatcher reads via the `FlowStep` alias —
   `FlowStep = ResolvedJob` at `lotsa/flows.py:139`). Thread the value
   through **both** `ResolvedJob(...)` constructor calls in
   `lotsa/flows.py`:
   - the main `_resolve_jobs` path at line 372 (`model=job.model`)
   - the catalog fallback at line 818 (`model=j.model`) — the
     "minimal ResolvedJob for jobs declared but not part of any flow"
     copies every other `Job` field; omitting `model` here means
     declared-but-unused jobs would silently drop the configured
     override when later wired into a flow.

   Both classes are required: the dispatcher reads `step.model` off
   the `ResolvedJob`, so a one-sided addition produces `AttributeError`
   at dispatch time. Both constructor sites are required: copying
   every other field except `model` at line 818 would be exactly the
   kind of silent-drop bug ADR-014's pattern sweep is meant to catch.
3. Add `model: str | None = None` keyword arg to
   `rigg/agent_runner.py:AgentRunner.run` (the Protocol). Two
   implementations must absorb the same kwarg or Protocol conformance
   breaks and per-step overrides raise `TypeError` at runtime:
   - `ClaudeCodeRunner.run` (`rigg/agent_runner.py`) — honours
     it by passing `--model` per call.
   - `DockerAgentRunner.run` (`lotsa/docker_runner.py:57`) — honours
     it by forwarding `--model` per call inside the container, the
     same way it already forwards `--max-budget-usd`.

   Both runner impls already carry a constructor-level `model`
   parameter (today's global default); the new per-call kwarg
   overrides it when set.
4. Thread the value through the sole `runner.run(…)` callsite in
   `lotsa/orchestrator.py`: `_run_agent` at
   `lotsa/orchestrator.py:2898` — the agent dispatch path that
   handles both the fresh-session and `session_id`-resumed cases.
   (`_execute_action_step` at `lotsa/orchestrator.py:2538` dispatches
   tool callables for `type: action` jobs and never calls
   `runner.run`; it's the wrong target for this change.) Resolution
   happens via a small helper used at every call site that needs the
   resolved model: ``model = info.step.model or self.config.model``.
   ``_run_agent`` uses it for the ``runner.run(...)`` call and the
   adjacent ``output_meta["agent_model"]`` write (step 5, line 2910).
   The ``chat_meta["agent_model"]`` write (step 5, line 3504) is in
   ``_completion_drainer`` — a different function from ``_run_agent``;
   the drainer applies the same helper independently against the
   ``ResolvedJob`` it reads from the drain queue, so the resolved
   value is consistent across both audit-row write paths without
   needing to flow a local variable across function boundaries.
5. Pin `output_meta["agent_model"]` to the **resolved** value (after
   override) — the two existing write sites both currently read
   `self.config.model` directly: `lotsa/orchestrator.py:2910`
   (`output_meta`) and `lotsa/orchestrator.py:3504` (`chat_meta`).
   Both must be updated to read the per-step resolved value.
6. Tests:
   - per-step override: a job with `model: opus` in `process.yaml`
     dispatches with `opus`; the audit metadata records `opus`.
   - fallback: a job with no `model:` uses `config.model`.
   - inheritance into `pr_fix`: a job in a sub-flow inherits or
     overrides the same way (no special path).
   - chat path (the `chat_meta["agent_model"]` write site at
     `lotsa/orchestrator.py:3504`) records the resolved value. Note
     that the adjacent `chat_meta["model"] = result.model` line writes
     a different field — the runner-reported actual model ID — and is
     out of scope for this ADR; this change updates `agent_model`, the
     configured-alias field, only.
7. Update `lotsa/prompts/full/process.yaml` to lead by example: declare
   `model:` per step where it matters (especially review/verify against
   coding).

In scope for **Lotsa Community Edition** (`lotsa/` + `rigg/`).

---

## Out of scope

- **Multi-provider routing.** Routing `model: gpt-5` to a different
  `AgentRunner` impl (OpenAI, Bedrock, a local Ollama runner, etc.)
  requires a runner registry analogous to the tool/engine registries
  added in ADR-014. That's a separate ADR — call it ADR-multi-provider
  when it's drafted. The schema this ADR introduces is the
  prerequisite, not the same change.
- **Per-step budgets.** `config.budget` is currently a single number
  too. Per-step budgets are a natural sibling but should be a separate
  ADR — one moving part at a time.
- **Dynamic model selection** (e.g. "use opus on retry, sonnet on first
  attempt"). The `model:` field is a static per-job value at process
  load time. Conditional / state-dependent model selection is a future
  feature; if it lands, it should reuse the same per-call runner
  override the dispatcher already plumbs.
- **CLI override per step** (e.g. `lotsa serve --step-model
  review=opus`). The static `lotsa.yaml` / `process.yaml` schema is
  expressive enough; CLI complexity isn't worth the marginal value.

---

## Future direction: multi-provider

Once `model:` is a per-step field, the model *name* is the contract
between process author and runner. Today the only valid names are the
ones `ClaudeCodeRunner` accepts (`sonnet`, `opus`, `haiku`, etc.). The
forward-compatible step looks like:

- Introduce an `AgentRunner` registry (mirrors the tool/engine
  registries from ADR-014). `name → AgentRunner` impl.
- A runner registers a model prefix or a list of model names it
  handles (`claude-*`, `gpt-*`, `ollama:*`).
- At dispatch time, the orchestrator resolves
  `(model name) → (runner)` against the registry and calls
  `runner.run(model=model_name, …)`.
- Providers ship as third-party packages registered via `lotsa.yaml`'s
  `runners:` block (same shape as `tools:` / `engines:`).

That work doesn't belong in this ADR. The point is that **this ADR's
schema doesn't preclude it** — a future change to dispatch routing
would not need a second model-selection field.

---

## Migration

Pure addition. Existing `lotsa.yaml` and bundled `process.yaml` files
continue to work without modification because `model:` on a job is
optional and defaults to the global. No deprecation cycle needed.

Operators who currently lean on a single model see no behaviour change.
Operators who want per-step selection add `model:` to the jobs in their
process YAML and the dispatcher picks it up on next restart.

---

## Acceptance criteria

- `Job` dataclass and `process.yaml` validator accept `model:` on
  any job entry (the field is silently ignored on non-agent types;
  see Scope step 1).
- The orchestrator dispatches every agent job with `job.model or
  self.config.model`.
- `ClaudeCodeRunner.run` passes the resolved model as `--model` per
  invocation.
- `output_meta["agent_model"]` records the **resolved** model in both
  the dispatch and chat write paths.
- Tests cover: override, fallback, sub-flow inheritance, audit metadata.
- `CLAUDE.md` ADR index gets a new row pointing at this file.
- Bundled `lotsa/prompts/full/process.yaml` updated to lead by
  example.
