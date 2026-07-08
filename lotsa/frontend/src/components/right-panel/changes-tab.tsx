import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PatchDiff } from '@pierre/diffs/react'
import { GitPullRequest } from 'lucide-react'
import { fetchDiff } from '@/api/tasks'
import type { TaskStatus } from '@/api/types'
import { useTheme } from '@/hooks/use-theme'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

interface ChangesTabProps {
  taskId: string
  active: boolean
  status: TaskStatus
  prNumber?: number | string
  prUrl?: string
}

// Statuses under which the orchestrator deletes the worktree, so the diff
// endpoint returns an empty patch (200 { diff: "" }). `complete`/`abandoned`
// clean up via `_cleanup_worktree_if_done`; `archived` via the archive path.
const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set([
  'complete',
  'abandoned',
  'archived',
])

type Layout = 'unified' | 'split'

// A concatenated git diff begins each file section with `diff --git`. Split on
// that boundary so each chunk is a single-file patch — `PatchDiff` renders one
// file at a time (it throws on a multi-file patch).
function splitPatchByFile(patch: string): string[] {
  return patch
    .split(/(?=^diff --git )/m)
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.startsWith('diff --git '))
}

// First line of a file chunk (`diff --git a/x b/x`) is stable across polls, so
// it makes a good React key even if files reorder between refreshes.
function chunkKey(chunk: string, index: number): string {
  const firstLine = chunk.slice(0, chunk.indexOf('\n'))
  return firstLine || `file-${index}`
}

export function ChangesTab({ taskId, active, status, prNumber, prUrl }: ChangesTabProps) {
  const { theme } = useTheme()
  const [layout, setLayout] = useState<Layout>('unified')

  const { data } = useQuery({
    queryKey: ['diff', taskId],
    queryFn: () => fetchDiff(taskId),
    enabled: active,
    refetchInterval: active ? 5000 : false,
  })

  const files = useMemo(() => splitPatchByFile(data?.diff ?? ''), [data?.diff])

  // Totals for the header bar: hunk body lines only — `+++`/`---` are file
  // metadata, not changes.
  const stats = useMemo(() => {
    let additions = 0
    let deletions = 0
    for (const line of (data?.diff ?? '').split('\n')) {
      if (line.startsWith('+') && !line.startsWith('+++')) additions++
      else if (line.startsWith('-') && !line.startsWith('---')) deletions++
    }
    return { additions, deletions }
  }, [data?.diff])

  if (files.length === 0) {
    // A terminal task's worktree is gone, so the diff endpoint returns an empty
    // patch. Distinguish that from a brand-new active task (which also has no
    // diff yet) — otherwise both render the misleading "No changes yet".
    if (TERMINAL_STATUSES.has(status)) {
      const hasPr = typeof prNumber === 'number' || typeof prNumber === 'string'
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center text-sm text-muted-foreground">
          <p>We can't display changes for completed or archived tasks.</p>
          {hasPr &&
            (typeof prUrl === 'string' ? (
              <a
                href={prUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono hover:text-foreground transition-colors"
              >
                <GitPullRequest className="size-3 shrink-0" />
                <span>Check the PR #{prNumber}</span>
              </a>
            ) : (
              <span className="inline-flex items-center gap-1 font-mono">
                <GitPullRequest className="size-3 shrink-0" />
                <span>Check the PR #{prNumber}</span>
              </span>
            ))}
        </div>
      )
    }
    return (
      <div className="flex h-full items-center justify-center p-4 text-sm text-muted-foreground">
        No changes yet
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <div className="text-xs text-muted-foreground">
          {files.length} {files.length === 1 ? 'file' : 'files'} changed
          <span className="ml-2 text-green-600 dark:text-green-500">+{stats.additions}</span>
          <span className="ml-1 text-red-600 dark:text-red-500">−{stats.deletions}</span>
        </div>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          value={layout}
          onValueChange={(value) => value && setLayout(value as Layout)}
        >
          <ToggleGroupItem value="unified" className="text-xs">
            Unified
          </ToggleGroupItem>
          <ToggleGroupItem value="split" className="text-xs">
            Side-by-side
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      {/* min-h-0: a flex child's min-height defaults to its content size,
          which let the viewport grow to the full diff height (~48k px) —
          nothing to scroll within. min-h-0 clamps it to the panel. */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-3">
          {files.map((chunk, index) => (
            <PatchDiff
              key={chunkKey(chunk, index)}
              patch={chunk}
              // @pierre/diffs only offloads shiki highlighting to a Web Worker
              // when a `WorkerPoolContextProvider` supplies a bundler-specific
              // `workerFactory` — the package ships none. We don't mount that
              // provider, so highlighting runs on the main thread either way;
              // setting this makes that explicit rather than relying on the
              // absent-provider fallback (diffs here are a single task's branch,
              // not large enough to warrant the worker-pool setup).
              disableWorkerPool
              options={{
                diffStyle: layout,
                themeType: theme,
                // The package's default themes (pierre-dark) sit on pure
                // black, clashing with Lotsa's neutral-900 surface. The
                // github pair tracks the dashboard's look in both modes.
                theme: { dark: 'github-dark-default', light: 'github-light' },
                overflow: 'scroll',
                stickyHeader: true,
              }}
            />
          ))}
        </div>
      </ScrollArea>
    </div>
  )
}
