import { useRef, useEffect } from 'react'
import type { DisplayMessage } from '../types'

interface Props {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  imageUrl?: string | null
  onImageChange?: (imageUrl: string | null) => void
  disabled?: boolean
  quotedMessage?: DisplayMessage | null
  onClearQuote?: () => void
  userName?: string
}

export default function Composer({
  value,
  onChange,
  onSend,
  imageUrl,
  onImageChange,
  disabled,
  quotedMessage,
  onClearQuote,
  userName,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

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

  async function handleFileChange(file: File | null) {
    if (!file || !onImageChange) return
    if (!file.type.startsWith('image/')) return

    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : null
      onImageChange(result)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
    reader.readAsDataURL(file)
  }

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

      {imageUrl && (
        <div className="composer-image-preview">
          <img src={imageUrl} alt="待送出的圖片" className="composer-image-preview__img" />
          <button
            className="composer-image-preview__remove"
            onClick={() => onImageChange?.(null)}
            aria-label="移除圖片"
            disabled={disabled}
          >
            ✕
          </button>
        </div>
      )}

      <div className="composer">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          style={{ display: 'none' }}
          onChange={(e) => handleFileChange(e.target.files?.[0] ?? null)}
        />
        <button
          className="composer-attach"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled}
          aria-label="加入圖片"
          type="button"
        >
          📷
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
          disabled={disabled || (!value.trim() && !imageUrl)}
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
  align-self: flex-end;
  max-width: min(240px, 72vw);
}
.composer-image-preview__img {
  display: block;
  width: 100%;
  border-radius: 16px;
  border: 1px solid var(--border);
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.18);
}
.composer-image-preview__remove {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: rgba(18, 22, 34, 0.78);
  color: #fff;
  border: 1px solid rgba(255,255,255,0.12);
  font-size: 14px;
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
