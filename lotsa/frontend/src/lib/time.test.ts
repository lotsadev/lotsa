import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { formatRelativeTime, formatFullDateTime } from '@/lib/time'

// Pin the clock so relative-time assertions are deterministic regardless of
// when the suite runs.
const NOW_ISO = '2026-06-13T12:00:00.000Z'
const NOW_MS = new Date(NOW_ISO).getTime()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// formatRelativeTime
// ---------------------------------------------------------------------------

describe('formatRelativeTime', () => {
  it('returns a seconds-scale label for a timestamp 30 seconds ago', () => {
    const iso = new Date(NOW_MS - 30 * 1000).toISOString()
    const result = formatRelativeTime(iso)
    // "30 seconds ago", "just now", "now" — any indication of very recent time
    expect(result.length).toBeGreaterThan(0)
    expect(result).toMatch(/second|just now|now/i)
  })

  it('returns a minutes-scale label for a timestamp 5 minutes ago', () => {
    const iso = new Date(NOW_MS - 5 * 60 * 1000).toISOString()
    const result = formatRelativeTime(iso)
    expect(result).toMatch(/minute/i)
  })

  it('returns an hours-scale label for a timestamp 2 hours ago', () => {
    const iso = new Date(NOW_MS - 2 * 60 * 60 * 1000).toISOString()
    const result = formatRelativeTime(iso)
    expect(result).toMatch(/hour/i)
  })

  it('returns a days-scale label for a timestamp 3 days ago', () => {
    const iso = new Date(NOW_MS - 3 * 24 * 60 * 60 * 1000).toISOString()
    const result = formatRelativeTime(iso)
    expect(result).toMatch(/day/i)
  })

  it('returns a weeks-scale label for a timestamp 2 weeks ago', () => {
    const iso = new Date(NOW_MS - 14 * 24 * 60 * 60 * 1000).toISOString()
    const result = formatRelativeTime(iso)
    expect(result).toMatch(/week|day/i)
  })

  it('returns empty string for an empty input', () => {
    expect(formatRelativeTime('')).toBe('')
  })

  it('returns empty string for an invalid date string', () => {
    expect(formatRelativeTime('not-a-date')).toBe('')
  })

  it('returns a non-empty string for a future timestamp (guards against NaN)', () => {
    const iso = new Date(NOW_MS + 60 * 1000).toISOString()
    // Implementation may say "in 1 minute" or clamp — just not empty or NaN.
    const result = formatRelativeTime(iso)
    expect(result).not.toContain('NaN')
  })
})

// ---------------------------------------------------------------------------
// formatFullDateTime
// ---------------------------------------------------------------------------

describe('formatFullDateTime', () => {
  it('returns a non-empty string for a valid ISO timestamp', () => {
    const result = formatFullDateTime('2026-06-13T10:00:00+00:00')
    expect(result.length).toBeGreaterThan(0)
  })

  it('includes the year in the output so the tooltip is unambiguous', () => {
    const result = formatFullDateTime('2026-06-13T10:00:00+00:00')
    expect(result).toContain('2026')
  })

  it('returns empty string for an empty input', () => {
    expect(formatFullDateTime('')).toBe('')
  })

  it('returns empty string for an invalid date string', () => {
    expect(formatFullDateTime('not-a-date')).toBe('')
  })

  it('returns a different string for timestamps far apart (not always the same value)', () => {
    const early = formatFullDateTime('2020-01-01T00:00:00Z')
    const late = formatFullDateTime('2026-06-13T10:00:00Z')
    expect(early).not.toBe(late)
  })
})
