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
const sendMessage = vi.fn()
vi.mock('@/api/tasks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/tasks')>()),
  uploadAttachment: (...args: unknown[]) => uploadAttachment(...args),
  reviseTask: (...args: unknown[]) => reviseTask(...args),
  sendMessage: (...args: unknown[]) => sendMessage(...args),
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
    flow_name: 'build',
    work_dir: '/tmp/worktrees/abc123',
    project_name: 'lotsa',
    project_path: '/repos/lotsa',
    ...overrides,
  }
}

const flow: Flow = {
  name: 'build',
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

describe('ChatInput hand-off gating (ADR-043)', () => {
  // The Hand off button is the one-way Think→Execute gesture: it shows only
  // while the task is in the chat (Think) process and disappears once handed
  // off to build/fix (no build↔fix re-routing surfaced from the UI).
  it('shows Hand off while the task is still in the chat (Think) process', () => {
    const { getByRole } = renderChatInput(makeData({ flow_name: 'chat' }))
    expect(getByRole('button', { name: 'Hand off' })).toBeInTheDocument()
  })

  it('hides Hand off once the task has been handed off to an Execute process', () => {
    const { queryByRole } = renderChatInput(makeData({ flow_name: 'build' }))
    expect(queryByRole('button', { name: 'Hand off' })).toBeNull()
  })
})

describe('ChatInput chat hand-off gate (ADR-045 Phase 2)', () => {
  // Phase 2 turns the chat hand-off into an operator-GATED CALL: the task parks
  // at ``awaiting_operator`` (not ``waiting``) and the chat frame persists on the
  // stack while build/fix runs beneath it (so ``flow_name`` stays ``chat``). The
  // input panel must key its chat affordances off the ACTIVE call-stack frame and
  // treat the gate as a live REPL — not the ADR-043 post-build escape hatch.
  beforeEach(() => {
    sendMessage.mockReset()
    sendMessage.mockResolvedValue({})
    uploadAttachment.mockReset()
  })

  const CHAT_GATE = makeData({
    flow_name: 'chat',
    status: 'awaiting_operator',
    metadata: { call_stack: [{ workflow: 'chat', step: 'chat', called_from: null }] },
  })

  it('keeps the textarea enabled at the chat hand-off gate and Send re-enters the REPL', async () => {
    // Fails pre-fix: the textarea is hard-disabled at ``awaiting_operator`` and
    // submitForStatus/submitDisabled have no ``awaiting_operator`` case, so the
    // operator cannot decline/keep talking from the browser at all.
    const { container, getByRole } = renderChatInput(CHAT_GATE)
    const textarea = container.querySelector('textarea')!
    expect(textarea.disabled).toBe(false)
    fireEvent.change(textarea, { target: { value: 'actually, let’s keep talking' } })
    fireEvent.click(getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1))
    expect(sendMessage).toHaveBeenCalledWith('abc123', 'actually, let’s keep talking', [])
  })

  it('shows a hand-off gate prompt, not the "work is committed" escape-hatch copy', () => {
    // Fails pre-fix: every awaiting_operator renders "the work is committed … Mark
    // complete", which is false at a chat gate (nothing is built/committed yet).
    const { getByText, queryByText, getByRole } = renderChatInput(CHAT_GATE)
    expect(queryByText(/the work is committed/i)).toBeNull()
    expect(getByText(/Ready to hand off/i)).toBeInTheDocument()
    expect(getByRole('button', { name: 'Hand off' })).toBeInTheDocument()
  })

  it('after execute_terminated: allows Q&A but hides Hand off (may talk, may not call)', () => {
    // The callee's PR merged/closed and unwound back into the live chat frame.
    // Fails pre-fix: Hand off shows (flow_name chat) and its dialog would offer an
    // Accept that the backend rejects with 400 for an execute_terminated frame.
    const returned = makeData({
      flow_name: 'chat',
      status: 'awaiting_operator',
      metadata: {
        call_stack: [{ workflow: 'chat', step: 'chat', called_from: null }],
        execute_terminated: true,
      },
    })
    const { container, queryByRole, getByText } = renderChatInput(returned)
    expect(container.querySelector('textarea')!.disabled).toBe(false)
    expect(queryByRole('button', { name: 'Hand off' })).toBeNull()
    expect(getByText(/has shipped/i)).toBeInTheDocument()
  })

  it('hides Hand off while a pushed Execute callee is running under the chat root', () => {
    // ADR-045 Phase 2 — the gated call keeps ``flow_name`` = ``chat`` while
    // build/fix runs as the active frame. Fails pre-fix: canPromote gated on
    // ``flow_name === 'chat'``, so Hand off showed here and clicking it would
    // re-root (promoteTask) and orphan the running build.
    const running = makeData({
      flow_name: 'chat',
      status: 'waiting_for_pr',
      current_step: 'wait_for_pr_signal',
      metadata: {
        pr_number: 7,
        call_stack: [
          { workflow: 'chat', step: 'chat', called_from: null },
          { workflow: 'build', step: 'push_pr', called_from: 'chat' },
          { workflow: 'pr-monitor', step: 'wait_for_pr_signal', called_from: 'push_pr' },
        ],
      },
    })
    const { queryByRole } = renderChatInput(running)
    expect(queryByRole('button', { name: 'Hand off' })).toBeNull()
  })

  it('leaves the ADR-043 post-build escape hatch (non-chat) untouched', () => {
    // A directly-selected build task parked awaiting the operator: textarea stays
    // disabled and the "work is committed / Mark complete" copy still shows.
    const postBuild = makeData({
      flow_name: 'build',
      status: 'awaiting_operator',
      metadata: { call_stack: [{ workflow: 'build', step: 'code', called_from: null }] },
    })
    const { container, getByText } = renderChatInput(postBuild)
    expect(container.querySelector('textarea')!.disabled).toBe(true)
    expect(getByText(/the work is committed/i)).toBeInTheDocument()
  })
})

describe('ChatInput PR-monitoring CI-check status', () => {
  // The monitoring row renders only while the task is parked on the PR
  // (status === 'waiting_for_pr'). The CI summary replaces the old bare
  // `checks 0/1` (which read like "0 of 1 passing" and alarmed operators —
  // see the attached screenshot) with a failing / running / passed summary.
  function monitoring(checks: Record<string, unknown>) {
    return renderChatInput(
      makeData({
        status: 'waiting_for_pr',
        metadata: { pr_number: 27, ...checks },
      }),
    )
  }

  it('shows a red failing summary when any CI check is failing', () => {
    const { getByText } = monitoring({
      pr_checks_total: 3,
      pr_checks_passing: 1,
      pr_checks_failing: 2,
    })
    const el = getByText(/2 CI checks failing/)
    expect(el).toBeInTheDocument()
    expect(el.className).toMatch(/text-destructive/)
  })

  it('singularises the failing summary for exactly one failing check', () => {
    const { getByText } = monitoring({
      pr_checks_total: 1,
      pr_checks_passing: 0,
      pr_checks_failing: 1,
    })
    expect(getByText(/1 CI check failing/)).toBeInTheDocument()
  })

  it('shows a running summary with passing/total while checks are pending (the 0/1 case)', () => {
    const { getByText } = monitoring({
      pr_checks_total: 1,
      pr_checks_passing: 0,
      pr_checks_failing: 0,
    })
    const el = getByText(/CI checks running/)
    expect(el.textContent).toContain('(0/1)')
  })

  it('shows an all-passed summary once every check has passed', () => {
    const { getByText } = monitoring({
      pr_checks_total: 2,
      pr_checks_passing: 2,
      pr_checks_failing: 0,
    })
    expect(getByText(/CI checks passed/)).toBeInTheDocument()
  })

  it('prioritises the failing summary over checks still in flight', () => {
    // failing must win over pending — a red ✗ summary shows even while other
    // checks are still running (pending = 3 - 1 - 1 = 1), so a real failure is
    // never masked by a "running…" message. Guards the branch ordering in
    // chat-input.tsx (failing check precedes the pending check).
    const { getByText, queryByText } = monitoring({
      pr_checks_total: 3,
      pr_checks_passing: 1,
      pr_checks_failing: 1,
    })
    const el = getByText(/1 CI check failing/)
    expect(el.className).toMatch(/text-destructive/)
    expect(queryByText(/CI checks running/)).toBeNull()
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
