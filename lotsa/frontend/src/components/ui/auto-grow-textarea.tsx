import * as React from "react"

import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"

interface AutoGrowTextareaProps
  extends Omit<React.ComponentProps<"textarea">, "onKeyDown"> {
  /** Called on Cmd/Ctrl+Enter. The component calls preventDefault for you. */
  onSubmit: () => void
  /** Max visible rows before the field scrolls internally. Default 10. */
  maxRows?: number
}

/**
 * Shared multi-line input for the new-task and chat-bar surfaces.
 *
 * - Grows with content (reset height, measure ``scrollHeight``) up to
 *   ``maxRows`` lines, then scrolls internally; shrinks as text is removed.
 * - Plain ``Enter`` inserts a newline; ``Cmd/Ctrl+Enter`` calls ``onSubmit``.
 *
 * The cap tracks the computed line-height rather than a hardcoded pixel value,
 * so it follows the font.
 */
function AutoGrowTextarea({
  onSubmit,
  maxRows = 10,
  className,
  value,
  ...props
}: AutoGrowTextareaProps) {
  const ref = React.useRef<HTMLTextAreaElement>(null)

  const resize = React.useCallback(() => {
    const el = ref.current
    if (!el) return

    // Reset so ``scrollHeight`` reflects the content, not the prior height.
    el.style.height = "auto"

    const styles = window.getComputedStyle(el)
    let lineHeight = parseFloat(styles.lineHeight)
    if (Number.isNaN(lineHeight)) {
      // ``line-height: normal`` doesn't resolve to px — fall back to the font.
      const fontSize = parseFloat(styles.fontSize) || 16
      lineHeight = fontSize * 1.5
    }
    const paddingTop = parseFloat(styles.paddingTop) || 0
    const paddingBottom = parseFloat(styles.paddingBottom) || 0
    const borderY =
      (parseFloat(styles.borderTopWidth) || 0) +
      (parseFloat(styles.borderBottomWidth) || 0)
    const maxHeight = lineHeight * maxRows + paddingTop + paddingBottom

    // ``scrollHeight`` excludes the border, but ``box-sizing: border-box``
    // (the Tailwind default, and this field has a 1px border) means the
    // height we set includes it. Add the border back so the interior fits
    // content + padding exactly instead of clipping the bottom ~2px. The
    // overflow comparison stays in border-excluded terms (both sides exclude
    // it), so the cap still triggers at maxRows lines of content.
    el.style.height = `${Math.min(el.scrollHeight, maxHeight) + borderY}px`
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden"
  }, [maxRows])

  // useLayoutEffect (not useEffect) avoids a one-frame flash of the wrong
  // height. Re-runs on every controlled-value change, so grow/shrink/reset
  // (after the parent clears the field on submit) all track the content.
  React.useLayoutEffect(() => {
    resize()
  }, [value, resize])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      onSubmit()
    }
  }

  return (
    <Textarea
      ref={ref}
      rows={1}
      value={value}
      onKeyDown={handleKeyDown}
      className={cn("min-h-11", className)}
      {...props}
    />
  )
}

export { AutoGrowTextarea }
