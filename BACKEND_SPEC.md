# myloverM-app 新後端規格筆記

> 這份文件是給 Claude 自己看的追蹤筆記。
> 後端放在 `myloverM-app/backend/`，完全獨立，不依賴舊的 myloverM repo。
> 參考舊後端邏輯，但全部重新寫，不是 copy。

---

## 進度狀態

- [ ] `backend/` 資料夾架構建好
- [ ] `requirements.txt`
- [ ] `database.py` — 所有 DB 表 + 函數
- [ ] `memory_extractor.py` — 記憶提取 / session 摘要 / checkpoint
- [ ] `main.py` — 所有 API 端點 + 背景任務
- [ ] `system_prompt.txt` — 用 SPONTANEOUS_RECALL_SPEC.md 裡的版本
- [ ] 自動向量化排程（新功能）
- [ ] 前端 localStorage key 對齊（SidePanel 和 MemoryPage 用同一個 key）
- [ ] Railway env 填好、測試連線
- [ ] 部署 + 新 app 前端對到新後端網址

---

## 資料夾結構

```
myloverM-app/
├── frontend/         ← 現有前端（不動）
│   └── src/
├── backend/          ← 新後端（全部重寫）
│   ├── main.py
│   ├── database.py
│   ├── memory_extractor.py
│   ├── requirements.txt
│   ├── system_prompt.txt
│   └── Procfile       ← Railway 部署用
└── BACKEND_SPEC.md    ← 本文件
```

---

## 資料庫表（PostgreSQL + pgvector）

### 必須建的表

1. **conversations** — 對話紀錄
   - `id`, `session_id`, `role`, `content`, `model`, `created_at`

2. **memories** — 三層記憶
   - `id`, `content`, `importance`(1-10), `tier`(evergreen/stable/ephemeral)
   - `status`(active/expired/conflicted/superseded)
   - `source_session`, `canonical_key`, `manual_locked`, `pending_review`
   - `replaced_by_id`, `valid_until`, `created_at`, `last_accessed`

3. **memory_confirmations** — 跨 session 確認計數
   - `id`, `memory_id`(FK), `session_id`, `confirmed_at`

4. **session_summaries** — Session 層摘要
   - `id`, `session_id`(unique), `summary`, `mood`, `topic_tags[]`, `msg_count`
   - `created_at`, `updated_at`

5. **open_loops** — 未完事項
   - `id`, `content`, `loop_type`(promise/follow_up), `source_session`
   - `status`(open/resolved/expired), `resolved_at`, `created_at`

6. **session_state** — 提取游標
   - `session_id`(PK), `last_extracted_message_id`, `updated_at`

7. **conversation_checkpoints** — 壓縮快照
   - `id`, `session_id`, `version`, `summary_text`, `covers_until_msg_id`
   - `parent_checkpoint_id`, `is_active`, `token_count`, `created_at`
   - `saved_as_card`, `card_title`, `card_edited_at`

8. **persona_entries** — Worldbook 世界書
   - `id`, `title`, `content`, `keywords`, `position`(0-3)
   - `always_on`, `enabled`, `priority`, `created_at`, `updated_at`

9. **conversation_vectors** — ✨ 新表：對話向量化（自動向量化功能用）
   - `id`, `session_id`, `chunk_text`, `embedding`(vector 1536-dim)
   - `message_ids`(int[]), `created_at`, `vectorized_at`
   - `days_old_at_vectorize`

---

## API 端點清單

### 聊天
- `POST /v1/chat/completions` — 主聊天端點（streaming）
  - 接收：`messages`, `model`, `stream`, `session_id`, `recall_card_ids`, `thinking_budget`
  - 自動：記憶注入、記憶提取（背景）、session 摘要（背景）

### Health
- `GET /` — 健康檢查 + 記憶數量

### 記憶 CRUD
- `GET /api/memories` — 搜尋/列表（支援 search, tier 過濾）
- `POST /api/memories` — 新增
- `PUT /api/memories/{id}` — 修改
- `DELETE /api/memories/{id}` — 刪除
- `POST /api/memories/batch-delete` — 批次刪除
- `POST /api/memories/{id}/upgrade` — 手動升級 tier
- `POST /api/memories/{id}/lock` — 切換手動鎖定

### Import / Export
- `GET /export/memories` — 匯出記憶 JSON
- `POST /import/memories` — 匯入記憶 JSON
- `POST /import/chatlog` — 匯入對話 TXT

### Open Loops
- `GET /api/open-loops` — 列表
- `PATCH /api/open-loops/{id}` — 更新狀態

### Session 摘要
- `GET /api/summaries` — 列表

### Checkpoint / Snapshot
- `POST /api/checkpoint/create` — 建立壓縮
- `GET /api/checkpoints` — 列出（by session_id）
- `GET /api/snapshots` — 列出已儲存的 Snapshot
- `POST /api/snapshots/{id}/save` — 存為 Snapshot
- `PUT /api/snapshots/{id}` — 改標題
- `DELETE /api/snapshots/{id}` — 取消 Snapshot

### Worldbook
- `GET /api/worldbook` — 列表
- `POST /api/worldbook` — 新增
- `PUT /api/worldbook/{id}` — 修改
- `DELETE /api/worldbook/{id}` — 刪除
- `PATCH /api/worldbook/{id}/toggle` — 切換啟用

### ✨ 新：自動向量化
- `GET /api/vectorize/status` — 查看向量化進度
- `POST /api/vectorize/run` — 手動觸發一次
- `GET /api/vectorize/settings` — 查看設定（幾天後向量化）
- `POST /api/vectorize/settings` — 更新設定

---

## 系統提示詞注入順序

```
世界書 pos_0
SYSTEM_PROMPT（人設）
世界書 pos_1
【時間參考】
【核心長期記憶】（evergreen）
【相關穩定記憶】（stable，精準匹配）
【你模糊想起的一些事】← ✨ 自發回憶（隨機加權，3條）
【未完事項】（open loops）
[Snapshot 召回]（recall cards）
【近期短期狀態】（ephemeral）
【對話 Checkpoint】
【最近會話摘要】
世界書 pos_2
世界書 pos_3
# 使用方式
```

---

## 自發回憶功能規格

> 完整規格參考：`myloverM/SPONTANEOUS_RECALL_SPEC.md`

**摘要：**
- 每次對話隨機撈 `MAX_SPONTANEOUS_INJECT`（預設 3）條 stable/evergreen 記憶
- 排除已精準匹配的記憶（避免重複）
- 用 `importance` 加權隨機（importance 高的更容易出現）
- 注入為 `【你模糊想起的一些事】` section
- 提示詞告訴 M：「話題自然沾到邊才順嘴說，不要刻意」
- `system_prompt.txt` 加「記憶浮現」模組（用 SPEC 裡的版本）
- env var：`MAX_SPONTANEOUS_INJECT=3`

**DB 函數：**
```python
async def get_random_memories(limit, tiers, exclude_ids) -> list[dict]
# ORDER BY RANDOM() * (importance + 1) DESC
```

---

## ✨ 新功能：定時自動向量化對話

**目標：** 對話發生 N 天後，自動切分成小段、計算向量、存進 DB，供 RAG 搜尋。

**設定：**
- `VECTORIZE_AFTER_DAYS=7`（預設 7 天，可調）
- `VECTORIZE_CHUNK_SIZE=5`（每幾條訊息一段）
- `EMBEDDING_MODEL=text-embedding-3-small`

**流程：**
1. 背景任務每天跑一次（或啟動時跑）
2. 找出 `created_at < NOW() - VECTORIZE_AFTER_DAYS` 且尚未向量化的 session
3. 把該 session 的訊息切成小段
4. 每段呼叫 OpenAI embedding API 算向量
5. 存進 `conversation_vectors` 表

**RAG 使用：**
- 每次對話時，用當前用戶訊息向量搜尋 `conversation_vectors`
- 最相似的段落注入為 `【相關對話片段】`
- 注入位置：在 `【相關穩定記憶】` 之後

---

## Environment Variables 必填清單

```
DATABASE_URL=           ← Railway PostgreSQL 自動填
ANTHROPIC_API_KEY=      ← Anthropic API
OPENAI_API_KEY=         ← 向量化用（text-embedding-3-small）
MEMORY_ENABLED=true
DEFAULT_MODEL=claude-sonnet-4-5-20251001
CORS_ORIGINS=https://annimaddox.github.io
TAVILY_API_KEY=         ← 聯網搜尋（選填）
MAX_SPONTANEOUS_INJECT=3
VECTORIZE_AFTER_DAYS=7
```

---

## 前端修改（myloverM-app/src/）

- [ ] `src/pages/MemoryPage.tsx`：把 `BACKEND_URL_KEY = 'myloverM-api-url'` 改成 import `API_BASE_LS_KEY` from `'../api'`
- [ ] `src/pages/WorldbookPage.tsx`：同上

---

## 注意事項

- `get_random_memories` 的 SQL 用 PostgreSQL `RANDOM()` 語法
- 記憶注入格式不帶日期/tier 標記，純 content
- Spontaneous 數量不計入現有 total_count log，用獨立 log 行
- 新後端 Railway 跟舊的完全獨立，不共用 DB
- 舊的 myloverM 繼續跑，不動
