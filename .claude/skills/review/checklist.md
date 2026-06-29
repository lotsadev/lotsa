# Pre-Landing Review Checklist

## Instructions

Review the `git diff origin/main` output for the issues listed below. Be specific — cite `file:line` and suggest fixes. Skip anything that's fine. Only flag real problems.

**Confidence threshold: 7/10.** For every potential issue, silently rate your confidence that it's a real bug (not a style nit, not already handled, not a false positive). Only report issues at 7/10 or above. If you're unsure, re-read the surrounding code before deciding.

**Multi-pass review:**
- **Pass 0 (INTENT):** Does the diff actually accomplish what the PR description / linked issue / referenced ADR says? Flag goals without corresponding code, and code changes without a stated goal. This pass catches scope drift and missing-but-described work that tests don't cover.
- **Pass 1 (CRITICAL):** Run all critical categories. These block merge.
- **Pass 2 (INFORMATIONAL):** Run all remaining categories. These are noted but do not block.
- **Pass 3 (REPO-SPECIFIC):** If `.claude/review-checklist.md` exists at repo root, apply the items there as an additional pass. Severity defaults to Pass 2 unless the file specifies otherwise.

**Pattern sweep.** When you find one instance of an anti-pattern, name the class of bug in a sentence and grep for siblings before reporting. The reviewer's job is to surface the *class*, not a single instance — a partial fix invites the same bug to come back. Examples of class-shaped findings:
- "CAS happens, then a side effect runs without checking `result.won`" — grep every `atomic_transition` / `claim_task_transition` site.
- "Type annotation `str` where it should be a `Literal`" — grep every signature with that name.
- "Comment describes pre-refactor behaviour" — grep every nearby comment for stale concepts.

Report the class as one finding with all sites listed, not N separate findings.

**Output format:**

```
Pre-Landing Review: N issues (X critical, Y informational)

**INTENT** (blocking if scope drift is severe):
- description (linked stated goal vs. observed change)

**CRITICAL** (blocking):
- [file:line] Problem description
  Fix: suggested fix

**Issues** (non-blocking):
- [file:line] Problem description
  Fix: suggested fix
```

If no issues found: `Pre-Landing Review: No issues found.`

Be terse. For each issue: one line describing the problem, one line with the fix. No preamble, no summaries, no "looks good overall."

The critical categories below mirror `CONSTITUTION.md`; the informational ones mirror the per-directory `CLAUDE.md` conventions for `lotsa/` (CE runner, FastAPI + React dashboard, SQLite, orchestrator) and `rigg/` (shared SDK). When in doubt about whether a rule applies, the constitution is authoritative for the criticals.

---

## Review Categories

### Pass 0 — INTENT

Before scanning the diff for technical issues, confirm the diff actually does what it claims:

- **Stated goal vs. observed change.** Read the PR description, linked issue, or referenced ADR. List the goals it states. Walk the diff and confirm each goal has a corresponding code change. Flag goals with no code, code with no stated goal (scope creep), and code that does the opposite of the stated goal.
- **Branch staleness.** If the diff appears to revert or remove code, confirm the branch isn't simply behind `origin/main` — `git fetch origin main` and check. Apparent reverts are often missing commits, which is severe INTENT drift.
- **Acceptance criteria.** If the PR/issue lists "done when…" items, verify each is satisfied by the diff (or by tests in the diff). Missing criteria is a finding even if no test fails.
- **Test plan vs. test diff.** If the PR has a test plan, confirm the tests added cover what the plan describes. Missing tests for a stated test-plan item is an INTENT-level finding.

INTENT findings do not block merge automatically — surface them so the user can decide whether the missing/extra scope is acceptable. Severe scope drift (e.g. PR claims a bug fix but the diff is a refactor with the bug unaddressed) is blocking.

### Pass 1 — CRITICAL

#### Injection & Data Safety (CONSTITUTION §1.1)
- **SQL injection.** SQL built by string concatenation, f-strings, or `.format()` over non-literal values. Bind parameters are the rule (SQLAlchemy params, raw `?` placeholders).
- **Shell / command injection.** Subprocess calls that hand a shell a non-literal string (`shell=True` plus interpolation, `os.system`-style APIs). Use the list form (`subprocess.run([...])`) so each argument is separate. This matters acutely for the agent runner and git operations — never interpolate a branch name, repo URL, or path into a shell string.
- **HTML / template injection (XSS).** User-controlled values rendered into the dashboard without React's default escaping — flag `dangerouslySetInnerHTML` on request-derived data.
- **Untrusted deserialization.** `eval`/`exec` on input; YAML's unsafe loader on untrusted data (use `yaml.safe_load`); pickling across a trust boundary.
- **Path traversal.** File paths built from task/project input without an allowed-root check. Resolve and verify (`Path(...).resolve().is_relative_to(...)`) — critical for worktree and project paths.
- **SSRF.** Outbound HTTP with user-controlled URLs without a host/scheme allowlist.
- **Git authority.** Agents must not own git authority beyond stage + commit — branch creation, push, rebase, and remote reconciliation are the orchestrator's. Flag agent-runner or generated code that creates side branches, force-pushes, or rewrites history (orchestrator-owned git, ADR-013/024).

#### Credential & Secret Safety (CONSTITUTION §1.2)
- Credentials, API keys, OAuth tokens, or `GITHUB_TOKEN` logged, serialised, written to the DB, or returned in API responses / error messages.
- `.env` values committed or hardcoded — must use `os.environ` / the config layer.
- Secrets passed via process argv (visible in `ps` / `/proc/<pid>/cmdline`) — route through env vars or a `GIT_ASKPASS`-style helper. The git clone/push paths are the usual offenders.
- Operator config bleed — agent subprocesses must not inherit the operator's `~/.claude/` plugins/hooks/skills (pin `--setting-sources ""`).
- PII / raw task content written to human-readable summary columns on audit tables (summaries are display strings, not raw data).

#### Cryptographic Correctness (CONSTITUTION §1.3)
- General-purpose RNG (`random`, `Math.random`) used for tokens, IDs, salts, session values — require a CSPRNG (`secrets`, `crypto.randomBytes`).
- Weak password hashing (MD5, SHA1, unsalted SHA256/512) — require bcrypt/argon2/scrypt.
- Hardcoded keys, IVs, or seeds in source.
- Hand-rolled crypto primitives, even "just a wrapper".
- TLS verification disabled (`verify=False`, `rejectUnauthorized: false`) without an ADR.
- Non-constant-time comparison on tokens / signatures (`==`) — require `hmac.compare_digest` / `crypto.timingSafeEqual`.

#### Authorization (CONSTITUTION §1.4)
- API routes missing authentication except explicit unauthenticated endpoints (health, login). The dashboard's public exposure is basic-auth at Caddy; app routes still shouldn't roll custom per-route auth.
- Authorization logic at the router level only — service-layer code reachable from workers/CLI can bypass it. Authorization belongs in the service layer.
- Hardcoded user/project IDs or roles instead of the request/auth context.

#### Audit Trail Integrity (CONSTITUTION §1.5, §4.1)
- `DELETE` or `UPDATE` on append-only audit/event tables (agent messages, run/event logs) after the event completes.
- `UPDATE` on immutable version rows (versioned artifacts) — editing creates a new row.
- Hard deletes on core entities that should soft-delete (`is_active` / `is_deprecated`).
- Modifying an existing migration file after merge — schema changes go in new migrations.
- System-required rows enforced undeletable only in the UI, not the service layer.

### Pass 2 — INFORMATIONAL

#### Concurrency & Atomic Transitions (CONSTITUTION §3)
- Status/state transitions with a precondition that aren't a single atomic `UPDATE … WHERE` (CAS). The named helper is `atomic_transition` in `lotsa/db.py` (delegates to `claim_task_transition`). Never check-then-update.
- **After every CAS site, the return value (`result.won`, `rowcount`) is checked before any side effect** (`add_message`, `commit`, dispatch). Flag a CAS not followed by a won-guard — the CAS-loser must write nothing.
- Asymmetric guards — sibling branches/methods handling the same race differently (one raises, one returns silently) without an explicit reason.
- Re-entrant dispatch between awaits without a single-process `set`-based guard (`_dispatching_*`): add before the await that can yield, discard in `finally`.

#### Performance & Async Hygiene (CONSTITUTION §2)
- **Sync I/O in async context** — blocking HTTP clients, blocking sleeps, blocking file/subprocess calls inside an async function. Use the async equivalent or `asyncio.to_thread`.
- Independent awaits in a hot loop run serially instead of `asyncio.gather`.
- **Unbounded queries** — list queries returning user-visible rows without a limit/pagination; `.all()` on a potentially large table.
- **N+1 queries** — missing eager loading for relationships read in a loop.
- **Resource leaks** — file handles, DB sessions, subprocesses, sockets opened without `with` / `try-finally` / explicit close. The agent-runner subprocess and git invocations are the usual suspects.
- **Hot-path logging** — `logger.info`/`debug` inside per-request or per-poll tight loops.
- **Quadratic-or-worse** algorithms over user-controlled collection sizes.
- Caches without an invalidation strategy at write time.

#### API / CLI Discipline
- DB queries in FastAPI routers (belong in the service/orchestrator layer); HTTP concerns (status codes, headers) leaking into service code.
- Raw dicts in router signatures instead of Pydantic schemas; inconsistent error response shape.
- Blocking JSON response for an operation that can take many seconds where SSE streaming is the established pattern.
- `click` CLI commands that duplicate logic better shared with the server, or that diverge in flag/naming conventions from existing commands.

#### Database Conventions
- Missing `created_at` on new tables, missing `updated_at` on mutable tables.
- `camelCase` column names instead of `snake_case`.
- Sequential integer IDs where the codebase uses opaque/UUID-style task & project IDs.
- A schema change not delivered as a migration (or mixed with application logic in a way that makes rollback hard — migrations go in their own PR or the first commit).

#### Frontend Patterns (`lotsa/frontend`, React + shadcn/ui per ADR-012)
- Hardcoded colour/spacing/font values instead of Tailwind config tokens; custom CSS files instead of utility classes.
- New component-library dependencies (MUI, Chakra) without flagging — the stack is shadcn/ui.
- `any` in TypeScript where the API type is known; fetch logic bypassing the established API client/types in `src/api/`.

#### SDK Boundary (lotsa ↔ rigg)
- Changes to the `rigg` public surface (`StateMachine`, `OrchestrationEngine`, `AgentRunner`, shared models, git utilities) — this is the contract both editions consume. Flag as a cross-boundary change requiring explicit sign-off (root `CLAUDE.md`: "Changes to the contract between `pilot/` and `autopilot/`").
- New top-level dependency added to `rigg` (the SDK must stay self-hostable, no mandatory outbound calls).

#### LLM Prompt Issues
- 0-indexed lists in prompts (LLMs reliably return 1-indexed).
- Prompt text listing tools/capabilities that don't match what's wired up.
- `process.yaml` step markers (`SPEC_COMPLETE:`, `VERIFIED:`, `REVIEW_PASS`/`FAIL`, `PR_FIX_*`) or rule targets changed without updating the orchestrator code that parses them.
- Structured-output schema changes without updating the parser that consumes them.

#### Test Gaps
- New orchestrator/flow behaviour with no corresponding test in `lotsa/tests/`; new `rigg` behaviour with none in `rigg/tests/`.
- Negative-path tests that assert status/type but not the side effect (e.g. assert a transition was rejected but not that no message was written).
- A new state-machine transition or guard with no test exercising the race / loser path.

#### Dead Code & Consistency
- Variables assigned but never read; imports for unused modules.
- Comments or docstrings describing old behaviour after the code changed.
- Stale `TODO`s referencing completed work.
- Redundant comments (CONSTITUTION §5.1): a comment restating *what* the code already shows. Leave *why*-comments that capture a non-obvious constraint, race, or workaround.

#### Type Safety (CONSTITUTION §4.2)
- Missing type hints on new function signatures.
- Free `str` where a closed set (status enums, step kinds, role names) should be a `Literal` / typed constant.
- `dict[str, Any]` / `Record<string, unknown>` for a known shape — use a Pydantic model / dataclass / TS interface.
- `# type: ignore` / `as` assertions without a comment explaining why.

---

## Gate Classification

```
INTENT (surface, may block):         CRITICAL (blocks merge):
└─ Stated goal vs. observed change   ├─ Injection & Data Safety (incl. git authority)
   (incl. branch staleness)          ├─ Credential & Secret Safety
                                     ├─ Cryptographic Correctness
INFORMATIONAL (in PR body):          ├─ Authorization
├─ Concurrency & Atomic Transitions  └─ Audit Trail Integrity
├─ Performance & Async Hygiene
├─ API / CLI Discipline              REPO-SPECIFIC:
├─ Database Conventions              └─ .claude/review-checklist.md (if present)
├─ Frontend Patterns
├─ SDK Boundary (lotsa ↔ rigg)
├─ LLM Prompt Issues
├─ Test Gaps
├─ Dead Code & Consistency
└─ Type Safety
```

---

## Suppressions — DO NOT flag these

- Style-only issues: import ordering, blank lines, naming preferences — Ruff handles these.
- "Add a comment explaining why" — comments rot, code should be self-documenting.
- "This assertion could be tighter" when the assertion already covers the behavior.
- Consistency-only changes that don't fix or prevent a bug.
- "Regex doesn't handle edge case X" when the input is constrained and X never occurs.
- Eval/threshold magic numbers that are tuned empirically and change constantly.
- Harmless no-ops (e.g., `.filter()` on a condition always true in context).
- Missing error handling for conditions that can't occur given the call site.
- **Hypothetical "what if X changes" concerns** — review the code as it IS, not as it might become.
- **Missing validation on trusted internal values** — system-generated task/project IDs, config from known sources.
- "Consider using X instead of Y" when Y works correctly — only flag if Y has a concrete bug.
- Test-structure opinions ("split this", "use parametrize") when coverage is adequate.
- Docstring completeness on internal functions.
- Type-hint completeness on existing unchanged code — only flag on new/modified code.
- ANYTHING already addressed in the diff you're reviewing — read the FULL diff before commenting.
