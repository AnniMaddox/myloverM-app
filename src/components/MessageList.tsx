import { useEffect, useRef, useState } from 'react'
import type { DisplayMessage } from '../types'

interface Props {
  messages: DisplayMessage[]
  checkpointIndex?: number
  isStreaming?: boolean
  selectMode?: boolean
  selectedIds?: Set<string>
  onToggleSelect?: (id: string) => void
  onQuote?: (msg: DisplayMessage) => void
  onEdit?: (msg: DisplayMessage) => void
  onDelete?: (msg: DisplayMessage) => void
  onReroll?: (msg: DisplayMessage) => void
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-TW', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function MessageList({
  messages,
  checkpointIndex,
  isStreaming,
  selectMode,
  selectedIds,
  onToggleSelect,
  onQuote,
  onEdit,
  onDelete,
  onReroll,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const [activeActionId, setActiveActionId] = useState<string | null>(null)
  const [expandedThinking, setExpandedThinking] = useState<Record<string, boolean>>({})

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // 進入選取模式或 streaming 時關閉 action bar
  useEffect(() => {
    if (selectMode || isStreaming) setActiveActionId(null)
  }, [selectMode, isStreaming])

  if (messages.length === 0) {
    return (
      <div className="chat-empty">
        <p className="chat-empty-hint">開始和 M 聊天吧</p>
      </div>
    )
  }

  return (
    <div className="message-list" onClick={() => setActiveActionId(null)}>
      {messages.map((msg, index) => {
        const showImage = msg.role === 'user' && (Boolean(msg.imageUrl) || Boolean(msg.hasImage))
        const showBubble = Boolean(msg.quotedMessageRef || msg.content || msg.isStreaming || msg.isError)

        return (
        <div key={msg.id}>
          {/* 外層 row：處理 checkbox + 整體對齊 */}
          <div
            style={{
              display: 'flex',
              flexDirection: 'row',
              alignItems: 'flex-end',
              gap: 8,
              justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
            }}
          >
            {/* Checkbox 左側（M 的訊息） */}
            {selectMode && msg.role === 'assistant' && (
              <input
                type="checkbox"
                checked={selectedIds?.has(msg.id) ?? false}
                onChange={() => onToggleSelect?.(msg.id)}
                style={{
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  accentColor: 'var(--accent)',
                  cursor: 'pointer',
                  marginBottom: 6,
                }}
              />
            )}

            {/* 主欄：泡泡 + action bar + 時間 */}
            <div
              className={`msg-row msg-row--${msg.role}`}
              style={{ minWidth: 0, maxWidth: selectMode ? 'calc(100% - 26px)' : '100%' }}
            >
              {showImage && (
                <div
                  className={`image-card image-card--${msg.role}`}
                  onClick={(e) => {
                    if (selectMode || msg.isStreaming) return
                    e.stopPropagation()
                    setActiveActionId(activeActionId === msg.id ? null : msg.id)
                  }}
                  style={{ cursor: selectMode || msg.isStreaming ? 'default' : 'pointer' }}
                >
                  {msg.imageUrl ? (
                    <img src={msg.imageUrl} alt="使用者傳送的圖片" className="image-card__img" />
                  ) : (
                    <div className="image-card__placeholder">已傳送圖片，原圖未保留</div>
                  )}
                </div>
              )}

              {/* 泡泡 */}
              {showBubble && (
                <div
                  className={[
                    'bubble',
                    `bubble--${msg.role}`,
                    msg.isError ? 'bubble--error' : '',
                  ].join(' ').trim()}
                  onClick={(e) => {
                    if (selectMode || msg.isStreaming) return
                    e.stopPropagation()
                    setActiveActionId(activeActionId === msg.id ? null : msg.id)
                  }}
                  style={{ cursor: selectMode || msg.isStreaming ? 'default' : 'pointer' }}
                >
                  {/* 引用區塊 */}
                  {msg.quotedMessageRef && (
                    <div className="quote-block">
                      <span className="quote-name">{msg.quotedMessageRef.senderName}</span>
                      <p className="quote-content">
                        {msg.quotedMessageRef.content.length > 120
                          ? msg.quotedMessageRef.content.slice(0, 120) + '…'
                          : msg.quotedMessageRef.content}
                      </p>
                    </div>
                  )}
                  {msg.content || (msg.isStreaming ? '' : '（無回應）')}
                  {msg.isStreaming && !msg.isError && (
                    <span className="streaming-cursor" aria-hidden />
                  )}
                </div>
              )}

              {/* 思考過程折疊區塊（Anthropic extended thinking） */}
              {!msg.isStreaming && msg.role === 'assistant' && msg.thinkingContent && (
                <div style={{ marginTop: 4, marginBottom: 4 }}>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      setExpandedThinking((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))
                    }}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      background: 'rgba(255,255,255,0.04)',
                      border: '1px solid rgba(255,255,255,0.10)',
                      borderRadius: 8, padding: '3px 10px',
                      fontSize: 11, color: 'var(--text-secondary)',
                      cursor: 'pointer', lineHeight: 1.6,
                    }}
                  >
                    <span style={{ fontSize: 9, opacity: 0.7 }}>{expandedThinking[msg.id] ? '▼' : '▶'}</span>
                    思考過程
                  </button>
                  {expandedThinking[msg.id] && (
                    <div style={{
                      marginTop: 6,
                      background: 'rgba(255,255,255,0.03)',
                      border: '1px solid rgba(255,255,255,0.08)',
                      borderRadius: 10,
                      padding: '10px 12px',
                      fontSize: 12,
                      color: 'var(--text-secondary)',
                      lineHeight: 1.7,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      maxHeight: 360,
                      overflowY: 'auto',
                    }}>
                      {msg.thinkingContent}
                    </div>
                  )}
                </div>
              )}

              {/* Action bar */}
              {!selectMode && !msg.isStreaming && !msg.isError && activeActionId === msg.id && (
                <div
                  className={`action-bar action-bar--${msg.role}`}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    className="action-btn"
                    onClick={(e) => { e.stopPropagation(); onQuote?.(msg); setActiveActionId(null) }}
                  >
                    引用
                  </button>
                  {msg.role === 'user' && !msg.hasImage && (
                    <button
                      className="action-btn"
                      onClick={(e) => { e.stopPropagation(); onEdit?.(msg); setActiveActionId(null) }}
                    >
                      編輯
                    </button>
                  )}
                  {msg.role === 'assistant' && (
                    <button
                      className="action-btn"
                      onClick={(e) => { e.stopPropagation(); onReroll?.(msg); setActiveActionId(null) }}
                    >
                      重新生成
                    </button>
                  )}
                  <button
                    className="action-btn action-btn--danger"
                    onClick={(e) => { e.stopPropagation(); onDelete?.(msg); setActiveActionId(null) }}
                  >
                    刪除
                  </button>
                </div>
              )}

              {/* 時間戳 */}
              {!msg.isStreaming && (
                <div className="msg-time">
                  {formatTime(msg.createdAt)}
                  {msg.role === 'assistant' && !msg.isError && msg.totalTokens ? (
                    <span style={{ marginLeft: 6, opacity: 0.7 }}>
                      · {msg.promptTokens?.toLocaleString()} → {msg.completionTokens?.toLocaleString()} tokens
                      {msg.reasoningTokens ? ` (推理 ${msg.reasoningTokens.toLocaleString()})` : ''}
                      {msg.usedCheckpoint ? ' 📦' : ''}
                    </span>
                  ) : null}
                </div>
              )}
            </div>

            {/* Checkbox 右側（你的訊息） */}
            {selectMode && msg.role === 'user' && (
              <input
                type="checkbox"
                checked={selectedIds?.has(msg.id) ?? false}
                onChange={() => onToggleSelect?.(msg.id)}
                style={{
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  accentColor: 'var(--accent)',
                  cursor: 'pointer',
                  marginBottom: 6,
                }}
              />
            )}
          </div>

          {/* 壓縮分隔線 */}
          {checkpointIndex !== undefined && index === checkpointIndex && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '8px 16px', opacity: 0.55,
            }}>
              <div style={{ flex: 1, height: 1, background: 'currentColor' }} />
              <span style={{ fontSize: 11, whiteSpace: 'nowrap' }}>以上已壓縮</span>
              <div style={{ flex: 1, height: 1, background: 'currentColor' }} />
            </div>
          )}
        </div>
      )})}
      <div ref={bottomRef} />

      <style>{MSG_STYLES}</style>
    </div>
  )
}

const MSG_STYLES = `
.image-card {
  width: min(280px, 72vw);
  border-radius: 18px;
  overflow: hidden;
  box-shadow: 0 14px 32px rgba(0, 0, 0, 0.16);
  border: 1px solid rgba(255,255,255,0.08);
  margin-bottom: 8px;
  background: rgba(255,255,255,0.04);
}
.image-card--assistant {
  align-self: flex-start;
}
.image-card--user {
  align-self: flex-end;
}
.image-card__img {
  display: block;
  width: 100%;
  height: auto;
}
.image-card__placeholder {
  padding: 18px 16px;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.5;
}
.quote-block {
  background: rgba(255,255,255,0.07);
  border-left: 2px solid var(--accent-dim);
  border-radius: 4px;
  padding: 5px 8px;
  margin-bottom: 8px;
}
.quote-name {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 2px;
}
.quote-content {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.4;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  white-space: normal;
}
.action-bar {
  display: flex;
  gap: 4px;
  padding: 4px 2px;
}
.action-bar--user { justify-content: flex-end; }
.action-bar--assistant { justify-content: flex-start; }
.action-btn {
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-secondary);
  transition: background 0.12s, color 0.12s;
  white-space: nowrap;
}
.action-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.action-btn--danger { color: var(--color-error); }
.action-btn--danger:hover {
  background: rgba(217,112,112,0.12);
  border-color: rgba(217,112,112,0.3);
}
`
