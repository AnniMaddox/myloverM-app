"""
AI Memory Gateway — 带记忆系统的 LLM 转发网关
=============================================
让你的 AI 拥有长期记忆。

工作原理：
1. 接收客户端（Kelivo / ChatBox / 任何 OpenAI 兼容客户端）的消息
2. 自动搜索数据库中的相关记忆，注入 system prompt
3. 转发给 LLM API（支持 OpenRouter / OpenAI / 任何兼容接口）
4. 后台自动存储对话 + 用 AI 提取新记忆

环境变量 MEMORY_ENABLED=false 时退化为纯转发网关（第一阶段）。
"""

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from database import (
    ACTIVE_STATUS,
    MEMORY_TIER_EPHEMERAL,
    MEMORY_TIER_EVERGREEN,
    MEMORY_TIER_STABLE,
    add_memory_confirmation,
    close_pool,
    count_distinct_confirmations,
    count_unextracted_messages,
    create_open_loop,
    create_memory_bank_item,
    deactivate_old_checkpoints,
    delete_memory_bank_item,
    delete_memories_batch,
    delete_memory,
    expire_old_memories,
    expire_old_open_loops,
    get_active_checkpoint,
    get_always_load_items,
    get_all_persona_entries,
    get_persona_entry,
    create_persona_entry,
    update_persona_entry,
    delete_persona_entry,
    toggle_persona_entry,
    reorder_persona_entries,
    get_enabled_persona_entries,
    get_active_memory_briefs,
    get_all_memories,
    get_all_memories_count,
    get_all_memories_detail,
    get_first_confirmation_time,
    get_last_session_message_id,
    get_latest_summary_time,
    get_memory_bank_item,
    get_memory_bank_items,
    get_memories_by_tier,
    get_memory,
    get_random_memories,
    get_messages_after_checkpoint,
    get_messages_for_compression,
    get_open_loops,
    get_pool,
    get_recent_messages_for_context,
    get_recent_session_summaries,
    get_session_messages,
    get_stale_unsummarized_sessions,
    get_unextracted_messages,
    init_tables,
    insert_checkpoint,
    resolve_open_loops,
    save_memory,
    save_message,
    search_memory_bank_by_tags,
    search_memory_bank_by_vector,
    search_memories,
    update_extract_cursor,
    update_embedding,
    update_memory_bank_item,
    update_memory,
    upsert_session_summary,
    get_sessions_to_vectorize,
    get_messages_for_session_vectorize,
    save_conversation_vector,
    search_conversation_vectors,
    count_vectorize_status,
)
from llm_router import (
    ProviderRoute,
    create_chat_completion_with_route,
    get_default_routes,
    get_effective_routes,
    get_provider_statuses,
    list_models_for_provider,
    normalize_provider,
    route_to_dict,
    stream_chat_with_route,
    uses_openai_max_completion_tokens,
)
from memory_extractor import (
    extract_memory_actions,
    generate_checkpoint_summary,
    score_memories,
    summarize_session,
)

# ============================================================
# 配置项 —— 全部从环境变量读取，部署时在云平台面板里设置
# ============================================================

# 你的 API Key（OpenRouter / OpenAI / 其他兼容服务）
API_KEY = os.getenv("API_KEY", "")

# API 地址（改这个就能切换不同的 LLM 服务商）
# OpenRouter: https://openrouter.ai/api/v1/chat/completions
# OpenAI:     https://api.openai.com/v1/chat/completions
# 本地 Ollama: http://localhost:11434/v1/chat/completions
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 默认模型（如果客户端没指定就用这个）
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "anthropic/claude-sonnet-4")

# 网关端口
PORT = int(os.getenv("PORT", "8080"))

# 记忆系统开关（数据库出问题时可以临时关掉）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "false").lower() == "true"

# 每次注入的最大记忆条数
MAX_MEMORIES_INJECT = int(os.getenv("MAX_MEMORIES_INJECT", "15"))
MAX_EVERGREEN_INJECT = int(os.getenv("MAX_EVERGREEN_INJECT", "12"))
MAX_STABLE_INJECT = int(os.getenv("MAX_STABLE_INJECT", str(MAX_MEMORIES_INJECT)))
MAX_EPHEMERAL_INJECT = int(os.getenv("MAX_EPHEMERAL_INJECT", "8"))
MAX_SUMMARIES_INJECT = int(os.getenv("MAX_SUMMARIES_INJECT", "2"))
MAX_OPEN_LOOPS_INJECT = int(os.getenv("MAX_OPEN_LOOPS_INJECT", "8"))
MAX_BANK_ALWAYS_INJECT = int(os.getenv("MAX_BANK_ALWAYS_INJECT", "10"))
MAX_BANK_ALWAYS_CHARS = int(os.getenv("MAX_BANK_ALWAYS_CHARS", "2000"))
MAX_BANK_ONDEMAND_INJECT = int(os.getenv("MAX_BANK_ONDEMAND_INJECT", "3"))
MAX_BANK_ONDEMAND_CHARS = int(os.getenv("MAX_BANK_ONDEMAND_CHARS", "3000"))

# 记忆提取间隔（0 = 禁用自动提取，1 = 每轮提取，N = 每 N 轮提取一次）
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "1"))

# recent raw turns 保底（不管 slider 設多低，至少送這麼多則給模型）
MIN_RECENT_RAW_TURNS = int(os.getenv("MIN_RECENT_RAW_TURNS", "6"))
MAX_SPONTANEOUS_INJECT = int(os.getenv("MAX_SPONTANEOUS_INJECT", "3"))

# 时区偏移（小时），用于记忆注入时的日期显示，默认 UTC+8
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "30"))
MIN_MESSAGES_FOR_SUMMARY = int(os.getenv("MIN_MESSAGES_FOR_SUMMARY", "4"))
EPHEMERAL_CONFIRMATIONS_TO_STABLE = int(os.getenv("EPHEMERAL_CONFIRMATIONS_TO_STABLE", "3"))
STABLE_CONFIRMATIONS_TO_REVIEW = int(os.getenv("STABLE_CONFIRMATIONS_TO_REVIEW", "5"))
STABLE_REVIEW_DAYS = int(os.getenv("STABLE_REVIEW_DAYS", "14"))

# （輪次計數器已移除，extraction 改由 DB cursor 追蹤）

# 额外的请求头（有些 API 需要，比如 OpenRouter 需要 Referer）
EXTRA_REFERER = os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local")
EXTRA_TITLE = os.getenv("EXTRA_TITLE", "AI Memory Gateway")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
VECTOR_FALLBACK_ENABLED = os.getenv("VECTOR_FALLBACK_ENABLED", "true").lower() == "true"
CONV_VECTORIZE_AFTER_DAYS = int(os.getenv("VECTORIZE_AFTER_DAYS", "7"))
CONV_CHUNK_SIZE = int(os.getenv("VECTORIZE_CHUNK_SIZE", "8"))
CONV_CHUNK_OVERLAP = int(os.getenv("VECTORIZE_CHUNK_OVERLAP", "3"))
_CONV_EMBEDDING_MODEL_ENV = os.getenv("CONV_EMBEDDING_MODEL", "text-embedding-3-small")
_vectorize_settings: dict = {"embedding_model": _CONV_EMBEDDING_MODEL_ENV}
MAX_CONV_RAG_INJECT = int(os.getenv("MAX_CONV_RAG_INJECT", "3"))
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_MAX_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS", "5"))

META_BLACKLIST = [
    "记忆库",
    "记忆系统",
    "检索",
    "没有被记录",
    "没有被提取",
    "记忆遗漏",
    "尚未被记录",
    "写入不完整",
    "检索功能",
    "系统没有返回",
    "关键词匹配",
    "语义匹配",
    "语义检索",
    "阈值",
    "数据库",
    "seed",
    "导入",
    "部署",
    "bug",
    "debug",
    "端口",
    "网关",
]
SKIPPED_SUMMARY_PREFIX = "【短会话略过】"
_tag_lexicon: dict[str, str] = {}

# ============================================================
# 聯網搜尋（Tavily）
# ============================================================

_SEARCH_JUDGE_PROMPT = """\
你是一個決策助手。判斷使用者的訊息是否需要聯網搜尋才能回答（例如：最新電影/歌曲/新聞/時事/近期資訊等），還是只需根據對話就能回答。

規則：
- 若需要搜尋，輸出 JSON：{{"search": true, "query": "搜尋關鍵字（繁體中文）"}}
- 若不需要，輸出 JSON：{{"search": false}}
- query 應該是簡潔的搜尋關鍵字，不含問句語氣
- 只輸出 JSON，不要說明

使用者訊息：
{msg}"""


async def _should_web_search(msg: str, route: ProviderRoute | None) -> str | None:
    """讓 LLM 判斷是否需要聯網搜尋，回傳搜尋 query（或 None）。"""
    if not TAVILY_API_KEY:
        return None
    msg_stripped = msg.strip()
    if not msg_stripped:
        return None
    try:
        prompt = _SEARCH_JUDGE_PROMPT.format(msg=msg_stripped)
        raw = ""
        if route is not None:
            result = await create_chat_completion_with_route(
                route,
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=64,
            )
            # 從各 provider 回應中取出文字
            if isinstance(result, dict):
                choices = result.get("choices") or []
                if choices:
                    raw = (choices[0].get("message") or {}).get("content") or ""
                else:
                    # Anthropic format
                    content = result.get("content") or []
                    if content:
                        raw = (content[0].get("text") or "") if isinstance(content[0], dict) else ""
        else:
            # Legacy path：直接呼叫 API_BASE_URL
            legacy_headers = {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            }
            if "openrouter" in API_BASE_URL:
                legacy_headers["HTTP-Referer"] = EXTRA_REFERER
                legacy_headers["X-Title"] = EXTRA_TITLE
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    API_BASE_URL,
                    headers=legacy_headers,
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.0,
                        "max_tokens": 64,
                    },
                )
                resp.raise_for_status()
                data_raw = resp.json()
                raw = ((data_raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        raw = raw.strip()
        # 嘗試解析 JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        if data.get("search") and data.get("query"):
            return str(data["query"]).strip()
    except Exception:
        pass
    return None


async def _tavily_search(query: str) -> str:
    """呼叫 Tavily 搜尋，回傳格式化的搜尋結果文字。"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": TAVILY_MAX_RESULTS,
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return f"（搜尋失敗：{exc}）"

    lines: list[str] = []
    if data.get("answer"):
        lines.append(f"摘要：{data['answer']}")
        lines.append("")

    results = data.get("results") or []
    for i, r in enumerate(results[:TAVILY_MAX_RESULTS], 1):
        title = r.get("title", "")
        content = r.get("content", "")
        url = r.get("url", "")
        lines.append(f"{i}. {title}")
        if content:
            lines.append(f"   {content[:300]}")
        if url:
            lines.append(f"   來源：{url}")
        lines.append("")

    return "\n".join(lines).strip() if lines else "（無搜尋結果）"


def serialize_model_routes(routes: dict[str, ProviderRoute] | None) -> dict[str, dict[str, str] | None]:
    route_map = routes or {}
    return {
        "chat": route_to_dict(route_map.get("chat")),
        "summary": route_to_dict(route_map.get("summary")),
        "extraction": route_to_dict(route_map.get("extraction")),
    }


# ============================================================
# 人设加载
# ============================================================

def load_system_prompt():
    """从 system_prompt.txt 文件读取人设内容"""
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except FileNotFoundError:
        pass
    print("ℹ️  未找到 system_prompt.txt 或文件为空，将不注入 system prompt")
    return ""


SYSTEM_PROMPT = load_system_prompt()
if SYSTEM_PROMPT:
    print(f"✅ 人设已加载，长度：{len(SYSTEM_PROMPT)} 字符")
else:
    print("ℹ️  无人设，纯转发模式")


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时断开连接"""
    if MEMORY_ENABLED:
        try:
            await init_tables()
            await refresh_tag_lexicon()
            count = await get_all_memories_count()
            print(f"✅ 记忆系统已启动，当前记忆数量：{count}")
            # 啟動時跑一次對話向量化，然後每日排程
            asyncio.create_task(run_vectorize_job())
            asyncio.create_task(_daily_vectorize_loop())
        except Exception as e:
            print(f"⚠️  数据库初始化失败: {e}")
            print("⚠️  记忆系统将不可用，但网关仍可正常转发")
    else:
        print("ℹ️  记忆系统已关闭（设置 MEMORY_ENABLED=true 开启）")

    yield
    
    if MEMORY_ENABLED:
        await close_pool()


app = FastAPI(title="AI Memory Gateway", version="2.0.0", lifespan=lifespan)

raw = os.getenv("CORS_ORIGINS", "")
origins = [x.strip() for x in raw.split(",") if x.strip()]

app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
)

_API_SECRET = os.getenv("API_SECRET_KEY", "").strip()

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # OPTIONS preflight 和 GET / 放行，讓 CORSMiddleware 處理
    if request.method == "OPTIONS" or (request.method == "GET" and request.url.path == "/"):
        return await call_next(request)
    if _API_SECRET:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:].strip() != _API_SECRET:
            origin = request.headers.get("origin", "")
            cors_headers = {"access-control-allow-origin": origin} if origin else {}
            return JSONResponse(status_code=401, content={"error": "Unauthorized"}, headers=cors_headers)
    return await call_next(request)


# ============================================================
# 记忆注入
# ============================================================

def row_get(row: Any, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)


def format_local_datetime(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_dt = value.astimezone(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)
    return local_dt.strftime("%Y-%m-%d %H:%M")


def format_relative_time(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value.astimezone(timezone.utc)
    seconds = int(max(delta.total_seconds(), 0))
    if seconds < 3600:
        return f"{max(seconds // 60, 1)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def normalize_messages_for_memory(messages: list[dict]) -> list[dict]:
    normalized = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        content = extract_text_from_content(msg.get("content"))
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def normalize_text_key(text: str) -> str:
    return "".join(text.lower().split())


def normalize_text(text: str) -> str:
    return normalize_text_key(text)


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_openai_token_limit(body: dict[str, Any], model: str, *, api_base_url: str) -> dict[str, Any]:
    lowered_base = api_base_url.strip().lower()
    if "api.openai.com" not in lowered_base:
        return body
    if not uses_openai_max_completion_tokens(model):
        return body
    if "max_completion_tokens" in body:
        body.pop("max_tokens", None)
        return body
    max_tokens = body.pop("max_tokens", None)
    if max_tokens is not None:
        body["max_completion_tokens"] = max_tokens
    return body


def parse_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.replace("，", ",").split(",")]
    else:
        items = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def parse_bool_value(raw: Any, default: bool | None = None) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


async def refresh_tag_lexicon():
    items = await get_memory_bank_items(enabled_only=True)
    global _tag_lexicon
    next_lexicon: dict[str, str] = {}
    for item in items:
        for tag in item.get("tags") or []:
            normalized = normalize_text(str(tag or ""))
            if normalized and normalized not in next_lexicon:
                next_lexicon[normalized] = str(tag)
    _tag_lexicon = next_lexicon


def match_tags_in_text(text: str) -> list[str]:
    normalized = normalize_text(text)
    if not normalized or not _tag_lexicon:
        return []

    matched = [
        original
        for normalized_tag, original in sorted(_tag_lexicon.items(), key=lambda item: len(item[0]), reverse=True)
        if normalized_tag in normalized
    ]
    return matched


async def compute_embedding(text: str) -> list[float]:
    api_key = OPENAI_API_KEY or API_KEY
    if not api_key:
        raise ValueError("OPENAI_API_KEY 未设置，无法计算 embedding")

    payload = {
        "model": EMBEDDING_MODEL,
        "input": text[:8000],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{OPENAI_API_BASE}/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code != 200:
        raise ValueError(f"Embedding API error {response.status_code}: {response.text[:400]}")
    data = response.json()
    embedding = data.get("data", [{}])[0].get("embedding")
    if not isinstance(embedding, list):
        raise ValueError("Embedding API returned empty embedding")
    return [float(v) for v in embedding]


async def compute_embedding_conv(text: str) -> list[float]:
    """用 CONV_EMBEDDING_MODEL（text-embedding-3-small）計算向量，供對話 RAG 用"""
    api_key = OPENAI_API_KEY or API_KEY
    if not api_key:
        raise ValueError("OPENAI_API_KEY 未設定")
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{OPENAI_API_BASE}/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": _vectorize_settings["embedding_model"], "input": text[:8000]},
        )
    if response.status_code != 200:
        raise ValueError(f"Embedding API error {response.status_code}: {response.text[:200]}")
    data = response.json()
    embedding = data.get("data", [{}])[0].get("embedding")
    if not isinstance(embedding, list):
        raise ValueError("Embedding API returned empty result")
    return [float(v) for v in embedding]


def _chunk_messages(
    messages: list[dict],
    chunk_size: int,
    overlap: int = 0,
) -> list[tuple[list[int], str]]:
    """滑動窗口切 chunk，相鄰 chunk 共用 overlap 條，回傳 [(message_ids, chunk_text)]"""
    if not messages:
        return []
    step = max(1, chunk_size - overlap)
    chunks = []
    seen: set[tuple[int, ...]] = set()
    for i in range(0, len(messages), step):
        batch = messages[i : i + chunk_size]
        ids = tuple(int(m["id"]) for m in batch)
        if ids in seen:
            continue
        seen.add(ids)
        lines = []
        for m in batch:
            label = "Anni" if m["role"] == "user" else "M"
            content = str(m.get("content", "")).strip()[:500]
            if content:
                lines.append(f"[{label}] {content}")
        text = "\n".join(lines)
        if text.strip():
            chunks.append((list(ids), text))
    return chunks


async def _vectorize_session(session_id: str) -> int:
    """向量化一個 session，回傳儲存的 chunk 數"""
    messages = await get_messages_for_session_vectorize(session_id)
    if not messages:
        return 0
    oldest = messages[0].get("created_at")
    if oldest and hasattr(oldest, "tzinfo"):
        from datetime import timezone as _tz
        days_old = (datetime.now(_tz.utc) - oldest).total_seconds() / 86400
    else:
        days_old = float(CONV_VECTORIZE_AFTER_DAYS)
    chunks = _chunk_messages(messages, CONV_CHUNK_SIZE, overlap=CONV_CHUNK_OVERLAP)
    saved = 0
    for msg_ids, text in chunks:
        try:
            embedding = await compute_embedding_conv(text)
            await save_conversation_vector(session_id, text, embedding, msg_ids, days_old)
            saved += 1
        except Exception as e:
            print(f"⚠️  向量化 chunk 失敗 (session={session_id[:8]}): {e}")
    return saved


async def run_vectorize_job() -> dict:
    """跑一次向量化任務，回傳統計"""
    if not (OPENAI_API_KEY or API_KEY):
        print("⚠️  向量化跳過：OPENAI_API_KEY 未設定")
        return {"error": "OPENAI_API_KEY 未設定", "sessions": 0, "chunks": 0}
    sessions = await get_sessions_to_vectorize(CONV_VECTORIZE_AFTER_DAYS)
    if not sessions:
        return {"sessions": 0, "chunks": 0}
    total_chunks = 0
    for sid in sessions:
        n = await _vectorize_session(sid)
        total_chunks += n
        if n:
            print(f"✅ 向量化 {sid[:8]}… → {n} chunks")
    print(f"🔢 向量化完成：{len(sessions)} sessions, {total_chunks} chunks")
    return {"sessions": len(sessions), "chunks": total_chunks}


async def _daily_vectorize_loop():
    """每 24 小時跑一次向量化"""
    while True:
        await asyncio.sleep(86400)
        try:
            await run_vectorize_job()
        except Exception as e:
            print(f"⚠️  每日向量化失敗: {e}")


async def compute_and_store_embedding(item_id: int, content: str):
    try:
        vec = await compute_embedding(content)
        await update_embedding(item_id, vec)
    except Exception as exc:
        print(f"⚠️  Embedding failed for memory_bank #{item_id}: {exc}")


def build_memory_bank_payload(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if not partial or "title" in body:
        title = str(body.get("title", "") or "").strip()
        if not partial and not title:
            raise ValueError("title 不能為空")
        if title or "title" in body:
            payload["title"] = title

    if not partial or "content" in body:
        content = str(body.get("content", "") or "").strip()
        if not partial and not content:
            raise ValueError("content 不能為空")
        if content or "content" in body:
            payload["content"] = content
            payload["content_hash"] = compute_content_hash(content)

    if not partial or "category" in body:
        payload["category"] = str(body.get("category", "general") or "general").strip() or "general"

    if "tags" in body or not partial:
        payload["tags"] = parse_tags(body.get("tags", []))

    always_load = parse_bool_value(body.get("always_load"), None if partial else False)
    if always_load is not None:
        payload["always_load"] = always_load

    enabled = parse_bool_value(body.get("enabled"), None if partial else True)
    if enabled is not None:
        payload["enabled"] = enabled

    if "sort_order" in body or not partial:
        raw_sort = body.get("sort_order", 0)
        try:
            payload["sort_order"] = int(raw_sort)
        except (TypeError, ValueError):
            payload["sort_order"] = 0

    if "source_ref" in body or not partial:
        source_ref = str(body.get("source_ref", "") or "").strip()
        payload["source_ref"] = source_ref or None

    if "notes" in body or not partial:
        notes = str(body.get("notes", "") or "").strip()
        payload["notes"] = notes or None

    return payload


def is_meta_memory(content: str) -> bool:
    return any(keyword in content for keyword in META_BLACKLIST)


def build_valid_until(valid_until_days: Any) -> datetime | None:
    try:
        days = int(valid_until_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def format_memory_line(memory: Any, include_date: bool = False) -> str:
    content = str(row_get(memory, "content", "") or "").strip()
    if not content:
        return ""
    if not include_date:
        return f"- {content}"
    created_at = row_get(memory, "created_at")
    if not created_at:
        return f"- {content}"
    date_str = format_local_datetime(created_at)
    return f"- [{date_str}] {content}"


def build_memory_lookup(memories: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_content: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for mem in memories:
        content = str(row_get(mem, "content", "") or "").strip()
        canonical_key = str(row_get(mem, "canonical_key", "") or "").strip()
        if content:
            by_content.setdefault(normalize_text_key(content), mem)
        if canonical_key:
            by_key.setdefault(canonical_key, mem)
    return by_content, by_key


async def maybe_promote_memory(memory_id: int):
    memory = await get_memory(memory_id)
    if not memory:
        return
    if row_get(memory, "status") != ACTIVE_STATUS or row_get(memory, "manual_locked", False):
        return

    confirmations = await count_distinct_confirmations(memory_id)
    tier = row_get(memory, "tier")

    # 情緒狀態護欄：canonical_key 以 "emotion:" 開頭的記憶不升級，只延壽
    canonical_key = row_get(memory, "canonical_key") or ""
    if tier == MEMORY_TIER_EPHEMERAL and canonical_key.startswith("emotion:"):
        new_valid_until = datetime.now(timezone.utc) + timedelta(days=14)
        await update_memory(memory_id, valid_until=new_valid_until)
        print(f"💛 情緒記憶 #{memory_id} 已延壽至 {new_valid_until.date()}（不升級）")
        return

    if tier == MEMORY_TIER_EPHEMERAL and confirmations >= EPHEMERAL_CONFIRMATIONS_TO_STABLE:
        await update_memory(memory_id, tier=MEMORY_TIER_STABLE)
        print(f"⬆️  記憶 #{memory_id} 已從 ephemeral 升級為 stable")
        memory = await get_memory(memory_id)
        tier = row_get(memory, "tier")

    if tier != MEMORY_TIER_STABLE or row_get(memory, "pending_review", False):
        return

    if confirmations < STABLE_CONFIRMATIONS_TO_REVIEW:
        return

    first_confirmation = await get_first_confirmation_time(memory_id)
    if not first_confirmation:
        return
    if datetime.now(timezone.utc) - first_confirmation < timedelta(days=STABLE_REVIEW_DAYS):
        return

    await update_memory(memory_id, pending_review=True)
    print(f"🪄  stable 記憶 #{memory_id} 已進入 evergreen review queue")


async def save_action_memory(action: dict, source_session: str) -> int | None:
    return await save_memory(
        content=action["content"],
        importance=action.get("importance", 5),
        source_session=source_session,
        tier=action.get("tier", MEMORY_TIER_EPHEMERAL),
        status=ACTIVE_STATUS,
        canonical_key=action.get("canonical_key"),
        valid_until=build_valid_until(action.get("valid_until_days")),
    )


async def handle_memory_conflict(action: dict, source_session: str):
    target_id = action.get("memory_id")
    target = await get_memory(target_id)
    if not target:
        await save_action_memory(action, source_session)
        return

    if row_get(target, "manual_locked", False):
        await save_action_memory(action, source_session)
        print(f"🔒  记忆 #{target_id} 已锁定，冲突内容作为新记忆追加")
        return

    new_memory_id = await save_action_memory(action, source_session)
    if not new_memory_id:
        return

    target_tier = row_get(target, "tier")
    if target_tier == MEMORY_TIER_EPHEMERAL:
        await update_memory(target_id, status="superseded", replaced_by_id=new_memory_id)
        print(f"♻️  临时记忆 #{target_id} 被新记忆 #{new_memory_id} 取代")
    else:
        await update_memory(target_id, status="conflicted")
        print(f"⚠️  记忆 #{target_id} 被标记为 conflicted，新版本为 #{new_memory_id}")


async def summarize_stale_sessions(limit: int = 3, route: ProviderRoute | None = None):
    stale_sessions = await get_stale_unsummarized_sessions(
        idle_minutes=SESSION_IDLE_MINUTES,
        limit=limit,
    )
    for row in stale_sessions:
        session_id = row_get(row, "session_id")
        msg_count = int(row_get(row, "msg_count", 0) or 0)
        if not session_id:
            continue
        if msg_count < MIN_MESSAGES_FOR_SUMMARY:
            await upsert_session_summary(
                session_id,
                f"{SKIPPED_SUMMARY_PREFIX} {msg_count} 条消息",
                mood=None,
                topic_tags=["short-session"],
                msg_count=msg_count,
            )
            continue

        messages = await get_session_messages(session_id)
        normalized_messages = [
            {"role": row_get(message, "role", ""), "content": row_get(message, "content", "")}
            for message in messages
            if row_get(message, "content", "")
        ]
        summary_data = await summarize_session(normalized_messages, route=route)
        summary_text = summary_data.get("summary", "").strip()
        if not summary_text:
            continue
        await upsert_session_summary(
            session_id,
            summary_text,
            mood=summary_data.get("mood"),
            topic_tags=summary_data.get("topic_tags") or [],
            msg_count=msg_count,
        )
        print(f"🧾 已生成 session 摘要: {session_id}")


async def build_system_prompt_with_memories(
    user_message: str,
    session_id: str = "",
    recall_card_ids: list[int] | None = None,
) -> str:
    """
    构建带分层记忆的 system prompt。
    session_id 用於查詢 active checkpoint（若有則注入）。
    recall_card_ids 用於注入勾選的 Snapshot 卡片。
    """
    if not MEMORY_ENABLED:
        return SYSTEM_PROMPT

    try:
        # ── 世界書條目 ──
        wb_all = await get_enabled_persona_entries()
        wb_by_pos: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
        WORLDBOOK_TOKEN_LIMIT = 4000
        triggered_chars = 0
        for entry in wb_all:
            pos = int(entry.get("position", 1))
            content = str(entry.get("content", "") or "").strip()
            if not content:
                continue
            if entry.get("always_on"):
                wb_by_pos.setdefault(pos, []).append(content)
            else:
                # 關鍵字觸發：substring match，掃 user_message（OR 邏輯）
                kw_raw = str(entry.get("keywords", "") or "")
                keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
                if not keywords:
                    continue
                scan_text = user_message
                if any(kw in scan_text for kw in keywords):
                    if triggered_chars + len(content) > WORLDBOOK_TOKEN_LIMIT:
                        continue
                    wb_by_pos.setdefault(pos, []).append(content)
                    triggered_chars += len(content)

        evergreen = await get_memories_by_tier(MEMORY_TIER_EVERGREEN, limit=MAX_EVERGREEN_INJECT)
        stable = await search_memories(
            user_message,
            limit=MAX_STABLE_INJECT,
            tiers=[MEMORY_TIER_STABLE],
            statuses=[ACTIVE_STATUS],
        )
        recent_summaries = await get_recent_session_summaries(limit=MAX_SUMMARIES_INJECT)
        open_loops = await get_open_loops(status="open", limit=MAX_OPEN_LOOPS_INJECT)
        # ── 自發回憶：隨機撈幾條，排除已精準匹配的 ──
        already_matched_ids = [int(row_get(m, "id", 0)) for m in stable if row_get(m, "id", 0)]
        spontaneous = await get_random_memories(
            limit=MAX_SPONTANEOUS_INJECT,
            tiers=[MEMORY_TIER_STABLE, MEMORY_TIER_EVERGREEN],
            exclude_ids=already_matched_ids,
        )
        ephemeral = await get_memories_by_tier(
            MEMORY_TIER_EPHEMERAL,
            limit=MAX_EPHEMERAL_INJECT,
            days=3,
            touch=False,
        )
        latest_summary_time = await get_latest_summary_time()
        bank_always = await get_always_load_items()
        recent_messages = await get_recent_messages_for_context(session_id, limit=4) if session_id else []

        # 有 session_id 才查 checkpoint
        checkpoint = None
        if session_id:
            try:
                checkpoint = await get_active_checkpoint(session_id)
            except Exception:
                pass

        sections = []

        bank_always_lines: list[str] = []
        if bank_always:
            total_chars = 0
            count = 0
            for item in bank_always:
                content = str(row_get(item, "content", "") or "").strip()
                if not content:
                    continue
                if count >= MAX_BANK_ALWAYS_INJECT:
                    break
                if total_chars + len(content) > MAX_BANK_ALWAYS_CHARS:
                    break
                bank_always_lines.append(content)
                total_chars += len(content)
                count += 1
        if bank_always_lines:
            sections.append(
                "【核心記憶（永久）】\n"
                "（以下為背景記憶，若與最近對話狀態矛盾，以最近對話為準）\n"
                + "\n".join(bank_always_lines)
            )

        search_text = user_message
        if recent_messages:
            for msg in recent_messages[-4:]:
                content = str(row_get(msg, "content", "") or "").strip()
                if content:
                    search_text += " " + content

        matched_tags = match_tags_in_text(search_text)
        bank_candidates = []
        if matched_tags:
            bank_candidates = await search_memory_bank_by_tags(
                matched_tags,
                limit=MAX_BANK_ONDEMAND_INJECT + 2,
            )

        if VECTOR_FALLBACK_ENABLED and len(bank_candidates) < MAX_BANK_ONDEMAND_INJECT:
            remaining = MAX_BANK_ONDEMAND_INJECT + 2 - len(bank_candidates)
            exclude_ids = [int(row_get(item, "id", 0) or 0) for item in bank_candidates if row_get(item, "id", 0)]
            try:
                query_vec = await compute_embedding(user_message)
                vector_items = await search_memory_bank_by_vector(
                    query_vec,
                    limit=remaining,
                    exclude_ids=exclude_ids,
                )
                bank_candidates.extend(vector_items)
            except Exception:
                pass

        bank_ondemand_parts: list[str] = []
        if bank_candidates:
            total_chars = 0
            injected_count = 0
            for item in bank_candidates:
                if injected_count >= MAX_BANK_ONDEMAND_INJECT:
                    break
                title = str(row_get(item, "title", "") or "").strip()
                content = str(row_get(item, "content", "") or "").strip()
                if not content:
                    continue
                candidate_chars = len(content) + (len(title) + 4 if title else 0)
                if total_chars + candidate_chars > MAX_BANK_ONDEMAND_CHARS:
                    continue
                if title:
                    bank_ondemand_parts.append(f"〔{title}〕")
                bank_ondemand_parts.append(content)
                total_chars += candidate_chars
                injected_count += 1
        if bank_ondemand_parts:
            sections.append("【相關記憶（按需召回）】\n" + "\n".join(bank_ondemand_parts))

        evergreen_lines = [format_memory_line(mem) for mem in evergreen]
        evergreen_lines = [line for line in evergreen_lines if line]
        if evergreen_lines:
            sections.append("【核心长期记忆】\n" + "\n".join(evergreen_lines))

        stable_lines = [format_memory_line(mem, include_date=True) for mem in stable]
        stable_lines = [line for line in stable_lines if line]
        if stable_lines:
            sections.append("【相关稳定记忆】\n" + "\n".join(stable_lines))

        # ── 對話 RAG 注入 ──
        conv_rag_lines: list[str] = []
        if user_message and MAX_CONV_RAG_INJECT > 0:
            try:
                conv_vec = await compute_embedding_conv(user_message[:500])
                conv_chunks = await search_conversation_vectors(conv_vec, limit=MAX_CONV_RAG_INJECT)
                for chunk in conv_chunks:
                    text = str(chunk.get("chunk_text", "")).strip()
                    if text:
                        conv_rag_lines.append(text)
            except Exception:
                pass
        if conv_rag_lines:
            sections.append(
                "【相關對話片段】\n"
                "（以下是你們過去對話中，與現在話題相近的片段。僅供參考，不需要刻意提起。）\n"
                + "\n---\n".join(conv_rag_lines)
            )

        # ── 自發回憶注入 ──
        spontaneous_lines = []
        for mem in spontaneous:
            content = str(mem.get("content", "") or "").strip()
            if content:
                spontaneous_lines.append(f"- {content}")
        if spontaneous_lines:
            sections.append(
                "【你模糊想起的一些事】\n"
                "（不一定跟當前話題直接相關。如果聊到沾邊的，可以自然帶一句；不提也完全沒關係。）\n"
                + "\n".join(spontaneous_lines)
            )

        loop_lines = []
        for loop in open_loops:
            content = str(row_get(loop, "content", "") or "").strip()
            if not content:
                continue
            loop_type = str(row_get(loop, "loop_type", "") or "").strip()
            if loop_type:
                loop_lines.append(f"- [{loop_type}] {content}")
            else:
                loop_lines.append(f"- {content}")
        if loop_lines:
            sections.append("【未完事项】\n" + "\n".join(loop_lines))

        # Snapshot 召回注入（open loops 之後、active checkpoint 之前）
        snapshot_count = 0
        if recall_card_ids:
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT id, summary_text, card_title, version, created_at
                        FROM conversation_checkpoints
                        WHERE id = ANY($1::int[]) AND saved_as_card = TRUE
                        ORDER BY created_at ASC
                        """,
                        recall_card_ids,
                    )
                for row in rows:
                    snap_text = str(row["summary_text"] or "").strip()
                    if not snap_text:
                        continue
                    title_note = f"（{row['card_title']}）" if row["card_title"] else ""
                    sections.append(
                        f"[從前次對話召回的備忘{title_note} — 非本次對話內容，僅供接續參考]\n"
                        f"{snap_text}\n"
                        f"[備忘結束 — 如果與最近對話或記憶有衝突，以最近的為準]"
                    )
                    snapshot_count += 1
            except Exception as snap_exc:
                print(f"⚠️  Snapshot 召回失敗: {snap_exc}")

        ephemeral_lines = [format_memory_line(mem, include_date=True) for mem in ephemeral]
        ephemeral_lines = [line for line in ephemeral_lines if line]
        if ephemeral_lines:
            sections.append("【近期短期状态】\n" + "\n".join(ephemeral_lines))

        # Checkpoint 注入（放在 ephemeral 之後、session summaries 之前）
        if checkpoint:
            cp_text = str(checkpoint.get("summary_text", "") or "").strip()
            cp_covers = checkpoint.get("covers_until_msg_id", 0)
            if cp_text:
                sections.append(
                    f"【對話 Checkpoint（非權威，如有衝突以最新訊息為準）】\n"
                    f"[涵蓋至訊息 #{cp_covers}]\n"
                    f"{cp_text}"
                )

        # Session summaries（有 checkpoint 時仍注入，因為 summaries 是其他 session 的，不重疊）
        summary_lines = []
        for summary in recent_summaries:
            summary_text = str(row_get(summary, "summary", "") or "").strip()
            if not summary_text or summary_text.startswith(SKIPPED_SUMMARY_PREFIX):
                continue
            mood = str(row_get(summary, "mood", "") or "").strip()
            prefix = f"- ({mood}) " if mood else "- "
            summary_lines.append(prefix + summary_text)
        if summary_lines:
            sections.append("【最近会话摘要】\n" + "\n".join(summary_lines))

        if not sections:
            return SYSTEM_PROMPT

        time_lines = [f"- 当前本地时间：{local_now().strftime('%Y-%m-%d %H:%M')}"]
        if latest_summary_time:
            time_lines.append(f"- 最近一段已总结的会话大约在 {format_relative_time(latest_summary_time)} 前。")
        sections.insert(0, "【时间参考】\n" + "\n".join(time_lines))

        # 世界書 pos_1：注入在 sections 最前面（system prompt 之後、記憶之前）
        if wb_by_pos.get(1):
            sections.insert(0, "\n\n".join(wb_by_pos[1]))

        # 世界書 pos_2：注入在 sections 結尾（記憶之後、對話之前）
        if wb_by_pos.get(2):
            sections.append("\n\n".join(wb_by_pos[2]))

        # 世界書 pos_3：最末段
        if wb_by_pos.get(3):
            sections.append("\n\n".join(wb_by_pos[3]))

        if len(sections) == 1 and not SYSTEM_PROMPT:
            return sections[0]

        # 世界書 pos_0：在 SYSTEM_PROMPT 之前
        wb_pos0_prefix = ("\n\n".join(wb_by_pos[0]) + "\n\n") if wb_by_pos.get(0) else ""

        enhanced_prompt = f"""{wb_pos0_prefix}{SYSTEM_PROMPT}

{chr(10).join(sections)}

# 使用方式
- 这些内容只是辅助你接住上下文，不要机械复述。
- 优先使用核心长期记忆和相关稳定记忆；短期状态只在相关时轻描淡写带一下。
- open loops 是待追问或待完成事项，合适时自然接上。
- 若当前用户消息与旧记忆冲突，以当前明确新信息为准。
- Checkpoint 為非權威壓縮摘要，以最近對話和記憶為準。
- 【你模糊想起的一些事】是你腦裡浮現的片段，只在話題自然沾到邊時順嘴提一句，不要刻意、不要每次都用。"""

        total_count = len(evergreen_lines) + len(stable_lines) + len(summary_lines) + len(loop_lines) + len(ephemeral_lines)
        cp_note = " + checkpoint" if checkpoint else ""
        snap_note = f" + {snapshot_count} snapshot" if snapshot_count else ""
        print(f"📚 注入了分层上下文，共 {total_count} 条片段{cp_note}{snap_note}")
        if spontaneous_lines:
            print(f"🎲 自發回憶注入 {len(spontaneous_lines)} 條")
        return enhanced_prompt

    except Exception as exc:
        print(f"⚠️  记忆检索失败: {exc}，使用纯人设")
        return SYSTEM_PROMPT


# ============================================================
# 后台记忆处理
# ============================================================

async def process_memories_background(
    session_id: str,
    user_msg: str,
    assistant_msg: str,
    model: str,
    context_messages: list | None = None,
    has_stable_session_id: bool = False,
    model_routes: dict[str, ProviderRoute] | None = None,
    extract_interval: int | None = None,
):
    """
    后台异步：存储对话 + 提取记忆（不阻塞主流程）
    extract_interval：覆蓋 MEMORY_EXTRACT_INTERVAL（0=禁用，1=每輪，N=每N輪）
    """
    interval = extract_interval if extract_interval is not None else MEMORY_EXTRACT_INTERVAL
    try:
        route_map = model_routes or {}
        # extraction / summary 沒有獨立設定時，fallback 用 chat route
        extraction_route = route_map.get("extraction") or route_map.get("summary") or route_map.get("chat")
        summary_route = route_map.get("summary") or route_map.get("chat")

        await save_message(session_id, "user", user_msg, model)
        await save_message(session_id, "assistant", assistant_msg, model)

        if interval == 0:
            if has_stable_session_id:
                await summarize_stale_sessions(route=summary_route)
            print("⏭️  记忆自动提取已禁用，跳过")
            return

        # 從 DB 讀取未提取的 raw turns（不依賴前端送來的 context_messages）
        if interval > 1:
            unextracted_count = await count_unextracted_messages(session_id)
            threshold = interval * 2
            if unextracted_count < threshold:
                if has_stable_session_id:
                    await summarize_stale_sessions(route=summary_route)
                print(f"⏭️  [{session_id[:8]}] 未提取 {unextracted_count} 條，未達門檻 {threshold}，跳過")
                return
            print(f"📝 [{session_id[:8]}] 未提取 {unextracted_count} 條，執行提取")

        raw_turns = await get_unextracted_messages(session_id, limit=max(interval * 2 + 4, 20))
        if not raw_turns:
            if has_stable_session_id:
                await summarize_stale_sessions(route=summary_route)
            print(f"⏭️  [{session_id[:8]}] DB 無新訊息，跳過提取")
            return

        messages_for_extraction = [{"role": r["role"], "content": r["content"]} for r in raw_turns]
        print(f"📝 [{session_id[:8]}] 從 DB 讀取 {len(messages_for_extraction)} 條訊息提取記憶")

        existing_memories = [dict(row) for row in await get_active_memory_briefs(limit=60)]
        existing_by_content, existing_by_key = build_memory_lookup(existing_memories)
        open_loops = [dict(row) for row in await get_open_loops(status="open", limit=20)]
        extraction_result = await extract_memory_actions(
            messages_for_extraction,
            existing_memories=existing_memories,
            open_loops=open_loops,
            route=extraction_route,
        )

        saved_count = 0
        confirmation_count = 0
        conflict_count = 0

        for action in extraction_result["memory_actions"]:
            action_type = action.get("action")
            if action_type in {"create", "conflict"} and is_meta_memory(action["content"]):
                print(f"🚫 过滤掉 meta 记忆: {action['content'][:60]}...")
                continue

            if action_type == "create":
                canonical_key = str(action.get("canonical_key") or "").strip()
                matched = None
                if canonical_key:
                    matched = existing_by_key.get(canonical_key)
                if not matched:
                    matched = existing_by_content.get(normalize_text_key(action["content"]))

                if matched:
                    if has_stable_session_id:
                        matched_id = row_get(matched, "id")
                        if matched_id and await add_memory_confirmation(matched_id, session_id):
                            confirmation_count += 1
                            await maybe_promote_memory(matched_id)
                    else:
                        print("ℹ️  命中已有记忆，但当前请求没有稳定 session_id，跳过 confirm 计数")
                    continue

                new_memory_id = await save_action_memory(action, session_id)
                if new_memory_id:
                    saved_count += 1
                    fresh_memory = {
                        "id": new_memory_id,
                        "content": action["content"],
                        "canonical_key": action.get("canonical_key"),
                    }
                    existing_by_content[normalize_text_key(action["content"])] = fresh_memory
                    if action.get("canonical_key"):
                        existing_by_key[action["canonical_key"]] = fresh_memory
            elif action_type == "confirm":
                if not has_stable_session_id:
                    continue
                memory_id = action.get("memory_id")
                if memory_id and await add_memory_confirmation(memory_id, session_id):
                    confirmation_count += 1
                    await maybe_promote_memory(memory_id)
            elif action_type == "conflict":
                await handle_memory_conflict(action, session_id)
                conflict_count += 1

        loop_creates = extraction_result["open_loops"]["create"]
        for loop in loop_creates:
            await create_open_loop(
                content=loop["content"],
                loop_type=loop.get("loop_type", "promise"),
                source_session=session_id,
            )

        if extraction_result["open_loops"]["resolve"]:
            await resolve_open_loops(extraction_result["open_loops"]["resolve"])

        # 提取完成後更新 cursor
        if raw_turns:
            await update_extract_cursor(session_id, raw_turns[-1]["id"])

        await expire_old_memories()
        await expire_old_open_loops()
        if has_stable_session_id:
            await summarize_stale_sessions(route=summary_route)

        if saved_count or confirmation_count or conflict_count or loop_creates or extraction_result["open_loops"]["resolve"]:
            total = await get_all_memories_count()
            print(
                f"💾 记忆处理完成：新增 {saved_count}，确认 {confirmation_count}，冲突 {conflict_count}，"
                f"open_loops +{len(loop_creates)} / resolved {len(extraction_result['open_loops']['resolve'])}，总计 {total} 条"
            )

    except Exception as exc:
        print(f"⚠️  后台记忆处理失败: {exc}")


# ============================================================
# API 接口
# ============================================================

@app.get("/")
async def health_check():
    """健康检查"""
    memory_count = 0
    if MEMORY_ENABLED:
        try:
            memory_count = await get_all_memories_count()
        except:
            pass
    
    return {
        "status": "running",
        "gateway": "AI Memory Gateway v2.0",
        "system_prompt_loaded": len(SYSTEM_PROMPT) > 0,
        "system_prompt_length": len(SYSTEM_PROMPT),
        "memory_enabled": MEMORY_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": MEMORY_EXTRACT_INTERVAL,
    }


@app.get("/v1/models")
async def list_models():
    """模型列表（让客户端不报错）"""
    default_routes = get_default_routes()
    chat_route = default_routes.get("chat")
    model_id = chat_route.model if chat_route else DEFAULT_MODEL
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ai-memory-gateway",
            }
        ],
    }


@app.get("/api/model-routing/meta")
async def model_routing_meta():
    default_routes = get_default_routes()
    return {
        "providers": get_provider_statuses(),
        "defaults": serialize_model_routes(default_routes),
        "legacy_default_model": DEFAULT_MODEL,
    }


@app.get("/api/model-routing/models")
async def model_routing_models(provider: str = Query(...)):
    provider_id = normalize_provider(provider)
    if not provider_id:
        return JSONResponse({"error": "不支援的 provider"}, status_code=400)

    try:
        models = await list_models_for_provider(provider_id)
        return {"provider": provider_id, "models": models}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/memory-bank")
async def api_list_memory_bank(
    category: str = Query(default=""),
    always_load: str = Query(default=""),
):
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    always_flag = parse_bool_value(always_load, None)
    items = await get_memory_bank_items(
        category=category.strip() or None,
        always_load=always_flag,
        enabled_only=False,
    )
    for item in items:
        for field in ("created_at", "updated_at"):
            if item.get(field):
                item[field] = str(item[field])
    return items


@app.post("/api/memory-bank")
async def api_create_memory_bank(request: Request):
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    try:
        body = await request.json()
        payload = build_memory_bank_payload(body, partial=False)
        item = await create_memory_bank_item(**payload)
        await refresh_tag_lexicon()
        asyncio.create_task(compute_and_store_embedding(item["id"], payload["content"]))
        item = await get_memory_bank_item(item["id"]) or item
        for field in ("created_at", "updated_at"):
            if item.get(field):
                item[field] = str(item[field])
        return item
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.put("/api/memory-bank/{item_id}")
async def api_update_memory_bank(item_id: int, request: Request):
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    old_item = await get_memory_bank_item(item_id)
    if not old_item:
        return JSONResponse({"error": "找不到該項目"}, status_code=404)

    try:
        body = await request.json()
        payload = build_memory_bank_payload(body, partial=True)
        should_reembed = False
        content_for_reembed = ""
        if "content" in payload:
            should_reembed = old_item.get("content_hash") != payload.get("content_hash")
            content_for_reembed = str(payload.get("content") or "")
        item = await update_memory_bank_item(item_id, **payload)
        if not item:
            return JSONResponse({"error": "更新失敗"}, status_code=500)
        await refresh_tag_lexicon()
        if should_reembed and content_for_reembed:
            asyncio.create_task(compute_and_store_embedding(item_id, content_for_reembed))
        for field in ("created_at", "updated_at"):
            if item.get(field):
                item[field] = str(item[field])
        return item
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/memory-bank/{item_id}")
async def api_delete_memory_bank(item_id: int):
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    ok = await delete_memory_bank_item(item_id)
    if not ok:
        return JSONResponse({"error": "找不到該項目"}, status_code=404)
    await refresh_tag_lexicon()
    return {"ok": True}


@app.post("/api/memory-bank/import")
async def api_import_memory_bank(request: Request):
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    try:
        body = await request.json()
        if not isinstance(body, list):
            return JSONResponse({"error": "請傳入 JSON 陣列"}, status_code=400)

        imported = 0
        for raw_item in body:
            if not isinstance(raw_item, dict):
                continue
            payload = build_memory_bank_payload(raw_item, partial=False)
            item = await create_memory_bank_item(**payload)
            asyncio.create_task(compute_and_store_embedding(item["id"], payload["content"]))
            imported += 1

        await refresh_tag_lexicon()
        return {"imported": imported}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/memory-bank/export")
async def api_export_memory_bank():
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    items = await get_memory_bank_items(enabled_only=False)
    for item in items:
        item.pop("has_embedding", None)
        for field in ("created_at", "updated_at"):
            item.pop(field, None)
    return items


@app.post("/api/memory-bank/reembed-all")
async def api_reembed_all_memory_bank():
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    try:
        items = await get_memory_bank_items(enabled_only=False)
        updated = 0
        for item in items:
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            await compute_and_store_embedding(int(item["id"]), content)
            updated += 1
        return {"updated": updated}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    body = await request.json()
    model_routes = get_effective_routes(body.pop("model_routing", None))
    chat_route = model_routes.get("chat")

    if not chat_route and not API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请在环境变量中配置"},
        )

    messages = body.get("messages", [])

    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_message = extract_text_from_content(msg.get("content"))
            break

    original_messages = normalize_messages_for_memory(messages)

    # ---------- 先取 session_id，後面 system prompt 和 context 都需要它 ----------
    provided_session_id = str(body.pop("session_id", "") or "").strip()
    has_stable_session_id = bool(provided_session_id)
    session_id = provided_session_id or str(uuid.uuid4())[:8]

    # 召回的 Snapshot 卡片 ID 列表
    raw_recall = body.pop("recall_card_ids", None)
    recall_card_ids: list[int] | None = None
    if isinstance(raw_recall, list) and raw_recall:
        try:
            recall_card_ids = [int(x) for x in raw_recall]
        except (TypeError, ValueError):
            recall_card_ids = None

    # Extended thinking budget（只對 Anthropic 有效）
    raw_thinking = body.pop("thinking_budget", None)
    thinking_budget: int | None = None
    if raw_thinking is not None:
        try:
            v = int(raw_thinking)
            if v >= 1024:
                thinking_budget = v
        except (TypeError, ValueError):
            pass

    # 記憶提取間隔覆蓋（0=禁用，1=每輪，N=每N輪）
    extract_interval_override: int | None = None
    raw_extract = body.pop("_extract_interval", None)
    if raw_extract is not None:
        try:
            v = int(raw_extract)
            if v >= 0:
                extract_interval_override = v
        except (TypeError, ValueError):
            pass

    # ---------- 模型处理 ----------
    model = chat_route.model if chat_route else body.get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    body["model"] = model
    if not chat_route:
        body = normalize_openai_token_limit(body, model, api_base_url=API_BASE_URL)

    if SYSTEM_PROMPT or (MEMORY_ENABLED and user_message):
        if MEMORY_ENABLED and user_message:
            enhanced_prompt = await build_system_prompt_with_memories(user_message, session_id, recall_card_ids)
        else:
            enhanced_prompt = SYSTEM_PROMPT

        if enhanced_prompt:
            has_system = any(msg.get("role") == "system" for msg in messages)
            if has_system:
                for i, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        messages[i]["content"] = enhanced_prompt + "\n\n" + msg["content"]
                        break
            else:
                messages.insert(0, {"role": "system", "content": enhanced_prompt})

    body["messages"] = messages

    print(f"📨 [{session_id[:8]}] {local_now().strftime('%m-%d %H:%M')} 收到訊息（{len(user_message)} 字）")

    # ---------- Checkpoint context / Packer hard floor ----------
    used_checkpoint = False
    if MEMORY_ENABLED and has_stable_session_id:
        try:
            active_cp = await get_active_checkpoint(session_id)
        except Exception:
            active_cp = None

        if active_cp:
            # 有 checkpoint：只送 checkpoint 之後的 DB raw turns + 當前 user message
            post_cp = await get_messages_after_checkpoint(
                session_id, active_cp["covers_until_msg_id"]
            )
            current_user_msg = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            system_msgs = [m for m in messages if m.get("role") == "system"]
            new_non_system = post_cp + ([current_user_msg] if current_user_msg else [])
            if new_non_system:
                messages = system_msgs + new_non_system
                body["messages"] = messages
                used_checkpoint = True
                print(f"📦 [{session_id[:8]}] checkpoint 模式：送出 {len(post_cp)} 則 post-cp + 當前訊息")
        else:
            # 無 checkpoint：packer hard floor 保底
            non_system = [m for m in messages if m.get("role") != "system"]
            if len(non_system) < MIN_RECENT_RAW_TURNS and MIN_RECENT_RAW_TURNS > 0:
                try:
                    db_history = await get_recent_messages_for_context(
                        session_id, MIN_RECENT_RAW_TURNS - 1
                    )
                    if db_history:
                        current_msg = non_system[-1] if non_system else None
                        system_msgs = [m for m in messages if m.get("role") == "system"]
                        new_non_system = db_history + ([current_msg] if current_msg else [])
                        if len(new_non_system) > len(non_system):
                            messages = system_msgs + new_non_system
                            body["messages"] = messages
                            print(f"🔒 [{session_id[:8]}] hard floor：{len(non_system)} → {len(new_non_system)} 則")
                except Exception as _exc:
                    print(f"⚠️  hard floor 補底失敗: {_exc}")

    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE
    
    is_stream = body.get("stream", False)

    if is_stream:
        if chat_route:
            return StreamingResponse(
                stream_route_and_capture(
                    chat_route,
                    messages,
                    session_id,
                    user_message,
                    model,
                    body.get("temperature"),
                    body.get("top_p"),
                    body.get("max_tokens"),
                    original_messages,
                    has_stable_session_id,
                    model_routes,
                    used_checkpoint=used_checkpoint,
                    thinking_budget=thinking_budget,
                    extract_interval=extract_interval_override,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        return StreamingResponse(
            stream_and_capture(
                headers,
                body,
                session_id,
                user_message,
                model,
                original_messages,
                has_stable_session_id,
                model_routes,
                used_checkpoint=used_checkpoint,
                extract_interval=extract_interval_override,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        if chat_route:
            try:
                resp_data = await create_chat_completion_with_route(
                    chat_route,
                    messages,
                    temperature=body.get("temperature"),
                    top_p=body.get("top_p"),
                    max_tokens=body.get("max_tokens"),
                )
            except ValueError as exc:
                return JSONResponse(status_code=400, content={"error": str(exc)})
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

            assistant_msg = ""
            try:
                assistant_msg = resp_data["choices"][0]["message"]["content"]
            except (KeyError, IndexError):
                pass

            if MEMORY_ENABLED and user_message and assistant_msg:
                asyncio.create_task(
                    process_memories_background(
                        session_id,
                        user_message,
                        assistant_msg,
                        model,
                        context_messages=original_messages,
                        has_stable_session_id=has_stable_session_id,
                        model_routes=model_routes,
                        extract_interval=extract_interval_override,
                    )
                )

            resp_data["_gateway_meta"] = {"used_checkpoint": used_checkpoint}
            return JSONResponse(status_code=200, content=resp_data)

        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(API_BASE_URL, headers=headers, json=body)

            if response.status_code == 200:
                resp_data = response.json()
                assistant_msg = ""
                try:
                    assistant_msg = resp_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    pass

                if MEMORY_ENABLED and user_message and assistant_msg:
                    asyncio.create_task(
                        process_memories_background(
                            session_id,
                            user_message,
                            assistant_msg,
                            model,
                            context_messages=original_messages,
                            has_stable_session_id=has_stable_session_id,
                            model_routes=model_routes,
                            extract_interval=extract_interval_override,
                        )
                    )
                
                resp_data["_gateway_meta"] = {"used_checkpoint": used_checkpoint}
                return JSONResponse(status_code=200, content=resp_data)
            else:
                return JSONResponse(status_code=response.status_code, content=response.json())


async def stream_route_and_capture(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    session_id: str,
    user_message: str,
    model: str,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    original_messages: list | None = None,
    has_stable_session_id: bool = False,
    model_routes: dict[str, ProviderRoute] | None = None,
    used_checkpoint: bool = False,
    thinking_budget: int | None = None,
    extract_interval: int | None = None,
):
    """真正的 token-by-token 串流（三個 provider 都支援），同時捕獲完整回覆用於記憶提取。"""
    full_response: list[str] = []

    # ── 注入當前時間到最後一則 user 訊息 ──────────────────────
    now_ts = local_now().strftime("%Y-%m-%d %H:%M")
    time_prefix = f"[現在時間：{now_ts}]\n"
    messages = list(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            orig = messages[i].get("content", "")
            if isinstance(orig, str):
                messages[i] = {**messages[i], "content": time_prefix + orig}
            elif isinstance(orig, list):
                new_parts = list(orig)
                for j, part in enumerate(new_parts):
                    if isinstance(part, dict) and part.get("type") == "text":
                        new_parts[j] = {**part, "text": time_prefix + part["text"]}
                        break
                messages[i] = {**messages[i], "content": new_parts}
            break
    # ──────────────────────────────────────────────────────────

    # ── 聯網搜尋（Tavily）──────────────────────────────────────
    search_query = await _should_web_search(user_message, route)
    if search_query:
        # 先推 _searching 事件讓前端顯示「搜尋中」
        yield f"data: {json.dumps({'_searching': True, 'query': search_query}, ensure_ascii=False)}\n\n"
        search_result = await _tavily_search(search_query)
        now_str = datetime.now(timezone(timedelta(hours=TIMEZONE_HOURS))).strftime("%Y-%m-%d %H:%M")
        search_injection = (
            f"\n\n[聯網搜尋結果 - {now_str}]\n"
            f"查詢：{search_query}\n\n"
            f"{search_result}\n\n"
            f"（以上為即時搜尋結果，請根據這些資訊回答。）"
        )
        # 把搜尋結果注入到最後一則 user 訊息
        messages = list(messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                orig_content = messages[i].get("content", "")
                if isinstance(orig_content, str):
                    messages[i] = {**messages[i], "content": orig_content + search_injection}
                elif isinstance(orig_content, list):
                    # multipart（含圖）：在第一個 text part 後面附加
                    new_parts = list(orig_content)
                    for j, part in enumerate(new_parts):
                        if isinstance(part, dict) and part.get("type") == "text":
                            new_parts[j] = {**part, "text": part["text"] + search_injection}
                            break
                    else:
                        new_parts.append({"type": "text", "text": search_injection})
                    messages[i] = {**messages[i], "content": new_parts}
                break
    # ──────────────────────────────────────────────────────────

    try:
        async for sse_line in stream_chat_with_route(
            route,
            messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            used_checkpoint=used_checkpoint,
            thinking_budget=thinking_budget,
        ):
            yield sse_line
            # 捕獲 delta 內容（用於記憶提取）
            if sse_line.startswith("data: ") and "[DONE]" not in sse_line:
                try:
                    chunk = json.loads(sse_line[6:])
                    delta_content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta_content:
                        full_response.append(delta_content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
    except Exception as exc:
        error_chunk = {"error": {"message": str(exc)}}
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    assistant_msg = "".join(full_response)
    if MEMORY_ENABLED and user_message and assistant_msg:
        asyncio.create_task(
            process_memories_background(
                session_id,
                user_message,
                assistant_msg,
                model,
                context_messages=original_messages,
                has_stable_session_id=has_stable_session_id,
                model_routes=model_routes,
                extract_interval=extract_interval,
            )
        )


async def stream_and_capture(
    headers: dict,
    body: dict,
    session_id: str,
    user_message: str,
    model: str,
    original_messages: list | None = None,
    has_stable_session_id: bool = False,
    model_routes: dict[str, ProviderRoute] | None = None,
    used_checkpoint: bool = False,
    extract_interval: int | None = None,
):
    """流式响应 + 捕获完整回复（OpenRouter / legacy 路徑）"""
    full_response = []
    # 讓 OpenRouter 在串流結尾帶 usage 資料
    body = dict(body)
    body.setdefault("stream_options", {"include_usage": True})

    # ── 注入當前時間到最後一則 user 訊息 ──────────────────────
    now_ts = local_now().strftime("%Y-%m-%d %H:%M")
    time_prefix = f"[現在時間：{now_ts}]\n"
    messages = list(body.get("messages", []))
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            orig = messages[i].get("content", "")
            if isinstance(orig, str):
                messages[i] = {**messages[i], "content": time_prefix + orig}
            elif isinstance(orig, list):
                new_parts = list(orig)
                for j, part in enumerate(new_parts):
                    if isinstance(part, dict) and part.get("type") == "text":
                        new_parts[j] = {**part, "text": time_prefix + part["text"]}
                        break
                messages[i] = {**messages[i], "content": new_parts}
            break
    # ──────────────────────────────────────────────────────────

    # ── 聯網搜尋（Tavily）──────────────────────────────────────
    search_query = await _should_web_search(user_message, None)
    if search_query:
        yield f"data: {json.dumps({'_searching': True, 'query': search_query}, ensure_ascii=False)}\n\n"
        search_result = await _tavily_search(search_query)
        now_str = datetime.now(timezone(timedelta(hours=TIMEZONE_HOURS))).strftime("%Y-%m-%d %H:%M")
        search_injection = (
            f"\n\n[聯網搜尋結果 - {now_str}]\n"
            f"查詢：{search_query}\n\n"
            f"{search_result}\n\n"
            f"（以上為即時搜尋結果，請根據這些資訊回答。）"
        )
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                orig_content = messages[i].get("content", "")
                if isinstance(orig_content, str):
                    messages[i] = {**messages[i], "content": orig_content + search_injection}
                elif isinstance(orig_content, list):
                    new_parts = list(orig_content)
                    for j, part in enumerate(new_parts):
                        if isinstance(part, dict) and part.get("type") == "text":
                            new_parts[j] = {**part, "text": part["text"] + search_injection}
                            break
                    else:
                        new_parts.append({"type": "text", "text": search_injection})
                    messages[i] = {**messages[i], "content": new_parts}
                break
    body["messages"] = messages
    # ──────────────────────────────────────────────────────────

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", API_BASE_URL, headers=headers, json=body) as response:
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line == "data: [DONE]":
                    yield "data: [DONE]\n\n"
                    break
                if line.startswith("data: "):
                    try:
                        chunk = json.loads(line[6:])
                        # 在含 usage 的 chunk 注入 _gateway_meta
                        if chunk.get("usage"):
                            chunk["_gateway_meta"] = {"used_checkpoint": used_checkpoint}
                        delta_content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta_content:
                            full_response.append(delta_content)
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    except (json.JSONDecodeError, KeyError, IndexError):
                        yield line + "\n\n"
                else:
                    yield line + "\n"
    
    assistant_msg = "".join(full_response)
    if MEMORY_ENABLED and user_message and assistant_msg:
        asyncio.create_task(
            process_memories_background(
                session_id,
                user_message,
                assistant_msg,
                model,
                context_messages=original_messages,
                has_stable_session_id=has_stable_session_id,
                model_routes=model_routes,
                extract_interval=extract_interval,
            )
        )


# ============================================================
# 记忆管理接口
# ============================================================


@app.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址就会返回所有记忆数据
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        memories = await get_all_memories()
        for mem in memories:
            for field in ("created_at", "last_accessed", "valid_until"):
                if mem.get(field):
                    mem[field] = str(mem[field])
        
        payload = {
            "total": len(memories),
            "exported_at": str(__import__("datetime").datetime.now()),
            "memories": memories,
        }
        filename = f"memories_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        return JSONResponse(
            content=payload,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return {"error": str(e)}


@app.get("/import/memories", response_class=HTMLResponse)
async def import_memories_page():
    """导入记忆的网页界面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return HTMLResponse("""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>导入记忆</title>
<style>
    body { font-family: sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; }
    textarea { width: 100%%; height: 200px; font-size: 14px; margin: 10px 0; }
    button { padding: 10px 20px; font-size: 16px; cursor: pointer; background: #4CAF50; color: white; border: none; border-radius: 4px; margin-right: 8px; }
    button:hover { background: #45a049; }
    input[type="file"] { margin: 10px 0; font-size: 14px; }
    #result { margin-top: 15px; padding: 10px; white-space: pre-wrap; }
    .ok { background: #e8f5e9; } .err { background: #ffebee; } .info { background: #e3f2fd; }
    .tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid #eee; }
    .tab { padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; color: #666; }
    .tab.active { border-bottom-color: #4CAF50; color: #333; font-weight: bold; }
    .panel { display: none; } .panel.active { display: block; }
    .hint { color: #888; font-size: 13px; margin: 5px 0; }
    label { cursor: pointer; }
    .preview { background: #f5f5f5; border: 1px solid #ddd; padding: 10px; margin: 10px 0; max-height: 200px; overflow-y: auto; font-size: 13px; }
    .preview-item { padding: 3px 0; border-bottom: 1px solid #eee; }
    .nav { margin-bottom: 15px; font-size: 14px; color: #666; }
    .nav a { color: #4CAF50; text-decoration: none; }
</style></head><body>
<h2>📥 导入记忆</h2>
<div class="nav"><a href="/manage/memories">→ 管理已有记忆</a></div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('text')">纯文本导入</div>
    <div class="tab" onclick="switchTab('json')">JSON 备份恢复</div>
</div>

<div id="panel-text" class="panel active">
    <p>上传 <b>.txt 文件</b>（每行一条记忆），或直接在下方输入。</p>
    <p class="hint">示例：一行写一条，比如 "用户的名字叫小花"、"用户喜欢吃火锅"</p>
    <input type="file" id="txtFile" accept=".txt">
    <div style="margin: 15px 0; text-align: center; color: #999;">—— 或者直接输入 ——</div>
    <textarea id="txtInput" placeholder="每行一条记忆，例如：&#10;用户的名字叫小花&#10;用户喜欢吃火锅&#10;用户养了一只狗叫豆豆"></textarea>
    <p><label><input type="checkbox" id="skipScore"> 跳过自动评分（所有记忆默认权重 5，不消耗 API 额度）</label></p>
    <button onclick="doTextImport()">导入</button>
</div>

<div id="panel-json" class="panel">
    <p>上传从 <code>/export/memories</code> 保存的 <b>.json 文件</b>，用于备份恢复或平台迁移。</p>
    <input type="file" id="jsonFile" accept=".json">
    <div style="margin: 15px 0; text-align: center; color: #999;">—— 或者直接粘贴 ——</div>
    <textarea id="jsonInput" placeholder="粘贴导出的 JSON"></textarea>
    <br><button onclick="previewJson()">预览</button>
    <div id="jsonPreview"></div>
</div>

<div id="result"></div>

<script>
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    document.getElementById('result').textContent = '';
    document.getElementById('result').className = '';
    document.getElementById('jsonPreview').innerHTML = '';
}

async function doTextImport() {
    const r = document.getElementById('result');
    const file = document.getElementById('txtFile').files[0];
    const text = document.getElementById('txtInput').value.trim();
    const skip = document.getElementById('skipScore').checked;
    
    let content = '';
    if (file) { content = await file.text(); }
    else if (text) { content = text; }
    else { r.className = 'err'; r.textContent = '请先上传文件或输入文本'; return; }
    
    const lines = content.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
    if (lines.length === 0) { r.className = 'err'; r.textContent = '没有找到有效的记忆条目'; return; }
    
    r.className = 'info';
    r.textContent = skip ? '正在导入 ' + lines.length + ' 条记忆...' : '正在为 ' + lines.length + ' 条记忆自动评分，请稍候...';
    
    try {
        const resp = await fetch('/import/text', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lines: lines, skip_scoring: skip})
        });
        const data = await resp.json();
        if (data.error) { r.className = 'err'; r.textContent = '❌ ' + data.error; }
        else { r.className = 'ok'; r.textContent = '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条'; }
    } catch(e) { r.className = 'err'; r.textContent = '❌ 请求失败：' + e.message; }
}

let pendingJsonData = null;

async function previewJson() {
    const r = document.getElementById('result');
    const p = document.getElementById('jsonPreview');
    const file = document.getElementById('jsonFile').files[0];
    const text = document.getElementById('jsonInput').value.trim();
    
    let jsonStr = '';
    if (file) { jsonStr = await file.text(); }
    else if (text) { jsonStr = text; }
    else { r.className = 'err'; r.textContent = '请先上传文件或粘贴 JSON'; return; }
    
    try {
        const parsed = JSON.parse(jsonStr);
        const mems = parsed.memories || [];
        if (mems.length === 0) { r.className = 'err'; r.textContent = '❌ 没有找到 memories 字段，请确认这是从 /export/memories 导出的文件'; p.innerHTML = ''; return; }
        
        pendingJsonData = parsed;
        let html = '<p><b>预览：共 ' + mems.length + ' 条记忆</b></p>';
        const show = mems.slice(0, 10);
        show.forEach(m => { html += '<div class="preview-item">权重 ' + (m.importance || '?') + ' | ' + (m.content || '').substring(0, 80) + '</div>'; });
        if (mems.length > 10) html += '<div class="preview-item" style="color:#999;">...还有 ' + (mems.length - 10) + ' 条</div>';
        html += '<br><button onclick="confirmJsonImport()">确认导入</button>';
        p.innerHTML = html;
        r.textContent = ''; r.className = '';
    } catch(e) { r.className = 'err'; r.textContent = '❌ JSON 格式错误：' + e.message; p.innerHTML = ''; }
}

async function confirmJsonImport() {
    const r = document.getElementById('result');
    if (!pendingJsonData) { r.className = 'err'; r.textContent = '请先预览'; return; }
    
    r.className = 'info'; r.textContent = '导入中...';
    try {
        const resp = await fetch('/import/memories', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(pendingJsonData)
        });
        const data = await resp.json();
        if (data.error) { r.className = 'err'; r.textContent = '❌ ' + data.error; }
        else { r.className = 'ok'; r.textContent = '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条'; }
        document.getElementById('jsonPreview').innerHTML = '';
        pendingJsonData = null;
    } catch(e) { r.className = 'err'; r.textContent = '❌ 请求失败：' + e.message; }
}
</script></body></html>
""")


@app.get("/manage/memories", response_class=HTMLResponse)
async def manage_memories_page():
    """记忆管理页面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return HTMLResponse("""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>管理记忆</title>
<style>
    body { font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
    .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 15px; flex-wrap: wrap; }
    input[type="text"] { padding: 8px 12px; font-size: 14px; border: 1px solid #ddd; border-radius: 4px; width: 250px; }
    button { padding: 8px 16px; font-size: 14px; cursor: pointer; border: none; border-radius: 4px; }
    .btn-green { background: #4CAF50; color: white; } .btn-green:hover { background: #45a049; }
    .btn-red { background: #f44336; color: white; } .btn-red:hover { background: #d32f2f; }
    .btn-gray { background: #9e9e9e; color: white; } .btn-gray:hover { background: #757575; }
    table { width: 100%%; border-collapse: collapse; font-size: 14px; }
    th { background: #f5f5f5; padding: 10px 8px; text-align: left; border-bottom: 2px solid #ddd; position: sticky; top: 0; }
    td { padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; }
    tr:hover { background: #fafafa; }
    .content-cell { max-width: 450px; word-break: break-all; }
    .importance-input { width: 45px; padding: 4px; text-align: center; border: 1px solid #ddd; border-radius: 3px; }
    .content-input { width: 100%%; padding: 4px; border: 1px solid #ddd; border-radius: 3px; font-size: 13px; min-height: 40px; resize: vertical; }
    .actions button { padding: 4px 8px; font-size: 12px; margin: 2px; }
    .msg { padding: 10px; margin-bottom: 10px; border-radius: 4px; }
    .ok { background: #e8f5e9; } .err { background: #ffebee; } .info { background: #e3f2fd; }
    .stats { color: #666; font-size: 14px; margin-bottom: 10px; }
    .nav { margin-bottom: 15px; font-size: 14px; color: #666; }
    .nav a { color: #4CAF50; text-decoration: none; }
    .check-col { width: 30px; text-align: center; }
    .id-col { width: 40px; }
    .imp-col { width: 60px; }
    .source-col { width: 90px; font-size: 12px; color: #888; }
    .time-col { width: 140px; font-size: 12px; color: #888; white-space: nowrap; }
    .actions-col { width: 120px; }
</style></head><body>
<h2>🧠 记忆管理</h2>
<div class="nav"><a href="/import/memories">→ 导入新记忆</a> ｜ <a href="/export/memories">→ 导出备份</a></div>

<div class="toolbar">
    <input type="text" id="searchBox" placeholder="搜索记忆..." oninput="filterAndSort()">
    <input type="date" id="dateFilter" onchange="filterAndSort()" style="padding:7px 10px;font-size:14px;border:1px solid #ddd;border-radius:4px;" title="按日期筛选">
    <button class="btn-gray" onclick="document.getElementById('dateFilter').value='';filterAndSort()" style="padding:7px 10px;font-size:12px;" title="清除日期">✕</button>
    <select id="sortSelect" onchange="filterAndSort()" style="padding:8px 12px;font-size:14px;border:1px solid #ddd;border-radius:4px;">
        <option value="id-desc">ID 从新到旧</option>
        <option value="id-asc">ID 从旧到新</option>
        <option value="imp-desc">权重 从高到低</option>
        <option value="imp-asc">权重 从低到高</option>
    </select>
    <button class="btn-green" onclick="batchSave()">批量保存全部</button>
    <button class="btn-red" onclick="batchDelete()">批量删除选中</button>
    <label style="font-size:13px;color:#666;cursor:pointer;"><input type="checkbox" id="selectAll" onchange="toggleAll()"> 全选</label>
</div>
<div id="msg"></div>
<div class="stats" id="stats"></div>
<div style="overflow-x: auto;">
<table>
    <thead><tr>
        <th class="check-col"><input type="checkbox" id="selectAllHead" onchange="toggleAll()"></th>
        <th class="id-col">ID</th>
        <th>内容</th>
        <th class="imp-col">权重</th>
        <th class="source-col">来源</th>
        <th class="time-col">时间</th>
        <th class="actions-col">操作</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
</table>
</div>

<script>
let allMemories = [];

async function loadMemories() {
    try {
        const resp = await fetch('/api/memories');
        const data = await resp.json();
        allMemories = data.memories || [];
        document.getElementById('stats').textContent = '共 ' + allMemories.length + ' 条记忆';
        filterAndSort();
    } catch(e) { showMsg('err', '加载失败：' + e.message); }
}

function fmtTime(s) {
    if (!s) return '-';
    var d = new Date(s.endsWith('Z') ? s : s + 'Z');
    if (isNaN(d)) return s.slice(0, 19).replace('T', ' ');
    var pad = function(n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function renderTable(mems) {
    const tbody = document.getElementById('tbody');
    tbody.innerHTML = mems.map(m => '<tr data-id="' + m.id + '">' +
        '<td class="check-col"><input type="checkbox" class="mem-check" value="' + m.id + '"></td>' +
        '<td class="id-col">' + m.id + '</td>' +
        '<td class="content-cell"><textarea class="content-input" id="c_' + m.id + '">' + escHtml(m.content) + '</textarea></td>' +
        '<td><input type="number" class="importance-input" id="i_' + m.id + '" value="' + m.importance + '" min="1" max="10"></td>' +
        '<td class="source-col">' + (m.source_session || '-') + '</td>' +
        '<td class="time-col">' + fmtTime(m.created_at) + '</td>' +
        '<td class="actions"><button class="btn-green" onclick="saveMem(' + m.id + ')">保存</button><button class="btn-red" onclick="delMem(' + m.id + ')">删除</button></td>' +
        '</tr>').join('');
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function filterAndSort() {
    const q = document.getElementById('searchBox').value.trim().toLowerCase();
    const sort = document.getElementById('sortSelect').value;
    const dateVal = document.getElementById('dateFilter').value;
    let mems = allMemories;
    if (q) {
        mems = mems.filter(m => m.content.toLowerCase().includes(q));
    }
    if (dateVal) {
        mems = mems.filter(m => m.created_at && fmtTime(m.created_at).slice(0, 10) === dateVal);
    }
    mems = [...mems].sort((a, b) => {
        if (sort === 'id-desc') return b.id - a.id;
        if (sort === 'id-asc') return a.id - b.id;
        if (sort === 'imp-desc') return b.importance - a.importance || b.id - a.id;
        if (sort === 'imp-asc') return a.importance - b.importance || a.id - b.id;
        return 0;
    });
    renderTable(mems);
    const parts = [];
    if (q || dateVal) {
        parts.push('筛选到 ' + mems.length + ' / ' + allMemories.length + ' 条');
        if (dateVal) parts.push('日期: ' + dateVal);
    } else {
        parts.push('共 ' + allMemories.length + ' 条记忆');
    }
    document.getElementById('stats').textContent = parts.join('  ');
}

async function saveMem(id) {
    const content = document.getElementById('c_' + id).value;
    const importance = parseInt(document.getElementById('i_' + id).value);
    try {
        const resp = await fetch('/api/memories/' + id, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content, importance})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已保存 #' + id); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function delMem(id) {
    if (!confirm('确定删除 #' + id + '？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/memories/' + id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已删除 #' + id); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function batchSave() {
    const rows = document.querySelectorAll('#tbody tr');
    if (rows.length === 0) { showMsg('err', '没有记忆可保存'); return; }
    const updates = [];
    rows.forEach(row => {
        const id = parseInt(row.dataset.id);
        const cEl = document.getElementById('c_' + id);
        const iEl = document.getElementById('i_' + id);
        if (cEl && iEl) updates.push({id, content: cEl.value, importance: parseInt(iEl.value)});
    });
    if (!confirm('确定保存全部 ' + updates.length + ' 条记忆的修改？')) return;
    try {
        const resp = await fetch('/api/memories/batch-update', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({updates: updates})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已保存 ' + data.updated + ' 条'); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function batchDelete() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    if (checked.length === 0) { showMsg('err', '请先勾选要删除的记忆'); return; }
    if (!confirm('确定删除选中的 ' + checked.length + ' 条记忆？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/memories/batch-delete', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids: checked})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已删除 ' + data.deleted + ' 条'); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

function toggleAll() {
    const val = event.target.checked;
    document.querySelectorAll('.mem-check').forEach(c => c.checked = val);
    document.getElementById('selectAll').checked = val;
    document.getElementById('selectAllHead').checked = val;
}

function showMsg(cls, text) {
    const el = document.getElementById('msg');
    el.className = 'msg ' + cls;
    el.textContent = text;
    setTimeout(() => { el.textContent = ''; el.className = ''; }, 4000);
}

loadMemories();
</script></body></html>
""")


# ============================================================
# 管理 API
# ============================================================

@app.get("/api/memories")
async def api_get_memories(
    search: str = Query(default=""),
    tier: str = Query(default=""),
    status: str = Query(default=""),
    sort: str = Query(default="date"),
    order: str = Query(default="desc"),
):
    """取得記憶列表（管理頁面用），支援 search/tier/status/sort/order"""
    if not MEMORY_ENABLED:
        return {"error": "記憶系統未啟用"}

    if search.strip():
        tiers_filter = [tier] if tier.strip() else None
        rows = await search_memories(search.strip(), limit=200, tiers=tiers_filter, touch=False)
        memories = [dict(r) for r in rows]
        for m in memories:
            for f in ("canonical_key", "manual_locked", "pending_review",
                      "replaced_by_id", "valid_until", "source_session"):
                m.setdefault(f, None)
        if status.strip():
            memories = [m for m in memories if m.get("status") == status.strip()]
    else:
        memories = await get_all_memories_detail()
        if tier.strip():
            memories = [m for m in memories if m.get("tier") == tier.strip()]
        if status.strip():
            memories = [m for m in memories if m.get("status") == status.strip()]

    # 排序
    reverse = order.lower() != "asc"
    if sort == "importance":
        memories.sort(key=lambda m: (m.get("importance") or 0, m.get("created_at") or ""), reverse=reverse)
    else:
        memories.sort(key=lambda m: m.get("created_at") or "", reverse=reverse)

    for m in memories:
        for field in ("created_at", "last_accessed", "valid_until"):
            if m.get(field):
                m[field] = str(m[field])
    return {"memories": memories}


@app.post("/api/memories")
async def api_create_memory(request: Request):
    """手動新增一條記憶"""
    if not MEMORY_ENABLED:
        return {"error": "記憶系統未啟用"}
    data = await request.json()
    content = str(data.get("content", "")).strip()
    if not content:
        return {"error": "記憶內容不能為空"}
    tier = data.get("tier", "stable")
    if tier not in ("ephemeral", "stable", "evergreen"):
        tier = "stable"
    importance = data.get("importance", 7)
    try:
        importance = max(1, min(10, int(importance)))
    except (TypeError, ValueError):
        importance = 7
    memory_id = await save_memory(
        content=content,
        importance=importance,
        source_session="manual",
        tier=tier,
        manual_locked=True,
    )
    return {"status": "ok", "id": memory_id}


@app.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await update_memory(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
    )
    return {"status": "ok", "id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int):
    """删除单条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    await delete_memory(memory_id)
    return {"status": "ok", "id": memory_id}


@app.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await update_memory(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
        )
    return {"status": "ok", "updated": len(updates)}


@app.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量删除记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    await delete_memories_batch(ids)
    return {"status": "ok", "deleted": len(ids)}


@app.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)
        
        if not lines:
            return {"error": "没有找到记忆条目"}
        
        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            scored = await score_memories(lines)
        
        imported = 0
        skipped = 0
        
        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        memories = data.get("memories", [])
        
        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}
        
        imported = 0
        skipped = 0
        
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
                tier=mem.get("tier", MEMORY_TIER_EPHEMERAL),
                status=mem.get("status", ACTIVE_STATUS),
                canonical_key=mem.get("canonical_key"),
                manual_locked=bool(mem.get("manual_locked", False)),
                pending_review=bool(mem.get("pending_review", False)),
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/chatlog")
async def import_chatlog(request: Request):
    """匯入舊對話紀錄（TXT 格式解析後的訊息列表），存進 DB 供記憶抽取和上下文打包使用"""
    try:
        body = await request.json()
        session_id = body.get("session_id", "").strip()
        messages = body.get("messages", [])
        if not session_id:
            return JSONResponse({"error": "missing session_id"}, status_code=400)
        if not messages:
            return {"imported": 0}

        pool = await get_pool()
        imported = 0
        async with pool.acquire() as conn:
            for msg in messages:
                role = msg.get("role", "").strip()
                content = msg.get("content", "").strip()
                created_at_str = msg.get("created_at", "")
                if role not in ("user", "assistant") or not content:
                    continue
                try:
                    created_at = datetime.fromisoformat(created_at_str).astimezone(timezone.utc) if created_at_str else None
                except Exception:
                    created_at = None
                if created_at:
                    await conn.execute(
                        "INSERT INTO conversations (session_id, role, content, model, created_at) VALUES ($1, $2, $3, $4, $5)",
                        session_id, role, content, "imported", created_at,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO conversations (session_id, role, content, model) VALUES ($1, $2, $3, $4)",
                        session_id, role, content, "imported",
                    )
                imported += 1
        return {"imported": imported}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ============================================================
# 完整備份 / 還原
# ============================================================

@app.get("/api/backup/full")
async def backup_full():
    """完整資料庫備份：記憶、Loops、Snapshot、世界書全部打包。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        def clean_list(rows):
            result = []
            for row in rows:
                d = dict(row) if not isinstance(row, dict) else row
                cleaned = {}
                for k, v in d.items():
                    if k == "has_embedding":
                        continue
                    cleaned[k] = v.isoformat() if hasattr(v, "isoformat") else v
                result.append(cleaned)
            return result

        pool = await get_pool()
        async with pool.acquire() as conn:
            memories_rows    = await conn.fetch("SELECT * FROM memories ORDER BY created_at")
            loops_rows       = await conn.fetch("SELECT * FROM open_loops ORDER BY created_at")
            checkpoints_rows = await conn.fetch("SELECT * FROM conversation_checkpoints ORDER BY created_at")
            mem_bank_rows    = await conn.fetch("SELECT * FROM memory_bank ORDER BY sort_order, created_at")
            personas_rows    = await conn.fetch("SELECT * FROM persona_entries ORDER BY sort_order, id")

        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": 1,
            "memories":    clean_list(memories_rows),
            "open_loops":  clean_list(loops_rows),
            "checkpoints": clean_list(checkpoints_rows),
            "memory_bank": clean_list(mem_bank_rows),
            "personas":    clean_list(personas_rows),
        }
        filename = f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        return JSONResponse(
            content=payload,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/restore/full")
async def restore_full(request: Request):
    """完整資料庫還原：記憶、Loops、Snapshot 全部匯入。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        data = await request.json()
        memories_in    = data.get("memories", [])
        loops_in       = data.get("open_loops", [])
        checkpoints_in = data.get("checkpoints", [])

        # ── 1. 記憶 ──────────────────────────────────────────────
        mem_imported = mem_skipped = 0
        for m in memories_in:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            pool = await get_pool()
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            if exists:
                mem_skipped += 1
                continue
            await save_memory(
                content=content,
                importance=m.get("importance", 5),
                source_session=m.get("source_session", "restore"),
                tier=m.get("tier", MEMORY_TIER_EPHEMERAL),
                status=m.get("status", ACTIVE_STATUS),
                canonical_key=m.get("canonical_key"),
                manual_locked=bool(m.get("manual_locked", False)),
                pending_review=bool(m.get("pending_review", False)),
            )
            mem_imported += 1

        # ── 2. Open Loops ─────────────────────────────────────────
        loop_imported = loop_skipped = 0
        for lp in loops_in:
            content = (lp.get("content") or "").strip()
            if not content:
                continue
            pool = await get_pool()
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT COUNT(*) FROM open_loops WHERE content = $1 AND status = 'open'",
                    content,
                )
            if exists:
                loop_skipped += 1
                continue
            await create_open_loop(
                content=content,
                loop_type=lp.get("loop_type", "promise"),
                source_session=lp.get("source_session", "restore"),
            )
            loop_imported += 1

        # ── 3. Checkpoints（保留原始分類，壓縮摘要就是壓縮摘要）──────
        snap_imported = snap_skipped = 0
        pool = await get_pool()
        for cp in checkpoints_in:
            summary = (cp.get("summary_text") or "").strip()
            if not summary:
                continue
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT COUNT(*) FROM conversation_checkpoints WHERE summary_text = $1",
                    summary,
                )
                if exists:
                    snap_skipped += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO conversation_checkpoints
                        (session_id, version, summary_text, covers_until_msg_id,
                         is_active, token_count, saved_as_card, card_title)
                    VALUES ($1, $2, $3, $4, FALSE, $5, $6, $7)
                    """,
                    cp.get("session_id", "restored"),
                    cp.get("version", 1),
                    summary,
                    cp.get("covers_until_msg_id", 0),
                    cp.get("token_count"),
                    bool(cp.get("saved_as_card", False)),
                    cp.get("card_title"),
                )
                snap_imported += 1

        return JSONResponse({
            "memories":  {"imported": mem_imported,  "skipped": mem_skipped},
            "loops":     {"imported": loop_imported, "skipped": loop_skipped},
            "snapshots": {"imported": snap_imported, "skipped": snap_skipped},
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ============================================================
# 新增 API：記憶升降級 / 鎖定 / Open Loops / Summaries
# ============================================================

@app.post("/api/memories/{memory_id}/upgrade")
async def api_upgrade_memory(memory_id: int, request: Request):
    """
    手動升級記憶 tier：
      ephemeral → stable（直接升）
      stable → evergreen（標 pending_review=true，等人工確認）
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    mem = await get_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "找不到記憶"}, status_code=404)

    current_tier = mem.get("tier", MEMORY_TIER_EPHEMERAL)
    if current_tier == MEMORY_TIER_EPHEMERAL:
        await update_memory(memory_id, tier=MEMORY_TIER_STABLE)
        return {"status": "ok", "id": memory_id, "new_tier": MEMORY_TIER_STABLE}
    elif current_tier == MEMORY_TIER_STABLE:
        await update_memory(memory_id, tier=MEMORY_TIER_EVERGREEN, pending_review=True)
        return {"status": "ok", "id": memory_id, "new_tier": MEMORY_TIER_EVERGREEN, "pending_review": True}
    else:
        return {"status": "already_top", "id": memory_id, "tier": current_tier}


@app.post("/api/memories/{memory_id}/lock")
async def api_toggle_lock(memory_id: int):
    """切換 manual_locked（鎖定/解鎖）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    mem = await get_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "找不到記憶"}, status_code=404)
    new_locked = not bool(mem.get("manual_locked", False))
    await update_memory(memory_id, manual_locked=new_locked)
    return {"status": "ok", "id": memory_id, "manual_locked": new_locked}


@app.get("/api/open-loops")
async def api_get_open_loops(status: str = Query(default="open")):
    """取得 Open Loops 列表，預設只取 open 狀態；傳 status=all 取全部"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if status == "all":
        # 全部狀態
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, loop_type, source_session, status, resolved_at, created_at
                FROM open_loops
                ORDER BY created_at DESC
                LIMIT 200
                """
            )
    else:
        rows = await get_open_loops(status=status, limit=200)
    loops = []
    for r in rows:
        d = dict(r)
        for f in ("resolved_at", "created_at"):
            if d.get(f):
                d[f] = str(d[f])
        loops.append(d)
    return {"loops": loops}


@app.patch("/api/open-loops/{loop_id}")
async def api_patch_open_loop(loop_id: int, request: Request):
    """更新 open loop 狀態：resolved / dropped"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    new_status = data.get("status", "resolved")
    if new_status not in ("resolved", "dropped", "open"):
        return JSONResponse({"error": "status 只能是 resolved / dropped / open"}, status_code=400)
    pool = await get_pool()
    async with pool.acquire() as conn:
        resolved_at_sql = "NOW()" if new_status == "resolved" else "NULL"
        await conn.execute(
            f"""
            UPDATE open_loops
            SET status = $1, resolved_at = {resolved_at_sql}
            WHERE id = $2
            """,
            new_status,
            loop_id,
        )
    return {"status": "ok", "id": loop_id, "new_status": new_status}


@app.get("/api/summaries")
async def api_get_summaries(limit: int = Query(default=50)):
    """取得最近 N 筆 session 摘要"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    rows = await get_recent_session_summaries(limit=min(limit, 200))
    summaries = []
    for r in rows:
        d = dict(r)
        for f in ("created_at", "updated_at"):
            if d.get(f):
                d[f] = str(d[f])
        summaries.append(d)
    return {"summaries": summaries}


# ============================================================
# Checkpoint API
# ============================================================


async def _force_extract_for_checkpoint(session_id: str, route: ProviderRoute | None = None):
    """checkpoint 建立前，強制提取所有尚未提取的 raw turns。"""
    raw_turns = await get_unextracted_messages(session_id, limit=200)
    if not raw_turns:
        return
    messages_for_extraction = [{"role": r["role"], "content": r["content"]} for r in raw_turns]
    existing_memories = [dict(row) for row in await get_active_memory_briefs(limit=60)]
    existing_by_content, existing_by_key = build_memory_lookup(existing_memories)
    open_loops_list = [dict(row) for row in await get_open_loops(status="open", limit=20)]
    extraction_result = await extract_memory_actions(
        messages_for_extraction,
        existing_memories=existing_memories,
        open_loops=open_loops_list,
        route=route,
    )
    for action in extraction_result["memory_actions"]:
        action_type = action.get("action")
        if action_type in {"create", "conflict"} and is_meta_memory(action.get("content", "")):
            continue
        if action_type == "create":
            canonical_key = str(action.get("canonical_key") or "").strip()
            matched = existing_by_key.get(canonical_key) if canonical_key else None
            if not matched:
                matched = existing_by_content.get(normalize_text_key(action["content"]))
            if not matched:
                new_id = await save_action_memory(action, session_id)
                if new_id:
                    existing_by_content[normalize_text_key(action["content"])] = {
                        "id": new_id, "content": action["content"]
                    }
        elif action_type == "confirm":
            memory_id = action.get("memory_id")
            if memory_id:
                if await add_memory_confirmation(memory_id, session_id):
                    await maybe_promote_memory(memory_id)
        elif action_type == "conflict":
            await handle_memory_conflict(action, session_id)
    for loop in extraction_result["open_loops"]["create"]:
        await create_open_loop(
            content=loop["content"],
            loop_type=loop.get("loop_type", "promise"),
            source_session=session_id,
        )
    if extraction_result["open_loops"]["resolve"]:
        await resolve_open_loops(extraction_result["open_loops"]["resolve"])
    await update_extract_cursor(session_id, raw_turns[-1]["id"])
    print(f"📝 [{session_id[:8]}] 強制提取完成（checkpoint 前），共 {len(raw_turns)} 則")


@app.post("/api/checkpoint/create")
async def create_checkpoint_endpoint(request: Request):
    """
    建立對話壓縮 checkpoint。
    前端送 { "session_id": "uuid" }。
    後端自動決定涵蓋範圍（session 最後一條已儲存訊息）。
    若已有 checkpoint，則做 rolling rebase（涵蓋舊 checkpoint + 新增訊息）。
    """
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)

    body = await request.json()
    model_routes = get_effective_routes(body.get("model_routing"))
    extraction_route = model_routes.get("extraction") or model_routes.get("summary") or model_routes.get("chat")
    summary_route = model_routes.get("summary") or model_routes.get("chat")
    session_id = str(body.get("session_id", "")).strip()
    if not session_id:
        return JSONResponse({"error": "缺少 session_id"}, status_code=400)

    try:
        # 1. 強制提取（確保 checkpoint 前的記憶都已提取）
        await _force_extract_for_checkpoint(session_id, route=extraction_route)

        # 2. 取舊 checkpoint（rolling rebase 用）
        old_checkpoint = await get_active_checkpoint(session_id)
        from_msg_id = old_checkpoint["covers_until_msg_id"] if old_checkpoint else 0
        old_checkpoint_id = old_checkpoint["id"] if old_checkpoint else None
        old_version = old_checkpoint["version"] if old_checkpoint else 0
        old_summary = old_checkpoint["summary_text"] if old_checkpoint else None

        # 3. 取要壓縮的 raw turns（從舊 checkpoint 之後到最新）
        messages_to_compress = await get_messages_for_compression(session_id, from_msg_id=from_msg_id)
        if not messages_to_compress:
            return JSONResponse({"error": "沒有可壓縮的新對話"}, status_code=400)

        # 4. 如果有舊 checkpoint，前置舊摘要（rolling rebase）
        compress_input = []
        if old_summary:
            compress_input.append(
                {"role": "user", "content": f"[前一個 checkpoint 的摘要]\n{old_summary}"}
            )
        compress_input += [{"role": r["role"], "content": r["content"]} for r in messages_to_compress]

        # 5. 取現有記憶和 open loops 供摘要參考
        existing_memories = [dict(r) for r in await get_active_memory_briefs(limit=40)]
        open_loops_list = [dict(r) for r in await get_open_loops(status="open", limit=20)]

        # 6. 呼叫小模型生成摘要
        summary_text = await generate_checkpoint_summary(
            compress_input,
            existing_memories=existing_memories,
            open_loops=open_loops_list,
            version=old_version + 1,
            route=summary_route,
        )
        if not summary_text:
            return JSONResponse({"error": "摘要生成失敗，請重試"}, status_code=500)

        # 7. covers_until_msg_id = 要壓縮範圍的最後一則訊息 id
        covers_until_msg_id = messages_to_compress[-1]["id"]

        # 8. 停用舊 checkpoint，插入新 checkpoint
        await deactivate_old_checkpoints(session_id)
        new_id = await insert_checkpoint(
            session_id=session_id,
            version=old_version + 1,
            summary_text=summary_text,
            covers_until_msg_id=covers_until_msg_id,
            parent_checkpoint_id=old_checkpoint_id,
        )

        print(
            f"📦 [{session_id[:8]}] Checkpoint v{old_version + 1} 建立完成，"
            f"涵蓋至訊息 #{covers_until_msg_id}"
        )
        return JSONResponse({
            "status": "ok",
            "checkpoint_id": new_id,
            "version": old_version + 1,
            "covers_until_msg_id": covers_until_msg_id,
            "summary": summary_text,
        })

    except Exception as exc:
        print(f"⚠️  建立 checkpoint 失敗: {exc}")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ============================================================
# Snapshot API
# ============================================================

@app.get("/api/checkpoints")
async def list_checkpoints_endpoint(session_id: str):
    """列出指定 session 的所有 checkpoint（含非 active）。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    if not session_id:
        return JSONResponse({"error": "缺少 session_id"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, version, summary_text, covers_until_msg_id,
                       is_active, created_at, saved_as_card, card_title, card_edited_at
                FROM conversation_checkpoints
                WHERE session_id = $1
                ORDER BY version ASC
                """,
                session_id,
            )
        def _ser(r):
            return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
        return JSONResponse({"checkpoints": [_ser(r) for r in rows]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/checkpoints/all")
async def list_all_checkpoints_endpoint(limit: int = 50):
    """列出全部 checkpoint（不限 session），按建立時間倒序。供 MMemoryPage 壓縮紀錄 tab 用。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, version, summary_text, covers_until_msg_id,
                       is_active, created_at, saved_as_card, card_title, card_edited_at
                FROM conversation_checkpoints
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        def _ser(r):
            return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
        return JSONResponse({"checkpoints": [_ser(r) for r in rows]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/snapshots")
async def list_snapshots_endpoint():
    """列出所有 saved_as_card=TRUE 的 Snapshot 卡片。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, version, summary_text, covers_until_msg_id,
                       is_active, created_at, saved_as_card, card_title, card_edited_at
                FROM conversation_checkpoints
                WHERE saved_as_card = TRUE
                ORDER BY created_at DESC
                """,
            )
        def _ser(r):
            return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
        return JSONResponse({"snapshots": [_ser(r) for r in rows]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/snapshots/{checkpoint_id}/save")
async def save_snapshot_endpoint(checkpoint_id: int, request: Request):
    """將指定 checkpoint 標記為 Snapshot 卡片。可帶 card_title。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    card_title = str(body.get("card_title", "")).strip() or None
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE conversation_checkpoints
                SET saved_as_card = TRUE,
                    card_title = COALESCE($2, card_title),
                    card_edited_at = NOW()
                WHERE id = $1
                RETURNING id, card_title, saved_as_card
                """,
                checkpoint_id,
                card_title,
            )
        if not row:
            return JSONResponse({"error": "找不到該 checkpoint"}, status_code=404)
        return JSONResponse({"status": "ok", **dict(row)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.put("/api/snapshots/{checkpoint_id}")
async def update_snapshot_endpoint(checkpoint_id: int, request: Request):
    """編輯 Snapshot 卡片標題。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "無法解析請求"}, status_code=400)
    card_title = str(body.get("card_title", "")).strip()
    if not card_title:
        return JSONResponse({"error": "card_title 不能為空"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE conversation_checkpoints
                SET card_title = $2, card_edited_at = NOW()
                WHERE id = $1 AND saved_as_card = TRUE
                RETURNING id, card_title, card_edited_at
                """,
                checkpoint_id,
                card_title,
            )
        if not row:
            return JSONResponse({"error": "找不到該 Snapshot"}, status_code=404)
        return JSONResponse({"status": "ok", **dict(row)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/snapshots/{checkpoint_id}")
async def delete_snapshot_endpoint(checkpoint_id: int):
    """取消 Snapshot 保存（saved_as_card=FALSE），不刪除 checkpoint 本體。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE conversation_checkpoints
                SET saved_as_card = FALSE, card_title = NULL, card_edited_at = NULL
                WHERE id = $1
                RETURNING id
                """,
                checkpoint_id,
            )
        if not row:
            return JSONResponse({"error": "找不到該 checkpoint"}, status_code=404)
        return JSONResponse({"status": "ok", "id": checkpoint_id})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/checkpoints/{checkpoint_id}")
async def delete_checkpoint_endpoint(checkpoint_id: int):
    """永久刪除一筆 checkpoint（壓縮摘要）。"""
    if not MEMORY_ENABLED:
        return JSONResponse({"error": "記憶系統未啟用"}, status_code=400)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM conversation_checkpoints WHERE id = $1 RETURNING id",
                checkpoint_id,
            )
        if not row:
            return JSONResponse({"error": "找不到該 checkpoint"}, status_code=404)
        return JSONResponse({"status": "ok", "id": checkpoint_id})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ============================================================
# 世界書 API
# ============================================================

def _wb_entry(e) -> dict:
    d = dict(e) if not isinstance(e, dict) else dict(e)
    for k in ('created_at', 'updated_at'):
        if k in d and hasattr(d[k], 'isoformat'):
            d[k] = d[k].isoformat()
    return d

@app.get("/api/worldbook")
async def worldbook_list():
    try:
        entries = await get_all_persona_entries()
        return JSONResponse({"entries": [_wb_entry(e) for e in entries]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/worldbook")
async def worldbook_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        entry = await create_persona_entry(
            title=body.get("title", "").strip(),
            content=body.get("content", "").strip(),
            keywords=body.get("keywords", ""),
            position=int(body.get("position", 1)),
            always_on=bool(body.get("always_on", False)),
            enabled=bool(body.get("enabled", True)),
            priority=int(body.get("priority", 50)),
        )
        return JSONResponse({"entry": _wb_entry(entry)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.put("/api/worldbook/{entry_id}")
async def worldbook_update(entry_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    allowed_fields = ["title", "content", "keywords", "position", "always_on", "enabled", "priority"]
    updates = {k: body[k] for k in allowed_fields if k in body}
    try:
        entry = await update_persona_entry(entry_id, **updates)
        if entry is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"entry": _wb_entry(entry)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/worldbook/{entry_id}")
async def worldbook_delete(entry_id: int):
    try:
        ok = await delete_persona_entry(entry_id)
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.patch("/api/worldbook/{entry_id}/toggle")
async def worldbook_toggle(entry_id: int):
    try:
        entry = await toggle_persona_entry(entry_id)
        if entry is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"entry": _wb_entry(entry)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/worldbook/reorder")
async def worldbook_reorder(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    items = body.get("items", [])
    if not isinstance(items, list):
        return JSONResponse({"error": "items must be a list"}, status_code=400)
    try:
        await reorder_persona_entries(items)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/worldbook/import")
async def worldbook_import(request: Request):
    """批次匯入世界書條目（接受 JSON 陣列）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, list):
        return JSONResponse({"error": "請傳入 JSON 陣列"}, status_code=400)
    imported = skipped = 0
    for item in body:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        content = str(item.get("content", "") or "").strip()
        if not title or not content:
            skipped += 1
            continue
        try:
            await create_persona_entry(
                title=title,
                content=content,
                keywords=str(item.get("keywords", "") or ""),
                position=int(item.get("position", 1)),
                always_on=bool(item.get("always_on", False)),
                enabled=bool(item.get("enabled", True)),
                priority=int(item.get("priority", 50)),
            )
            imported += 1
        except Exception:
            skipped += 1
    return JSONResponse({"imported": imported, "skipped": skipped})


@app.get("/api/worldbook/active")
async def worldbook_active(session_id: str = Query(default="")):
    """Debug endpoint: 顯示當前會話中哪些世界書條目會被注入。"""
    try:
        entries = await get_enabled_persona_entries()
        active = []
        for e in entries:
            if e["always_on"]:
                active.append({**e, "trigger": "always_on"})
        return JSONResponse({"active": active, "total_enabled": len(entries)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ============================================================
# 對話向量化 API
# ============================================================

@app.get("/api/vectorize/status")
async def vectorize_status_endpoint():
    try:
        status = await count_vectorize_status(CONV_VECTORIZE_AFTER_DAYS)
        return JSONResponse(status)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/vectorize/run")
async def vectorize_run_endpoint():
    asyncio.create_task(run_vectorize_job())
    return JSONResponse({"message": "向量化任務已觸發（背景執行中）"})


@app.get("/api/vectorize/settings")
async def vectorize_settings_get():
    return JSONResponse({
        "vectorize_after_days": CONV_VECTORIZE_AFTER_DAYS,
        "chunk_size": CONV_CHUNK_SIZE,
        "chunk_overlap": CONV_CHUNK_OVERLAP,
        "embedding_model": _vectorize_settings["embedding_model"],
        "max_inject": MAX_CONV_RAG_INJECT,
    })


@app.post("/api/vectorize/settings")
async def vectorize_settings_post(request: Request):
    body = await request.json()
    model = str(body.get("embedding_model", "")).strip()
    if not model:
        return JSONResponse({"error": "embedding_model 不能為空"}, status_code=400)
    _vectorize_settings["embedding_model"] = model
    return JSONResponse({"ok": True, "embedding_model": model})


if __name__ == "__main__":
    import uvicorn
    print(f"🚀 AI Memory Gateway 启动中... 端口 {PORT}")
    print(f"📝 人设长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"🤖 默认模型：{DEFAULT_MODEL}")
    print(f"🔗 API 地址：{API_BASE_URL}")
    print(f"🧠 记忆系统：{'开启' if MEMORY_ENABLED else '关闭'}")
    print(f"🔄 记忆提取间隔：{'禁用' if MEMORY_EXTRACT_INTERVAL == 0 else '每轮提取' if MEMORY_EXTRACT_INTERVAL == 1 else f'每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
