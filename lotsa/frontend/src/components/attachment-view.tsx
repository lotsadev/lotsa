import { Paperclip } from 'lucide-react'
import { attachmentRawUrl } from '@/api/tasks'
import { formatBytes } from '@/lib/utils'
import type { Attachment } from '@/api/types'

// Raster image MIME types the backend actually serves `inline` — mirror of
// `_INLINE_SAFE_MIMES` in `lotsa/attachments.py`. A thumbnail is only rendered
// for these: any other type (notably `image/svg+xml`, which the backend forces
// to an `application/octet-stream` download to close the stored-XSS path) would
// render as a broken `<img>`, so it falls through to the paperclip chip below.
const INLINE_IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

// One attachment rendered inline. Server-inlineable images become a clickable
// thumbnail that opens the full-size file in a new tab; other types (including
// non-raster images the server won't serve inline) become a paperclip chip with
// name + size. Both anchor to the raw-bytes endpoint. Shared by the chat bubble
// strip (message-scoped) and the right-panel list (task-scoped).
export function AttachmentItem({ taskId, att }: { taskId: string; att: Attachment }) {
  const url = attachmentRawUrl(taskId, att.filename)
  const isImage = INLINE_IMAGE_MIMES.has(att.mime)

  if (isImage) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={att.filename}
        className="block overflow-hidden rounded-md border border-border hover:border-primary/50"
      >
        <img
          src={url}
          alt={att.filename}
          loading="lazy"
          className="max-h-32 max-w-[12rem] object-cover"
        />
      </a>
    )
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={att.filename}
      className="inline-flex max-w-[16rem] items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-xs hover:bg-muted"
    >
      <Paperclip className="size-3.5 shrink-0" />
      <span className="truncate">{att.filename}</span>
      <span className="shrink-0 text-muted-foreground">· {formatBytes(att.size_bytes)}</span>
    </a>
  )
}

// A flex-wrap strip of attachments. Renders nothing when empty.
export function AttachmentStrip({ taskId, attachments }: { taskId: string; attachments: Attachment[] }) {
  if (!attachments.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {attachments.map((a) => (
        <AttachmentItem key={a.filename} taskId={taskId} att={a} />
      ))}
    </div>
  )
}
