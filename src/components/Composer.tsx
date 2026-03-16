import { useRef, useEffect } from 'react'
import type { DisplayMessage } from '../types'

interface Props {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  imageUrls?: string[]
  onImageChange?: (imageUrls: string[]) => void
  disabled?: boolean
  quotedMessage?: DisplayMessage | null
  onClearQuote?: () => void
  userName?: string
}

export default function Composer({
  value,
  onChange,
  onSend,
  imageUrls,
  onImageChange,
  disabled,
  quotedMessage,
  onClearQuote,
  userName,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const imageInputRef = useRef<HTMLInputElement>(null)
  const txtInputRef = useRef<HTMLInputElement>(null)

  // 隨內容自動調整高度，最高 160px
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [value])

  // 引用後自動 focus 輸入框
  useEffect(() => {
    if (quotedMessage) {
      textareaRef.current?.focus()
    }
  }, [quotedMessage])

  const senderName = quotedMessage
    ? (quotedMessage.role === 'assistant' ? 'M' : (userName || '你'))
    : ''

  const previewText = quotedMessage
    ? (quotedMessage.content.length > 80
        ? quotedMessage.content.slice(0, 80) + '…'
        : quotedMessage.content)
    : ''

  async function handleImageFilesChange(files: FileList | null) {
    if (!files || !onImageChange) return
    const results: string[] = []
    for (const file of Array.from(files)) {
      if (!file.type.startsWith('image/')) continue
      const url = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(reader.result as string)
        reader.onerror = reject
        reader.readAsDataURL(file)
      })
      results.push(url)
    }
    if (results.length > 0) {
      onImageChange([...(imageUrls ?? []), ...results])
    }
    if (imageInputRef.current) imageInputRef.current.value = ''
  }

  async function handleTxtFileChange(file: File | null) {
    if (!file) return
    const content = await file.text()
    const sep = '─'.repeat(20)
    const block = `【附件：${file.name}】\n${sep}\n${content}\n${sep}\n\n`
    onChange(block + value)
    if (txtInputRef.current) txtInputRef.current.value = ''
    textareaRef.current?.focus()
  }

  const hasImages = (imageUrls ?? []).length > 0

  return (
    <div className="composer-outer">
      {/* 引用預覽 */}
      {quotedMessage && (
        <div className="composer-quote-preview">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 3 }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--accent)' }}>
              引用 {senderName}
            </span>
            <button
              onClick={onClearQuote}
              style={{ fontSize: 14, color: 'var(--text-muted)', lineHeight: 1, padding: '0 2px' }}
              aria-label="取消引用"
            >
              ✕
            </button>
          </div>
          <p style={{
            fontSize: 12,
            color: 'var(--text-secondary)',
            lineHeight: 1.4,
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            margin: 0,
          }}>
            {previewText}
          </p>
        </div>
      )}

      {/* 多圖預覽 */}
      {hasImages && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          {(imageUrls ?? []).map((url, i) => (
            <div key={i} className="composer-image-preview">
              <img src={url} alt={`待送出的圖片 ${i + 1}`} className="composer-image-preview__img" />
              <button
                className="composer-image-preview__remove"
                onClick={() => onImageChange?.((imageUrls ?? []).filter((_, j) => j !== i))}
                aria-label="移除圖片"
                disabled={disabled}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="composer">
        {/* 圖片 input（multiple） */}
        <input
          ref={imageInputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: 'none' }}
          onChange={(e) => handleImageFilesChange(e.target.files)}
        />
        {/* TXT input */}
        <input
          ref={txtInputRef}
          type="file"
          accept=".txt,text/plain"
          style={{ display: 'none' }}
          onChange={(e) => handleTxtFileChange(e.target.files?.[0] ?? null)}
        />
        <button
          className="composer-attach"
          onClick={() => imageInputRef.current?.click()}
          disabled={disabled}
          aria-label="加入圖片"
          type="button"
        >
          📷
        </button>
        <button
          className="composer-attach"
          onClick={() => txtInputRef.current?.click()}
          disabled={disabled}
          aria-label="加入文字檔"
          type="button"
          style={{ fontSize: 16 }}
        >
          📄
        </button>
        <textarea
          ref={textareaRef}
          className="composer-input"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="輸入訊息… (↑ 送出)"
          disabled={disabled}
          rows={1}
        />
        <button
          className="composer-send"
          onClick={onSend}
          disabled={disabled || (!value.trim() && !hasImages)}
          aria-label="送出"
        >
          ↑
        </button>
      </div>

      <style>{COMPOSER_STYLES}</style>
    </div>
  )
}

const COMPOSER_STYLES = `
.composer-outer {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.composer-quote-preview {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent-dim);
  border-radius: var(--radius-md);
  padding: 7px 10px;
}
.composer-image-preview {
  position: relative;
  max-width: min(120px, 36vw);
}
.composer-image-preview__img {
  display: block;
  width: 100%;
  border-radius: 12px;
  border: 1px solid var(--border);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.18);
}
.composer-image-preview__remove {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 24px;
  height: 24px;
  border-radius: 999px;
  background: rgba(18, 22, 34, 0.78);
  color: #fff;
  border: 1px solid rgba(255,255,255,0.12);
  font-size: 12px;
}
.composer-attach {
  flex: 0 0 auto;
  align-self: flex-end;
  width: 42px;
  height: 42px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.composer-attach:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
`
