import { apiFetch } from './client'
import type { TaskSummary, TaskDetailFull, Message, Flow, Process, Project, AgentActivity } from './types'

export const fetchTasks = () => apiFetch<TaskSummary[]>('/api/tasks')

export const fetchTaskDetail = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}`)

// ADR-017 — incremental fetch of in-flight agent activity. Pass the last
// `next_index` as `since` to avoid re-transferring events already held.
export const fetchAgentActivity = (taskId: string, since = 0) =>
  apiFetch<AgentActivity>(`/api/tasks/${taskId}/agent-activity?since=${since}`)

export const fetchMessages = (taskId: string) =>
  apiFetch<Message[]>(`/api/tasks/${taskId}/messages`)

export const fetchDiff = (taskId: string) =>
  apiFetch<{ diff: string | null }>(`/api/tasks/${taskId}/diff`)

export const fetchFlow = () => apiFetch<Flow>('/api/flow')

export const fetchProcesses = () => apiFetch<Process[]>('/api/processes')

export const fetchProjects = () => apiFetch<Project[]>('/api/projects')

export const createTask = (data: {
  message?: string
  title?: string
  body?: string
  process?: string
  project?: string
}) =>
  apiFetch<TaskDetailFull>('/api/tasks', {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const approveTask = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/approve`, { method: 'POST' })

export const reviseTask = (taskId: string, feedback: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/revise`, {
    method: 'POST',
    body: JSON.stringify({ feedback }),
  })

export const answerTask = (taskId: string, answer: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/answer`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  })

export const sendMessage = (taskId: string, message: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/message`, {
    method: 'POST',
    body: JSON.stringify({ message }),
  })

export const blockTask = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/block`, { method: 'POST' })

export const retryTask = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/retry`, { method: 'POST' })

export const jumpToStep = (taskId: string, stepName: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/jump`, {
    method: 'POST',
    body: JSON.stringify({ step_name: stepName }),
  })

// Acknowledge a fired guard (ADR-019). Resets the guard's block, writes an
// audit row, and resumes the step in one action (acknowledge_override calls
// retry() downstream — ADR-019 revised 2026-06-16). The request carries only
// the guard name — the operator-reason field was removed (ADR-019 revised
// 2026-07-02); rationale, when wanted, is a normal chat message.
export const acknowledgeOverride = (taskId: string, guardName: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/acknowledge-override`, {
    method: 'POST',
    body: JSON.stringify({ guard_name: guardName }),
  })

// Stop the running agent and park the task at blocked (Retry resumes it).
export const stopAgent = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/stop`, { method: 'POST' })

// Terminal: stop the agent, tear down the worktree/branch, move to archived.
export const archiveTask = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/archive`, { method: 'POST' })

// ADR-043 — operator escape hatch: drive a non-terminal task to `complete`
// (for GitHub-less setups / a task parked at `awaiting_operator`).
export const markCompleteTask = (taskId: string) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/mark-complete`, { method: 'POST' })

// ADR-027 — switch a task to a different loaded process mid-life. The seeded
// artifacts are keyed by the destination's promotion_inputs (or a generic
// "promotion_context"), which the destination's first step reads.
export const promoteTask = (
  taskId: string,
  toProcess: string,
  initialArtifacts?: Record<string, string>
) =>
  apiFetch<TaskDetailFull>(`/api/tasks/${taskId}/promote`, {
    method: 'POST',
    body: JSON.stringify({
      to_process: toProcess,
      initial_artifacts: initialArtifacts ?? null,
    }),
  })
