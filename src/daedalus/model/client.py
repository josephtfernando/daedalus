"""One client over a provider chain, with a ledger and a cheap fallback."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..config import Settings
from .providers import (
    ChatResult,
    DeltaFn,
    HttpProvider,
    NoProviderAvailable,
    Provider,
)
from .ratelimit import RateLimitLedger
from .routing import choose_route


class ModelClient:
    def __init__(
        self,
        settings: Settings,
        *,
        ledger: RateLimitLedger | None = None,
        providers: Sequence[Provider] | None = None,
        cheap: Provider | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger or RateLimitLedger(settings.home / "state" / "ratelimit.db")
        self.providers: list[Provider] = (
            list(providers)
            if providers is not None
            else [HttpProvider(p, settings.env) for p in settings.heavy]
        )
        if cheap is not None:
            self.cheap: Provider | None = cheap
        elif providers is not None:
            self.cheap = None
        else:
            self.cheap = HttpProvider(settings.cheap, settings.env) if settings.cheap else None
        self.last_provider: str = ""

    def cheap_available(self) -> bool:
        return self.cheap is not None and self.cheap.available()

    def _chain(self, route: str) -> list[Provider]:
        if route == "cheap" and self.cheap is not None:
            # Cheap is a preference, not a trap. A local runner that is down, or
            # has no weights pulled, must not fail a trivial turn outright when
            # the heavy chain is right there -- the local route exists to save
            # money, not to make the agent unreachable.
            return [self.cheap, *self.providers]
        return list(self.providers)

    def chain_is_local(self, text_hint: str = "") -> bool:
        """True only if *everything* this turn could reach is on this machine.

        ``local_only`` memory may be included in a prompt only when the whole
        resolved chain is local. One remote fallback is enough to make the fact
        unsafe, so this is deliberately an "all", not a "first".
        """
        route = choose_route(text_hint, cheap_available=self.cheap_available())
        available = [p for p in self._chain(route) if p.available()]
        if not available:
            return False
        return all(getattr(p, "is_local", False) for p in available)

    def local_available(self) -> bool:
        """Is any local provider running? The seat-bound turn's honest gate.

        A seat the owner has bound to the local chain (T24) may not fall back to
        a remote provider — so when nothing local is running, the turn is
        refused outright rather than routed somewhere the bound forbids. This is
        the probe that lets the engine say *why* in the owner's words.
        """
        return any(
            getattr(p, "is_local", False) and p.available()
            for p in ([self.cheap] if self.cheap else []) + self.providers
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        route: str = "auto",
        on_delta: DeltaFn | None = None,
        text_hint: str = "",
        local_only: bool = False,
    ) -> ChatResult:
        resolved = choose_route(
            text_hint or _last_user_text(messages),
            explicit=None if route == "auto" else route,
            cheap_available=self.cheap_available(),
        )
        errors: list[str] = []
        chain = self._chain(resolved)
        if local_only:
            # The bound is route-blind (T24): a seat bound to this machine's
            # chain never reaches a remote provider *whatever route the text
            # resolves to* — heavy included. The local providers, cheap first,
            # are the whole chain for this call.
            seen: list[Provider] = []
            for p in ([self.cheap] if self.cheap else []) + self.providers:
                if getattr(p, "is_local", False) and p not in seen:
                    seen.append(p)
            chain = seen
        for provider in chain:
            if not provider.available():
                continue
            if not self.ledger.allow(provider.name, provider.window_s, provider.max_calls):
                errors.append(f"{provider.name}: rate-limited")
                continue
            try:
                self.ledger.record(provider.name)
                result = await provider.complete(messages, tools, on_delta)
            except Exception as exc:  # noqa: BLE001 — try the next provider on anything
                # An exception with an empty message (`httpx.ReadTimeout()`) would
                # read as `provider: ` — name the *kind* of failure instead.
                detail = str(exc).strip() or type(exc).__name__
                errors.append(f"{provider.name}: {detail}")
                continue
            self.last_provider = provider.name
            return result
        raise NoProviderAvailable(
            "; ".join(errors)
            or (
                "the local chain has nothing running — this seat is bound to it "
                "(charter [bounds]) and will not fall back to a remote provider"
                if local_only
                else "no provider available"
            )
        )


def _last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for msg in reversed(list(messages)):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""
