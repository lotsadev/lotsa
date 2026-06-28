# Pre-Landing Review Checklist

## Instructions

Review the `git diff origin/main` output for the issues listed below. Be specific — cite `file:line` and suggest fixes. Skip anything that's fine. Only flag real problems.

**Confidence threshold: 7/10.** For every potential issue, silently rate your confidence that it's a real bug (not a style nit, not already handled, not a false positive). Only report issues at 7/10 or above. If you're unsure, re-read the surrounding code before deciding.

**Multi-pass review:**
- **Pass 0 (INTENT):** Does the diff actually accomplish what the PR description / linked issue / referenced plan says? Flag goals without corresponding code, and code changes without a stated goal. This pass catches scope drift and missing-but-described work that tests don't cover.
- **Pass 1 (CRITICAL):** Run all critical categories. These block merge.
- **Pass 2 (INFORMATIONAL):** Run all remaining categories. These are noted but do not block.
- **Pass 3 (REPO-SPECIFIC):** If `.claude/review-checklist.md` exists at repo root, apply the items there as an additional pass. Severity defaults to Pass 2 unless the file specifies otherwise.

**Pattern sweep.** When you find one instance of an anti-pattern, name the class of bug in a sentence and grep for siblings before reporting. The reviewer's job is to surface the *class*, not a single instance — a partial fix invites the same bug to come back. Examples of class-shaped findings:
- "X happens before guard Y" — grep every site of Y for sites of X.
- "Type annotation `str` where it should be a Literal" — grep every signature with that name.
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

---

## Review Categories

### Pass 0 — INTENT

Before scanning the diff for technical issues, confirm the diff actually does what it claims:

- **Stated goal vs. observed change.** Read the PR description, linked issue, or referenced plan. List the goals it states. Walk the diff and confirm each goal has a corresponding code change. Flag goals with no code, code with no stated goal (scope creep), and code that does the opposite of the stated goal.
- **Acceptance criteria.** If the PR/issue lists acceptance criteria or "done when…" items, verify each one is satisfied by the diff (or by tests in the diff). Missing criteria is a finding even if no test fails.
- **Test plan vs. test diff.** If the PR description has a test plan, confirm the tests added cover what the plan describes. Missing tests for a stated test plan item is an INTENT-level finding, not a test-gap finding.

INTENT findings do not block merge automatically — surface them so the user can decide whether the missing/extra scope is acceptable. Severe scope drift (e.g. PR claims a bug fix but the diff is a refactor with the bug unaddressed) is blocking.

### Pass 1 — CRITICAL

#### Injection & Data Safety
- **SQL injection.** SQL constructed by string concatenation, f-strings, or `.format()` over non-literal values — flag regardless of which DB layer is in use. Bind parameters are the rule (SQLAlchemy params, raw `?`/`%s` placeholders, `PreparedStatement`, etc.).
- **Shell / command injection.** Subprocess invocations that hand a shell a non-literal string (`shell=True` plus interpolation, `os.system`-style APIs with concatenation, equivalent in other languages). Use the list/array form so each argument is a separate element. Secrets to a child process route via env or askpass — never argv.
- **HTML / template injection (XSS).** User-controlled values rendered into HTML without the framework's default escaping. In any framework, flag the explicit-bypass APIs (raw-HTML props in React, `| safe` in Jinja, `v-html` in Vue, `[innerHTML]` in Angular, etc.) when applied to data that originated in a request without a vetted sanitiser in between.
- **Untrusted deserialization.** Serialisation parsers run on untrusted input — anything that executes constructors or arbitrary code during decode (binary object pickling, YAML's unsafe loader, XML with external entities), or `eval`/`exec` on input. Prefer JSON or a typed schema parser for cross-boundary data.
- **Path traversal.** File paths built from user input without an allowed-root check. Resolve and verify (Python `Path(...).resolve().is_relative_to(...)`, Node `path.resolve` + `startsWith`, etc.) regardless of language.
- **SSRF.** Outbound HTTP clients called with user-controlled URLs without a host/IP/scheme allowlist. Outbound URL is a trust boundary in any language.
- **TOCTOU races.** Check-then-update patterns that should be a single atomic `UPDATE ... WHERE` statement.
- **Migration safety.** Missing rollback path on schema migrations; modifications to existing migration files. (Project may opt for forward-only — confirm in repo conventions before flagging.)
- **N+1 queries.** Eager loading missing for relationships used in loops. The exact API is ORM-specific (`selectinload`/`joinedload`, `select_related`/`prefetch_related`, `include`, etc.) but the rule is universal.
- **`DELETE` or `UPDATE` on append-only audit tables** (`tool_runs`, `messages`, etc.) after creation.
- **`UPDATE` on `tool_versions.code`** — versions are immutable once created.

#### Authentication & Authorization
- Routes missing `current_user` dependency (except `/health` and `/auth/*`)
- Custom auth logic instead of using the Zitadel JWT flow from `api/dependencies/auth.py`
- Missing organisation/tenant scoping on queries — cross-tenant data leaks
- Hardcoded user IDs, roles, or permissions instead of checking the JWT claims
- Authorization checks at the router level only — service-layer code that bypasses HTTP can skip them. Authorization belongs in the service layer.

#### Credential & Secret Safety
- Credentials, API keys, or `vault_secret_path` values logged, serialised, or returned in API responses
- Direct credential access outside `connector.get()` in generated tool code
- `.env` values committed or hardcoded — must use `os.environ` / config
- PII from tool runs written to `input_summary` or `output_summary` (these are human-readable summaries, not raw data)
- Secrets passed via process argv (visible in `/proc/<pid>/cmdline`, ps output) — pass via env var or `GIT_ASKPASS`-style helper instead.

#### Cryptographic Correctness
- General-purpose RNG used for security tokens, IDs, salts, or session values — flag whatever the language's name is for it (Python `random`, JS `Math.random`, Java `Random`, etc.). Required: a CSPRNG (`secrets`, `crypto.randomBytes`, `crypto/rand`, `SecureRandom`).
- Weak hashes for passwords or auth (MD5, SHA1, unsalted SHA256/SHA512). Use a memory-hard / cost-tunable algorithm — bcrypt, argon2, or scrypt via the language's standard hashing library.
- Hardcoded keys, IVs, or seeds in source.
- Custom crypto primitives — flag any hand-rolled encryption / signing / KDF, even if "just a wrapper".
- TLS verification disabled (`verify=False` in httpx/requests, `rejectUnauthorized: false` in node, `InsecureSkipVerify` in Go, etc.) without a documented reason.
- Predictable comparison on tokens / signatures — flag `==` / `===` / `bytes.Equal`. Required: constant-time compare (`hmac.compare_digest`, `crypto.timingSafeEqual`, `subtle.ConstantTimeCompare`).

#### Audit Trail Integrity
- Hard deletes on core entity tables (`tool_runs`, `tool_versions`, `tools`, `users`, `organisations`) — use soft deletes (`is_active`, `is_deprecated`)
- Modification of completed `tool_runs` records
- Deletion of `everyone` or `admins` system groups

### Pass 2 — INFORMATIONAL

#### API Layer Discipline
- Database queries in routers (should be in services layer)
- HTTP concerns (status codes, headers) in service layer (should be in routers)
- Raw dicts in router function signatures instead of Pydantic schemas
- Inconsistent error response format — should be `{"error": "message", "code": "CODE"}`
- Blocking JSON response for operations that could take more than a few seconds (should use SSE streaming)

#### Database Conventions
- Sequential integer IDs instead of UUIDs for user-facing or cross-tenant entities
- Missing `created_at` on new tables, missing `updated_at` on mutable tables
- `camelCase` column names instead of `snake_case`
- JSONB columns that should be graduated to indexed columns (if the field is queried frequently)

#### Frontend Patterns
- Hardcoded colour values, spacing, or font sizes instead of Tailwind config tokens
- Custom CSS files instead of Tailwind utility classes
- Introduction of component library dependencies (shadcn, MUI, Chakra) without flagging
- `fetch()` or `XMLHttpRequest` in web UI iframe code that bypasses the `lotsa-bridge.js` communication layer

#### Concurrency & Race Conditions
- Read-check-write without unique constraint or conflict handling
- `find_or_create`-style patterns on columns without unique DB index
- Status transitions without atomic `WHERE old_status = ? UPDATE SET new_status`. After every CAS site, the return value (e.g. `won`, `rowcount`) must be checked before any side effect (`add_message`, `commit`, etc.) — flag CAS calls that aren't followed by a guard.
- Missing lock or queue for operations that shouldn't run concurrently
- Asymmetric guard semantics — sibling branches/methods of the same operation that handle the same race differently (one raises, another silently returns) without an explicit reason.

#### Performance & Resource Use
- **Sync I/O in async context.** Blocking calls (synchronous HTTP clients, blocking sleeps, blocking file/subprocess) inside an async function. Use the async equivalent or the runtime's thread offload (`asyncio.to_thread`, `Promise`-wrapped worker, etc.).
- **Unbounded queries.** `SELECT … FROM table` with no row limit / pagination on endpoints that return user-visible lists. Same for ORM "load all" calls (`.all()`, `.findAll()`, `.fetchAll()`, etc.) on potentially large tables.
- **Hot-path logging.** Info/debug log calls inside tight loops or per-request paths that fire thousands of times per second. Use sampling or a metrics counter (Prometheus, OTel, your stack's equivalent).
- **Resource leaks.** File handles, DB connections, subprocesses, network sockets opened without a matching close / scope-exit / try-finally. Flag any resource-acquiring call whose lifetime isn't bounded — language-specific idioms differ (`with` in Python, `defer` in Go, `using` in C#, `try-with-resources` in Java) but the rule is universal.
- **Quadratic-or-worse algorithms.** Nested loops over collections of unbounded size (e.g. iterating all messages for each task in a list view). Flag with the input size assumption.
- **Cache invalidation.** New caches without a documented invalidation strategy; cache reads on data that mutates without a corresponding cache-bust on the writer.

#### Security Misconfiguration
- Debug flags / verbose-error modes (Django `DEBUG=True`, Flask debug mode, FastAPI `debug=True`, Express `NODE_ENV=development`, etc.) in code paths that ship to production
- Default credentials or seeded test users that can run in production
- CORS allowlists containing `*` on endpoints that handle authenticated requests
- Session/auth cookies missing the `Secure`, `HttpOnly`, or `SameSite` flags
- Error responses that leak stack traces, file paths, or library versions to clients (regardless of framework)
- Public bind (`0.0.0.0`) on services that should be loopback-only

#### LLM Prompt Issues
- 0-indexed lists in prompts (LLMs reliably return 1-indexed)
- Prompt text listing tools/capabilities that don't match what's actually wired up
- Word/token limits stated in multiple places that could drift
- Structured output schema changes without updating the parser that consumes them

#### Test Gaps
- New API endpoint with no corresponding test file in `tests/api/`
- New service function with no corresponding test in `tests/services/`
- Negative-path tests that assert type/status but not side effects
- Security enforcement features without integration tests verifying the enforcement path
- Factory fixtures inlined in test functions instead of using `conftest.py` factories

#### SDK Contract
- Changes to `lotsa_sdk` public interface (`connector`, `output`, `log`, `storage`, `agent`) — these are breaking changes requiring a harness version bump
- New top-level imports added to generated tool code templates
- Changes to the code review prompt output schema without updating the review service parser

#### Dead Code & Consistency
- Variables assigned but never read
- Comments or docstrings describing old behavior after code changed
- Stale `TODO` comments referencing completed work
- Import statements for unused modules

#### Type Safety
- Missing type hints on new function signatures (Python)
- `any` types in TypeScript where a specific type is known
- Type assertions (`as`, `# type: ignore`) without a comment explaining why
- Pydantic models with `dict` fields where a typed model would be more appropriate

---

## Gate Classification

```
INTENT (surface, may block):         CRITICAL (blocks merge):
└─ Stated goal vs. observed change   ├─ Injection & Data Safety
                                     ├─ Authentication & Authorization
INFORMATIONAL (in PR body):          ├─ Credential & Secret Safety
├─ API Layer Discipline              ├─ Cryptographic Correctness
├─ Database Conventions              └─ Audit Trail Integrity
├─ Frontend Patterns
├─ Concurrency & Race Conditions     REPO-SPECIFIC:
├─ Performance & Resource Use        └─ .claude/review-checklist.md (if present)
├─ Security Misconfiguration
├─ LLM Prompt Issues
├─ Test Gaps
├─ SDK Contract
├─ Dead Code & Consistency
└─ Type Safety
```

---

## Suppressions — DO NOT flag these

- Style-only issues: import ordering, blank lines, naming preferences — Ruff handles these
- "Add a comment explaining why" — comments rot, code should be self-documenting
- "This assertion could be tighter" when the assertion already covers the behavior
- Suggesting consistency-only changes that don't fix a bug or prevent one
- "Regex doesn't handle edge case X" when the input is constrained and X never occurs
- Eval threshold changes or magic numbers that are tuned empirically and change constantly
- Harmless no-ops (e.g., `.filter()` on a condition that's always true in context)
- Missing error handling for conditions that can't occur given the call site
- **Hypothetical "what if X changes" concerns** — review the code as it IS, not as it might become. If the concern requires a code change that hasn't happened, it's not a bug.
- **Missing validation on trusted internal values** — UUIDs, task IDs generated by the system, config from known sources. Don't flag missing validation on values the system itself generates.
- "Consider using X instead of Y" when Y works correctly — only flag if Y has a concrete bug
- Test structure opinions ("split this into two tests", "use parametrize") when coverage is adequate
- Docstring completeness — don't flag missing docstrings on internal functions
- Type hint completeness on existing unchanged code — only flag on new/modified code
- ANYTHING already addressed in the diff you're reviewing — read the FULL diff before commenting
