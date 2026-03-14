import type { StoredChat } from '../types'

interface Props {
  chats: StoredChat[]
  activeChatId: string
  onClose?: () => void
  onSelectChat: (chatId: string) => void
  onNewChat: () => void
  onDeleteChat?: (chatId: string) => void
  onBack?: () => void
}

function formatRelativeTime(ts: number): string {
  const diff = Date.now() - ts
  const min  = Math.floor(diff / 60_000)
  const hr   = Math.floor(diff / 3_600_000)
  const day  = Math.floor(diff / 86_400_000)
  if (min < 1)   return '剛剛'
  if (min < 60)  return `${min} 分鐘前`
  if (hr  < 24)  return `${hr} 小時前`
  if (day < 7)   return `${day} 天前`
  return new Date(ts).toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' })
}

export default function ChatSidebar({ chats, activeChatId, onClose, onSelectChat, onNewChat, onDeleteChat, onBack }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        {onBack && (
          <button className="sidebar-back" onClick={onBack} aria-label="返回">‹</button>
        )}
        <span className="sidebar-title">對話</span>
        <button className="sidebar-close" onClick={onClose} aria-label="關閉">✕</button>
      </div>

      <div className="sidebar-new">
        <button className="btn-new-chat" onClick={onNewChat}>＋ 新對話</button>
      </div>

      <div className="sidebar-list">
        {chats.length === 0 ? (
          <div className="sidebar-empty">還沒有對話</div>
        ) : (
          chats.map((chat) => (
            <div
              key={chat.id}
              className={`chat-item${chat.id === activeChatId ? ' chat-item--active' : ''}`}
              onClick={() => onSelectChat(chat.id)}
            >
              <div className="chat-item-body">
                <span className="chat-item-title">{chat.title}</span>
                <span className="chat-item-time">{formatRelativeTime(chat.updatedAt)}</span>
              </div>
              {onDeleteChat && (
                <button
                  className="chat-item-del"
                  onClick={(e) => { e.stopPropagation(); onDeleteChat(chat.id) }}
                  aria-label="刪除"
                >✕</button>
              )}
            </div>
          ))
        )}
      </div>

      <style>{SIDEBAR_STYLES}</style>
    </aside>
  )
}

const SIDEBAR_STYLES = `
.sidebar-back {
  font-size: 22px;
  color: var(--accent);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  transition: opacity 0.15s;
  line-height: 1;
}
.sidebar-back:hover { opacity: 0.7; }

.chat-item {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 4px;
  width: 100%;
  padding: 9px 10px;
  border-radius: var(--radius-md);
  text-align: left;
  transition: background 0.12s;
  cursor: pointer;
}
.chat-item:hover {
  background: var(--bg-hover);
}
.chat-item--active {
  background: var(--accent-bg);
}
.chat-item-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1;
  min-width: 0;
}
.chat-item-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-item--active .chat-item-title {
  color: var(--accent);
}
.chat-item-time {
  font-size: 11px;
  color: var(--text-muted);
}
.chat-item-del {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0;
  transition: opacity 0.15s, background 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
}
.chat-item:hover .chat-item-del {
  opacity: 1;
}
.chat-item-del:hover {
  background: rgba(255,80,80,0.18);
  color: #ff6b6b;
}
`
