import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StageBar } from './stage-bar'
import type { Flow, TaskDetail, Totals } from '@/api/types'

// created_at is stored as a UTC ISO string in the DB. Use a clearly historical
// value so formatRelativeTime always produces a non-trivial relative label
// (e.g. "6 years ago") regardless of when the suite runs.
const OLD_CREATED_AT = '2020-01-15T08:30:00.000Z'

// Spec: per-task totals (working time, tokens, USD cost) in the task header.
// The data already arrives at the frontend as TaskDetailFull.totals; the
// header (StageBar) is the component that must render it. These tests pin
// the four acceptance criteria for the header rendering:
//   1. A task with activity shows time + tokens + cost.
//   2. The values reflect the data.totals payload.
//   3. After a refresh with larger totals, the header reflects the new sum.
//   4. A brand-new task (no activity) shows no zero-filled totals line.

function makeTask(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: 'abc123',
    title: 'Add per-task totals to the header',
    state: 'coding',
    priority: 0,
    created_at: OLD_CREATED_AT,
    status: 'working',
    current_step: 'code',
    is_conversational: false,
    elapsed_s: 0,
    timeout_status: 'ok',
    metadata: {},
    body: '',
    flow_name: 'full',
    work_dir: '/tmp/worktrees/abc123',
    ...overrides,
  }
}

// A flow with no steps keeps the test focused on the totals line and avoids
// coupling to the step-pipeline rendering.
const emptyFlow: Flow = { name: 'full', steps: [], gate_states: [] }

function renderStageBar(totals: Totals, task: TaskDetail = makeTask()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <StageBar task={task} flow={emptyFlow} totals={totals} />
    </QueryClientProvider>,
  )
}

describe('StageBar per-task totals', () => {
  it('renders working time, total tokens, and total cost when the task has activity', () => {
    const totals: Totals = {
      total_duration_s: 192,
      total_tokens: 45200,
      total_cost_usd: 1.84,
      display: '3m 12s · 45,200 tokens · $1.84',
    }

    const { container } = renderStageBar(totals)
    const text = container.textContent ?? ''

    // Working time, tokens, and cost must all be surfaced in the header.
    expect(text).toContain('3m 12s')
    expect(text).toContain('45,200 tokens')
    expect(text).toContain('$1.84')
  })

  it('reflects the values from the totals payload (cost at 2 decimals)', () => {
    const totals: Totals = {
      total_duration_s: 45,
      total_tokens: 1234,
      total_cost_usd: 0.5,
      display: '45s · 1,234 tokens · $0.50',
    }

    const { container } = renderStageBar(totals)
    const text = container.textContent ?? ''

    expect(text).toContain('1,234 tokens')
    // 2-decimal cost (task-totals format), not the per-message 4-decimal footer.
    expect(text).toContain('$0.50')
    expect(text).not.toContain('$0.5000')
  })

  it('updates the displayed totals when task detail is refetched with larger sums', () => {
    const initial: Totals = {
      total_duration_s: 60,
      total_tokens: 10000,
      total_cost_usd: 0.4,
      display: '1m 0s · 10,000 tokens · $0.40',
    }
    const grown: Totals = {
      total_duration_s: 240,
      total_tokens: 80000,
      total_cost_usd: 2.1,
      display: '4m 0s · 80,000 tokens · $2.10',
    }

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const task = makeTask()
    const { container, rerender } = render(
      <QueryClientProvider client={queryClient}>
        <StageBar task={task} flow={emptyFlow} totals={initial} />
      </QueryClientProvider>,
    )
    expect(container.textContent ?? '').toContain('10,000 tokens')

    rerender(
      <QueryClientProvider client={queryClient}>
        <StageBar task={task} flow={emptyFlow} totals={grown} />
      </QueryClientProvider>,
    )
    const text = container.textContent ?? ''
    expect(text).toContain('80,000 tokens')
    expect(text).toContain('$2.10')
    expect(text).not.toContain('10,000 tokens')
  })

  it('omits the totals line entirely for a brand-new task with no activity', () => {
    const empty: Totals = {
      total_duration_s: 0,
      total_tokens: 0,
      total_cost_usd: 0,
      display: '',
    }

    const { container } = renderStageBar(empty)
    const text = container.textContent ?? ''

    // No zero-filled placeholder — the line is absent, not "0s · 0 tokens · $0.00".
    expect(text).not.toContain('tokens')
    expect(text).not.toContain('$0.00')
    expect(text).not.toContain('0s')
  })

  it('still renders the rest of the header (title) alongside the totals', () => {
    const totals: Totals = {
      total_duration_s: 10,
      total_tokens: 500,
      total_cost_usd: 0.02,
      display: '10s · 500 tokens · $0.02',
    }

    const { getByText } = renderStageBar(
      totals,
      makeTask({ title: 'A distinctive task title' }),
    )
    // Sanity check that adding totals doesn't break the existing header content.
    expect(getByText('A distinctive task title')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// StageBar — task start timestamp (AC #1)
//
// The spec requires:
//   "Task header displays 'started X minutes ago' (or similar) with full
//    datetime on hover."
//
// Acceptance criteria also require the timestamp to appear even when the task
// has not yet accrued any spend (i.e. independently of hasTotals).
// ---------------------------------------------------------------------------

const emptyTotals: Totals = {
  total_duration_s: 0,
  total_tokens: 0,
  total_cost_usd: 0,
  display: '',
}

describe('StageBar start timestamp', () => {
  it('renders a "started" label in the task header', () => {
    const { container } = renderStageBar(emptyTotals, makeTask())
    expect(container.textContent).toContain('started')
  })

  it('renders the start timestamp even when the task has no recorded activity', () => {
    // The hasTotals gate must NOT suppress the timestamp: a brand-new task
    // with zero spend still has a creation time to show.
    const { container } = renderStageBar(emptyTotals, makeTask())

    // The totals line itself is absent (existing behaviour — tested above)…
    expect(container.textContent).not.toContain('tokens')
    // …but the timestamp must still appear.
    expect(container.textContent).toContain('started')
  })

  it('exposes the full datetime as a browser tooltip on the start-time element', () => {
    // Spec: "Show full ISO datetime on hover (tooltip)."
    // Implementation uses a native `title` attribute so the tooltip works
    // without a JS-driven component.
    // OLD_CREATED_AT is 2020-01-15 — year 2020 appears in any locale.
    const { container } = renderStageBar(emptyTotals, makeTask())

    // Find the element that carries the hover tooltip on the "started" text.
    const startEl = Array.from(container.querySelectorAll('[title]')).find(
      (el) => el.textContent?.includes('started'),
    )

    expect(startEl).not.toBeNull()
    // The title should reference the actual date (year 2020 appears in any locale).
    expect(startEl!.getAttribute('title')).toContain('2020')
  })

  it('does not regress: title and totals still render alongside the start time', () => {
    const activeTotals: Totals = {
      total_duration_s: 60,
      total_tokens: 1000,
      total_cost_usd: 0.05,
      display: '1m 0s · 1,000 tokens · $0.05',
    }

    const { container } = renderStageBar(activeTotals, makeTask({ title: 'Task with timestamp and totals' }))
    const text = container.textContent ?? ''

    expect(text).toContain('Task with timestamp and totals')
    expect(text).toContain('1,000 tokens')
    expect(text).toContain('started')
  })
})

// ---------------------------------------------------------------------------
// StageBar — PR badge (ADR-030 Req 6)
//
// "An opened PR is never invisible." Whenever metadata.pr_number is set, the
// header must show a `PR #{number}` badge linking to the GitHub PR (the URL is
// carried as metadata.pr_url, written next to pr_number by the push step).
// Today Row 2 shows only the branch + worktree, so a PR-bearing task parked
// outside the PR view gives the operator no pointer to the merged/closed PR.
// ---------------------------------------------------------------------------

describe('StageBar PR badge', () => {
  it('renders a PR #{number} badge when metadata.pr_number is set', () => {
    const task = makeTask({
      metadata: { pr_number: 116, pr_url: 'https://github.com/acme/repo/pull/116' },
    })
    const { container } = renderStageBar(emptyTotals, task)

    expect(container.textContent ?? '').toContain('PR #116')
  })

  it('links the PR badge to the GitHub PR URL', () => {
    const task = makeTask({
      metadata: { pr_number: 116, pr_url: 'https://github.com/acme/repo/pull/116' },
    })
    const { container } = renderStageBar(emptyTotals, task)

    const link = Array.from(container.querySelectorAll('a')).find((a) =>
      a.textContent?.includes('PR #116'),
    )
    expect(link).toBeTruthy()
    expect(link!.getAttribute('href')).toBe('https://github.com/acme/repo/pull/116')
  })

  it('shows no PR badge when metadata.pr_number is absent', () => {
    const { container } = renderStageBar(emptyTotals, makeTask({ metadata: {} }))
    expect(container.textContent ?? '').not.toContain('PR #')
  })
})
