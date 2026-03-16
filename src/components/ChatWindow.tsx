import { useState, useCallback, useEffect } from 'react'
import MessageList from './MessageList'
import Composer from './Composer'
import { streamChat, MODEL_LS_KEY, CONTEXT_TURNS_LS_KEY, TEMPERATURE_LS_KEY, TOP_P_LS_KEY, USER_NAME_LS_KEY, THINKING_BUDGET_LS_KEY, EXTRACT_INTERVAL_LS_KEY, fetchBackendModel, createCheckpoint } from '../api'
import { createClientId } from '../id'
import { formatRouteLabel, loadModelRouting, MODEL_ROUTING_CHANGE_EVENT } from '../modelRouting'
import { resolveSessionId } from '../session'
import type { ChatContentPart, ChatMessage, ChatMessageContent, DisplayMessage, QuotedMessageRef, StoredChat } from '../types'

// ── localStorage helpers ──────────────────────────────────────

function getLegacyModel(): string {
  try {
    const saved = localStorage.getItem(MODEL_LS_KEY)
    if (saved && saved.trim()) return saved.trim()
  } catch { /* ignore */ }
  return ''
}

function getDisplayModelLabel(): string {
  const routing = loadModelRouting()
  if (routing.chat) return formatRouteLabel(routing.chat)
  const saved = getLegacyModel()
  return saved ? saved.split('/').pop() ?? saved : ''
}

function getContextTurns(): number {
  try {
    const saved = localStorage.getItem(CONTEXT_TURNS_LS_KEY)
    if (saved) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n > 0) return n
    }
  } catch { /* ignore */ }
  return 0
}

function getTemperature(): number | undefined {
  try {
    const saved = localStorage.getItem(TEMPERATURE_LS_KEY)
    if (saved !== null) {
      const n = parseFloat(saved)
      if (!isNaN(n)) return n
    }
  } catch { /* ignore */ }
  return undefined
}

function getTopP(): number | undefined {
  try {
    const saved = localStorage.getItem(TOP_P_LS_KEY)
    if (saved !== null) {
      const n = parseFloat(saved)
      if (!isNaN(n)) return n
    }
  } catch { /* ignore */ }
  return undefined
}

function getUserName(): string {
  try { return localStorage.getItem(USER_NAME_LS_KEY) || '你' } catch { return '你' }
}

function getThinkingBudget(): number | undefined {
  try {
    const saved = localStorage.getItem(THINKING_BUDGET_LS_KEY)
    if (saved !== null) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 1024) return n
    }
  } catch { /* ignore */ }
  return undefined
}

function getExtractInterval(): number | undefined {
  try {
    const saved = localStorage.getItem(EXTRACT_INTERVAL_LS_KEY)
    if (saved !== null) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 0) return n
    }
  } catch { /* ignore */ }
  return undefined
}

const RECALL_KEY = 'myloverM-recall-card-ids'
type RecallMode = 'manual' | 'once'
const IMAGE_CONTEXT_USER_TURNS = 2

function getRecallCardIds(): number[] {
  try {
    const raw = localStorage.getItem(RECALL_KEY)
    if (!raw) return []
    const map = JSON.parse(raw) as Record<string, RecallMode>
    return Object.keys(map).map(Number)
  } catch { return [] }
}

function consumeOnceRecallIds() {
  try {
    const raw = localStorage.getItem(RECALL_KEY)
    if (!raw) return
    const map = JSON.parse(raw) as Record<string, RecallMode>
    const next: Record<string, RecallMode> = {}
    for (const [id, mode] of Object.entries(map)) {
      if (mode !== 'once') next[id] = mode
    }
    localStorage.setItem(RECALL_KEY, JSON.stringify(next))
  } catch { /* ignore */ }
}

function handleBack() {
  if (window.history.length > 1) {
    window.history.back()
  } else {
    window.location.href = import.meta.env.BASE_URL || '/'
  }
}

function buildQuotedText(displayText: string, quotedRef: QuotedMessageRef | null): string {
  if (!quotedRef) return displayText
  const truncated = quotedRef.content.length > 100
    ? quotedRef.content.slice(0, 100) + '…'
    : quotedRef.content
  return `> ${quotedRef.senderName}：「${truncated}」\n\n${displayText}`
}

function hasChatContent(content: ChatMessageContent): boolean {
  if (Array.isArray(content)) return content.length > 0
  return Boolean(content.trim())
}

function countLaterUserTurns(messages: DisplayMessage[], index: number): number {
  let count = 0
  for (let i = index + 1; i < messages.length; i += 1) {
    if (messages[i].role === 'user') count += 1
  }
  return count
}

function buildChatContent(
  text: string,
  imageUrl?: string,
  keepImage: boolean = false,
): ChatMessageContent {
  const trimmedText = text.trim()

  if (keepImage && imageUrl) {
    const parts: ChatContentPart[] = []
    if (trimmedText) {
      parts.push({ type: 'text', text: trimmedText })
    }
    parts.push({ type: 'image_url', image_url: { url: imageUrl } })
    return parts
  }

  return trimmedText
}

// ── 確認 Dialog 型別 ──────────────────────────────────────────

type ConfirmDialogState =
  | { type: 'delete'; msg: DisplayMessage }
  | { type: 'reroll'; msg: DisplayMessage; hasSubsequent: boolean }
  | { type: 'batchDelete'; count: number }

// ── Props ────────────────────────────────────────────────────

interface Props {
  onOpenSidebar: () => void
  onOpenPanel: () => void
  activeChat: StoredChat | null
  onUpdateChat: (chatId: string, messages: DisplayMessage[], sessionId?: string, checkpointData?: { index: number; summary: string }) => void
}

export default function ChatWindow({ onOpenSidebar, onOpenPanel, activeChat, onUpdateChat }: Props) {
  const [input, setInput] = useState('')
  const [pendingImageUrl, setPendingImageUrl] = useState<string | null>(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [isCheckpointing, setIsCheckpointing] = useState(false)
  const [checkpointPreview, setCheckpointPreview] = useState<string | null>(null)

  // 引用 & 編輯
  const [quotedMessage, setQuotedMessage] = useState<DisplayMessage | null>(null)
  const [editingFromIndex, setEditingFromIndex] = useState<number | null>(null)

  // 批量選取
  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // 確認對話框
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState | null>(null)

  const [displayModel, setDisplayModel] = useState<string>(() => {
    return getDisplayModelLabel()
  })

  useEffect(() => {
    const syncDisplayModel = () => {
      const label = getDisplayModelLabel()
      if (label) {
        setDisplayModel(label)
        return
      }
      fetchBackendModel().then((m) => {
        if (m) setDisplayModel(m.split('/').pop() ?? m)
      }).catch(() => { /* ignore */ })
    }

    syncDisplayModel()
    window.addEventListener(MODEL_ROUTING_CHANGE_EVENT, syncDisplayModel)
    window.addEventListener('storage', syncDisplayModel)
    return () => {
      window.removeEventListener(MODEL_ROUTING_CHANGE_EVENT, syncDisplayModel)
      window.removeEventListener('storage', syncDisplayModel)
    }
  }, [])

  // 切換聊天時清除狀態
  useEffect(() => {
    setQuotedMessage(null)
    setEditingFromIndex(null)
    setPendingImageUrl(null)
    setSelectMode(false)
    setSelectedIds(new Set())
    setConfirmDialog(null)
  }, [activeChat?.id])

  // ── 核心發送邏輯 ──────────────────────────────────────────

  const performSend = useCallback(async (
    displayText: string,
    quotedRef: QuotedMessageRef | null,
    baseMessages: DisplayMessage[],
    imageUrl?: string | null,
  ) => {
    if (!activeChat || isStreaming) return

    setIsStreaming(true)

    const { sessionId, renewed } = resolveSessionId(activeChat)
    const trimmedText = displayText.trim()
    const normalizedImageUrl = imageUrl?.trim() || undefined

    const userMsg: DisplayMessage = {
      id: createClientId(),
      role: 'user',
      content: trimmedText,
      createdAt: Date.now(),
      ...(normalizedImageUrl ? { imageUrl: normalizedImageUrl, hasImage: true } : {}),
      ...(quotedRef ? { quotedMessageRef: quotedRef } : {}),
    }

    const assistantMsg: DisplayMessage = {
      id: createClientId(),
      role: 'assistant',
      content: '',
      createdAt: Date.now(),
      isStreaming: true,
    }

    const newMessages: DisplayMessage[] = [...baseMessages, userMsg, assistantMsg]
    onUpdateChat(activeChat.id, newMessages, renewed ? sessionId : undefined)

    const apiContent = buildQuotedText(trimmedText, quotedRef)

    // 組 payload（歷史訊息也要還原引用前綴）
    const prevCompleted = baseMessages.filter((m) => !m.isError && !m.isStreaming)
    const contextTurns = getContextTurns()
    const trimmedPrev = contextTurns > 0
      ? prevCompleted.slice(-(contextTurns * 2 - 1))
      : prevCompleted

    const payload: ChatMessage[] = [
      ...trimmedPrev.flatMap((m, index) => {
        const content = buildChatContent(
          buildQuotedText(m.content, m.quotedMessageRef ?? null),
          m.imageUrl,
          Boolean(m.imageUrl) && countLaterUserTurns(trimmedPrev, index) <= IMAGE_CONTEXT_USER_TURNS,
        )
        if (!hasChatContent(content)) return []
        return [{
          role: m.role as 'user' | 'assistant',
          content,
        }]
      }),
      {
        role: 'user' as const,
        content: buildChatContent(apiContent, normalizedImageUrl, Boolean(normalizedImageUrl)),
      },
    ]

    let accContent = ''
    let hadError = false
    let isSearching = false
    let usageData: { promptTokens: number; completionTokens: number; totalTokens: number; reasoningTokens?: number; usedCheckpoint: boolean; thinkingContent?: string } | null = null

    try {
      const recallCardIds = getRecallCardIds()
      const modelRouting = loadModelRouting()
      for await (const event of streamChat({
        model: getLegacyModel(),
        messages: payload,
        session_id: sessionId,
        temperature: getTemperature(),
        top_p: getTopP(),
        thinking_budget: getThinkingBudget(),
        recall_card_ids: recallCardIds.length > 0 ? recallCardIds : undefined,
        model_routing: modelRouting,
        extract_interval: getExtractInterval(),
      })) {
        if (event.type === 'searching') {
          isSearching = true
          const searchingMsg = `🔍 正在搜尋「${event.query}」...`
          const updated = newMessages.map((m) =>
            m.id === assistantMsg.id ? { ...m, content: searchingMsg } : m
          )
          onUpdateChat(activeChat.id, updated)
        } else if (event.type === 'delta') {
          if (isSearching) {
            // 第一個 delta 到了，清掉「搜尋中」狀態
            isSearching = false
          }
          accContent += event.text
          const updated = newMessages.map((m) =>
            m.id === assistantMsg.id ? { ...m, content: accContent } : m
          )
          onUpdateChat(activeChat.id, updated)
        } else if (event.type === 'usage') {
          usageData = {
            promptTokens: event.promptTokens,
            completionTokens: event.completionTokens,
            totalTokens: event.totalTokens,
            reasoningTokens: event.reasoningTokens,
            usedCheckpoint: event.usedCheckpoint,
            thinkingContent: event.thinkingContent,
          }
        } else if (event.type === 'error') {
          hadError = true
          const updated = newMessages.map((m) =>
            m.id === assistantMsg.id
              ? { ...m, content: event.message, isStreaming: false, isError: true }
              : m
          )
          onUpdateChat(activeChat.id, updated)
          break
        } else if (event.type === 'done') {
          break
        }
      }
    } finally {
      if (!hadError) {
        const final = newMessages.map((m) =>
          m.id === assistantMsg.id
            ? { ...m, content: accContent || '（無回應）', isStreaming: false, ...usageData }
            : m
        )
        onUpdateChat(activeChat.id, final)
        consumeOnceRecallIds()
      }
      setIsStreaming(false)
    }
  }, [activeChat, isStreaming, onUpdateChat])

  // ── 一般送訊息 ───────────────────────────────────────────

  const sendMessage = useCallback(async () => {
    if (!activeChat || (!input.trim() && !pendingImageUrl) || isStreaming) return

    const text = input.trim()
    setInput('')
    const imageUrl = pendingImageUrl
    setPendingImageUrl(null)

    const base = editingFromIndex !== null
      ? activeChat.messages.slice(0, editingFromIndex)
      : activeChat.messages

    const qRef = quotedMessage ? {
      role: quotedMessage.role,
      senderName: quotedMessage.role === 'assistant' ? 'M' : getUserName(),
      content: quotedMessage.content.slice(0, 200),
    } : null

    setEditingFromIndex(null)
    setQuotedMessage(null)

    await performSend(text, qRef, base, imageUrl)
  }, [activeChat, input, pendingImageUrl, isStreaming, editingFromIndex, quotedMessage, performSend])

  // ── Action handlers ──────────────────────────────────────

  const handleQuote = useCallback((msg: DisplayMessage) => {
    setQuotedMessage(msg)
  }, [])

  const handleEdit = useCallback((msg: DisplayMessage) => {
    if (!activeChat) return
    const idx = activeChat.messages.findIndex((m) => m.id === msg.id)
    if (idx < 0) return
    setInput(msg.content)
    setEditingFromIndex(idx)
    setQuotedMessage(null)
  }, [activeChat])

  const handleDelete = useCallback((msg: DisplayMessage) => {
    setConfirmDialog({ type: 'delete', msg })
  }, [])

  const handleReroll = useCallback((msg: DisplayMessage) => {
    if (!activeChat) return
    const idx = activeChat.messages.findIndex((m) => m.id === msg.id)
    const hasSubsequent = idx >= 0 && idx < activeChat.messages.length - 1
    setConfirmDialog({ type: 'reroll', msg, hasSubsequent })
  }, [activeChat])

  const handleDeleteConfirm = useCallback(() => {
    if (!activeChat || !confirmDialog || confirmDialog.type !== 'delete') return
    const { msg } = confirmDialog
    let updated: DisplayMessage[]
    if (msg.role === 'user') {
      // 刪除這條及之後所有訊息
      const idx = activeChat.messages.findIndex((m) => m.id === msg.id)
      updated = idx >= 0 ? activeChat.messages.slice(0, idx) : activeChat.messages.filter((m) => m.id !== msg.id)
    } else {
      updated = activeChat.messages.filter((m) => m.id !== msg.id)
    }
    onUpdateChat(activeChat.id, updated)
    setConfirmDialog(null)
  }, [activeChat, confirmDialog, onUpdateChat])

  const handleRerollConfirm = useCallback(async () => {
    if (!activeChat || !confirmDialog || confirmDialog.type !== 'reroll') return
    const { msg } = confirmDialog
    setConfirmDialog(null)

    const idx = activeChat.messages.findIndex((m) => m.id === msg.id)
    if (idx < 0) return

    // M 的訊息前面應該是 user 的訊息
    const userMsgIdx = idx - 1
    if (userMsgIdx < 0) return
    const userMsg = activeChat.messages[userMsgIdx]
    if (userMsg.role !== 'user') return

    const base = activeChat.messages.slice(0, userMsgIdx)
    const qRef = userMsg.quotedMessageRef ?? null

    await performSend(userMsg.content, qRef, base, userMsg.imageUrl)
  }, [activeChat, confirmDialog, performSend])

  const handleBatchDelete = useCallback(() => {
    if (selectedIds.size === 0) return
    setConfirmDialog({ type: 'batchDelete', count: selectedIds.size })
  }, [selectedIds])

  const handleBatchDeleteConfirm = useCallback(() => {
    if (!activeChat || !confirmDialog || confirmDialog.type !== 'batchDelete') return
    const updated = activeChat.messages.filter((m) => !selectedIds.has(m.id))
    onUpdateChat(activeChat.id, updated)
    setSelectedIds(new Set())
    setSelectMode(false)
    setConfirmDialog(null)
  }, [activeChat, confirmDialog, selectedIds, onUpdateChat])

  const handleToggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  // ── 壓縮 ────────────────────────────────────────────────

  const handleCompress = useCallback(async () => {
    if (!activeChat || isStreaming || isCheckpointing) return
    const sessionId = activeChat.sessionId
    if (!sessionId) return
    setIsCheckpointing(true)
    try {
      const result = await createCheckpoint(sessionId, loadModelRouting())
      const completedMessages = activeChat.messages.filter((m) => !m.isStreaming && !m.isError)
      const checkpointIndex = completedMessages.length - 1
      onUpdateChat(activeChat.id, activeChat.messages, undefined, {
        index: checkpointIndex,
        summary: result.summary,
      })
      setCheckpointPreview(result.summary)
    } catch (err) {
      alert(`壓縮失敗：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setIsCheckpointing(false)
    }
  }, [activeChat, isStreaming, isCheckpointing, onUpdateChat])

  // ── Render ───────────────────────────────────────────────

  const allSelected = !!activeChat && selectedIds.size === activeChat.messages.length && activeChat.messages.length > 0

  return (
    <main className="chat-window">
      {/* Checkpoint 摘要預覽 overlay */}
      {checkpointPreview && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 50,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: '24px',
          }}
          onClick={() => setCheckpointPreview(null)}
        >
          <div
            style={{
              background: '#2c2c2e', color: '#f0ebe3', borderRadius: 12,
              padding: '20px 24px', maxWidth: 480, width: '100%',
              maxHeight: '70vh', overflowY: 'auto',
              boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <p style={{ fontWeight: 600, marginBottom: 12, color: '#d4a46a' }}>📦 壓縮完成</p>
            <pre style={{
              fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap',
              wordBreak: 'break-word', margin: 0, color: '#ddd8d0',
              fontFamily: 'inherit',
            }}>{checkpointPreview}</pre>
            <button
              style={{
                marginTop: 16, width: '100%', padding: '8px 0',
                background: '#d4a46a', color: '#1c1c1e',
                border: 'none', borderRadius: 8, fontWeight: 600,
                cursor: 'pointer', fontSize: 14,
              }}
              onClick={() => setCheckpointPreview(null)}
            >確認</button>
          </div>
        </div>
      )}

      {/* 確認對話框 */}
      {confirmDialog && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 60,
            background: 'rgba(0,0,0,0.55)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 24,
          }}
          onClick={() => setConfirmDialog(null)}
        >
          <div
            style={{
              background: '#2b2620', borderRadius: 12,
              padding: '20px 24px', maxWidth: 340, width: '100%',
              boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {confirmDialog.type === 'delete' && (
              <>
                <p style={{ fontWeight: 600, marginBottom: 8, fontSize: 15 }}>刪除這則訊息？</p>
                <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 20 }}>
                  {confirmDialog.msg.role === 'user'
                    ? '這條之後的對話也會一起刪除。'
                    : '這個動作無法復原。'}
                </p>
                <div style={{ display: 'flex', gap: 10 }}>
                  <button style={cancelBtnStyle} onClick={() => setConfirmDialog(null)}>取消</button>
                  <button style={dangerBtnStyle} onClick={handleDeleteConfirm}>刪除</button>
                </div>
              </>
            )}
            {confirmDialog.type === 'reroll' && (
              <>
                <p style={{ fontWeight: 600, marginBottom: 8, fontSize: 15 }}>重新生成 M 的回覆？</p>
                {confirmDialog.hasSubsequent && (
                  <p style={{ fontSize: 13, color: 'var(--color-warning)', marginBottom: 6 }}>
                    這之後的訊息也會一起刪除。
                  </p>
                )}
                <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 20 }}>
                  M 會用同樣的問題重新回答。
                </p>
                <div style={{ display: 'flex', gap: 10 }}>
                  <button style={cancelBtnStyle} onClick={() => setConfirmDialog(null)}>取消</button>
                  <button style={accentBtnStyle} onClick={handleRerollConfirm}>重新生成</button>
                </div>
              </>
            )}
            {confirmDialog.type === 'batchDelete' && (
              <>
                <p style={{ fontWeight: 600, marginBottom: 8, fontSize: 15 }}>
                  刪除 {confirmDialog.count} 則訊息？
                </p>
                <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 20 }}>
                  這個動作無法復原。
                </p>
                <div style={{ display: 'flex', gap: 10 }}>
                  <button style={cancelBtnStyle} onClick={() => setConfirmDialog(null)}>取消</button>
                  <button style={dangerBtnStyle} onClick={handleBatchDeleteConfirm}>刪除</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Header */}
      <header className="chat-header">
        <button className="chat-header-btn" onClick={handleBack} aria-label="返回">‹</button>
        <button className="chat-header-btn mobile-only" onClick={onOpenSidebar} aria-label="聊天列表">☰</button>
        <div className="chat-header-title">
          <span>{activeChat?.title ?? 'M'}</span>
          {displayModel && <span className="chat-header-model">{displayModel}</span>}
        </div>
        <button
          className="chat-header-btn"
          onClick={() => { setSelectMode((v) => !v); setSelectedIds(new Set()) }}
          aria-label={selectMode ? '完成選取' : '選取訊息'}
          style={{ fontSize: 13, color: selectMode ? 'var(--accent)' : undefined }}
          disabled={!activeChat}
        >
          {selectMode ? '完成' : '選取'}
        </button>
        <button
          className="chat-header-btn"
          onClick={handleCompress}
          disabled={isCheckpointing || isStreaming || !activeChat || selectMode}
          aria-label="壓縮對話"
          title="壓縮對話（Checkpoint）"
          style={{ fontSize: 13, opacity: isCheckpointing ? 0.5 : 1 }}
        >
          {isCheckpointing ? '壓縮中…' : '壓縮'}
        </button>
        <button className="chat-header-btn mobile-only" onClick={onOpenPanel} aria-label="資訊">⋯</button>
      </header>

      {/* 訊息列表 */}
      <div className="chat-messages">
        <MessageList
          messages={activeChat?.messages ?? []}
          checkpointIndex={activeChat?.checkpointIndex}
          isStreaming={isStreaming}
          selectMode={selectMode}
          selectedIds={selectedIds}
          onToggleSelect={handleToggleSelect}
          onQuote={handleQuote}
          onEdit={handleEdit}
          onDelete={handleDelete}
          onReroll={handleReroll}
        />
      </div>

      {/* 底部：批量選取操作 or Composer */}
      <div className="chat-composer">
        {selectMode ? (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '2px 0',
          }}>
            <span style={{ flex: 1, fontSize: 13, color: 'var(--text-secondary)' }}>
              已選 {selectedIds.size} 則
            </span>
            <button
              style={cancelBtnStyle}
              onClick={() => {
                if (allSelected) {
                  setSelectedIds(new Set())
                } else {
                  setSelectedIds(new Set(activeChat?.messages.map((m) => m.id) ?? []))
                }
              }}
            >
              {allSelected ? '取消全選' : '全選'}
            </button>
            <button
              style={{
                ...dangerBtnStyle,
                opacity: selectedIds.size === 0 ? 0.4 : 1,
                cursor: selectedIds.size === 0 ? 'not-allowed' : 'pointer',
              }}
              onClick={handleBatchDelete}
              disabled={selectedIds.size === 0}
            >
              刪除{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
            </button>
            <button
              style={cancelBtnStyle}
              onClick={() => { setSelectMode(false); setSelectedIds(new Set()) }}
            >
              取消
            </button>
          </div>
        ) : (
          <Composer
            value={input}
            onChange={setInput}
            onSend={sendMessage}
            imageUrl={pendingImageUrl}
            onImageChange={setPendingImageUrl}
            disabled={isStreaming || !activeChat}
            quotedMessage={quotedMessage}
            onClearQuote={() => setQuotedMessage(null)}
            userName={getUserName()}
          />
        )}
      </div>
    </main>
  )
}

// ── 共用按鈕樣式 ─────────────────────────────────────────────

const cancelBtnStyle: React.CSSProperties = {
  padding: '6px 14px',
  borderRadius: 8,
  border: '1px solid var(--border)',
  background: 'var(--bg-elevated)',
  fontSize: 13,
  color: 'var(--text-primary)',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}

const dangerBtnStyle: React.CSSProperties = {
  padding: '6px 14px',
  borderRadius: 8,
  background: 'rgba(217,112,112,0.15)',
  border: '1px solid rgba(217,112,112,0.3)',
  color: 'var(--color-error)',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}

const accentBtnStyle: React.CSSProperties = {
  padding: '6px 14px',
  borderRadius: 8,
  background: 'var(--accent-bg)',
  border: '1px solid var(--accent-dim)',
  color: 'var(--accent)',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}
