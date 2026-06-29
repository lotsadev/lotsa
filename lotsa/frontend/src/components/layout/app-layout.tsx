import { useState } from 'react'
import { PanelRight } from 'lucide-react'
import {
  SidebarProvider,
  Sidebar,
  SidebarInset,
  SidebarTrigger,
  useSidebar,
} from '@/components/ui/sidebar'
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from '@/components/ui/resizable'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { useIsMobile } from '@/hooks/use-mobile'
import { AppSidebar } from '@/components/sidebar/sidebar'
import { EmptyState } from '@/components/empty-state'
import { ChatPanel } from '@/components/chat/chat-panel'
import { RightPanel } from '@/components/right-panel/right-panel'
import { ThemeToggle } from './theme-toggle'

interface AppLayoutProps {
  selectedTaskId: string | null
  onSelectTask: (taskId: string | null) => void
}

// Single 768px breakpoint (the codebase's definition of "mobile") switches
// between the chat-primary mobile shell and the desktop multi-column layout.
// The two are separate subtrees so the desktop path stays byte-for-byte
// unchanged (AC#4) and all mobile risk is isolated to MobileShell.
export function AppLayout(props: AppLayoutProps) {
  const isMobile = useIsMobile()
  return isMobile ? <MobileShell {...props} /> : <DesktopShell {...props} />
}

function DesktopShell({ selectedTaskId, onSelectTask }: AppLayoutProps) {
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

function MobileShell(props: AppLayoutProps) {
  // The right panel (Artifacts/Changes/Activity) lives in a bottom sheet on
  // mobile; its open state is owned here and shared by the header button and
  // the peek bar. ``h-dvh`` tracks the dynamic viewport so the layout shrinks
  // when the on-screen keyboard opens, keeping the chat input visible (AC#6).
  const [rightOpen, setRightOpen] = useState(false)
  return (
    <SidebarProvider className="h-dvh flex-col overflow-hidden">
      <MobileShellInner {...props} rightOpen={rightOpen} setRightOpen={setRightOpen} />
    </SidebarProvider>
  )
}

function MobileShellInner({
  selectedTaskId,
  onSelectTask,
  rightOpen,
  setRightOpen,
}: AppLayoutProps & {
  rightOpen: boolean
  setRightOpen: (open: boolean) => void
}) {
  // Inside the provider so we can close the drawer on task selection.
  const { setOpenMobile } = useSidebar()
  const handleSelect = (taskId: string | null) => {
    onSelectTask(taskId)
    setOpenMobile(false)
  }

  return (
    <>
      <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-border px-4">
        <div className="flex items-center gap-2">
          {/* Left drawer trigger — toggles the task-list slide-over (AC#2). */}
          <SidebarTrigger />
          <h1 className="text-lg font-bold tracking-tight">Lotsa</h1>
        </div>
        <div className="flex items-center gap-1">
          {/* Right/bottom-sheet trigger (AC#3). Disabled when no task is
              selected — there's nothing to show. */}
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setRightOpen(true)}
            disabled={!selectedTaskId}
            aria-label="Open task panel"
          >
            <PanelRight className="size-4" />
          </Button>
          <ThemeToggle />
        </div>
      </header>

      {/* Task list as a left offcanvas drawer. ``collapsible`` defaults to
          "offcanvas", and because useIsMobile() is true here the shadcn
          Sidebar renders its internal mobile Sheet, driven by openMobile and
          toggled by SidebarTrigger. */}
      <Sidebar>
        <AppSidebar selectedTaskId={selectedTaskId} onSelectTask={handleSelect} />
      </Sidebar>

      <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {selectedTaskId ? (
          <ChatPanel taskId={selectedTaskId} onOpenPanel={() => setRightOpen(true)} />
        ) : (
          <EmptyState onTaskCreated={(taskId) => handleSelect(taskId)} />
        )}
      </main>

      {/* Right panel as a tap-to-open bottom sheet (~85vh). */}
      <Sheet open={rightOpen} onOpenChange={setRightOpen}>
        <SheetContent side="bottom" className="flex h-[85vh] flex-col gap-0 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Task panel</SheetTitle>
            <SheetDescription>Artifacts, changes, and activity</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1">
            <RightPanel taskId={selectedTaskId} />
          </div>
        </SheetContent>
      </Sheet>
    </>
  )
}
