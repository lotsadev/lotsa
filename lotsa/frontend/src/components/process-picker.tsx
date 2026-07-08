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

// ADR-043 — friendly labels for the two-phase Think→Execute catalog. The
// picker is a *mode switcher*: Chat (Think) is the default entry mode; the
// operator can flip to Build / Quick fix (Execute) on the first turn. Inline
// or custom processes fall through to their raw name.
const MODE_LABELS: Record<string, string> = {
  chat: 'Chat',
  build: 'Build',
  fix: 'Quick fix',
}

const modeLabel = (name: string) => MODE_LABELS[name] ?? name

/**
 * Mode switcher for the new-task surface (ADR-021 / ADR-043).
 *
 * ``GET /api/processes`` returns every loaded process; ``POST /api/tasks``
 * accepts any of them. Chat is the default entry mode (``undefined`` → the
 * server's configured default, which is ``chat``); the operator can pick Build
 * or Quick fix — or any inline process — before the first turn. Picking the
 * Default option never strands the parent on a stale name if the active process
 * changes between sessions. Renders nothing when only one process is loaded.
 */
export function ProcessPicker({ value, onChange, className, disabled }: ProcessPickerProps) {
  const { data: processes } = useProcesses()

  if (!processes || processes.length <= 1) return null

  const active = processes.find((p) => p.is_active)
  // ``undefined`` (dispatch against the server default) renders as the Default
  // option; an explicit selection renders as that process's mode label.
  const selected = value ?? DEFAULT_VALUE

  return (
    <Select
      value={selected}
      onValueChange={(v) => onChange(v === DEFAULT_VALUE ? undefined : v)}
      disabled={disabled}
    >
      <SelectTrigger className={cn('h-9', className)} aria-label="Mode">
        <SelectValue placeholder="Mode" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT_VALUE}>
          Default
          {active && (
            <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {modeLabel(active.name)}
            </span>
          )}
        </SelectItem>
        {processes.map((p) => (
          <SelectItem key={p.name} value={p.name}>
            {modeLabel(p.name)}
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
