import { useQuery } from '@tanstack/react-query'
import { fetchTaskDetail } from '@/api/tasks'

export function useTask(taskId: string | null) {
  return useQuery({
    queryKey: ['task', taskId],
    queryFn: () => fetchTaskDetail(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const task = query.state.data?.task
      if (!task) return 5000
      return task.status === 'working' ? 3000 : 10000
    },
  })
}
