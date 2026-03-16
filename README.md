# myloverM-app

伴侶 AI 聊天前端 + 後端，個人部署，單人使用。

---

## 架構

```
myloverM-app/
  src/          ← React + Vite + TypeScript 前端
  backend/      ← Python FastAPI 後端
```

- **前端**：React + Vite + TypeScript，部署到 Vercel
- **後端**：Python FastAPI，部署到 Railway（PostgreSQL + pgvector）

---

## Railway 後端 — 環境變數清單

| 變數名 | 必填 | 說明 |
|--------|------|------|
| `DATABASE_URL` | ✅ | Railway Postgres 自動填入 |
| `OPENAI_API_KEY` | ✅ | 用於聊天 / embedding / 摘要 |
| `ANTHROPIC_API_KEY` | 選填 | 用 Claude 當聊天模型才需要 |
| `GEMINI_API_KEY` | 選填 | 用 Gemini 當聊天模型才需要 |
| `API_SECRET_KEY` | 建議填 | Bearer token 驗證，防外人呼叫 API |
| `PORT` | 選填 | 預設 8000 |
| `CORS_ORIGINS` | 建議填 | 前端網址，例如 `https://annimaddox.github.io` |
| `DEFAULT_MODEL` | 選填 | 預設 `gpt-4o` |
| `MEMORY_ENABLED` | 選填 | 預設 `true` |
| `MEMORY_EXTRACT_INTERVAL` | 選填 | 幾輪提取一次記憶，預設 `5` |
| `CHECKPOINT_MODEL` | 選填 | 壓縮/摘要模型，預設 `gpt-4o` |
| `MEMORY_MODEL` | 選填 | 記憶提取模型，預設 `gpt-4o` |
| `VECTORIZE_AFTER_DAYS` | 選填 | 幾天前的對話才向量化，預設 `7` |
| `VECTORIZE_CHUNK_SIZE` | 選填 | 每段幾條訊息，預設 `8` |
| `VECTORIZE_CHUNK_OVERLAP` | 選填 | 相鄰段重疊幾條，預設 `3` |
| `CONV_EMBEDDING_MODEL` | 選填 | 對話向量化模型，預設 `text-embedding-3-small` |
| `MAX_CONV_RAG_INJECT` | 選填 | 每次注入幾段舊對話，預設 `3` |

---

## 前端 — SidePanel 設定（存在 localStorage）

| 欄位 | 說明 |
|------|------|
| 後端 URL | Railway 服務網址（例如 `https://xxx.railway.app`）|
| 密鑰 | 對應 Railway 的 `API_SECRET_KEY` |
| 上下文輪數 | 每次送幾輪對話給後端 |
| Temperature / Top-P | 模型參數 |

**Model Routing**（SidePanel → 齒輪圖示）：
- 聊天 / 摘要壓縮 / 提取 / Embedding（對話向量化）
- 各自可選不同公司 + 模型
- Embedding 只支援 OpenAI embedding 系列（`text-embedding-3-small` / `text-embedding-3-large`）
- 選好後自動同步到後端，後端重啟後回 Railway env var 的值

---

## 主要功能

### 聊天
- SSE streaming
- 圖片附件（base64）
- 引用回覆

### 記憶系統
- 三層：evergreen / stable / ephemeral
- 提取：每 N 輪跑一次 + session 結束強制一次
- 升級：ephemeral → stable（distinct session 確認 ≥ 3）→ evergreen（人工）
- Memory Bank 頁面：手動管理條目，支援 always_load、分類、標籤
- Worldbook 頁面：靜態背景知識，每次都注入 system prompt

### Checkpoint（壓縮）
- 點 header 壓縮按鈕 → 後端生成 M 視角摘要 → 注入下次 system prompt
- SidePanel Checkpoint tab：列出本 session 所有版本，可存成 Snapshot
- Snapshot 頁面：管理跨 session 的召回卡片（手動取消 / 一次性）

### 對話向量化（RAG）
- 超過 N 天的舊對話，滑動窗口切 chunk（8條 + 3重疊）→ embedding → pgvector
- 每次聊天時，把 user message 向量化 → 搜尋相近舊片段 → 注入 system prompt
- 背景任務每 24 小時自動跑一次

---

## 本機開發

### 前端
```bash
npm install
npm run dev
```

### 後端
```bash
cd backend
pip install -r requirements.txt
DATABASE_URL=xxx OPENAI_API_KEY=xxx python main.py
```

---

## 部署

### 後端（Railway）
Push 到 GitHub → Railway 自動偵測 `backend/` → 用 `Procfile` 或 `railway.toml` 啟動

### 前端（Vercel）
```bash
vercel --prod --yes
```
（git push 後 Vercel 的自動 build 有時會卡，直接 CLI 部署最穩）
