import { useQuery } from '@tanstack/react-query'
import { fetchProcesses } from '@/api/tasks'

// The workflow catalog for a project (bundled + that project's repo workflows,
// ADR-044 Phase 5/6). Like the process catalog it changes only on a Lotsa
// restart, so it's effectively static for the session — no polling. Scoped by
// project so repo-shipped workflows are included and carry their provenance.
export function useWorkflows(projectId: string | undefined) {
  return useQuery({
    queryKey: ['workflows', projectId ?? null],
    queryFn: () => fetchProcesses(projectId),
    staleTime: Infinity,
    enabled: projectId !== undefined,
  })
}
