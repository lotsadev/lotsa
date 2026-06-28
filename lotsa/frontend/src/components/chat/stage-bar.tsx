import { useState } from 'react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { GitBranch, FolderOpen, Copy, Check, Archive, Boxes, GitPullRequest } from 'lucide-react'
import { archiveTask, jumpToStep } from '@/api/tasks'
import type { Flow, TaskDetail, Totals } from '@/api/types'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { formatRelativeTime, formatFullDateTime } from '@/lib/time'

interface StageBarProps {
  task: TaskDetail
  flow: Flow | null
  totals: Totals
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button
      onClick={handleCopy}
      className="ml-1 inline-flex items-center text-muted-foreground hover:text-foreground transition-colors"
      title="Copy to clipboard"
    >
      {copied ? <Check className="size-3 text-primary" /> : <Copy className="size-3" />}
    </button>
  )
}

function Copyable({ icon: Icon, text }: { icon: typeof GitBranch; text: string }) {
  return (
    <span className="inline-flex items-center gap-1 group">
      <Icon className="size-3 shrink-0" />
      <span className="font-mono truncate">{text}</span>
      <CopyButton text={text} />
    </span>
  )
}

export function StageBar({ task, flow, totals }: StageBarProps) {
  const queryClient = useQueryClient()

  const jumpMutation = useMutation({
    mutationFn: (stepName: string) => jumpToStep(task.id, stepName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', task.id] })
    },
  })

  const archiveMutation = useMutation({
    mutationFn: () => archiveTask(task.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', task.id] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })

  const handleArchive = () => {
    // Archive is terminal and destructive — it removes the worktree + branch.
    if (
      window.confirm(
        'Archive this task? The running agent (if any) is stopped and the worktree + branch are removed. The message log is kept, but the task cannot be retried or restored.',
      )
    ) {
      archiveMutation.mutate()
    }
  }

  const steps = flow?.steps ?? []
  const activeIndex = steps.findIndex((s) => s.name === task.current_step)

  // ADR-030: "an opened PR is never invisible." Whenever a PR has been opened
  // for this task, surface a `PR #{number}` badge linking to the GitHub PR.
  // pr_number / pr_url are written into metadata by the push step and reach the
  // frontend via the generic metadata passthrough. A PR-bearing task parked
  // outside the PR view (blocked, needs_input, …) otherwise gives the operator
  // no pointer to the PR that is still being watched until it merges/closes.
  const prNumber = task.metadata?.pr_number
  const prUrl = task.metadata?.pr_url
  const hasPr = typeof prNumber === 'number' || typeof prNumber === 'string'

  // Omit the totals line until the task has recorded activity — never show a
  // zero-filled "0s · 0 tokens · $0.00" placeholder for a brand-new task.
  const hasTotals =
    totals.total_duration_s > 0 ||
    totals.total_tokens > 0 ||
    totals.total_cost_usd > 0

  return (
    <div className="shrink-0 border-b border-border px-4 py-2.5 space-y-1.5">
      {/* Row 1: Title + Archive action */}
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-sm font-bold leading-tight line-clamp-2">{task.title}</h2>
        {task.status !== 'archived' && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 shrink-0 gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
            onClick={handleArchive}
            disabled={archiveMutation.isPending}
            title="Archive task"
          >
            <Archive className="size-3.5" />
            Archive
          </Button>
        )}
      </div>

      {/* Row 2: Project + branch + worktree (click to copy) + PR badge */}
      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        {task.project_name && (
          <span className="flex items-center gap-1" title={task.project_path}>
            <Boxes className="size-3.5" />
            {task.project_name}
          </span>
        )}
        <Copyable icon={GitBranch} text={`lotsa/${task.id}`} />
        {task.work_dir && <Copyable icon={FolderOpen} text={task.work_dir} />}
        {hasPr &&
          (typeof prUrl === 'string' ? (
            <a
              href={prUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-mono hover:text-foreground transition-colors"
            >
              <GitPullRequest className="size-3 shrink-0" />
              <span>PR #{prNumber}</span>
            </a>
          ) : (
            <span className="inline-flex items-center gap-1 font-mono">
              <GitPullRequest className="size-3 shrink-0" />
              <span>PR #{prNumber}</span>
            </span>
          ))}
      </div>

      {/* Row 3: Per-task totals + start time */}
      <div className="font-mono text-xs text-muted-foreground">
        {hasTotals && <span>{totals.display} · </span>}
        <span title={formatFullDateTime(task.created_at)}>
          started {formatRelativeTime(task.created_at)}
        </span>
      </div>

      {/* Row 4: Workflow steps (monospace) + jump selector */}
      {steps.length > 0 && (
        <div className="flex items-center gap-2 font-mono text-xs">
          <div className="flex items-center gap-1 overflow-x-auto">
            {steps.map((step, i) => {
              const isActive = i === activeIndex
              const isCompleted = activeIndex >= 0 && i < activeIndex

              return (
                <div key={step.name} className="flex shrink-0 items-center gap-1">
                  {i > 0 && (
                    <span className="text-muted-foreground">&rarr;</span>
                  )}
                  <span
                    className={cn(
                      'rounded px-1.5 py-0.5 font-semibold',
                      isActive && 'bg-primary text-primary-foreground',
                      isCompleted && 'text-primary',
                      !isActive && !isCompleted && 'text-muted-foreground opacity-60',
                    )}
                  >
                    {step.name}
                  </span>
                </div>
              )
            })}
          </div>

          {/* Jump is an action affordance — hidden for archived (read-only). */}
          {task.status !== 'archived' && (
            <div className="ml-auto shrink-0">
              <Select
                value=""
                onValueChange={(v) => jumpMutation.mutate(v)}
              >
                <SelectTrigger className="h-7 w-[110px] text-xs font-mono">
                  <SelectValue placeholder="Jump to..." />
                </SelectTrigger>
                <SelectContent>
                  {steps.map((step) => (
                    <SelectItem key={step.name} value={step.name} className="font-mono text-xs">
                      {step.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
