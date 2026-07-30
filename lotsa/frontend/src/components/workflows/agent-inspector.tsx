import { useQuery } from '@tanstack/react-query'
import { fetchAgentDetail } from '@/api/tasks'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'

interface AgentInspectorProps {
  workflow: string
  project: string | null
  promptName: string | null
  onClose: () => void
}

// ADR-044 Phase 6 — the node-detail inspector: a read-only view of an agent's
// declared properties + prompts. This is the future agent-editor form, minus
// write affordances — a props flip away from editable.
export function AgentInspector({ workflow, project, promptName, onClose }: AgentInspectorProps) {
  const { data, isLoading } = useQuery({
    queryKey: ['agent-detail', workflow, promptName, project],
    queryFn: () => fetchAgentDetail(workflow, promptName!, project ?? undefined),
    enabled: !!promptName,
    staleTime: Infinity,
  })

  return (
    <Sheet open={!!promptName} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="flex w-full flex-col gap-0 sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{promptName}</SheetTitle>
          <SheetDescription>Agent definition (read-only)</SheetDescription>
        </SheetHeader>
        {isLoading || !data ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <ScrollArea className="flex-1">
            <div className="space-y-4 p-4">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant="default">{data.agent_class}</Badge>
                {data.outcomes.map((o) => (
                  <Badge key={o} variant="outline">
                    {o}
                  </Badge>
                ))}
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted-foreground">needs_worktree</dt>
                <dd>{String(data.needs_worktree)}</dd>
                <dt className="text-muted-foreground">produces_changes</dt>
                <dd>{String(data.produces_changes)}</dd>
              </dl>
              <PromptBlock title="system.md" body={data.system_prompt} />
              <Separator />
              <PromptBlock title="user.md" body={data.user_prompt} />
            </div>
          </ScrollArea>
        )}
      </SheetContent>
    </Sheet>
  )
}

function PromptBlock({ title, body }: { title: string; body: string | null }) {
  return (
    <div className="space-y-1">
      <h4 className="text-xs font-semibold text-muted-foreground">{title}</h4>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2 font-mono text-xs">
        {body ?? '(none)'}
      </pre>
    </div>
  )
}
