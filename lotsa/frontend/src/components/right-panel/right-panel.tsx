import { useState, useEffect, useRef } from 'react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useTask } from '@/hooks/use-task'
import { ArtifactsTab } from './artifacts-tab'
import { ChangesTab } from './changes-tab'
import { ActivityTab } from './activity-tab'

interface RightPanelProps {
  taskId: string | null
}

export function RightPanel({ taskId }: RightPanelProps) {
  const { data } = useTask(taskId)
  const [activeTab, setActiveTab] = useState('artifacts')
  const prevRunning = useRef(false)
  const prevArtifactCount = useRef(0)

  // Read the exact fields the effects below depend on, so their dependency
  // arrays list primitives/values rather than the whole `data` object — that
  // keeps them from re-firing on every poll while satisfying exhaustive-deps.
  const taskStatus = data?.task.status
  const isConversational = data?.task.is_conversational
  const artifacts = data?.artifacts

  // Auto-switch tab only for non-conversational steps (coding, review)
  // Don't switch for conversational steps (spec, verify) where the agent
  // runs briefly to respond to a message
  useEffect(() => {
    if (taskStatus === undefined) return
    const running = taskStatus === 'working'
    if (running && !prevRunning.current && !isConversational) setActiveTab('changes')
    if (!running && prevRunning.current && !isConversational) setActiveTab('artifacts')
    prevRunning.current = running
  }, [taskStatus, isConversational])

  // When a new artifact lands, jump to the Artifacts tab so the user sees it.
  // Wins over the running→changes switch above when both fire on the same render.
  useEffect(() => {
    const count = Object.keys(artifacts ?? {}).length
    if (count > prevArtifactCount.current) {
      setActiveTab('artifacts')
    }
    prevArtifactCount.current = count
  }, [artifacts])

  if (!taskId || !data) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-sm text-muted-foreground">
        Select a task to view artifacts
      </div>
    )
  }

  return (
    <Tabs value={activeTab} onValueChange={setActiveTab} className="flex h-full flex-col">
      <TabsList className="w-full shrink-0 rounded-none border-b border-border">
        <TabsTrigger value="artifacts" className="flex-1">
          Artifacts
        </TabsTrigger>
        <TabsTrigger value="changes" className="flex-1">
          Changes
        </TabsTrigger>
        <TabsTrigger value="activity" className="flex-1">
          Activity
        </TabsTrigger>
      </TabsList>
      <TabsContent value="artifacts" className="flex-1 overflow-hidden mt-0">
        <ArtifactsTab
          key={taskId}
          artifacts={data.artifacts}
          flow={data.flow}
          currentStep={data.task.current_step}
        />
      </TabsContent>
      <TabsContent value="changes" className="flex-1 overflow-hidden mt-0">
        <ChangesTab
          taskId={taskId}
          active={activeTab === 'changes'}
          status={data.task.status}
          prNumber={data.task.metadata?.pr_number as number | string | undefined}
          prUrl={data.task.metadata?.pr_url as string | undefined}
        />
      </TabsContent>
      <TabsContent value="activity" className="flex-1 overflow-hidden mt-0">
        <ActivityTab key={taskId} taskId={taskId} active={activeTab === 'activity'} />
      </TabsContent>
    </Tabs>
  )
}
