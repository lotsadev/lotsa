const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

const THRESHOLDS: [number, Intl.RelativeTimeFormatUnit][] = [
  [60, 'second'],
  [3600, 'minute'],
  [86400, 'hour'],
  [604800, 'day'],
  [2592000, 'week'],
  [31536000, 'month'],
  [Infinity, 'year'],
]

export function formatRelativeTime(iso: string): string {
  if (!iso) return ''
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return ''

  const diffSeconds = (ms - Date.now()) / 1000
  let prev = 1
  for (const [threshold, unit] of THRESHOLDS) {
    if (Math.abs(diffSeconds) < threshold) {
      return rtf.format(Math.round(diffSeconds / prev), unit)
    }
    prev = threshold
  }
  return ''
}

export function formatFullDateTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString()
}
