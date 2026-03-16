"""
数据库模块 —— 负责所有跟 PostgreSQL 打交道的事情
==============================================
包括：
- 创建/升级表结构
- 存储对话记录
- 存储/检索分层记忆
- 管理确认记录、摘要、未完事项
"""

import os
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

import asyncpg
import jieba

DATABASE_URL = os.getenv("DATABASE_URL", "")

# 搜索权重（向量搜索加入后可重新分配）
WEIGHT_KEYWORD = float(os.getenv("WEIGHT_KEYWORD", "0.5"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.3"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.2"))
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.1"))

ACTIVE_STATUS = "active"
MEMORY_TIER_EVERGREEN = "evergreen"
MEMORY_TIER_STABLE = "stable"
MEMORY_TIER_EPHEMERAL = "ephemeral"


# ============================================================
# 连接池管理
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未设置！")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        print("✅ 数据库连接池已创建")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("✅ 数据库连接池已关闭")


# ============================================================
# 表结构初始化
# ============================================================


async def init_tables():
    pool = await get_pool()

    # ── 先單獨裝 pgvector（失敗不影響主表建立）──
    try:
        async with pool.acquire() as ext_conn:
            await ext_conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception:
        print("⚠️  pgvector 擴充套件不可用，向量搜尋功能將停用")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                model           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id              SERIAL PRIMARY KEY,
                content         TEXT NOT NULL,
                importance      INTEGER DEFAULT 5,
                source_session  TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_accessed   TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              CREATE TYPE memory_tier AS ENUM ('evergreen', 'stable', 'ephemeral');
            EXCEPTION
              WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              CREATE TYPE memory_status AS ENUM ('active', 'expired', 'conflicted', 'superseded');
            EXCEPTION
              WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )

        await conn.execute(
            """
            ALTER TABLE memories
              ADD COLUMN IF NOT EXISTS tier memory_tier NOT NULL DEFAULT 'ephemeral',
              ADD COLUMN IF NOT EXISTS status memory_status NOT NULL DEFAULT 'active',
              ADD COLUMN IF NOT EXISTS canonical_key TEXT,
              ADD COLUMN IF NOT EXISTS manual_locked BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS pending_review BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS replaced_by_id INTEGER,
              ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'memories_replaced_by_id_fkey'
              ) THEN
                ALTER TABLE memories
                ADD CONSTRAINT memories_replaced_by_id_fkey
                FOREIGN KEY (replaced_by_id)
                REFERENCES memories(id)
                ON DELETE SET NULL;
              END IF;
            END
            $$;
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_confirmations (
                id            SERIAL PRIMARY KEY,
                memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                session_id    TEXT NOT NULL,
                confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (memory_id, session_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_summaries (
                id           SERIAL PRIMARY KEY,
                session_id   TEXT NOT NULL UNIQUE,
                summary      TEXT NOT NULL,
                mood         TEXT,
                topic_tags   TEXT[],
                msg_count    INTEGER,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_loops (
                id             SERIAL PRIMARY KEY,
                content        TEXT NOT NULL,
                loop_type      TEXT NOT NULL DEFAULT 'promise',
                source_session TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                resolved_at    TIMESTAMPTZ,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_bank (
                id              SERIAL PRIMARY KEY,
                title           TEXT NOT NULL,
                category        TEXT NOT NULL DEFAULT 'general',
                tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
                content         TEXT NOT NULL,
                always_load     BOOLEAN NOT NULL DEFAULT FALSE,
                enabled         BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order      INTEGER NOT NULL DEFAULT 0,
                source_ref      TEXT,
                notes           TEXT,
                content_hash    TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_fts
            ON memories
            USING gin(to_tsvector('simple', content));
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_session
            ON conversations (session_id, created_at);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_tier_active
            ON memories (tier, importance DESC, created_at DESC)
            WHERE status = 'active';
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_status
            ON memories (status);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_canonical
            ON memories (canonical_key)
            WHERE canonical_key IS NOT NULL;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_valid_until
            ON memories (valid_until)
            WHERE valid_until IS NOT NULL;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_pending_review
            ON memories (id)
            WHERE pending_review = TRUE;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_confirmations_memory
            ON memory_confirmations (memory_id);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_summaries_created
            ON session_summaries (created_at DESC);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_loops_open
            ON open_loops (created_at DESC)
            WHERE status = 'open';
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_bank_tags
            ON memory_bank
            USING gin(tags);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_bank_category
            ON memory_bank(category);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_bank_always_load
            ON memory_bank(always_load)
            WHERE always_load = TRUE;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_bank_enabled_sort
            ON memory_bank(enabled, sort_order, updated_at DESC);
            """
        )
        # session_state — 追蹤每個 session 的 extraction cursor
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_state (
                session_id                  TEXT PRIMARY KEY,
                last_extracted_message_id   INTEGER NOT NULL DEFAULT 0,
                updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # conversation_checkpoints — 對話壓縮 checkpoint
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_checkpoints (
                id                      SERIAL PRIMARY KEY,
                session_id              TEXT NOT NULL,
                version                 INTEGER NOT NULL DEFAULT 1,
                summary_text            TEXT NOT NULL,
                covers_until_msg_id     INTEGER NOT NULL,
                parent_checkpoint_id    INTEGER REFERENCES conversation_checkpoints(id) ON DELETE SET NULL,
                is_active               BOOLEAN NOT NULL DEFAULT TRUE,
                token_count             INTEGER,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                saved_as_card           BOOLEAN NOT NULL DEFAULT FALSE,
                card_title              TEXT,
                card_edited_at          TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoint_active
                ON conversation_checkpoints (session_id)
                WHERE is_active = TRUE;
            """
        )
        # 若表已存在，補上 snapshot 欄位（舊部署升級用）
        await conn.execute(
            "ALTER TABLE conversation_checkpoints ADD COLUMN IF NOT EXISTS saved_as_card BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE conversation_checkpoints ADD COLUMN IF NOT EXISTS card_title TEXT"
        )
        await conn.execute(
            "ALTER TABLE conversation_checkpoints ADD COLUMN IF NOT EXISTS card_edited_at TIMESTAMPTZ"
        )

        # persona_entries — 世界書條目
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_entries (
                id          SERIAL PRIMARY KEY,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                keywords    TEXT NOT NULL DEFAULT '',
                position    INTEGER NOT NULL DEFAULT 1,
                always_on   BOOLEAN NOT NULL DEFAULT FALSE,
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                priority    INTEGER NOT NULL DEFAULT 50,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_persona_entries_enabled_pos
            ON persona_entries (enabled, position, priority);
            """
        )

        # conversation_vectors — 對話向量化（RAG）
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_vectors (
                id                    SERIAL PRIMARY KEY,
                session_id            TEXT NOT NULL,
                chunk_text            TEXT NOT NULL,
                message_ids           INTEGER[] DEFAULT '{}',
                days_old_at_vectorize REAL,
                created_at            TIMESTAMPTZ DEFAULT NOW(),
                vectorized_at         TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conv_vectors_session
            ON conversation_vectors (session_id);
            """
        )
    print("✅ 数据库表结构已就绪")

    # ── 向量欄位與索引（需要 pgvector，裝不上就略過）──
    try:
        async with pool.acquire() as vec_conn:
            await vec_conn.execute(
                "ALTER TABLE memory_bank ADD COLUMN IF NOT EXISTS embedding vector(1536);"
            )
            await vec_conn.execute(
                "ALTER TABLE conversation_vectors ADD COLUMN IF NOT EXISTS embedding vector(1536);"
            )
            await vec_conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_bank_embedding
                ON memory_bank
                USING hnsw (embedding vector_cosine_ops);
                """
            )
            await vec_conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conv_vectors_embedding
                ON conversation_vectors USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL;
                """
            )
            print("✅ 向量欄位與索引已就緒")
    except Exception as e:
        print(f"⚠️ 向量欄位不可用（pgvector 未安裝），embedding 功能停用：{e}")


# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9]*")
NUM_PATTERN = re.compile(r"\d{2,}")

_STOP_WORDS = frozenset(
    {
        "的",
        "了",
        "在",
        "是",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "这",
        "那",
        "有",
        "和",
        "与",
        "也",
        "都",
        "又",
        "就",
        "但",
        "而",
        "或",
        "到",
        "被",
        "把",
        "让",
        "从",
        "对",
        "为",
        "以",
        "及",
        "等",
        "个",
        "不",
        "没",
        "很",
        "太",
        "吗",
        "呢",
        "吧",
        "啊",
        "嗯",
        "哦",
        "哈",
        "呀",
        "嘛",
        "么",
        "啦",
        "哇",
        "喔",
        "会",
        "能",
        "要",
        "想",
        "去",
        "来",
        "说",
        "做",
        "看",
        "给",
        "上",
        "下",
        "里",
        "中",
        "大",
        "小",
        "多",
        "少",
        "好",
        "可以",
        "什么",
        "怎么",
        "如何",
        "哪里",
        "哪个",
        "为什么",
        "还是",
        "然后",
        "因为",
        "所以",
        "虽然",
        "但是",
        "已经",
        "一个",
        "一些",
        "一下",
        "一点",
        "一起",
        "一样",
        "比较",
        "应该",
        "可能",
        "如果",
        "这个",
        "那个",
        "自己",
        "知道",
        "觉得",
        "感觉",
        "时候",
        "现在",
    }
)


def extract_search_keywords(query: str) -> list[str]:
    keywords = set()

    for match in EN_WORD_PATTERN.finditer(query):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    for match in NUM_PATTERN.finditer(query):
        keywords.add(match.group())

    for word in jieba.cut(query, cut_all=False):
        word = word.strip()
        if not word:
            continue
        if EN_WORD_PATTERN.fullmatch(word) or NUM_PATTERN.fullmatch(word):
            continue
        if len(word) < 2 or word in _STOP_WORDS:
            continue
        keywords.add(word)

    return list(keywords)


# ============================================================
# 对话记录操作
# ============================================================


async def save_message(session_id: str, role: str, content: str, model: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model) VALUES ($1, $2, $3, $4)",
            session_id,
            role,
            content,
            model,
        )


async def get_recent_messages(session_id: str, limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            session_id,
            limit,
        )
        return list(reversed(rows))


async def get_session_messages(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            session_id,
        )


async def get_stale_unsummarized_sessions(idle_minutes: int = 30, limit: int = 5):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT c.session_id, MAX(c.created_at) AS last_message_at, COUNT(*)::int AS msg_count
            FROM conversations c
            LEFT JOIN session_summaries s ON s.session_id = c.session_id
            WHERE s.session_id IS NULL
            GROUP BY c.session_id
            HAVING MAX(c.created_at) < NOW() - make_interval(mins => $1)
            ORDER BY MAX(c.created_at) DESC
            LIMIT $2
            """,
            idle_minutes,
            limit,
        )


# ============================================================
# 记忆操作
# ============================================================


async def save_memory(
    content: str,
    importance: int = 5,
    source_session: str = "",
    tier: str = MEMORY_TIER_EPHEMERAL,
    status: str = ACTIVE_STATUS,
    canonical_key: Optional[str] = None,
    manual_locked: bool = False,
    pending_review: bool = False,
    replaced_by_id: Optional[int] = None,
    valid_until: Optional[datetime] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (
                content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review, replaced_by_id, valid_until
            )
            VALUES ($1, $2, $3, $4::memory_tier, $5::memory_status, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            content,
            importance,
            source_session,
            tier,
            status,
            canonical_key,
            manual_locked,
            pending_review,
            replaced_by_id,
            valid_until,
        )
        return row["id"] if row else None


async def get_memory(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, content, importance, source_session, tier, status, canonical_key,
                   manual_locked, pending_review, replaced_by_id, valid_until,
                   created_at, last_accessed
            FROM memories
            WHERE id = $1
            """,
            memory_id,
        )


async def touch_memories(memory_ids: Iterable[int]):
    ids = [mid for mid in memory_ids if mid]
    if not ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
            ids,
        )


async def search_memories(
    query: str,
    limit: int = 10,
    tiers: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
    created_after: Optional[datetime] = None,
    exclude_ids: Optional[Sequence[int]] = None,
    touch: bool = True,
):
    keywords = extract_search_keywords(query)
    if not keywords:
        return []

    params: list[object] = []
    case_parts: list[str] = []
    where_parts: list[str] = []
    for kw in keywords:
        params.append(kw)
        idx = len(params)
        case_parts.append(f"CASE WHEN content ILIKE '%' || ${idx} || '%' THEN 1 ELSE 0 END")
        where_parts.append(f"content ILIKE '%' || ${idx} || '%'")

    filters = [f"({' OR '.join(where_parts)})"]

    if statuses:
        params.append(list(statuses))
        filters.append(f"status::text = ANY(${len(params)}::text[])")
    if tiers:
        params.append(list(tiers))
        filters.append(f"tier::text = ANY(${len(params)}::text[])")
    if created_after:
        params.append(created_after)
        filters.append(f"created_at >= ${len(params)}")
    if exclude_ids:
        params.append(list(exclude_ids))
        filters.append(f"NOT (id = ANY(${len(params)}::int[]))")

    max_hits = len(keywords)
    hit_count_expr = " + ".join(case_parts)

    params.append(limit)
    sql = f"""
        SELECT
            id, content, importance, tier, status, pending_review,
            created_at, last_accessed,
            ({hit_count_expr}) AS hit_count,
            (
                {WEIGHT_KEYWORD} * ({hit_count_expr})::float / {max_hits}.0 +
                {WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                {WEIGHT_RECENCY} * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0))
            ) AS score
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY score DESC, importance DESC, created_at DESC
        LIMIT ${len(params)}
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        results = await conn.fetch(sql, *params)

    if MIN_SCORE_THRESHOLD > 0:
        results = [r for r in results if (r["score"] or 0) >= MIN_SCORE_THRESHOLD]

    if results and touch:
        await touch_memories([r["id"] for r in results])

    if results:
        print(
            f"🔍 搜索 '{query}' → 关键词 {keywords[:8]}{'...' if len(keywords) > 8 else ''} → 命中 {len(results)} 条"
        )
    else:
        print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]} → 无结果")
    return results


async def get_memories_by_tier(
    tier: str,
    limit: int = 20,
    days: Optional[int] = None,
    touch: bool = True,
):
    filters = ["tier::text = $1", "status::text = $2"]
    params: list[object] = [tier, ACTIVE_STATUS]
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        params.append(cutoff)
        # 允許 valid_until 延壽：在時間窗口內 OR valid_until 尚未到期
        filters.append(f"(created_at >= ${len(params)} OR (valid_until IS NOT NULL AND valid_until > NOW()))")

    where_clause = ' AND '.join(filters)
    select_cols = """id, content, importance, source_session, tier, status,
            canonical_key, manual_locked, pending_review, replaced_by_id,
            valid_until, created_at, last_accessed"""

    # Ephemeral 用混合排序：一半按 importance、一半按時間，去重合併
    if tier != MEMORY_TIER_EVERGREEN:
        half = max(limit // 2, 1)
        params_imp = list(params) + [half]
        params_rec = list(params) + [half]

        sql_importance = f"""
            SELECT {select_cols}
            FROM memories
            WHERE {where_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT ${len(params_imp)}
        """
        sql_recent = f"""
            SELECT {select_cols}
            FROM memories
            WHERE {where_clause}
            ORDER BY created_at DESC, importance DESC
            LIMIT ${len(params_rec)}
        """

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows_imp = await conn.fetch(sql_importance, *params_imp)
            rows_rec = await conn.fetch(sql_recent, *params_rec)

        # 去重合併，importance 的排前面
        seen_ids: set[int] = set()
        merged = []
        for row in list(rows_imp) + list(rows_rec):
            rid = row["id"]
            if rid not in seen_ids:
                seen_ids.add(rid)
                merged.append(row)
        merged = merged[:limit]

        if merged and touch:
            await touch_memories([r["id"] for r in merged])
        return merged

    # Evergreen 維持原邏輯：按 importance 排
    params.append(limit)
    sql = f"""
        SELECT {select_cols}
        FROM memories
        WHERE {where_clause}
        ORDER BY importance DESC, created_at DESC
        LIMIT ${len(params)}
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    if rows and touch:
        await touch_memories([r["id"] for r in rows])
    return rows


async def get_recent_memories(limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                id, content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )


async def get_active_memory_briefs(limit: int = 50):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                LEFT(content, 80) AS brief,
                content,
                importance,
                tier,
                canonical_key,
                manual_locked,
                created_at
            FROM memories
            WHERE status = 'active'
            ORDER BY importance DESC, last_accessed DESC, created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return rows


async def expire_old_memories(ephemeral_days: int = 7):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET status = 'expired'
            WHERE tier = 'ephemeral'
              AND status = 'active'
              AND last_accessed < NOW() - make_interval(days => $1)
              AND manual_locked = FALSE
            """,
            ephemeral_days,
        )
        await conn.execute(
            """
            UPDATE memories
            SET status = 'expired'
            WHERE valid_until IS NOT NULL
              AND valid_until < NOW()
              AND status = 'active'
              AND manual_locked = FALSE
            """
        )


async def add_memory_confirmation(memory_id: int, session_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO memory_confirmations (memory_id, session_id)
            VALUES ($1, $2)
            ON CONFLICT (memory_id, session_id) DO NOTHING
            """,
            memory_id,
            session_id,
        )
    return result.endswith("1")


async def count_distinct_confirmations(memory_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT session_id)
            FROM memory_confirmations
            WHERE memory_id = $1
            """,
            memory_id,
        )
    return int(count or 0)


async def get_first_confirmation_time(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT MIN(confirmed_at) FROM memory_confirmations WHERE memory_id = $1",
            memory_id,
        )


async def upsert_session_summary(
    session_id: str,
    summary: str,
    mood: Optional[str] = None,
    topic_tags: Optional[Sequence[str]] = None,
    msg_count: Optional[int] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO session_summaries (session_id, summary, mood, topic_tags, msg_count)
            VALUES ($1, $2, $3, $4::text[], $5)
            ON CONFLICT (session_id) DO UPDATE SET
              summary = EXCLUDED.summary,
              mood = EXCLUDED.mood,
              topic_tags = EXCLUDED.topic_tags,
              msg_count = EXCLUDED.msg_count,
              updated_at = NOW()
            """,
            session_id,
            summary,
            mood,
            list(topic_tags) if topic_tags else None,
            msg_count,
        )


async def has_session_summary(session_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM session_summaries WHERE session_id = $1",
            session_id,
        )
    return bool(exists)


async def get_recent_session_summaries(limit: int = 2):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, session_id, summary, mood, topic_tags, msg_count, created_at, updated_at
            FROM session_summaries
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )


async def get_latest_summary_time():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT MAX(created_at) FROM session_summaries")


async def create_open_loop(content: str, loop_type: str = "promise", source_session: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT id
            FROM open_loops
            WHERE content = $1 AND status = 'open'
            LIMIT 1
            """,
            content,
        )
        if existing:
            return existing

        row = await conn.fetchrow(
            """
            INSERT INTO open_loops (content, loop_type, source_session)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            content,
            loop_type,
            source_session,
        )
    return row["id"] if row else None


async def get_open_loops(status: str = "open", limit: Optional[int] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if limit is None:
            return await conn.fetch(
                """
                SELECT id, content, loop_type, source_session, status, resolved_at, created_at
                FROM open_loops
                WHERE status = $1
                ORDER BY created_at DESC
                """,
                status,
            )
        return await conn.fetch(
            """
            SELECT id, content, loop_type, source_session, status, resolved_at, created_at
            FROM open_loops
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status,
            limit,
        )


async def resolve_open_loops(loop_ids: Sequence[int]):
    ids = [loop_id for loop_id in loop_ids if loop_id]
    if not ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE open_loops
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = ANY($1::int[]) AND status = 'open'
            """,
            ids,
        )


async def expire_old_open_loops(days: int = 14):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE open_loops
            SET status = 'expired'
            WHERE status = 'open'
              AND created_at < NOW() - make_interval(days => $1)
            """,
            days,
        )


async def get_all_memories_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def get_all_memories_detail():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id, content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def update_memory(
    memory_id: int,
    content: Optional[str] = None,
    importance: Optional[int] = None,
    tier: Optional[str] = None,
    status: Optional[str] = None,
    canonical_key: Optional[str] = None,
    manual_locked: Optional[bool] = None,
    pending_review: Optional[bool] = None,
    replaced_by_id: Optional[int] = None,
    valid_until: Optional[datetime] = None,
):
    updates = []
    params: list[object] = []

    def add(field_sql: str, value: object):
        params.append(value)
        updates.append(f"{field_sql} = ${len(params)}")

    if content is not None:
        add("content", content)
    if importance is not None:
        add("importance", importance)
    if tier is not None:
        add("tier", tier)
        updates[-1] += "::memory_tier"
    if status is not None:
        add("status", status)
        updates[-1] += "::memory_status"
    if canonical_key is not None:
        add("canonical_key", canonical_key)
    if manual_locked is not None:
        add("manual_locked", manual_locked)
    if pending_review is not None:
        add("pending_review", pending_review)
    if replaced_by_id is not None:
        add("replaced_by_id", replaced_by_id)
    if valid_until is not None:
        add("valid_until", valid_until)

    if not updates:
        return

    params.append(memory_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ${len(params)}",
            *params,
        )


async def delete_memory(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)


async def delete_memories_batch(memory_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = ANY($1::int[])", memory_ids)


# ============================================================
# Memory Bank
# ============================================================

def _normalize_tags(tags: Optional[Sequence[str]]) -> list[str]:
    if not tags:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = str(raw or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.12g}" for v in values) + "]"


def _memory_bank_select_clause() -> str:
    return """
        id, title, category, tags, content,
        always_load, enabled, sort_order,
        source_ref, notes, content_hash,
        created_at, updated_at,
        FALSE AS has_embedding
    """


def _memory_bank_row_to_dict(row: asyncpg.Record | dict) -> dict:
    item = dict(row)
    tags = item.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    item["tags"] = [str(tag) for tag in (tags or [])]
    item["has_embedding"] = bool(item.get("has_embedding"))
    return item


async def get_memory_bank_items(
    *,
    category: str | None = None,
    always_load: bool | None = None,
    enabled_only: bool = True,
) -> list[dict]:
    filters: list[str] = []
    params: list[object] = []

    if enabled_only:
        filters.append("enabled = TRUE")
    if category:
        params.append(category)
        filters.append(f"category = ${len(params)}")
    if always_load is not None:
        params.append(always_load)
        filters.append(f"always_load = ${len(params)}")

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"""
        SELECT {_memory_bank_select_clause()}
        FROM memory_bank
        {where_clause}
        ORDER BY always_load DESC, sort_order ASC, updated_at DESC, id DESC
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_memory_bank_row_to_dict(row) for row in rows]


async def get_memory_bank_item(item_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_memory_bank_select_clause()}
            FROM memory_bank
            WHERE id = $1
            """,
            item_id,
        )
    return _memory_bank_row_to_dict(row) if row else None


async def get_always_load_items() -> list[dict]:
    return await get_memory_bank_items(always_load=True, enabled_only=True)


async def search_memory_bank_by_tags(keywords: list[str], limit: int = 3) -> list[dict]:
    tags = _normalize_tags(keywords)
    if not tags or limit <= 0:
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                {_memory_bank_select_clause()},
                hits.hit_count
            FROM memory_bank AS mb
            CROSS JOIN LATERAL (
                SELECT COUNT(*)::int AS hit_count
                FROM jsonb_array_elements_text(mb.tags) AS t(tag)
                WHERE t.tag = ANY($1::text[])
            ) AS hits
            WHERE mb.enabled = TRUE
              AND mb.always_load = FALSE
              AND mb.tags ?| $1::text[]
            ORDER BY hits.hit_count DESC, mb.sort_order ASC, mb.updated_at DESC, mb.id DESC
            LIMIT $2
            """,
            tags,
            limit,
        )
    return [_memory_bank_row_to_dict(row) for row in rows]


async def create_memory_bank_item(
    title: str,
    content: str,
    category: str = "general",
    tags: list[str] | None = None,
    always_load: bool = False,
    enabled: bool = True,
    sort_order: int = 0,
    source_ref: str | None = None,
    notes: str | None = None,
    content_hash: str | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO memory_bank (
                title, category, tags, content, always_load, enabled,
                sort_order, source_ref, notes, content_hash
            )
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10)
            RETURNING {_memory_bank_select_clause()}
            """,
            title,
            category,
            json.dumps(_normalize_tags(tags), ensure_ascii=False),
            content,
            always_load,
            enabled,
            sort_order,
            source_ref,
            notes,
            content_hash,
        )
    return _memory_bank_row_to_dict(row)


async def update_memory_bank_item(item_id: int, **fields) -> dict | None:
    updates: list[str] = []
    params: list[object] = []

    def add(field_name: str, value: object, cast: str = ""):
        params.append(value)
        updates.append(f"{field_name} = ${len(params)}{cast}")

    if "title" in fields:
        add("title", fields["title"])
    if "category" in fields:
        add("category", fields["category"])
    if "tags" in fields:
        add("tags", json.dumps(_normalize_tags(fields["tags"]), ensure_ascii=False), "::jsonb")
    if "content" in fields:
        add("content", fields["content"])
    if "always_load" in fields:
        add("always_load", fields["always_load"])
    if "enabled" in fields:
        add("enabled", fields["enabled"])
    if "sort_order" in fields:
        add("sort_order", fields["sort_order"])
    if "source_ref" in fields:
        add("source_ref", fields["source_ref"])
    if "notes" in fields:
        add("notes", fields["notes"])
    if "content_hash" in fields:
        add("content_hash", fields["content_hash"])

    if not updates:
        return await get_memory_bank_item(item_id)

    updates.append("updated_at = NOW()")
    params.append(item_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE memory_bank
            SET {', '.join(updates)}
            WHERE id = ${len(params)}
            RETURNING {_memory_bank_select_clause()}
            """,
            *params,
        )
    return _memory_bank_row_to_dict(row) if row else None


async def delete_memory_bank_item(item_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM memory_bank WHERE id = $1", item_id)
    return result.endswith("1")


async def search_memory_bank_by_vector(
    query_embedding: list[float],
    limit: int = 3,
    exclude_ids: list[int] | None = None,
) -> list[dict]:
    if not query_embedding or limit <= 0:
        return []

    filters = [
        "enabled = TRUE",
        "always_load = FALSE",
        "embedding IS NOT NULL",
    ]
    params: list[object] = [_vector_literal(query_embedding)]
    if exclude_ids:
        params.append(list(exclude_ids))
        filters.append(f"NOT (id = ANY(${len(params)}::int[]))")
    params.append(limit)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                {_memory_bank_select_clause()},
                embedding <=> $1::vector AS distance
            FROM memory_bank
            WHERE {' AND '.join(filters)}
            ORDER BY distance ASC, sort_order ASC, updated_at DESC, id DESC
            LIMIT ${len(params)}
            """,
            *params,
        )
    return [_memory_bank_row_to_dict(row) for row in rows]


async def update_embedding(item_id: int, embedding: Sequence[float] | None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if embedding is None:
            await conn.execute(
                """
                UPDATE memory_bank
                SET embedding = NULL
                WHERE id = $1
                """,
                item_id,
            )
            return
        await conn.execute(
            """
            UPDATE memory_bank
            SET embedding = $2::vector
            WHERE id = $1
            """,
            item_id,
            _vector_literal(embedding),
        )


# ============================================================
# Recent messages for context (hard floor / checkpoint)
# ============================================================

async def get_recent_messages_for_context(session_id: str, limit: int = 10) -> list[dict]:
    """取 session 最近 N 則 user/assistant 訊息，供 packer hard floor 補底用。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content
            FROM conversations
            WHERE session_id = $1
              AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT $2
            """,
            session_id, limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ============================================================
# Extraction cursor — extractor 從 DB 讀 raw turns 用
# ============================================================

async def get_unextracted_messages(session_id: str, limit: int = 40) -> list[dict]:
    """從 DB 撈 cursor 之後尚未提取的 raw turns，給 extractor 用。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        cursor = await conn.fetchval(
            "SELECT last_extracted_message_id FROM session_state WHERE session_id = $1",
            session_id,
        ) or 0
        rows = await conn.fetch(
            """
            SELECT id, role, content
            FROM conversations
            WHERE session_id = $1
              AND id > $2
              AND role IN ('user', 'assistant')
            ORDER BY id ASC
            LIMIT $3
            """,
            session_id, cursor, limit,
        )
    return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]


async def count_unextracted_messages(session_id: str) -> int:
    """計算 cursor 之後尚未提取的 raw turns 數量。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        cursor = await conn.fetchval(
            "SELECT last_extracted_message_id FROM session_state WHERE session_id = $1",
            session_id,
        ) or 0
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM conversations
            WHERE session_id = $1 AND id > $2 AND role IN ('user', 'assistant')
            """,
            session_id, cursor,
        )
    return int(count or 0)


async def update_extract_cursor(session_id: str, message_id: int) -> None:
    """提取完成後更新 cursor。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO session_state (session_id, last_extracted_message_id, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (session_id) DO UPDATE
              SET last_extracted_message_id = EXCLUDED.last_extracted_message_id,
                  updated_at = NOW()
            """,
            session_id, message_id,
        )


# ============================================================
# Checkpoint CRUD — 對話壓縮 checkpoint
# ============================================================


async def get_active_checkpoint(session_id: str) -> Optional[dict]:
    """取當前 session 的 active checkpoint，無則返回 None。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, session_id, version, summary_text, covers_until_msg_id,
                   parent_checkpoint_id, is_active, token_count, created_at
            FROM conversation_checkpoints
            WHERE session_id = $1 AND is_active = TRUE
            """,
            session_id,
        )
    return dict(row) if row else None


async def get_last_session_message_id(session_id: str) -> int:
    """取 session 最後一條已儲存的 user/assistant 訊息 id。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg_id = await conn.fetchval(
            """
            SELECT MAX(id) FROM conversations
            WHERE session_id = $1 AND role IN ('user', 'assistant')
            """,
            session_id,
        )
    return int(msg_id or 0)


async def deactivate_old_checkpoints(session_id: str) -> Optional[int]:
    """停用舊 checkpoint，返回舊 checkpoint 的 id（供新 checkpoint 填 parent_checkpoint_id）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        old_id = await conn.fetchval(
            "SELECT id FROM conversation_checkpoints WHERE session_id = $1 AND is_active = TRUE",
            session_id,
        )
        if old_id:
            await conn.execute(
                "UPDATE conversation_checkpoints SET is_active = FALSE WHERE session_id = $1 AND is_active = TRUE",
                session_id,
            )
    return old_id


async def insert_checkpoint(
    session_id: str,
    version: int,
    summary_text: str,
    covers_until_msg_id: int,
    parent_checkpoint_id: Optional[int] = None,
) -> int:
    """插入新 checkpoint，返回新 id。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversation_checkpoints
                (session_id, version, summary_text, covers_until_msg_id, parent_checkpoint_id, is_active)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            RETURNING id
            """,
            session_id,
            version,
            summary_text,
            covers_until_msg_id,
            parent_checkpoint_id,
        )
    return row["id"] if row else 0


async def get_messages_for_compression(session_id: str, from_msg_id: int = 0) -> list[dict]:
    """取從 from_msg_id 之後的所有 user/assistant 訊息，用來生成 checkpoint 摘要。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, content
            FROM conversations
            WHERE session_id = $1
              AND id > $2
              AND role IN ('user', 'assistant')
            ORDER BY id ASC
            """,
            session_id, from_msg_id,
        )
    return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]


async def get_messages_after_checkpoint(
    session_id: str, covers_until_msg_id: int, limit: int = 60
) -> list[dict]:
    """取 checkpoint 之後的 raw turns，用於 packer 注入 context（不含當前 user message）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content
            FROM conversations
            WHERE session_id = $1
              AND id > $2
              AND role IN ('user', 'assistant')
            ORDER BY id ASC
            LIMIT $3
            """,
            session_id, covers_until_msg_id, limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# ============================================================
# 世界書（persona_entries）CRUD
# ============================================================


async def get_all_persona_entries() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, content, keywords, position, always_on, enabled, priority, created_at, updated_at
            FROM persona_entries
            ORDER BY position ASC, priority ASC, id ASC
            """
        )
    return [dict(r) for r in rows]


async def get_persona_entry(entry_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM persona_entries WHERE id = $1", entry_id
        )
    return dict(row) if row else None


async def create_persona_entry(
    title: str,
    content: str,
    keywords: str = "",
    position: int = 1,
    always_on: bool = False,
    enabled: bool = True,
    priority: int = 50,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO persona_entries (title, content, keywords, position, always_on, enabled, priority)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            title, content, keywords, position, always_on, enabled, priority,
        )
    return dict(row)


async def update_persona_entry(entry_id: int, **fields) -> dict | None:
    allowed = {"title", "content", "keywords", "position", "always_on", "enabled", "priority"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_persona_entry(entry_id)
    set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
    values = list(updates.values())
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE persona_entries
            SET {set_clauses}, updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            entry_id, *values,
        )
    return dict(row) if row else None


async def delete_persona_entry(entry_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM persona_entries WHERE id = $1", entry_id
        )
    return result == "DELETE 1"


async def toggle_persona_entry(entry_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE persona_entries
            SET enabled = NOT enabled, updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            entry_id,
        )
    return dict(row) if row else None


async def reorder_persona_entries(items: list[dict]) -> None:
    """items: list of {id: int, priority: int}"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for item in items:
            await conn.execute(
                "UPDATE persona_entries SET priority = $1, updated_at = NOW() WHERE id = $2",
                item["priority"], item["id"],
            )


async def get_enabled_persona_entries() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, content, keywords, position, always_on, priority
            FROM persona_entries
            WHERE enabled = TRUE
            ORDER BY position ASC, priority ASC
            """
        )
    return [dict(r) for r in rows]


async def get_random_memories(
    limit: int = 3,
    tiers: list[str] | None = None,
    exclude_ids: list[int] | None = None,
) -> list[dict]:
    """
    撈隨機記憶，用 importance 加權（importance 高的更容易被撈到）。
    排除 exclude_ids 避免跟精準匹配重複。
    """
    pool = await get_pool()
    filters = ["status = 'active'"]
    params: list[object] = []

    if tiers:
        params.append(list(tiers))
        filters.append(f"tier::text = ANY(${len(params)}::text[])")
    if exclude_ids:
        params.append(list(exclude_ids))
        filters.append(f"NOT (id = ANY(${len(params)}::int[]))")

    params.append(limit)
    sql = f"""
        SELECT id, content, importance, tier, created_at
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY RANDOM() * (importance + 1) DESC
        LIMIT ${len(params)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ============================================================
# 對話向量化（RAG）
# ============================================================

async def get_sessions_to_vectorize(days_old: int = 7) -> list[str]:
    """找出所有訊息都超過 N 天、且尚未向量化的 session_ids"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT DISTINCT c.session_id
            FROM conversations c
            WHERE c.role IN ('user', 'assistant')
              AND c.created_at < NOW() - INTERVAL '{days_old} days'
              AND c.session_id NOT IN (
                  SELECT DISTINCT session_id FROM conversation_vectors
              )
            """
        )
    return [row["session_id"] for row in rows]


async def get_messages_for_session_vectorize(session_id: str) -> list[dict]:
    """取得一個 session 的所有 user/assistant 訊息（按時間排序）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, content, created_at
            FROM conversations
            WHERE session_id = $1
              AND role IN ('user', 'assistant')
            ORDER BY id ASC
            """,
            session_id,
        )
    return [dict(r) for r in rows]


async def save_conversation_vector(
    session_id: str,
    chunk_text: str,
    embedding: list[float],
    message_ids: list[int],
    days_old: float,
) -> None:
    """儲存一段對話的向量"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversation_vectors
                (session_id, chunk_text, embedding, message_ids, days_old_at_vectorize)
            VALUES ($1, $2, $3::vector, $4, $5)
            """,
            session_id,
            chunk_text,
            _vector_literal(embedding),
            message_ids,
            days_old,
        )


async def search_conversation_vectors(
    query_embedding: list[float],
    limit: int = 3,
) -> list[dict]:
    """向量相似搜尋，回傳最相關的對話片段"""
    if not query_embedding or limit <= 0:
        return []
    pool = await get_pool()
    params: list[object] = [_vector_literal(query_embedding), limit]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT chunk_text, session_id, days_old_at_vectorize,
                   (embedding <=> $1::vector) AS distance
            FROM conversation_vectors
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def count_vectorize_status(days_old: int = 7) -> dict:
    """回傳向量化進度統計"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vectorized = await conn.fetchval(
            "SELECT COUNT(DISTINCT session_id) FROM conversation_vectors"
        )
        pending = await conn.fetchval(
            f"""
            SELECT COUNT(DISTINCT c.session_id)
            FROM conversations c
            WHERE c.role IN ('user', 'assistant')
              AND c.created_at < NOW() - INTERVAL '{days_old} days'
              AND c.session_id NOT IN (
                  SELECT DISTINCT session_id FROM conversation_vectors
              )
            """
        )
        total_chunks = await conn.fetchval(
            "SELECT COUNT(*) FROM conversation_vectors"
        )
    return {
        "vectorized_sessions": int(vectorized or 0),
        "pending_sessions": int(pending or 0),
        "total_chunks": int(total_chunks or 0),
        "days_threshold": days_old,
    }
