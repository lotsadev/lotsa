import { useProcesses } from '@/hooks/use-processes'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

interface ProcessPickerProps {
  /** Selected process name, or ``undefined`` to dispatch against the default. */
  value: string | undefined
  onChange: (processName: string | undefined) => void
  className?: string
  disabled?: boolean
}

// Sentinel for the "use the server default" option. The radix Select can't use
// an empty string as an item value, so we map this sentinel to ``undefined``
// in/out of the controlled value.
const DEFAULT_VALUE = '__default__'

/**
 * Real process picker for the new-task surface (ADR-021).
 *
 * ``GET /api/processes`` returns every loaded process; ``POST /api/tasks``
 * accepts any of them. The list is sorted active-first by the API. A
 * "Default" option maps to ``undefined`` so the caller can always fall back to
 * the server's configured default — picking an explicit process never strands
 * the parent on a stale name if the active process changes between sessions.
 * Renders nothing when only one process is loaded — there's no choice to make.
 */
export function ProcessPicker({ value, onChange, className, disabled }: ProcessPickerProps) {
  const { data: processes } = useProcesses()

  if (!processes || processes.length <= 1) return null

  const active = processes.find((p) => p.is_active)
  // ``undefined`` (dispatch against the server default) renders as the Default
  // option; an explicit selection renders as that process's name.
  const selected = value ?? DEFAULT_VALUE

  return (
    <Select
      value={selected}
      onValueChange={(v) => onChange(v === DEFAULT_VALUE ? undefined : v)}
      disabled={disabled}
    >
      <SelectTrigger className={cn('h-9', className)} aria-label="Process">
        <SelectValue placeholder="Process" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT_VALUE}>
          Default
          {active && (
            <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {active.name}
            </span>
          )}
        </SelectItem>
        {processes.map((p) => (
          <SelectItem key={p.name} value={p.name}>
            {p.name}
            {p.is_active && (
              <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                default
              </span>
            )}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
