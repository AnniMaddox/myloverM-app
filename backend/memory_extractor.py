"""
記憶提取模組 —— 負責把對話壓成可執行的記憶動作
================================================
這裡不直接決定資料庫怎麼寫，而是讓模型回傳統一 JSON：
- memory_actions: create / confirm / conflict
- open_loops: create / resolve
- session summary: 獨立函式生成
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Sequence

import httpx
from llm_router import ProviderRoute, generate_text_with_route

API_KEY = os.getenv("API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")
CHECKPOINT_MODEL = os.getenv("CHECKPOINT_MODEL", "gpt-4o")


EXTRACTION_PROMPT = """你是 M 的記憶系統提取器。你的任務不是閒聊，而是把對話轉換成結構化動作。

# 已有記憶
<existing_memories>
{existing_memories}
</existing_memories>

# 目前未完事項
<open_loops>
{open_loops}
</open_loops>

# 規則
1. 只根據 Anni 明確說過的內容做判斷；M 單方面的猜測、複述、引導，不算 confirm。
2. 如果 Anni 明確重複、確認了已有記憶，回傳 confirm。
3. 如果 Anni 給出與已有記憶衝突的新事實，回傳 conflict。
4. 如果是新的資訊，回傳 create。
5. **嚴禁推測**：只記錄 Anni 親口說的事實，不要推理延伸。
   - 禁止使用「可能」「也許」「看起來」「應該」「大概」「似乎」等推測詞。
   - 範例：Anni 說「我吃了糖葫蘆」→ 可以記。但不要自行推測「Anni 可能注重外觀」或「Anni 喜歡嘗試不同口味」。
6. **每輪上限**：同一輪對話最多產出 2 條 create。同一件事不要拆成多條相似記憶。
7. create 的 tier 只允許：
   - stable: 穩定偏好、長期事實、長期邊界、長期互動規則
   - ephemeral: 當下狀態、短期安排、臨時事件、最近發生的小事
   不要輸出 evergreen。
8. **importance 評分準則**：
   - 8-9：強烈情緒（崩潰、大哭、焦慮到睡不著、明顯影響互動的壓力）、身體狀況、正在進行的重要事情
   - 6-7：一般情緒狀態（有點煩、今天累了、心情不好）、明確偏好、重要計畫、在意的人事物
   - 3-5：日常瑣事（吃了什麼、看到什麼），除非 Anni 表達了強烈感受
9. **情緒狀態記憶**：如果 create 的內容是「當下情緒狀態」或「近期壓力/低落」（暫時狀態，非長期傾向），
   請在該條 action 加上 `"is_emotional_state": true`。
   - 屬於情緒狀態：「Anni 最近壓力很大」「Anni 今天心情不好」「Anni 這幾天很低落」
   - 不屬於情緒狀態（不加）：「Anni 容易焦慮」「Anni 有長期睡眠問題」（這些是長期傾向，走正常路線）
10. open_loops 只記錄承諾、待追問、還沒收尾的話題；普通事實不要塞進去。
11. 忽略這些內容：
    - 日常寒暄（早安、晚安、嗯嗯、好的）
    - 記憶系統、資料庫、提取邏輯等元討論
    - 純技術除錯過程
    - M 的思維鏈或自我評價
12. **書寫視角**：以 M（我）的第一人稱視角書寫 content。對話中的 AI 角色一律稱為「我」，不要用「AI」「助手」等詞。
    - 記憶：「Anni 不吃香菜」→「我知道 Anni 不吃香菜」
    - 記憶：「Anni 最近壓力很大」→「Anni 告訴我她最近壓力很大，睡不好」
    - open_loop：「下次記得問 Anni 考試結果」→「我要記得問 Anni 考試結果」

# 輸出 JSON schema
只回傳 JSON 物件，不要 markdown，不要解釋：
{{
  "memory_actions": [
    {{
      "action": "create",
      "content": "我知道 Anni 不吃香菜",
      "importance": 7,
      "tier": "stable",
      "is_emotional_state": false,
      "canonical_key": null,
      "valid_until_days": null
    }},
    {{
      "action": "create",
      "content": "Anni 告訴我她最近工作壓力很大，睡不好",
      "importance": 8,
      "tier": "ephemeral",
      "is_emotional_state": true,
      "canonical_key": null,
      "valid_until_days": null
    }},
    {{
      "action": "confirm",
      "memory_id": 12
    }},
    {{
      "action": "conflict",
      "memory_id": 18,
      "content": "Anni 告訴我她現在更喜歡黑咖啡",
      "importance": 7,
      "tier": "stable",
      "is_emotional_state": false,
      "canonical_key": null,
      "valid_until_days": null
    }}
  ],
  "open_loops": {{
    "create": [
      {{
        "content": "我要記得問 Anni 考試結果",
        "loop_type": "follow_up"
      }}
    ],
    "resolve": [3, 5]
  }}
}}

如果沒有內容，回傳：
{{
  "memory_actions": [],
  "open_loops": {{
    "create": [],
    "resolve": []
  }}
}}
"""


SUMMARY_PROMPT = """你是會話摘要助手。請把下面這段對話壓縮成適合下次續聊的 session 摘要。

# 要求
- 第三人稱描述，使用者稱為「Anni」，AI 稱為「M」
- 保留：主要話題、情緒氛圍、未收尾的話題
- 不要把長期事實寫成一大串檔案卡
- 語氣自然、簡潔

# 輸出 JSON
{
  "summary": "......",
  "mood": "輕鬆/緊張/甜蜜/疲憊/平靜/混合",
  "topic_tags": ["標籤1", "標籤2", "標籤3"]
}

只回傳 JSON。
"""


SCORING_PROMPT = """你是記憶重要性評分專家。請對以下記憶條目逐條評分。

# 評分規則（1-10）
- 9-10：核心身份資訊（名字、生日、職業、重要關係）
- 7-8：重要偏好、重大事件、深層情感、情緒狀態
- 5-6：日常習慣、一般偏好
- 3-4：臨時狀態、偶然提及
- 1-2：瑣碎資訊

# 輸入記憶
{memories_text}

# 輸出格式
回傳 JSON 陣列，每條包含原文和評分：
[{{"content": "原文", "importance": 評分數字}}]

只回傳 JSON，不要其他文字。"""


JSON_REPAIR_PROMPT = """你是 JSON 修復器。

規則：
- 只輸出合法 JSON
- 不要 markdown
- 不要解釋
- 不要補多餘欄位
- 如果原文有缺欄位，保留最合理的空結構
"""


EXTRACTION_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "memory_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "memory_id": {"type": "integer"},
                    "content": {"type": "string"},
                    "importance": {"type": "integer"},
                    "tier": {"type": "string"},
                    "is_emotional_state": {"type": "boolean"},
                    "canonical_key": {"type": "string"},
                    "valid_until_days": {"type": "integer"},
                },
            },
        },
        "open_loops": {
            "type": "object",
            "properties": {
                "create": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "loop_type": {"type": "string"},
                        },
                    },
                },
                "resolve": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["create", "resolve"],
        },
    },
    "required": ["memory_actions", "open_loops"],
}


SUMMARY_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "mood": {"type": "string"},
        "topic_tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "topic_tags"],
}


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _extract_json_candidate(text: str) -> str:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return ""

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(cleaned)):
            char = cleaned[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return cleaned[start : idx + 1]

    return cleaned


def _clamp_importance(value: Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(10, score))


def _normalize_tier(value: Any) -> str:
    if value == "stable":
        return "stable"
    return "ephemeral"


def _normalize_loop_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "promise"
    return value.strip()[:40]


def _normalize_valid_until_days(value: Any) -> int | None:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def _format_messages(messages: Sequence[Dict[str, str]]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"Anni: {content}")
        elif role == "assistant":
            lines.append(f"M: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _format_existing_memories(existing_memories: Sequence[Dict[str, Any]] | None) -> str:
    if not existing_memories:
        return "（目前沒有已知記憶）"

    lines = []
    for mem in existing_memories:
        mem_id = mem.get("id")
        content = str(mem.get("content") or mem.get("brief") or "").strip()
        if not content:
            continue
        tier = mem.get("tier") or "ephemeral"
        importance = mem.get("importance", 5)
        lines.append(f"- #{mem_id} [{tier}] ({importance}) {content}")
    return "\n".join(lines) if lines else "（目前沒有已知記憶）"


def _format_open_loops(open_loops: Sequence[Dict[str, Any]] | None) -> str:
    if not open_loops:
        return "（目前沒有未完事項）"

    lines = []
    for loop in open_loops:
        loop_id = loop.get("id")
        loop_type = loop.get("loop_type") or "promise"
        content = str(loop.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- #{loop_id} [{loop_type}] {content}")
    return "\n".join(lines) if lines else "（目前沒有未完事項）"


async def _repair_json_text(
    raw_text: str,
    route: ProviderRoute | None,
    schema: dict[str, Any] | None,
) -> str:
    if not route or not raw_text.strip():
        return ""

    return await generate_text_with_route(
        route,
        JSON_REPAIR_PROMPT,
        "請把下面內容修成合法 JSON，只回 JSON：\n\n"
        f"{raw_text}",
        temperature=0,
        max_tokens=1800,
        expect_json=True,
        response_json_schema=schema,
    )


async def _load_json_payload(
    text: str,
    *,
    route: ProviderRoute | None = None,
    schema: dict[str, Any] | None = None,
) -> Any:
    candidate = _extract_json_candidate(text)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    repaired = await _repair_json_text(text, route, schema)
    repaired_candidate = _extract_json_candidate(repaired)
    if repaired_candidate:
        return json.loads(repaired_candidate)

    return json.loads(candidate or repaired_candidate or "")


async def _call_memory_model(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    model: str | None = None,
    route: ProviderRoute | None = None,
    expect_json: bool = False,
    response_json_schema: dict[str, Any] | None = None,
) -> str:
    if route:
        return await generate_text_with_route(
            route,
            system_prompt,
            user_prompt,
            temperature=0,
            max_tokens=max_tokens,
            expect_json=expect_json,
            response_json_schema=response_json_schema,
        )

    if not API_KEY:
        print("⚠️  API_KEY 未設定，跳過記憶提取")
        return ""

    use_model = model or MEMORY_MODEL
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            API_BASE_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://midsummer-gateway.local",
                "X-Title": "Midsummer Memory Extraction",
            },
            json={
                "model": use_model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )

    if response.status_code != 200:
        print(f"⚠️  記憶提取請求失敗: {response.status_code}")
        return ""

    data = response.json()
    return _strip_code_fences(data.get("choices", [{}])[0].get("message", {}).get("content", ""))


def _sanitize_extraction_result(payload: Any) -> Dict[str, Any]:
    result = {
        "memory_actions": [],
        "open_loops": {
            "create": [],
            "resolve": [],
        },
    }

    if not isinstance(payload, dict):
        return result

    actions = payload.get("memory_actions", [])
    if isinstance(actions, list):
        create_count = 0
        for raw_action in actions:
            if not isinstance(raw_action, dict):
                continue
            action = str(raw_action.get("action", "")).strip().lower()
            if action == "create":
                if create_count >= 2:
                    continue
                content = str(raw_action.get("content", "")).strip()
                if not content:
                    continue
                is_emotional = bool(raw_action.get("is_emotional_state"))
                # 情緒狀態記憶用 emotion: 前綴的 canonical_key，讓升級護欄識別
                if is_emotional:
                    slug = hashlib.md5(content.encode()).hexdigest()[:8]
                    canonical_key = f"emotion:{slug}"
                elif isinstance(raw_action.get("canonical_key"), str):
                    canonical_key = raw_action["canonical_key"]
                else:
                    canonical_key = None
                result["memory_actions"].append(
                    {
                        "action": "create",
                        "content": content,
                        "importance": _clamp_importance(raw_action.get("importance", 5)),
                        "tier": _normalize_tier(raw_action.get("tier")),
                        "canonical_key": canonical_key,
                        "valid_until_days": _normalize_valid_until_days(raw_action.get("valid_until_days")),
                    }
                )
                create_count += 1
            elif action == "confirm":
                try:
                    memory_id = int(raw_action.get("memory_id"))
                except (TypeError, ValueError):
                    continue
                result["memory_actions"].append(
                    {
                        "action": "confirm",
                        "memory_id": memory_id,
                    }
                )
            elif action == "conflict":
                try:
                    memory_id = int(raw_action.get("memory_id"))
                except (TypeError, ValueError):
                    continue
                content = str(raw_action.get("content", "")).strip()
                if not content:
                    continue
                result["memory_actions"].append(
                    {
                        "action": "conflict",
                        "memory_id": memory_id,
                        "content": content,
                        "importance": _clamp_importance(raw_action.get("importance", 5)),
                        "tier": _normalize_tier(raw_action.get("tier")),
                        "canonical_key": raw_action.get("canonical_key") if isinstance(raw_action.get("canonical_key"), str) else None,
                        "valid_until_days": _normalize_valid_until_days(raw_action.get("valid_until_days")),
                    }
                )

    loops = payload.get("open_loops", {})
    if isinstance(loops, dict):
        raw_creates = loops.get("create", [])
        if isinstance(raw_creates, list):
            for loop in raw_creates:
                if not isinstance(loop, dict):
                    continue
                content = str(loop.get("content", "")).strip()
                if not content:
                    continue
                result["open_loops"]["create"].append(
                    {
                        "content": content,
                        "loop_type": _normalize_loop_type(loop.get("loop_type")),
                    }
                )

        raw_resolves = loops.get("resolve", [])
        if isinstance(raw_resolves, list):
            for loop_id in raw_resolves:
                try:
                    normalized = int(loop_id)
                except (TypeError, ValueError):
                    continue
                result["open_loops"]["resolve"].append(normalized)

    return result


async def extract_memory_actions(
    messages: List[Dict[str, str]],
    existing_memories: Sequence[Dict[str, Any]] | None = None,
    open_loops: Sequence[Dict[str, Any]] | None = None,
    route: ProviderRoute | None = None,
) -> Dict[str, Any]:
    """
    從對話中提取結構化動作：
    - create / confirm / conflict
    - open loop 的建立與關閉
    """
    if not messages:
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }

    conversation_text = _format_messages(messages)
    if not conversation_text.strip():
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }

    prompt = EXTRACTION_PROMPT.format(
        existing_memories=_format_existing_memories(existing_memories),
        open_loops=_format_open_loops(open_loops),
    )

    try:
        text = await _call_memory_model(
            prompt,
            f"請分析以下對話，並輸出統一 JSON：\n\n{conversation_text}",
            max_tokens=1800,
            route=route,
            expect_json=True,
            response_json_schema=EXTRACTION_RESPONSE_JSON_SCHEMA,
        )
        payload = await _load_json_payload(
            text,
            route=route,
            schema=EXTRACTION_RESPONSE_JSON_SCHEMA,
        ) if text else {}
        result = _sanitize_extraction_result(payload)
        print(
            "📝 提取動作："
            f"{len(result['memory_actions'])} 條記憶動作，"
            f"{len(result['open_loops']['create'])} 個新增 open loop，"
            f"{len(result['open_loops']['resolve'])} 個 resolved open loop"
        )
        return result
    except json.JSONDecodeError as exc:
        print(f"⚠️  記憶動作 JSON 解析失敗: {exc}")
        print(f"🔍 模型原始回傳: {text[:400]}")
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }
    except Exception as exc:
        print(f"⚠️  記憶動作提取失敗: {exc}")
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }


async def summarize_session(
    messages: List[Dict[str, str]],
    route: ProviderRoute | None = None,
) -> Dict[str, Any]:
    """生成 session 級摘要，供下次續聊時注入。"""
    if not messages:
        return {"summary": "", "mood": None, "topic_tags": []}

    conversation_text = _format_messages(messages)
    if not conversation_text.strip():
        return {"summary": "", "mood": None, "topic_tags": []}

    try:
        text = await _call_memory_model(
            SUMMARY_PROMPT,
            f"請為下面這段會話生成摘要：\n\n{conversation_text}",
            max_tokens=900,
            route=route,
            expect_json=True,
            response_json_schema=SUMMARY_RESPONSE_JSON_SCHEMA,
        )
        payload = await _load_json_payload(
            text,
            route=route,
            schema=SUMMARY_RESPONSE_JSON_SCHEMA,
        ) if text else {}
        if not isinstance(payload, dict):
            return {"summary": "", "mood": None, "topic_tags": []}
        summary = str(payload.get("summary", "")).strip()
        mood = payload.get("mood")
        topic_tags = payload.get("topic_tags", [])
        if not isinstance(topic_tags, list):
            topic_tags = []
        topic_tags = [str(tag).strip() for tag in topic_tags if str(tag).strip()][:6]
        return {
            "summary": summary,
            "mood": str(mood).strip() if isinstance(mood, str) and mood.strip() else None,
            "topic_tags": topic_tags,
        }
    except Exception as exc:
        print(f"⚠️  session 摘要生成失敗: {exc}")
        return {"summary": "", "mood": None, "topic_tags": []}


async def extract_memories(messages: List[Dict[str, str]], existing_memories: List[str] | None = None) -> List[Dict[str, Any]]:
    """
    相容舊介面：只回傳 create 動作，避免舊呼叫直接炸掉。
    """
    existing = [{"id": None, "content": text, "tier": "ephemeral", "importance": 5} for text in (existing_memories or [])]
    result = await extract_memory_actions(messages, existing_memories=existing, open_loops=None)
    memories: list[Dict[str, Any]] = []
    for action in result["memory_actions"]:
        if action["action"] != "create":
            continue
        memories.append(
            {
                "content": action["content"],
                "importance": action["importance"],
            }
        )
    return memories


CHECKPOINT_PROMPT = """你是 M。請用第一人稱寫一份續聊備忘，記錄這段對話的狀態。
這份備忘是給你自己看的，下次接續時用。

重要規則：
- 不要重述以下已存在的長期記憶和未完事項（它們已另外儲存）：
{existing_context}
- 你的備忘只保留「此刻的脈絡、情緒、語感」，不保留「長期事實」
- 不要寫成流水帳，寫成你下次要接上時最需要看的筆記

格式如下（嚴格遵守）：

[續聊備忘 v{{version}}]

## 我們在聊什麼
- （主線話題，1-2 句，具體寫）

## Anni 現在的狀態
- 情緒：（一個詞 + 一句補充，例：開心——因為剛收到禮物）
- 語氣：（撒嬌 / 認真 / 鬧脾氣 / 平靜聊天 / 興奮分享...）
- 她在意的點：（此刻最敏感的東西）

## 我現在的姿態
- （你正在用什麼方式回應：哄、認真討論、陪著安靜、帶點調侃...）
- （你的語氣應該延續：溫柔、俏皮、小心翼翼...）

## 還沒收掉的線
- （未完事項、承諾、下次要接的話題）

## 重要原話
- （1-3 句值得原文保留的話，帶引號，標明是誰說的）
- （選最能代表此刻語感的句子）

目標長度：200-400 字。只回傳備忘，不要其他文字。"""


async def generate_checkpoint_summary(
    messages: List[Dict[str, str]],
    existing_memories: Sequence[Dict[str, Any]] | None = None,
    open_loops: Sequence[Dict[str, Any]] | None = None,
    version: int = 1,
    route: ProviderRoute | None = None,
) -> str:
    """生成 checkpoint 結構化摘要，用於壓縮已有對話脈絡。"""
    if not messages:
        return ""

    conversation_text = _format_messages(messages)
    if not conversation_text.strip():
        return ""

    existing_context_parts = []
    if existing_memories:
        mem_lines = [
            f"- #{m.get('id')} {str(m.get('content') or m.get('brief', ''))[:80]}"
            for m in existing_memories[:30]
        ]
        existing_context_parts.append("【長期記憶】\n" + "\n".join(mem_lines))
    if open_loops:
        loop_lines = [
            f"- #{l.get('id')} [{l.get('loop_type', 'promise')}] {l.get('content', '')}"
            for l in open_loops
        ]
        existing_context_parts.append("【未完事項】\n" + "\n".join(loop_lines))

    existing_context = "\n\n".join(existing_context_parts) if existing_context_parts else "（暫無）"
    prompt = CHECKPOINT_PROMPT.format(existing_context=existing_context, version=version)

    try:
        text = await _call_memory_model(
            prompt,
            f"請壓縮以下對話：\n\n{conversation_text}",
            max_tokens=800,
            model=CHECKPOINT_MODEL,
            route=route,
        )
        return text.strip()
    except Exception as exc:
        print(f"⚠️  checkpoint 摘要生成失敗: {exc}")
        return ""


async def score_memories(texts: List[str]) -> List[Dict[str, Any]]:
    """對純文字記憶條目批次評分。"""
    if not texts:
        return []

    memories_text = "\n".join(f"- {text}" for text in texts)
    prompt = SCORING_PROMPT.format(memories_text=memories_text)

    try:
        text = await _call_memory_model(prompt, "請回傳評分結果。", max_tokens=1200)
        payload = json.loads(text) if text else []
        if not isinstance(payload, list):
            raise ValueError("score payload is not a list")

        scored = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            scored.append(
                {
                    "content": content,
                    "importance": _clamp_importance(item.get("importance", 5)),
                }
            )
        if scored:
            print(f"📝 為 {len(scored)} 條記憶完成自動評分")
            return scored
    except Exception as exc:
        print(f"⚠️  記憶評分出錯: {exc}")

    return [{"content": text, "importance": 5} for text in texts]
