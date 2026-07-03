import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { EmptyState } from './empty-state'

// Regression: a deferred create's `dispatchTask` call must not strand the task
// on a failure. The create-then-upload-then-dispatch flow creates the task
// server-side FIRST; if the final `dispatchTask` throws (network blip, 5xx),
// the earlier code let it fall into the outer catch — which shows the generic
// "Failed to create task" and never calls onTaskCreated. The task then exists
// but is invisible to the operator, and a resubmit creates a duplicate. The
// fix treats a dispatch failure like an upload failure: record it, still
// navigate to the created task.

const createTask = vi.fn()
const uploadAttachment = vi.fn()
const dispatchTask = vi.fn()

vi.mock('@/api/tasks', () => ({
  createTask: (...args: unknown[]) => createTask(...args),
  uploadAttachment: (...args: unknown[]) => uploadAttachment(...args),
  dispatchTask: (...args: unknown[]) => dispatchTask(...args),
}))

// Isolate from the project/process pickers and the projects hook — this test
// exercises the submit error-handling branch, not the picker wiring.
vi.mock('@/hooks/use-projects', () => ({
  useProjects: () => ({ data: [{ id: 'proj1', name: 'lotsa', path: '/repos/lotsa' }] }),
}))
vi.mock('@/components/process-picker', () => ({
  ProcessPicker: () => null,
}))
vi.mock('@/components/project-picker', () => ({
  ProjectPicker: () => null,
}))

function renderEmptyState(onTaskCreated: (id: string) => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <EmptyState onTaskCreated={onTaskCreated} />
    </QueryClientProvider>,
  )
}

function fillAndAttach(container: HTMLElement) {
  const textarea = container.querySelector('textarea')!
  fireEvent.change(textarea, { target: { value: 'Build me a thing' } })
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
  const file = new File([new Uint8Array([1, 2, 3])], 'bug.png', { type: 'image/png' })
  fireEvent.change(fileInput, { target: { files: [file] } })
}

describe('EmptyState deferred-dispatch failure handling', () => {
  beforeEach(() => {
    createTask.mockReset()
    uploadAttachment.mockReset()
    dispatchTask.mockReset()
    localStorage.clear()
  })

  it('still navigates to the created task when dispatchTask fails, instead of stranding it', async () => {
    createTask.mockResolvedValue({ task: { id: 'newid' } })
    uploadAttachment.mockResolvedValue({})
    dispatchTask.mockRejectedValue(new Error('502 Bad Gateway'))
    const onTaskCreated = vi.fn()

    const { container, findByText } = renderEmptyState(onTaskCreated)
    fillAndAttach(container)
    fireEvent.submit(container.querySelector('form')!)

    // The created task is NOT lost — we land on it despite the dispatch failure.
    await waitFor(() => expect(onTaskCreated).toHaveBeenCalledWith('newid'))
    // Deferred create was requested (so uploads could land before first step).
    expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ defer_dispatch: true }))
    // The failure is surfaced, not swallowed into the generic create error.
    const err = await findByText(/Failed to start task/)
    expect(err.textContent).not.toContain('Failed to create task')
  })

  it('dispatches immediately (no defer) and needs no dispatchTask call when there are no files', async () => {
    createTask.mockResolvedValue({ task: { id: 'plainid' } })
    const onTaskCreated = vi.fn()

    const { container } = renderEmptyState(onTaskCreated)
    const textarea = container.querySelector('textarea')!
    fireEvent.change(textarea, { target: { value: 'No attachments here' } })
    fireEvent.submit(container.querySelector('form')!)

    await waitFor(() => expect(onTaskCreated).toHaveBeenCalledWith('plainid'))
    expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ defer_dispatch: false }))
    expect(dispatchTask).not.toHaveBeenCalled()
  })
})
