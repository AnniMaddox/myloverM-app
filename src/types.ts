// ============================================================
// 共用型別定義
// ============================================================

export type MessageRole = 'user' | 'assistant' | 'system'

export interface ChatTextContentPart {
  type: 'text'
  text: string
}

export interface ChatImageUrlContentPart {
  type: 'image_url'
  image_url: {
    url: string
  }
}

export type ChatContentPart = ChatTextContentPart | ChatImageUrlContentPart
export type ChatMessageContent = string | ChatContentPart[]

export interface ChatMessage {
  role: MessageRole
  content: ChatMessageContent
}

// 引用訊息的參考資料（顯示用）
export interface QuotedMessageRef {
  role: MessageRole
  senderName: string  // "M" 或使用者名字
  content: string     // 截斷後的顯示內容
}

// 前端顯示用的訊息（含 id 和狀態）
export interface DisplayMessage {
  id: string
  role: MessageRole
  content: string
  createdAt: number
  imageUrl?: string
  imageMemo?: string
  hasImage?: boolean
  isStreaming?: boolean
  isError?: boolean
  // 引用訊息
  quotedMessageRef?: QuotedMessageRef
  // token 使用量（完成後填入）
  promptTokens?: number
  completionTokens?: number
  totalTokens?: number
  reasoningTokens?: number   // 推理模型的 thinking tokens（o1/o3/DeepSeek-R1 等）
  usedCheckpoint?: boolean
  thinkingContent?: string   // Anthropic extended thinking 內容（折疊顯示）
}

// streaming 事件
export type StreamEvent =
  | { type: 'delta'; text: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
  | { type: 'usage'; promptTokens: number; completionTokens: number; totalTokens: number; reasoningTokens?: number; usedCheckpoint: boolean; thinkingContent?: string }
  | { type: 'searching'; query: string }

// 後端 health 回應
export interface HealthStatus {
  status: string
  gateway?: string
  memory_enabled?: boolean
  memory_count?: number
  system_prompt_loaded?: boolean
  system_prompt_length?: number
  memory_extract_interval?: number
}

export type ModelProvider = 'openai' | 'anthropic' | 'gemini'
export type ModelRouteTask = 'chat' | 'summary' | 'extraction'

export interface ModelRouteChoice {
  provider: ModelProvider
  model: string
}

export interface ModelRoutingConfig {
  chat?: ModelRouteChoice
  summary?: ModelRouteChoice
  extraction?: ModelRouteChoice
}

export interface ProviderStatus {
  id: ModelProvider
  label: string
  enabled: boolean
}

export interface ProviderModel {
  id: string
  label: string
}

export interface ModelRoutingMeta {
  providers: ProviderStatus[]
  defaults: {
    chat: ModelRouteChoice | null
    summary: ModelRouteChoice | null
    extraction: ModelRouteChoice | null
  }
  legacy_default_model: string
}

// 一段聊天紀錄（存 localStorage）
export interface StoredChat {
  id: string
  title: string
  sessionId: string
  messages: DisplayMessage[]
  updatedAt: number
  createdAt: number
  lastActiveAt: number  // 用來判斷 session 是否過期
  checkpointIndex?: number    // messages 陣列中最後一個被壓縮的訊息 index
  checkpointSummary?: string  // checkpoint 摘要文字（顯示用）
  checkpointCreatedAt?: number // checkpoint 建立時間
}

// checkpoint 建立回應
export interface CheckpointResult {
  status: string
  checkpoint_id: number
  version: number
  covers_until_msg_id: number
  summary: string
}

export interface MemoryBankItem {
  id: number
  title: string
  category: string
  tags: string[]
  content: string
  always_load: boolean
  enabled: boolean
  sort_order: number
  source_ref: string | null
  notes: string | null
  content_hash: string | null
  has_embedding: boolean
  created_at: string
  updated_at: string
}

export interface MemoryBankUpsertInput {
  title: string
  content: string
  category?: string
  tags?: string[]
  always_load?: boolean
  enabled?: boolean
  sort_order?: number
  source_ref?: string
  notes?: string
}
