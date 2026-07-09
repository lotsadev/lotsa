import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { ChatMessage } from './chat-message'
import type { Message } from '@/api/types'

// Spec — the task-detail chat bubbles have several rendering defects:
//   R1 — wide content (code blocks, tables, long URLs) overflows the bubble.
//   R2 — an agent pause-for-input is styled as an "awaiting your answer" bubble.
//   R3 — bubble max width is 90% (was 80%).
//   R4 — operator-authored text (incl. type="feedback") always reads as a
//        right-aligned "You" bubble in a distinct colour; genuine system status
//        notes stay centered but width-constrained so they can't overflow.

let nextId = 1
function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: nextId++,
    task_id: 'task1',
    role: 'agent',
    step_name: 'plan',
    content: 'placeholder',
    type: 'chat',
    metadata: {},
    created_at: '2020-01-15T08:30:00.000Z',
    ...overrides,
  }
}

// A pr_decision message (the pr-fix audit row). `pr_decision` is a distinct
// message type the frontend must render as a first-class chat bubble via its
// own branch — not by an incidental fallthrough to the role==='agent' case.
// Built via a cast because the type is only added to the Message union as part
// of this change; this helper carries the runtime value the backend sends.
function prDecisionMessage(overrides: Partial<Message> = {}): Message {
  const msg = makeMessage({ role: 'agent', step_name: 'pr-fix', ...overrides })
  ;(msg as { type: string }).type = 'pr_decision'
  return msg
}

// Scan every rendered element's class attribute (SVG icons expose className as
// an object, so read the attribute string rather than el.className).
function anyClass(container: HTMLElement, re: RegExp): boolean {
  return Array.from(container.querySelectorAll('*')).some((el) =>
    re.test(el.getAttribute('class') ?? ''),
  )
}

describe('ChatMessage — operator "You" bubble (R4)', () => {
  it('renders operator feedback as a right-aligned "You" bubble, not a centered note', () => {
    // type="feedback" is stored with role="user" (approve / revise / pr-fix
    // feedback). role must win over type so it reads as "You".
    const msg = makeMessage({ role: 'user', type: 'feedback', content: 'Approved' })
    const { container, getByText } = render(<ChatMessage message={msg} />)

    expect(getByText('You')).toBeInTheDocument()
    expect(anyClass(container, /justify-end/)).toBe(true)
    // Must NOT fall into the centered muted status-note branch.
    expect(anyClass(container, /text-center/)).toBe(false)
  })

  it('gives the user bubble a distinct primary-tinted colour, not bg-muted', () => {
    const msg = makeMessage({ role: 'user', type: 'chat', content: 'do the thing' })
    const { container } = render(<ChatMessage message={msg} />)

    expect(anyClass(container, /bg-primary/)).toBe(true)
    // Standalone `bg-muted` bubble background only — not the prose
    // `[&_code]:bg-muted` variant (preceded by `:`), which every bubble carries.
    expect(anyClass(container, /(?:^|\s)bg-muted(?:\s|$)/)).toBe(false)
  })
})

describe('ChatMessage — system status note stays compact (R4)', () => {
  it('keeps a system-authored feedback note centered but width-constrained', () => {
    const msg = makeMessage({ role: 'system', type: 'feedback', content: 'x'.repeat(500) })
    const { container } = render(<ChatMessage message={msg} />)

    // A system note is not a "You" bubble...
    expect(anyClass(container, /justify-end/)).toBe(false)
    // ...but it must be width-constrained + wrapping so long text can't overflow.
    expect(anyClass(container, /max-w-/)).toBe(true)
    expect(anyClass(container, /break-words/)).toBe(true)
  })
})

describe('ChatMessage — bubble max width 90% (R3)', () => {
  const bubbleVariants = [
    { label: 'agent output', msg: makeMessage({ role: 'agent', type: 'output', content: 'x' }) },
    { label: 'user chat', msg: makeMessage({ role: 'user', type: 'chat', content: 'x' }) },
    { label: 'question', msg: makeMessage({ role: 'agent', type: 'question', content: 'x' }) },
    { label: 'error', msg: makeMessage({ role: 'agent', type: 'error', content: 'x' }) },
    { label: 'fallback', msg: makeMessage({ role: 'system', type: 'output', content: 'x' }) },
    { label: 'pr_decision', msg: prDecisionMessage({ content: 'reviewer approved' }) },
  ]

  it.each(bubbleVariants)('caps the $label bubble at 90pct width, not 80pct', ({ msg }) => {
    const { container } = render(<ChatMessage message={msg} />)
    expect(anyClass(container, /max-w-\[90%\]/)).toBe(true)
    expect(anyClass(container, /max-w-\[80%\]/)).toBe(false)
  })
})

describe('ChatMessage — awaiting-input accent (R2)', () => {
  it('marks an awaiting agent bubble with the amber needs-input accent', () => {
    const msg = makeMessage({
      role: 'agent',
      type: 'output',
      step_name: 'plan',
      content: 'Analysis complete.\n\nNEEDS_INPUT: A or B?',
    })
    const { container } = render(<ChatMessage message={msg} awaitingInput />)

    expect(anyClass(container, /amber/)).toBe(true)
  })

  it('leaves a plain (non-awaiting) agent bubble neutral, with no amber accent', () => {
    const msg = makeMessage({ role: 'agent', type: 'output', content: 'done' })
    const { container } = render(<ChatMessage message={msg} />)

    expect(anyClass(container, /bg-card/)).toBe(true)
    expect(anyClass(container, /amber/)).toBe(false)
  })
})

describe('ChatMessage — soft line breaks (remark-breaks)', () => {
  // Per CommonMark a single `\n` is a soft break (rendered as a space). The
  // chat expectation is that a single newline shows a visible line break, so
  // both ReactMarkdown calls must include `remark-breaks`, turning `\n` into
  // a <br>. `\n\n` must still separate paragraphs (not collapse into one).

  it('renders a single newline in an agent message as a <br>, not a collapsed space', () => {
    const msg = makeMessage({ role: 'agent', type: 'output', content: 'line one\nline two' })
    const { container } = render(<ChatMessage message={msg} />)

    // With only remark-gfm the newline collapses to whitespace and no <br> exists.
    expect(container.querySelector('br')).not.toBeNull()
  })

  it('renders a single newline in a plain operator/user message as a <br>', () => {
    // Operator/user text flows through the same MarkdownContent path — one
    // render path, fixed once. A single newline must break there too.
    const msg = makeMessage({ role: 'user', type: 'chat', content: 'first\nsecond' })
    const { container } = render(<ChatMessage message={msg} />)

    expect(container.querySelector('br')).not.toBeNull()
  })

  it('breaks single newlines in the large-content preview path too', () => {
    // Large content renders via the *second* ReactMarkdown call (the truncated
    // preview). remark-breaks must be wired into that call as well, so the
    // first preview lines still break on single newlines.
    // Mirrors the component's LARGE_CONTENT_THRESHOLD (10_000 chars); anything
    // larger routes through the truncated-preview render path.
    const content = 'alpha\nbeta\ngamma\n' + 'x'.repeat(10_100)
    const msg = makeMessage({ role: 'agent', type: 'output', content })
    const { container } = render(<ChatMessage message={msg} />)

    // Confirm we're on the preview path (the "Render full content" button shows).
    expect(container.textContent).toMatch(/Render full content/)
    expect(container.querySelector('br')).not.toBeNull()
  })

  it('keeps a blank line as separate paragraphs (does not merge on soft breaks)', () => {
    // Guard: remark-breaks must not collapse a paragraph break — `\n\n` stays
    // two <p> elements. (This holds pre-fix too; it protects against a
    // regression where enabling breaks flattens paragraph structure.)
    const msg = makeMessage({ role: 'agent', type: 'output', content: 'para one\n\npara two' })
    const { container } = render(<ChatMessage message={msg} />)

    expect(container.querySelectorAll('p').length).toBeGreaterThanOrEqual(2)
  })
})

describe('ChatMessage — overflow containment (R1)', () => {
  it('scopes a horizontal scrollbar to code blocks inside the bubble', () => {
    const msg = makeMessage({
      role: 'agent',
      type: 'output',
      content: '```\n' + 'x'.repeat(400) + '\n```',
    })
    const { container } = render(<ChatMessage message={msg} />)

    const prose = container.querySelector('.prose')
    expect(prose).not.toBeNull()
    expect(prose!.getAttribute('class') ?? '').toMatch(/\[&_pre\]:overflow-x-auto/)
  })

  it('wraps long unbroken strings/URLs instead of widening the bubble', () => {
    const msg = makeMessage({
      role: 'agent',
      type: 'output',
      content: 'https://example.com/' + 'a'.repeat(300),
    })
    const { container } = render(<ChatMessage message={msg} />)

    const prose = container.querySelector('.prose')
    expect(prose).not.toBeNull()
    // break-words / overflow-wrap: anywhere lets long tokens wrap.
    expect(prose!.getAttribute('class') ?? '').toMatch(/break-words|overflow-wrap/)
  })

  it('wraps wide tables in a horizontally scrollable container', () => {
    const md = '| col a | col b |\n| --- | --- |\n| 1 | 2 |'
    const msg = makeMessage({ role: 'agent', type: 'output', content: md })
    const { container } = render(<ChatMessage message={msg} />)

    const table = container.querySelector('table')
    expect(table).not.toBeNull()
    // The table's own wrapper scrolls — not the bubble or the chat column.
    expect(table!.parentElement?.getAttribute('class') ?? '').toMatch(/overflow-x-auto/)
  })

  it('lets the bubble shrink (min-w-0) so the width cap is respected', () => {
    const msg = makeMessage({ role: 'agent', type: 'output', content: 'ok' })
    const { container } = render(<ChatMessage message={msg} />)

    expect(anyClass(container, /min-w-0/)).toBe(true)
  })
})

describe('ChatMessage — stage_transition divider cannot overflow', () => {
  // A long stage_transition label (e.g. an unshortened "pr-fix skipped: <long
  // reasoning>") rendered as a `shrink-0 font-mono` span forces the flex row —
  // and the whole window — wider than the viewport, in monospace. The divider
  // must shrink and wrap instead.
  it('wraps a long transition label instead of forcing horizontal scroll', () => {
    const msg = makeMessage({ role: 'system', type: 'stage_transition', content: 'x'.repeat(300) })
    const { container } = render(<ChatMessage message={msg} />)

    // `shrink-0` pins the label to its content width — the overflow source.
    expect(anyClass(container, /shrink-0/)).toBe(false)
    // `min-w-0` lets the flex child shrink below its content size so the label
    // can wrap; `break-words` performs the wrap on a long unbroken string.
    expect(anyClass(container, /min-w-0/)).toBe(true)
    expect(anyClass(container, /break-words/)).toBe(true)
  })
})

describe('ChatMessage — pr_decision is a first-class chat bubble', () => {
  it('renders a pr_decision message as the "pr-fix Agent" chat bubble', () => {
    const msg = prDecisionMessage({ content: 'reviewer approved — nothing actionable' })
    const { container, getByText } = render(<ChatMessage message={msg} />)

    expect(getByText('pr-fix Agent')).toBeInTheDocument()
    // Left-aligned bubble, not the centered stage_transition divider treatment.
    expect(anyClass(container, /justify-start/)).toBe(true)
    expect(anyClass(container, /bg-card/)).toBe(true)
    expect(anyClass(container, /text-center/)).toBe(false)
  })

  it('renders via its own type branch, not an incidental role==="agent" fallthrough', () => {
    // A pr_decision row keyed on type must render as the labelled chat bubble
    // regardless of role. With only the role==='agent' fallthrough, a
    // non-agent role drops to the unlabelled generic fallback (no "pr-fix
    // Agent" label), so this asserts the explicit `type === 'pr_decision'`
    // branch exists.
    const msg = prDecisionMessage({ role: 'system', content: 'reviewer approved' })
    const { getByText } = render(<ChatMessage message={msg} />)

    expect(getByText('pr-fix Agent')).toBeInTheDocument()
  })
})

describe('ChatMessage — readable code blocks under the typography plugin (R5)', () => {
  // The `@tailwindcss/typography` plugin colours `pre`/`pre code` with its
  // light "meant for a dark pre background" `--tw-prose-pre-code`, which is
  // grey-on-grey against the light `bg-muted` we set — and wraps inline `code`
  // in literal backtick quotes. The PROSE_CLASSES overrides must re-assert the
  // readable look. Guard the exact classes so the plugin can't silently win again.
  it('forces fenced code text to the theme foreground (not the plugin pre-code grey)', () => {
    const msg = makeMessage({
      role: 'agent',
      type: 'output',
      content: '```\nconst x = 1\n```',
    })
    const { container } = render(<ChatMessage message={msg} />)

    const prose = container.querySelector('.prose')
    expect(prose).not.toBeNull()
    expect(prose!.getAttribute('class') ?? '').toMatch(/\[&_pre\]:text-foreground/)
  })

  it('strips the plugin-injected backtick quotes around inline code', () => {
    const msg = makeMessage({ role: 'agent', type: 'output', content: 'use `npm run build`' })
    const { container } = render(<ChatMessage message={msg} />)

    const prose = container.querySelector('.prose')
    expect(prose).not.toBeNull()
    const cls = prose!.getAttribute('class') ?? ''
    expect(cls).toMatch(/\[&_code\]:before:content-none/)
    expect(cls).toMatch(/\[&_code\]:after:content-none/)
  })
})
