// ============================================================
// api.ts — 所有後端呼叫集中在這裡
// UI 層不直接 fetch，也不解析 SSE
// ============================================================

import type {
  ChatMessage,
  CheckpointResult,
  HealthStatus,
  MemoryBankItem,
  MemoryBankUpsertInput,
  ModelProvider,
  ModelRoutingConfig,
  ModelRoutingMeta,
  ProviderModel,
  StreamEvent,
} from './types'

export const API_BASE_LS_KEY      = 'myloverM-api-base-url'
export const API_SECRET_LS_KEY    = 'myloverM-api-secret'
export const MODEL_LS_KEY         = 'myloverM-model'
export const CONTEXT_TURNS_LS_KEY = 'myloverM-context-turns'
export const TEMPERATURE_LS_KEY   = 'myloverM-temperature'
export const TOP_P_LS_KEY         = 'myloverM-top-p'
export const USER_NAME_LS_KEY     = 'myloverM-user-name'
export const THINKING_BUDGET_LS_KEY = 'myloverM-thinking-budget'

function getBaseUrl(): string {
  try {
    const saved = localStorage.getItem(API_BASE_LS_KEY)
    if (saved && saved.trim()) return saved.trim().replace(/\/$/, '')
  } catch { /* 非瀏覽器環境 */ }
  const env = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''
  return env.replace(/\/$/, '')
}

export function getConfiguredApiBaseUrl(): string {
  return getBaseUrl()
}

function apiUrl(path: string): string {
  const base = getBaseUrl()
  return base ? `${base}${path}` : path
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra }
  try {
    const secret = localStorage.getItem(API_SECRET_LS_KEY)?.trim()
    if (secret) headers['Authorization'] = `Bearer ${secret}`
  } catch { /* ignore */ }
  return headers
}

// ────────────────────────────────────────────────────────────
// Health check
// ────────────────────────────────────────────────────────────

export async function checkHealth(): Promise<HealthStatus> {
  const res = await fetch(apiUrl('/'))
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`)
  return res.json() as Promise<HealthStatus>
}

// 取得後端設定的 model 名稱
export async function fetchBackendModel(): Promise<string> {
  const res = await fetch(apiUrl('/v1/models'), { headers: authHeaders() })
  if (!res.ok) return ''
  const data = await res.json() as { data?: Array<{ id?: string }> }
  return data?.data?.[0]?.id ?? ''
}

export async function fetchModelRoutingMeta(): Promise<ModelRoutingMeta> {
  const res = await fetch(apiUrl('/api/model-routing/meta'), { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<ModelRoutingMeta>
}

export async function fetchProviderModels(provider: ModelProvider): Promise<ProviderModel[]> {
  const res = await fetch(apiUrl(`/api/model-routing/models?provider=${encodeURIComponent(provider)}`), { headers: authHeaders() })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const data = await res.json() as { error?: string }
      if (data?.error) message = data.error
    } catch {
      /* ignore */
    }
    throw new Error(message)
  }
  const data = await res.json() as { models?: ProviderModel[] }
  return data.models ?? []
}

// ────────────────────────────────────────────────────────────
// Chat（非 streaming，跟測試頁一樣）
// ────────────────────────────────────────────────────────────

export async function* streamChat(input: {
  model?: string         // 空字串或不傳 → 讓後端用 DEFAULT_MODEL
  messages: ChatMessage[]
  session_id: string
  temperature?: number   // 不傳 → 讓後端用預設值
  top_p?: number         // 不傳 → 讓後端用預設值
  thinking_budget?: number // Anthropic extended thinking budget（>= 1024 才生效）
  recall_card_ids?: number[] // 勾選召回的 Snapshot 卡片 ID
  model_routing?: ModelRoutingConfig
}): AsyncGenerator<StreamEvent, void, unknown> {

  const body: Record<string, unknown> = {
    messages: input.messages,
    session_id: input.session_id,
    stream: true,
  }
  // 只有明確指定才帶進去，讓後端用自己的預設值
  if (input.model && input.model.trim()) {
    body.model = input.model.trim()
  }
  if (input.temperature !== undefined) {
    body.temperature = input.temperature
  }
  if (input.top_p !== undefined) {
    body.top_p = input.top_p
  }
  if (input.thinking_budget !== undefined && input.thinking_budget >= 1024) {
    body.thinking_budget = input.thinking_budget
  }
  if (input.recall_card_ids && input.recall_card_ids.length > 0) {
    body.recall_card_ids = input.recall_card_ids
  }
  if (input.model_routing && Object.keys(input.model_routing).length > 0) {
    body.model_routing = input.model_routing
  }

  let res: Response
  try {
    res = await fetch(apiUrl('/v1/chat/completions'), {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    })
  } catch {
    yield { type: 'error', message: '無法連線到後端，請確認服務是否啟動。' }
    return
  }

  if (!res.ok) {
    let detail = ''
    try {
      const data = await res.json() as { error?: unknown; detail?: unknown }
      const raw = data?.error ?? data?.detail ?? ''
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
    } catch { /* ignore */ }
    yield { type: 'error', message: `後端回傳錯誤 ${res.status}${detail ? `：${detail}` : ''}` }
    return
  }

  if (!res.body) {
    yield { type: 'error', message: '無串流回應。' }
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue

        const raw = line.slice(6).trim()
        if (raw === '[DONE]') {
          yield { type: 'done' }
          return
        }

        try {
          const parsed = JSON.parse(raw) as {
            choices?: Array<{ delta?: { content?: string } }>
            usage?: {
              prompt_tokens?: number
              completion_tokens?: number
              total_tokens?: number
              completion_tokens_details?: { reasoning_tokens?: number }
            }
            _gateway_meta?: { used_checkpoint?: boolean }
            _thinking_content?: string
            _searching?: boolean
            query?: string
            error?: { message?: string }
          }

          if (parsed.error) {
            yield { type: 'error', message: parsed.error.message ?? 'Stream error' }
            return
          }

          if (parsed._searching) {
            yield { type: 'searching', query: parsed.query ?? '' }
            continue
          }

          const chunk = parsed.choices?.[0]?.delta?.content
          if (chunk) yield { type: 'delta', text: chunk }

          if (parsed.usage) {
            yield {
              type: 'usage',
              promptTokens: parsed.usage.prompt_tokens ?? 0,
              completionTokens: parsed.usage.completion_tokens ?? 0,
              totalTokens: parsed.usage.total_tokens ?? 0,
              reasoningTokens: parsed.usage.completion_tokens_details?.reasoning_tokens,
              usedCheckpoint: parsed._gateway_meta?.used_checkpoint ?? false,
              thinkingContent: parsed._thinking_content,
            }
          }
        } catch { /* ignore parse errors */ }
      }
    }
  } finally {
    reader.releaseLock()
  }

  yield { type: 'done' }
}

// ────────────────────────────────────────────────────────────
// Checkpoint（壓縮對話脈絡）
// ────────────────────────────────────────────────────────────

export async function createCheckpoint(sessionId: string, modelRouting?: ModelRoutingConfig): Promise<CheckpointResult> {
  const res = await fetch(apiUrl('/api/checkpoint/create'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      session_id: sessionId,
      ...(modelRouting && Object.keys(modelRouting).length > 0 ? { model_routing: modelRouting } : {}),
    }),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<CheckpointResult>
}

// ────────────────────────────────────────────────────────────
// Snapshot API
// ────────────────────────────────────────────────────────────

export interface CheckpointRecord {
  id: number
  session_id: string
  version: number
  summary_text: string
  covers_until_msg_id: number
  is_active: boolean
  created_at: string
  saved_as_card: boolean
  card_title: string | null
  card_edited_at: string | null
}

export async function listCheckpoints(sessionId: string): Promise<CheckpointRecord[]> {
  const res = await fetch(apiUrl(`/api/checkpoints?session_id=${encodeURIComponent(sessionId)}`), { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const data = await res.json() as { checkpoints: CheckpointRecord[] }
  return data.checkpoints ?? []
}

export async function listSnapshots(): Promise<CheckpointRecord[]> {
  const res = await fetch(apiUrl('/api/snapshots'), { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const data = await res.json() as { snapshots: CheckpointRecord[] }
  return data.snapshots ?? []
}

export async function saveSnapshot(checkpointId: number, cardTitle?: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/snapshots/${checkpointId}/save`), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ card_title: cardTitle ?? null }),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
}

export async function updateSnapshot(checkpointId: number, cardTitle: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/snapshots/${checkpointId}`), {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ card_title: cardTitle }),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
}

export async function deleteSnapshot(checkpointId: number): Promise<void> {
  const res = await fetch(apiUrl(`/api/snapshots/${checkpointId}`), { method: 'DELETE', headers: authHeaders() })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
}

// ────────────────────────────────────────────────────────────
// Memory Bank API
// ────────────────────────────────────────────────────────────

export async function listMemoryBank(params?: {
  category?: string
  always_load?: boolean
}): Promise<MemoryBankItem[]> {
  const query = new URLSearchParams()
  if (params?.category) query.set('category', params.category)
  if (params?.always_load !== undefined) query.set('always_load', String(params.always_load))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  const res = await fetch(apiUrl(`/api/memory-bank${suffix}`), { headers: authHeaders() })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<MemoryBankItem[]>
}

export async function createMemoryBankItem(item: MemoryBankUpsertInput): Promise<MemoryBankItem> {
  const res = await fetch(apiUrl('/api/memory-bank'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(item),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<MemoryBankItem>
}

export async function updateMemoryBankItem(
  id: number,
  fields: Partial<MemoryBankUpsertInput> & { enabled?: boolean },
): Promise<MemoryBankItem> {
  const res = await fetch(apiUrl(`/api/memory-bank/${id}`), {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(fields),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<MemoryBankItem>
}

export async function deleteMemoryBankItem(id: number): Promise<void> {
  const res = await fetch(apiUrl(`/api/memory-bank/${id}`), { method: 'DELETE', headers: authHeaders() })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
}

export async function importMemoryBank(items: MemoryBankUpsertInput[]): Promise<{ imported: number }> {
  const res = await fetch(apiUrl('/api/memory-bank/import'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(items),
  })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<{ imported: number }>
}

export async function exportMemoryBank(): Promise<MemoryBankItem[]> {
  const res = await fetch(apiUrl('/api/memory-bank/export'), { headers: authHeaders() })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<MemoryBankItem[]>
}

export async function reembedAllMemoryBank(): Promise<{ updated: number }> {
  const res = await fetch(apiUrl('/api/memory-bank/reembed-all'), { method: 'POST', headers: authHeaders() })
  if (!res.ok) {
    const data = await res.json() as { error?: string }
    throw new Error(data?.error ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<{ updated: number }>
}
