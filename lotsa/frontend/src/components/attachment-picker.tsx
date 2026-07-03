import { useRef } from 'react'
import { Paperclip, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface AttachmentPickerProps {
  files: File[]
  onChange: (files: File[]) => void
  disabled?: boolean
  error?: string | null
  className?: string
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Local file selection for a chat-input surface. Purely picks files — the
// parent uploads them (after the task exists) on submit. Shows each selected
// file with a remove-before-send affordance and surfaces server errors inline.
export function AttachmentPicker({ files, onChange, disabled, error, className }: AttachmentPickerProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  const addFiles = (picked: FileList | null) => {
    if (!picked || picked.length === 0) return
    onChange([...files, ...Array.from(picked)])
    // Reset so re-picking the same file still fires onChange.
    if (inputRef.current) inputRef.current.value = ''
  }

  const removeAt = (idx: number) => onChange(files.filter((_, i) => i !== idx))

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        disabled={disabled}
        onChange={(e) => addFiles(e.target.files)}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
        title="Attach files"
      >
        <Paperclip className="size-4" />
      </Button>

      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((f, i) => (
            <span
              key={`${f.name}-${i}`}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
            >
              <span className="max-w-40 truncate">{f.name}</span>
              <span className="text-muted-foreground/70">{humanSize(f.size)}</span>
              <button
                type="button"
                onClick={() => removeAt(i)}
                disabled={disabled}
                className="text-muted-foreground/70 hover:text-foreground"
                aria-label={`Remove ${f.name}`}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}
