import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppLayout } from '@/components/layout/app-layout'
import { useTheme } from '@/hooks/use-theme'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      refetchOnWindowFocus: false,
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
