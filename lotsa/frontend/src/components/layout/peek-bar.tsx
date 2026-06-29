import { ChevronUp, PanelTop } from 'lucide-react'
import type { TaskDetailFull } from '@/api/types'

interface PeekBarProps {
  data: TaskDetailFull
  onOpen: () => void
}

// Persistent peek bar above the chat input on mobile (mobile-first redesign,
// AC#3). Summarises the task's current status — current step and/or artifact
// count — and taps to open the right-panel bottom sheet. Purely
// presentational: it receives the already-fetched ``TaskDetailFull`` so it
// needs no data fetching of its own.
export function PeekBar({ data, onOpen }: PeekBarProps) {
  const step = data.task.current_step
  const artifactCount = Object.keys(data.artifacts ?? {}).length

  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="Open task panel"
      className="flex w-full shrink-0 items-center justify-between gap-2 border-t border-border bg-muted/40 px-4 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted"
    >
      <span className="flex min-w-0 items-center gap-2">
        <PanelTop className="size-3.5 shrink-0" />
        <span className="truncate">
          {step ? <span className="font-mono">{step}</span> : 'Task panel'}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {artifactCount > 0 && (
          <span>
            {artifactCount} artifact{artifactCount === 1 ? '' : 's'}
          </span>
        )}
        <ChevronUp className="size-3.5" />
      </span>
    </button>
  )
}
