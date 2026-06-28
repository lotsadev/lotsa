import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchAgentActivity } from '@/api/tasks'
import { useTask } from '@/hooks/use-task'
import { formatRelativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import type { AgentActivityEvent } from '@/api/types'

interface ActivityTabProps {
  taskId: string
  active: boolean
}

// Idle threshold (ADR-017 §5): a working agent with no new event for this long
// is flagged so operators can spot a possible stall.
const IDLE_MS = 120_000

const KIND_LABEL: Record<AgentActivityEvent['kind'], string> = {
  thinking: 'thinking',
  tool_use: 'tool',
  tool_result: 'result',
  text: 'text',
  system: 'system',
}

const KIND_CLASS: Record<AgentActivityEvent['kind'], string> = {
  thinking: 'text-muted-foreground',
  tool_use: 'text-primary',
  tool_result: 'text-amber-600 dark:text-amber-500',
  text: 'text-foreground',
  system: 'text-muted-foreground',
}

export function ActivityTab({ taskId, active }: ActivityTabProps) {
  const { data: taskData } = useTask(taskId)
  const working = taskData?.task.status === 'working'

  // Accumulated events (oldest-first by index) and the incremental cursor.
  // State is reset two ways: the parent remounts this component with
  // key={taskId} on task switch, and the effect below resets in place when the
  // *same* task is re-dispatched under a new session_id (a retry — see below).
  const [events, setEvents] = useState<AgentActivityEvent[]>([])
  const [since, setSince] = useState(0)
  const [supported, setSupported] = useState(true)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const { data } = useQuery({
    // `since` in the key drives the incremental fetch: advancing it re-queries
    // from the new cursor; a poll that returns nothing leaves it unchanged and
    // the interval keeps polling the same key. `sessionId` is in the key too so
    // a retry (new session_id, cursor reset to 0 below) buckets into a fresh
    // cache entry — the prior session's stale-cursor payload can't bleed back
    // in while the refetch under the reset cursor is in flight.
    queryKey: ['activity', taskId, sessionId, since],
    queryFn: () => fetchAgentActivity(taskId, since),
    enabled: active,
    refetchInterval: active ? (working ? 2000 : 30000) : false,
  })

  useEffect(() => {
    if (!data) return
    setSupported(data.runner_supports_activity)
    setLoaded(true)

    // A retry re-dispatches the *same task* under a new session_id, but
    // key={taskId} only remounts on task switch, not on retry. The prior
    // session's events and `since` cursor index a different JSONL file; left in
    // place, a stale cursor at index N filters out the new session's first N
    // events forever (the server returns only index >= N). When the session_id
    // changes, drop the prior accumulator and restart paging from 0. Skip
    // merging the triggering payload — it was fetched under the stale cursor;
    // the refetch under the reset cursor (fresh queryKey) re-reads the new
    // session from index 0.
    if (sessionId !== null && data.session_id !== null && data.session_id !== sessionId) {
      setSessionId(data.session_id)
      setEvents([])
      setSince(0)
      setExpanded(new Set())
      return
    }
    setSessionId(data.session_id)

    if (data.events.length > 0) {
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.index))
        const fresh = data.events.filter((e) => !seen.has(e.index))
        return fresh.length > 0 ? [...prev, ...fresh] : prev
      })
      if (data.next_index > since) setSince(data.next_index)
    }
  }, [data, since, sessionId])

  const newestFirst = useMemo(() => [...events].reverse(), [events])

  // Idle is a time-since-last-event check, but `events` stays frozen exactly
  // when the agent stalls (empty polls short-circuit the setEvents above), so a
  // memo keyed only on `events` would never recompute and the banner would
  // never appear. Tick a `now` value on an interval while working so the check
  // re-evaluates against the clock, not against event arrivals.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active || !working) return
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [active, working])

  const idle = useMemo(() => {
    if (!working || events.length === 0) return false
    const last = events[events.length - 1]
    return now - new Date(last.timestamp).getTime() > IDLE_MS
  }, [working, events, now])

  const toggle = (index: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })

  if (loaded && !supported) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
        Activity is unavailable for this runner.
      </div>
    )
  }

  if (loaded && sessionId === null) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
        Agent has not dispatched yet.
      </div>
    )
  }

  if (events.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-sm text-muted-foreground">
        No activity yet
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {idle && (
        <div className="shrink-0 border-b border-border bg-amber-500/10 px-3 py-1.5 text-xs text-amber-600 dark:text-amber-500">
          Agent looks idle — no new activity for over 2 minutes.
        </div>
      )}
      <div className="flex-1 overflow-y-auto min-h-0">
        <ul className="divide-y divide-border">
          {newestFirst.map((ev) => {
            const isOpen = expanded.has(ev.index)
            return (
              <li key={ev.index}>
                <button
                  onClick={() => toggle(ev.index)}
                  className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-accent"
                >
                  <span className={cn('mt-0.5 w-14 shrink-0 font-mono text-[10px] uppercase', KIND_CLASS[ev.kind])}>
                    {KIND_LABEL[ev.kind]}
                  </span>
                  <span className="flex-1 text-xs leading-snug break-words">
                    {ev.summary || <span className="text-muted-foreground">(empty)</span>}
                    {ev.truncated && <span className="ml-1 text-muted-foreground">…</span>}
                  </span>
                  <span className="mt-0.5 shrink-0 text-[10px] text-muted-foreground">
                    {formatRelativeTime(ev.timestamp)}
                  </span>
                </button>
                {isOpen && ev.detail && (
                  <pre className="overflow-x-auto bg-muted/40 px-3 py-2 text-[11px] leading-relaxed">
                    {JSON.stringify(ev.detail, null, 2)}
                  </pre>
                )}
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
