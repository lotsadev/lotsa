// RED spec for ADR-044 Phase 6 (v1) — the workflows viewer surface.
//
// `WorkflowsView` lists the loaded workflows for the active project and renders
// the selected one's graph. The two things this test pins are the operator-
// requested behaviours: (1) every workflow is listed, and (2) a repo-shipped
// workflow is visually identified with a provenance badge carrying the repo /
// project name (bundled workflows are not). The React Flow canvas itself is
// stubbed — this test is about the list + badge, not Dagre rendering.
//
// Reds today — `./workflows-view` does not exist.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Stub the graph panel so the list test doesn't pull in @xyflow/react / dagre.
vi.mock('./workflow-graph', () => ({
  WorkflowGraph: ({ graph }: { graph: { name: string } }) => (
    <div data-testid="workflow-graph">{graph.name}</div>
  ),
}))

// The view reads its data through the api layer (same pattern as the existing
// right-panel tab tests, which mock '@/api/tasks').
vi.mock('@/api/tasks', () => ({
  fetchProjects: vi.fn(),
  fetchProcesses: vi.fn(),
  fetchWorkflowGraph: vi.fn(),
  fetchAgentDetail: vi.fn(),
}))

import { WorkflowsView } from './workflows-view'
import { fetchProjects, fetchProcesses } from '@/api/tasks'

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkflowsView />
    </QueryClientProvider>,
  )
}

describe('WorkflowsView', () => {
  beforeEach(() => {
    cleanup()
    vi.mocked(fetchProjects).mockResolvedValue([{ id: 'alpha', name: 'Alpha', path: '/tmp/alpha' }])
    vi.mocked(fetchProcesses).mockResolvedValue([
      {
        name: 'build',
        is_active: false,
        is_default: false,
        step_names: ['plan', 'code'],
        description: null,
        promotion_inputs: [],
        source: 'bundled',
        project: null,
      },
      {
        name: 'myflow',
        is_active: false,
        is_default: false,
        step_names: ['work'],
        description: 'repo flow',
        promotion_inputs: [],
        source: 'repo',
        project: 'alpha',
      },
    ] as unknown as Awaited<ReturnType<typeof fetchProcesses>>)
  })

  it('lists every loaded workflow for the active project', async () => {
    renderView()
    expect(await screen.findByText('build')).toBeInTheDocument()
    expect(await screen.findByText('myflow')).toBeInTheDocument()
  })

  it('badges a repo-shipped workflow with its provenance and project name', async () => {
    renderView()
    // Wait for the list to populate, then assert the repo badge is present and
    // names the project (the operator ask: "identify it as a repo workflow with
    // the repo name").
    await screen.findByText('myflow')
    const badge = await screen.findByText(/Repo/)
    expect(badge).toBeInTheDocument()
    expect(screen.getByText(/alpha/i)).toBeInTheDocument()
  })

  it('scopes the workflow list to the active project', async () => {
    renderView()
    await screen.findByText('myflow')
    // The list must be fetched project-scoped so repo workflows (Phase 5,
    // project-local) are included at all.
    expect(fetchProcesses).toHaveBeenCalledWith('alpha')
  })
})
