import { useRef, useState, useEffect, useCallback } from 'react'
import { useTask } from '@/hooks/use-task'
import { ChatMessage } from './chat-message'
import { StageBar } from './stage-bar'
import { ChatInput } from './chat-input'
import { PeekBar } from '@/components/layout/peek-bar'

interface ChatPanelProps {
  taskId: string
  // Mobile-only: opens the right-panel bottom sheet. When provided, the peek
  // bar renders above the chat input. Omitted on desktop → render unchanged.
  onOpenPanel?: () => void
}

export function ChatPanel({ taskId, onOpenPanel }: ChatPanelProps) {
  const { data, isLoading, error } = useTask(taskId)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const [isNearBottom, setIsNearBottom] = useState(true)
  const prevMessageCount = useRef(0)

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    setIsNearBottom(distFromBottom < 100)
  }, [])

  useEffect(() => {
    const count = data?.messages.length ?? 0
    if (count > prevMessageCount.current && isNearBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
    prevMessageCount.current = count
  }, [data?.messages.length, isNearBottom])

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <p className="text-sm">Loading task...</p>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <p className="text-sm">Failed to load task</p>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <StageBar task={data.task} flow={data.flow} totals={data.totals} />

      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto min-h-0"
      >
        <div className="mx-auto max-w-3xl px-4 py-4">
          {data.messages.map((message) => (
            <ChatMessage key={message.id} message={message} />
          ))}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {onOpenPanel && <PeekBar data={data} onOpen={onOpenPanel} />}
      <ChatInput data={data} />
    </div>
  )
}
