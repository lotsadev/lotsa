import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ChatInput } from './chat-input'
import type { Flow, TaskDetail, TaskDetailFull } from '@/api/types'

// Spec (mobile-first redesign, AC#6): the chat-input action button row
// (Send / Stop / Accept / override / Promote / Retry) must wrap or reflow
// gracefully on narrow screens instead of overflowing a single line. The
// mechanism is a wrapping flex row (``flex-wrap``) on the form, with the
// textarea allowed to shrink (``min-w-0``) so the buttons drop below it on
// narrow widths while staying inline on desktop.

function makeTask(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: 'abc123',
    title: 'A task',
    state: 'coding',
    priority: 0,
    created_at: '2020-01-15T08:30:00.000Z',
    status: 'waiting',
    current_step: 'code',
    is_conversational: false,
    elapsed_s: 0,
    project_id: 'proj1',
    timeout_status: 'ok',
    metadata: {},
    body: '',
    flow_name: 'full',
    work_dir: '/tmp/worktrees/abc123',
    project_name: 'lotsa',
    project_path: '/repos/lotsa',
    ...overrides,
  }
}

const flow: Flow = {
  name: 'full',
  steps: [
    {
      name: 'code',
      conversational: false,
      evaluate: false,
      output: null,
      inputs: [],
      is_gate: false,
    },
  ],
  gate_states: [],
}

function makeData(task: Partial<TaskDetail> = {}): TaskDetailFull {
  return {
    task: makeTask(task),
    messages: [],
    question: null,
    flow,
    artifacts: {},
    next_step_name: null,
    totals: { total_duration_s: 0, total_tokens: 0, total_cost_usd: 0, display: '' },
    available_overrides: [],
  }
}

function renderChatInput(data: TaskDetailFull = makeData()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ChatInput data={data} />
    </QueryClientProvider>,
  )
}

describe('ChatInput action row reflow', () => {
  it('lays the action row out as a wrapping flex row so buttons reflow on narrow screens', () => {
    const { container } = renderChatInput()
    const form = container.querySelector('form')

    expect(form).not.toBeNull()
    // The action row must be allowed to wrap rather than overflow a single line.
    expect(form!.className).toMatch(/flex-wrap/)
  })

  it('lets the textarea shrink (min-w-0) so the buttons can wrap beneath it', () => {
    const { container } = renderChatInput()
    const textarea = container.querySelector('textarea')

    expect(textarea).not.toBeNull()
    expect(textarea!.className).toMatch(/min-w-0/)
  })

  it('still renders the Send control', () => {
    const { getByRole } = renderChatInput()
    expect(getByRole('button', { name: 'Send' })).toBeInTheDocument()
  })
})
