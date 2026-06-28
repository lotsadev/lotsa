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
} from '@/api/tasks'
import type { TaskDetailFull } from '@/api/types'
import { PromoteDialog } from './promote-dialog'

interface ChatInputProps {
  data: TaskDetailFull
}

export function ChatInput({ data }: ChatInputProps) {
  const [inputValue, setInputValue] = useState('')
  const [reasonOpen, setReasonOpen] = useState(false)
  const [reasonText, setReasonText] = useState('')
  const [promoteOpen, setPromoteOpen] = useState(false)
  const queryClient = useQueryClient()
  const { task } = data
  const availableOverrides = data.available_overrides ?? []

  // ADR-027 — promotion is valid from any non-terminal state. Mirror the
  // server-side guard in promote_task exactly: it rejects terminal tasks on
  // BOTH columns (status in complete/abandoned/archived OR state in
  // complete/abandoned), since "terminal" is observable on either depending on
  // the path that finalized the task. Gating on status alone would, in the
  // edge case where the columns diverge, show a clickable button that 400s.
  const canPromote =
    !['complete', 'abandoned', 'archived'].includes(task.status) &&
    !['complete', 'abandoned'].includes(task.state)

  const onSuccess = () => {
    queryClient.invalidateQueries({ queryKey: ['task', task.id] })
    setInputValue('')
  }

  const sendMutation = useMutation({ mutationFn: () => sendMessage(task.id, inputValue), onSuccess })
  const reviseMutation = useMutation({ mutationFn: () => reviseTask(task.id, inputValue), onSuccess })
  const answerMutation = useMutation({ mutationFn: () => answerTask(task.id, inputValue), onSuccess })
  const approveMutation = useMutation({ mutationFn: () => approveTask(task.id), onSuccess })
  const retryMutation = useMutation({ mutationFn: () => retryTask(task.id), onSuccess })
  const stopMutation = useMutation({ mutationFn: () => stopAgent(task.id), onSuccess })
  // Acknowledge a fired guard. Empty reason submits as null. On success the
  // task query is invalidated: the new audit row appears and detect() now
  // returns False, so the override button disappears.
  const overrideMutation = useMutation({
    mutationFn: (guardName: string) =>
      acknowledgeOverride(task.id, guardName, reasonText.trim() || null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', task.id] })
      setReasonText('')
      setReasonOpen(false)
    },
  })

  const isPending =
    sendMutation.isPending ||
    reviseMutation.isPending ||
    answerMutation.isPending ||
    approveMutation.isPending ||
    retryMutation.isPending ||
    stopMutation.isPending ||
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
  // Accept only appears at a GATE: a step with an output artifact to accept, or
  // an evaluate gate. A plain conversational REPL (chat) has neither and is ended
  // by Promote/Abandon, not Accept — so no button there (matches approve()).
  const isGate = (requiredArtifact !== null && requiredArtifact in artifacts) || currentStep?.evaluate === true
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

      {availableOverrides.length > 0 && (
        <div className="mb-2 text-xs">
          {reasonOpen ? (
            <textarea
              value={reasonText}
              onChange={(e) => setReasonText(e.target.value)}
              placeholder="Add reason (optional)"
              rows={2}
              disabled={isPending}
              className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
          ) : (
            <button
              type="button"
              onClick={() => setReasonOpen(true)}
              className="text-muted-foreground underline"
            >
              Add reason (optional)
            </button>
          )}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault()
          submitForStatus()
        }}
        className="flex items-end gap-2"
      >
        <AutoGrowTextarea
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onSubmit={submitForStatus}
          placeholder={placeholders[task.status] ?? ''}
          disabled={isPending || task.status === 'complete' || task.status === 'abandoned'}
          className="flex-1"
        />
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
        {canPromote && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setPromoteOpen(true)}
            disabled={isPending}
          >
            Promote
          </Button>
        )}
      </form>

      <PromoteDialog taskId={task.id} open={promoteOpen} onOpenChange={setPromoteOpen} />
    </div>
  )
}
