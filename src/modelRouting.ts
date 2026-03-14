import type { ModelProvider, ModelRouteChoice, ModelRoutingConfig, ModelRouteTask } from './types'

export const MODEL_ROUTING_LS_KEY = 'myloverM-model-routing-v1'
export const MODEL_ROUTING_CHANGE_EVENT = 'myloverM-model-routing-change'

const TASKS: ModelRouteTask[] = ['chat', 'summary', 'extraction']

function normalizeProvider(value: unknown): ModelProvider | null {
  if (value === 'openai' || value === 'anthropic' || value === 'gemini') return value
  return null
}

function normalizeChoice(value: unknown): ModelRouteChoice | null {
  if (!value || typeof value !== 'object') return null
  const entry = value as Record<string, unknown>
  const provider = normalizeProvider(entry.provider)
  const model = typeof entry.model === 'string' ? entry.model.trim() : ''
  if (!provider || !model) return null
  return { provider, model }
}

export function normalizeModelRouting(value: unknown): ModelRoutingConfig {
  if (!value || typeof value !== 'object') return {}
  const raw = value as Record<string, unknown>
  const next: ModelRoutingConfig = {}
  for (const task of TASKS) {
    const choice = normalizeChoice(raw[task])
    if (choice) next[task] = choice
  }
  return next
}

export function hasAnyModelRouting(value: ModelRoutingConfig | null | undefined): boolean {
  if (!value) return false
  return TASKS.some((task) => {
    const choice = value[task]
    return Boolean(choice?.provider && choice.model)
  })
}

export function loadModelRouting(): ModelRoutingConfig {
  try {
    return normalizeModelRouting(JSON.parse(localStorage.getItem(MODEL_ROUTING_LS_KEY) ?? '{}'))
  } catch {
    return {}
  }
}

export function saveModelRouting(value: ModelRoutingConfig): void {
  const normalized = normalizeModelRouting(value)
  try {
    localStorage.setItem(MODEL_ROUTING_LS_KEY, JSON.stringify(normalized))
    window.dispatchEvent(new CustomEvent(MODEL_ROUTING_CHANGE_EVENT, { detail: normalized }))
  } catch {
    /* ignore */
  }
}

export function clearModelRouting(): void {
  try {
    localStorage.removeItem(MODEL_ROUTING_LS_KEY)
    window.dispatchEvent(new CustomEvent(MODEL_ROUTING_CHANGE_EVENT, { detail: {} }))
  } catch {
    /* ignore */
  }
}

export function mergeModelRouting(
  base: ModelRoutingConfig,
  task: ModelRouteTask,
  choice: ModelRouteChoice | null,
): ModelRoutingConfig {
  const next: ModelRoutingConfig = { ...base }
  if (choice) {
    next[task] = choice
  } else {
    delete next[task]
  }
  return next
}

export function formatRouteLabel(choice: ModelRouteChoice | null | undefined): string {
  if (!choice?.model) return 'Railway 預設'
  const providerLabel = choice.provider === 'anthropic'
    ? 'Claude'
    : choice.provider === 'gemini'
      ? 'Gemini'
      : 'OpenAI'
  return `${providerLabel} · ${choice.model}`
}
