import { useState } from 'react'
import { Waypoints } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { WorkflowsView } from './workflows-view'

// ADR-044 Phase 6 — the header entry point to the workflows viewer. The viewer
// is a big surface (list + graph board), so it opens in a large dialog overlay
// rather than crowding the task-centric three-column layout. Self-contained
// (owns its open state) so both the desktop and mobile headers can drop it in.
export function WorkflowsButton() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        onClick={() => setOpen(true)}
        aria-label="Workflows"
        title="Workflows"
      >
        <Waypoints className="size-4" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex h-[90vh] w-[95vw] max-w-none flex-col gap-0 p-0">
          <DialogHeader className="sr-only">
            <DialogTitle>Workflows</DialogTitle>
            <DialogDescription>Read-only view of a workflow's agent graph</DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1">
            <WorkflowsView />
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
