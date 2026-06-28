# Lotsa Constitution

Read this before writing or modifying code that touches security, secrets,
data persistence, concurrency, or performance-sensitive paths. Per-directory
`CLAUDE.md` files reference this; the constitution and the relevant
`CLAUDE.md`(s) load together.

These rules are **non-negotiable**. They exist because Lotsa runs AI-generated
code against production data in regulated industries, and the Community
Edition runs agents that mutate user repositories. Every rule traces to a
class of bug that is either expensive to detect or impossible to recover from
once shipped. If a rule seems wrong for a specific case, flag it for product
decision rather than working around it.

Scope: this file is the *constitution* — what to never do and what to always
do, in language-agnostic terms. Per-directory `CLAUDE.md` files are the
*architecture and conventions* — how each area is organised and which
concrete tables, modules, or APIs instantiate each principle. The
constitution drives them; they implement it.

---

## Section 1 — Security

### 1.1 Injection prevention

The principle: data flows through bind parameters, not string concatenation.
The tools change; the principle doesn't.

- **SQL**: every query that mixes data with structure uses the bind-parameter
  interface of whatever DB layer is in play (SQLAlchemy core/ORM, raw
  driver `?`/`%s` placeholders, JDBC `PreparedStatement`, etc.). Never
  construct SQL with f-strings, `.format()`, or `+` over values that
  originate in a request.
- **Shell / command**: never hand a shell a string built from non-literal
  values. Pass arguments as separate elements via the array/list form
  of your subprocess API (Python's `subprocess.run([...])`, Node's
  `execFile`, Go's `exec.Command`). When a token must reach a child
  process, route it through env vars or an askpass-style helper —
  never via argv (visible in `ps` and `/proc/<pid>/cmdline`).
- **HTML / template**: user-controlled values render through escaping by
  default. Bypassing escapes (raw HTML props in React, `| safe` in
  Jinja, `v-html` in Vue, `[innerHTML]` in Angular, etc.) requires the
  value to have been sanitised through a vetted library — never apply
  the bypass to raw request data.
- **Untrusted deserialization**: never run `eval` / `exec` on input.
  Never use a serialisation format that executes constructors or
  arbitrary code during decode (binary object pickling, YAML's unsafe
  loaders, XML with external entities, etc.) on data crossing a trust
  boundary. Prefer JSON or a typed schema parser.
- **Path traversal**: any file path built from a request parameter is
  resolved and checked against an allowed root. The exact API differs
  per language (Python `pathlib.Path(...).resolve().is_relative_to(...)`,
  Node `path.resolve` + `startsWith`), but the rule is universal.
- **SSRF**: outbound HTTP clients called with user-controlled URLs
  require an allowlist of hosts / IPs / schemes. Outbound URL is a
  trust boundary regardless of which HTTP client is used.

### 1.2 Credential & secret handling

- Credentials, API keys, and secret-manager paths never appear in
  databases, in logs, in API responses, or in error messages.
- `.env` values are never committed and never hardcoded. Read via
  `os.environ` or the config layer.
- Secrets do not pass via process argv (visible in `/proc/<pid>/cmdline`
  and `ps`). Use env vars or a `GIT_ASKPASS`-style helper.
- PII from tool/agent runs never enters human-readable summary columns
  on audit tables. Summary columns are display strings, not raw data.

### 1.3 Cryptographic correctness

The principles below are language-agnostic; each language has its own
"right" library to satisfy them. When in doubt about which API to
reach for, check the language's stdlib crypto guidance first.

- **CSPRNG for security values.** Tokens, session IDs, salts, OTP
  codes, anything that needs unpredictability uses a cryptographically
  secure RNG — Python's `secrets`, Node's `crypto.randomBytes`, Go's
  `crypto/rand`, etc. Never the general-purpose `random` / `Math.random`
  for security-sensitive values.
- **Strong password hashing.** Bcrypt, Argon2, or scrypt via the
  language's standard hashing library. Never MD5, SHA1, or unsalted
  SHA256/SHA512 for password storage.
- **Constant-time comparison** on tokens, signatures, MACs — Python's
  `hmac.compare_digest`, Node's `crypto.timingSafeEqual`, Go's
  `subtle.ConstantTimeCompare`. Never `==` / `===` / `bytes.Equal`.
- **TLS verification stays on** for every outbound request. Disabling
  cert validation requires an ADR.
- **No hand-rolled primitives.** Use the language's vetted crypto
  library. If the right primitive isn't there, that's a flag — not a
  license to invent.
- **No hardcoded keys, IVs, or seeds** in source. They live in env or a
  secrets manager.

### 1.4 Authorization

- Every API route requires authentication except explicit unauthenticated
  endpoints (health checks, login flows). Never roll custom auth checks
  per-route.
- Authorization belongs in the service layer, not just the router or
  request handler. Service-layer code is reachable from non-HTTP paths
  (workers, SDK re-entrant calls); a router-only check is bypassable.
- Queries that return tenant data scope by organisation/tenant — no
  cross-tenant leaks. Use the tenant context from the auth token,
  never hardcoded IDs.
- All authentication-provider integration goes through a single auth
  module. Never reimplement JWT parsing, OIDC flows, or session handling
  inline.

### 1.5 Audit trail integrity

- **Append-only audit tables.** Tables that record what happened (tool
  runs, agent messages, decision logs) are insert-only after the
  recorded event completes. No `UPDATE` for "cleanup", no `DELETE`
  for "tidying". The audit trail's value is that the row someone
  wrote yesterday is the row that's there today.
- **Immutable version rows.** Tables that represent versioned artifacts
  (tool versions, prompt versions) are never updated after creation.
  Editing creates a new version. Never write a migration or fixup
  that mutates an existing version.
- **Soft deletes on user-facing entities.** Users, tools, and similar
  surface-level records get an `is_active` / `is_deprecated` flag. No
  hard deletes on core entities.
- **System-required rows cannot be deleted by users.** System groups,
  system tenants, and similar required records are enforced
  undeletable in the service layer, not just hidden in the UI.

The per-directory `CLAUDE.md` files enumerate which concrete tables
instantiate each of the above rules.

### 1.6 Configuration safety

- No `DEBUG=True` or verbose error pages in code paths that ship to
  production.
- No default credentials or seeded test users that work in production.
- CORS allowlists do not contain `*` on endpoints that handle authenticated
  requests.
- Session/auth cookies set `Secure`, `HttpOnly`, and `SameSite`.
- Error responses do not leak stack traces, file paths, or library
  versions to clients.
- Services bind to loopback or private interfaces by default. Public
  binds (`0.0.0.0`) on internal-only services need an explicit reason.

---

## Section 2 — Performance & resource discipline

### 2.1 Async hygiene

- Inside an async function, never call blocking I/O — synchronous HTTP
  clients, blocking sleeps, blocking file or subprocess calls. Use the
  async equivalent (`httpx.AsyncClient` for HTTP, `aiofiles` for files,
  `asyncio.create_subprocess_*` for processes) or wrap in the runtime's
  thread offload (`asyncio.to_thread`, equivalents in other ecosystems).
- Independent awaits in a hot loop fan out instead of running serially —
  use the runtime's gather/all primitive (`asyncio.gather`, `Promise.all`,
  `errgroup.Group`).

### 2.2 Database access

- Every list query returning user-visible rows has a row limit and
  pagination. Never load an unbounded result set into memory (no
  `.all()` / equivalent on a potentially large table).
- Loops that read related rows use eager loading from the start — the
  N+1 query is the single most common back-pressure bug. The exact API
  is ORM-specific (SQLAlchemy `selectinload`/`joinedload`, Django
  `select_related`/`prefetch_related`, Sequelize `include`), but the
  principle is constant.
- Index any column the application filters on at scale.

### 2.3 Resource lifetime

- File handles, DB sessions, subprocesses, network sockets are bounded
  by `with` / `try-finally` / explicit `close`. Long-lived processes
  cannot leak handles.
- Caches have an invalidation strategy at write time, not "we'll figure
  it out later."

### 2.4 Hot-path discipline

- `logger.info` / `logger.debug` does not run inside per-request hot
  loops. Use sampling or a metrics counter if you need observability
  there.
- Avoid quadratic-or-worse algorithms over collections whose size is
  user-controlled. If you must, document the input-size assumption in
  the function docstring.

---

## Section 3 — Concurrency

### 3.1 Atomic transitions

- State changes that have a precondition ("only advance if currently in
  state X") use a single atomic `UPDATE … WHERE …` — the
  compare-and-swap (CAS) pattern. Never check-then-update. This is exposed
  as a named helper: `atomic_transition` in `lotsa/db.py`, which writes any
  paired audit row in the same transaction and delegates to the lower-level
  `claim_task_transition` CAS primitive.
- After a CAS, check the return value (`result.won`, `rowcount`) before
  any side effect. The CAS-loser writes nothing.

### 3.2 Symmetric guards

- When two branches of the same operation handle the same race, they
  handle it the same way — both raise, or both return silently. Mixing
  semantics for the same race shape produces confusing UX.

### 3.3 Single-process race guards

- Within one process, `set`-based guards (e.g. `_dispatching_*`) prevent
  re-entrant dispatches between awaits. Add to the set before any await
  that could yield to a competing call; discard in `finally`.

---

## Section 4 — Data integrity

### 4.1 Immutability where promised

The principle is universal; the per-directory `CLAUDE.md` files enumerate
which concrete tables are append-only or version-immutable.

- **Audit/event log tables** — append-only.
- **Versioned artifact tables** — never updated after creation; new
  rows are how you "edit".
- **Run/execution result tables** — updated during execution (status,
  output, timing), immutable after completion.
- **Migration files** — never modified after merge. Schema changes go
  in new migrations.

### 4.2 Type expressiveness

- Values from a closed set (status enums, priority levels, role names)
  carry a closed-set type, not a free string. Python `Literal`,
  TypeScript string-literal unions, Go `type Foo string` constants —
  pick the language's idiom. Typos at the call site fail type-check
  rather than silently persist.
- Schemas for nested structure use typed models, not freeform maps.
  The current Python stack uses Pydantic and dataclasses; the
  frontend uses TypeScript interfaces. If the stack changes the rule
  is "no `dict[str, Any]` / `Record<string, unknown>` for known shapes".

---

## Section 5 — Readability

### 5.1 Comments earn their place

- Comment only what the code cannot say for itself — the *why* (a
  non-obvious constraint, a race, a workaround and its cause), never the
  *what* a reader can already see. If the code is clear, add no comment.
- Keep comments to a line or two. Long comment blocks bury the code they
  describe and drift out of sync with it — they make a file harder to
  read, not easier. When the rationale needs more room, reach for a
  clearer name or a smaller function first; put genuinely long-form
  context in an ADR or the relevant `CLAUDE.md`, not inline.

---

## When this file should be amended

Add a rule when:
- A class of bug (not a single instance) has been fixed reactively in
  review or production. The constitution captures the rule so the next
  agent doesn't relearn it.
- A new architectural decision establishes a non-negotiable invariant.
- A regulatory or compliance requirement attaches to the codebase.

Don't add:
- Style preferences (Ruff handles those).
- Conventions that are "nice to have" rather than non-negotiable.
- Area-specific implementation details — those go in the relevant
  `CLAUDE.md` instead.
- Project-specific anti-patterns derived from a single PR's review —
  those go in the project's review checklist instead.

When in doubt: the constitution is for "never do X" in language-agnostic
terms. Conventions, area specifics, and preferences live elsewhere.
