# rigg/CLAUDE.md — Shared SDK

`rigg/` is the **SDK layer** that Lotsa consumes. It provides the core
orchestration primitives (state machine, dispatch engine, agent runner,
models, git utilities) without taking a position on storage, transport, or
product surface. Lotsa (`lotsa/`) brings its own infrastructure and calls
into rigg. The layer is kept deliberately product-agnostic so the
boundary stays clean and the SDK could serve a second consumer without
rework.

Read this together with the root [CLAUDE.md](../CLAUDE.md) and
[CONSTITUTION.md](../CONSTITUTION.md). Constitution rules apply here in full —
rigg is the foundation everything else builds on, so a rule violation
here propagates widely.

---

## What lives here

```
rigg/
├── __init__.py         — public re-exports (the SDK surface)
├── models.py           — Item, AgentResult, DispatchResult, RunRecord,
│                         Proof, ReviewStatus, ValidationResult,
│                         BlockingReason
├── state_machine.py    — StateMachine, TransitionRule, InvalidTransition
├── orchestration.py    — OrchestrationEngine, DispatchRule, ItemSource
│                         protocol
├── agent_runner.py     — AgentRunner protocol + ClaudeCodeRunner impl +
│                         CLI_DISPATCH_SHAPE_FRAGMENT (ADR-028) +
│                         runner registry (ADR-023): register_runner /
│                         resolve_runner / ResolvedRunner / RunnerNotFound
├── claude_agent_sdk_runner.py — ClaudeAgentSDKRunner (SDK-shaped runner,
│                         ADR-028); lazily imports claude-agent-sdk in run()
├── git.py              — GitRunner, WorktreeManager, CredentialStrategy,
│                         TokenCredentialStrategy
├── prompt_registry.py  — PromptRegistry, PromptNotFound
├── blocking.py         — BlockingProtocol, Notifier
├── proof_collector.py  — ProofCollector, ProofValidator
├── review_pipeline.py  — ReviewPipeline, ReviewParser,
│                         MarkdownReviewParser
├── parsing.py          — shared parsing helpers
├── py.typed            — PEP 561 marker (this is a typed package)
└── tests/              — pytest, covers the SDK in isolation
```

The full public surface is the `__all__` list in `__init__.py`. Anything
not re-exported there is private — don't import it from outside `rigg/`.
ADR-028 added `ClaudeAgentSDKRunner` and `CLI_DISPATCH_SHAPE_FRAGMENT` to that
surface (the fragment crosses the lotsa↔rigg boundary — `DockerAgentRunner`
imports it — so it must be public).
ADR-023 added the runner-registry surface — `register_runner`, `resolve_runner`,
`registered_prefixes_in_priority_order`, `ResolvedRunner`, `RunnerNotFound`,
`clear_registry`, and the `DEFAULT_RUNNER_PREFIXES` constant — so the
orchestrator can resolve a runner per dispatch and share the default runner's
prefix list rather than re-declaring it. The `snapshot` / `restore`
test-isolation pair stays module-internal (import `rigg.agent_runner`
directly), matching how `lotsa.registry` keeps its snapshot/restore off the
package `__all__`.

---

## Protocols rigg defines (and who implements them)

| Protocol      | Defined in              | Implementations                                                                  |
|---------------|-------------------------|----------------------------------------------------------------------------------|
| `ItemSource`  | `orchestration.py`      | `SQLiteItemSource` (`lotsa/db.py`)                                                |
| `AgentRunner` | `agent_runner.py`       | `ClaudeCodeRunner` + `ClaudeAgentSDKRunner` (rigg itself, ADR-028); `DockerAgentRunner` (`lotsa/docker_runner.py`) |
| `Notifier`    | `blocking.py`           | `ConsoleNotifier` (`lotsa/console_notifier.py`)                                   |

Consumers implement these protocols to plug their storage, dispatch, and
notification mechanisms into the shared orchestration machinery.

---

## Stability contract

Rigg is a shared layer kept product-agnostic. Any change to the public
surface is a coordinated breaking change for its consumers. The discipline:

- **Don't change a re-exported signature without updating every caller in
  the same PR** (at minimum `lotsa/`).
- **Don't add a new protocol method without a default-or-fallback path.**
  Mid-rollout, some implementations will not yet provide it. ADR-028's
  `AgentRunner.dispatch_shape_prompt()` satisfies the *intent* via a
  **simultaneous-update strategy** instead: every concrete runner declares
  the method explicitly in the same PR (no Protocol default body — rigg's
  runners satisfy the Protocol *structurally*, so a default body is silently
  absent for duck-typed implementers, raising `AttributeError` at runtime).
- **Don't grow `__all__` casually.** Anything exported becomes a
  compatibility surface. Internal helpers stay internal.
- **Models are dataclasses.** Adding optional fields is safe; adding
  required fields, removing fields, or changing types is breaking.
  Treat them as a contract.
- **Async signatures stay async.** Mixing sync and async in the SDK
  forces every caller to fan out around the difference. If you need a
  sync entry point, add one; don't convert.

When in doubt: consumer code (`lotsa/`) can grow features without touching
rigg. Rigg grows only when a primitive is genuinely generic.

---

## Where the boundary should bend

Things that look shared but aren't (yet) — and the rule for promoting them:

- **`lotsa/push_step.py`** stays in `lotsa/` for now. The current
  implementation is product-shaped (GitHub-only, local git); promote it to
  `rigg/` only when a second consumer needs it.
- **`lotsa/pr_monitor.py`** likewise — a `lotsa/` engine today. Its
  orchestrator-callback contract (`PrMonitorOrchestrator` Protocol) becomes a
  candidate for rigg once a second consumer grows PR/MR monitoring.
- **Tool/engine registry (ADR-014).** The registry primitive is generic and
  could live in rigg eventually; it lives in `lotsa/` today because only
  `lotsa/` has callers.

Rule of promotion: a module moves from `lotsa/` to `rigg/` only when
there's a real second consumer (or a consumer with a different deployment
shape). Speculative promotion creates surface without value.

---

## Testing rigg

```bash
python -m pytest rigg/tests/ -v
ruff check rigg/
ruff format --check rigg/
mypy rigg/
```

Tests cover rigg **in isolation** — no SQLite, no GitHub, no real
Claude Code subprocesses. Use fakes that satisfy the relevant protocol.
End-to-end coverage that crosses the rigg/consumer seam lives in the
consumer's own tests (`lotsa/tests/`).

---

## What to flag rather than decide

- Any change that breaks a re-export signature.
- Adding a new top-level module to `__all__`.
- Adding a runtime dependency to `rigg/` (every consumer inherits the
  install footprint).
- Changing the `Item`/`AgentResult`/`DispatchResult` model shapes.
- Tightening a protocol that already has implementations (e.g. adding a
  new required method to `ItemSource`).
