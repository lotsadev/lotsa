import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { Separator } from '@/components/ui/separator'
import { Button } from '@/components/ui/button'
import { FileText, ChevronDown, ExternalLink } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatRelativeTime, formatFullDateTime } from '@/lib/time'
import type { Message } from '@/api/types'

interface ChatMessageProps {
  message: Message
  // Set by chat-panel's needs-input collapse: this agent chat/output bubble is
  // the single "awaiting your answer" bubble a duplicate question folded into
  // (R2). Renders the amber needs-input accent.
  awaitingInput?: boolean
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

function TruncatedFooter({ message }: { message: Message }) {
  if (!message.metadata?.content_truncated) return null
  const originalLength = Number(message.metadata.original_length) || 0
  const rawUrl = `/api/tasks/${message.task_id}/messages/${message.id}/raw`
  return (
    <div className="mt-2 flex items-center gap-2 border-t border-border/50 pt-2 text-xs text-muted-foreground">
      <span>Output truncated ({formatBytes(originalLength)} original).</span>
      <a
        href={rawUrl}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 text-primary underline hover:no-underline"
      >
        View full
        <ExternalLink className="size-3" />
      </a>
    </div>
  )
}

const LARGE_CONTENT_THRESHOLD = 10_000 // chars
// R1 — overflow containment. `break-words [overflow-wrap:anywhere]` wraps long
// unbroken strings/URLs; `[&_pre]:overflow-x-auto [&_pre]:max-w-full` scopes a
// horizontal scrollbar to code blocks inside the bubble instead of widening it;
// `[&_a]:break-words` wraps long link text. Wide tables are wrapped in a scroll
// container by MARKDOWN_COMPONENTS below.
//
// R5 — the `@tailwindcss/typography` plugin restyles `code`/`pre` (backtick
// quotes on inline code; light `--tw-prose-pre-code` grey, unreadable on our
// light `bg-muted`); the higher-specificity `[&_code]`/`[&_pre]` overrides below
// re-assert the readable pre-plugin look.
const PROSE_CLASSES = "prose prose-sm dark:prose-invert max-w-none break-words [overflow-wrap:anywhere] [&_h1]:text-lg [&_h1]:font-bold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_h4]:text-sm [&_h4]:font-medium [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-xs [&_code]:font-normal [&_code]:before:content-none [&_code]:after:content-none [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3 [&_pre]:my-2 [&_pre]:text-foreground [&_pre]:overflow-x-auto [&_pre]:max-w-full [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-xs [&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_ul]:list-disc [&_ul]:pl-4 [&_ol]:list-decimal [&_ol]:pl-4 [&_a]:text-primary [&_a]:underline [&_a]:break-words"

// R1 — wide tables scroll horizontally inside their own container rather than
// widening the bubble. The prose [&_table]/[&_th]/[&_td] selectors still apply:
// the table stays a descendant of the prose div.
const MARKDOWN_COMPONENTS: Components = {
  // Destructure `node` out so react-markdown's AST node isn't spread onto the
  // <table> DOM element (which would warn); only the real table props pass through.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  table: ({ node, ...props }) => (
    <div className="max-w-full overflow-x-auto">
      <table {...props} />
    </div>
  ),
}

export function MarkdownContent({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false)
  const isLarge = content.length > LARGE_CONTENT_THRESHOLD

  if (isLarge && !expanded) {
    const lines = content.split('\n')
    const lineCount = lines.length
    const sizeKb = (content.length / 1024).toFixed(0)

    return (
      <div className="space-y-2">
        {/* Show first few lines as preview */}
        <div className={cn(PROSE_CLASSES, "line-clamp-6 opacity-70")}>
          <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={MARKDOWN_COMPONENTS}>
            {lines.slice(0, 8).join('\n')}
          </ReactMarkdown>
        </div>
        <div className="flex items-center gap-2 pt-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setExpanded(true)}
            className="gap-1.5 text-xs"
          >
            <FileText className="size-3.5" />
            Render full content ({sizeKb}KB · {lineCount} lines)
            <ChevronDown className="size-3.5" />
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className={PROSE_CLASSES}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={MARKDOWN_COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  )
}

function MessageMetadata({ metadata, createdAt }: { metadata: Record<string, unknown>; createdAt?: string }) {
  const parts: string[] = []
  if (metadata.duration_ms) parts.push(`${(Number(metadata.duration_ms) / 1000).toFixed(1)}s`)
  const tokens = (Number(metadata.input_tokens || 0)) + (Number(metadata.output_tokens || 0))
  if (tokens > 0) parts.push(`${tokens.toLocaleString()} tokens`)
  if (metadata.cost_usd) parts.push(`$${Number(metadata.cost_usd).toFixed(4)}`)

  const relTime = createdAt ? formatRelativeTime(createdAt) : ''
  const fullTime = createdAt ? formatFullDateTime(createdAt) : ''

  if (parts.length === 0 && !relTime) return null

  return (
    <div className="mt-1 font-mono text-xs text-muted-foreground">
      {parts.length > 0 && <span>{parts.join(' · ')}</span>}
      {parts.length > 0 && relTime && <span> · </span>}
      {relTime && (
        <span title={fullTime}>posted {relTime}</span>
      )}
    </div>
  )
}

export function ChatMessage({ message, awaitingInput = false }: ChatMessageProps) {
  // Hide internal events — not user-facing
  if (
    message.type === 'status_change' ||
    message.type === 'stderr' ||
    message.type === 'artifact' ||
    message.type === 'artifact_seeded'
  ) {
    return null
  }

  // Process promotion (ADR-027) — a cross-process switch is a distinct event
  // from an in-flow stage transition. Render it as a centered pill so reading
  // the history makes clear the task changed process (not silent corruption).
  if (message.type === 'process_promotion') {
    const oldProcess = message.metadata?.old_process as string | undefined
    const newProcess = message.metadata?.new_process as string | undefined
    return (
      <div className="flex items-center gap-3 py-3">
        <Separator className="flex-1" />
        <span className="shrink-0 rounded-full border border-primary/40 bg-primary/10 px-3 py-1 font-mono text-xs font-medium text-primary">
          {oldProcess && newProcess
            ? `promoted: ${oldProcess} → ${newProcess}`
            : message.content}
        </span>
        <Separator className="flex-1" />
      </div>
    )
  }

  // Stage transition — horizontal divider with label
  if (message.type === 'stage_transition') {
    const isBackward = message.metadata?.direction === 'backward'
    return (
      <div className="flex items-center gap-3 py-3">
        <Separator className="flex-1" />
        <span
          className={cn(
            'shrink-0 font-mono text-xs font-medium',
            isBackward ? 'text-amber-500' : 'text-green-500'
          )}
        >
          {message.content}
        </span>
        <Separator className="flex-1" />
      </div>
    )
  }

  // Operator-authored messages (chat/answer/feedback + user) — right-aligned
  // "You" bubble in a distinct primary tint (R4). Checked BEFORE `feedback` so
  // approve/revise/pr-fix feedback (stored as type="feedback", role="user")
  // reads as "You" rather than a centered status note.
  if (message.role === 'user') {
    return (
      <div className="flex justify-end py-1.5">
        <div className="max-w-[90%] min-w-0">
          <div className="mb-1 text-right font-mono text-xs text-muted-foreground">
            You
          </div>
          <div className="rounded-xl border border-primary/30 bg-primary/10 px-4 py-2.5">
            <MarkdownContent content={message.content} />
            <MessageMetadata metadata={message.metadata} createdAt={message.created_at} />
          </div>
        </div>
      </div>
    )
  }

  // Feedback — centered, system-authored status note. Width-constrained +
  // wrapping (R4) so long text can't overflow the viewport.
  if (message.type === 'feedback') {
    return (
      <div className="flex justify-center py-2">
        <span className="max-w-[90%] break-words text-center text-xs text-muted-foreground">
          {message.content}
        </span>
      </div>
    )
  }

  // Error — left-aligned red bubble
  if (message.type === 'error') {
    return (
      <div className="flex justify-start py-1.5">
        <div className="max-w-[90%] min-w-0 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-2.5">
          <MarkdownContent content={message.content} />
          <TruncatedFooter message={message} />
          <MessageMetadata metadata={message.metadata} createdAt={message.created_at} />
        </div>
      </div>
    )
  }

  // Question — left-aligned amber bubble
  if (message.type === 'question') {
    return (
      <div className="flex justify-start py-1.5">
        <div className="max-w-[90%] min-w-0">
          <div className="mb-1 font-mono text-xs text-amber-500">
            {message.step_name} Agent
          </div>
          <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5">
            <MarkdownContent content={message.content} />
            <MessageMetadata metadata={message.metadata} createdAt={message.created_at} />
          </div>
        </div>
      </div>
    )
  }

  // Agent messages (chat+agent, output+agent) — left-aligned with agent label.
  // When `awaitingInput` (R2), this is the single bubble a duplicate
  // needs-input question collapsed into: render the amber "awaiting your
  // answer" accent instead of the neutral card.
  if (message.role === 'agent') {
    const agentModel = message.metadata?.agent_model
      ? ` · ${message.metadata.agent_model}`
      : ''

    return (
      <div className="flex justify-start py-1.5">
        <div className="max-w-[90%] min-w-0">
          <div
            className={cn(
              'mb-1 font-mono text-xs',
              awaitingInput ? 'text-amber-500' : 'text-primary'
            )}
          >
            {message.step_name} Agent{agentModel}
            {awaitingInput && ' · awaiting your answer'}
          </div>
          <div
            className={cn(
              'rounded-xl px-4 py-2.5',
              awaitingInput
                ? 'border border-amber-500/30 bg-amber-500/10'
                : 'border border-border bg-card'
            )}
          >
            <MarkdownContent content={message.content} />
            <TruncatedFooter message={message} />
            <MessageMetadata metadata={message.metadata} createdAt={message.created_at} />
          </div>
        </div>
      </div>
    )
  }

  // Fallback — render as left-aligned plain message
  return (
    <div className="flex justify-start py-1.5">
      <div className="max-w-[90%] min-w-0 rounded-xl border border-border bg-card px-4 py-2.5">
        <MarkdownContent content={message.content} />
        <TruncatedFooter message={message} />
        <MessageMetadata metadata={message.metadata} createdAt={message.created_at} />
      </div>
    </div>
  )
}
