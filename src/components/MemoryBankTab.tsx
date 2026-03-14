import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  createMemoryBankItem,
  deleteMemoryBankItem,
  exportMemoryBank,
  importMemoryBank,
  listMemoryBank,
  reembedAllMemoryBank,
  updateMemoryBankItem,
} from '../api'
import type { MemoryBankItem, MemoryBankUpsertInput } from '../types'

const CATEGORY_OPTIONS = [
  { value: 'anchor', label: 'anchor' },
  { value: 'personality', label: 'personality' },
  { value: 'event', label: 'event' },
  { value: 'diary', label: 'diary' },
  { value: 'letter', label: 'letter' },
  { value: 'conversation', label: 'conversation' },
  { value: 'general', label: 'general' },
]

type FormState = {
  title: string
  category: string
  tags: string
  content: string
  always_load: boolean
  enabled: boolean
  sort_order: string
  source_ref: string
  notes: string
}

const EMPTY_FORM: FormState = {
  title: '',
  category: 'general',
  tags: '',
  content: '',
  always_load: false,
  enabled: true,
  sort_order: '0',
  source_ref: '',
  notes: '',
}

function formFromItem(item: MemoryBankItem | null): FormState {
  if (!item) return { ...EMPTY_FORM }
  return {
    title: item.title,
    category: item.category || 'general',
    tags: (item.tags || []).join(', '),
    content: item.content,
    always_load: item.always_load,
    enabled: item.enabled,
    sort_order: String(item.sort_order ?? 0),
    source_ref: item.source_ref ?? '',
    notes: item.notes ?? '',
  }
}

function buildPayload(form: FormState): MemoryBankUpsertInput & { enabled: boolean } {
  return {
    title: form.title.trim(),
    category: form.category,
    tags: form.tags
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean),
    content: form.content.trim(),
    always_load: form.always_load,
    enabled: form.enabled,
    sort_order: Number.parseInt(form.sort_order, 10) || 0,
    source_ref: form.source_ref.trim(),
    notes: form.notes.trim(),
  }
}

function fmtTime(value: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function previewText(content: string, max = 160): string {
  const trimmed = content.trim()
  return trimmed.length > max ? `${trimmed.slice(0, max)}…` : trimmed
}

export default function MemoryBankTab() {
  const [items, setItems] = useState<MemoryBankItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')
  const [alwaysFilter, setAlwaysFilter] = useState<'all' | 'always' | 'ondemand'>('all')
  const [editingItem, setEditingItem] = useState<MemoryBankItem | null>(null)
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM })
  const [saving, setSaving] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [importText, setImportText] = useState('')
  const [showImport, setShowImport] = useState(false)

  const loadItems = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const rows = await listMemoryBank({
        category: categoryFilter || undefined,
        ...(alwaysFilter === 'all' ? {} : { always_load: alwaysFilter === 'always' }),
      })
      setItems(rows)
    } catch (err) {
      setError(err instanceof Error ? err.message : '載入失敗')
    } finally {
      setLoading(false)
    }
  }, [alwaysFilter, categoryFilter])

  useEffect(() => {
    void loadItems()
  }, [loadItems])

  const filteredItems = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return items
    return items.filter((item) => {
      const haystack = [
        item.title,
        item.category,
        ...(item.tags || []),
        item.content,
        item.source_ref || '',
        item.notes || '',
      ].join('\n').toLowerCase()
      return haystack.includes(normalized)
    })
  }, [items, query])

  function resetEditor() {
    setEditingItem(null)
    setForm({ ...EMPTY_FORM })
  }

  function startCreate() {
    setMessage('')
    setEditingItem(null)
    setForm({ ...EMPTY_FORM })
  }

  function startEdit(item: MemoryBankItem) {
    setMessage('')
    setEditingItem(item)
    setForm(formFromItem(item))
  }

  async function handleSave() {
    setSaving(true)
    setMessage('')
    try {
      const payload = buildPayload(form)
      if (editingItem) {
        await updateMemoryBankItem(editingItem.id, payload)
        setMessage(`已更新：${payload.title}`)
      } else {
        await createMemoryBankItem(payload)
        setMessage(`已新增：${payload.title}`)
      }
      resetEditor()
      await loadItems()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '保存失敗')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(item: MemoryBankItem) {
    if (!window.confirm(`刪除「${item.title}」？`)) return
    setBusy(`delete-${item.id}`)
    setMessage('')
    try {
      await deleteMemoryBankItem(item.id)
      setMessage(`已刪除：${item.title}`)
      if (editingItem?.id === item.id) resetEditor()
      await loadItems()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '刪除失敗')
    } finally {
      setBusy('')
    }
  }

  async function handleToggleEnabled(item: MemoryBankItem) {
    setBusy(`toggle-${item.id}`)
    setMessage('')
    try {
      await updateMemoryBankItem(item.id, { enabled: !item.enabled })
      await loadItems()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '更新失敗')
    } finally {
      setBusy('')
    }
  }

  async function handleExport() {
    setBusy('export')
    setMessage('')
    try {
      const rows = await exportMemoryBank()
      const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `memory-bank-${new Date().toISOString().slice(0, 10)}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      setMessage('已匯出 JSON')
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '匯出失敗')
    } finally {
      setBusy('')
    }
  }

  async function handleImport() {
    setBusy('import')
    setMessage('')
    try {
      const parsed = JSON.parse(importText)
      if (!Array.isArray(parsed)) {
        throw new Error('請貼上 JSON 陣列')
      }
      const result = await importMemoryBank(parsed)
      setMessage(`已匯入 ${result.imported} 筆`)
      setImportText('')
      setShowImport(false)
      await loadItems()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '匯入失敗')
    } finally {
      setBusy('')
    }
  }

  async function handleReembed() {
    if (!window.confirm('重算全部 embedding 會跑一陣子，要繼續嗎？')) return
    setBusy('reembed')
    setMessage('')
    try {
      const result = await reembedAllMemoryBank()
      setMessage(`已重算 ${result.updated} 筆 embedding`)
      await loadItems()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '重算失敗')
    } finally {
      setBusy('')
    }
  }

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input
          className="sp-input"
          style={{ flex: '1 1 180px' }}
          placeholder="搜尋標題 / tag / 內容"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select className="sp-input" style={{ width: 140 }} value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
          <option value="">全部分類</option>
          {CATEGORY_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select
          className="sp-input"
          style={{ width: 120 }}
          value={alwaysFilter}
          onChange={(e) => setAlwaysFilter(e.target.value as 'all' | 'always' | 'ondemand')}
        >
          <option value="all">全部</option>
          <option value="always">永載</option>
          <option value="ondemand">按需</option>
        </select>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button className="sp-btn" onClick={loadItems} disabled={loading}>{loading ? '載入中…' : '重整'}</button>
        <button className="sp-btn" onClick={startCreate}>新增</button>
        <button className="sp-btn" onClick={handleExport} disabled={busy !== ''}>{busy === 'export' ? '匯出中…' : '匯出 JSON'}</button>
        <button className="sp-btn" onClick={() => setShowImport((v) => !v)}>{showImport ? '收起匯入' : '批量匯入'}</button>
        <button className="sp-btn" onClick={handleReembed} disabled={busy !== ''}>{busy === 'reembed' ? '重算中…' : '重算向量'}</button>
      </div>

      {message && (
        <div className={`sp-health ${message.includes('失敗') || message.includes('錯') ? 'sp-health--err' : 'sp-health--ok'}`}>
          <span>{message}</span>
        </div>
      )}
      {error && <div className="sp-health sp-health--err"><span>{error}</span></div>}

      {showImport && (
        <div style={{ display: 'grid', gap: 8, padding: 12, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-elevated)' }}>
          <label className="sp-label" style={{ margin: 0 }}>貼上 JSON 陣列</label>
          <textarea
            className="sp-input"
            style={{ minHeight: 140, resize: 'vertical' }}
            placeholder='[{"title":"...","content":"..."}]'
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
          />
          <button className="sp-btn" onClick={handleImport} disabled={busy !== '' || !importText.trim()}>
            {busy === 'import' ? '匯入中…' : '開始匯入'}
          </button>
        </div>
      )}

      <div style={{ display: 'grid', gap: 8, padding: 12, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-elevated)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span className="sp-label" style={{ margin: 0 }}>{editingItem ? `編輯 #${editingItem.id}` : '新增項目'}</span>
          {(editingItem || form.title || form.content || form.tags) && (
            <button className="sp-btn" onClick={resetEditor}>清空</button>
          )}
        </div>

        <input className="sp-input" placeholder="標題" value={form.title} onChange={(e) => setForm((prev) => ({ ...prev, title: e.target.value }))} />
        <select className="sp-input" value={form.category} onChange={(e) => setForm((prev) => ({ ...prev, category: e.target.value }))}>
          {CATEGORY_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <input className="sp-input" placeholder="tags，用逗號分隔" value={form.tags} onChange={(e) => setForm((prev) => ({ ...prev, tags: e.target.value }))} />
        <textarea
          className="sp-input"
          style={{ minHeight: 140, resize: 'vertical' }}
          placeholder="完整原文"
          value={form.content}
          onChange={(e) => setForm((prev) => ({ ...prev, content: e.target.value }))}
        />
        <input className="sp-input" placeholder="source_ref" value={form.source_ref} onChange={(e) => setForm((prev) => ({ ...prev, source_ref: e.target.value }))} />
        <textarea
          className="sp-input"
          style={{ minHeight: 70, resize: 'vertical' }}
          placeholder="notes"
          value={form.notes}
          onChange={(e) => setForm((prev) => ({ ...prev, notes: e.target.value }))}
        />
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 13 }}>
            <input type="checkbox" checked={form.always_load} onChange={(e) => setForm((prev) => ({ ...prev, always_load: e.target.checked }))} />
            永載
          </label>
          <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 13 }}>
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.checked }))} />
            啟用
          </label>
          <input
            className="sp-input"
            style={{ width: 100 }}
            placeholder="sort"
            value={form.sort_order}
            onChange={(e) => setForm((prev) => ({ ...prev, sort_order: e.target.value }))}
          />
        </div>
        <button className="sp-btn" onClick={handleSave} disabled={saving || !form.title.trim() || !form.content.trim()}>
          {saving ? '保存中…' : editingItem ? '更新項目' : '新增項目'}
        </button>
      </div>

      <div style={{ display: 'grid', gap: 8 }}>
        {filteredItems.length === 0 && !loading && (
          <p className="sp-hint" style={{ margin: 0 }}>目前沒有符合條件的素材。</p>
        )}

        {filteredItems.map((item) => (
          <div key={item.id} style={{ display: 'grid', gap: 6, padding: 12, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-elevated)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 4 }}>
                  <strong style={{ color: 'var(--text-primary)' }}>{item.title}</strong>
                  <Tag text={item.category} />
                  <Tag text={item.always_load ? 'always' : 'ondemand'} muted />
                  <Tag text={item.enabled ? 'enabled' : 'disabled'} muted={!item.enabled} />
                  <Tag text={item.has_embedding ? 'vec' : 'no-vec'} muted />
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {(item.tags || []).map((tag) => <Tag key={tag} text={tag} muted />)}
                </div>
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>{fmtTime(item.updated_at)}</span>
            </div>

            <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.6 }}>
              {previewText(item.content)}
            </p>

            {(item.source_ref || item.notes) && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', display: 'grid', gap: 2 }}>
                {item.source_ref && <span>source: {item.source_ref}</span>}
                {item.notes && <span>notes: {previewText(item.notes, 80)}</span>}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button className="sp-btn" onClick={() => startEdit(item)}>編輯</button>
              <button className="sp-btn" onClick={() => handleToggleEnabled(item)} disabled={busy === `toggle-${item.id}`}>
                {busy === `toggle-${item.id}` ? '處理中…' : item.enabled ? '停用' : '啟用'}
              </button>
              <button className="sp-btn" onClick={() => handleDelete(item)} disabled={busy === `delete-${item.id}`}>
                {busy === `delete-${item.id}` ? '刪除中…' : '刪除'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Tag({ text, muted = false }: { text: string; muted?: boolean }) {
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: 11,
      lineHeight: 1.5,
      background: muted ? 'rgba(255,255,255,0.04)' : 'rgba(212,164,106,0.14)',
      color: muted ? 'var(--text-muted)' : '#d4a46a',
      border: '1px solid rgba(255,255,255,0.08)',
    }}>
      {text}
    </span>
  )
}
