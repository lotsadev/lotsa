import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PromoteDialog } from './promote-dialog'
import type { Process, TaskDetailFull } from '@/api/types'

// Formalized handoff suggestion (ADR-044 Phase 4c): when the chat agent has
// recorded a `handoff_suggestion` artifact, the Hand off dialog pre-selects that
// destination and turns the primary button into an accept-the-recommendation
// action, while still offering every other hand-off destination. When there is
// no suggestion, the dialog behaves exactly as today (nothing pre-selected).
//
// These tests fail against the pre-fix tree: PromoteDialog does not read the
// suggestion, so there is no "Accept" button and clicking never promotes to the
// recommended destination.

const promoteTask = vi.fn()
vi.mock('@/api/tasks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/tasks')>()),
  promoteTask: (...args: unknown[]) => promoteTask(...args),
}))

// The dialog reads the loaded process catalog (destinations) and the task
// detail (the recommendation) via these hooks — mock both so the component
// renders synchronously without touching the network.
const BUILD: Process = {
  name: 'build',
  is_active: false,
  is_default: false,
  step_names: ['plan', 'code'],
  description: 'Execute at full depth.',
  promotion_inputs: [],
  invocable: ['start', 'hand-off'],
}
const FIX: Process = {
  name: 'fix',
  is_active: false,
  is_default: false,
  step_names: ['code'],
  description: 'Shallow fix.',
  promotion_inputs: [],
  invocable: ['start', 'hand-off'],
}

let processesData: Process[] = [BUILD, FIX]
vi.mock('@/hooks/use-processes', () => ({
  useProcesses: () => ({ data: processesData }),
}))

let artifactsData: Record<string, string> = {}
vi.mock('@/hooks/use-task', () => ({
  useTask: () => ({ data: { artifacts: artifactsData } as Partial<TaskDetailFull> }),
}))

function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PromoteDialog taskId="task-1" open onOpenChange={() => {}} />
    </QueryClientProvider>,
  )
}

describe('PromoteDialog handoff-suggestion pre-selection', () => {
  beforeEach(() => {
    promoteTask.mockReset()
    promoteTask.mockResolvedValue({})
    processesData = [BUILD, FIX]
    artifactsData = {}
  })

  it('pre-selects the recommended destination and offers an Accept action', () => {
    artifactsData = { handoff_suggestion: 'build' }
    const { getByRole } = renderDialog()
    // The primary button reflects acceptance of the recommendation (short copy).
    const accept = getByRole('button', { name: /Accept/i })
    expect(accept).toBeInTheDocument()
    expect(accept).not.toBeDisabled()
  })

  it('promotes to the recommended destination in one click', async () => {
    artifactsData = { handoff_suggestion: 'build' }
    const { getByRole } = renderDialog()
    fireEvent.click(getByRole('button', { name: /Accept/i }))
    await waitFor(() => expect(promoteTask).toHaveBeenCalledTimes(1))
    expect(promoteTask).toHaveBeenCalledWith('task-1', 'build', undefined)
  })

  it('falls back to the plain Hand off flow when there is no suggestion', () => {
    artifactsData = {}
    const { getByRole, queryByRole } = renderDialog()
    // No pre-selection → the accept affordance is absent and the primary button
    // is the disabled "Hand off" until the operator picks a destination.
    expect(queryByRole('button', { name: /Accept/i })).toBeNull()
    expect(getByRole('button', { name: 'Hand off' })).toBeDisabled()
  })

  it('ignores a suggestion that is not a hand-off-invocable destination', () => {
    // A stale / non-offerable recommendation must not drive a pre-selection.
    artifactsData = { handoff_suggestion: 'chat' }
    const { queryByRole } = renderDialog()
    expect(queryByRole('button', { name: /Accept/i })).toBeNull()
  })
})
