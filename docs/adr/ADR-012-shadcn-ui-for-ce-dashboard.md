# ADR-012: shadcn/ui for Community Edition Dashboard

**Status**: Implemented
**Date**: 2026-04-23
**Implementation**: PR #46 — *"feat: rewrite CE dashboard as React SPA"*
(commit `2d5dad4`). Brought shadcn/ui in as the component library
alongside the React + Vite + Tailwind v4 stack; the dashboard has
shipped on shadcn since. Subsequent frontend PRs (chat-centric UI
refactor, sidebar, archive function, new task button fixes, etc.)
all build on the same component foundation.
**Related**: CLAUDE.md (frontend conventions)

---

## Context

The Community Edition web dashboard was rewritten from HTMX/Jinja2 to a React SPA to solve fundamental limitations (markdown CPU, input state loss, scroll fights, polling rigidity). The new SPA needs UI primitives — buttons, inputs, tabs, resizable panels, etc.

CLAUDE.md states: "don't introduce shadcn, MUI, Chakra, or similar without flagging it first." This policy was written for a frontend with an existing custom design system. The Lotsa dashboard is a separate green-field frontend with no prior design system.

## Decision

Use **shadcn/ui** for the CE dashboard (`lotsa/frontend/`).

shadcn/ui is not a runtime dependency — components are copied into the project as source files (`src/components/ui/`) and customized directly. This means:

- No npm package to version or update
- Full control over every component
- No risk of upstream breaking changes

## Tradeoffs

**Pros:**
- Accessible primitives built on Radix (keyboard nav, ARIA, focus management)
- Tailwind-native — integrates with our existing styling approach
- Components are owned source code, not a dependency
- Self-hostable: zero external CDN calls, all assets local

**Cons:**
- ~20 component files added to the repo (~750 lines for sidebar alone)
- Coupling to Radix primitives (but these are stable, well-maintained)

## Scope

This decision applies to the Lotsa dashboard (`lotsa/frontend/`).

## Self-Hostability

Confirmed: all shadcn/ui dependencies are npm packages bundled at build time. No runtime external calls. Fonts are self-hosted. Fully compatible with air-gapped deployments.
