import { useState, useEffect, useCallback, useRef } from 'react'
import { API_BASE_LS_KEY, API_SECRET_LS_KEY, CONTEXT_TURNS_LS_KEY, TEMPERATURE_LS_KEY, TOP_P_LS_KEY, USER_NAME_LS_KEY, THINKING_BUDGET_LS_KEY, EXTRACT_INTERVAL_LS_KEY, checkHealth, fetchBackendModel, listCheckpoints, saveSnapshot, type CheckpointRecord } from '../api'
import { formatRouteLabel, loadModelRouting, MODEL_ROUTING_CHANGE_EVENT } from '../modelRouting'
import MemoryBankTab from './MemoryBankTab'
import type { HealthStatus, StoredChat, DisplayMessage } from '../types'

type Tab = 'chat' | 'memory' | 'loops' | 'system' | 'checkpoint'

interface Props {
  activeChat: StoredChat | null
  onClose?: () => void
  onOpenModelRouting?: () => void
  onImportChats?: (chats: StoredChat[]) => void
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'chat',       label: 'Chat'       },
  { key: 'memory',     label: 'Memory'     },
  { key: 'loops',      label: 'Loops'      },
  { key: 'checkpoint', label: 'Checkpoint' },
  { key: 'system',     label: 'System'     },
]

function parseChatTxt(text: string): { role: 'user' | 'assistant'; content: string; createdAt: number }[] {
  const lines = text.split('\n')
  const result: { role: 'user' | 'assistant'; content: string; createdAt: number }[] = []
  const regex = /^【(user|assistant)】 \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)/
  let current: { role: 'user' | 'assistant'; content: string; createdAt: number } | null = null
  for (const line of lines) {
    const match = regex.exec(line)
    if (match) {
      if (current) result.push(current)
      const role = match[1] as 'user' | 'assistant'
      const createdAt = new Date(match[2].replace(' ', 'T')).getTime()
      current = { role, content: match[3], createdAt }
    } else if (current) {
      current.content += '\n' + line
    }
  }
  if (current) result.push(current)
  return result.map((m) => ({ ...m, content: m.content.trimEnd() }))
}

function normalizeApiBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, '')
  if (!trimmed) return ''
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  return `https://${trimmed}`
}

function loadApiUrl(): string {
  try {
    return normalizeApiBaseUrl(localStorage.getItem(API_BASE_LS_KEY) ?? '')
  } catch {
    return ''
  }
}

function loadContextTurns(): number {
  try {
    const saved = localStorage.getItem(CONTEXT_TURNS_LS_KEY)
    if (saved) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 0) return n
    }
  } catch { /* ignore */ }
  return 0
}

function loadTemperature(): number {
  try {
    const saved = localStorage.getItem(TEMPERATURE_LS_KEY)
    if (saved !== null) {
      const n = parseFloat(saved)
      if (!isNaN(n)) return n
    }
  } catch { /* ignore */ }
  return 1.0
}

function loadTopP(): number | null {
  try {
    const saved = localStorage.getItem(TOP_P_LS_KEY)
    if (saved !== null) {
      const n = parseFloat(saved)
      if (!isNaN(n)) return n
    }
  } catch { /* ignore */ }
  return null // null = 不調整
}

function loadThinkingBudget(): number | null {
  try {
    const saved = localStorage.getItem(THINKING_BUDGET_LS_KEY)
    if (saved !== null) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 0) return n
    }
  } catch { /* ignore */ }
  return null // null = 不啟用
}

function loadExtractInterval(): number {
  try {
    const saved = localStorage.getItem(EXTRACT_INTERVAL_LS_KEY)
    if (saved !== null) {
      const n = parseInt(saved, 10)
      if (!isNaN(n) && n >= 0) return n
    }
  } catch { /* ignore */ }
  return 1 // 預設每輪提取
}

type HealthState =
  | { status: 'idle' }
  | { status: 'checking' }
  | { status: 'ok'; data: HealthStatus }
  | { status: 'error'; message: string }

const TEST_PAGE_URL =
  import.meta.env.BASE_URL === '/Our-love/docs/m/'
    ? '/Our-love/docs/labs/ai-memory-chat.html'
    : ''

export default function SidePanel({ activeChat, onClose, onOpenModelRouting, onImportChats }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>('system')
  const [apiUrl, setApiUrl]       = useState(loadApiUrl)
  const [apiSecret, setApiSecret] = useState(() => {
    try { return localStorage.getItem(API_SECRET_LS_KEY) ?? '' } catch { return '' }
  })
  const [contextTurns, setContextTurns] = useState(loadContextTurns)
  const [temperature, setTemperature]   = useState(loadTemperature)
  const [topP, setTopP]                 = useState<number | null>(loadTopP)
  const [thinkingBudget, setThinkingBudget] = useState<number | null>(loadThinkingBudget)
  const [extractInterval, setExtractInterval] = useState(loadExtractInterval)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [userName, setUserName] = useState(() => {
    try { return localStorage.getItem(USER_NAME_LS_KEY) ?? '' } catch { return '' }
  })
  const [health, setHealth]       = useState<HealthState>({ status: 'idle' })
  const [backendModel, setBackendModel] = useState('')
  const [routingPreview, setRoutingPreview] = useState(() => loadModelRouting())

  // Checkpoint tab state
  const [checkpoints, setCheckpoints] = useState<CheckpointRecord[]>([])
  const [cpLoading, setCpLoading] = useState(false)
  const [cpError, setCpError] = useState('')
  const [savingId, setSavingId] = useState<number | null>(null)
  const [previewId, setPreviewId] = useState<number | null>(null)

  // Backup state
  const [backupMsg, setBackupMsg] = useState('')
  const logFileRef  = useRef<HTMLInputElement>(null)
  const chatFileRef = useRef<HTMLInputElement>(null)

  function handleContextTurnsChange(val: number) {
    setContextTurns(val)
    try { localStorage.setItem(CONTEXT_TURNS_LS_KEY, String(val)) } catch { /* ignore */ }
  }

  function handleTemperatureChange(val: number) {
    setTemperature(val)
    try { localStorage.setItem(TEMPERATURE_LS_KEY, String(val)) } catch { /* ignore */ }
  }

  function handleExtractIntervalChange(val: number) {
    setExtractInterval(val)
    try { localStorage.setItem(EXTRACT_INTERVAL_LS_KEY, String(val)) } catch { /* ignore */ }
  }

  // ── Backup helpers ──────────────────────────────────────
  function handleExportChats() {
    try {
      const raw = localStorage.getItem('myloverM_chats')
      const chats = raw ? JSON.parse(raw) : []
      const blob = new Blob(
        [JSON.stringify({ exported_at: new Date().toISOString(), chats }, null, 2)],
        { type: 'application/json' },
      )
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `chats_${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      URL.revokeObjectURL(url)
      setBackupMsg('對話已下載！')
    } catch (e) {
      setBackupMsg(e instanceof Error ? e.message : '匯出失敗')
    }
  }

  async function handleImportChatsJson(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const text = await file.text()
      const data = JSON.parse(text) as { chats?: StoredChat[] }
      if (!Array.isArray(data.chats)) { setBackupMsg('JSON 格式錯誤，找不到 chats 陣列'); return }
      onImportChats?.(data.chats)
      setBackupMsg(`匯入完成！${data.chats.length} 段對話`)
    } catch (e) {
      setBackupMsg(e instanceof Error ? e.message : '匯入失敗')
    } finally {
      if (chatFileRef.current) chatFileRef.current.value = ''
    }
  }

  async function handleImportTxt(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const text = await file.text()
      const parsed = parseChatTxt(text)
      if (parsed.length === 0) { setBackupMsg('找不到對話訊息（格式：【user】/【assistant】）'); return }
      const title = file.name.replace(/\.txt$/i, '')
      const now = Date.now()
      const messages: DisplayMessage[] = parsed.map((m) => ({
        id: crypto.randomUUID(),
        role: m.role,
        content: m.content,
        createdAt: m.createdAt,
      }))
      const newChat: StoredChat = {
        id: crypto.randomUUID(),
        title,
        sessionId: crypto.randomUUID(),
        messages,
        createdAt: parsed[0]?.createdAt ?? now,
        updatedAt: now,
        lastActiveAt: now,
      }
      onImportChats?.([newChat])
      setBackupMsg(`匯入完成！${parsed.length} 條訊息已加到對話列表`)
    } catch (e) {
      setBackupMsg(e instanceof Error ? e.message : '匯入失敗')
    } finally {
      if (logFileRef.current) logFileRef.current.value = ''
    }
  }

  const loadCheckpoints = useCallback(async () => {
    if (!activeChat?.sessionId) return
    setCpLoading(true)
    setCpError('')
    try {
      const rows = await listCheckpoints(activeChat.sessionId)
      setCheckpoints(rows)
    } catch (e) {
      setCpError(e instanceof Error ? e.message : '載入失敗')
    } finally {
      setCpLoading(false)
    }
  }, [activeChat?.sessionId])

  useEffect(() => {
    if (activeTab === 'checkpoint') {
      loadCheckpoints()
    }
  }, [activeTab, loadCheckpoints])

  useEffect(() => {
    const syncRouting = () => setRoutingPreview(loadModelRouting())
    syncRouting()
    window.addEventListener(MODEL_ROUTING_CHANGE_EVENT, syncRouting)
    window.addEventListener('storage', syncRouting)
    return () => {
      window.removeEventListener(MODEL_ROUTING_CHANGE_EVENT, syncRouting)
      window.removeEventListener('storage', syncRouting)
    }
  }, [])

  async function handleSaveSnapshot(cp: CheckpointRecord) {
    setSavingId(cp.id)
    try {
      await saveSnapshot(cp.id)
      setCheckpoints((prev) => prev.map((c) => c.id === cp.id ? { ...c, saved_as_card: true } : c))
    } catch (e) {
      alert(`保存失敗：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSavingId(null)
    }
  }

  function handleTopPChange(val: number | null) {
    setTopP(val)
    try {
      if (val === null) {
        localStorage.removeItem(TOP_P_LS_KEY)
      } else {
        localStorage.setItem(TOP_P_LS_KEY, String(val))
      }
    } catch { /* ignore */ }
  }

  function handleThinkingBudgetChange(val: number | null) {
    setThinkingBudget(val)
    try {
      if (val === null || val < 1024) {
        localStorage.removeItem(THINKING_BUDGET_LS_KEY)
      } else {
        localStorage.setItem(THINKING_BUDGET_LS_KEY, String(val))
      }
    } catch { /* ignore */ }
  }

  function handleUrlChange(val: string) {
    setApiUrl(val)
    setHealth({ status: 'idle' })
    try { localStorage.setItem(API_BASE_LS_KEY, val.trim()) } catch { /* ignore */ }
  }

  function persistNormalizedApiUrl(raw: string) {
    const normalized = normalizeApiBaseUrl(raw)
    setApiUrl(normalized)
    try { localStorage.setItem(API_BASE_LS_KEY, normalized) } catch { /* ignore */ }
    return normalized
  }

  async function handleHealthCheck() {
    const normalized = persistNormalizedApiUrl(apiUrl)
    if (!normalized) {
      setHealth({ status: 'error', message: '請先填後端 URL' })
      return
    }

    setHealth({ status: 'checking' })
    try {
      const data = await checkHealth()
      setHealth({ status: 'ok', data })
      const bm = await fetchBackendModel()
      if (bm) setBackendModel(bm)
    } catch (e) {
      const rawMessage = e instanceof Error ? e.message : '連線失敗'
      const message = /load failed|failed to fetch|networkerror/i.test(rawMessage)
        ? '連線失敗。常見是 CORS：請到 Railway 把目前前端網域加入允許來源。'
        : rawMessage
      setHealth({ status: 'error', message })
    }
  }

  // Chat tab：session 資訊
  const sessionAge = activeChat
    ? Math.floor((Date.now() - activeChat.lastActiveAt) / 60_000)
    : null

  return (
    <aside className="side-panel">
      <div className="panel-header">
        <div className="panel-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`panel-tab${activeTab === t.key ? ' panel-tab--active' : ''}`}
              onClick={() => setActiveTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button className="panel-close" onClick={onClose} aria-label="關閉">✕</button>
      </div>

      <div className="panel-body">

        {/* ── Chat tab ── */}
        {activeTab === 'chat' && (
          <div style={{ display: 'grid', gap: 12 }}>
            {!activeChat ? (
              <p className="sp-hint">沒有選中的對話。</p>
            ) : (
              <>
                <InfoRow label="對話標題" value={activeChat.title} />
                <InfoRow label="Session ID" value={activeChat.sessionId.slice(0, 16) + '…'} mono />
                <InfoRow
                  label="Session 閒置"
                  value={sessionAge === null ? '—' : sessionAge < 1 ? '剛剛' : `${sessionAge} 分鐘前`}
                />
                <InfoRow
                  label="訊息數"
                  value={`${activeChat.messages.filter((m) => !m.isStreaming).length} 則`}
                />
              </>
            )}

            <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: '4px 0' }} />

            {/* 對話備份 */}
            <div>
              <label className="sp-label">💬 對話備份</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="sp-btn" style={{ flex: 1 }} onClick={handleExportChats}>⬇ 下載 JSON</button>
                <button className="sp-btn" style={{ flex: 1 }} onClick={() => chatFileRef.current?.click()}>⬆ 匯入 JSON</button>
              </div>
              <input ref={chatFileRef} type="file" accept=".json" style={{ display: 'none' }} onChange={handleImportChatsJson} />
              <p className="sp-hint">對話只存在這台裝置，換裝置前記得下載備份。</p>
            </div>

            {/* TXT 匯入 */}
            <div>
              <label className="sp-label">📁 匯入舊對話 TXT</label>
              <button className="sp-btn" style={{ width: '100%' }} onClick={() => logFileRef.current?.click()}>⬆ 選擇 TXT 檔案</button>
              <input ref={logFileRef} type="file" accept=".txt" style={{ display: 'none' }} onChange={handleImportTxt} />
              <p className="sp-hint">將整理好的 ChatGPT 對話 TXT 匯入，可接著繼續聊。</p>
            </div>

            {backupMsg && <div style={{ fontSize: 12, color: 'var(--accent)', padding: '6px 0' }}>{backupMsg}</div>}
          </div>
        )}

        {/* ── Memory tab ── */}
        {activeTab === 'memory' && (
          <MemoryBankTab />
        )}

        {/* ── Loops tab ── */}
        {activeTab === 'loops' && (
          <div className="panel-placeholder"><span>Open Loops（後端 API 待接）</span></div>
        )}

        {/* ── Checkpoint tab ── */}
        {activeTab === 'checkpoint' && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="sp-label" style={{ margin: 0 }}>本次對話的壓縮紀錄</span>
              <button className="sp-btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={loadCheckpoints} disabled={cpLoading}>
                {cpLoading ? '載入中…' : '重整'}
              </button>
            </div>

            {cpError && <div className="sp-health sp-health--err"><span>✗ {cpError}</span></div>}

            {!cpLoading && checkpoints.length === 0 && !cpError && (
              <p className="sp-hint">目前沒有壓縮紀錄。</p>
            )}

            {checkpoints.map((cp) => (
              <div key={cp.id} style={{
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                borderRadius: 8, padding: '10px 12px', display: 'grid', gap: 6,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: '#d4a46a' }}>
                    📦 v{cp.version}{cp.is_active ? ' ★' : ''}
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    {new Date(cp.created_at).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                  </span>
                </div>

                {previewId === cp.id ? (
                  <>
                    <pre style={{ fontSize: 11, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0, color: 'var(--text-primary)', fontFamily: 'inherit', maxHeight: 200, overflowY: 'auto' }}>
                      {cp.summary_text}
                    </pre>
                    <button className="sp-btn" style={{ fontSize: 11, padding: '3px 8px' }} onClick={() => setPreviewId(null)}>收起</button>
                  </>
                ) : (
                  <p style={{ fontSize: 11, color: 'var(--text-muted)', margin: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {cp.summary_text.slice(0, 60)}…
                  </p>
                )}

                <div style={{ display: 'flex', gap: 6 }}>
                  <button className="sp-btn" style={{ fontSize: 11, padding: '3px 8px', flex: 1 }} onClick={() => setPreviewId(previewId === cp.id ? null : cp.id)}>
                    {previewId === cp.id ? '收起' : '預覽'}
                  </button>
                  {cp.saved_as_card ? (
                    <button className="sp-btn" style={{ fontSize: 11, padding: '3px 8px', flex: 1, opacity: 0.5, cursor: 'default' }} disabled>
                      已存為 Snapshot ✓
                    </button>
                  ) : (
                    <button
                      className="sp-btn"
                      style={{ fontSize: 11, padding: '3px 8px', flex: 1 }}
                      onClick={() => handleSaveSnapshot(cp)}
                      disabled={savingId === cp.id}
                    >
                      {savingId === cp.id ? '保存中…' : '存為 Snapshot'}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* ── System tab ── */}
        {activeTab === 'system' && (
          <div style={{ display: 'grid', gap: 20 }}>

            {/* 你的名字 */}
            <div>
              <label className="sp-label">你的名字</label>
              <input
                className="sp-input"
                type="text"
                placeholder="你"
                value={userName}
                onChange={(e) => {
                  const v = e.target.value
                  setUserName(v)
                  try { localStorage.setItem(USER_NAME_LS_KEY, v) } catch { /* ignore */ }
                }}
              />
              <p className="sp-hint">引用訊息時會顯示的名稱。</p>
            </div>

            {/* URL 設定 */}
            <div>
              <label className="sp-label">後端 URL</label>
              <input
                className="sp-input"
                type="url"
                placeholder="https://xxxx.up.railway.app"
                value={apiUrl}
                onChange={(e) => handleUrlChange(e.target.value)}
                onBlur={(e) => persistNormalizedApiUrl(e.target.value)}
              />
              <p className="sp-hint">填 Railway 根網址，不含 /v1。少貼 https:// 會自動補上。</p>
            </div>

            {/* 密鑰 */}
            <div>
              <label className="sp-label">密鑰</label>
              <input
                className="sp-input"
                type="password"
                placeholder="Railway 設定的 API_SECRET_KEY"
                value={apiSecret}
                onChange={(e) => {
                  setApiSecret(e.target.value)
                  try { localStorage.setItem(API_SECRET_LS_KEY, e.target.value) } catch { /* ignore */ }
                }}
              />
              <p className="sp-hint">後端設了 API_SECRET_KEY 才需要填。留空表示不驗證。</p>
            </div>

            {/* Model routing */}
            <div style={{ display: 'grid', gap: 10 }}>
              <label className="sp-label">模型路由</label>
              <div style={{
                display: 'grid',
                gap: 8,
                padding: '12px',
                borderRadius: 12,
                border: '1px solid var(--border)',
                background: 'var(--bg-elevated)',
              }}>
                <InfoRow
                  label="聊天"
                  value={routingPreview.chat ? formatRouteLabel(routingPreview.chat) : (backendModel || 'Railway 預設')}
                />
                <InfoRow label="摘要 / 壓縮" value={formatRouteLabel(routingPreview.summary)} />
                <InfoRow label="提取" value={formatRouteLabel(routingPreview.extraction)} />
              </div>
              <button className="sp-btn" onClick={() => onOpenModelRouting?.()}>
                打開 API 切換頁
              </button>
              <p className="sp-hint">
                三家 provider 和拉取模型都在新頁面做，這裡只留入口。API key 仍在 Railway。
              </p>
            </div>

            {/* 上下文截斷 */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                <label className="sp-label" style={{ margin: 0 }}>上下文輪數</label>
                <input
                  type="number"
                  min={0}
                  max={999}
                  value={contextTurns}
                  onChange={(e) => {
                    const v = Math.max(0, Math.min(999, Number(e.target.value) || 0))
                    handleContextTurnsChange(v)
                  }}
                  style={{
                    width: 64,
                    padding: '3px 8px',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border)',
                    background: 'var(--bg-elevated)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                    textAlign: 'center',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                />
              </div>
              <input
                type="range"
                min={0}
                max={999}
                step={1}
                value={contextTurns}
                onChange={(e) => handleContextTurnsChange(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--accent)' }}
              />
              <p className="sp-hint">
                0 = 不截斷（送全部歷史）。設 N 則每次只送最近 N 輪對話給 M。
              </p>
            </div>

            {/* Temperature */}
            <div>
              <label className="sp-label">
                Temperature&nbsp;
                <span style={{ color: 'var(--text-primary)', fontVariantNumeric: 'tabular-nums' }}>
                  {temperature.toFixed(1)}
                </span>
              </label>
              <input
                type="range"
                min={0}
                max={2}
                step={0.1}
                value={temperature}
                onChange={(e) => handleTemperatureChange(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--accent)' }}
              />
              <p className="sp-hint">
                0.0 = 穩定保守，1.0 = 預設，2.0 = 創意跳脫。
              </p>
            </div>

            {/* 記憶提取間隔 */}
            <div>
              <label className="sp-label">
                記憶提取間隔&nbsp;
                <span style={{ color: 'var(--text-primary)', fontVariantNumeric: 'tabular-nums' }}>
                  {extractInterval === 0 ? '停用' : extractInterval === 1 ? '每輪' : `每 ${extractInterval} 輪`}
                </span>
              </label>
              <input
                type="range"
                min={0}
                max={10}
                step={1}
                value={extractInterval}
                onChange={(e) => handleExtractIntervalChange(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--accent)' }}
              />
              <p className="sp-hint">
                0 = 停用，1 = 每輪都提取（預設），N = 每 N 輪提取一次。數字越大消耗越少 API。
              </p>
            </div>

            {/* 進階設定（Top-P） */}
            <div>
              <button
                className="sp-btn"
                style={{ width: '100%', textAlign: 'left', display: 'flex', justifyContent: 'space-between' }}
                onClick={() => setShowAdvanced((v) => !v)}
              >
                <span>進階設定</span>
                <span>{showAdvanced ? '▲' : '▼'}</span>
              </button>
              {showAdvanced && (
                <div style={{ marginTop: 12, display: 'grid', gap: 16 }}>
                  <div style={{ display: 'grid', gap: 8 }}>
                    <label className="sp-label" style={{ margin: 0 }}>Top-P</label>
                    <select
                      className="sp-input"
                      value={topP === null ? 'default' : String(topP)}
                      onChange={(e) => {
                        const v = e.target.value
                        handleTopPChange(v === 'default' ? null : parseFloat(v))
                      }}
                      style={{ cursor: 'pointer' }}
                    >
                      <option value="default">不調整（預設）</option>
                      <option value="0.9">0.9</option>
                      <option value="0.8">0.8</option>
                      <option value="0.7">0.7</option>
                      <option value="0.5">0.5</option>
                    </select>
                    <p className="sp-hint" style={{ margin: 0 }}>
                      控制詞彙多樣性，一般不需要調整。
                    </p>
                  </div>

                  <div style={{ display: 'grid', gap: 8 }}>
                    <label className="sp-label" style={{ margin: 0 }}>
                      Thinking Budget（僅 Anthropic）
                    </label>
                    <input
                      className="sp-input"
                      type="number"
                      min={0}
                      step={1000}
                      placeholder="0 = 不啟用"
                      value={thinkingBudget ?? ''}
                      onChange={(e) => {
                        const raw = e.target.value
                        if (raw === '') {
                          handleThinkingBudgetChange(null)
                        } else {
                          const n = parseInt(raw, 10)
                          if (!isNaN(n) && n >= 0) handleThinkingBudgetChange(n)
                        }
                      }}
                    />
                    <p className="sp-hint" style={{ margin: 0 }}>
                      Anthropic extended thinking 的 token 預算。
                      需 ≥ 1024 才生效，建議 8000。
                      啟用時 temperature 會被忽略。
                    </p>
                  </div>
                </div>
              )}
            </div>

            {/* Health check */}
            <div style={{ display: 'grid', gap: 8 }}>
              <button
                className="sp-btn"
                onClick={handleHealthCheck}
                disabled={health.status === 'checking'}
              >
                {health.status === 'checking' ? '測試中…' : '測試連線'}
              </button>

              {health.status === 'ok' && (
                <div className="sp-health sp-health--ok">
                  <span>✓ 連線正常</span>
                  {health.data.gateway && <InfoRow label="Gateway" value={health.data.gateway} />}
                  <InfoRow
                    label="記憶"
                    value={health.data.memory_enabled ? `開啟（${health.data.memory_count ?? 0} 條）` : '關閉'}
                  />
                </div>
              )}

              {health.status === 'error' && (
                <div className="sp-health sp-health--err">
                  <span>✗ {health.message}</span>
                </div>
              )}
            </div>

            {/* 測試頁 */}
            {TEST_PAGE_URL && (
              <div>
                <a
                  href={TEST_PAGE_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="sp-btn"
                  style={{ display: 'block', textAlign: 'center', textDecoration: 'none' }}
                >
                  開啟測試頁 ↗
                </a>
                <p className="sp-hint">簡易測試介面，可直接貼訊息測試後端。</p>
              </div>
            )}

            {/* Railway */}
            <div>
              <a
                href="https://railway.com"
                target="_blank"
                rel="noopener noreferrer"
                className="sp-btn"
                style={{ display: 'block', textAlign: 'center', textDecoration: 'none' }}
              >
                開啟 Railway ↗
              </a>
              <p className="sp-hint">查看後端用量、環境變數（Variables）等。</p>
            </div>

          </div>
        )}

      </div>

      <style>{SP_STYLES}</style>
    </aside>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'grid', gap: 2 }}>
      <span className="sp-label" style={{ margin: 0 }}>{label}</span>
      <span style={{ fontSize: 13, color: 'var(--text-primary)', fontFamily: mono ? 'monospace' : undefined, wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  )
}

const SP_STYLES = `
.sp-label {
  display: block;
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.sp-input {
  width: 100%;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: 14px;
}
.sp-input:focus {
  outline: none;
  border-color: var(--accent-dim);
}
.sp-hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
}
.sp-btn {
  padding: 8px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 500;
  transition: background 0.12s;
}
.sp-btn:hover:not(:disabled) { background: var(--bg-hover); }
.sp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.sp-health {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  display: grid;
  gap: 8px;
}
.sp-health--ok  { background: rgba(100,200,130,0.1); border: 1px solid rgba(100,200,130,0.3); color: #7ecf9e; }
.sp-health--err { background: rgba(217,112,112,0.1); border: 1px solid rgba(217,112,112,0.25); color: #e89090; }
`
