# Privacy Policy

**Effective date:** 2026-07-13 · **Last updated:** 2026-07-13

Lotsa is open-source software that you download and run yourself — there
is no Lotsa server, no account, and no company standing between you and
your code. This policy describes exactly what a running instance of Lotsa
does and does not send over the network.

## The short version

- No accounts, no telemetry, no analytics, no crash reporting, no
  phone-home update checks.
- Lotsa doesn't collect or transmit any data on its own initiative — the
  only network calls it makes are the ones your configuration tells it to
  make.
- Everything Lotsa stores (tasks, messages, git worktrees) lives in your
  own `--data-dir` (default `~/.lotsa`), on your own machine, under your
  own control.

## What Lotsa sends over the network

Only two kinds of outbound call exist, and both require you to have
configured them:

1. **The LLM provider you configure.** By default this is the Anthropic
   API (`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`), reached via the
   Claude Code CLI. If you set `ANTHROPIC_BASE_URL` to a different
   endpoint (including a self-hosted proxy), calls go there instead.
   Whatever endpoint you configure receives the task description,
   relevant code, and conversation history needed to do the work — the
   same information you'd give a human contractor working on the same
   task. That provider's own privacy policy governs what happens to that
   data on their end; Lotsa has no visibility into it and asks for none.
2. **GitHub**, only if you set `GITHUB_TOKEN` and use the push/PR
   features. Lotsa pushes branches and opens/updates pull requests on
   repositories you've configured under `projects:`. GitHub's own privacy
   policy governs that data.

There is no third outbound call. No analytics SDK, no error-reporting
service, no update-check ping, nothing else.

## What Lotsa stores locally

- **`lotsa.db`** — a local SQLite file holding tasks, messages, and the
  audit trail of what the orchestrator did and when.
- **Worktrees** — per-task git checkouts of the repos you've registered,
  used to dispatch the coding agent.
- **`lotsa.yaml`** — your configuration. Secrets like API keys are read
  from environment variables, not written into this file.

All of it lives under your `--data-dir` and never leaves your machine
except via the two outbound calls above.

## Third parties

The only third parties involved are the ones **you** choose to configure:
your LLM provider (Anthropic by default, or whichever `ANTHROPIC_BASE_URL`
you point at), and GitHub if you enable push/PR features. Lotsa introduces
no third party of its own.

## Cookies and browser storage

The dashboard is a local web UI served at `127.0.0.1` by default. It sets
no tracking cookies. It stores small UI preferences (like your last-used
project and color theme) in your browser's `localStorage`, which never
leaves your device.

## Children

Lotsa is developer tooling and isn't directed at children.

## Changes

This policy lives in the repository at
[`PRIVACY.md`](https://github.com/lotsadev/lotsa/blob/main/PRIVACY.md) —
check its git history for changes, or the date above for the latest
revision.

## Contact

Questions or concerns: open an issue at
[github.com/lotsadev/lotsa/issues](https://github.com/lotsadev/lotsa/issues).
