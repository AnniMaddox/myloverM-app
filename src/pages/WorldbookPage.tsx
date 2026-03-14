import { useState, useEffect, useCallback } from 'react';

interface PersonaEntry {
  id: number;
  title: string;
  content: string;
  keywords: string;
  position: number;
  always_on: boolean;
  enabled: boolean;
  priority: number;
  created_at: string;
  updated_at: string;
}

const POSITION_LABELS: Record<number, string> = {
  0: '系統提示詞之前',
  1: '系統提示詞之後（預設）',
  2: '記憶之後',
  3: '對話之前',
};

function getApiBase(): string {
  const url = localStorage.getItem('myloverM-api-url') || '';
  return url.replace(/\/+$/, '');
}

async function apiFetch(path: string, opts?: RequestInit) {
  const base = getApiBase();
  if (!base) throw new Error('請先在設定中填入後端 URL');
  const res = await fetch(base + path, opts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

interface WorldbookPageProps {
  onBack: () => void;
}

type ViewState = 'list' | 'edit' | 'new';

const EMPTY_ENTRY: Omit<PersonaEntry, 'id' | 'created_at' | 'updated_at'> = {
  title: '',
  content: '',
  keywords: '',
  position: 1,
  always_on: false,
  enabled: true,
  priority: 50,
};

const BACKEND_URL_KEY = 'myloverM-api-url';

export default function WorldbookPage({ onBack }: WorldbookPageProps) {
  const [entries, setEntries] = useState<PersonaEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [view, setView] = useState<ViewState>('list');
  const [editing, setEditing] = useState<PersonaEntry | null>(null);
  const [form, setForm] = useState({ ...EMPTY_ENTRY });
  const [saving, setSaving] = useState(false);
  const [hasUrl, setHasUrl] = useState(!!getApiBase());
  const [urlInput, setUrlInput] = useState('');

  function saveUrl() {
    const v = urlInput.trim().replace(/\/$/, '');
    if (!v) return;
    try { localStorage.setItem(BACKEND_URL_KEY, v); } catch { /* ignore */ }
    setUrlInput('');
    setHasUrl(true);
  }

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await apiFetch('/api/worldbook');
      setEntries(data.entries || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function openNew() {
    setForm({ ...EMPTY_ENTRY });
    setEditing(null);
    setView('new');
  }

  function openEdit(entry: PersonaEntry) {
    setForm({
      title: entry.title,
      content: entry.content,
      keywords: entry.keywords,
      position: entry.position,
      always_on: entry.always_on,
      enabled: entry.enabled,
      priority: entry.priority,
    });
    setEditing(entry);
    setView('edit');
  }

  async function handleSave() {
    if (!form.title.trim() || !form.content.trim()) return;
    setSaving(true);
    try {
      if (view === 'new') {
        await apiFetch('/api/worldbook', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(form),
        });
      } else if (editing) {
        await apiFetch(`/api/worldbook/${editing.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(form),
        });
      }
      await load();
      setView('list');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleToggle(id: number, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await apiFetch(`/api/worldbook/${id}/toggle`, { method: 'PATCH' });
      setEntries(prev => prev.map(en => en.id === id ? { ...en, enabled: !en.enabled } : en));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDelete(id: number) {
    if (!confirm('確定刪除這個條目？')) return;
    try {
      await apiFetch(`/api/worldbook/${id}`, { method: 'DELETE' });
      setEntries(prev => prev.filter(en => en.id !== id));
      setView('list');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const isEditView = view === 'edit' || view === 'new';

  return (
    <div className="absolute inset-0 z-50 flex flex-col" style={{ background: 'var(--bg-base)' }}>
      <style>{WB_STYLES}</style>

      {/* Header */}
      <div className="wb-header">
        <button className="wb-back" onClick={isEditView ? () => setView('list') : onBack}>‹</button>
        <span className="wb-title">{isEditView ? (view === 'new' ? '新增條目' : '編輯條目') : '世界書'}</span>
        {!isEditView && (
          <button className="wb-add-btn" onClick={openNew}>＋</button>
        )}
        {isEditView && (
          <button className="wb-save-btn" onClick={handleSave} disabled={saving}>
            {saving ? '儲存中...' : '儲存'}
          </button>
        )}
      </div>

      {!hasUrl && (
        <div className="wb-nourl">
          <div style={{ marginBottom: 8 }}>尚未設定後端 URL，請貼上你的 Railway 網址：</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              className="wb-input"
              value={urlInput}
              onChange={e => setUrlInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && saveUrl()}
              placeholder="https://xxxx.up.railway.app"
            />
            <button className="wb-save-btn" onClick={saveUrl}>儲存</button>
          </div>
        </div>
      )}

      {error && (
        <div className="wb-error" onClick={() => setError('')}>{error} ✕</div>
      )}

      {/* List View */}
      {view === 'list' && (
        <div className="wb-list">
          {loading ? (
            <div className="wb-empty">載入中...</div>
          ) : entries.length === 0 ? (
            <div className="wb-empty">還沒有條目。點右上角 ＋ 新增。</div>
          ) : (
            entries.map(entry => (
              <div
                key={entry.id}
                className={`wb-item${entry.enabled ? ' wb-item--on' : ''}`}
                onClick={(e) => handleToggle(entry.id, e)}
              >
                {/* 勾選圈 */}
                <div className={`wb-check${entry.enabled ? ' wb-check--on' : ''}`}>
                  {entry.enabled && <span>✓</span>}
                </div>
                <div className="wb-item-main">
                  <div className="wb-item-title">{entry.title}</div>
                  <div className="wb-item-meta">
                    {entry.always_on ? '常駐' : entry.keywords ? `🔑 ${entry.keywords}` : '無關鍵字'}
                    {' · '}{POSITION_LABELS[entry.position] || `位置 ${entry.position}`}
                  </div>
                </div>
                <button
                  className="wb-edit-btn"
                  onClick={(e) => { e.stopPropagation(); openEdit(entry) }}
                  aria-label="編輯"
                >✏</button>
              </div>
            ))
          )}
        </div>
      )}

      {/* Edit / New View */}
      {isEditView && (
        <div className="wb-form">
          <div className="wb-field">
            <label className="wb-label">標題</label>
            <input
              className="wb-input"
              value={form.title}
              onChange={e => setForm(f => ({ ...f, title: e.target.value }))}
              placeholder="條目名稱（自己看的）"
            />
          </div>

          <div className="wb-field">
            <label className="wb-label">內容</label>
            <textarea
              className="wb-textarea"
              value={form.content}
              onChange={e => setForm(f => ({ ...f, content: e.target.value }))}
              placeholder="注入 system prompt 的實際文字..."
              rows={8}
            />
          </div>

          <div className="wb-field">
            <label className="wb-label">插入位置</label>
            <select
              className="wb-select"
              value={form.position}
              onChange={e => setForm(f => ({ ...f, position: Number(e.target.value) }))}
            >
              {Object.entries(POSITION_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>

          <div className="wb-field wb-field--row">
            <label className="wb-label">常駐（always_on）</label>
            <button
              className={`wb-toggle${form.always_on ? ' wb-toggle--on' : ''}`}
              onClick={() => setForm(f => ({ ...f, always_on: !f.always_on }))}
            />
          </div>

          {!form.always_on && (
            <div className="wb-field">
              <label className="wb-label">關鍵字（逗號分隔，任一命中即觸發）</label>
              <input
                className="wb-input"
                value={form.keywords}
                onChange={e => setForm(f => ({ ...f, keywords: e.target.value }))}
                placeholder="關鍵字一, 關鍵字二, keyword"
              />
            </div>
          )}

          <div className="wb-field">
            <label className="wb-label">優先度（數字小 = 優先）</label>
            <input
              className="wb-input"
              type="number"
              value={form.priority}
              onChange={e => setForm(f => ({ ...f, priority: Number(e.target.value) }))}
              min={0}
              max={999}
            />
          </div>

          <div className="wb-field wb-field--row">
            <label className="wb-label">啟用</label>
            <button
              className={`wb-toggle${form.enabled ? ' wb-toggle--on' : ''}`}
              onClick={() => setForm(f => ({ ...f, enabled: !f.enabled }))}
            />
          </div>

          {view === 'edit' && editing && (
            <button className="wb-delete-btn" onClick={() => handleDelete(editing.id)}>
              刪除此條目
            </button>
          )}
        </div>
      )}
    </div>
  );
}

const WB_STYLES = `
.wb-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  height: 56px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  background: var(--bg-surface);
}
.wb-back {
  font-size: 22px;
  color: var(--accent);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  line-height: 1;
}
.wb-title {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}
.wb-add-btn {
  font-size: 20px;
  color: var(--accent);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
}
.wb-save-btn {
  font-size: 14px;
  font-weight: 600;
  color: var(--accent);
  padding: 6px 12px;
  border-radius: var(--radius-md);
  background: var(--accent-bg);
}
.wb-save-btn:disabled { opacity: 0.4; }

.wb-nourl {
  padding: 12px 16px;
  background: rgba(217,96,90,0.1);
  border-bottom: 1px solid rgba(217,96,90,0.22);
  color: #e8908a;
  font-size: 14px;
  line-height: 1.5;
  flex-shrink: 0;
}
.wb-input {
  flex: 1;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.15);
  background: rgba(255,255,255,0.07);
  color: inherit;
  font-size: 13px;
  outline: none;
}
.wb-input:focus { border-color: var(--accent-dim); }

.wb-error {
  padding: 10px 16px;
  background: rgba(217,112,112,0.12);
  color: var(--color-error);
  font-size: 13px;
  cursor: pointer;
  flex-shrink: 0;
}

.wb-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.wb-empty {
  padding: 40px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}
.wb-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background 0.12s;
  margin-bottom: 2px;
  border: 1px solid transparent;
}
.wb-item:hover { background: var(--bg-hover); }
.wb-item--on {
  background: var(--accent-bg);
  border-color: rgba(212,164,106,0.18);
}
.wb-item-main { flex: 1; min-width: 0; }
.wb-item-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wb-item--on .wb-item-title { color: var(--accent); }
.wb-item-meta {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 勾選圈 */
.wb-check {
  flex-shrink: 0;
  width: 22px; height: 22px;
  border-radius: 50%;
  border: 1.5px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px;
  color: transparent;
  transition: all 0.15s;
}
.wb-check--on {
  background: var(--accent);
  border-color: var(--accent);
  color: #1a1712;
  font-weight: 700;
}

/* 編輯按鈕 */
.wb-edit-btn {
  flex-shrink: 0;
  width: 28px; height: 28px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  color: var(--text-muted);
  display: flex; align-items: center; justify-content: center;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s;
}
.wb-item:hover .wb-edit-btn { opacity: 1; }
.wb-edit-btn:hover { background: var(--bg-elevated); color: var(--text-primary); }

.wb-form {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.wb-field { display: flex; flex-direction: column; gap: 6px; }
.wb-field--row { flex-direction: row; align-items: center; justify-content: space-between; }
.wb-label { font-size: 12px; color: var(--text-muted); font-weight: 500; }
.wb-input, .wb-select {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  font-size: 14px;
  color: var(--text-primary);
  width: 100%;
}
.wb-input:focus, .wb-select:focus { border-color: var(--accent-dim); outline: none; }
.wb-textarea {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  font-size: 14px;
  color: var(--text-primary);
  width: 100%;
  resize: vertical;
  min-height: 140px;
  line-height: 1.6;
}
.wb-textarea:focus { border-color: var(--accent-dim); outline: none; }
.wb-select { appearance: none; }
.wb-delete-btn {
  margin-top: 8px;
  padding: 10px;
  border-radius: var(--radius-md);
  color: var(--color-error);
  background: rgba(217,112,112,0.08);
  border: 1px solid rgba(217,112,112,0.2);
  font-size: 14px;
  text-align: center;
}
`;
