import { useQuery } from '@tanstack/react-query'
import { fetchProcesses } from '@/api/tasks'

// The process catalog changes only on a Lotsa restart (a new lotsa.yaml
// requires a restart per ADR-021), so the list is effectively static for the
// lifetime of the session — no polling needed.
export function useProcesses() {
  return useQuery({
    queryKey: ['processes'],
    queryFn: fetchProcesses,
    staleTime: Infinity,
  })
}
