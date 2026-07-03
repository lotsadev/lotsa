import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { AutoGrowTextarea } from '@/components/ui/auto-grow-textarea'
import {
  approveTask,
  reviseTask,
  sendMessage,
  answerTask,
  retryTask,
  stopAgent,
  acknowledgeOverride,
  markCompleteTask,
  uploadAttachment,
} from '@/api/tasks'
import type { TaskDetailFull } from '@/api/types'
import { AttachmentPicker } from '@/components/attachment-picker'
import { PromoteDialog } from './promote-dialog'

interface ChatInputProps {
  data: TaskDetailFull
}

export function ChatInput({ data }: ChatInputProps) {
  const [inputValue, setInputValue] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)
  const [promoteOpen, setPromoteOpen] = useState(false)
  const queryClient = useQueryClient()
  const { task } = data
  const availableOverrides = data.available_overrides ?? []

  // Upload any pending attachments to this task before the message dispatches,
  // so the next step materializes them. Throws (aborting the send) if an upload
  // fails, surfacing the error inline rather than sending a message that
  // references files that never arrived.
  //
  // A successfully-uploaded file is dropped from `files` as soon as its POST
  // resolves — even when a *later* file in the batch fails. Otherwise a partial
  // failure would leave the already-durable files selected, and the next
  // Send/Approve/Retry would re-upload them, creating duplicate suffixed
  // records (`name (1).png`) and burning the per-task count cap. The failed
  // file and any not-yet-attempted ones stay selected so a retry re-sends only
  // those.
  const uploadPending = async () => {
    setAttachError(null)
    const remaining = [...files]
    try {
      while (remaining.length > 0) {
        const f = remaining[0]
        try {
          await uploadAttachment(task.id, f)
        } catch (e) {
          setAttachError(`Failed to attach ${f.name}: ${(e as Error).message}`)
          throw e
        }
        remaining.shift() // Uploaded durably — never re-send it on a retry.
      }
    } finally {
      setFiles(remaining)
    }
  }

  // ADR-027 — promotion is valid from any non-terminal state. Mirror the
  // server-side guard in promote_task exactly: it rejects terminal tasks on
  // BOTH columns (status in complete/abandoned/archived OR state in
  // complete/abandoned), since "terminal" is observable on either depending on
  // the path that finalized the task. Gating on status alone would, in the
  // edge case where the columns diverge, show a clickable button that 400s.
  const canPromote =
    !['complete', 'abandoned', 'archived'].includes(task.status) &&
    !['complete', 'abandoned'].includes(task.state)

  // ADR-043 — the operator "Mark complete" escape hatch. Available on any
  // non-terminal task, surfaced where it matters: a task parked awaiting the
  // operator (``awaiting_operator``), watching a PR, or blocked — e.g. a
  // GitHub-less run that can't reach a PR terminal on its own.
  const canMarkComplete =
    ['awaiting_operator', 'waiting_for_pr', 'blocked'].includes(task.status)

  const onSuccess = () => {
    queryClient.invalidateQueries({ queryKey: ['task', task.id] })
    setInputValue('')
  }

  const sendMutation = useMutation({
    mutationFn: async () => {
      await uploadPending()
      return sendMessage(task.id, inputValue)
    },
    onSuccess,
  })
  const reviseMutation = useMutation({
    mutationFn: async () => {
      await uploadPending()
      return reviseTask(task.id, inputValue)
    },
    onSuccess,
  })
  const answerMutation = useMutation({
    mutationFn: async () => {
      await uploadPending()
      return answerTask(task.id, inputValue)
    },
    onSuccess,
  })
  // Accept / Retry / Acknowledge all (re)dispatch a step that materializes the
  // task's attachments, so pending files must be uploaded first — otherwise a
  // file attached-then-Accepted is silently dropped from that dispatch. Upload
  // is awaited before the action; a failed upload aborts it with an inline
  // error (same contract as Send/Revise), rather than advancing without the file.
  const approveMutation = useMutation({
    mutationFn: async () => {
      await uploadPending()
      return approveTask(task.id)
    },
    onSuccess,
  })
  const retryMutation = useMutation({
    mutationFn: async () => {
      await uploadPending()
      return retryTask(task.id)
    },
    onSuccess,
  })
  // Stop is a halt, not a dispatch — it must stay reliable, so it does NOT
  // upload (a transient upload failure can't be allowed to block stopping a
  // runaway agent). Any pending files stay selected in the picker and go out
  // with the operator's next Send.
  const stopMutation = useMutation({ mutationFn: () => stopAgent(task.id), onSuccess })
  const markCompleteMutation = useMutation({ mutationFn: () => markCompleteTask(task.id), onSuccess })
  // Acknowledge a fired guard: reset the guard and resume the step in one bare
  // action (no reason field — ADR-019 revised 2026-07-02). On success the task
  // query is invalidated: the new audit row appears and detect() now returns
  // False, so the override button disappears.
  const overrideMutation = useMutation({
    mutationFn: async (guardName: string) => {
      await uploadPending()
      return acknowledgeOverride(task.id, guardName)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', task.id] })
    },
  })

  const isPending =
    sendMutation.isPending ||
    reviseMutation.isPending ||
    answerMutation.isPending ||
    approveMutation.isPending ||
    retryMutation.isPending ||
    stopMutation.isPending ||
    markCompleteMutation.isPending ||
    overrideMutation.isPending

  // After a NON_FAST_FORWARD push the orchestrator parks the task at
  // (status='blocked', state='rebasing') and retry() rejects with
  // RetryNotAllowed — recovery is revise → pr-fix, not Retry. Surface a
  // feedback input here instead of the Retry-only blocked UI.
  const isRebasing = task.state === 'rebasing'

  const placeholders: Record<string, string> = {
    working: 'Type while waiting…',
    waiting: task.is_conversational ? 'Send a message…' : 'Type to revise…',
    waiting_for_pr: 'Send PR feedback to dispatch a fix cycle…',
    needs_input: "Answer the agent's question…",
    awaiting_operator: 'Awaiting you — Mark complete when the work is done.',
    blocked: isRebasing
      ? 'Describe how to recover the branch — e.g. rebase on main…'
      : 'Send a corrected message to resume, or Retry to re-run as-is…',
    complete: 'Task complete',
    abandoned: 'PR closed without merge',
  }

  const submitForStatus = () => {
    if (!inputValue.trim()) return
    if (isRebasing) {
      // revise() detects state='rebasing' and routes into a pr-fix cycle.
      reviseMutation.mutate()
      return
    }
    switch (task.status) {
      case 'waiting':
        if (task.is_conversational) sendMutation.mutate()
        else reviseMutation.mutate()
        break
      case 'waiting_for_pr':
        // Manual feedback on a PR — orchestrator routes this to a pr-fix dispatch.
        reviseMutation.mutate()
        break
      case 'needs_input':
        answerMutation.mutate()
        break
      case 'blocked':
        // Stop → amend → resume: send_message accepts blocked and
        // re-dispatches the preserved current_step with the message as
        // revision feedback (a bare Retry re-runs without the input).
        sendMutation.mutate()
        break
      // working/complete/abandoned: submit disabled.
    }
  }

  // Approve is allowed when the current step's declared output artifact
  // exists in the DB. Never appears in waiting_for_pr — the PR monitor
  // advances that state autonomously.
  const currentStep = data.flow?.steps.find((s) => s.name === task.current_step)
  const requiredArtifact = currentStep?.output ?? null
  const artifacts = data.artifacts ?? {}
  // Wait until the flow is loaded before resolving requiredArtifact —
  // otherwise on slow connections the Accept button briefly appears for a
  // status='waiting' task before currentStep can be looked up, causing a
  // flicker (and a 400 round-trip if the user clicks during the gap).
  // current_step is always set when status='waiting' in the new model, but
  // a data-layer bug that left it null would let canApprove flip true when
  // requiredArtifact is null — clicks would 400 server-side. Guard defensively.
  //
  // needs_input also shows Accept, but ONLY at an evaluate gate (spec/plan)
  // with its output artifact present: if the agent answered a clarification
  // and then asked its own question, the operator can accept the gate output
  // instead of being forced to answer (which re-runs the whole step). We gate
  // on `currentStep.evaluate` directly — the exact condition the backend
  // approve() enforces — rather than proxying via "has an output artifact",
  // so a future non-evaluate step that declares an output can't flash Accept
  // and then 400.
  // Accept only appears at a GATE — the backend's ResolvedJob.is_approval_gate:
  // a step with an output artifact, an evaluate gate, or a conversational step
  // with a forward advance rule (e.g. verify). A plain conversational REPL (chat)
  // has none and is ended by Promote/Abandon, not Accept (matches approve()).
  // When the gate produces an output artifact, also require it present so Accept
  // can't fire before e.g. the spec exists; evaluate/verify gates have no artifact.
  const isGate =
    currentStep?.is_gate === true && (requiredArtifact === null || requiredArtifact in artifacts)
  const canApprove =
    task.current_step !== null &&
    data.flow !== null &&
    ((task.status === 'waiting' && isGate) ||
      (task.status === 'needs_input' &&
        currentStep?.evaluate === true &&
        requiredArtifact !== null &&
        requiredArtifact in artifacts))

  const submitDisabled =
    isPending ||
    !inputValue.trim() ||
    !(
      task.status === 'waiting' ||
      task.status === 'waiting_for_pr' ||
      task.status === 'needs_input' ||
      task.status === 'blocked' ||
      isRebasing
    )

  // Metadata helper for waiting_for_pr status row.
  const meta = task.metadata as Record<string, unknown> | undefined
  const prNumber = meta?.pr_number as number | string | undefined
  const prUrl = meta?.pr_url as string | undefined
  const prDecision = meta?.pr_review_decision as string | undefined
  const checksPassing = meta?.pr_checks_passing as number | undefined
  const checksTotal = meta?.pr_checks_total as number | undefined
  const checksFailing = meta?.pr_checks_failing as number | undefined
  const feedbackCount = meta?.pr_feedback_count as number | undefined

  // Archived is terminal and review-only: no input, no action buttons. The
  // message log above stays fully readable.
  if (task.status === 'archived') {
    return (
      <div className="border-t border-border px-4 py-3 text-sm text-muted-foreground">
        This task is archived — read-only.
      </div>
    )
  }

  return (
    <div className="border-t border-border px-4 py-3">
      {task.status === 'working' && (
        <div className="mb-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span className="relative flex size-2">
            <span className="absolute size-full animate-ping rounded-full bg-primary opacity-75" />
            <span className="relative size-2 rounded-full bg-primary" />
          </span>
          <span>
            {task.current_step ? `${task.current_step} agent is working…` : 'Agent is working…'}
          </span>
          {task.elapsed_s > 0 && (
            <span className="ml-auto font-mono text-xs">{task.elapsed_s}s</span>
          )}
        </div>
      )}

      {isRebasing && (
        <div className="mb-2 text-xs text-muted-foreground">
          Push rejected as non-fast-forward. Send feedback to dispatch a pr-fix cycle.
        </div>
      )}

      {task.status === 'waiting_for_pr' && (
        <div className="mb-2 text-xs text-muted-foreground">
          Monitoring{' '}
          {prUrl ? (
            <a href={prUrl} className="text-primary underline" target="_blank" rel="noreferrer">
              PR #{prNumber ?? '?'}
            </a>
          ) : (
            <span>PR #{prNumber ?? '?'}</span>
          )}
          {prDecision && <span> · {prDecision}</span>}
          {checksTotal !== undefined && checksTotal > 0 && (
            <span>
              {' '}· checks {checksPassing ?? 0}/{checksTotal}
              {(checksFailing ?? 0) > 0 && <span> ({checksFailing} failing)</span>}
            </span>
          )}
          {(feedbackCount ?? 0) > 0 && (
            <span> · {feedbackCount} comment{feedbackCount === 1 ? '' : 's'}</span>
          )}
        </div>
      )}

      {task.status === 'awaiting_operator' && (
        <div className="mb-2 text-xs text-muted-foreground">
          Awaiting you — the work is committed on{' '}
          <span className="font-mono">lotsa/{task.id}</span>. Review it and click{' '}
          <strong>Mark complete</strong> to close the task (the GitHub-less escape hatch).
        </div>
      )}

      {/* ``flex-wrap`` lets the action button group drop below the textarea on
          narrow screens instead of overflowing a single line; ``min-w-0`` on
          the textarea lets it shrink. On desktop there's room, so it stays on
          one line — unchanged. */}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          submitForStatus()
        }}
        className="flex flex-wrap items-end gap-2"
      >
        {task.status !== 'complete' && task.status !== 'abandoned' && (
          <AttachmentPicker
            files={files}
            onChange={setFiles}
            disabled={isPending}
            error={attachError}
            className="w-full basis-full"
          />
        )}
        <AutoGrowTextarea
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onSubmit={submitForStatus}
          placeholder={placeholders[task.status] ?? ''}
          disabled={isPending || task.status === 'complete' || task.status === 'abandoned'}
          className="min-w-0 flex-1"
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={submitDisabled}>
            Send
          </Button>
          {task.status === 'working' && (
            <Button
              type="button"
              size="sm"
              variant="destructive"
              onClick={() => stopMutation.mutate()}
              disabled={isPending}
            >
              Stop
            </Button>
          )}
          {canApprove && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => approveMutation.mutate()}
              disabled={isPending}
            >
              Accept
            </Button>
          )}
          {/* Bare Retry is for plain blocks (crash, sync, agent error). When a
              guard override is available, its "Acknowledge & continue" button
              both clears the guard AND resumes (ADR-019 revised) — showing Retry
              too would be the redundant two-button confusion we removed. */}
          {task.status === 'blocked' && !isRebasing && availableOverrides.length === 0 && (
            <Button
              type="button"
              size="sm"
              variant="destructive"
              onClick={() => retryMutation.mutate()}
              disabled={isPending}
            >
              Retry
            </Button>
          )}
          {availableOverrides.map((ov) => (
            <Button
              key={ov.guard_name}
              type="button"
              size="sm"
              variant="secondary"
              title={ov.description}
              onClick={() => overrideMutation.mutate(ov.guard_name)}
              disabled={isPending}
            >
              {ov.label}
            </Button>
          ))}
          {canMarkComplete && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => markCompleteMutation.mutate()}
              disabled={isPending}
            >
              Mark complete
            </Button>
          )}
          {canPromote && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setPromoteOpen(true)}
              disabled={isPending}
            >
              Hand off
            </Button>
          )}
        </div>
      </form>

      <PromoteDialog taskId={task.id} open={promoteOpen} onOpenChange={setPromoteOpen} />
    </div>
  )
}
