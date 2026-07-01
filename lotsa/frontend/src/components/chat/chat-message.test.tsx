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
