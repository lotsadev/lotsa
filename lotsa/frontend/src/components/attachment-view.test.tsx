import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { Attachment } from '@/api/types'

// The raw-URL helper is a sibling contract shipped in the same change; stub it
// so this unit test exercises only the presentational component.
vi.mock('@/api/tasks', () => ({
  attachmentRawUrl: (taskId: string, filename: string) =>
    `/api/tasks/${taskId}/attachments/${encodeURIComponent(filename)}/raw`,
}))

// Import AFTER the mock. The component does not exist yet, so this import fails
// as the expected red.
import { AttachmentItem } from './attachment-view'

function att(overrides: Partial<Attachment> = {}): Attachment {
  return {
    filename: 'shot.png',
    rel_path: '.lotsa/attachments/shot.png',
    mime: 'image/png',
    size_bytes: 2048,
    created_at: '2026-07-02T00:00:00+00:00',
    ...overrides,
  }
}

afterEach(cleanup)

describe('AttachmentItem', () => {
  it('renders an image as a clickable thumbnail linking to the raw endpoint', () => {
    render(<AttachmentItem taskId="task-1" att={att()} />)
    const raw = '/api/tasks/task-1/attachments/shot.png/raw'
    const img = screen.getByRole('img')
    expect(img.getAttribute('src')).toBe(raw)
    // The thumbnail opens the full-size file in a new tab.
    const link = img.closest('a')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')).toBe(raw)
    expect(link!.getAttribute('target')).toBe('_blank')
  })

  it('renders a non-image as a chip with name + size linking to raw', () => {
    render(<AttachmentItem taskId="task-1" att={att({ filename: 'notes.pdf', mime: 'application/pdf' })} />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('/api/tasks/task-1/attachments/notes.pdf/raw')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.textContent).toContain('notes.pdf')
    // Size rendered via the shared formatBytes (2048 B → "2.0 KB").
    expect(link.textContent).toMatch(/KB/)
    // A non-image must not render an <img> preview.
    expect(screen.queryByRole('img')).toBeNull()
  })
})
