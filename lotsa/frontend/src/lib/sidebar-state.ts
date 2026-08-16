// Persistence for the sidebar's open/closed preference.
//
// Upstream shadcn stores this in a `sidebar_state` cookie, because its
// reference implementation is Next.js and the *server* reads that cookie during
// SSR to render the correct state on first paint. This dashboard is a pure
// client SPA served as static files — no server-side code reads the value — so
// the cookie bought nothing while still being sent on every same-origin
// request. localStorage matches every other UI preference here (theme, last
// project, the chat/artifacts split) and keeps PRIVACY.md's "preferences stay
// in localStorage and never leave your device" literally true.

export const SIDEBAR_STORAGE_KEY = 'lotsa-sidebar-open'

/** The stored preference, defaulting to open when absent or unparseable. */
export function readSidebarOpen(): boolean {
  if (typeof localStorage === 'undefined') return true
  const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY)
  if (stored === 'true') return true
  if (stored === 'false') return false
  return true
}

export function writeSidebarOpen(open: boolean): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(SIDEBAR_STORAGE_KEY, String(open))
}
