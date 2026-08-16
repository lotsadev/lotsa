import { describe, expect, it } from 'vitest'

// Tailwind v3 accepted a bare CSS variable inside an arbitrary value —
// `w-[--sidebar-width]` compiled to `width: var(--sidebar-width)`. Tailwind v4
// removed that shorthand in favour of `w-(--sidebar-width)`, and — this is the
// dangerous part — it fails *silently*: the utility emits no rule at all, so the
// element falls back to `width: auto` with no build error and no console warning.
//
// This bit us for real. `components/ui/sidebar.tsx` was vendored from shadcn at
// the v3 syntax while this project is on Tailwind v4, so every
// `w-[--sidebar-width]` was dead. It stayed invisible only because
// `app-layout.tsx` pinned an explicit `w-[18rem]` on the desktop sidebar. When
// the collapsible-panels change swapped `collapsible="none"` for
// `collapsible="offcanvas"` and dropped that literal width in favour of the
// `--sidebar-width` custom property, the dead utilities became load-bearing: the
// fixed rail shrank to fit its content (≈double the intended width) while the
// offcanvas slide — `left: calc(var(--sidebar-width) * -1)`, which uses a real
// `var()` and therefore *did* compile — still moved it by exactly 18rem, so
// collapsing only hid part of the panel.
//
// A rendering test can't catch this (jsdom never runs the Tailwind compiler), so
// the guard is at the source level and covers the whole class rather than the
// one utility that happened to break.

// Vite's glob import reads the sources as strings at transform time, which keeps
// this test free of Node built-ins (the frontend has no `@types/node`).
const SOURCES = import.meta.glob('/src/**/*.{ts,tsx}', {
  query: '?raw',
  eager: true,
  import: 'default',
}) as Record<string, string>

// Matches an arbitrary-value utility whose value is a bare custom property:
// `w-[--sidebar-width]`, `max-w-[--skeleton-width]`. Requiring the closing
// bracket immediately after the identifier keeps two *valid* v4 forms out:
// a `var()`-wrapped value (`left-[calc(var(--x)*-1)]`) and a call to a v4
// theme function (`gap-[--spacing(var(--gap))]`), which both still compile.
const BARE_VAR_ARBITRARY_VALUE = /[a-z0-9]-\[--[a-z][a-z0-9-]*\]/gi

describe('Tailwind v4 arbitrary-value syntax', () => {
  it('has no bare-CSS-variable arbitrary values, which v4 drops silently', () => {
    const offenders: string[] = []

    for (const [path, contents] of Object.entries(SOURCES)) {
      // This file documents the broken syntax in prose and in the matcher, so
      // it would otherwise flag itself.
      if (path.endsWith('tailwind-syntax.test.ts')) continue

      contents.split('\n').forEach((line, index) => {
        for (const match of line.matchAll(BARE_VAR_ARBITRARY_VALUE)) {
          const fixed = match[0].replace('-[--', '-(--').replace(/\]$/, ')')
          offenders.push(
            `${path}:${index + 1} — ${match[0]} silently emits nothing; use ${fixed}`
          )
        }
      })
    }

    expect(offenders).toEqual([])
  })
})
