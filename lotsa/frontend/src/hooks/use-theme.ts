import { useSyncExternalStore } from 'react'

type Theme = 'dark' | 'light'

// Module-level store so every useTheme() consumer shares one reactive value.
// The previous implementation was per-instance useState seeded from
// localStorage: toggling the theme in the header re-rendered only the
// header's hook instance, and components passing the JS value onward (e.g.
// the Changes tab handing themeType to @pierre/diffs) never updated until
// remount. Tailwind `dark:` styles masked the bug everywhere else because
// they react to the <html> class, not the hook.
let currentTheme: Theme = (() => {
  if (typeof window === 'undefined') return 'dark'
  return (localStorage.getItem('lotsa-theme') as Theme) || 'dark'
})()

const listeners = new Set<() => void>()

function applyTheme(theme: Theme) {
  currentTheme = theme
  if (typeof window !== 'undefined') {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem('lotsa-theme', theme)
  }
  listeners.forEach((l) => l())
}

// Apply on module load so the class is set before first paint.
if (typeof window !== 'undefined') {
  document.documentElement.classList.toggle('dark', currentTheme === 'dark')
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useTheme() {
  const theme = useSyncExternalStore(
    subscribe,
    () => currentTheme,
    () => 'dark' as Theme,
  )
  const setTheme = applyTheme
  const toggleTheme = () => applyTheme(currentTheme === 'dark' ? 'light' : 'dark')
  return { theme, setTheme, toggleTheme }
}
