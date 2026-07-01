import type { Message } from '@/api/types'

export interface ChatRenderItem {
  message: Message
  // True → render this agent chat/output bubble with the amber "awaiting your
  // answer" accent because a duplicate needs-input question row collapsed into
  // it (R2). Ignored for non-agent-bubble items — the standalone `question`
  // branch styles itself amber.
  awaitingInput: boolean
}

// Rows ChatMessage renders as null. Kept in the list (so nothing else shifts)
// but transparent to needs-input adjacency: the orchestrator writes stderr and
// a status_change dispatch event between the output row and the question row.
const HIDDEN_TYPES = new Set(['status_change', 'stderr', 'artifact', 'artifact_seeded'])

// A question collapses into the preceding same-step agent bubble when its text
// is equal to, or a subset of, that bubble's content (R2). The `output` row
// holds the full stdout (marker line included); the `question` row holds just
// the extracted question, so it is always contained in the agent's message.
function covers(primary: Message, question: Message): boolean {
  const q = question.content.trim()
  if (!q) return true
  return primary.content.trim().includes(q)
}

// Collapse the duplicate agent needs-input rows at render time (R2). The
// `messages` table is append-only, so both the agent chat/output row and the
// redundant `question` row are persisted; here we drop the question when it
// duplicates an adjacent same-step agent bubble and mark that bubble
// `awaitingInput`. A question with no covering bubble (e.g. pr-fix
// NEEDS_DECISION, which writes no separate chat row) renders on its own.
export function collapseNeedsInputMessages(messages: Message[]): ChatRenderItem[] {
  const items: ChatRenderItem[] = []
  let lastAgentBubble: ChatRenderItem | null = null

  for (const message of messages) {
    if (HIDDEN_TYPES.has(message.type)) {
      // Preserve in the list; don't disturb adjacency tracking.
      items.push({ message, awaitingInput: false })
      continue
    }

    if (message.role === 'agent' && message.type === 'question') {
      if (
        lastAgentBubble &&
        lastAgentBubble.message.step_name === message.step_name &&
        covers(lastAgentBubble.message, message)
      ) {
        lastAgentBubble.awaitingInput = true // collapse into the agent bubble
        continue // drop the duplicate question row
      }
      // Standalone question (no covering bubble): render on its own.
      items.push({ message, awaitingInput: false })
      lastAgentBubble = null
      continue
    }

    const item: ChatRenderItem = { message, awaitingInput: false }
    items.push(item)

    // Track the newest agent chat/output bubble; any other visible row is a
    // turn boundary a later question must not cross.
    lastAgentBubble =
      message.role === 'agent' && (message.type === 'chat' || message.type === 'output')
        ? item
        : null
  }

  return items
}
