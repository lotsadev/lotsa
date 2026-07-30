export type TaskStatus =
  | 'working'
  | 'waiting'
  | 'waiting_for_pr'
  | 'awaiting_operator'
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
  type: 'chat' | 'output' | 'stage_transition' | 'feedback' | 'question' | 'answer' | 'error' | 'status_change' | 'stderr' | 'artifact' | 'artifact_seeded' | 'process_promotion' | 'pr_decision'
  metadata: Record<string, unknown>
  created_at: string
}

export interface FlowStep {
  name: string
  conversational: boolean
  evaluate: boolean
  output: string | null
  inputs: string[]
  // Operator can Accept this step to advance it (output artifact, evaluate gate,
  // or a conversational step with a forward advance rule, e.g. verify). Backend
  // is the source of truth (ResolvedJob.is_approval_gate).
  is_gate: boolean
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
  // ADR-044 Phase 4 — where the workflow may be selected ('start' | 'hand-off').
  // The hand-off dialog filters destinations on 'hand-off' instead of the name
  // 'chat'. Optional so older payloads without the field still typecheck.
  invocable?: string[]
  // ADR-044 Phase 6 — provenance for the workflow viewer badge. 'bundled'
  // (in-wheel / inline) vs 'repo' (a project's .lotsa/workflows); repo entries
  // carry their owning project. Optional so older payloads still typecheck.
  source?: 'bundled' | 'repo'
  project?: string | null
}

// One entry from GET /api/projects — a registered project (repo) offered on
// the new-task picker (ADR-029).
export interface Project {
  id: string
  name: string
  path: string
}

// One prompt-attachment record (Path A) from
// GET/POST /api/tasks/{id}/attachments. Bytes live on disk; this is metadata.
export interface Attachment {
  filename: string
  rel_path: string
  mime: string
  size_bytes: number
  created_at: string
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

// ── ADR-044 Phase 6 — read-only workflow graph viewer ───────────────

// The declared properties of the agent resolved for a graph node. `agent_class`
// (not `class`, reserved) carries the worker|gate axis; null on a node means an
// action/monitor step (no agent).
export interface AgentInfo {
  name: string
  agent_class: string
  outcomes: string[]
  needs_worktree: boolean
  produces_changes: boolean
}

// One node of a workflow flow — a job with its resolved agent + hooks. The
// backend always sends the enrichment fields (conversational/evaluate/output/
// inputs), but the viewer only strictly needs id/type/agent/is_gate to render,
// so they are optional here (keeps node fixtures light).
export interface WorkflowGraphNode {
  id: string
  type: string
  prompt_name: string | null
  agent: AgentInfo | null
  is_gate: boolean
  conversational?: boolean
  evaluate?: boolean
  output?: string | null
  inputs?: string[]
  prehooks?: string[]
  posthooks?: string[]
}

// One routing edge. `outcome` is the AGENT_RESULT word (or null for a raw
// pattern, in which case `label` carries it). `kind` is 'route' (declared) or
// 'implicit' (the fall-through forward edge).
export interface WorkflowGraphEdge {
  source: string
  target: string
  outcome: string | null
  // 'route' (declared) | 'implicit' (fall-through forward edge). Typed as a
  // plain string so callers can construct edges without a literal-narrowing cast.
  kind: string
  // Fallback display label for a raw (non-AGENT_RESULT) rule pattern; null/
  // omitted when the outcome word is the label.
  label?: string | null
}

export interface WorkflowFlowGraph {
  name: string
  nodes: WorkflowGraphNode[]
  edges: WorkflowGraphEdge[]
}

export interface WorkflowGraph {
  name: string
  source: 'bundled' | 'repo'
  project: string | null
  project_name: string | null
  flows: WorkflowFlowGraph[]
}

// GET /api/workflows/{name}/agents/{prompt_name} — the node-detail inspector's
// payload (declared properties + prompt bodies).
export interface AgentDetail {
  name: string
  agent_class: string
  outcomes: string[]
  needs_worktree: boolean
  produces_changes: boolean
  system_prompt: string | null
  user_prompt: string | null
}
