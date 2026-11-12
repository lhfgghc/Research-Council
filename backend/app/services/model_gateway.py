from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from ..config import ModelCallSettings, ModelProfile
from ..db import Database, utc_now
from .context import estimate_messages


class BudgetExhausted(RuntimeError):
    pass


class ModelCallError(RuntimeError):
    pass


def model_request(
    profile: ModelProfile,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float = 0.5,
    stream: bool = True,
    force_reasoning_off: bool = False,
    omit_thinking: bool = False,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    headers = {"Content-Type": "application/json", "User-Agent": "ResearchCouncil/0.1"}
    payload: dict[str, Any] = {"model": profile.model, "max_tokens": max_tokens, "stream": stream}
    effort = "off" if force_reasoning_off else profile.reasoning_effort
    if profile.api_format == "anthropic":
        headers.update({"x-api-key": profile.api_key, "anthropic-version": "2023-06-01"})
        payload["messages"] = [item for item in messages if item["role"] != "system"]
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        if system:
            payload["system"] = system
        if effort == "off":
            model = profile.model.lower().replace(".", "-")
            if "claude-sonnet-5-5" in model:
                # Command Code accepts adaptive thinking for this model's short requests.
                payload["thinking"] = {"type": "adaptive"}
                payload["output_config"] = {"effort": "low"}
            else:
                payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": effort}
        return f"{profile.base_url}/messages", headers, payload

    headers["Authorization"] = f"Bearer {profile.api_key}"
    payload.update(messages=messages, temperature=temperature)
    if "deepseek" in profile.model.lower() and not omit_thinking:
        payload["reasoning_effort"] = effort
        payload["thinking"] = {"type": "disabled" if effort == "off" else "enabled"}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return f"{profile.base_url}/chat/completions", headers, payload


def _anthropic_event(event: dict[str, Any]) -> dict[str, Any]:
    kind = event.get("type")
    if kind == "error":
        return {"error": event.get("error") or "Anthropic stream error"}
    if kind == "message_start":
        event = event.get("message") or {}

    delta: dict[str, Any] = {}
    stop_reason = event.get("stop_reason")
    if kind in {"content_block_start", "content_block_delta"}:
        block = event.get("delta" if kind == "content_block_delta" else "content_block") or {}
        delta = {"content": block.get("text"), "reasoning_content": block.get("thinking")}
    elif kind in {"message", "message_start"}:
        blocks = event.get("content") or []
        delta = {
            "content": "".join(block.get("text", "") for block in blocks if block.get("type") == "text"),
            "reasoning_content": "".join(block.get("thinking", "") for block in blocks if block.get("type") == "thinking"),
        }
    elif kind == "message_delta":
        stop_reason = (event.get("delta") or {}).get("stop_reason")

    usage = event.get("usage") or {}
    counts: dict[str, int] = {}
    if usage.get("input_tokens") is not None:
        counts["prompt_tokens"] = sum(int(usage.get(field) or 0) for field in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        ))
    if usage.get("output_tokens") is not None:
        counts["completion_tokens"] = int(usage["output_tokens"])
    finish = {"max_tokens": "length", "end_turn": "stop", "stop_sequence": "stop"}.get(stop_reason, stop_reason)
    return {"usage": counts, "choices": [{"delta": delta, "finish_reason": finish}]}


async def response_events(response: httpx.Response, api_format: str = "openai") -> AsyncIterator[dict[str, Any]]:
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        await response.aread()
        event = response.json()
        if isinstance(event, dict):
            yield _anthropic_event(event) if api_format == "anthropic" else event
        return
    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield _anthropic_event(event) if api_format == "anthropic" else event


@dataclass
class ModelResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    finish_reason: str | None = None
    call_id: int | None = None
    reasoning_text: str = ""


class ModelGateway:
    """Single gate for every model request and every count against the hard budget."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self._budget_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _reserve(self, run_id: str) -> tuple[int, dict[str, Any]]:
        async with self._budget_lock:
            run = self.db.query_one("SELECT budget_used, budget_max, status FROM runs WHERE id = ?", (run_id,))
            if not run:
                raise ModelCallError("运行不存在")
            if run["budget_used"] >= run["budget_max"]:
                raise BudgetExhausted(f"已达到 {run['budget_max']} 次 LLM 调用上限")
            changed = self.db.execute(
                "UPDATE runs SET budget_used = budget_used + 1, updated_at = ? "
                "WHERE id = ? AND budget_used < budget_max",
                (utc_now(), run_id),
            )
            if changed != 1:
                raise BudgetExhausted("调用预算已被其他任务用完")
            return int(run["budget_used"]) + 1, run

    async def complete(
        self,
        *,
        run_id: str,
        role: str,
        purpose: str,
        messages: list[dict[str, str]],
        settings: ModelCallSettings,
        temperature: float = 0.5,
        max_tokens: int = 5000,
        on_delta: Any = None,
    ) -> ModelResult:
        if not settings.api_key or not settings.base_url:
            raise ModelCallError(f"请先保存模型 {settings.name or settings.model} 的 Base URL 和 API Key")
        if self._client is None:
            self._client = httpx.AsyncClient()
        client = self._client
        attempts = 0
        max_attempts = 2
        last_error = "模型请求失败"
        output_room = max(1, settings.context_window_tokens - estimate_messages(messages))
        max_tokens = min(max_tokens, output_room)
        force_reasoning_off = False
        omit_thinking = False
        deadline = asyncio.get_running_loop().time() + settings.request_timeout_seconds
        timeout_message = f"模型请求超过 {settings.request_timeout_seconds} 秒总时限"
        while attempts < max_attempts:
            if asyncio.get_running_loop().time() >= deadline:
                raise ModelCallError(timeout_message)
            attempts += 1
            await self._reserve(run_id)
            call_id = self.db.insert(
                "INSERT INTO model_calls "
                "(run_id, role, purpose, model, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, role, purpose, settings.model, "requesting", utc_now()),
            )
            started = time.perf_counter()
            try:
                timeout = httpx.Timeout(settings.request_timeout_seconds, connect=30.0)
                url, headers, payload = model_request(
                    settings, messages, max_tokens=max_tokens, temperature=temperature,
                    force_reasoning_off=force_reasoning_off, omit_thinking=omit_thinking,
                )
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                usage: dict[str, Any] = {}
                finish_reason: str | None = None
                async with (
                    asyncio.timeout_at(deadline),
                    client.stream(
                        "POST",
                        url,
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    ) as response,
                ):
                    if not response.is_success:
                        raw = await response.aread()
                        detail = raw.decode("utf-8", errors="replace")[:1200]
                        raise httpx.HTTPStatusError(
                            f"模型接口返回 {response.status_code}: {detail}",
                            request=response.request, response=response,
                        )
                    async for event_data in response_events(response, settings.api_format):
                        if event_data.get("error"):
                            raise ModelCallError(str(event_data["error"])[:800])
                        if event_data.get("usage"):
                            usage.update(event_data["usage"])
                        choices = event_data.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or choice.get("message") or {}
                        piece = delta.get("content")
                        if piece:
                            content_parts.append(piece)
                            if on_delta:
                                on_delta("content", piece)
                        for reasoning_key in ("reasoning_content", "reasoning"):
                            reasoning_piece = delta.get(reasoning_key)
                            if reasoning_piece:
                                reasoning_parts.append(reasoning_piece)
                                if on_delta:
                                    on_delta("reasoning", reasoning_piece)
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                content = "".join(content_parts)
                if not content.strip():
                    if finish_reason == "length" and attempts < max_attempts:
                        max_attempts = max(max_attempts, 3)
                        max_tokens = min(max_tokens + 6000, output_room)
                        if settings.api_format == "anthropic" or "deepseek" in settings.model.lower():
                            if not force_reasoning_off:
                                force_reasoning_off = True
                            else:
                                omit_thinking = True
                        raise ModelCallError(
                            f"输出预算被耗尽且正文为空（{settings.model}）；"
                            "将在该模型的上下文窗口内重试"
                        )
                    raise ModelCallError(
                        f"模型返回了空内容（{settings.model}，finish_reason={finish_reason}）"
                    )
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                cost = (
                    prompt_tokens / 1_000_000 * settings.input_price_per_million
                    + completion_tokens / 1_000_000 * settings.output_price_per_million
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.db.execute(
                    "UPDATE model_calls SET prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, "
                    "latency_ms = ?, status = ? WHERE id = ?",
                    (prompt_tokens, completion_tokens, cost, latency_ms, "success", call_id),
                )
                return ModelResult(
                    content.strip(),
                    prompt_tokens,
                    completion_tokens,
                    cost,
                    latency_ms,
                    finish_reason,
                    call_id,
                    "".join(reasoning_parts),
                )
            except asyncio.CancelledError:
                self.db.execute("UPDATE model_calls SET status = 'interrupted' WHERE id = ?", (call_id,))
                raise
            except TimeoutError as exc:
                self.db.execute(
                    "UPDATE model_calls SET latency_ms = ?, status = 'timed_out', error = ? WHERE id = ?",
                    (int((time.perf_counter() - started) * 1000), timeout_message, call_id),
                )
                raise ModelCallError(timeout_message) from exc
            except Exception as exc:
                last_error = str(exc).replace(settings.api_key, "[已隐藏]") or "模型请求失败"
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.db.execute(
                    "UPDATE model_calls SET latency_ms = ?, status = ?, error = ? WHERE id = ?",
                    (latency_ms, "failed", last_error[:1000], call_id),
                )
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in {429, 500, 502, 503, 504}:
                    break
                if isinstance(exc, ModelCallError) and "模型接口返回 4" in str(exc):
                    break
                if attempts >= max_attempts:
                    break
                await asyncio.sleep(min(1.2, max(0, deadline - asyncio.get_running_loop().time())))
        raise ModelCallError(last_error)
