import { useState } from 'react'
import { ReactFlow, Background, Controls } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { WorkflowGraph as WorkflowGraphType } from '@/api/types'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { layoutFlow } from './workflow-graph-layout'
import { WorkflowNode } from './workflow-node'
import { AgentInspector } from './agent-inspector'

const nodeTypes = { agentNode: WorkflowNode, terminalNode: WorkflowNode }

interface WorkflowGraphProps {
  graph: WorkflowGraphType
}

// ADR-044 Phase 6 — the read-only workflow board. The four `*={false}` props +
// hidden handles ARE the editor seam: editor mode enables them and adds
// onConnect/onNodesChange, no rewrite. A build/fix workflow ships main + pr_fix,
// so a flow selector picks which to render.
export function WorkflowGraph({ graph }: WorkflowGraphProps) {
  const flowNames = graph.flows.map((f) => f.name)
  const [selectedFlow, setSelectedFlow] = useState(
    flowNames.includes('main') ? 'main' : (flowNames[0] ?? ''),
  )
  const [inspected, setInspected] = useState<string | null>(null)

  const flow = graph.flows.find((f) => f.name === selectedFlow) ?? graph.flows[0]
  // React Compiler memoizes this; a manual useMemo can't be preserved (its
  // inferred deps include the stable setInspected).
  const { nodes, edges } = flow ? layoutFlow(flow, setInspected) : { nodes: [], edges: [] }

  if (!flow) return null

  return (
    <div className="flex h-full flex-col">
      {flowNames.length > 1 && (
        <div className="border-b border-border p-2">
          <Tabs value={selectedFlow} onValueChange={setSelectedFlow}>
            <TabsList>
              {flowNames.map((name) => (
                <TabsTrigger key={name} value={name}>
                  {name}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
      )}
      <div className="min-h-0 flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <AgentInspector
        workflow={graph.name}
        project={graph.project}
        promptName={inspected}
        onClose={() => setInspected(null)}
      />
    </div>
  )
}
