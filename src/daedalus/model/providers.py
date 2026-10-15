"""Providers: OpenAI-compatible HTTP plus a scripted provider for offline runs."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import ProviderConfig

DeltaFn = Callable[[str], Awaitable[None]]


class ProviderError(RuntimeError):
    """A provider call failed; the client should try the next one."""


class NoProviderAvailable(RuntimeError):
    """Every provider in the chain was unavailable or exhausted."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass(slots=True)
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str
    window_s: int
    max_calls: int

    def available(self) -> bool: ...

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_delta: DeltaFn | None = None,
        temperature: float = 0.3,
    ) -> ChatResult: ...


class HttpProvider:
    """An OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(self, cfg: ProviderConfig, env: dict[str, str]) -> None:
        self.name = cfg.name
        self.model = cfg.model
        self.base_url = cfg.base_url.rstrip("/")
        self.api_key = cfg.api_key(env)
        self.timeout_s = cfg.timeout_s
        self.window_s = cfg.window_s
        self.max_calls = cfg.max_calls

    @property
    def _is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url

    def available(self) -> bool:
        return True if self._is_local else bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_delta: DeltaFn | None = None,
        temperature: float = 0.3,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        url = f"{self.base_url}/chat/completions"
        if on_delta is not None:
            payload["stream"] = True
            return await self._stream(url, payload, on_delta)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {resp.status_code}: {resp.text[:400]}")
        return self._parse(resp.json())

    async def _stream(self, url: str, payload: dict[str, Any], on_delta: DeltaFn) -> ChatResult:
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise ProviderError(f"{self.name} HTTP {resp.status_code}: {body[:400]}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            content.append(piece)
                            await on_delta(piece)
                        for tc in delta.get("tool_calls", []) or []:
                            idx = tc.get("index", 0)
                            slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
        return ChatResult(
            content="".join(content),
            tool_calls=[_tool_call(v) for _, v in sorted(calls.items())],
            provider=self.name,
            model=self.model,
            usage=usage,
        )

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or [{}]
        message = choices[0].get("message", {}) or {}
        raw_calls = message.get("tool_calls") or []
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=[
                _tool_call(
                    {
                        "id": tc.get("id", ""),
                        "name": (tc.get("function") or {}).get("name", ""),
                        "args": (tc.get("function") or {}).get("arguments", ""),
                    }
                )
                for tc in raw_calls
            ],
            provider=self.name,
            model=self.model,
            usage=data.get("usage", {}) or {},
        )


def _tool_call(slot: dict[str, Any]) -> ToolCall:
    raw = slot.get("args") or ""
    if isinstance(raw, dict):
        args = raw
    else:
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {}
    return ToolCall(id=slot.get("id", ""), name=slot.get("name", ""), arguments=args, raw_arguments=str(raw))


class ScriptedProvider:
    """Deterministic provider for tests and the offline self-test.

    ``script`` is a list of :class:`ChatResult`, dicts, or callables taking the
    message list and returning either. Items are consumed one per call.
    """

    def __init__(self, script: Sequence[Any], *, name: str = "scripted", model: str = "scripted") -> None:
        self._script = list(script)
        self.name = name
        self.model = model
        self.window_s = 60
        self.max_calls = 0
        self.calls: list[list[dict[str, Any]]] = []

    def available(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_delta: DeltaFn | None = None,
        temperature: float = 0.3,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self._script:
            return ChatResult(content="", provider=self.name, model=self.model)
        item = self._script.pop(0)
        if callable(item):
            item = item(messages, tools)
        if isinstance(item, ChatResult):
            result = item
        elif isinstance(item, dict):
            result = ChatResult(**item)
        else:
            result = ChatResult(content=str(item))
        result.provider = result.provider or self.name
        result.model = result.model or self.model
        if on_delta and result.content:
            for word in result.content.split(" "):
                await on_delta(word + " ")
        return result
