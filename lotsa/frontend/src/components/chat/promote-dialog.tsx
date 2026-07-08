import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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

// ADR-027/043 — operator-driven handoff to a loaded destination process. If
// that process declares promotion_inputs, render one field per declared input,
// otherwise a single generic "promotion_context" field. The collected values
// become the initial_artifacts dict the destination's first step reads.
export function PromoteDialog({ taskId, open, onOpenChange }: PromoteDialogProps) {
  const queryClient = useQueryClient()
  const { data: processes } = useProcesses()
  const [destination, setDestination] = useState<string>('')
  const [fields, setFields] = useState<Record<string, string>>({})

  // Don't offer the chat process as a destination — promotion never targets
  // chat (no demotion; ADR-027 §7).
  const options = useMemo(
    () => (processes ?? []).filter((p) => p.name !== 'chat'),
    [processes]
  )
  const selected = options.find((p) => p.name === destination)
  const declaredInputs = selected?.promotion_inputs ?? []

  const mutation = useMutation({
    mutationFn: () => {
      const artifacts: Record<string, string> = {}
      if (declaredInputs.length > 0) {
        for (const input of declaredInputs) {
          if (fields[input.name]?.trim()) artifacts[input.name] = fields[input.name]
        }
      } else if (fields.promotion_context?.trim()) {
        artifacts.promotion_context = fields.promotion_context
      }
      return promoteTask(taskId, destination, Object.keys(artifacts).length ? artifacts : undefined)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskId] })
      onOpenChange(false)
      setDestination('')
      setFields({})
    },
  })

  const setField = (name: string, value: string) =>
    setFields((prev) => ({ ...prev, [name]: value }))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* On mobile the centered dialog caps to the viewport width and scrolls
          vertically so the destination picker + per-input fields never
          overflow a narrow screen. */}
      <DialogContent className="max-h-[85dvh] overflow-y-auto max-md:max-w-[calc(100vw-1rem)]">
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
            value={destination}
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

          {destination &&
            (declaredInputs.length > 0 ? (
              declaredInputs.map((input) => (
                <div key={input.name} className="flex flex-col gap-1">
                  <label className="text-xs font-medium text-muted-foreground">
                    {input.name}
                  </label>
                  <p className="text-xs text-muted-foreground">{input.description}</p>
                  <Input
                    value={fields[input.name] ?? ''}
                    onChange={(e) => setField(input.name, e.target.value)}
                    placeholder={`Content for ${input.name}…`}
                  />
                </div>
              ))
            ) : (
              <div className="flex flex-col gap-1">
                <label className="text-xs font-medium text-muted-foreground">
                  promotion_context
                </label>
                <p className="text-xs text-muted-foreground">
                  Optional context carried into the new process.
                </p>
                <Input
                  value={fields.promotion_context ?? ''}
                  onChange={(e) => setField('promotion_context', e.target.value)}
                  placeholder="Context for the new process…"
                />
              </div>
            ))}

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
          <Button onClick={() => mutation.mutate()} disabled={!destination || mutation.isPending}>
            Hand off
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
