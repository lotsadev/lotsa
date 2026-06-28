import { useProjects } from '@/hooks/use-projects'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

interface ProjectPickerProps {
  /** Selected project id, or ``undefined`` to let the server pick the default. */
  value: string | undefined
  onChange: (projectId: string | undefined) => void
  className?: string
  disabled?: boolean
}

/**
 * Project picker for the new-task surface (ADR-029).
 *
 * ``GET /api/projects`` returns every registered (YAML-declared) project;
 * ``POST /api/tasks`` accepts any of them as ``project``. Renders nothing when
 * only one project is registered — there's no choice to make, and the server
 * resolves the sole/``default`` project when the field is omitted. The parent
 * seeds ``value`` from the most-recently-used project (see EmptyState).
 */
export function ProjectPicker({ value, onChange, className, disabled }: ProjectPickerProps) {
  const { data: projects } = useProjects()

  if (!projects || projects.length <= 1) return null

  return (
    <Select value={value} onValueChange={(v) => onChange(v)} disabled={disabled}>
      <SelectTrigger className={cn('h-9', className)} aria-label="Project">
        <SelectValue placeholder="Project" />
      </SelectTrigger>
      <SelectContent>
        {projects.map((p) => (
          <SelectItem key={p.id} value={p.id}>
            {p.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
