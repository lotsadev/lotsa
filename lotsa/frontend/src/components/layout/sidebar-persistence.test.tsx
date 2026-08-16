import { render, screen, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SidebarProvider, Sidebar, SidebarTrigger } from '@/components/ui/sidebar'
import { readSidebarOpen, SIDEBAR_STORAGE_KEY } from '@/lib/sidebar-state'

// The sidebar open/closed preference is persisted in localStorage, like every
// other UI preference in this app (theme, last project, the chat/artifacts
// split). Upstream shadcn uses a cookie here because its reference
// implementation is Next.js and the *server* reads that cookie during SSR to
// avoid a wrong-state first paint. This dashboard is a pure client SPA served
// as static files — no Python module ever reads `sidebar_state` — so the cookie
// bought nothing and was sent on every same-origin request regardless.
//
// Keeping it out of cookies is also what makes PRIVACY.md's claim ("UI
// preferences are stored in your browser's localStorage, which never leaves
// your device") literally true rather than nearly true.

// `useIsMobile` calls `window.matchMedia`, which jsdom doesn't implement. Stub
// it locally rather than in `src/test/setup.ts` — PR #29 is concurrently adding
// a PointerEvent polyfill to that shared file, and this doesn't need to be
// global. jsdom's default 1024px `innerWidth` puts us on the desktop path.
beforeEach(() => {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
})

function Harness() {
  return (
    <SidebarProvider defaultOpen={readSidebarOpen()}>
      <Sidebar collapsible="offcanvas">
        <div>task list</div>
      </Sidebar>
      <SidebarTrigger />
    </SidebarProvider>
  )
}

describe('sidebar open/closed persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    // Wipe any cookie a previous implementation may have left behind.
    document.cookie = 'sidebar_state=; path=/; max-age=0'
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('persists the collapsed state to localStorage, not a cookie', () => {
    render(<Harness />)

    fireEvent.click(screen.getByRole('button', { name: 'Toggle Sidebar' }))

    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe('false')
    expect(document.cookie).not.toContain('sidebar_state')
  })

  it('round-trips through readSidebarOpen so the choice survives a reload', () => {
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Toggle Sidebar' }))
    unmount()

    // What a fresh page load would read.
    expect(readSidebarOpen()).toBe(false)

    const { container } = render(<Harness />)
    expect(container.querySelector('[data-collapsible]')).toHaveAttribute(
      'data-state',
      'collapsed'
    )
  })

  it('defaults to open when nothing is stored', () => {
    expect(readSidebarOpen()).toBe(true)
  })

  it('defaults to open when the stored value is junk', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, 'not-a-bool')
    expect(readSidebarOpen()).toBe(true)
  })
})
