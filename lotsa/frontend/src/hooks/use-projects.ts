import { useQuery } from '@tanstack/react-query'
import { fetchProjects } from '@/api/tasks'

// The project catalog changes only on a Lotsa restart (a new lotsa.yaml
// requires a restart per ADR-029), so the list is effectively static for the
// lifetime of the session — no polling needed.
export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: fetchProjects,
    staleTime: Infinity,
  })
}
