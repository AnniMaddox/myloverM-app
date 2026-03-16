"""
LLM provider router
===================
統一處理 OpenAI / Anthropic / Gemini 三家官方 API。

- OpenAI：走 chat completions
- Anthropic：走 messages API
- Gemini：走 generateContent
- 若沒有提供新路由設定，呼叫端可自行回退到 legacy API_BASE_URL / API_KEY
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

ProviderId = Literal["openai", "anthropic", "gemini"]
TaskId = Literal["chat", "summary", "extraction"]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
ANTHROPIC_API_BASE = os.getenv("ANTHROPIC_API_BASE", "https://api.anthropic.com/v1")
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")

CHAT_PROVIDER = os.getenv("CHAT_PROVIDER", "").strip().lower()
CHAT_MODEL = os.getenv("CHAT_MODEL", "").strip()
SUMMARY_PROVIDER = os.getenv("SUMMARY_PROVIDER", "").strip().lower()
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "").strip()
EXTRACTION_PROVIDER = os.getenv("EXTRACTION_PROVIDER", "").strip().lower()
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "").strip()

DEFAULT_MAX_TOKENS = int(os.getenv("LLM_ROUTER_MAX_TOKENS", "4096"))


@dataclass(frozen=True)
class ProviderRoute:
    provider: ProviderId
    model: str


def normalize_provider(value: Any) -> ProviderId | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"openai", "anthropic", "gemini"}:
        return lowered  # type: ignore[return-value]
    return None


def parse_route_config(raw: Any) -> dict[TaskId, ProviderRoute]:
    parsed: dict[TaskId, ProviderRoute] = {}
    if not isinstance(raw, dict):
        return parsed

    for task in ("chat", "summary", "extraction"):
        entry = raw.get(task)
        if not isinstance(entry, dict):
            continue
        provider = normalize_provider(entry.get("provider"))
        model = str(entry.get("model", "") or "").strip()
        if provider and model:
            parsed[task] = ProviderRoute(provider=provider, model=model)  # type: ignore[index]
    return parsed


def get_effective_routes(raw: Any = None) -> dict[TaskId, ProviderRoute]:
    routes = get_default_routes()
    routes.update(parse_route_config(raw))
    return routes


def uses_openai_max_completion_tokens(model: str) -> bool:
    lowered = str(model or "").strip().lower()
    return lowered.startswith(("o1", "o3", "o4", "gpt-5"))


def apply_openai_token_limit(payload: dict[str, Any], model: str, max_tokens: int | None) -> None:
    if max_tokens is None:
        return
    if uses_openai_max_completion_tokens(model):
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["max_tokens"] = max_tokens


def get_default_routes() -> dict[TaskId, ProviderRoute]:
    defaults: dict[TaskId, ProviderRoute] = {}
    env_map = {
        "chat": (CHAT_PROVIDER, CHAT_MODEL),
        "summary": (SUMMARY_PROVIDER, SUMMARY_MODEL),
        "extraction": (EXTRACTION_PROVIDER, EXTRACTION_MODEL),
    }
    for task, (provider_raw, model) in env_map.items():
        provider = normalize_provider(provider_raw)
        if provider and model:
            defaults[task] = ProviderRoute(provider=provider, model=model)  # type: ignore[index]
    return defaults


def route_to_dict(route: ProviderRoute | None) -> dict[str, str] | None:
    if not route:
        return None
    return {"provider": route.provider, "model": route.model}


def get_provider_key(provider: ProviderId) -> str:
    if provider == "openai":
        return OPENAI_API_KEY
    if provider == "anthropic":
        return ANTHROPIC_API_KEY
    return GEMINI_API_KEY


def get_provider_base(provider: ProviderId) -> str:
    if provider == "openai":
        return OPENAI_API_BASE.rstrip("/")
    if provider == "anthropic":
        return ANTHROPIC_API_BASE.rstrip("/")
    return GEMINI_API_BASE.rstrip("/")


def get_provider_statuses() -> list[dict[str, Any]]:
    return [
        {"id": "openai", "label": "OpenAI", "enabled": bool(OPENAI_API_KEY)},
        {"id": "anthropic", "label": "Claude", "enabled": bool(ANTHROPIC_API_KEY)},
        {"id": "gemini", "label": "Gemini", "enabled": bool(GEMINI_API_KEY)},
    ]


def _iter_openai_message_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": text}] if text else []
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", "") or "").strip()
            if part_type == "text":
                text = str(part.get("text", "") or "").strip()
                if text:
                    normalized.append({"type": "text", "text": text})
            elif part_type == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    url = str(image_url.get("url", "") or "").strip()
                else:
                    url = str(image_url or "").strip()
                if url:
                    normalized.append({"type": "image_url", "image_url": {"url": url}})
        return normalized
    text = str(content or "").strip()
    return [{"type": "text", "text": text}] if text else []


def _extract_text_from_openai_message(content: Any) -> str:
    parts = [
        str(part.get("text", "") or "").strip()
        for part in _iter_openai_message_parts(content)
        if part.get("type") == "text"
    ]
    parts = [text for text in parts if text]
    return "\n".join(parts).strip()


def _parse_data_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    if not data:
        return None
    meta = header[5:]
    mime_type = meta.split(";", 1)[0].strip() or "application/octet-stream"
    if ";base64" not in meta:
        return None
    return mime_type, data


def _split_system_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role", "") or "").strip()
        content = msg.get("content")
        text = _extract_text_from_openai_message(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        converted.append({"role": role, "content": content})
    return "\n\n".join(system_parts).strip(), converted


def _convert_content_for_anthropic(content: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for part in _iter_openai_message_parts(content):
        if part.get("type") == "text":
            text = str(part.get("text", "") or "").strip()
            if text:
                converted.append({"type": "text", "text": text})
        elif part.get("type") == "image_url":
            parsed = _parse_data_url(str(part.get("url", "") or ""))
            if not parsed:
                continue
            mime_type, data = parsed
            converted.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": data,
                    },
                }
            )
    return converted


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_text, converted = _split_system_messages(messages)
    anthropic_messages: list[dict[str, Any]] = []
    for msg in converted:
        role = "assistant" if msg["role"] == "assistant" else "user"
        parts = _convert_content_for_anthropic(msg.get("content"))
        if not parts:
            continue
        anthropic_messages.append({"role": role, "content": parts})
    return system_text, anthropic_messages


def _to_gemini_contents(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system_text, converted = _split_system_messages(messages)
    system_instruction = None
    if system_text:
        system_instruction = {"parts": [{"text": system_text}]}

    contents: list[dict[str, Any]] = []
    for msg in converted:
        gemini_role = "model" if msg["role"] == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        for part in _iter_openai_message_parts(msg.get("content")):
            if part.get("type") == "text":
                text = str(part.get("text", "") or "").strip()
                if text:
                    parts.append({"text": text})
            elif part.get("type") == "image_url":
                parsed = _parse_data_url(str(part.get("url", "") or ""))
                if not parsed:
                    continue
                mime_type, data = parsed
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": data,
                        }
                    }
                )
        if not parts:
            continue
        contents.append(
            {
                "role": gemini_role,
                "parts": parts,
            }
        )
    return system_instruction, contents


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    blocks = payload.get("content")
    if isinstance(blocks, list):
        texts = [
            str(block.get("text", "") or "").strip()
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        texts = [text for text in texts if text]
        if texts:
            return "\n".join(texts).strip()

    if isinstance(payload.get("error"), dict):
        message = str(payload["error"].get("message", "") or "").strip()
        if message:
            raise ValueError(message)

    raise ValueError("Anthropic returned empty content")


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            texts = [str(part.get("text", "") or "").strip() for part in parts if isinstance(part, dict)]
            texts = [text for text in texts if text]
            if texts:
                return "\n".join(texts).strip()

    if isinstance(payload.get("error"), dict):
        message = str(payload["error"].get("message", "") or "").strip()
        if message:
            raise ValueError(message)

    prompt_feedback = payload.get("promptFeedback")
    if isinstance(prompt_feedback, dict):
        reason = str(prompt_feedback.get("blockReason", "") or "").strip()
        if reason:
            raise ValueError(f"Gemini blocked the request: {reason}")

    raise ValueError("Gemini returned empty content")


async def list_models_for_provider(provider: ProviderId) -> list[dict[str, str]]:
    api_key = get_provider_key(provider)
    if not api_key:
        raise ValueError(f"{provider} API key not configured on Railway")

    async with httpx.AsyncClient(timeout=30) as client:
        if provider == "gemini":
            response = await client.get(
                f"{get_provider_base(provider)}/models",
                headers={"x-goog-api-key": api_key},
            )
        elif provider == "anthropic":
            response = await client.get(
                f"{get_provider_base(provider)}/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
        else:
            response = await client.get(
                f"{get_provider_base(provider)}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )

    if response.status_code != 200:
        detail = response.text[:400]
        raise ValueError(f"Failed to list {provider} models: {response.status_code} {detail}")

    data = response.json()
    models: list[dict[str, str]] = []
    if provider == "gemini":
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods", [])
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            name = str(item.get("name", "") or "").strip()
            if not name.startswith("models/"):
                continue
            model_id = name.split("/", 1)[1]
            models.append({"id": model_id, "label": model_id})
    elif provider == "anthropic":
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "") or "").strip()
            if model_id:
                label = str(item.get("display_name", "") or model_id).strip()
                models.append({"id": model_id, "label": label})
    else:
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "") or "").strip()
            if not model_id:
                continue
            lowered = model_id.lower()
            if any(bad in lowered for bad in ("whisper", "tts", "dall", "embedding", "transcribe", "moderation", "image")):
                continue
            models.append({"id": model_id, "label": model_id})

    models.sort(key=lambda item: item["id"])
    return models


async def create_chat_completion_with_route(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    api_key = get_provider_key(route.provider)
    if not api_key:
        raise ValueError(f"{route.provider} API key not configured on Railway")

    max_output_tokens = max_tokens or DEFAULT_MAX_TOKENS
    async with httpx.AsyncClient(timeout=300) as client:
        if route.provider == "gemini":
            system_instruction, contents = _to_gemini_contents(messages)
            payload: dict[str, Any] = {
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": max_output_tokens,
                },
            }
            if system_instruction:
                payload["system_instruction"] = system_instruction
            if temperature is not None:
                payload["generationConfig"]["temperature"] = temperature
            if top_p is not None:
                payload["generationConfig"]["topP"] = top_p

            response = await client.post(
                f"{get_provider_base(route.provider)}/models/{route.model}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code != 200:
                raise ValueError(f"Gemini API error {response.status_code}: {response.text[:400]}")
            data = response.json()
            text = _extract_gemini_text(data)
            usage = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
            prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
            completion_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
            total_tokens = int(usage.get("totalTokenCount", prompt_tokens + completion_tokens) or 0)
            return {
                "id": f"gemini-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": route.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }

        if route.provider == "anthropic":
            system_text, anthropic_messages = _to_anthropic_messages(messages)
            payload: dict[str, Any] = {
                "model": route.model,
                "max_tokens": max_output_tokens,
                "messages": anthropic_messages,
            }
            if system_text:
                payload["system"] = system_text
            if temperature is not None:
                payload["temperature"] = temperature
            if top_p is not None:
                payload["top_p"] = top_p

            response = await client.post(
                f"{get_provider_base(route.provider)}/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code != 200:
                raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:400]}")
            data = response.json()
            text = _extract_anthropic_text(data)
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            completion_tokens = int(usage.get("output_tokens", 0) or 0)
            total_tokens = prompt_tokens + completion_tokens
            return {
                "id": str(data.get("id", f"anthropic-{int(time.time())}")),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": route.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": str(data.get("stop_reason") or "stop"),
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }

        payload = {
            "model": route.model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        apply_openai_token_limit(payload, route.model, max_tokens)

        response = await client.post(
            f"{get_provider_base(route.provider)}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code != 200:
        detail = response.text[:500]
        raise ValueError(f"{route.provider} API error {response.status_code}: {detail}")

    return response.json()


async def generate_text_with_route(
    route: ProviderRoute,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0,
    top_p: float | None = None,
    max_tokens: int = 1500,
    expect_json: bool = False,
    response_json_schema: dict[str, Any] | None = None,
) -> str:
    api_key = get_provider_key(route.provider)
    if not api_key:
        raise ValueError(f"{route.provider} API key not configured on Railway")

    if route.provider == "gemini":
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_prompt.strip():
            payload["system_instruction"] = {
                "parts": [{"text": system_prompt.strip()}],
            }
        if top_p is not None:
            payload["generationConfig"]["topP"] = top_p
        if expect_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            if response_json_schema:
                payload["generationConfig"]["responseJsonSchema"] = response_json_schema

        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{get_provider_base(route.provider)}/models/{route.model}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code != 200:
            raise ValueError(f"Gemini API error {response.status_code}: {response.text[:400]}")
        return _extract_gemini_text(response.json())

    if route.provider == "anthropic":
        payload: dict[str, Any] = {
            "model": route.model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_prompt}],
                }
            ],
        }
        if system_prompt.strip():
            payload["system"] = system_prompt.strip()
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p

        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{get_provider_base(route.provider)}/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code != 200:
            raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:400]}")
        return _extract_anthropic_text(response.json())

    payload: dict[str, Any] = {
        "model": route.model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    if top_p is not None:
        payload["top_p"] = top_p
    apply_openai_token_limit(payload, route.model, max_tokens)

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{get_provider_base(route.provider)}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code != 200:
        raise ValueError(f"{route.provider} API error {response.status_code}: {response.text[:400]}")

    data = response.json()
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()


# ============================================================
# 真正的串流（SSE），三個 provider 統一回傳 OpenAI-format SSE 行
# ============================================================

async def stream_chat_with_route(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    used_checkpoint: bool = False,
    thinking_budget: int | None = None,
) -> AsyncGenerator[str, None]:
    """
    真正的 token-by-token 串流。
    yield 的每一行都是 SSE 格式字串（含結尾 \\n\\n），
    最後會 yield 一個含 usage 的 chunk，再 yield 'data: [DONE]\\n\\n'。
    """
    api_key = get_provider_key(route.provider)
    if not api_key:
        err = json.dumps({"error": {"message": f"{route.provider} API key not configured"}})
        yield f"data: {err}\n\n"
        yield "data: [DONE]\n\n"
        return

    max_output_tokens = max_tokens or DEFAULT_MAX_TOKENS
    msg_id = f"{route.provider}-{int(time.time())}"

    if route.provider == "anthropic":
        async for line in _stream_anthropic(
            route, messages, api_key, max_output_tokens, temperature, top_p, msg_id, used_checkpoint,
            thinking_budget=thinking_budget,
        ):
            yield line

    elif route.provider == "gemini":
        async for line in _stream_gemini(
            route, messages, api_key, max_output_tokens, temperature, top_p, msg_id, used_checkpoint
        ):
            yield line

    else:
        # OpenAI
        async for line in _stream_openai(
            route, messages, api_key, max_output_tokens, temperature, top_p, msg_id, used_checkpoint
        ):
            yield line


async def _stream_anthropic(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int,
    temperature: float | None,
    top_p: float | None,
    msg_id: str,
    used_checkpoint: bool,
    thinking_budget: int | None = None,
) -> AsyncGenerator[str, None]:
    system_text, anthropic_messages = _to_anthropic_messages(messages)
    payload: dict[str, Any] = {
        "model": route.model,
        "max_tokens": max_tokens,
        "messages": anthropic_messages,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text

    thinking_enabled = thinking_budget and thinking_budget >= 1024
    if thinking_enabled:
        # thinking 啟用時 temperature 必須是 1（Anthropic 限制）
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    else:
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p

    input_tokens = 0
    output_tokens = 0
    thinking_parts: list[str] = []
    chunk_base = {
        "id": msg_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": route.model,
    }

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{get_provider_base(route.provider)}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                err = json.dumps({"error": {"message": f"Anthropic {response.status_code}: {body[:200].decode()}"}})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 送一個 role chunk
            role_chunk = {**chunk_base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

            async for raw_line in response.aiter_lines():
                raw_line = raw_line.strip()
                if not raw_line or raw_line.startswith("event:"):
                    continue
                if not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type", "")

                if evt_type == "message_start":
                    usage = evt.get("message", {}).get("usage", {})
                    input_tokens = int(usage.get("input_tokens", 0) or 0)

                elif evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            text_chunk = {
                                **chunk_base,
                                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"
                    elif delta.get("type") == "thinking_delta":
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            thinking_parts.append(thinking_text)

                elif evt_type == "message_delta":
                    usage = evt.get("usage", {})
                    output_tokens = int(usage.get("output_tokens", 0) or 0)

                elif evt_type == "message_stop":
                    break

    total_tokens = input_tokens + output_tokens
    usage_chunk = {
        **chunk_base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "_gateway_meta": {"used_checkpoint": used_checkpoint},
    }
    if thinking_parts:
        usage_chunk["_thinking_content"] = "".join(thinking_parts)
    yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_gemini(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int,
    temperature: float | None,
    top_p: float | None,
    msg_id: str,
    used_checkpoint: bool,
) -> AsyncGenerator[str, None]:
    system_instruction, contents = _to_gemini_contents(messages)
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system_instruction:
        payload["system_instruction"] = system_instruction
    if temperature is not None:
        payload["generationConfig"]["temperature"] = temperature
    if top_p is not None:
        payload["generationConfig"]["topP"] = top_p

    chunk_base = {
        "id": msg_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": route.model,
    }
    prompt_tokens = 0
    completion_tokens = 0

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{get_provider_base(route.provider)}/models/{route.model}:streamGenerateContent?alt=sse",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                err = json.dumps({"error": {"message": f"Gemini {response.status_code}: {body[:200].decode()}"}})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return

            role_chunk = {**chunk_base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

            async for raw_line in response.aiter_lines():
                raw_line = raw_line.strip()
                if not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                text = _extract_gemini_text(evt)
                if text:
                    text_chunk = {
                        **chunk_base,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"

                # 收集最後一個 chunk 的 usageMetadata
                usage = evt.get("usageMetadata") if isinstance(evt, dict) else None
                if usage:
                    prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
                    completion_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    total_tokens = int(prompt_tokens + completion_tokens)
    usage_chunk = {
        **chunk_base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "_gateway_meta": {"used_checkpoint": used_checkpoint},
    }
    yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_openai(
    route: ProviderRoute,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int,
    temperature: float | None,
    top_p: float | None,
    msg_id: str,
    used_checkpoint: bool,
) -> AsyncGenerator[str, None]:
    payload: dict[str, Any] = {
        "model": route.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    apply_openai_token_limit(payload, route.model, max_tokens)

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{get_provider_base(route.provider)}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                err = json.dumps({"error": {"message": f"OpenAI {response.status_code}: {body[:200].decode()}"}})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return

            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    yield "data: [DONE]\n\n"
                    return
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # 在含 usage 的最終 chunk 裡注入 _gateway_meta
                if chunk.get("usage"):
                    chunk["_gateway_meta"] = {"used_checkpoint": used_checkpoint}
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"
