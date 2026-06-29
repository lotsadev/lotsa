import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// Spec for the shared multi-line, auto-growing, Cmd/Ctrl+Enter-submitting
// input that replaces the single-line <Input> in empty-state.tsx and
// chat-input.tsx: prop forwarding, the submit gesture, and the auto-grow
// height behaviour.
import { AutoGrowTextarea } from '@/components/ui/auto-grow-textarea'

describe('AutoGrowTextarea — element and props', () => {
  it('renders a textarea element (not a single-line input)', () => {
    render(<AutoGrowTextarea value="" onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox')
    expect(field.tagName).toBe('TEXTAREA')
  })

  it('forwards placeholder, value and disabled to the textarea', () => {
    render(
      <AutoGrowTextarea
        value="hello"
        onChange={() => {}}
        onSubmit={() => {}}
        placeholder="Describe your task..."
        disabled
      />,
    )
    const field = screen.getByPlaceholderText('Describe your task...')
    expect(field).toHaveValue('hello')
    expect(field).toBeDisabled()
  })

  it('merges a caller-supplied className with the defaults', () => {
    render(
      <AutoGrowTextarea value="" onChange={() => {}} onSubmit={() => {}} className="flex-1" />,
    )
    expect(screen.getByRole('textbox').className).toMatch(/flex-1/)
  })

  it('rests at a larger footprint than a 36px single-line input', () => {
    // The plan delivers the "roomier than h-9" resting height via rows={1}
    // plus a min-height utility class, rather than the old fixed h-9.
    render(<AutoGrowTextarea value="" onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox')
    expect(field).toHaveAttribute('rows', '1')
    expect(field.className).toMatch(/min-h-/)
  })
})

describe('AutoGrowTextarea — submit gesture', () => {
  it('does not submit on a plain Enter (Enter inserts a newline)', () => {
    const onSubmit = vi.fn()
    render(<AutoGrowTextarea value="line one" onChange={() => {}} onSubmit={onSubmit} />)
    const field = screen.getByRole('textbox')

    const notPrevented = fireEvent.keyDown(field, { key: 'Enter' })

    expect(onSubmit).not.toHaveBeenCalled()
    // Default not prevented -> the browser inserts a newline.
    expect(notPrevented).toBe(true)
  })

  it('submits on Cmd+Enter (metaKey) and prevents the default newline', () => {
    const onSubmit = vi.fn()
    render(<AutoGrowTextarea value="task text" onChange={() => {}} onSubmit={onSubmit} />)
    const field = screen.getByRole('textbox')

    const notPrevented = fireEvent.keyDown(field, { key: 'Enter', metaKey: true })

    expect(onSubmit).toHaveBeenCalledTimes(1)
    // preventDefault was called -> fireEvent reports the event as cancelled.
    expect(notPrevented).toBe(false)
  })

  it('submits on Ctrl+Enter (ctrlKey) and prevents the default newline', () => {
    const onSubmit = vi.fn()
    render(<AutoGrowTextarea value="task text" onChange={() => {}} onSubmit={onSubmit} />)
    const field = screen.getByRole('textbox')

    const notPrevented = fireEvent.keyDown(field, { key: 'Enter', ctrlKey: true })

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(notPrevented).toBe(false)
  })

  it('does not submit on Cmd/Ctrl + a non-Enter key', () => {
    const onSubmit = vi.fn()
    render(<AutoGrowTextarea value="text" onChange={() => {}} onSubmit={onSubmit} />)
    const field = screen.getByRole('textbox')

    fireEvent.keyDown(field, { key: 'a', metaKey: true })
    fireEvent.keyDown(field, { key: 'k', ctrlKey: true })

    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('AutoGrowTextarea — auto-grow height', () => {
  // jsdom has no layout engine, so scrollHeight and computed styles must be
  // stubbed deterministically. Line-height 20px + 8px top/bottom padding means
  // the 10-line cap is 20*10 + 16 = 216px.
  let mockScrollHeight = 0

  beforeEach(() => {
    Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => mockScrollHeight,
    })
    vi.spyOn(window, 'getComputedStyle').mockReturnValue({
      lineHeight: '20px',
      paddingTop: '8px',
      paddingBottom: '8px',
      fontSize: '16px',
    } as unknown as CSSStyleDeclaration)
  })

  afterEach(() => {
    // Remove the own-property shadow so the inherited getter is restored.
    delete (HTMLTextAreaElement.prototype as unknown as { scrollHeight?: unknown }).scrollHeight
    vi.restoreAllMocks()
  })

  it('sizes the field to its content height below the cap (no scroll)', () => {
    mockScrollHeight = 80
    render(<AutoGrowTextarea value={'a\nb\nc'} onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('80px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('caps at ~10 lines and switches to internal scroll past the cap', () => {
    mockScrollHeight = 1000
    render(<AutoGrowTextarea value={'many\nlines'} onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('216px')
    expect(field.style.overflowY).toBe('auto')
  })

  it('shrinks back down as content is removed', () => {
    mockScrollHeight = 300
    const { rerender } = render(
      <AutoGrowTextarea value={'lots\nof\ntext\nhere'} onChange={() => {}} onSubmit={() => {}} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(field.style.height).toBe('216px')

    mockScrollHeight = 40
    rerender(<AutoGrowTextarea value={'x'} onChange={() => {}} onSubmit={() => {}} />)

    expect(field.style.height).toBe('40px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('respects a custom maxRows cap', () => {
    mockScrollHeight = 1000
    // maxRows=3 -> cap = 20*3 + 16 = 76px.
    render(
      <AutoGrowTextarea value={'a\nb\nc\nd'} onChange={() => {}} onSubmit={() => {}} maxRows={3} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('76px')
    expect(field.style.overflowY).toBe('auto')
  })

  it('adds the border width so border-box height does not clip content', () => {
    // The field has a 1px border and box-sizing: border-box, so the height
    // we set includes the border while scrollHeight excludes it. Without
    // adding the border back, the interior is 2px short and clips the bottom
    // padding. 80 (content+padding) + 2 (1px top + 1px bottom border) = 82.
    vi.spyOn(window, 'getComputedStyle').mockReturnValue({
      lineHeight: '20px',
      paddingTop: '8px',
      paddingBottom: '8px',
      borderTopWidth: '1px',
      borderBottomWidth: '1px',
      fontSize: '16px',
    } as unknown as CSSStyleDeclaration)
    mockScrollHeight = 80
    render(<AutoGrowTextarea value={'a\nb\nc'} onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('82px')
    expect(field.style.overflowY).toBe('hidden')
  })
})

describe('AutoGrowTextarea — minRows floor', () => {
  // Same deterministic layout stubs as the auto-grow-height suite: line-height
  // 20px + 8px top/bottom padding. So a minRows floor of N is 20*N + 16 px, and
  // the default 10-line cap stays 20*10 + 16 = 216px.
  let mockScrollHeight = 0

  beforeEach(() => {
    Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => mockScrollHeight,
    })
    vi.spyOn(window, 'getComputedStyle').mockReturnValue({
      lineHeight: '20px',
      paddingTop: '8px',
      paddingBottom: '8px',
      fontSize: '16px',
    } as unknown as CSSStyleDeclaration)
  })

  afterEach(() => {
    delete (HTMLTextAreaElement.prototype as unknown as { scrollHeight?: unknown }).scrollHeight
    vi.restoreAllMocks()
  })

  it('floors the field at minRows when the content is shorter', () => {
    // Content height 40px is well below the 7-row floor (20*7 + 16 = 156px),
    // so the field renders at the floor, not at the content height.
    mockScrollHeight = 40
    render(
      <AutoGrowTextarea value="x" onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('156px')
    // Below the cap, so no internal scroll.
    expect(field.style.overflowY).toBe('hidden')
  })

  it('honours a minRows floor other than the new-task page default', () => {
    // minRows=3 -> floor = 20*3 + 16 = 76px. Content of 20px is below it.
    mockScrollHeight = 20
    render(
      <AutoGrowTextarea value="x" onChange={() => {}} onSubmit={() => {}} minRows={3} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('76px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('grows past the floor when content exceeds minRows but stays under the cap', () => {
    // Content 180px sits between the 7-row floor (156px) and the 10-row cap
    // (216px): the field tracks the content, not the floor.
    mockScrollHeight = 180
    render(
      <AutoGrowTextarea value={'a\nb\nc'} onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('180px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('still caps at maxRows and scrolls when content exceeds the cap, floor notwithstanding', () => {
    // The floor must never suppress the existing maxRows cap / overflow trigger.
    mockScrollHeight = 1000
    render(
      <AutoGrowTextarea value={'many\nlines'} onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('216px')
    expect(field.style.overflowY).toBe('auto')
  })

  it('shrinks back to the floor (never below) as content is removed', () => {
    mockScrollHeight = 1000
    const { rerender } = render(
      <AutoGrowTextarea value={'lots\nof\ntext'} onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(field.style.height).toBe('216px')

    mockScrollHeight = 30
    rerender(
      <AutoGrowTextarea value={'x'} onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )

    // Shrinks toward the floor, not down to the 30px content height.
    expect(field.style.height).toBe('156px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('keeps the border-box correction when flooring (border added on top of the floor)', () => {
    // 1px top + 1px bottom border + box-sizing: border-box. Floor of 7 rows is
    // 156px in border-excluded terms; the height we set adds the 2px border so
    // the interior isn't clipped: 156 + 2 = 158px.
    vi.spyOn(window, 'getComputedStyle').mockReturnValue({
      lineHeight: '20px',
      paddingTop: '8px',
      paddingBottom: '8px',
      borderTopWidth: '1px',
      borderBottomWidth: '1px',
      fontSize: '16px',
    } as unknown as CSSStyleDeclaration)
    mockScrollHeight = 40
    render(
      <AutoGrowTextarea value="x" onChange={() => {}} onSubmit={() => {}} minRows={7} />,
    )
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('158px')
    expect(field.style.overflowY).toBe('hidden')
  })

  it('defaults to a single-row floor so the chat-bar surface is unchanged', () => {
    // No minRows -> default of 1 -> floor = 20*1 + 16 = 36px, below the 40px
    // content, so the field tracks the content exactly as it did before the
    // floor existed. This guards the chat-bar consumer from regressing.
    mockScrollHeight = 40
    render(<AutoGrowTextarea value="x" onChange={() => {}} onSubmit={() => {}} />)
    const field = screen.getByRole('textbox') as HTMLTextAreaElement

    expect(field.style.height).toBe('40px')
    expect(field.style.overflowY).toBe('hidden')
  })
})
