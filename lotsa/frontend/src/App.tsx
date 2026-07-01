import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppLayout } from '@/components/layout/app-layout'
import { useTheme } from '@/hooks/use-theme'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      // Refetch when the tab regains focus. Interval polling pauses while the
      // tab is backgrounded (refetchIntervalInBackground defaults false), and
      // Lotsa's whole workflow is "kick off a task, walk away, come back" — so
      // without this, returning to the tab shows stale state (e.g. a verify
      // step that finished and is now waiting at the Accept gate) until a
      // manual reload. Focus refetch is the catch-up; background polling stays
      // off to save resources.
      refetchOnWindowFocus: true,
    },
  },
})

function AppInner() {
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const { theme } = useTheme()

  return (
    <div className={theme}>
      <AppLayout
        selectedTaskId={selectedTaskId}
        onSelectTask={setSelectedTaskId}
      />
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppInner />
    </QueryClientProvider>
  )
}
