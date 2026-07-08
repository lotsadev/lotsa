import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { TaskStatus } from '@/api/types'

// ChangesTab reads the theme (light/dark) to pass to the diff renderer; pin it
// so the component mounts without a ThemeProvider in the test tree.
vi.mock('@/hooks/use-theme', () => ({
  useTheme: () => ({ theme: 'light' }),
}))

// PatchDiff renders real syntax-highlighted diffs (shiki) which is heavy and
// irrelevant here — we only care about the empty-state vs. diff-present branch.
// Stub it to a marker element so a non-empty diff is observably rendered.
vi.mock('@pierre/diffs/react', () => ({
  PatchDiff: ({ patch }: { patch: string }) => <div data-testid="patch-diff">{patch}</div>,
}))

vi.mock('@/api/tasks', () => ({
  fetchDiff: vi.fn(),
}))

import { ChangesTab } from './changes-tab'
import { fetchDiff } from '@/api/tasks'

const mockFetch = vi.mocked(fetchDiff)

interface RenderProps {
  status: TaskStatus
  prNumber?: number | string
  prUrl?: string
}

function renderTab({ status, prNumber, prUrl }: RenderProps) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ChangesTab
        taskId="task-1"
        active={true}
        status={status}
        prNumber={prNumber}
        prUrl={prUrl}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  // Default: worktree gone → the endpoint returns an empty patch (200 { diff: '' }).
  mockFetch.mockResolvedValue({ diff: '' })
})

afterEach(() => {
  cleanup()
  mockFetch.mockReset()
})

const TERMINAL_MESSAGE = "We can't display changes for completed or archived tasks."

describe('ChangesTab — terminal empty state', () => {
  it('shows the terminal message with a PR link when a complete task has a pr_url', async () => {
    const { container, findByText } = renderTab({
      status: 'complete',
      prNumber: 42,
      prUrl: 'https://github.com/acme/repo/pull/42',
    })

    await findByText(TERMINAL_MESSAGE)

    const link = container.querySelector('a[href="https://github.com/acme/repo/pull/42"]')
    expect(link).not.toBeNull()
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noreferrer')
    expect(link?.textContent).toContain('Check the PR #42')
  })

  it('shows a non-link PR reference when a terminal task has a pr_number but no pr_url', async () => {
    const { container, findByText } = renderTab({ status: 'complete', prNumber: 42 })

    await findByText(TERMINAL_MESSAGE)
    expect(container.textContent).toContain('Check the PR #42')
    // No pr_url → the reference must not be an anchor.
    expect(container.querySelector('a')).toBeNull()
  })

  it('shows the terminal message alone with no dangling PR link when there is no PR', async () => {
    const { container, findByText } = renderTab({ status: 'archived' })

    await findByText(TERMINAL_MESSAGE)
    expect(container.textContent).not.toContain('Check the PR')
    expect(container.querySelector('a')).toBeNull()
  })

  it('treats abandoned as a terminal status', async () => {
    const { findByText, queryByText } = renderTab({ status: 'abandoned' })

    await findByText(TERMINAL_MESSAGE)
    expect(queryByText('No changes yet')).toBeNull()
  })

  it('accepts a string pr_number', async () => {
    const { container, findByText } = renderTab({
      status: 'archived',
      prNumber: '7',
      prUrl: 'https://github.com/acme/repo/pull/7',
    })

    await findByText(TERMINAL_MESSAGE)
    expect(container.textContent).toContain('Check the PR #7')
    expect(container.querySelector('a[href="https://github.com/acme/repo/pull/7"]')).not.toBeNull()
  })
})

describe('ChangesTab — non-terminal empty state', () => {
  it('keeps "No changes yet" for a working task with no diff', async () => {
    const { container, findByText } = renderTab({ status: 'working' })

    await findByText('No changes yet')
    // The terminal message must not appear for a live task.
    expect(container.textContent).not.toContain(TERMINAL_MESSAGE)
  })
})

describe('ChangesTab — diff present', () => {
  it('renders the diff, not the terminal message, when a terminal task still has changes', async () => {
    mockFetch.mockResolvedValue({
      diff: 'diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hello\n',
    })

    const { container, findByTestId } = renderTab({ status: 'complete', prNumber: 42 })

    await findByTestId('patch-diff')
    expect(container.textContent).not.toContain(TERMINAL_MESSAGE)
  })
})
