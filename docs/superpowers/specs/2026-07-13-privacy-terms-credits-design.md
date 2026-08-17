# Privacy Policy, Terms of Use, and an in-app Info menu

**Date:** 2026-07-13
**Status:** Approved (design), pending implementation plan

## Context

Lotsa CE has no Privacy Policy, Terms of Use, or in-app credits affordance.
Two other projects by the same author (Runepad, Retrotime — both hosted
SaaS) have mature patterns for this: a passive point-of-action notice, full
`/privacy` and `/terms` pages, and a footer/menu credit link. This spec
adapts that pattern for Lotsa, which is materially different: self-hosted
OSS with no server, no account, and no company — the "privacy policy" is
really "what does this software send over the network," and the "terms"
need to cover the risk of an AI agent having read/write access to the
user's own git repositories, rather than GDPR-style hosted-data-controller
language.

This builds on the "Built by Andrew Crookston" backlink already shipped in
`README.md` and `empty-state.tsx` in an earlier change on this branch.

## Goals

- A Privacy Policy and Terms of Use, written for Lotsa's actual shape
  (local software, agent has repo write access, no telemetry).
- Both served in-app at `/privacy` and `/terms` by the FastAPI backend,
  not just committed as repo docs nobody runs into.
- A small, always-available "Info" menu in the dashboard header linking to
  Privacy, Terms, and Credits — the discoverable, non-intrusive placement
  the user asked for ("small menu-button somewhere").
- A passive, non-dismissible notice on the empty-state screen (no banner
  chrome, no persistent workspace overlay — consistent with the existing
  credit-link constraint: loud on marketing surfaces, silent on the
  working UI).
- No new npm dependency (Radix's `DropdownMenu` is already available via
  the bundled `radix-ui` package). One new *concept* — pre-rendered static
  HTML — but no new Python runtime dependency either.

## Non-goals

- No cookie-consent banner or GDPR data-subject-request tooling — Lotsa
  collects no personal data of its own, so there's nothing to consent to
  or request deletion of.
- No dismissible/stateful banner (localStorage-backed consent banner à la
  Runepad's `ConsentBanner`) — explicitly decided against in favor of a
  passive line.
- Does not touch the enterprise version of Lotsa (out of scope, separate
  codebase).

## Design

### 1. Content: `PRIVACY.md` and `TERMS.md`

Live at the **repo root** (canonical, hand-edited, GitHub-browsable — same
convention as Runepad/Retrotime). Full text below; this is the actual
content to ship, not a placeholder.

Key adaptations from the Runepad/Retrotime reference docs:

- No named company or GDPR data-controller framing — Lotsa has no server
  that ever sees the user's data, so that framing doesn't apply. Personal
  attribution to Andrew Crookston only, per the same framing already used
  for the credit link.
- Contact channel is GitHub Issues (`github.com/lotsadev/lotsa/issues`),
  consistent with the README's existing "Issues and pull requests are
  welcome" contributing note — no new support-email channel introduced.
- Terms include a new section absent from Runepad/Retrotime entirely: the
  AI-agent-has-write-access-to-your-repo risk (unwanted commits, lost
  work, PRs opened before review) — this is the core risk specific to
  Lotsa's shape and the reason the user asked for a "loss of property"
  disclaimer.
- Terms explicitly note they *supplement*, not replace or narrow, the
  Apache-2.0 license's existing warranty disclaimer (§7) and liability
  limitation (§8) — Lotsa's terms are about the risk of *running* the
  software against your own repos, not a substitute license.
- Governing law: Swedish law / Swedish courts, matching Runepad/Retrotime,
  per explicit user confirmation.

#### `PRIVACY.md` (full text)

```markdown
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
```

#### `TERMS.md` (full text)

```markdown
# Terms of Use

**Effective date:** 2026-07-13 · **Last updated:** 2026-07-13

These terms govern your use of Lotsa, open-source software available at
[github.com/lotsadev/lotsa](https://github.com/lotsadev/lotsa) (the
"Software"). By running or using the Software, you agree to them. If you
don't agree, don't use it.

The Software is authored and maintained by Andrew Crookston. It is
licensed to you under the [Apache License 2.0](LICENSE); these terms are
additional usage terms about running an AI agent against your own code —
they don't replace or narrow the license.

## 1. What Lotsa is

Lotsa is a local task runner and dashboard that dispatches an AI coding
agent (Claude Code, or another provider you configure) against git
repositories you register, following a process you define. It runs
entirely on your own machine or infrastructure. There is no hosted
service, no account, and no central server operated by the author.

## 2. Acceptable use

Use Lotsa only for lawful purposes, and only against repositories you own
or have permission to modify. You must not use it to create or distribute
illegal content, or to attempt to disrupt or gain unauthorized access to
systems you don't control.

## 3. Your code and content

Your code, tasks, and everything Lotsa produces while working on them
remain entirely yours. Neither the Software nor its author claims any
rights in your code, collects it, or uses it for any purpose other than
running the task you gave it, on your own machine.

## 4. Agent access and risk — read this before pointing Lotsa at a repo you care about

Running Lotsa means granting an AI coding agent read/write access to the
repositories you register. Within its worktree, the agent can:

- read, create, edit, and delete files;
- commit changes;
- push branches and open or update pull requests (when GitHub push/PR
  features are enabled).

This is the core of what Lotsa does, and it carries real risk. A bug in
Lotsa, a mistake by the underlying model, a misconfigured process, or
simple misuse can result in **unwanted file changes, lost or overwritten
work, unintended commits, or unintended pull requests**. Lotsa's design
(isolated worktrees, orchestrator-owned git state, PR-based review)
reduces this risk but cannot eliminate it.

**You are responsible for:**

- reviewing agent output and PRs before merging;
- keeping your own backups and using version control as a safety net;
- never pointing Lotsa at a repository, branch, or credential you can't
  afford to have modified;
- understanding that a task, once dispatched, can commit and push before
  you've reviewed every line.

## 5. Costs

Running Lotsa dispatches calls to whichever LLM provider (and, optionally,
GitHub API) you've configured. **You are solely responsible for any usage
costs or charges those services bill you.** Lotsa's `--budget` setting is
a soft cap enforced on a best-effort basis by the runner in use — it is
not a guarantee against unexpected charges, and not every runner shape
enforces it (see the README's Agent runners section).

## 6. No warranty

The Software is provided **"as is"**, without warranty of any kind,
express or implied, to the fullest extent permitted by law — echoing (and
not replacing) the warranty disclaimer already in the Apache License 2.0,
§7. Lotsa does not warrant that it will be error-free, secure, or fit for
any particular purpose.

## 7. Limitation of liability

To the fullest extent the law allows, neither the Software's author nor
its contributors are liable for any loss or damage arising from your use
of it — including lost, corrupted, or unintended changes to your code,
unintended commits or pull requests, or costs incurred from services you
configured. This is free software; to the extent liability cannot be
excluded, it is limited to zero. This section supplements, and does not
narrow, the liability limitation already in the Apache License 2.0, §8.

## 8. Changes

These terms may be updated from time to time. The current version always
lives at [`TERMS.md`](https://github.com/lotsadev/lotsa/blob/main/TERMS.md)
in the repository, with the date above. Continued use after a change means
you accept it.

## 9. Governing law

These terms are governed by the laws of Sweden. Any dispute arising from
them is subject to the exclusive jurisdiction of the Swedish courts, to
the extent mandatory consumer-protection law in your own country of
residence doesn't provide otherwise.

## 10. Contact

Questions about these terms: open an issue at
[github.com/lotsadev/lotsa/issues](https://github.com/lotsadev/lotsa/issues).
```

### 2. Rendering: pre-rendered static HTML, committed

- The two `.md` files above are hand-rendered to `.html` **once, at
  authoring time** (not at request time) — no Python markdown library
  needed as a runtime dependency, and no new top-level dependency to flag.
- Rendered output is committed at
  `lotsa/server/static/legal/privacy.html` and
  `lotsa/server/static/legal/terms.html`. Unlike
  `lotsa/server/static/dist/` (gitignored build output), this directory
  **is** tracked in git — it's hand-authored content, not a build
  artifact, and it must ship inside the wheel. Because only
  `lotsa/server/static/dist/` is gitignored (confirmed via
  `.gitignore:21`) and the wheel packages the whole `lotsa` package
  (`pyproject.toml`'s `packages = ["lotsa", "rigg"]`), a plain tracked
  file under `lotsa/server/static/legal/` ships automatically with no
  changes to `hatch_build.py` or the `artifacts` glob.
- Each rendered page is a minimal standalone HTML document: inline CSS
  (light/dark via `prefers-color-scheme`, no dependency on the dashboard's
  Tailwind build), a small header with "← Back to Lotsa" (links to `/`)
  and a cross-link to the sibling doc (Privacy ↔ Terms), and the rendered
  markdown body styled simply (headings, paragraphs, lists, links, `hr`).
- If the source `.md` is ever edited, the corresponding `.html` must be
  re-rendered and re-committed in the same PR — this spec doesn't wire up
  an automated build step for it (out of scope; two static docs don't
  justify a doctoc-style pipeline).

### 3. Backend routes

`lotsa/server/app.py` gains two routes, following the existing
`/favicon.svg` pattern exactly (a conditional-existence check + a
`FileResponse`, both defined inside `create_app`):

```python
legal_dir = _STATIC_DIR / "legal"
for name, path in (("privacy", legal_dir / "privacy.html"), ("terms", legal_dir / "terms.html")):
    if path.exists():
        @app.get(f"/{name}", include_in_schema=False)
        async def _legal_page(path=path):
            return FileResponse(str(path), media_type="text/html")
```

(Implementation detail — the plan can write this as two explicit route
functions instead of a loop if that reads more clearly against the
existing favicon-route style; either is fine, no test depends on the
loop shape.)

### 4. Frontend: Info menu in the header

- New `lotsa/frontend/src/components/ui/dropdown-menu.tsx` — a
  shadcn-style wrapper around Radix's `DropdownMenu` primitives (from the
  `radix-ui` package already in `package.json`), following the same
  forwardRef + `cn()` wrapping convention as the existing `dialog.tsx`.
  Only the parts actually used are wrapped: `DropdownMenu`,
  `DropdownMenuTrigger`, `DropdownMenuContent`, `DropdownMenuItem`.
- New `lotsa/frontend/src/components/layout/info-menu.tsx` — an
  `InfoMenu` component: an icon-only `Button` (ghost variant, matching
  `ThemeToggle`'s sizing) using the `Info` icon from `lucide-react`
  (already a dependency), opening a `DropdownMenu` with three items:
  - **Privacy Policy** → `<a href="/privacy" target="_blank" rel="noopener noreferrer">`
  - **Terms of Use** → `<a href="/terms" target="_blank" rel="noopener noreferrer">`
  - **Credits** → `<a href="https://andrewcrookston.com/?ref=lotsa" target="_blank" rel="noopener noreferrer">Built by Andrew Crookston</a>` — identical URL and wording to the existing empty-state credit line.
- Wired into `app-layout.tsx`: rendered immediately to the left of
  `<ThemeToggle />` in both `DesktopShell`'s header and
  `MobileShellInner`'s header, so it's visible on every screen size in
  the same place, always — this is the "small menu-button somewhere"
  placement decided during brainstorming.

### 5. Passive notice on the empty state

In `empty-state.tsx`, one new line is added directly **above** the
existing "Built by Andrew Crookston" paragraph:

```tsx
<p className="text-xs text-muted-foreground">
  By using Lotsa, you agree to the{' '}
  <a href="/terms" target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-foreground">
    Terms of Use
  </a>{' '}
  and{' '}
  <a href="/privacy" target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-foreground">
    Privacy Policy
  </a>.
</p>
```

No dismiss state, no `localStorage`, no banner container — same muted
`text-xs` treatment as the credit line beneath it, and gone once a task is
selected (the `EmptyState` component unmounts).

### Error handling

- If a rendered HTML file is somehow missing (e.g. a from-source checkout
  that dropped the committed file), the route is simply not registered
  (same conditional-mount pattern the favicon route already uses) — the
  dashboard itself must never fail to start because a legal page is
  missing. Visiting `/privacy` or `/terms` in that case 404s normally
  (FastAPI's default), rather than the app crashing.
- The frontend places no runtime dependency on these routes existing —
  the Info menu and empty-state links are plain `<a>` tags, not
  data-fetched, so there is nothing to fail if the network is down; a
  broken link just doesn't load its target tab.

### Testing

- **Backend** (new `lotsa/tests/test_server_app.py`, since no test file
  covers `app.py` today): a `TestClient` hitting `GET /privacy` and
  `GET /terms`, asserting `200`, `content-type` starts with `text/html`,
  and the body contains an expected substring (e.g. `"Privacy Policy"` /
  `"Terms of Use"`) — enough to catch "route not registered" or "wrong
  file served" regressions without pinning exact HTML.
- **Frontend**: extend or add to existing component tests —
  `info-menu.test.tsx` asserting the three menu items render with the
  correct `href`s once the trigger is clicked; an assertion added to
  `empty-state.test.tsx` (or a new small test) that the new terms/privacy
  paragraph renders with the correct links. Follow the existing
  `@testing-library/react` + `vitest` conventions already used in
  `empty-state.test.tsx`.
- **Manual verification**: `npm run build` (frontend) + Python import
  sanity, then run `lotsa serve` and click through the Info menu and the
  empty-state links in a real browser, confirming both `/privacy` and
  `/terms` render correctly in light and dark mode.

## Files touched

- New: `PRIVACY.md`, `TERMS.md` (repo root)
- New: `lotsa/server/static/legal/privacy.html`,
  `lotsa/server/static/legal/terms.html`
- Modified: `lotsa/server/app.py` (two new routes)
- New: `lotsa/tests/test_server_app.py`
- New: `lotsa/frontend/src/components/ui/dropdown-menu.tsx`
- New: `lotsa/frontend/src/components/layout/info-menu.tsx`
- New: `lotsa/frontend/src/components/layout/info-menu.test.tsx`
- Modified: `lotsa/frontend/src/components/layout/app-layout.tsx` (mount
  `InfoMenu` next to `ThemeToggle` in both shells)
- Modified: `lotsa/frontend/src/components/empty-state.tsx` (add the
  passive terms/privacy line)
- Modified: `lotsa/frontend/src/components/empty-state.test.tsx` (or a new
  test) to cover the new paragraph
