# Contributing to Lotsa

Thanks for your interest in Lotsa. This guide covers local setup, the checks
your change needs to pass, and the conventions the project follows.

## Prerequisites

- **Python 3.12+**
- **Node.js** (to build the dashboard bundle)
- The **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** (Lotsa dispatches it)

## Local setup

```bash
# Install Lotsa + dev dependencies
pip install -e ".[dev]"

# Build the dashboard (one-time; re-run after changing lotsa/frontend/)
make frontend

# Run the dev server (installs editable + starts `lotsa serve`)
make dev
```

## Checks (run before opening a PR)

```bash
make test        # pytest (lotsa/tests + rigg/tests)
make lint        # ruff check + ruff format --check
make format      # ruff format (apply)
make typecheck   # mypy lotsa/ rigg/  (advisory; not a required gate)
```

CI runs `lint` and `test` on every PR and must pass. Pull requests are also
reviewed by an automated Claude Code reviewer (see
`.github/workflows/claude-code-review.yml`).

## Project layout

```
lotsa/   — the product: CLI, config, SQLite store, dashboard, orchestrator,
           push step, PR monitor.
rigg/    — the internal orchestration SDK (state machine, dispatch engine,
           agent runners, git utilities). You rarely need to touch this.
docs/adr — Architecture Decision Records.
```

Each main directory has its own `CLAUDE.md`. **Read the relevant `CLAUDE.md`
plus [`CONSTITUTION.md`](./CONSTITUTION.md) before changing code in that area** —
they capture the non-negotiable invariants (security, secrets, audit-trail
integrity, atomic state transitions, async hygiene, orchestrator-owned git).

## Conventions

- **One branch per change**; never push directly to `main`.
- **Tests are part of every change.** A regression test should fail against
  the pre-fix code and pass after — include the failing message in the commit
  body when fixing a bug.
- **Keep changes focused.** No drive-by refactors bundled with a fix.
- **Comments earn their place** (CONSTITUTION §5.1): comment the *why*, not the
  *what*, and keep them short.
- **Write an ADR** in `docs/adr/` before a significant architectural decision
  (a protocol-contract change, a new top-level dependency, a new flow primitive).
- **Self-hostable dependency rule**: every dependency must be self-hostable with
  no mandatory outbound calls, or carry a documented alternative (see
  `CLAUDE.md`). New top-level dependencies should be flagged in the PR.

## Reporting bugs and proposing features

Open a GitHub issue describing the behaviour you saw vs. expected, with steps to
reproduce. For security issues, **do not open a public issue** — see
[`SECURITY.md`](./SECURITY.md).
