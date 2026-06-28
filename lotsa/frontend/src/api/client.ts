const BASE_URL = '' // same origin, proxied in dev

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`
    try {
      const body = await resp.json()
      if (body?.detail?.error) message = body.detail.error
      else if (body?.error) message = body.error
    } catch { /* non-JSON error body */ }
    throw new Error(message)
  }
  return resp.json()
}
