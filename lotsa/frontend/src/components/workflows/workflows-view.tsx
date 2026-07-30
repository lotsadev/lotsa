import { useState } from 'react'
import { Workflow as WorkflowIcon } from 'lucide-react'
import { useProjects } from '@/hooks/use-projects'
import { useWorkflows } from '@/hooks/use-workflows'
import { useWorkflowGraph } from '@/hooks/use-workflow-graph'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import { WorkflowGraph } from './workflow-graph'

// ADR-044 Phase 6 — the workflows viewer surface: a project selector, the list
// of that project's workflows (bundled + repo, each with a provenance badge),
// and the selected workflow's read-only graph. Read-only precursors to the
// eventual editor (a workflows library + a board).
export function WorkflowsView() {
  const { data: projects } = useProjects()
  // The operator's explicit pick, or (derived during render — no effect) the
  // first project once the list loads. Deriving rather than storing avoids a
  // setState-in-effect cascade.
  const [pickedProject, setPickedProject] = useState<string | undefined>(undefined)
  const projectId = pickedProject ?? projects?.[0]?.id

  const { data: workflows, isLoading } = useWorkflows(projectId)
  const [selected, setSelected] = useState<string | null>(null)

  const sorted = [...(workflows ?? [])].sort((a, b) => a.name.localeCompare(b.name))

  const { data: graph } = useWorkflowGraph(selected ?? undefined, projectId)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border p-3">
        <WorkflowIcon className="size-5" />
        <h2 className="text-base font-semibold">Workflows</h2>
        {projects && projects.length > 1 && (
          <Select value={projectId} onValueChange={(v) => { setPickedProject(v); setSelected(null) }}>
            <SelectTrigger className="ml-auto w-48">
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
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Workflow list */}
        <div className="w-64 shrink-0 border-r border-border">
          <ScrollArea className="h-full">
            <ul className="p-2">
              {isLoading && (
                <li className="space-y-2 p-2">
                  <Skeleton className="h-8 w-full" />
                  <Skeleton className="h-8 w-full" />
                </li>
              )}
              {sorted.map((wf) => (
                <li key={wf.name}>
                  <button
                    onClick={() => setSelected(wf.name)}
                    className={cn(
                      'flex w-full flex-col items-start gap-1 rounded-md px-2 py-2 text-left transition-colors hover:bg-accent',
                      selected === wf.name && 'bg-accent',
                    )}
                  >
                    <span className="text-sm font-medium">{wf.name}</span>
                    {wf.source === 'repo' ? (
                      <Badge variant="secondary" className="text-[10px]">
                        Repo · {wf.project}
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="text-[10px]">
                        Bundled
                      </Badge>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </ScrollArea>
        </div>

        {/* Graph canvas */}
        <div className="min-w-0 flex-1">
          {graph ? (
            <WorkflowGraph graph={graph} />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {selected ? 'Loading graph…' : 'Select a workflow to view its graph'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
