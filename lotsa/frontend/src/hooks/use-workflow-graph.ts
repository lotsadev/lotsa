import { useQuery } from '@tanstack/react-query'
import { fetchWorkflowGraph } from '@/api/tasks'

// A workflow's read-only agent graph (ADR-044 Phase 6). Static for the session
// (the built process only changes on restart), so no polling. Disabled until a
// workflow is selected.
export function useWorkflowGraph(name: string | undefined, projectId: string | undefined) {
  return useQuery({
    queryKey: ['workflow-graph', name ?? null, projectId ?? null],
    queryFn: () => fetchWorkflowGraph(name!, projectId),
    staleTime: Infinity,
    enabled: !!name,
  })
}
