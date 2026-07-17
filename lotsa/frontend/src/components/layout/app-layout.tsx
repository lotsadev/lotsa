import { useState, type CSSProperties } from 'react'
import { PanelRight, PanelRightClose, PanelRightOpen } from 'lucide-react'
import { usePanelRef, useDefaultLayout } from 'react-resizable-panels'
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
// The two are separate subtrees so each shell's collapse wiring stays
// isolated — desktop changes can't regress MobileShell and vice versa.
export function AppLayout(props: AppLayoutProps) {
  const isMobile = useIsMobile()
  return isMobile ? <MobileShell {...props} /> : <DesktopShell {...props} />
}

// Persists the desktop chat/artifacts split — including the artifacts
// collapsed state (0%) — to localStorage. In react-resizable-panels v4 the
// `useDefaultLayout` hook is the replacement for the old `autoSaveId` prop.
const DESKTOP_LAYOUT_ID = 'lotsa-desktop-layout'

// shadcn's SidebarProvider writes the open/closed choice to the
// `sidebar_state` cookie but only *reads* it during SSR. The dashboard is a
// pure client SPA, so we read the cookie here and seed `defaultOpen` to make
// the left task-list collapse survive a reload.
function readSidebarOpenCookie(): boolean {
  if (typeof document === 'undefined') return true
  const match = document.cookie.match(/(?:^|;\s*)sidebar_state=(true|false)/)
  return match ? match[1] === 'true' : true
}

function DesktopShell({ selectedTaskId, onSelectTask }: AppLayoutProps) {
  // v4 exposes the imperative collapse/expand API via a dedicated `panelRef`
  // prop (not the React `ref`); `usePanelRef` returns a correctly-typed ref.
  const artifactsPanel = usePanelRef()
  const [artifactsCollapsed, setArtifactsCollapsed] = useState(false)
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: DESKTOP_LAYOUT_ID,
  })

  const toggleArtifacts = () => {
    const panel = artifactsPanel.current
    if (!panel) return
    if (panel.isCollapsed()) {
      panel.expand()
    } else {
      panel.collapse()
    }
  }

  return (
    <SidebarProvider
      defaultOpen={readSidebarOpenCookie()}
      style={{ '--sidebar-width': '18rem' } as CSSProperties}
      className="h-screen flex-col overflow-hidden min-h-0!"
    >
      {/* Header — full width, above everything. z-20 keeps it above the
          offcanvas sidebar's fixed rail (z-10). */}
      <header className="z-20 flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-4">
        <div className="flex items-center gap-2">
          {/* Left task-list toggle — mirrors the mobile shell (⌘/Ctrl+B for
              free). Collapse state persists via the `sidebar_state` cookie. */}
          <SidebarTrigger />
          <h1 className="text-lg font-bold tracking-tight">Lotsa</h1>
        </div>
        <div className="flex items-center gap-1">
          {/* Right artifacts-panel toggle. */}
          <Button
            variant="ghost"
            size="icon"
            onClick={toggleArtifacts}
            aria-expanded={!artifactsCollapsed}
            aria-label={
              artifactsCollapsed ? 'Show artifacts panel' : 'Hide artifacts panel'
            }
          >
            {artifactsCollapsed ? (
              <PanelRightOpen className="size-4" />
            ) : (
              <PanelRightClose className="size-4" />
            )}
          </Button>
          <ThemeToggle />
        </div>
      </header>

      {/* Body — sidebar + resizable content area */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Offcanvas task list. The fixed rail is pinned below the h-12 header
            (top-12, `!important` beats the primitive's `inset-y-0`) and left to
            stretch to the viewport bottom (h-auto drops the primitive's h-svh
            so top+bottom govern) so it never covers the title/triggers. */}
        <Sidebar collapsible="offcanvas" className="top-12! h-auto">
          <AppSidebar selectedTaskId={selectedTaskId} onSelectTask={onSelectTask} />
        </Sidebar>
        <SidebarInset className="flex-1 overflow-hidden">
          <ResizablePanelGroup
            orientation="horizontal"
            className="h-full"
            defaultLayout={defaultLayout}
            onLayoutChanged={onLayoutChanged}
          >
            {/* Chat panel */}
            <ResizablePanel id="chat" defaultSize={55} minSize={35}>
              <div className="flex h-full flex-col overflow-hidden">
                {selectedTaskId ? (
                  <ChatPanel taskId={selectedTaskId} />
                ) : (
                  <EmptyState onTaskCreated={(taskId) => onSelectTask(taskId)} />
                )}
              </div>
            </ResizablePanel>

            {/* Hide the drag handle while the artifacts panel is collapsed;
                the header button is the way back. */}
            {!artifactsCollapsed && <ResizableHandle withHandle />}

            {/* Right panel — artifacts + changes */}
            <ResizablePanel
              id="artifacts"
              panelRef={artifactsPanel}
              defaultSize={45}
              minSize={25}
              collapsible
              collapsedSize={0}
              onResize={(size) => setArtifactsCollapsed(size.asPercentage === 0)}
            >
              <div className="flex h-full flex-col overflow-hidden">
                <RightPanel taskId={selectedTaskId} />
              </div>
            </ResizablePanel>
          </ResizablePanelGroup>
        </SidebarInset>
      </div>
    </SidebarProvider>
  )
}

function MobileShell(props: AppLayoutProps) {
  // The right panel (Artifacts/Changes/Activity) lives in a bottom sheet on
  // mobile; its open state is owned here and shared by the header button and
  // the peek bar. ``h-dvh`` tracks the dynamic viewport so the layout shrinks
  // when the on-screen keyboard opens, keeping the chat input visible (AC#6).
  // ``!min-h-0`` overrides SidebarProvider's base ``min-h-svh``: otherwise CSS
  // resolves the height to ``max(100dvh, 100svh)`` and the container stays at
  // ``100svh`` when the keyboard shrinks ``dvh``, hiding the input. Same
  // override the DesktopShell SidebarProvider uses.
  const [rightOpen, setRightOpen] = useState(false)
  return (
    <SidebarProvider className="h-dvh !min-h-0 flex-col overflow-hidden">
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
          {/* Visible header bar so the Sheet's built-in close button (absolute
              right-4 top-4) has its own row instead of overlapping RightPanel's
              full-width tab bar. The description stays sr-only for a11y. */}
          <SheetHeader className="flex shrink-0 flex-row items-center justify-between space-y-0 border-b border-border px-4 py-3 pr-12 text-left">
            <SheetTitle className="text-sm font-medium">Task panel</SheetTitle>
            <SheetDescription className="sr-only">
              Artifacts, changes, and activity
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1">
            <RightPanel taskId={selectedTaskId} />
          </div>
        </SheetContent>
      </Sheet>
    </>
  )
}
