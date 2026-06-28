import { useMemo, useState } from 'react'
import { Send } from 'lucide-react'
import { createTask } from '@/api/tasks'
import { AutoGrowTextarea } from '@/components/ui/auto-grow-textarea'
import { Button } from '@/components/ui/button'
import { ProcessPicker } from '@/components/process-picker'
import { ProjectPicker } from '@/components/project-picker'
import { useProjects } from '@/hooks/use-projects'

interface EmptyStateProps {
  onTaskCreated: (taskId: string) => void
}

// Persist the operator's last-used project so the picker defaults to it
// (ADR-029 §5 — "default: most recently used").
const LAST_PROJECT_KEY = 'lotsa:lastProject'

export function EmptyState({ onTaskCreated }: EmptyStateProps) {
  const [message, setMessage] = useState('')
  const [process, setProcess] = useState<string | undefined>(undefined)
  // An explicit picker selection; `undefined` means "use the remembered
  // default" computed below (no effect/setState — derived during render).
  const [projectOverride, setProjectOverride] = useState<string | undefined>(undefined)
  const [isCreating, setIsCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { data: projects } = useProjects()

  // Default to the most-recently-used project (persisted in localStorage).
  // On first load, before any project has been picked, there's no remembered
  // value, so this falls back to the first entry — the list arrives ordered by
  // id ASC from ``GET /api/projects``, so that's the alphabetically-first
  // project, not a recency pick. Derived, so there's no setState-in-effect.
  const rememberedDefault = useMemo(() => {
    if (!projects || projects.length === 0) return undefined
    const last = localStorage.getItem(LAST_PROJECT_KEY)
    return last && projects.some((p) => p.id === last) ? last : projects[0].id
  }, [projects])
  const project = projectOverride ?? rememberedDefault

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    const trimmed = message.trim()
    if (!trimmed || isCreating) return

    setIsCreating(true)
    setError(null)
    try {
      const result = await createTask({ message: trimmed, process, project })
      if (project) localStorage.setItem(LAST_PROJECT_KEY, project)
      setMessage('')
      onTaskCreated(result.task.id)
    } catch {
      setError('Failed to create task')
    } finally {
      setIsCreating(false)
    }
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 px-8">
      <div className="flex flex-col items-center gap-2 text-center">
        <h2 className="text-xl font-semibold text-foreground">
          What would you like to build?
        </h2>
        <p className="max-w-md text-sm text-muted-foreground">
          Describe a task and Lotsa will plan, build, and deliver it with full
          governance.
        </p>
      </div>

      {error && (
        <p className="text-sm text-destructive">{error}</p>
      )}

      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-lg items-end gap-2"
      >
        <ProjectPicker
          value={project}
          onChange={setProjectOverride}
          disabled={isCreating}
          className="w-40 shrink-0"
        />
        <ProcessPicker
          value={process}
          onChange={setProcess}
          disabled={isCreating}
          className="w-40 shrink-0"
        />
        <AutoGrowTextarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onSubmit={() => handleSubmit()}
          placeholder="Describe your task..."
          disabled={isCreating}
          className="flex-1"
        />
        <Button type="submit" size="icon" disabled={!message.trim() || isCreating}>
          <Send className="size-4" />
        </Button>
      </form>
    </div>
  )
}
