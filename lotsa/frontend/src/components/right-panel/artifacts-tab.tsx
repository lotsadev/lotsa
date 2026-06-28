import { useState, useEffect, useRef } from 'react'
import { MarkdownContent } from '@/components/chat/chat-message'
// Using native overflow instead of Radix ScrollArea (same fix as chat panel)
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { FileText } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Flow } from '@/api/types'

interface ArtifactsTabProps {
  artifacts: Record<string, string>
  flow: Flow | null
  currentStep: string | null
}

export function ArtifactsTab({ artifacts, flow, currentStep }: ArtifactsTabProps) {
  const artifactEntries = Object.entries(artifacts)
  const stepsWithOutput = flow?.steps.filter((s) => s.output) ?? []

  // All possible artifact names from the flow
  const allNames = stepsWithOutput.map((s) => s.output!)
  for (const name of Object.keys(artifacts)) {
    if (!allNames.includes(name)) allNames.push(name)
  }

  const [selectedName, setSelectedName] = useState<string | null>(
    artifactEntries.length > 0 ? artifactEntries[artifactEntries.length - 1][0] : null
  )

  // When a new artifact arrives, select it.
  const prevKeys = useRef<Set<string>>(new Set(Object.keys(artifacts)))
  useEffect(() => {
    const current = new Set(Object.keys(artifacts))
    const newKeys = [...current].filter((k) => !prevKeys.current.has(k))
    if (newKeys.length > 0) {
      setSelectedName(newKeys[newKeys.length - 1])
    } else if (selectedName === null && current.size > 0) {
      // First artifact seen after mount with empty initial state.
      setSelectedName([...current][current.size - 1])
    }
    prevKeys.current = current
  }, [artifacts, selectedName])

  const selectedContent = selectedName ? artifacts[selectedName] : null

  if (allNames.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-sm text-muted-foreground">
        No artifacts in this flow
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Artifact selector */}
      <div className="shrink-0 border-b border-border px-3 py-2">
        <ToggleGroup
          type="single"
          value={selectedName ?? ''}
          onValueChange={(v: string) => { if (v) setSelectedName(v) }}
          className="flex flex-wrap gap-1.5 justify-start"
        >
          {allNames.map((name) => {
            const exists = name in artifacts
            const step = stepsWithOutput.find((s) => s.output === name)
            const isCurrent = step && step.name === currentStep

            return (
              <ToggleGroupItem
                key={name}
                value={name}
                disabled={!exists}
                className={cn(
                  'gap-2 font-mono text-sm px-4 py-2.5 h-auto',
                  !exists && 'opacity-35',
                  isCurrent && !exists && 'border-dashed border-primary/30',
                )}
              >
                <FileText className="size-4" />
                {name}
              </ToggleGroupItem>
            )
          })}
        </ToggleGroup>
      </div>

      {/* Content area */}
      {selectedContent ? (
        <div className="flex-1 overflow-y-auto min-h-0">
          <div className="p-4 text-sm break-words overflow-wrap-anywhere">
            <MarkdownContent content={selectedContent} />
          </div>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center p-4 text-sm text-muted-foreground">
          {artifactEntries.length === 0
            ? 'Artifacts will appear as the workflow progresses'
            : 'Select an artifact to view'}
        </div>
      )}
    </div>
  )
}
