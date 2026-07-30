import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Bot, GitPullRequest, Radio, Wrench, CircleDot } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { WorkflowRFNode } from './workflow-graph-layout'

// ADR-044 Phase 6 — a workflow graph node, styled with shadcn/Tailwind tokens
// (ADR-012 — no new visual language). Read-only in v1: the connection handles
// exist (so the viewer→editor flip only has to reveal them + wire onConnect)
// but are hidden. Clicking an agent node opens the inspector.

const TYPE_ICON: Record<string, typeof Bot> = {
  agent: Bot,
  action: Wrench,
  monitor: Radio,
  terminal: CircleDot,
}

function WorkflowNodeImpl({ data }: NodeProps<WorkflowRFNode>) {
  const isTerminal = data.type === 'terminal'
  const isAgent = data.type === 'agent' && data.agent !== null
  const Icon = data.prompt_name === 'pr-fix' ? GitPullRequest : (TYPE_ICON[data.type] ?? Bot)

  const clickable = isAgent && data.prompt_name
  const onClick = clickable ? () => data.onInspect?.(data.prompt_name!) : undefined

  return (
    <div
      onClick={onClick}
      className={cn(
        'flex w-[200px] flex-col gap-1 rounded-lg border px-3 py-2 text-left shadow-sm transition-colors',
        isTerminal
          ? 'border-dashed border-border bg-muted text-muted-foreground'
          : 'border-border bg-card text-card-foreground',
        clickable && 'cursor-pointer hover:border-primary/50 hover:bg-accent',
      )}
    >
      {/* Hidden handles — revealed when editor mode lands. */}
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} isConnectable={false} />
      <div className="flex items-center gap-1.5">
        <Icon className="size-4 shrink-0 opacity-70" />
        <span className="truncate text-sm font-medium">{data.id}</span>
      </div>
      {isAgent && data.agent && (
        <div className="flex flex-wrap items-center gap-1">
          <Badge variant={data.is_gate ? 'default' : 'secondary'} className="text-[10px]">
            {data.agent.agent_class}
          </Badge>
          {data.agent.outcomes.map((o) => (
            <Badge key={o} variant="outline" className="text-[10px]">
              {o}
            </Badge>
          ))}
        </div>
      )}
      {!isAgent && !isTerminal && (
        <span className="text-xs text-muted-foreground">{data.type}</span>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} isConnectable={false} />
    </div>
  )
}

export const WorkflowNode = memo(WorkflowNodeImpl)
