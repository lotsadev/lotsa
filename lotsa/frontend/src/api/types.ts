export type TaskStatus =
  | 'working'
  | 'waiting'
  | 'waiting_for_pr'
  | 'needs_input'
  | 'blocked'
  | 'complete'
  | 'abandoned'
  | 'archived'

export interface TaskSummary {
  id: string
  title: string
  state: string  // legacy field; kept for sidebar's "PR #N" / "Abandoned" badges
  priority: number
  created_at: string
  status: TaskStatus
  current_step: string | null
  is_conversational: boolean
  elapsed_s: number
  project_id: string  // ADR-029 — for the project badge + filter
  // ADR-017 soft-timeout indicator. 'ok' → no dot, 'warn' → yellow, 'over' → red.
  timeout_status: 'ok' | 'warn' | 'over'
  metadata: Record<string, unknown>
}

export interface TaskDetail extends TaskSummary {
  body: string
  flow_name: string
  work_dir: string
  project_name: string  // ADR-029 — surfaced in the task detail view
  project_path: string
}

export interface Message {
  id: number
  task_id: string
  role: 'user' | 'agent' | 'system'
  step_name: string
  content: string
  type: 'chat' | 'output' | 'stage_transition' | 'feedback' | 'question' | 'answer' | 'error' | 'status_change' | 'stderr' | 'artifact' | 'artifact_seeded' | 'process_promotion'
  metadata: Record<string, unknown>
  created_at: string
}

export interface FlowStep {
  name: string
  conversational: boolean
  evaluate: boolean
  output: string | null
  inputs: string[]
}

export interface Flow {
  name: string
  steps: FlowStep[]
  gate_states: string[]
}

export interface Totals {
  total_duration_s: number
  total_tokens: number
  total_cost_usd: number
  display: string
}

// One guard-override action currently applicable to a task (ADR-019).
// Rendered as a button in the chat input's action row.
export interface AvailableOverride {
  guard_name: string
  label: string
  description: string
}

export interface TaskDetailFull {
  task: TaskDetail
  messages: Message[]
  question: string | null
  flow: Flow | null
  artifacts: Record<string, string>
  next_step_name: string | null
  totals: Totals
  available_overrides: AvailableOverride[]
}

// One entry from GET /api/processes — every process the orchestrator
// has loaded (bundled + any defined inline in lotsa.yaml). The active
// flag marks which one new tasks dispatch against today; per-task
// simultaneous dispatch is a tracked follow-up. The dropdown surfacing
// this catalog can render the non-active entries informationally
// ("restart with --process X to use this") until the follow-up lands.
export interface PromotionInput {
  name: string
  description: string
}

export interface Process {
  name: string
  is_active: boolean
  is_default: boolean
  step_names: string[]
  description: string | null
  promotion_inputs: PromotionInput[]
}

// One entry from GET /api/projects — a registered project (repo) offered on
// the new-task picker (ADR-029).
export interface Project {
  id: string
  name: string
  path: string
}

// ADR-017 — one in-flight agent activity event surfaced by
// GET /api/tasks/{id}/agent-activity.
export interface AgentActivityEvent {
  index: number
  timestamp: string
  kind: 'thinking' | 'tool_use' | 'tool_result' | 'text' | 'system'
  summary: string
  detail: Record<string, unknown> | null
  truncated: boolean
}

export interface AgentActivity {
  session_id: string | null
  runner_supports_activity: boolean
  session_complete: boolean
  events: AgentActivityEvent[]
  next_index: number
}
