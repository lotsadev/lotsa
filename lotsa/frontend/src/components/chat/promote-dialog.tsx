import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { promoteTask } from '@/api/tasks'
import { useProcesses } from '@/hooks/use-processes'
import { useTask } from '@/hooks/use-task'

interface PromoteDialogProps {
  taskId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

// ADR-043 — the handoff (Think→Execute) gesture. Friendly labels for the
// bundled Execute processes; inline/custom processes fall through to their raw
// name. Internals (promote_task / initial_artifacts) are unchanged (ADR-043 §15
// — UI language only).
const HANDOFF_LABELS: Record<string, string> = {
  build: 'Build it',
  fix: 'Quick fix',
}
const handoffLabel = (name: string) => HANDOFF_LABELS[name] ?? name

// ADR-027/043 — operator-driven handoff to a loaded destination process. The
// dialog only picks the destination: promotion carries the full chat transcript
// forward automatically (promote_task seeds it under promotion_context and each
// of the destination's declared promotion_inputs when called with no explicit
// artifacts), so there are no per-input fields to fill in.
//
// ADR-044 Phase 4c — when the chat agent has recorded a `handoff_suggestion`
// (the task detail's named-artifact map), the dialog pre-selects that
// destination and turns the primary button into an accept-the-recommendation
// action. The operator can still pick another destination or cancel and keep
// chatting; promotion seeding is unchanged (still called with no artifacts).
export function PromoteDialog({ taskId, open, onOpenChange }: PromoteDialogProps) {
  const queryClient = useQueryClient()
  const { data: processes } = useProcesses()
  const { data: taskData } = useTask(taskId)
  const [destination, setDestination] = useState<string>('')

  // ADR-044 Phase 4 — offer only workflows that advertise themselves as a
  // hand-off destination (``invocable`` includes 'hand-off'), driving the
  // filter off the declared property instead of the hardcoded name 'chat'.
  // chat is ``invocable: [start]`` so it excludes itself; a payload missing the
  // field (older server) defaults to offerable, preserving prior behaviour.
  const options = useMemo(
    () =>
      (processes ?? []).filter(
        (p) => p.invocable === undefined || p.invocable.includes('hand-off')
      ),
    [processes]
  )

  // ADR-044 Phase 4c — the agent's recommendation, but only trust it if it is
  // actually an offered destination (drops a stale / non-hand-off-invocable
  // suggestion so a bad recommendation never drives a pre-selection).
  const suggested = useMemo(() => {
    const rec = taskData?.artifacts?.handoff_suggestion
    return rec && options.some((p) => p.name === rec) ? rec : ''
  }, [taskData, options])

  // The effective selection: an explicit operator pick (``destination``) wins;
  // otherwise fall back to the recommendation. Deriving it (rather than syncing
  // ``destination`` in an effect) keeps the pre-selection reactive without a
  // setState-in-effect, and the operator can still override via the dropdown.
  const selected = destination || suggested
  const isAccepting = selected !== '' && selected === suggested

  const mutation = useMutation({
    // No artifacts: the destination's first step (build's plan, fix's coding)
    // reads the full chat transcript, which promote_task seeds under
    // promotion_context and each of the destination's declared promotion_inputs
    // (draft_spec for build, instruction for fix) when called with no explicit
    // fields.
    mutationFn: () => promoteTask(taskId, selected, undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskId] })
      onOpenChange(false)
      setDestination('')
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* On mobile the centered dialog caps to the viewport width and scrolls
          vertically so the destination picker never overflows a narrow
          screen. */}
      <DialogContent className="max-h-[85dvh] overflow-y-auto md:max-w-2xl max-md:max-w-[calc(100vw-1rem)]">
        <DialogHeader>
          <DialogTitle>Hand off to Execute</DialogTitle>
          <DialogDescription>
            Choose how thorough: <strong>Build it</strong> for the full SDLC
            pass, or <strong>Quick fix</strong> for a mechanical change. The
            worktree and the full audit log stay; the handoff is one-way (no
            return to chat) but the running task stays steerable.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Select
            value={selected}
            onValueChange={(value) => {
              // Clear any prior refusal so a stale PROMOTE_NOT_ALLOWED message
              // doesn't linger after the operator picks a different destination.
              if (mutation.isError) mutation.reset()
              setDestination(value)
            }}
          >
            <SelectTrigger aria-label="Handoff destination">
              <SelectValue placeholder="Choose Build it or Quick fix…" />
            </SelectTrigger>
            <SelectContent>
              {options.map((p) => (
                <SelectItem key={p.name} value={p.name}>
                  {handoffLabel(p.name)}
                  {p.description ? ` — ${p.description.split('\n')[0]}` : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {/* Surface a refused promotion (HTTP 400 PROMOTE_NOT_ALLOWED —
              unknown/unloaded destination, terminal task, demotion attempt).
              apiFetch throws an Error whose message is the response's
              detail.error, so the operator sees *why* it was refused and the
              dialog stays open to correct the selection. */}
          {mutation.isError && (
            <p role="alert" className="text-sm text-destructive">
              {mutation.error instanceof Error ? mutation.error.message : 'Promotion failed.'}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!selected || mutation.isPending}>
            {isAccepting ? `Accept → ${handoffLabel(selected)}` : 'Hand off'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
