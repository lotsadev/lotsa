import { useQuery } from '@tanstack/react-query'
import { fetchAttachments } from '@/api/tasks'
import { AttachmentStrip } from '@/components/attachment-view'

interface AttachmentsTabProps {
  taskId: string
  active: boolean
}

// Task-scoped view: every attachment on the task, regardless of which message
// it rode in with. Reads the existing metadata list endpoint (not the raw
// bytes) and renders thumbnails/chips via the shared strip.
export function AttachmentsTab({ taskId, active }: AttachmentsTabProps) {
  const { data } = useQuery({
    queryKey: ['attachments', taskId],
    queryFn: () => fetchAttachments(taskId),
    enabled: active,
  })
  const attachments = data ?? []

  if (attachments.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-sm text-muted-foreground">
        No attachments on this task
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <AttachmentStrip taskId={taskId} attachments={attachments} />
    </div>
  )
}
