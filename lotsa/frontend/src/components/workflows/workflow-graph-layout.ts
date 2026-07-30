import dagre from '@dagrejs/dagre'
import type { Node, Edge } from '@xyflow/react'
import type { WorkflowFlowGraph, WorkflowGraphNode } from '@/api/types'

// ADR-044 Phase 6 — the PURE mapping from a serialized workflow flow to React
// Flow nodes + edges, with Dagre auto-layout. Kept free of React / the canvas
// so the mapping is unit-testable in jsdom. React Flow deliberately does not
// position nodes; Dagre lays out the mostly-linear plan→…→push DAGs left-to-
// right. The viewer→editor path is a props flip on <ReactFlow>, not a rewrite —
// this layout is reused unchanged.

const NODE_W = 200
const NODE_H = 76

// The serialized node fields (flat) plus the inspector callback. The
// `[key: string]: unknown` index signature is what lets this satisfy React
// Flow's `Record<string, unknown>` node-data constraint (an interface / plain
// intersection would not) while keeping the named fields strongly typed.
export type WorkflowNodeData = WorkflowGraphNode & {
  onInspect?: (promptName: string) => void
  [key: string]: unknown
}

export type WorkflowRFNode = Node<WorkflowNodeData>

export interface LaidOutFlow {
  nodes: WorkflowRFNode[]
  edges: Edge[]
}

// `onInspect` is threaded onto agent-node data so a node click opens the
// inspector; the layout itself doesn't care about it.
export function layoutFlow(
  flow: WorkflowFlowGraph,
  onInspect?: (promptName: string) => void,
): LaidOutFlow {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 90 })

  for (const n of flow.nodes) {
    g.setNode(n.id, { width: NODE_W, height: NODE_H })
  }
  for (const e of flow.edges) {
    // Guard against an edge to a target that isn't a laid-out node (e.g. a
    // cross-flow sink) so Dagre doesn't invent a phantom node.
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target)
  }
  dagre.layout(g)

  const nodes: WorkflowRFNode[] = flow.nodes.map((n) => {
    const pos = g.node(n.id)
    return {
      id: n.id,
      type: n.type === 'terminal' ? 'terminalNode' : 'agentNode',
      // Dagre centres nodes; React Flow positions by top-left corner.
      position: { x: (pos?.x ?? 0) - NODE_W / 2, y: (pos?.y ?? 0) - NODE_H / 2 },
      data: { ...n, onInspect },
    }
  })

  const edges: Edge[] = flow.edges.map((e, i) => ({
    id: `${e.source}->${e.target}:${e.outcome ?? e.label ?? i}`,
    source: e.source,
    target: e.target,
    label: e.outcome ?? e.label ?? undefined,
    data: { outcome: e.outcome, kind: e.kind },
    // Implicit forward edges are muted + dashed; a FAILED verdict is the
    // destructive accent. Tokens only (ADR-012) — no hardcoded colours.
    animated: false,
    className:
      e.kind === 'implicit'
        ? 'wf-edge-implicit'
        : e.outcome === 'FAILED'
          ? 'wf-edge-failed'
          : 'wf-edge-route',
  }))

  return { nodes, edges }
}
