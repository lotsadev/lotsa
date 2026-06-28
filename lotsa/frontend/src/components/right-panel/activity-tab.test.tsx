import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AgentActivity, AgentActivityEvent } from '@/api/types'

// useTask drives the poll cadence (2s while the task is 'working'); pin it so
// the Activity tab polls on the fast interval throughout the test.
vi.mock('@/hooks/use-task', () => ({
  useTask: () => ({ data: { task: { status: 'working' } } }),
}))

vi.mock('@/api/tasks', () => ({
  fetchAgentActivity: vi.fn(),
}))

import { ActivityTab } from './activity-tab'
import { fetchAgentActivity } from '@/api/tasks'

const mockFetch = vi.mocked(fetchAgentActivity)

function ev(index: number, summary: string): AgentActivityEvent {
  return {
    index,
    timestamp: '2026-06-14T11:00:00.000Z',
    kind: 'text',
    summary,
    detail: null,
    truncated: false,
  }
}

// A fake "session JSONL server": holds the *current* session and answers each
// poll the way the real endpoint does — events filtered to index >= since,
// next_index just past the last returned event (or `since` when none match).
// `session_id` always reflects the task's current session, so flipping it
// models a retry assigning a brand-new session_id to the same task.
let session: { id: string; events: AgentActivityEvent[] }

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ActivityTab taskId="task-1" active={true} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mockFetch.mockImplementation(async (_taskId: string, since = 0): Promise<AgentActivity> => {
    const slice = session.events.filter((e) => e.index >= since)
    return {
      session_id: session.id,
      runner_supports_activity: true,
      session_complete: false,
      events: slice,
      next_index: slice.length ? slice[slice.length - 1].index + 1 : since,
    }
  })
})

afterEach(() => {
  cleanup()
  mockFetch.mockReset()
})

describe('ActivityTab — session lifecycle', () => {
  it('resets the accumulator and cursor when a retry assigns a new session_id', async () => {
    // Session A accrues three events; the client cursor advances to index 3.
    session = { id: 'sess-A', events: [ev(0, 'A-zero'), ev(1, 'A-one'), ev(2, 'A-two')] }
    const { container } = renderTab()
    await waitFor(() => expect(container.textContent ?? '').toContain('A-two'))

    // The task is retried: a NEW session_id, whose JSONL starts back at index 0
    // and (so far) has fewer events than the stale cursor (3). The endpoint
    // returns only index >= 3 for the old cursor, so without a reset the new
    // session's first two events are filtered out forever and the tab keeps
    // showing the dead session A.
    session = { id: 'sess-B', events: [ev(0, 'B-zero'), ev(1, 'B-one')] }

    await waitFor(
      () => {
        const text = container.textContent ?? ''
        expect(text).toContain('B-zero')
        expect(text).toContain('B-one')
      },
      { timeout: 8000 },
    )

    // The prior session's events are gone — not interleaved with the new run.
    const text = container.textContent ?? ''
    expect(text).not.toContain('A-zero')
    expect(text).not.toContain('A-two')
  }, 12000)

  it('still accumulates incrementally within a single session', async () => {
    session = { id: 'sess-A', events: [ev(0, 'first')] }
    const { container } = renderTab()
    await waitFor(() => expect(container.textContent ?? '').toContain('first'))

    // Same session, more events appended — the next poll (cursor at 1) fetches
    // only the new event and appends it without dropping the earlier one.
    session = { id: 'sess-A', events: [ev(0, 'first'), ev(1, 'second')] }
    await waitFor(
      () => {
        const text = container.textContent ?? ''
        expect(text).toContain('first')
        expect(text).toContain('second')
      },
      { timeout: 8000 },
    )
  }, 12000)
})
