"""One client over a provider chain, with a ledger, retries, and a cheap fallback."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .providers import (
    ChatResult,
    ContextOverflowError,
    DeltaFn,
    HttpProvider,
    NoProviderAvailable,
    Provider,
    ProviderError,
    QuotaExhaustedError,
    TransientProviderError,
)
from .ratelimit import RateLimitLedger
from .routing import choose_route

log = logging.getLogger("daedalus.model")

#: The first pause before a second try. It doubles per attempt, up to the cap.
RETRY_BASE_S = 0.5
#: The longest single pause a retry may take. A provider that asks for a longer
#: ``Retry-After`` than this has told us it will not answer soon, so the chain
#: moves on instead of sitting on it.
RETRY_CAP_S = 5.0
#: ±25% jitter, so a chain of retries does not march in lockstep.
RETRY_JITTER = 0.25
#: How long a provider that says it is out of quota is skipped for. A quota
#: window is not a pause, so this is deliberately much longer than a retry —
#: and a provider that names its own pause gets that instead, up to the max.
QUOTA_COOLDOWN_S = 300.0
#: Ceiling on the bench, so a bogus ``Retry-After`` cannot disable a provider
#: for a day. One hour is long enough to stop hammering a spent key.
QUOTA_COOLDOWN_MAX_S = 3600.0

ResetFn = Callable[[], Awaitable[None]]


class _RateLimited(RuntimeError):
    """The ledger has no room for another attempt; this provider is done."""


@dataclass(slots=True)
class CallStats:
    """What one ``chat()`` cost in attempts — the fact the usage line was missing.

    A turn that took four seconds and answered is not the same as a turn that
    took four seconds because three tries failed first, and until this existed
    the record could not tell them apart. ``failure`` is the *kind* of the last
    failure (an exception class name) and never its body: the body is provider
    prose that can echo the request, and usage is structure only.
    """

    attempts: int = 0
    retries: int = 0
    failure: str = ""
    emitted: bool = False


def _key(provider: Provider) -> str:
    """What the rate limit and the bench actually belong to: the credential.

    Keyed by provider *name* it was wrong twice over, because the shipped chain
    has two NIM entries and two Z.ai entries sharing one key each. The free
    tier's cap is per account, so name keying let the twins spend twice the
    real quota before the ledger said stop — and one exhausted key cost two
    full requests (and two separate benches) to learn one fact. A local runner
    has no credential, so its base URL is the thing being shared.
    """
    return str(getattr(provider, "ledger_key", "") or provider.name)


def _kind(exc: BaseException) -> str:
    """A short name for a failure, safe to log and record (never its body)."""
    status = getattr(exc, "status", None)
    name = type(exc).__name__
    return f"{name} {status}" if status else name


def _without_name(provider: Provider, exc: BaseException) -> str:
    """The error text with the provider's own name prefix removed.

    ``ProviderError`` details already carry ``<name> HTTP 503: …``; the chain
    prepends the name again, which shipped as ``nim-x: nim-x HTTP 503``.
    """
    text = str(exc).strip()
    prefix = f"{provider.name} "
    return text[len(prefix) :] if text.startswith(prefix) else text


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
        #: The provider the last call *tried last*, whether or not it answered.
        #: A failed turn used to record `provider: ""`, which made the usage
        #: ledger unable to answer the one question a failure raises: which
        #: entry died.
        self.last_attempted: str = ""
        #: What the last `chat()` cost. Read by the engine and written to
        #: `state/usage.jsonl`, so a flaky turn is visible as flaky.
        self.last_stats = CallStats()
        #: ledger key -> monotonic deadline. A provider that answered
        #: "out of quota" sits this out, so the rest of the turn (up to 16
        #: steps) does not pay the same three tries at every step.
        self._cooldown: dict[str, float] = {}
        #: ledger key -> consecutive failures. A provider that has just failed
        #: is tried *after* the healthy ones, without rewriting the configured
        #: order: static "fastest first" cannot know that the fastest entry is
        #: having a bad minute.
        self._health: dict[str, int] = {}

    # -- credential-scoped accounting -------------------------------------

    def credential(self, provider: Provider) -> str:
        """The ledger key this provider's cap belongs to (the key, never its value)."""
        return _key(provider)

    def limits_for(self, provider: Provider) -> tuple[float, int]:
        """``(window, ceiling)`` this provider is held to — see `_limits`."""
        return self._limits(provider)

    def health_for(self, provider: Provider) -> int:
        """Consecutive failures for this credential; 0 means healthy."""
        return self._health.get(_key(provider), 0)

    def _limits(self, provider: Provider) -> tuple[float, int]:
        """The strictest rolling window among entries sharing this credential.

        Two entries on one key share one cap, but each declares its own window
        and ceiling. Whatever the owner wrote on either entry has to hold for
        both, so the longest window and the smallest positive ceiling win.
        """
        key = _key(provider)
        window = float(getattr(provider, "window_s", 60))
        caps: list[int] = []
        for p in [*( [self.cheap] if self.cheap else []), *self.providers]:
            if _key(p) != key:
                continue
            window = max(window, float(getattr(p, "window_s", 60)))
            cap = int(getattr(p, "max_calls", 0) or 0)
            if cap > 0:
                caps.append(cap)
        return window, min(caps) if caps else 0

    def cooling_for(self, provider: Provider) -> float:
        """Seconds this provider is benched for, or 0 when it is available."""
        key = _key(provider)
        until = self._cooldown.get(key)
        if until is None:
            return 0.0
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._cooldown.pop(key, None)
            return 0.0
        return remaining

    def _cool_down(self, provider: Provider, exc: ProviderError) -> None:
        base = float(getattr(provider, "quota_cooldown_s", QUOTA_COOLDOWN_S) or QUOTA_COOLDOWN_S)
        wanted = max(base, float(exc.retry_after or 0.0))
        bench = min(wanted, QUOTA_COOLDOWN_MAX_S)
        self._cooldown[_key(provider)] = time.monotonic() + bench
        log.warning(
            "%s is out of quota — its key is benched for %.0fs (%s)",
            provider.name,
            bench,
            _kind(exc),
        )

    def _note(self, provider: Provider, *, ok: bool) -> None:
        key = _key(provider)
        if ok:
            self._health[key] = 0
        else:
            self._health[key] = self._health.get(key, 0) + 1

    def _order(self, chain: list[Provider]) -> list[Provider]:
        """Healthy entries keep their order; recently failing ones go behind.

        Stable, not clever: a provider that has just failed is demoted for the
        rest of this process's memory of it, and a single answer promotes it
        straight back. The configured order is the owner's preference, so it is
        the tie-break rather than something this rewrites.
        """
        if len(chain) < 2:
            return chain
        return [
            provider
            for _, provider in sorted(
                enumerate(chain),
                key=lambda pair: (self._health.get(_key(pair[1]), 0), pair[0]),
            )
        ]

    # -- chain -------------------------------------------------------------

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

    def _local_chain(self) -> list[Provider]:
        seen: list[Provider] = []
        for p in ([self.cheap] if self.cheap else []) + self.providers:
            if getattr(p, "is_local", False) and p not in seen:
                seen.append(p)
        return seen

    def context_window(self, text_hint: str = "", *, local_only: bool = False) -> int:
        """The smallest context window this call could land in, or 0 if unknown.

        The chain has several models with different windows and only one will
        answer, so the budget has to be the pessimistic one: trimming for the
        largest window and landing on the smallest is how a request overflows.
        """
        route = choose_route(text_hint, cheap_available=self.cheap_available())
        chain = self._local_chain() if local_only else self._chain(route)
        windows = [
            int(getattr(p, "context_window", 0) or 0)
            for p in chain
            if p.available()
        ]
        known = [w for w in windows if w > 0]
        return min(known) if known else 0

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
        return any(getattr(p, "is_local", False) and p.available() for p in self._local_chain())

    # -- the call ----------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        route: str = "auto",
        on_delta: DeltaFn | None = None,
        on_restart: ResetFn | None = None,
        text_hint: str = "",
        local_only: bool = False,
    ) -> ChatResult:
        self.last_stats = CallStats()
        self.last_attempted = ""
        resolved = choose_route(
            text_hint or _last_user_text(messages),
            explicit=None if route == "auto" else route,
            cheap_available=self.cheap_available(),
        )
        errors: list[str] = []
        overflows = 0
        tried = 0
        # The bound is route-blind (T24): a seat bound to this machine's chain
        # never reaches a remote provider *whatever route the text resolves to*
        # — heavy included. The local providers, cheap first, are the whole
        # chain for this call.
        chain = self._local_chain() if local_only else self._chain(resolved)
        chain = self._order(chain)
        emitted = False

        async def deliver(piece: str) -> None:
            nonlocal emitted
            emitted = True
            self.last_stats.emitted = True
            if on_delta is not None:
                await on_delta(piece)

        for provider in chain:
            if not provider.available():
                continue
            benched = self.cooling_for(provider)
            if benched:
                errors.append(
                    f"{provider.name}: out of quota, skipping it for another {benched:.0f}s"
                )
                continue
            if emitted and on_restart is not None:
                # A previous entry put half an answer on screen before it died.
                # This one will say its own version from the start, so clear the
                # partial first: two half-answers in one message is worse than
                # the short wait. (No *retry* ever does this — a retry after
                # output began is refused outright in `_call`.)
                await on_restart()
                emitted = False
            tried += 1
            self.last_stats.attempts += 1
            self.last_attempted = provider.name
            try:
                result = await self._call(provider, messages, tools, deliver if on_delta else None)
            except _RateLimited:
                self._note(provider, ok=False)
                self.last_stats.failure = "rate-limited"
                errors.append(f"{provider.name}: rate-limited")
                continue
            except QuotaExhaustedError as exc:
                # pi's rule, kept: quota is not a blip. Do not retry, do not
                # keep choosing it every step — bench it and let the chain answer.
                self._note(provider, ok=False)
                self.last_stats.failure = _kind(exc)
                self._cool_down(provider, exc)
                errors.append(f"{provider.name}: {_without_name(provider, exc)}")
                continue
            except ContextOverflowError as exc:
                overflows += 1
                self._note(provider, ok=False)
                self.last_stats.failure = _kind(exc)
                errors.append(f"{provider.name}: {_without_name(provider, exc)}")
                continue
            except ProviderError as exc:
                self._note(provider, ok=False)
                self.last_stats.failure = _kind(exc)
                errors.append(f"{provider.name}: {_without_name(provider, exc)}")
                continue
            except Exception as exc:  # noqa: BLE001 — try the next provider on anything
                # An exception with an empty message (`httpx.ReadTimeout()`) would
                # read as `provider: ` — name the *kind* of failure instead.
                self._note(provider, ok=False)
                self.last_stats.failure = type(exc).__name__
                detail = str(exc).strip() or type(exc).__name__
                errors.append(f"{provider.name}: {detail}")
                continue
            self._note(provider, ok=True)
            self.last_provider = provider.name
            return result
        if overflows and overflows == tried:
            # Nothing answered and *every* attempt was a size problem: telling
            # the engine it was an overflow is what lets it trim and retry
            # instead of reporting a dead chain it could have made room for.
            raise ContextOverflowError("; ".join(errors))
        raise NoProviderAvailable(
            "; ".join(errors)
            or (
                "the local chain has nothing running — this seat is bound to it "
                "(charter [bounds]) and will not fall back to a remote provider"
                if local_only
                else "no provider available"
            )
        )

    async def _call(
        self,
        provider: Provider,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        on_delta: DeltaFn | None,
    ) -> ChatResult:
        """One provider, retried while its transient failures stay cheap.

        Retrying lives here rather than in the provider because **a retry is an
        API call** and the ledger is what keeps the free tier under its cap: an
        attempt the ledger cannot see is how a provider gets hammered into the
        429s this is meant to survive. So every attempt is `allow`-checked and
        recorded before it is made.

        Three deliberate limits:

        * **Nothing is retried once output has begun.** A stream that dies
          halfway has already put words in front of the owner; a second attempt
          would say them twice. That failure goes to the chain instead.
        * **A timeout is not repeated.** A 60s stall spends the retry budget all
          by itself, so the next provider — a warm fallback — is cheaper than
          waiting out the same silence again (SPEC §4).
        * **The call has a total deadline.** `timeout_s` bounds *silence*; a
          provider that trickles, or a chain of retries, can still spend
          minutes. `deadline_s` bounds the whole `_call`, attempts included.
        """
        retries = max(0, int(getattr(provider, "retries", 0) or 0))
        budget = max(0.0, float(getattr(provider, "retry_budget_s", 0.0) or 0.0))
        retry_deadline = time.monotonic() + budget
        call_deadline_s = max(0.0, float(getattr(provider, "deadline_s", 0.0) or 0.0))
        call_deadline = time.monotonic() + call_deadline_s if call_deadline_s > 0 else None
        emitted = False

        async def deliver(piece: str) -> None:
            nonlocal emitted
            emitted = True
            if on_delta is not None:
                await on_delta(piece)

        for attempt in range(retries + 1):
            if call_deadline is not None and time.monotonic() >= call_deadline:
                raise ProviderError(
                    f"{provider.name} used its whole {call_deadline_s:g}s call deadline"
                )
            window, cap = self._limits(provider)
            if not self.ledger.allow(_key(provider), window, cap):
                raise _RateLimited(provider.name)
            self.ledger.record(_key(provider))
            try:
                try:
                    call = provider.complete(messages, tools, deliver if on_delta else None)
                    if call_deadline is None:
                        return await call
                    remaining = max(0.0, call_deadline - time.monotonic())
                    return await asyncio.wait_for(call, timeout=remaining)
                except TimeoutError as exc:
                    if call_deadline is None:
                        # Not our timeout: the provider raised it. Let the chain
                        # treat it like any other failure instead of blaming a
                        # deadline that does not exist.
                        raise
                    raise TransientProviderError(
                        f"{provider.name} did not finish inside its "
                        f"{call_deadline_s:g}s call deadline"
                    ) from exc
            except TransientProviderError as exc:
                self.last_stats.failure = _kind(exc)
                if emitted:
                    raise ProviderError(
                        f"{exc} (output had already begun, so it was not tried again)"
                    ) from exc
                if attempt >= retries:
                    if retries == 0:
                        raise
                    raise ProviderError(f"{exc} (gave up after {attempt + 1} tries)") from exc
                delay = _retry_delay(attempt, exc)
                if delay is None:
                    raise ProviderError(
                        f"{exc} (the provider asked for a longer pause than a retry is worth)"
                    ) from exc
                if time.monotonic() + delay >= retry_deadline:
                    raise ProviderError(
                        f"{exc} (gave up: the retry would not fit the {budget:g}s budget)"
                    ) from exc
                if call_deadline is not None and time.monotonic() + delay >= call_deadline:
                    raise ProviderError(
                        f"{exc} (gave up: the retry would not fit the "
                        f"{call_deadline_s:g}s call deadline)"
                    ) from exc
                self.last_stats.retries += 1
                log.info(
                    "%s: %s — trying again in %.2fs (try %d of %d)",
                    provider.name,
                    _kind(exc),
                    delay,
                    attempt + 2,
                    retries + 1,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: the loop either returns or raises")


def _retry_delay(attempt: int, exc: ProviderError) -> float | None:
    """How long to wait before trying the same provider again.

    ``None`` means *don't*: the provider named a pause longer than a retry is
    worth, and believing it is cheaper than ignoring it.
    """
    after = getattr(exc, "retry_after", None)
    if after is not None:
        if after > RETRY_CAP_S:
            return None
        return float(after)
    base = min(RETRY_BASE_S * (2**attempt), RETRY_CAP_S)
    return base * (1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER))


def _last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for msg in reversed(list(messages)):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""
