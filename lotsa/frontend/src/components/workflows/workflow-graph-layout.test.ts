// RED spec for ADR-044 Phase 6 (v1) — the pure graph-layout mapping.
//
// `layoutFlow` maps a `WorkflowFlowGraph` payload (from the backend graph
// endpoint) into React Flow `nodes` + `edges`, running Dagre auto-layout so
// each node gets a concrete {x, y} position. It is a PURE function (no canvas,
// no React) so the core mapping is testable in jsdom without rendering
// `@xyflow/react`. Reds today — the module does not exist.

import { describe, it, expect } from 'vitest'
import { layoutFlow } from './workflow-graph-layout'

// A small, representative flow: a worker → gate happy path, a gate FAILED
// back-edge, and a materialized terminal node. Shaped exactly like the
// serializer's per-flow payload.
const FLOW = {
  name: 'main',
  nodes: [
    { id: 'code', type: 'agent', prompt_name: 'coding', agent: { name: 'coding', agent_class: 'worker', outcomes: ['COMPLETED'], needs_worktree: true, produces_changes: true }, is_gate: false, prehooks: [], posthooks: ['commit'] },
    { id: 'review', type: 'agent', prompt_name: 'review', agent: { name: 'review', agent_class: 'gate', outcomes: ['PASSED', 'FAILED'], needs_worktree: true, produces_changes: false }, is_gate: true, prehooks: [], posthooks: [] },
    { id: 'complete', type: 'terminal', prompt_name: null, agent: null, is_gate: false, prehooks: [], posthooks: [] },
    { id: 'blocked', type: 'terminal', prompt_name: null, agent: null, is_gate: false, prehooks: [], posthooks: [] },
  ],
  edges: [
    { source: 'code', target: 'review', outcome: 'COMPLETED', kind: 'implicit' },
    { source: 'review', target: 'complete', outcome: 'PASSED', kind: 'route' },
    { source: 'review', target: 'blocked', outcome: 'FAILED', kind: 'route' },
  ],
}

describe('layoutFlow', () => {
  it('produces one React Flow node per payload node', () => {
    const { nodes } = layoutFlow(FLOW)
    expect(nodes).toHaveLength(FLOW.nodes.length)
    expect(new Set(nodes.map((n) => n.id))).toEqual(new Set(['code', 'review', 'complete', 'blocked']))
  })

  it('assigns each node a concrete numeric Dagre position', () => {
    const { nodes } = layoutFlow(FLOW)
    for (const n of nodes) {
      expect(typeof n.position.x).toBe('number')
      expect(typeof n.position.y).toBe('number')
      expect(Number.isFinite(n.position.x)).toBe(true)
      expect(Number.isFinite(n.position.y)).toBe(true)
    }
  })

  it('carries the original node payload through on node.data', () => {
    const { nodes } = layoutFlow(FLOW)
    const review = nodes.find((n) => n.id === 'review')!
    // The custom node component reads the agent + gate flag off data.
    expect(review.data).toMatchObject({ id: 'review', is_gate: true })
    expect((review.data as { agent: { agent_class: string } }).agent.agent_class).toBe('gate')
  })

  it('produces one edge per payload edge, labelled with the outcome', () => {
    const { edges } = layoutFlow(FLOW)
    expect(edges).toHaveLength(FLOW.edges.length)
    const failEdge = edges.find((e) => e.source === 'review' && e.target === 'blocked')!
    expect(failEdge.label).toBe('FAILED')
  })

  it('preserves the edge kind so implicit edges can be styled apart', () => {
    const { edges } = layoutFlow(FLOW)
    const implicit = edges.find((e) => e.source === 'code' && e.target === 'review')!
    expect((implicit.data as { kind: string }).kind).toBe('implicit')
  })

  it('gives every edge a unique id', () => {
    const { edges } = layoutFlow(FLOW)
    const ids = edges.map((e) => e.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})
