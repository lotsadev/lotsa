# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Report privately via GitHub's **[Report a vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
(the repository's *Security* tab → *Report a vulnerability*), or email
**security@lotsa.dev**.

Please include: a description of the issue, steps to reproduce or a proof of
concept, the impact you foresee, and any suggested remediation. We aim to
acknowledge reports within a few business days and will keep you updated on
remediation. Please give us a reasonable window to fix the issue before any
public disclosure.

## Supported versions

Lotsa is pre-1.0. Security fixes land on the latest `main` only; there are no
backported release branches yet.

## Security model — what to know before you run Lotsa

Lotsa runs AI coding agents that **read and modify your code and git
repositories** on your own machine. That power carries inherent risk; the
project is designed to bound it:

- **Self-hosted, no telemetry.** State lives in a local SQLite database. There
  is no external database and no usage telemetry. The only outbound calls are to
  the LLM endpoint you configure (Lotsa honours `ANTHROPIC_BASE_URL`).
- **Sandboxed agent execution (ADR-038).** The agent is isolated so it can't
  modify the host outside its task worktree. On **macOS** the native runner uses
  the OS sandbox (Seatbelt) to confine writes to the worktree. On **Linux**
  (where Claude's native sandbox doesn't reliably start) isolation is **Docker**:
  the agent runs inside a container — this is what the single-host deploy uses.
  Running natively without a working sandbox requires an explicit per-launch
  `--dangerously-skip-permissions` opt-out (not recommended); the server refuses
  to start otherwise.
- **Orchestrator-owned git authority.** Agents work inside a per-task git
  worktree and only stage/commit there. Branch creation, pushes, and rebases are
  performed by the deterministic orchestrator — never by an agent (ADR-013).
- **Credential hygiene.** Tokens are supplied via environment variables and a
  `GIT_ASKPASS`-style helper, never via process argv; secrets are never written
  to the database, logs, API responses, or error messages
  (`CONSTITUTION.md` §1.2).
- **Human gates.** Structured processes include approval gates (e.g. plan
  review) so consequential work pauses for an operator.

### Operator responsibilities

- **Treat `lotsa.yaml` as executable, not data.** The optional `tools:`,
  `engines:`, and `runners:` blocks name `pkg.mod:callable` paths that Lotsa
  imports at startup — importing runs that module's code. Only run a config you
  wrote or fully trust; never point Lotsa at a cloned or shared `lotsa.yaml`
  without reading it first.
- Use **least-privilege tokens** — scope `GITHUB_TOKEN` to only the repos Lotsa
  manages.
- The dashboard binds to **`127.0.0.1` by default**; only expose it on a network
  deliberately (`--host`), and put authentication in front of it if you do.
- Review what the agents change, especially before merging — the gates help, but
  you are the final reviewer.

The non-negotiable security rules the codebase is held to live in
[`CONSTITUTION.md`](./CONSTITUTION.md).
