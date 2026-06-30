import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// Spec (mobile-first redesign, AC#3): the persistent peek bar sits above the
// chat input on mobile, summarises the task's current status (step and/or
// artifact count), and taps to open the right-panel bottom sheet. It is a
// pure presentational component — it receives the already-fetched
// ``TaskDetailFull`` as a prop and calls ``onOpen`` when clicked, so it needs
// no data fetching and no ``matchMedia`` mock.
import { PeekBar } from '@/components/layout/peek-bar'
import type { TaskDetail, TaskDetailFull } from '@/api/types'

function makeTask(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: 'abc123',
    title: 'Mobile-first dashboard redesign',
    state: 'coding',
    priority: 0,
    created_at: '2020-01-15T08:30:00.000Z',
    status: 'working',
    current_step: 'planning',
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

function makeData(
  task: Partial<TaskDetail> = {},
  artifacts: Record<string, string> = {},
): TaskDetailFull {
  return {
    task: makeTask(task),
    messages: [],
    question: null,
    flow: null,
    artifacts,
    next_step_name: null,
    totals: {
      total_duration_s: 0,
      total_tokens: 0,
      total_cost_usd: 0,
      display: '',
    },
    available_overrides: [],
  }
}

describe('PeekBar — status summary', () => {
  it('surfaces the task current step', () => {
    render(<PeekBar data={makeData({ current_step: 'planning' })} onOpen={() => {}} />)
    expect(screen.getByText('planning')).toBeInTheDocument()
  })

  it('falls back to a generic label when there is no current step', () => {
    render(<PeekBar data={makeData({ current_step: null })} onOpen={() => {}} />)
    expect(screen.getByText('Task panel')).toBeInTheDocument()
  })

  it('shows the artifact count (plural) when several artifacts exist', () => {
    const data = makeData({}, { spec: '...', plan: '...' })
    render(<PeekBar data={data} onOpen={() => {}} />)
    expect(screen.getByText('2 artifacts')).toBeInTheDocument()
  })

  it('shows the artifact count in the singular when exactly one exists', () => {
    const data = makeData({}, { spec: '...' })
    render(<PeekBar data={data} onOpen={() => {}} />)
    expect(screen.getByText('1 artifact')).toBeInTheDocument()
  })

  it('shows no artifact count when there are no artifacts', () => {
    render(<PeekBar data={makeData({}, {})} onOpen={() => {}} />)
    // No "N artifact(s)" status — the peek bar still renders, just without a count.
    expect(screen.queryByText(/\d+ artifact/)).toBeNull()
  })
})

describe('PeekBar — open gesture', () => {
  it('renders as a single button control', () => {
    render(<PeekBar data={makeData()} onOpen={() => {}} />)
    expect(screen.getByRole('button')).toBeInTheDocument()
  })

  it('calls onOpen when tapped', () => {
    const onOpen = vi.fn()
    render(<PeekBar data={makeData()} onOpen={onOpen} />)

    fireEvent.click(screen.getByRole('button'))

    expect(onOpen).toHaveBeenCalledTimes(1)
  })
})
