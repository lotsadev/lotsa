import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ChatInput } from './chat-input'
import type { Flow, TaskDetail, TaskDetailFull } from '@/api/types'

// Spy only the two calls the partial-upload regression drives; keep every other
// export (fetchProcesses etc., used by PromoteDialog's subtree) real so the
// component renders. No submit hits the network — reviseTask is stubbed.
const uploadAttachment = vi.fn()
const reviseTask = vi.fn()
vi.mock('@/api/tasks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/tasks')>()),
  uploadAttachment: (...args: unknown[]) => uploadAttachment(...args),
  reviseTask: (...args: unknown[]) => reviseTask(...args),
}))

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

describe('ChatInput partial-upload retry', () => {
  beforeEach(() => {
    uploadAttachment.mockReset()
    reviseTask.mockReset()
    reviseTask.mockResolvedValue({})
  })

  // Regression: uploadPending() must drop each successfully-uploaded file from
  // the picker as its POST resolves, even when a *later* file in the batch
  // fails. Before the fix `setFiles([])` only ran on full success, so a partial
  // failure left the already-durable files selected and the next Send
  // re-uploaded them — duplicate suffixed records + burning the 10-file cap.
  it('does not re-upload already-uploaded files after a mid-batch failure', async () => {
    // File 1 uploads fine; file 2 fails.
    uploadAttachment
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error('502 Bad Gateway'))

    const { container, findByText } = renderChatInput()
    const textarea = container.querySelector('textarea')!
    fireEvent.change(textarea, { target: { value: 'here are two files' } })
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const fileA = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
    const fileB = new File([new Uint8Array([2])], 'b.png', { type: 'image/png' })
    fireEvent.change(fileInput, { target: { files: [fileA, fileB] } })

    // First Send: uploads a.png (ok) then b.png (fails), aborting the action.
    fireEvent.submit(container.querySelector('form')!)
    await findByText(/Failed to attach b\.png/)
    expect(reviseTask).not.toHaveBeenCalled()
    // The failed file's chip stays; the uploaded one is gone.
    expect(container.querySelector('[aria-label="Remove b.png"]')).not.toBeNull()
    expect(container.querySelector('[aria-label="Remove a.png"]')).toBeNull()

    // Second Send: only b.png is retried — a.png is never re-uploaded.
    uploadAttachment.mockResolvedValue({})
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(reviseTask).toHaveBeenCalledTimes(1))
    const uploadedNames = uploadAttachment.mock.calls.map((c) => (c[1] as File).name)
    expect(uploadedNames).toEqual(['a.png', 'b.png', 'b.png'])
    expect(uploadedNames.filter((n) => n === 'a.png')).toHaveLength(1)
  })
})
