import { useState, useEffect, useCallback, useRef } from 'react';

// ─── API ─────────────────────────────────────────────────────
const BACKEND_URL_KEY = 'myloverM-api-url';
const CHAT_STORAGE_KEY = 'myloverM_chats';

function getApiBase(): string {
  try {
    return localStorage.getItem(BACKEND_URL_KEY)?.trim().replace(/\/$/, '') ?? '';
  } catch { return ''; }
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const base = getApiBase();
  if (!base) throw new Error('NO_URL');
  return fetch(`${base}${path}`, init);
}

// ─── Types ───────────────────────────────────────────────────
interface Memory {
  id: number;
  content: string;
  tier: string;
  status: string;
  importance: number;
  manual_locked: boolean;
  pending_review: boolean;
  created_at: string;
  source_session?: string;
  canonical_key?: string;
}

interface OpenLoop {
  id: number;
  content: string;
  loop_type: string;
  status: string;
  created_at: string;
  resolved_at?: string;
}

// ─── Constants ───────────────────────────────────────────────
const TIER_LABEL: Record<string, string> = {
  ephemeral: '短期',
  stable: '穩定',
  evergreen: '永久',
};
const TIER_COLOR: Record<string, string> = {
  ephemeral: '#888',
  stable: '#5b8dd9',
  evergreen: '#c9953a',
};

type Tab = 'memories' | 'review' | 'loops' | 'snapshot' | 'backup' | 'guide';
const TABS: { key: Tab; label: string; icon: string }[] = [
  { key: 'memories', label: '記憶',   icon: '🧠' },
  { key: 'review',   label: '待審',   icon: '✅' },
  { key: 'loops',    label: 'Loops',  icon: '🔄' },
  { key: 'snapshot', label: '快照',   icon: '📸' },
  { key: 'backup',   label: '備份',   icon: '💾' },
  { key: 'guide',    label: '說明',   icon: '📖' },
];

const RECALL_KEY = 'myloverM-recall-card-ids';

function loadRecallMap(): Record<number, 'manual' | 'once'> {
  try {
    const raw = localStorage.getItem(RECALL_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Record<number, 'manual' | 'once'>;
  } catch { return {}; }
}

function saveRecallMap(map: Record<number, 'manual' | 'once'>) {
  try { localStorage.setItem(RECALL_KEY, JSON.stringify(map)); } catch { /* ignore */ }
}

interface SnapshotRecord {
  id: number;
  session_id: string;
  version: number;
  summary_text: string;
  covers_until_msg_id: number;
  is_active: boolean;
  created_at: string;
  saved_as_card: boolean;
  card_title: string | null;
  card_edited_at: string | null;
}

// ─── Main ────────────────────────────────────────────────────
interface Props { onBack: () => void; }

export default function MemoryPage({ onBack }: Props) {
  const [tab, setTab] = useState<Tab>('memories');
  const [hasUrl, setHasUrl] = useState(!!getApiBase());
  const [urlInput, setUrlInput] = useState('');

  function saveUrl() {
    const v = urlInput.trim().replace(/\/$/, '');
    if (!v) return;
    try { localStorage.setItem(BACKEND_URL_KEY, v); } catch { /* ignore */ }
    setUrlInput('');
    setHasUrl(true);
  }

  return (
    <div className="absolute inset-0 z-50 flex flex-col" style={{ background: '#1a1612', color: '#f0e8d8', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif', overflow: 'hidden' }}>
      <style>{MEM_STYLES}</style>

      {/* Header */}
      <header className="mem-header">
        <button className="mem-back" onClick={onBack} aria-label="返回">
          <span style={{ transform: 'translateY(-1px)', display: 'block' }}>‹</span>
        </button>
        <span className="mem-title">記憶室</span>
        <div style={{ width: 36 }} />
      </header>

      {!hasUrl && (
        <div className="mem-nourl">
          <div style={{ marginBottom: 8 }}>尚未設定後端 URL，請貼上你的 Railway 網址：</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={urlInput}
              onChange={e => setUrlInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && saveUrl()}
              placeholder="https://xxxx.up.railway.app"
              className="mem-input"
              style={{ flex: 1 }}
            />
            <button onClick={saveUrl} className="mem-pill-btn">儲存</button>
          </div>
        </div>
      )}

      {/* Tab bar */}
      <nav className="mem-tabbar">
        {TABS.map(t => (
          <button
            key={t.key}
            className={`mem-tab${tab === t.key ? ' mem-tab--on' : ''}`}
            onClick={() => setTab(t.key)}
          >
            <span className="mem-tab-icon">{t.icon}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </nav>

      <div className="mem-body">
        {tab === 'memories'  && <MemoriesTab />}
        {tab === 'review'    && <ReviewTab />}
        {tab === 'loops'     && <LoopsTab />}
        {tab === 'snapshot'  && <SnapshotTab />}
        {tab === 'backup'    && <BackupTab />}
        {tab === 'guide'     && <GuideTab />}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Tab 1 — 記憶列表
// ═══════════════════════════════════════════════════════════
function MemoriesTab() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [search, setSearch] = useState('');
  const [tierFilter, setTierFilter] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [sortBy, setSortBy] = useState<'date' | 'importance'>('date');
  const [sortOrder, setSortOrder] = useState<'desc' | 'asc'>('desc');
  const [showAdd, setShowAdd] = useState(false);
  const [addContent, setAddContent] = useState('');
  const [addTier, setAddTier] = useState<string>('stable');
  const [addImportance, setAddImportance] = useState(7);
  const [addLoading, setAddLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (q: string, tier: string, sort: string, order: string) => {
    setLoading(true);
    setErr('');
    try {
      const params = new URLSearchParams();
      if (q) params.set('search', q);
      if (tier) params.set('tier', tier);
      params.set('sort', sort);
      params.set('order', order);
      const res = await apiFetch(`/api/memories?${params}`);
      const data = await res.json() as { memories?: Memory[]; error?: string };
      if (data.error) { setErr(data.error); return; }
      setMemories(data.memories ?? []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '載入失敗');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(search, tierFilter, sortBy, sortOrder); }, [load, search, tierFilter, sortBy, sortOrder]);

  function handleSearchInput(v: string) {
    setSearchInput(v);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setSearch(v.trim()), 500);
  }

  function toggleSort(field: 'date' | 'importance') {
    if (sortBy === field) {
      setSortOrder(o => o === 'desc' ? 'asc' : 'desc');
    } else {
      setSortBy(field);
      setSortOrder('desc');
    }
  }

  async function handleAdd() {
    if (!addContent.trim()) return;
    setAddLoading(true);
    try {
      const res = await apiFetch('/api/memories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: addContent.trim(), tier: addTier, importance: addImportance }),
      });
      const data = await res.json() as { status?: string; error?: string };
      if (data.error) { alert(data.error); return; }
      setAddContent('');
      setShowAdd(false);
      load(search, tierFilter, sortBy, sortOrder);
    } catch { alert('新增失敗'); }
    finally { setAddLoading(false); }
  }

  async function handleUpgrade(id: number) {
    try {
      await apiFetch(`/api/memories/${id}/upgrade`, { method: 'POST' });
      load(search, tierFilter, sortBy, sortOrder);
    } catch { alert('升級失敗'); }
  }

  async function handleLock(id: number) {
    try {
      await apiFetch(`/api/memories/${id}/lock`, { method: 'POST' });
      load(search, tierFilter, sortBy, sortOrder);
    } catch { alert('鎖定切換失敗'); }
  }

  async function handleDelete(id: number, content: string) {
    if (!confirm(`確定刪除？\n\n「${content.slice(0, 60)}」`)) return;
    try {
      await apiFetch(`/api/memories/${id}`, { method: 'DELETE' });
      setMemories(prev => prev.filter(m => m.id !== id));
    } catch { alert('刪除失敗'); }
  }

  return (
    <div className="mem-section">
      <div className="mem-filters">
        <input
          className="mem-input"
          placeholder="搜尋記憶…"
          value={searchInput}
          onChange={e => handleSearchInput(e.target.value)}
        />
        <div className="mem-chips">
          {(['', 'ephemeral', 'stable', 'evergreen'] as const).map(t => (
            <button
              key={t}
              className={`mem-chip${tierFilter === t ? ' mem-chip--on' : ''}`}
              style={t && tierFilter === t ? { borderColor: TIER_COLOR[t], color: TIER_COLOR[t], background: `${TIER_COLOR[t]}18` } : {}}
              onClick={() => setTierFilter(t)}
            >
              {t ? TIER_LABEL[t] : '全部'}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <button
          className={`mem-chip${sortBy === 'date' ? ' mem-chip--on' : ''}`}
          onClick={() => toggleSort('date')}
        >
          日期 {sortBy === 'date' ? (sortOrder === 'desc' ? '↓' : '↑') : ''}
        </button>
        <button
          className={`mem-chip${sortBy === 'importance' ? ' mem-chip--on' : ''}`}
          onClick={() => toggleSort('importance')}
        >
          權重 {sortBy === 'importance' ? (sortOrder === 'desc' ? '↓' : '↑') : ''}
        </button>
        <div style={{ flex: 1 }} />
        <button
          className="mem-chip mem-chip--accent"
          onClick={() => setShowAdd(v => !v)}
        >
          {showAdd ? '✕ 取消' : '＋ 新增'}
        </button>
      </div>

      {showAdd && (
        <div className="mem-card" style={{ background: 'rgba(255,255,255,0.04)' }}>
          <textarea
            className="mem-input"
            placeholder="輸入記憶內容…"
            value={addContent}
            onChange={e => setAddContent(e.target.value)}
            rows={3}
            style={{ width: '100%', resize: 'vertical', marginBottom: 10, boxSizing: 'border-box' }}
          />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
            <span className="mem-label">層級：</span>
            {(['ephemeral', 'stable', 'evergreen'] as const).map(t => (
              <button
                key={t}
                className={`mem-chip${addTier === t ? ' mem-chip--on' : ''}`}
                style={addTier === t ? { borderColor: TIER_COLOR[t], color: TIER_COLOR[t], background: `${TIER_COLOR[t]}18` } : {}}
                onClick={() => setAddTier(t)}
              >
                {TIER_LABEL[t]}
              </button>
            ))}
            <span className="mem-label" style={{ marginLeft: 8 }}>重要度：</span>
            <input
              type="number" min={1} max={10} value={addImportance}
              onChange={e => setAddImportance(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
              style={{ width: 48, padding: '3px 6px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(255,255,255,0.06)', color: 'inherit', fontSize: 13, textAlign: 'center' }}
            />
          </div>
          <button
            className="mem-act mem-act--ok"
            onClick={handleAdd}
            disabled={addLoading || !addContent.trim()}
            style={{ width: '100%', padding: '9px 0', justifyContent: 'center' }}
          >
            {addLoading ? '新增中…' : '確認新增'}
          </button>
        </div>
      )}

      {err && <div className="mem-err">{err}</div>}
      {loading && <div className="mem-empty">載入中…</div>}
      {!loading && !err && memories.length === 0 && (
        <div className="mem-empty">沒有符合的記憶</div>
      )}

      <div className="mem-list">
        {memories.map(m => (
          <div key={m.id} className="mem-card">
            <div className="mem-card-top">
              <span className="mem-badge" style={{ background: TIER_COLOR[m.tier] ?? '#666' }}>
                {TIER_LABEL[m.tier] ?? m.tier}
              </span>
              {m.manual_locked && <span className="mem-badge mem-badge--lock">🔒 鎖定</span>}
              {m.pending_review && <span className="mem-badge mem-badge--review">待審</span>}
              <span className="mem-label" style={{ marginLeft: 'auto' }}>重要度 {m.importance}</span>
            </div>
            <p className="mem-card-content">{m.content}</p>
            <div className="mem-card-meta">{m.created_at ? new Date(m.created_at).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' }) : ''}</div>
            <div className="mem-card-actions">
              {m.tier !== 'evergreen' && (
                <button className="mem-act mem-act--up" onClick={() => handleUpgrade(m.id)}>↑ 升級</button>
              )}
              <button className="mem-act" onClick={() => handleLock(m.id)}>
                {m.manual_locked ? '解鎖' : '🔒 鎖'}
              </button>
              <button className="mem-act mem-act--del" onClick={() => handleDelete(m.id, m.content)}>刪除</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Tab 2 — 待審
// ═══════════════════════════════════════════════════════════
function ReviewTab() {
  const [list, setList] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');

  const load = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const res = await apiFetch('/api/memories?tier=evergreen');
      const data = await res.json() as { memories?: Memory[]; error?: string };
      if (data.error) { setErr(data.error); return; }
      setList((data.memories ?? []).filter(m => m.pending_review));
    } catch (e) {
      setErr(e instanceof Error ? e.message : '載入失敗');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function approve(id: number) {
    try {
      await apiFetch(`/api/memories/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pending_review: false }),
      });
      setList(prev => prev.filter(m => m.id !== id));
    } catch { alert('確認失敗'); }
  }

  async function reject(id: number) {
    try {
      await apiFetch(`/api/memories/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tier: 'stable', pending_review: false }),
      });
      setList(prev => prev.filter(m => m.id !== id));
    } catch { alert('拒絕失敗'); }
  }

  if (loading) return <div className="mem-empty">載入中…</div>;
  if (err) return <div className="mem-err" style={{ margin: 16 }}>{err}</div>;
  if (list.length === 0) return <div className="mem-empty">沒有待審記憶 👍</div>;

  return (
    <div className="mem-section">
      <p className="mem-hint">以下記憶已升到「永久」層但尚未人工確認，請逐一確認是否保留。</p>
      <div className="mem-list">
        {list.map(m => (
          <div key={m.id} className="mem-card">
            <div className="mem-card-top">
              <span className="mem-badge" style={{ background: TIER_COLOR['evergreen'] }}>永久</span>
              <span className="mem-label" style={{ marginLeft: 'auto' }}>重要度 {m.importance}</span>
            </div>
            <p className="mem-card-content">{m.content}</p>
            <div className="mem-card-meta">{m.created_at ? new Date(m.created_at).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' }) : ''}</div>
            <div className="mem-card-actions">
              <button className="mem-act mem-act--ok" onClick={() => approve(m.id)}>✓ 確認保留</button>
              <button className="mem-act mem-act--del" onClick={() => reject(m.id)}>✗ 降回穩定</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Tab 3 — Open Loops
// ═══════════════════════════════════════════════════════════
function LoopsTab() {
  const [loops, setLoops] = useState<OpenLoop[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async (all: boolean) => {
    setLoading(true); setErr('');
    try {
      const res = await apiFetch(`/api/open-loops?status=${all ? 'all' : 'open'}`);
      const data = await res.json() as { loops?: OpenLoop[]; error?: string };
      if (data.error) { setErr(data.error); return; }
      setLoops(data.loops ?? []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '載入失敗');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(showAll); }, [load, showAll]);

  async function setStatus(id: number, status: string) {
    try {
      await apiFetch(`/api/open-loops/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      load(showAll);
    } catch { alert('更新失敗'); }
  }

  const statusLabel: Record<string, string> = { open: '未解決', resolved: '已解決', dropped: '已放棄' };
  const statusColor: Record<string, string> = { open: '#888', resolved: '#c9953a', dropped: '#555' };

  return (
    <div className="mem-section">
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button
          className={`mem-chip${showAll ? ' mem-chip--on' : ''}`}
          onClick={() => setShowAll(v => !v)}
        >
          {showAll ? '只看未解決' : '顯示全部'}
        </button>
      </div>

      {err && <div className="mem-err">{err}</div>}
      {loading && <div className="mem-empty">載入中…</div>}
      {!loading && !err && loops.length === 0 && (
        <div className="mem-empty">{showAll ? '沒有任何 Loops' : '沒有未解決的 Loops 🎉'}</div>
      )}

      <div className="mem-list">
        {loops.map(l => (
          <div key={l.id} className="mem-card">
            <div className="mem-card-top">
              <span className="mem-badge" style={{ background: statusColor[l.status] ?? '#666' }}>
                {statusLabel[l.status] ?? l.status}
              </span>
              {l.loop_type && (
                <span className="mem-badge" style={{ background: 'rgba(255,255,255,0.08)', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(240,232,216,0.5)' }}>
                  {l.loop_type}
                </span>
              )}
            </div>
            <p className="mem-card-content">{l.content}</p>
            <div className="mem-card-meta">{l.created_at ? new Date(l.created_at).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' }) : ''}</div>
            {l.status === 'open' && (
              <div className="mem-card-actions">
                <button className="mem-act mem-act--ok" onClick={() => setStatus(l.id, 'resolved')}>✓ 已解決</button>
                <button className="mem-act mem-act--del" onClick={() => setStatus(l.id, 'dropped')}>放棄</button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// TXT 解析
// ═══════════════════════════════════════════════════════════
function parseChatTxt(text: string): { role: 'user' | 'assistant'; content: string; createdAt: number }[] {
  const results: { role: 'user' | 'assistant'; content: string; createdAt: number }[] = [];
  const tagRe = /^【(user|assistant)】 \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)/;
  let currentRole: 'user' | 'assistant' | null = null;
  let currentContent: string[] = [];
  let currentTs = 0;
  for (const line of text.split('\n')) {
    const m = line.match(tagRe);
    if (m) {
      if (currentRole) results.push({ role: currentRole, content: currentContent.join('\n').trim(), createdAt: currentTs });
      currentRole = m[1] as 'user' | 'assistant';
      currentTs = new Date(m[2]).getTime();
      currentContent = [m[3]];
    } else if (currentRole !== null) {
      currentContent.push(line);
    }
  }
  if (currentRole) results.push({ role: currentRole, content: currentContent.join('\n').trim(), createdAt: currentTs });
  return results.filter(m => m.content.length > 0);
}

// ═══════════════════════════════════════════════════════════
// Tab 4 — 備份
// ═══════════════════════════════════════════════════════════
function BackupTab() {
  const [memMsg, setMemMsg] = useState('');
  const [chatMsg, setChatMsg] = useState('');
  const [logMsg, setLogMsg] = useState('');
  const memFileRef = useRef<HTMLInputElement>(null);
  const chatFileRef = useRef<HTMLInputElement>(null);
  const logFileRef = useRef<HTMLInputElement>(null);

  async function exportMemories() {
    try {
      const res = await apiFetch('/export/memories');
      if (!res.ok) { alert('匯出失敗'); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `memories_${new Date().toISOString().slice(0, 10)}.json`;
      a.click(); URL.revokeObjectURL(url);
      setMemMsg('記憶已下載！');
    } catch (e) { setMemMsg(e instanceof Error ? e.message : '匯出失敗'); }
  }

  async function importMemories(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text) as { memories?: unknown[] };
      if (!Array.isArray(data.memories)) { setMemMsg('JSON 格式錯誤，找不到 memories 陣列'); return; }
      const res = await apiFetch('/import/memories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memories: data.memories }),
      });
      const result = await res.json() as { imported?: number; skipped?: number; error?: string };
      if (result.error) { setMemMsg(`錯誤：${result.error}`); return; }
      setMemMsg(`匯入完成！新增 ${result.imported} 條，略過 ${result.skipped} 條（重複）`);
    } catch (e) { setMemMsg(e instanceof Error ? e.message : '匯入失敗'); }
    finally { if (memFileRef.current) memFileRef.current.value = ''; }
  }

  async function importChatLog(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = parseChatTxt(text);
      if (parsed.length === 0) { setLogMsg('找不到對話訊息（格式：【user】/【assistant】）'); return; }
      const title = file.name.replace(/\.txt$/i, '');
      const sessionId = crypto.randomUUID();
      const chatId = crypto.randomUUID();
      const now = Date.now();
      const messages = parsed.map(m => ({ id: crypto.randomUUID(), role: m.role, content: m.content, createdAt: m.createdAt }));
      const existing: unknown[] = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) ?? '[]');
      const newChat = { id: chatId, title, sessionId, messages, createdAt: parsed[0]?.createdAt ?? now, updatedAt: now, lastActiveAt: now };
      localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify([newChat, ...existing].slice(0, 50)));
      const apiBase = getApiBase();
      let dbMsg = '';
      if (apiBase) {
        try {
          const res = await apiFetch('/import/chatlog', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, messages: parsed.map(m => ({ role: m.role, content: m.content, created_at: new Date(m.createdAt).toISOString() })) }),
          });
          const result = await res.json() as { imported?: number; error?: string };
          dbMsg = result.error ? `（DB 同步失敗：${result.error}）` : `，DB 同步 ${result.imported ?? '?'} 筆`;
        } catch { dbMsg = '（DB 同步失敗，但對話已存到手機）'; }
      }
      setLogMsg(`匯入完成！${parsed.length} 條訊息，已加到對話列表${dbMsg}`);
    } catch (e) { setLogMsg(e instanceof Error ? e.message : '匯入失敗'); }
    finally { if (logFileRef.current) logFileRef.current.value = ''; }
  }

  function exportChats() {
    try {
      const raw = localStorage.getItem(CHAT_STORAGE_KEY);
      const chats = raw ? JSON.parse(raw) : [];
      const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), chats }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `chats_${new Date().toISOString().slice(0, 10)}.json`;
      a.click(); URL.revokeObjectURL(url);
      setChatMsg('對話已下載！');
    } catch (e) { setChatMsg(e instanceof Error ? e.message : '匯出失敗'); }
  }

  async function importChats(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text) as { chats?: unknown[] };
      if (!Array.isArray(data.chats)) { setChatMsg('JSON 格式錯誤，找不到 chats 陣列'); return; }
      if (!confirm(`確定匯入 ${data.chats.length} 段對話？\n這會合併到現有對話列表（不會刪除現有的）。`)) return;
      const existing: unknown[] = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) ?? '[]');
      const existingIds = new Set((existing as { id?: string }[]).map(c => c.id));
      const newChats = (data.chats as { id?: string }[]).filter(c => c.id && !existingIds.has(c.id));
      localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify([...newChats, ...existing].slice(0, 50)));
      setChatMsg(`匯入完成！新增 ${newChats.length} 段對話`);
    } catch (e) { setChatMsg(e instanceof Error ? e.message : '匯入失敗'); }
    finally { if (chatFileRef.current) chatFileRef.current.value = ''; }
  }

  return (
    <div className="mem-section">
      <div className="mem-backup-block">
        <h3 className="mem-backup-title">🧠 記憶備份</h3>
        <p className="mem-hint">記憶存在後端資料庫（Railway），備份可防止服務遷移時資料遺失。</p>
        <div className="mem-backup-btns">
          <button className="mem-btn mem-btn--dl" onClick={exportMemories}>⬇ 下載記憶 JSON</button>
          <button className="mem-btn" onClick={() => memFileRef.current?.click()}>⬆ 匯入記憶 JSON</button>
        </div>
        <input ref={memFileRef} type="file" accept=".json" style={{ display: 'none' }} onChange={importMemories} />
        {memMsg && <div className="mem-msg">{memMsg}</div>}
      </div>

      <div className="mem-backup-block">
        <h3 className="mem-backup-title">💬 對話備份</h3>
        <p className="mem-hint">對話紀錄只存在手機瀏覽器裡，換手機或清除快取就會消失，記得定期備份！</p>
        <div className="mem-backup-btns">
          <button className="mem-btn mem-btn--dl" onClick={exportChats}>⬇ 下載對話 JSON</button>
          <button className="mem-btn" onClick={() => chatFileRef.current?.click()}>⬆ 匯入對話 JSON</button>
        </div>
        <input ref={chatFileRef} type="file" accept=".json" style={{ display: 'none' }} onChange={importChats} />
        {chatMsg && <div className="mem-msg">{chatMsg}</div>}
      </div>

      <div className="mem-backup-block">
        <h3 className="mem-backup-title">📁 匯入舊對話 TXT</h3>
        <p className="mem-hint">將整理好的對話紀錄 TXT 匯入，可以在聊天窗口繼續接著聊。</p>
        <div className="mem-backup-btns">
          <button className="mem-btn" onClick={() => logFileRef.current?.click()}>⬆ 選擇 TXT 檔案</button>
        </div>
        <input ref={logFileRef} type="file" accept=".txt" style={{ display: 'none' }} onChange={importChatLog} />
        {logMsg && <div className="mem-msg">{logMsg}</div>}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Tab 5 — Snapshot
// ═══════════════════════════════════════════════════════════
function SnapshotTab() {
  const [subTab, setSubTab] = useState<'saved' | 'history'>('saved');
  const [snapshots, setSnapshots] = useState<SnapshotRecord[]>([]);
  const [history, setHistory] = useState<SnapshotRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [recallMap, setRecallMap] = useState<Record<number, 'manual' | 'once'>>(loadRecallMap);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [previewId, setPreviewId] = useState<number | null>(null);
  const [savingId, setSavingId] = useState<number | null>(null);

  const loadSaved = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const res = await apiFetch('/api/snapshots');
      const data = await res.json() as { snapshots?: SnapshotRecord[]; error?: string };
      if (data.error) { setErr(data.error); return; }
      setSnapshots(data.snapshots ?? []);
    } catch (e) { setErr(e instanceof Error ? e.message : '載入失敗'); }
    finally { setLoading(false); }
  }, []);

  const loadHistory = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const res = await apiFetch('/api/checkpoints/all?limit=100');
      const data = await res.json() as { checkpoints?: SnapshotRecord[]; error?: string };
      if (data.error) { setErr(data.error); return; }
      setHistory(data.checkpoints ?? []);
    } catch (e) { setErr(e instanceof Error ? e.message : '載入失敗'); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (subTab === 'saved') loadSaved();
    else loadHistory();
  }, [subTab, loadSaved, loadHistory]);

  function toggleRecall(id: number, mode: 'manual' | 'once') {
    const next = { ...recallMap };
    if (next[id] === mode) { delete next[id]; } else { next[id] = mode; }
    setRecallMap(next);
    saveRecallMap(next);
  }

  async function handleUnsave(id: number) {
    if (!confirm('取消保存後這個 Snapshot 會從清單移除（但壓縮紀錄還在）。確定嗎？')) return;
    try {
      const res = await apiFetch(`/api/snapshots/${id}`, { method: 'DELETE' });
      const data = await res.json() as { error?: string };
      if (data.error) { alert(data.error); return; }
      const next = { ...recallMap }; delete next[id];
      setRecallMap(next); saveRecallMap(next);
      setSnapshots(prev => prev.filter(s => s.id !== id));
    } catch { alert('取消保存失敗'); }
  }

  async function handleEditSave(id: number) {
    if (!editTitle.trim()) return;
    try {
      const res = await apiFetch(`/api/snapshots/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_title: editTitle.trim() }),
      });
      const data = await res.json() as { error?: string };
      if (data.error) { alert(data.error); return; }
      setSnapshots(prev => prev.map(s => s.id === id ? { ...s, card_title: editTitle.trim() } : s));
      setEditingId(null);
    } catch { alert('編輯失敗'); }
  }

  async function handleSaveFromHistory(cp: SnapshotRecord) {
    setSavingId(cp.id);
    try {
      const res = await apiFetch(`/api/snapshots/${cp.id}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_title: null }),
      });
      const data = await res.json() as { error?: string };
      if (data.error) { alert(data.error); return; }
      setHistory(prev => prev.map(c => c.id === cp.id ? { ...c, saved_as_card: true } : c));
    } catch { alert('保存失敗'); }
    finally { setSavingId(null); }
  }

  const checkedIds = Object.keys(recallMap).map(Number);
  const checkedSnapshots = snapshots.filter(s => checkedIds.includes(s.id));
  const estTokens = Math.round(checkedSnapshots.reduce((acc, s) => acc + s.summary_text.length, 0) / 1.5);

  return (
    <div className="mem-section">
      <div style={{ display: 'flex', gap: 6 }}>
        {(['saved', 'history'] as const).map(k => (
          <button
            key={k}
            className={`mem-chip${subTab === k ? ' mem-chip--on' : ''}`}
            onClick={() => setSubTab(k)}
          >
            {k === 'saved' ? '📸 Snapshot' : '📦 壓縮紀錄'}
          </button>
        ))}
      </div>

      {err && <div className="mem-err">{err}</div>}
      {loading && <div className="mem-empty">載入中…</div>}

      {subTab === 'saved' && !loading && (
        <>
          {checkedSnapshots.length > 0 && (
            <div style={{ background: 'rgba(201,149,58,0.1)', border: '1px solid rgba(201,149,58,0.25)', borderRadius: 8, padding: '8px 12px', fontSize: 12, color: '#c9953a' }}>
              目前召回約 +{estTokens} tokens（{checkedSnapshots.length} 張）
            </div>
          )}
          {snapshots.length === 0 && !err && (
            <div className="mem-empty">還沒有 Snapshot，去「壓縮紀錄」tab 保存一個吧</div>
          )}
          <div className="mem-list">
            {snapshots.map(s => (
              <div key={s.id} className="mem-card">
                <div className="mem-card-top">
                  <span className="mem-badge" style={{ background: 'rgba(201,149,58,0.55)' }}>📸 v{s.version}</span>
                  <span style={{ fontSize: 11, opacity: 0.55, marginLeft: 'auto' }}>
                    {new Date(s.created_at).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' })}
                  </span>
                </div>

                {editingId === s.id ? (
                  <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
                    <input
                      className="mem-input"
                      style={{ flex: 1, padding: '5px 10px' }}
                      value={editTitle}
                      onChange={e => setEditTitle(e.target.value)}
                      placeholder="輸入標題…"
                      autoFocus
                    />
                    <button className="mem-act mem-act--ok" onClick={() => handleEditSave(s.id)}>存</button>
                    <button className="mem-act" onClick={() => setEditingId(null)}>取消</button>
                  </div>
                ) : (
                  <p className="mem-card-content" style={{ fontWeight: 600 }}>
                    {s.card_title ?? '（未命名）'}
                  </p>
                )}

                {previewId === s.id && (
                  <pre style={{ fontSize: 11, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '0 0 8px', opacity: 0.8, fontFamily: 'inherit', maxHeight: 200, overflowY: 'auto' }}>
                    {s.summary_text}
                  </pre>
                )}

                <div style={{ display: 'flex', gap: 6, marginBottom: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span className="mem-label">召回：</span>
                  <select
                    value={recallMap[s.id] ?? ''}
                    onChange={e => {
                      const v = e.target.value as 'manual' | 'once' | '';
                      if (!v) { const n = { ...recallMap }; delete n[s.id]; setRecallMap(n); saveRecallMap(n); }
                      else toggleRecall(s.id, v);
                    }}
                    style={{ fontSize: 12, padding: '3px 8px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.15)', background: recallMap[s.id] ? 'rgba(201,149,58,0.15)' : 'rgba(255,255,255,0.05)', color: 'inherit', cursor: 'pointer' }}
                  >
                    <option value="">不召回</option>
                    <option value="manual">手動取消</option>
                    <option value="once">一次</option>
                  </select>
                </div>

                <div className="mem-card-actions">
                  <button className="mem-act" onClick={() => setPreviewId(previewId === s.id ? null : s.id)}>
                    {previewId === s.id ? '收起' : '預覽'}
                  </button>
                  <button className="mem-act" onClick={() => { setEditingId(s.id); setEditTitle(s.card_title ?? ''); }}>編輯</button>
                  <button className="mem-act mem-act--del" onClick={() => handleUnsave(s.id)}>移除</button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {subTab === 'history' && !loading && (
        <>
          {history.length === 0 && !err && (
            <div className="mem-empty">還沒有任何壓縮紀錄</div>
          )}
          <div className="mem-list">
            {history.map(cp => (
              <div key={cp.id} className="mem-card">
                <div className="mem-card-top">
                  <span className="mem-badge" style={{ background: 'rgba(100,100,140,0.5)' }}>📦 v{cp.version}</span>
                  <span style={{ fontSize: 11, opacity: 0.45, fontFamily: 'monospace' }}>{cp.session_id.slice(0, 8)}…</span>
                  <span style={{ fontSize: 11, opacity: 0.55, marginLeft: 'auto' }}>
                    {new Date(cp.created_at).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' })}
                  </span>
                </div>

                {previewId === cp.id && (
                  <pre style={{ fontSize: 11, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '0 0 8px', opacity: 0.8, fontFamily: 'inherit', maxHeight: 200, overflowY: 'auto' }}>
                    {cp.summary_text}
                  </pre>
                )}

                <div className="mem-card-actions">
                  <button className="mem-act" onClick={() => setPreviewId(previewId === cp.id ? null : cp.id)}>
                    {previewId === cp.id ? '收起' : '預覽'}
                  </button>
                  {cp.saved_as_card ? (
                    <button className="mem-act" style={{ opacity: 0.45, cursor: 'default' }} disabled>已存為 Snapshot ✓</button>
                  ) : (
                    <button className="mem-act mem-act--ok" onClick={() => handleSaveFromHistory(cp)} disabled={savingId === cp.id}>
                      {savingId === cp.id ? '保存中…' : '存為 Snapshot'}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Tab 6 — 說明
// ═══════════════════════════════════════════════════════════
function GuideTab() {
  const tierColorMap = { ephemeral: '#888', stable: '#5b8dd9', evergreen: '#c9953a' };
  return (
    <div className="mem-section mem-guide">
      <h2>🔧 第一次設定</h2>
      <p>進入 <strong>聊天</strong>，點右上角 <strong>⋯</strong>，切到 <strong>System</strong> 分頁。</p>
      <ol>
        <li>在「後端 URL」貼上你的 Railway 服務網址（格式：<code>https://xxxx.up.railway.app</code>）</li>
        <li>點「測試連線」，顯示 ✓ 連線正常就成功了</li>
        <li>Model 欄位留空 → 自動用 Railway 環境變數設定的模型</li>
      </ol>

      <h2>🧠 記憶三層系統</h2>
      <p>M 的記憶分三層：</p>
      <div className="mem-guide-tiers">
        {(['ephemeral', 'stable', 'evergreen'] as const).map(t => (
          <div key={t} className="mem-guide-tier" style={{ borderColor: tierColorMap[t] }}>
            <strong style={{ color: tierColorMap[t] }}>
              {t === 'ephemeral' ? '短期（ephemeral）' : t === 'stable' ? '穩定（stable）' : '永久（evergreen）'}
            </strong>
            <p>
              {t === 'ephemeral' && '最近聊天裡提到的日常小事、當下狀態。幾天後會過期，不會永久保存。每次對話會注入最近的短期記憶。'}
              {t === 'stable' && '已確認的事實、習慣、喜好。需要在不同 session 重複提到才會從短期升上來，或手動升級。聊天時用關鍵字搜尋注入。'}
              {t === 'evergreen' && '最核心的關係設定、你最重要的特質。每次對話都會全部注入。需要人工確認才能升到這層（見「待審」）。'}
            </p>
          </div>
        ))}
      </div>

      <h2>⬆ 升級記憶</h2>
      <ul>
        <li><strong>短期 → 穩定</strong>：在記憶卡片按「↑ 升級」，立即生效</li>
        <li><strong>穩定 → 永久</strong>：按「↑ 升級」後進入待審狀態，需在「待審」tab 人工確認才正式生效</li>
        <li>自動升級：同一條記憶在 3 個不同 session 都被確認，系統會自動從短期升到穩定</li>
      </ul>

      <h2>🔒 鎖定記憶</h2>
      <p>按「🔒 鎖」可防止 AI 自動覆蓋或修改這條記憶。適合用在你刻意手動寫入的重要設定。</p>

      <h2>🔄 Open Loops</h2>
      <p>M 在對話中捕捉到的「未完成事項」、「承諾」、「待跟進的問題」會自動記在 Loops。</p>
      <ul>
        <li>例：「明天我要去看醫生」→ M 會記下來，下次對話可以跟進</li>
        <li>事情完成後在「Loops」tab 標記「已解決」</li>
      </ul>

      <h2>💾 備份建議</h2>
      <ul>
        <li><strong>記憶</strong>：Railway 免費方案每月有睡眠限制，遷移前記得備份</li>
        <li><strong>對話</strong>：只存在手機瀏覽器，換手機/清快取會消失，建議每週備份一次</li>
      </ul>

      <h2>❓ 常見問題</h2>
      <div className="mem-faq">
        <div className="mem-faq-q">Q：M 不記得我說的事情了？</div>
        <div className="mem-faq-a">確認後端 URL 有設定且連線正常。記憶提取在每段 session 結束時觸發，session 沒結束不會提取新記憶。</div>
        <div className="mem-faq-q">Q：Railway 服務掛了怎麼辦？</div>
        <div className="mem-faq-a">先到 Railway 控制台確認服務狀態。免費方案有月流量限制，用完會睡眠。</div>
        <div className="mem-faq-q">Q：換手機怎麼辦？</div>
        <div className="mem-faq-a">1. 在「備份」tab 下載對話 JSON，存到 iCloud/Google Drive。2. 新手機設定好後端 URL。3. 匯入對話 JSON。記憶在後端，不用另外遷移。</div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Styles
// ═══════════════════════════════════════════════════════════
const MEM_STYLES = `
.mem-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  height: 52px;
  background: #241e18;
  border-bottom: 1px solid rgba(200,170,120,0.12);
  flex-shrink: 0;
}
.mem-back {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: rgba(255,255,255,0.07);
  border: 1px solid rgba(255,255,255,0.12);
  color: #f0e8d8;
  font-size: 22px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: background 0.15s;
}
.mem-back:active { background: rgba(255,255,255,0.14); }
.mem-title {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #f0e8d8;
}

.mem-nourl {
  background: rgba(217,96,90,0.1);
  border-bottom: 1px solid rgba(217,96,90,0.22);
  color: #e8908a;
  padding: 12px 20px;
  font-size: 14px;
  line-height: 1.5;
  flex-shrink: 0;
}

/* ── Pill Tab Bar ── */
.mem-tabbar {
  display: flex;
  gap: 7px;
  padding: 10px 14px;
  overflow-x: auto;
  scrollbar-width: none;
  background: #1f1a15;
  border-bottom: 1px solid rgba(200,170,120,0.1);
  flex-shrink: 0;
}
.mem-tabbar::-webkit-scrollbar { display: none; }

.mem-tab {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 14px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 500;
  color: rgba(240,232,216,0.45);
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.07);
  white-space: nowrap;
  transition: all 0.18s;
  cursor: pointer;
}
.mem-tab:active { opacity: 0.7; }
.mem-tab--on {
  background: rgba(201,149,58,0.16);
  border-color: rgba(201,149,58,0.35);
  color: #c9953a;
}
.mem-tab-icon { font-size: 14px; line-height: 1; }

.mem-body {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.mem-section {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mem-filters { display: flex; flex-direction: column; gap: 8px; }

.mem-input {
  width: 100%;
  padding: 10px 14px;
  border-radius: 10px;
  border: 1px solid rgba(200,170,120,0.15);
  background: #2e2820;
  color: #f0e8d8;
  font-size: 14px;
  box-sizing: border-box;
  font-family: inherit;
}
.mem-input::placeholder { color: rgba(240,232,216,0.35); }
.mem-input:focus { outline: none; border-color: #c9953a; }

.mem-chips { display: flex; gap: 6px; flex-wrap: wrap; }

.mem-chip {
  padding: 5px 13px;
  border-radius: 20px;
  border: 1px solid rgba(200,170,120,0.15);
  background: #2e2820;
  color: rgba(240,232,216,0.45);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.mem-chip--on {
  background: rgba(201,149,58,0.12);
  border-color: #c9953a;
  color: #c9953a;
}
.mem-chip--accent {
  background: rgba(201,149,58,0.12);
  border-color: rgba(201,149,58,0.35);
  color: #c9953a;
}
.mem-chip:active { opacity: 0.7; }

.mem-pill-btn {
  padding: 6px 16px;
  border-radius: 8px;
  background: rgba(255,255,255,0.12);
  color: #f0e8d8;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid rgba(255,255,255,0.08);
  white-space: nowrap;
  transition: background 0.15s;
}
.mem-pill-btn:active { background: rgba(255,255,255,0.2); }

.mem-label {
  font-size: 12px;
  color: rgba(240,232,216,0.45);
}

.mem-empty {
  padding: 40px 0;
  text-align: center;
  color: rgba(240,232,216,0.35);
  font-size: 14px;
}
.mem-err {
  padding: 10px 14px;
  background: rgba(217,96,90,0.1);
  border: 1px solid rgba(217,96,90,0.22);
  border-radius: 8px;
  color: #e8908a;
  font-size: 13px;
}
.mem-hint {
  font-size: 13px;
  color: rgba(240,232,216,0.45);
  line-height: 1.6;
  margin: 0;
}

.mem-list { display: flex; flex-direction: column; gap: 10px; }

.mem-card {
  background: #241e18;
  border: 1px solid rgba(200,170,120,0.12);
  border-radius: 12px;
  padding: 14px;
}
.mem-card-top {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-bottom: 8px;
}
.mem-badge {
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.02em;
}
.mem-badge--lock { background: #555 !important; }
.mem-badge--review { background: #6a5aad !important; }

.mem-card-content {
  font-size: 14px;
  line-height: 1.65;
  color: #f0e8d8;
  margin: 0 0 6px;
  word-break: break-word;
}
.mem-card-meta {
  font-size: 11px;
  color: rgba(240,232,216,0.35);
  margin-bottom: 10px;
}
.mem-card-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.mem-act {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-radius: 8px;
  border: 1px solid rgba(200,170,120,0.15);
  background: #2e2820;
  color: #f0e8d8;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s;
}
.mem-act:active { opacity: 0.7; }
.mem-act:disabled { opacity: 0.4; cursor: default; }
.mem-act--up  { border-color: #5b8dd9; color: #5b8dd9; }
.mem-act--ok  { border-color: #5ead7e; color: #5ead7e; }
.mem-act--del { border-color: #d9605a; color: #d9605a; }

/* Backup */
.mem-backup-block {
  background: #241e18;
  border: 1px solid rgba(200,170,120,0.12);
  border-radius: 14px;
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.mem-backup-title { font-size: 16px; font-weight: 600; margin: 0; color: #f0e8d8; }
.mem-backup-btns { display: flex; gap: 10px; flex-wrap: wrap; }
.mem-btn {
  flex: 1;
  min-width: 120px;
  padding: 12px 16px;
  border-radius: 10px;
  border: 1px solid rgba(200,170,120,0.15);
  background: #2e2820;
  color: #f0e8d8;
  font-size: 14px;
  cursor: pointer;
  text-align: center;
  transition: background 0.15s;
}
.mem-btn--dl {
  background: rgba(201,149,58,0.1);
  border-color: rgba(201,149,58,0.3);
  color: #c9953a;
}
.mem-btn:active { opacity: 0.7; }
.mem-msg {
  font-size: 13px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(94,173,126,0.1);
  border: 1px solid rgba(94,173,126,0.22);
  color: #5ead7e;
}

/* Guide */
.mem-guide { gap: 0 !important; }
.mem-guide h2 {
  font-size: 15px;
  font-weight: 700;
  color: #c9953a;
  margin: 22px 0 9px;
  padding-bottom: 4px;
  border-bottom: 1px solid rgba(200,170,120,0.12);
}
.mem-guide h2:first-child { margin-top: 0; }
.mem-guide p { font-size: 14px; line-height: 1.7; color: #f0e8d8; margin: 0 0 8px; }
.mem-guide ol, .mem-guide ul { padding-left: 20px; margin: 0 0 8px; }
.mem-guide li { font-size: 14px; line-height: 1.7; color: #f0e8d8; margin-bottom: 4px; }
.mem-guide code {
  background: #2e2820;
  border: 1px solid rgba(200,170,120,0.15);
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  color: #c9953a;
}
.mem-guide strong { color: #f0e8d8; }
.mem-guide-tiers { display: flex; flex-direction: column; gap: 10px; margin-bottom: 8px; }
.mem-guide-tier {
  background: #241e18;
  border: 1px solid;
  border-radius: 12px;
  padding: 14px;
}
.mem-guide-tier strong { display: block; font-size: 14px; margin-bottom: 6px; }
.mem-guide-tier p { margin: 0; font-size: 13px; color: rgba(240,232,216,0.5); line-height: 1.6; }
.mem-faq { display: flex; flex-direction: column; gap: 0; }
.mem-faq-q { font-size: 14px; font-weight: 600; color: #f0e8d8; margin-top: 14px; }
.mem-faq-a { font-size: 13px; color: rgba(240,232,216,0.5); line-height: 1.6; margin-top: 4px; }
`;
