import { useMemo, useState } from 'react'
import { Send } from 'lucide-react'
import { createTask, dispatchTask, uploadAttachment } from '@/api/tasks'
import { AutoGrowTextarea } from '@/components/ui/auto-grow-textarea'
import { Button } from '@/components/ui/button'
import { AttachmentPicker } from '@/components/attachment-picker'
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
  const [files, setFiles] = useState<File[]>([])
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
      // The task must exist before attachments can be posted to it, so create
      // first, then upload each file sequentially (the count cap is per task).
      // When there are files, DEFER the first dispatch: the agent's first step
      // materializes attachments from tasks.metadata, so it must not run until
      // the uploads have landed. We start it explicitly via dispatchTask() once
      // the files are in. With no files, the task dispatches immediately.
      const hasFiles = files.length > 0
      const result = await createTask({ message: trimmed, process, project, defer_dispatch: hasFiles })
      // The task now exists server-side. A subsequent attachment failure must
      // NOT strand it on this form — returning early here would leave an
      // orphaned task the operator can't see and a resubmit would create a
      // duplicate. So on an upload error we record it but still start + navigate
      // to the created task, where the operator can re-attach via the chat input.
      let attachError: string | null = null
      for (const f of files) {
        try {
          await uploadAttachment(result.task.id, f)
        } catch (e) {
          attachError = `Failed to attach ${f.name}: ${(e as Error).message}`
          break
        }
      }
      // Release the deferred first dispatch now that uploads have run (even on a
      // partial failure — a stranded, never-dispatched task is worse than one
      // that starts with whatever attached). Only needed when we deferred.
      if (hasFiles) await dispatchTask(result.task.id)
      if (project) localStorage.setItem(LAST_PROJECT_KEY, project)
      setMessage('')
      setFiles([])
      if (attachError) setError(attachError)
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
        className="flex w-full max-w-2xl flex-col gap-2"
      >
        <AutoGrowTextarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onSubmit={() => handleSubmit()}
          placeholder="Describe your task..."
          disabled={isCreating}
          minRows={7}
          className="w-full"
        />
        <AttachmentPicker files={files} onChange={setFiles} disabled={isCreating} />
        {/* Below 768px the pickers stack full-width so they + Send don't
            overflow; at md they return to the side-by-side fixed-width row. */}
        <div className="flex flex-col gap-2 md:flex-row md:items-center">
          <ProjectPicker
            value={project}
            onChange={setProjectOverride}
            disabled={isCreating}
            className="w-full md:w-40 md:shrink-0"
          />
          <ProcessPicker
            value={process}
            onChange={setProcess}
            disabled={isCreating}
            className="w-full md:w-40 md:shrink-0"
          />
          <div className="hidden md:block md:flex-1" />
          <Button
            type="submit"
            size="icon"
            disabled={!message.trim() || isCreating}
            className="shrink-0 self-end md:self-auto"
          >
            <Send className="size-4" />
          </Button>
        </div>
      </form>
    </div>
  )
}
