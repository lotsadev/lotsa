import { describe, it, expect } from 'vitest'
import type { Message } from '@/api/types'
import { collapseNeedsInputMessages } from './needs-input'

// Spec R2 — when an agent pauses for input the operator must see EXACTLY ONE
// bubble. The orchestrator persists two rows for a non-conversational
// NEEDS_INPUT pause: an ``agent``/``output`` row with the full stdout (marker
// line included) and an ``agent``/``question`` row with just the extracted
// question text (a subset of the output). Because ``messages`` is append-only,
// the duplicate is collapsed at RENDER time: ``collapseNeedsInputMessages``
// drops the redundant ``question`` row when it duplicates (equals, or is a
// subset of) an adjacent same-step agent ``chat``/``output`` bubble, and marks
// that surviving bubble ``awaitingInput`` so it renders with the amber
// needs-input accent. pr-fix NEEDS_DECISION is non-conversational, so it also
// persists a full-stdout ``output`` row before its ``question`` row and
// collapses the same way. A ``question`` with no covering bubble is a defensive
// fallback that renders on its own as a single bubble.

let nextId = 1
function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: nextId++,
    task_id: 'task1',
    role: 'agent',
    step_name: 'plan',
    content: '',
    type: 'chat',
    metadata: {},
    created_at: '2020-01-15T08:30:00.000Z',
    ...overrides,
  }
}

describe('collapseNeedsInputMessages', () => {
  it('collapses a non-conversational NEEDS_INPUT duplicate into one awaiting bubble', () => {
    const output = makeMessage({
      type: 'output',
      step_name: 'plan',
      content: 'Here is my analysis.\n\nNEEDS_INPUT: Should I use option A or B?',
    })
    const question = makeMessage({
      type: 'question',
      step_name: 'plan',
      content: 'Should I use option A or B?',
    })

    const items = collapseNeedsInputMessages([output, question])

    // The redundant question row is dropped — only the agent's message remains.
    expect(items).toHaveLength(1)
    expect(items[0].message.id).toBe(output.id)
    // The surviving bubble is flagged so it renders the amber needs-input accent.
    expect(items[0].awaitingInput).toBe(true)
  })

  it('collapses when the question equals the agent chat content exactly', () => {
    // Conversational shape: an ``agent``/``chat`` row plus an equal
    // ``agent``/``question`` row from the same step.
    const chat = makeMessage({
      type: 'chat',
      step_name: 'spec',
      content: 'Which database should I target?',
    })
    const question = makeMessage({
      type: 'question',
      step_name: 'spec',
      content: 'Which database should I target?',
    })

    const items = collapseNeedsInputMessages([chat, question])

    expect(items).toHaveLength(1)
    expect(items[0].message.type).toBe('chat')
    expect(items[0].awaitingInput).toBe(true)
  })

  it('keeps the agent chat/output message and only ever drops the question row', () => {
    const output = makeMessage({
      type: 'output',
      step_name: 'plan',
      content: 'Draft plan.\n\nNEEDS_INPUT: proceed?',
    })
    const question = makeMessage({ type: 'question', step_name: 'plan', content: 'proceed?' })

    const items = collapseNeedsInputMessages([output, question])

    // The agent's message survives; the question is the row that disappears.
    expect(items.some((i) => i.message.id === output.id)).toBe(true)
    expect(items.some((i) => i.message.id === question.id)).toBe(false)
  })

  it('does NOT collapse a question from a different step', () => {
    // Content is a subset, but the step differs — must not collapse across steps.
    const output = makeMessage({
      type: 'output',
      step_name: 'plan',
      content: 'Planning done. NEEDS_INPUT: choose A or B',
    })
    const question = makeMessage({ type: 'question', step_name: 'code', content: 'choose A or B' })

    const items = collapseNeedsInputMessages([output, question])

    expect(items).toHaveLength(2)
    expect(items.some((i) => i.message.id === question.id)).toBe(true)
    const outputItem = items.find((i) => i.message.id === output.id)!
    expect(outputItem.awaitingInput).toBe(false)
  })

  it('collapses a pr-fix NEEDS_DECISION into its output bubble', () => {
    // pr-fix is non-conversational: the orchestrator persists a full-stdout
    // ``output`` row (line 4489 in orchestrator.py) BEFORE the needs_input rule
    // fires and writes the ``question`` row (line 4741), both step_name
    // "pr-fix". The question is extracted verbatim from that same stdout, so it
    // is always a substring of the output — the pair collapses like any other
    // non-conversational NEEDS_INPUT pause, into a single awaiting bubble.
    const output = makeMessage({
      type: 'output',
      step_name: 'pr-fix',
      content:
        'Reviewed the feedback.\n\nPR_FIX_NEEDS_DECISION: The reviewer disagrees with the approach. Proceed anyway?',
    })
    const question = makeMessage({
      type: 'question',
      step_name: 'pr-fix',
      content: 'The reviewer disagrees with the approach. Proceed anyway?',
    })

    const items = collapseNeedsInputMessages([output, question])

    expect(items).toHaveLength(1)
    expect(items[0].message.id).toBe(output.id)
    expect(items[0].awaitingInput).toBe(true)
  })

  it('renders a standalone question with no covering bubble as a single bubble (defensive fallback)', () => {
    // Defensive path: a ``question`` row that arrives with no preceding covering
    // agent bubble still renders exactly once, on its own, with no crash.
    const question = makeMessage({
      type: 'question',
      step_name: 'pr-fix',
      content: 'The reviewer disagrees with the approach. Proceed anyway?',
    })

    const items = collapseNeedsInputMessages([question])

    expect(items).toHaveLength(1)
    expect(items[0].message.id).toBe(question.id)
  })

  it('collapses across intervening hidden rows (stderr, dispatch event)', () => {
    // The orchestrator writes stderr + a status_change dispatch event between
    // the output row and the question row. Those are hidden (rendered null) and
    // must not break needs-input adjacency.
    const output = makeMessage({
      type: 'output',
      step_name: 'plan',
      content: 'Analysis.\n\nNEEDS_INPUT: A or B?',
    })
    const stderr = makeMessage({ type: 'stderr', role: 'agent', step_name: 'plan', content: 'a warning' })
    const event = makeMessage({
      type: 'status_change',
      role: 'system',
      step_name: '',
      content: '{"type":"dispatch"}',
    })
    const question = makeMessage({ type: 'question', step_name: 'plan', content: 'A or B?' })

    const items = collapseNeedsInputMessages([output, stderr, event, question])

    // Hidden rows are preserved in order; the question row is dropped.
    expect(items.map((i) => i.message.id)).toEqual([output.id, stderr.id, event.id])
    const outputItem = items.find((i) => i.message.id === output.id)!
    expect(outputItem.awaitingInput).toBe(true)
  })

  it('does NOT collapse across a turn boundary (an operator message intervenes)', () => {
    // A user message closes the turn — a later question can't collapse into the
    // agent bubble that preceded the answer.
    const output = makeMessage({
      type: 'output',
      step_name: 'plan',
      content: 'Draft.\n\nNEEDS_INPUT: A or B?',
    })
    const userMsg = makeMessage({ role: 'user', type: 'answer', step_name: 'plan', content: 'A' })
    const question = makeMessage({ type: 'question', step_name: 'plan', content: 'A or B?' })

    const items = collapseNeedsInputMessages([output, userMsg, question])

    expect(items).toHaveLength(3)
    const outputItem = items.find((i) => i.message.id === output.id)!
    expect(outputItem.awaitingInput).toBe(false)
  })

  it('passes non-question messages through unchanged, preserving order and ids', () => {
    const a = makeMessage({ role: 'user', type: 'chat', content: 'hello' })
    const b = makeMessage({ role: 'agent', type: 'chat', content: 'hi there' })
    const c = makeMessage({ type: 'stage_transition', role: 'system', content: '→ coding' })

    const items = collapseNeedsInputMessages([a, b, c])

    expect(items.map((i) => i.message.id)).toEqual([a.id, b.id, c.id])
    expect(items.every((i) => i.awaitingInput === false)).toBe(true)
  })
})
