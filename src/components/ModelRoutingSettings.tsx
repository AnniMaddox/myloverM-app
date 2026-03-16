import { useCallback, useEffect, useState } from 'react'
import { fetchModelRoutingMeta, fetchProviderModels, getConfiguredApiBaseUrl, saveVectorizeSettings } from '../api'
import {
  clearModelRouting,
  formatRouteLabel,
  hasAnyModelRouting,
  loadModelRouting,
  mergeModelRouting,
  normalizeModelRouting,
  saveModelRouting,
} from '../modelRouting'
import type {
  ModelProvider,
  ModelRouteTask,
  ModelRoutingConfig,
  ModelRoutingMeta,
  ProviderModel,
} from '../types'

interface Props {
  open: boolean
  onClose: () => void
}

const TASKS: Array<{ id: ModelRouteTask; label: string; description: string }> = [
  { id: 'chat', label: '聊天', description: '主聊天回覆走這條。' },
  { id: 'summary', label: '摘要 / 壓縮', description: 'session summary 跟 checkpoint 都共用這條。' },
  { id: 'extraction', label: '提取', description: '記憶提取、open loop 提取走這條。' },
  { id: 'conv_embedding', label: 'Embedding（對話向量化）', description: '舊對話向量化用的 embedding 模型。只有 OpenAI embedding 系列有效（text-embedding-3-small 等）。' },
]

const UPCOMING_TASK = {
  id: 'monitor',
  label: '監聽 / 主動敲你',
  description: '第二階段預留位。之後讓 M 自己判斷要不要主動來敲你。',
}

export default function ModelRoutingSettings({ open, onClose }: Props) {
  const [meta, setMeta] = useState<ModelRoutingMeta | null>(null)
  const [routing, setRouting] = useState<ModelRoutingConfig>({})
  const [apiBase, setApiBase] = useState('')
  const [loading, setLoading] = useState(false)
  const [metaError, setMetaError] = useState('')
  const [modelCache, setModelCache] = useState<Partial<Record<ModelProvider, ProviderModel[]>>>({})
  const [providerLoading, setProviderLoading] = useState<Partial<Record<ModelProvider, boolean>>>({})
  const [providerErrors, setProviderErrors] = useState<Partial<Record<ModelProvider, string>>>({})

  const loadModels = useCallback(async (provider: ModelProvider) => {
    setProviderLoading((prev) => ({ ...prev, [provider]: true }))
    setProviderErrors((prev) => ({ ...prev, [provider]: '' }))
    try {
      const models = await fetchProviderModels(provider)
      setModelCache((prev) => ({ ...prev, [provider]: models }))
      return models
    } catch (error) {
      const message = error instanceof Error ? error.message : '拉取模型失敗'
      setProviderErrors((prev) => ({ ...prev, [provider]: message }))
      return []
    } finally {
      setProviderLoading((prev) => ({ ...prev, [provider]: false }))
    }
  }, [])

  useEffect(() => {
    if (!open) return
    let cancelled = false

    const run = async () => {
      const nextApiBase = getConfiguredApiBaseUrl()
      setApiBase(nextApiBase)
      if (!nextApiBase) {
        setMeta(null)
        setMetaError('先回 System 填 Railway 後端 URL，這頁才知道要去哪拉 provider 跟模型。')
        setLoading(false)
        return
      }

      setLoading(true)
      setMetaError('')
      try {
        const nextMeta = await fetchModelRoutingMeta()
        if (cancelled) return

        setMeta(nextMeta)
        const stored = loadModelRouting()
        const initial = hasAnyModelRouting(stored)
          ? stored
          : normalizeModelRouting(nextMeta.defaults)
        setRouting(initial)
        if (!hasAnyModelRouting(stored) && hasAnyModelRouting(initial)) {
          saveModelRouting(initial)
        }

        const providerSet = new Set<ModelProvider>()
        for (const task of TASKS) {
          const provider = initial[task.id]?.provider
          if (provider) providerSet.add(provider)
        }
        for (const provider of providerSet) {
          void loadModels(provider)
        }
      } catch (error) {
        if (cancelled) return
        const message = error instanceof Error ? error.message : '載入模型設定失敗'
        setMetaError(
          /404|Failed to fetch|Network/i.test(message)
            ? `連不到模型設定 API。先檢查 System 裡的後端 URL 是否是 Railway 根網址。`
            : message,
        )
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void run()
    return () => {
      cancelled = true
    }
  }, [open])

  if (!open) return null

  const providers = meta?.providers ?? []
  const providerEnabled = new Map(providers.map((provider) => [provider.id, provider.enabled]))

  function persist(next: ModelRoutingConfig) {
    setRouting(next)
    if (hasAnyModelRouting(next)) {
      saveModelRouting(next)
    } else {
      clearModelRouting()
    }
    // 若有設 conv_embedding，同步推到後端
    const embChoice = next.conv_embedding
    if (embChoice?.model) {
      void saveVectorizeSettings(embChoice.model).catch(() => {/* 靜默失敗 */})
    }
  }

  function handleProviderChange(task: ModelRouteTask, providerValue: string) {
    if (!providerValue) {
      persist(mergeModelRouting(routing, task, null))
      return
    }

    const provider = providerValue as ModelProvider
    const current = routing[task]
    const next = mergeModelRouting(routing, task, {
      provider,
      model: current?.provider === provider ? current.model : '',
    })
    persist(next)
    void loadModels(provider)
  }

  function handleModelChange(task: ModelRouteTask, model: string) {
    const current = routing[task]
    if (!current) return
    persist(mergeModelRouting(routing, task, { ...current, model }))
  }

  function applyRailwayDefaults() {
    if (!meta) return
    const defaults = normalizeModelRouting(meta.defaults)
    persist(defaults)
    for (const task of TASKS) {
      const provider = defaults[task.id]?.provider
      if (provider) void loadModels(provider)
    }
  }

  function getModelsForTask(task: ModelRouteTask): ProviderModel[] {
    const choice = routing[task]
    if (!choice?.provider) return []
    const models = modelCache[choice.provider] ?? []
    if (!choice.model || models.some((model) => model.id === choice.model)) return models
    return [{ id: choice.model, label: choice.model }, ...models]
  }

  return (
    <div className="mrs-overlay" role="dialog" aria-modal="true">
      <div className="mrs-shell">
        <div className="mrs-header">
          <div>
            <p className="mrs-kicker">Model Routing</p>
            <h2>官方 API 切換</h2>
            <p className="mrs-subtitle">畫面只存 provider / model，真正的 key 還是留在 Railway 後端。</p>
            <p className="mrs-api-base">
              後端：{apiBase || '尚未設定'}
            </p>
          </div>
          <button className="mrs-close" onClick={onClose} aria-label="關閉">✕</button>
        </div>

        <div className="mrs-toolbar">
          <button className="mrs-btn" onClick={applyRailwayDefaults} disabled={!meta || loading}>
            套用 Railway 預設
          </button>
          <button className="mrs-btn mrs-btn--ghost" onClick={() => persist({})}>
            清掉本地覆蓋
          </button>
        </div>

        {loading && <div className="mrs-banner">載入中…</div>}
        {metaError && <div className="mrs-banner mrs-banner--error">{metaError}</div>}

        {!loading && meta && (
          <>
            <div className="mrs-provider-row">
              {providers.map((provider) => (
                <div key={provider.id} className={`mrs-provider-pill${provider.enabled ? '' : ' is-off'}`}>
                  <span>{provider.label}</span>
                  <strong>{provider.enabled ? '已接上' : '未放 key'}</strong>
                </div>
              ))}
            </div>

            <div className="mrs-grid">
              {TASKS.map((task) => {
                const choice = routing[task.id]
                const models = getModelsForTask(task.id)
                const provider = choice?.provider
                const enabled = provider ? providerEnabled.get(provider) !== false : true
                const providerError = provider ? providerErrors[provider] : ''
                const isLoadingModels = provider ? providerLoading[provider] === true : false

                return (
                  <section key={task.id} className="mrs-card">
                    <div className="mrs-card-head">
                      <div>
                        <h3>{task.label}</h3>
                        <p>{task.description}</p>
                      </div>
                      <span className="mrs-current">{formatRouteLabel(choice)}</span>
                    </div>

                    <label className="mrs-label">公司</label>
                    <select
                      className="mrs-input"
                      value={provider ?? ''}
                      onChange={(event) => handleProviderChange(task.id, event.target.value)}
                    >
                      <option value="">跟 Railway 預設走</option>
                      {providers.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}{item.enabled ? '' : '（未設定 key）'}
                        </option>
                      ))}
                    </select>

                    <div className="mrs-model-row">
                      <div>
                        <label className="mrs-label">模型</label>
                        <select
                          className="mrs-input"
                          value={choice?.model ?? ''}
                          onChange={(event) => handleModelChange(task.id, event.target.value)}
                          disabled={!provider || !enabled}
                        >
                          <option value="">
                            {!provider ? '先選公司' : !enabled ? '這家還沒放 key' : '先拉模型清單'}
                          </option>
                          {models.map((model) => (
                            <option key={model.id} value={model.id}>{model.label}</option>
                          ))}
                        </select>
                      </div>

                      <button
                        className="mrs-btn"
                        onClick={() => provider && void loadModels(provider)}
                        disabled={!provider || !enabled || isLoadingModels}
                      >
                        {isLoadingModels ? '拉取中…' : '拉取模型'}
                      </button>
                    </div>

                    {provider && !enabled && (
                      <p className="mrs-hint mrs-hint--warn">這家在 Railway 還沒放 key，現在選了也打不出去。</p>
                    )}
                    {providerError && (
                      <p className="mrs-hint mrs-hint--warn">{providerError}</p>
                    )}
                    {!providerError && provider && enabled && models.length > 0 && (
                      <p className="mrs-hint">已拉到 {models.length} 個模型。要換別家的話直接切上面那個公司。</p>
                    )}
                  </section>
                )
              })}

              <section className="mrs-card mrs-card--disabled">
                <div className="mrs-card-head">
                  <div>
                    <h3>{UPCOMING_TASK.label}</h3>
                    <p>{UPCOMING_TASK.description}</p>
                  </div>
                  <span className="mrs-current">尚未接線</span>
                </div>

                <label className="mrs-label">公司</label>
                <select className="mrs-input" disabled>
                  <option>下一階段開放</option>
                </select>

                <div className="mrs-model-row">
                  <div>
                    <label className="mrs-label">模型</label>
                    <select className="mrs-input" disabled>
                      <option>等監聽邏輯接上後開放</option>
                    </select>
                  </div>
                  <button className="mrs-btn" disabled>拉取模型</button>
                </div>

                <p className="mrs-hint">這格先保留位置，避免你覺得少一塊。真的要動手接，是下一輪。</p>
              </section>
            </div>

            <div className="mrs-footer-note">
              <strong>補充：</strong>
              摘要 / 壓縮 這條同時影響 session summary 和 checkpoint。提取這條則影響記憶提取跟 open loop 提取。
            </div>
          </>
        )}
      </div>

      <style>{STYLES}</style>
    </div>
  )
}

const STYLES = `
.mrs-overlay {
  position: fixed;
  inset: 0;
  z-index: 70;
  background:
    radial-gradient(circle at top left, rgba(212, 164, 106, 0.18), transparent 34%),
    linear-gradient(135deg, rgba(15, 16, 22, 0.98), rgba(23, 26, 35, 0.96));
  backdrop-filter: blur(14px);
  display: flex;
  justify-content: center;
  padding: 28px;
  overflow-y: auto;
}

.mrs-shell {
  width: min(1120px, 100%);
  height: fit-content;
  background: rgba(15, 17, 24, 0.94);
  border: 1px solid rgba(212, 164, 106, 0.22);
  border-radius: 24px;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
  padding: 28px;
  display: grid;
  gap: 20px;
}

.mrs-header {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
}

.mrs-kicker {
  margin: 0 0 6px;
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #d4a46a;
}

.mrs-header h2 {
  margin: 0;
  font-size: 30px;
  color: #f4efe8;
}

.mrs-subtitle {
  margin: 8px 0 0;
  color: rgba(244, 239, 232, 0.72);
  font-size: 14px;
}

.mrs-api-base {
  margin: 10px 0 0;
  font-size: 12px;
  color: rgba(244, 239, 232, 0.58);
  word-break: break-all;
}

.mrs-close {
  width: 40px;
  height: 40px;
  border-radius: 999px;
  border: 1px solid rgba(212, 164, 106, 0.24);
  color: #f4efe8;
  background: rgba(255, 255, 255, 0.04);
}

.mrs-toolbar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.mrs-btn {
  border-radius: 12px;
  border: 1px solid rgba(212, 164, 106, 0.28);
  padding: 10px 14px;
  background: rgba(212, 164, 106, 0.12);
  color: #f4efe8;
  font-size: 13px;
}

.mrs-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.mrs-btn--ghost {
  background: rgba(255, 255, 255, 0.04);
}

.mrs-banner {
  padding: 12px 14px;
  border-radius: 14px;
  background: rgba(212, 164, 106, 0.12);
  color: #f4efe8;
  font-size: 13px;
}

.mrs-banner--error {
  background: rgba(199, 92, 92, 0.18);
  color: #ffd0d0;
}

.mrs-provider-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.mrs-provider-pill {
  padding: 12px 14px;
  border-radius: 16px;
  border: 1px solid rgba(212, 164, 106, 0.22);
  background: rgba(255, 255, 255, 0.04);
  color: #f4efe8;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
}

.mrs-provider-pill strong {
  font-weight: 600;
  color: #d4a46a;
}

.mrs-provider-pill.is-off strong {
  color: #ff9a9a;
}

.mrs-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.mrs-card {
  display: grid;
  gap: 10px;
  padding: 18px;
  border-radius: 18px;
  border: 1px solid rgba(212, 164, 106, 0.18);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.02));
}

.mrs-card--disabled {
  opacity: 0.7;
  border-style: dashed;
}

.mrs-card-head {
  display: grid;
  gap: 8px;
}

.mrs-card-head h3 {
  margin: 0;
  font-size: 18px;
  color: #f4efe8;
}

.mrs-card-head p,
.mrs-footer-note,
.mrs-hint {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: rgba(244, 239, 232, 0.72);
}

.mrs-current {
  display: inline-flex;
  width: fit-content;
  max-width: 100%;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(212, 164, 106, 0.14);
  color: #f5d7ab;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mrs-label {
  font-size: 12px;
  color: rgba(244, 239, 232, 0.62);
}

.mrs-input {
  width: 100%;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid rgba(212, 164, 106, 0.18);
  background: rgba(7, 9, 13, 0.7);
  color: #f4efe8;
}

.mrs-model-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: end;
}

.mrs-hint--warn {
  color: #ffb4b4;
}

.mrs-footer-note {
  padding: 14px 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.04);
}

@media (max-width: 980px) {
  .mrs-overlay {
    padding: 16px;
  }

  .mrs-shell {
    padding: 18px;
  }

  .mrs-provider-row,
  .mrs-grid {
    grid-template-columns: 1fr;
  }
}
`
