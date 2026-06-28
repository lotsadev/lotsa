import { SidebarProvider, Sidebar, SidebarInset } from '@/components/ui/sidebar'
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from '@/components/ui/resizable'
import { AppSidebar } from '@/components/sidebar/sidebar'
import { EmptyState } from '@/components/empty-state'
import { ChatPanel } from '@/components/chat/chat-panel'
import { RightPanel } from '@/components/right-panel/right-panel'
import { ThemeToggle } from './theme-toggle'

interface AppLayoutProps {
  selectedTaskId: string | null
  onSelectTask: (taskId: string | null) => void
}

export function AppLayout({ selectedTaskId, onSelectTask }: AppLayoutProps) {
  return (
    <div className="flex h-screen flex-col overflow-hidden">
      {/* Header — full width, above everything */}
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
        <h1 className="text-lg font-bold tracking-tight">Lotsa</h1>
        <ThemeToggle />
      </header>

      {/* Body — sidebar + resizable content area */}
      <SidebarProvider className="flex-1 !min-h-0">
        <Sidebar collapsible="none" className="w-[18rem] shrink-0 border-r border-border">
          <AppSidebar selectedTaskId={selectedTaskId} onSelectTask={onSelectTask} />
        </Sidebar>
        <SidebarInset className="flex-1 overflow-hidden">
          <ResizablePanelGroup orientation="horizontal" className="h-full">
            {/* Chat panel */}
            <ResizablePanel defaultSize={55} minSize={35}>
              <div className="flex h-full flex-col overflow-hidden">
                {selectedTaskId ? (
                  <ChatPanel taskId={selectedTaskId} />
                ) : (
                  <EmptyState onTaskCreated={(taskId) => onSelectTask(taskId)} />
                )}
              </div>
            </ResizablePanel>

            <ResizableHandle withHandle />

            {/* Right panel — artifacts + changes */}
            <ResizablePanel defaultSize={45} minSize={25}>
              <div className="flex h-full flex-col overflow-hidden">
                <RightPanel taskId={selectedTaskId} />
              </div>
            </ResizablePanel>
          </ResizablePanelGroup>
        </SidebarInset>
      </SidebarProvider>
    </div>
  )
}
