// Vitest global setup: extend `expect` with jest-dom matchers and clean up
// the DOM between tests.
import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
})

// jsdom implements neither ResizeObserver, pointer capture, nor PointerEvent
// itself, all of which Radix's Popper-positioned content (DropdownMenu,
// Select, Tooltip, ...) relies on internally. Without PointerEvent in
// particular, `fireEvent.pointerDown` falls back to a plain `Event` that has
// no `.button`/`.ctrlKey`, so Radix's trigger never sees a main-button click
// and silently never opens — the polyfill below is what makes that possible
// to test at all.
if (typeof window !== 'undefined') {
  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }
  const proto = window.HTMLElement.prototype as unknown as {
    hasPointerCapture?: () => boolean
    setPointerCapture?: () => void
    releasePointerCapture?: () => void
  }
  proto.hasPointerCapture ??= () => false
  proto.setPointerCapture ??= () => {}
  proto.releasePointerCapture ??= () => {}

  if (!window.PointerEvent) {
    class PointerEventPolyfill extends MouseEvent {
      public pointerType: string
      public pointerId: number
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init)
        this.pointerType = init.pointerType ?? 'mouse'
        this.pointerId = init.pointerId ?? 0
      }
    }
    window.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent
  }
}
