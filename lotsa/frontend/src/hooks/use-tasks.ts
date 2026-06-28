import { useQuery } from '@tanstack/react-query'
import { fetchTasks } from '@/api/tasks'

export function useTasks() {
  return useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
    refetchInterval: (query) => {
      const tasks = query.state.data
      const anyRunning = tasks?.some((t) => t.status === 'working') ?? false
      return anyRunning ? 3000 : 10_000
    },
  })
}
