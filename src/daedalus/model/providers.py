"""Providers: OpenAI-compatible HTTP plus a scripted provider for offline runs."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import ProviderConfig

DeltaFn = Callable[[str], Awaitable[None]]

#: HTTP statuses that mean *not now* rather than *not ever*. NIM's free tier
#: answers these under load and the same request usually works seconds later,
#: so they are kept apart from 400/401/403/404 — a bad request or a model that
#: is not in the catalogue will not fix itself on a second try.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Substrings that mean a retryable-looking status is really *out of quota*.
#: Pi learned this the expensive way and keeps provider-level retries at zero
#: partly for it: a 429 that says "insufficient_quota" is not a pause, and
#: spending three tries on it only delays the owner learning the key is done.
QUOTA_PATTERNS = (
    "insufficient_quota",
    "quota exceeded",
    "exceeded your quota",
    "out of quota",
    "usage limit reached",
    "monthly usage limit",
    "daily usage limit",
    "available balance",
    "insufficient balance",
    "out of budget",
    "billing",
    "subscription_sharing",
)

#: Substrings providers use when the *request* did not fit the context window.
#: This is not a transient failure and not a bad request: it is recoverable by
#: trimming what the model has already read (pi compacts and retries for it).
OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length of the messages",
)


class ProviderError(RuntimeError):
    """A provider call failed; the client should try the next one.

    ``status`` and ``retry_after`` are carried when the provider said something
    structured, so the client can decide from facts rather than from a message.
    """

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.retry_after = retry_after


class TransientProviderError(ProviderError):
    """The *same* provider may answer on a second try."""


class QuotaExhaustedError(ProviderError):
    """This provider's key is out of quota; a retry only wastes the turn.

    The chain should move on to the next provider, and this one should be
    benched until its window rolls over — retrying it every step is how one
    exhausted key turns a turn into three hundred seconds of 429s.
    """


class ContextOverflowError(ProviderError):
    """The request did not fit the model's context window.

    Recoverable, but not by trying again unchanged: whoever holds the messages
    has to make room first (the engine trims old tool results and retries once).
    """


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

    def __init__(
        self,
        cfg: ProviderConfig,
        env: dict[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = cfg.name
        self.model = cfg.model
        self.base_url = cfg.base_url.rstrip("/")
        self.api_key = cfg.api_key(env)
        self.timeout_s = cfg.timeout_s
        self.window_s = cfg.window_s
        self.max_calls = cfg.max_calls
        # How many extra tries this provider gets, and how much wall-clock the
        # retries may spend. The client reads these; the provider itself never
        # retries, because a retry is an API call the ledger has to count.
        self.retries = cfg.retries
        self.retry_budget_s = cfg.retry_budget_s
        #: The bench after an "out of quota" answer, and the total a single call
        #: may take (retries included) — the idle ceiling above is not a
        #: duration ceiling.
        self.quota_cooldown_s = cfg.cooldown_s
        self.deadline_s = cfg.deadline_s
        #: The model's window in tokens; 0 means unknown, and the engine then
        #: leaves trimming to overflow recovery.
        self.context_window = cfg.context_window
        #: What the rate limit and the quota bench actually belong to. Two NIM
        #: entries share ``NVIDIA_API_KEY``, so they are one account with one
        #: cap — keying by name let the twins spend twice the real quota, and a
        #: spent key took two requests and two benches to learn. The env *name*
        #: is used, never the value: the ledger is a state file, not a
        #: secret store.
        self.ledger_key = (
            f"key:{cfg.api_key_env}" if cfg.api_key_env else f"local:{self.base_url}"
        )
        # A seam for tests: an `httpx.MockTransport` answers without a socket.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport)

    @property
    def is_local(self) -> bool:
        """True when this provider runs on this machine (nothing crosses the wire)."""
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url

    def available(self) -> bool:
        return True if self.is_local else bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _http_error(self, resp: httpx.Response, body: str) -> ProviderError:
        """Classify a non-2xx answer: *not now*, *not ever*, or *out of quota*.

        The order matters. A 429 that means "out of quota" must not be mistaken
        for "slow down" (pi's rule, learned from providers that use one status
        for both), and an overflow is a shape-of-request problem that neither a
        retry nor the next provider fixes — only trimming the context does.
        """
        detail = f"{self.name} HTTP {resp.status_code}: {body[:400]}"
        lowered = body.lower()
        retry_after = _retry_after(resp)
        if resp.status_code in (400, 413, 422) and _mentions(lowered, OVERFLOW_PATTERNS):
            return ContextOverflowError(detail, status=resp.status_code)
        if resp.status_code == 402 or _mentions(lowered, QUOTA_PATTERNS):
            return QuotaExhaustedError(
                detail, status=resp.status_code, retry_after=retry_after
            )
        if resp.status_code in RETRYABLE_STATUS:
            return TransientProviderError(
                detail, status=resp.status_code, retry_after=retry_after
            )
        return ProviderError(detail, status=resp.status_code)

    def _transport_error(self, exc: httpx.HTTPError) -> ProviderError:
        """Name a connection failure by *kind* — ``ReadTimeout()`` has no message.

        Timeouts are marked transient like any other blip; whether a retry fits
        is the client's call, and a 60s stall already spends a retry budget on
        its own, so the chain falls through rather than stalling twice.
        """
        kind = type(exc).__name__
        message = str(exc).strip()
        detail = f"{kind}: {message}" if message and message != kind else kind
        if isinstance(exc, httpx.TimeoutException):
            return TransientProviderError(f"{self.name} timed out after {self.timeout_s}s ({detail})")
        return TransientProviderError(f"{self.name} connection failed ({detail})")

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
        async with self._client() as client:
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                raise self._transport_error(exc) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp, resp.text)
        try:
            data = resp.json()
        except ValueError as exc:
            # A gateway that answers 200 with a maintenance page. Worth one
            # more try, not a turn-ending failure on the first attempt.
            raise TransientProviderError(
                f"{self.name} returned a body that is not JSON: {resp.text[:200]!r}"
            ) from exc
        if not data.get("choices"):
            # A 200 with no choices is not an empty answer — it is a malformed
            # one, and treating it as "the model said nothing" ends the turn
            # with a silent blank instead of trying the next provider.
            raise TransientProviderError(
                f"{self.name} returned no choices: {resp.text[:200]!r}"
            )
        return self._parse(data)

    async def _stream(self, url: str, payload: dict[str, Any], on_delta: DeltaFn) -> ChatResult:
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finished = False
        async with self._client() as client:
            try:
                async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise self._http_error(resp, body)
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices", []):
                            # A finish_reason is the other legal terminal marker:
                            # some OpenAI-compatible servers never send [DONE].
                            if choice.get("finish_reason"):
                                finished = True
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
            except httpx.HTTPError as exc:
                raise self._transport_error(exc) from exc
        if not finished:
            # pi's "stream ended before message_stop": a connection that drops
            # mid-answer is not an answer. Raising means the chain tries again
            # (or the turn says it was cut off) instead of the owner receiving
            # half a sentence that looks finished.
            raise TransientProviderError(
                f"{self.name} stream ended before the answer was finished "
                "(no [DONE] and no finish_reason)"
            )
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


def _mentions(text: str, patterns: tuple[str, ...]) -> bool:
    """Does a provider's error body say one of these things? (case-blind)"""
    return any(pattern in text for pattern in patterns)


def _retry_after(resp: httpx.Response) -> float | None:
    """How long the provider asked us to wait, in seconds, when it said so.

    ``Retry-After`` may legally be an HTTP date; that form is treated as
    unknown so the client falls back to its own backoff — waiting until a
    date far enough out to matter is not a decision this layer should make.
    """
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


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

    def __init__(
        self,
        script: Sequence[Any],
        *,
        name: str = "scripted",
        model: str = "scripted",
        is_local: bool = False,
    ) -> None:
        self._script = list(script)
        self.name = name
        self.model = model
        self.window_s = 60
        self.max_calls = 0
        # Whether this stands in for a local runner. The memory facade reads it
        # to decide if `local_only` content may be included.
        self.is_local = is_local
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
