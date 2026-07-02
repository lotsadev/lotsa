import { useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { useTasks } from '@/hooks/use-tasks'
import { useProjects } from '@/hooks/use-projects'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuSkeleton,
} from '@/components/ui/sidebar'
import { cn } from '@/lib/utils'
import type { TaskSummary } from '@/api/types'

interface SidebarProps {
  selectedTaskId: string | null
  onSelectTask: (taskId: string | null) => void
}

// Sentinel for the "show every project" filter option (radix Select can't use
// an empty string as a value).
const ALL_PROJECTS = '__all__'

function StatusIndicator({ task }: { task: TaskSummary }) {
  switch (task.status) {
    case 'working':
      return (
        <span className="flex items-center gap-1 text-[10px] text-primary shrink-0">
          <span className="relative flex size-1.5">
            <span className="absolute size-full animate-ping rounded-full bg-primary opacity-75" />
            <span className="relative size-1.5 rounded-full bg-primary" />
          </span>
          {task.current_step ?? 'Running'}
        </span>
      )
    case 'needs_input':
      return <Badge variant="outline" className="border-amber-500/40 text-amber-500 text-[10px] h-5 shrink-0">Input</Badge>
    case 'waiting':
      return <Badge variant="outline" className="border-amber-500/40 text-amber-500 text-[10px] h-5 shrink-0">Review</Badge>
    case 'waiting_for_pr': {
      const prNumber = task.metadata?.pr_number
      return <Badge variant="outline" className="border-amber-500/40 text-amber-500 text-[10px] h-5 shrink-0">{prNumber ? `PR #${prNumber}` : 'PR'}</Badge>
    }
    case 'awaiting_operator':
      return <Badge variant="outline" className="border-amber-500/40 text-amber-500 text-[10px] h-5 shrink-0">Awaiting you</Badge>
    case 'complete':
      return <Badge variant="outline" className="border-primary/40 text-primary text-[10px] h-5 shrink-0">Done</Badge>
    case 'abandoned':
      return <Badge variant="secondary" className="text-[10px] h-5 shrink-0">Abandoned</Badge>
    case 'archived':
      return <Badge variant="secondary" className="text-[10px] h-5 shrink-0">Archived</Badge>
    case 'blocked':
      return <Badge variant="destructive" className="text-[10px] h-5 shrink-0">Blocked</Badge>
    default:
      return null
  }
}

function TaskItem({
  task,
  isActive,
  onClick,
  projectLabel,
}: {
  task: TaskSummary
  isActive: boolean
  onClick: () => void
  // The project's display name, shown as a badge. Omitted when only one
  // project is registered (the badge would be noise).
  projectLabel?: string
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex w-full cursor-pointer flex-col gap-1 border-b border-border px-3 py-2.5 text-left transition-colors',
        'hover:bg-accent',
        isActive && 'bg-accent border-l-2 border-l-primary',
      )}
    >
      {/* Row 1: Title + status */}
      <div className="flex items-start justify-between gap-2">
        <span className="flex-1 text-sm font-semibold leading-snug line-clamp-2">
          {task.title}
        </span>
        <StatusIndicator task={task} />
      </div>

      {/* Row 2: Step + elapsed — muted preview */}
      <span className="text-[13px] text-muted-foreground line-clamp-1">
        {projectLabel && (
          <Badge variant="secondary" className="mr-1.5 text-[10px] h-5 align-middle">
            {projectLabel}
          </Badge>
        )}
        {task.current_step && <span className="font-mono">{task.current_step}</span>}
        {task.status === 'working' && task.elapsed_s > 0 && (
          <span className="ml-1 opacity-70">{task.elapsed_s}s</span>
        )}
        {/* ADR-017 soft-timeout dot: yellow at warn, red at over. */}
        {task.timeout_status === 'warn' && (
          <span
            className="ml-1.5 inline-block size-1.5 rounded-full bg-amber-500 align-middle"
            title="Running longer than expected"
          />
        )}
        {task.timeout_status === 'over' && (
          <span
            className="ml-1.5 inline-block size-1.5 rounded-full bg-destructive align-middle"
            title="Running much longer than expected"
          />
        )}
        {!task.current_step && <span className="capitalize">{task.state.replace(/_/g, ' ')}</span>}
      </span>
    </button>
  )
}

export function AppSidebar({ selectedTaskId, onSelectTask }: SidebarProps) {
  const { data: tasks, isLoading } = useTasks()
  const { data: projects } = useProjects()
  const [projectFilter, setProjectFilter] = useState<string>(ALL_PROJECTS)

  // Only surface the project badge + filter when there's more than one project
  // (otherwise both are noise).
  const multiProject = (projects?.length ?? 0) > 1
  const projectNames = useMemo(
    () => new Map((projects ?? []).map((p) => [p.id, p.name])),
    [projects],
  )
  const projectLabelFor = (task: TaskSummary) =>
    multiProject ? projectNames.get(task.project_id) ?? task.project_id : undefined

  const visibleTasks =
    projectFilter === ALL_PROJECTS
      ? tasks
      : tasks?.filter((t) => t.project_id === projectFilter)

  const activeTasks =
    visibleTasks?.filter(
      (t) => t.status !== 'complete' && t.status !== 'abandoned' && t.status !== 'archived',
    ) ?? []
  const completedTasks =
    visibleTasks?.filter((t) => t.status === 'complete' || t.status === 'abandoned') ?? []
  // Archived tasks are hidden from the default list and shown in their own
  // section below Completed (interim — the full list redesign is deferred).
  const archivedTasks = visibleTasks?.filter((t) => t.status === 'archived') ?? []

  // Reveal the EmptyState start screen (app-layout renders it when no task is
  // selected) so the user can describe a task before anything is created. No
  // task is created or dispatched until they submit from EmptyState.
  const handleNewTask = () => onSelectTask(null)

  return (
    <>
      <SidebarHeader className="border-b border-sidebar-border px-3 py-2.5 gap-2">
        <span className="text-sm font-semibold">Tasks</span>
        {multiProject && (
          <Select value={projectFilter} onValueChange={setProjectFilter}>
            <SelectTrigger className="h-8 text-xs" aria-label="Filter by project">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_PROJECTS}>All projects</SelectItem>
              {(projects ?? []).map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </SidebarHeader>

      <SidebarContent>
        {isLoading && (
          <SidebarGroup className="p-0">
            <SidebarMenu>
              {Array.from({ length: 5 }).map((_, i) => (
                <SidebarMenuItem key={i}>
                  <SidebarMenuSkeleton />
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        )}

        {!isLoading && activeTasks.length > 0 && (
          <SidebarGroup className="p-0">
            <div className="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Active
            </div>
            {activeTasks.map(task => (
              <TaskItem
                key={task.id}
                task={task}
                isActive={task.id === selectedTaskId}
                onClick={() => onSelectTask(task.id)}
                projectLabel={projectLabelFor(task)}
              />
            ))}
          </SidebarGroup>
        )}

        {!isLoading && completedTasks.length > 0 && (
          <SidebarGroup className="p-0">
            <div className="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Completed
            </div>
            {completedTasks.map(task => (
              <TaskItem
                key={task.id}
                task={task}
                isActive={task.id === selectedTaskId}
                onClick={() => onSelectTask(task.id)}
                projectLabel={projectLabelFor(task)}
              />
            ))}
          </SidebarGroup>
        )}

        {!isLoading && archivedTasks.length > 0 && (
          <SidebarGroup className="p-0">
            <div className="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Archived
            </div>
            {archivedTasks.map(task => (
              <TaskItem
                key={task.id}
                task={task}
                isActive={task.id === selectedTaskId}
                onClick={() => onSelectTask(task.id)}
                projectLabel={projectLabelFor(task)}
              />
            ))}
          </SidebarGroup>
        )}
      </SidebarContent>

      <SidebarFooter className="border-t border-sidebar-border p-2">
        <Button
          variant="outline"
          size="sm"
          className="w-full gap-1.5"
          onClick={handleNewTask}
        >
          <Plus className="size-4" />
          New Task
        </Button>
      </SidebarFooter>
    </>
  )
}
