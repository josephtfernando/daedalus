"""Resilience: a transient provider failure is retried before the chain moves on.

The chain already had one fallback per provider; what it lacked was a second
try at the *same* provider — and the judgement to know which failures deserve
one. NIM's free tier answers 429/502/503 under load and works seconds later, so
those are pauses; a bad request, a spent key and an overflow are not.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from daedalus.config import ProviderConfig, Settings
from daedalus.model.client import RETRY_CAP_S, ModelClient, _retry_delay
from daedalus.model.providers import (
    ChatResult,
    ContextOverflowError,
    HttpProvider,
    NoProviderAvailable,
    ProviderError,
    QuotaExhaustedError,
    TransientProviderError,
)
from daedalus.model.ratelimit import RateLimitLedger


def _cfg(**overrides: Any) -> ProviderConfig:
    base: dict[str, Any] = {
        "name": "nim-test",
        "base_url": "https://nim.test/v1",
        "model": "stepfun-ai/step-5-preview",
        "api_key_env": "NVIDIA_API_KEY",
        "timeout_s": 60,
    }
    base.update(overrides)
    return ProviderConfig(**base)


def _provider(handler: Any, **overrides: Any) -> HttpProvider:
    return HttpProvider(
        _cfg(**overrides),
        {"NVIDIA_API_KEY": "nvapi-test"},
        transport=httpx.MockTransport(handler),
    )


def _ok(content: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _client(settings: Settings, tmp_path: Path, *providers: Any, db: str = "ratelimit.db") -> ModelClient:
    return ModelClient(
        settings,
        ledger=RateLimitLedger(tmp_path / db),
        providers=list(providers),
    )


# -- classification: "not now" vs "not ever" ---------------------------------


async def test_a_failure_is_classified_by_what_it_means_for_the_next_call(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """Only a pause is retryable; the rest must be named so the caller can act.

    A bad request does not fix itself, a 429 can mean "slow down" *or* "spent",
    an overflow needs trimming rather than another identical request, and a 200
    that is not an answer is still a failed call.
    """
    # case: 503 under load is a pause, and the server's own pause is carried.
    provider = _provider(lambda request: httpx.Response(503, text="busy", headers={"Retry-After": "2"}))
    with pytest.raises(TransientProviderError) as caught:
        await provider.complete([{"role": "user", "content": "hi"}])
    assert caught.value.status == 503
    assert caught.value.retry_after == 2.0, "the server's own pause is carried, not guessed"
    assert "503" in str(caught.value)

    # case: 400 is not a blip — no retry, and a plain ProviderError.
    seen: list[httpx.Request] = []

    def bad(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, text="bad tools payload")

    with pytest.raises(ProviderError) as caught:
        await _provider(bad).complete([{"role": "user", "content": "hi"}])
    assert not isinstance(caught.value, TransientProviderError)
    assert len(seen) == 1

    # case: 429 "insufficient_quota" is spent, not busy.
    with pytest.raises(QuotaExhaustedError) as caught:
        await _provider(
            lambda request: httpx.Response(429, json={"error": {"message": "insufficient_quota"}})
        ).complete([{"role": "user", "content": "hi"}])
    assert not isinstance(caught.value, TransientProviderError)
    assert caught.value.status == 429

    # case: 402 says the same thing in a different word.
    with pytest.raises(QuotaExhaustedError):
        await _provider(lambda request: httpx.Response(402, text="payment required")).complete(
            [{"role": "user", "content": "hi"}]
        )

    # case: an overflow is named so the engine can trim rather than retry.
    with pytest.raises(ContextOverflowError) as caught:
        await _provider(
            lambda request: httpx.Response(400, text='{"error":"context_length_exceeded"}')
        ).complete([{"role": "user", "content": "hi"}])
    assert caught.value.status == 400

    # case: a stall with no message is still given a reason.
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    with pytest.raises(TransientProviderError) as caught:
        await _provider(stall).complete([{"role": "user", "content": "hi"}])
    assert "timed out after 60s" in str(caught.value)
    assert "ReadTimeout" in str(caught.value)

    # case: a 200 gateway page is a hiccup, not an answer.
    with pytest.raises(TransientProviderError) as caught:
        await _provider(lambda request: httpx.Response(200, text="<html>maintenance</html>")).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert "not JSON" in str(caught.value)

    # case: a 200 with no choices must not become a blank reply.
    with pytest.raises(TransientProviderError) as caught:
        await _provider(lambda request: httpx.Response(200, json={})).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert "no choices" in str(caught.value)

    # case: a healthy answer parses, and names the entry that produced it.
    result = await _provider(lambda request: _ok("hello")).complete([{"role": "user", "content": "hi"}])
    assert result.content == "hello"
    assert result.provider == "nim-test"

    # case: when every entry overflows, the client re-raises the overflow so the
    # engine can trim — it must not become a generic "no provider" failure.
    overflow = Flaky(
        failures=99,
        error=ContextOverflowError("HTTP 400: context_length_exceeded", status=400),
    )
    with pytest.raises(ContextOverflowError):
        await _client(settings, tmp_path, overflow, db="overflow.db").chat(
            [{"role": "user", "content": "hi"}]
        )


async def test_a_stream_is_an_answer_only_when_it_finishes() -> None:
    """pi's rule: half an answer is no answer, and the deltas already arrived."""
    # case: a stream that ends without a terminal event is a transient failure.
    body = b'data: {"choices":[{"delta":{"content":"half"}}]}\n\n'
    provider = _provider(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )
    )
    seen: list[str] = []

    async def collect(piece: str) -> None:
        seen.append(piece)

    with pytest.raises(TransientProviderError) as caught:
        await provider.complete([{"role": "user", "content": "hi"}], on_delta=collect)
    assert "before the answer was finished" in str(caught.value)
    assert seen == ["half"]

    # case: a stream that finishes is still an answer.
    body = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    provider = _provider(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )
    )
    seen.clear()
    result = await provider.complete([{"role": "user", "content": "hi"}], on_delta=collect)
    assert result.content == "hi"
    assert seen == ["hi"]


# -- retrying, with a ledger that can see every attempt ----------------------


class Flaky:
    """Stands in for a provider that fails a fixed number of times."""

    def __init__(
        self,
        name: str = "nim-flaky",
        *,
        failures: int = 1,
        error: Exception | None = None,
        retries: int = 2,
        retry_budget_s: float = 10.0,
    ) -> None:
        self.name = name
        self.model = "m"
        self.window_s = 60
        self.max_calls = 0
        self.is_local = False
        self.retries = retries
        self.retry_budget_s = retry_budget_s
        self.failures = failures
        self.error = error
        self.calls = 0

    def available(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        on_delta: Any = None,
        temperature: float = 0.3,
    ) -> ChatResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error or TransientProviderError("HTTP 503: busy", status=503)
        if on_delta is not None:
            await on_delta("ok")
        return ChatResult(content="ok", provider=self.name, model=self.model)


@pytest.fixture
def paused(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry pauses instead of sleeping through them."""
    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waits


async def test_a_transient_failure_is_retried_on_the_same_entry_then_left_behind(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """One 503 should not cost a model swap; a budget or a cap can still say no.

    A retry is an API call, so the ledger counts both; when the retry budget,
    the ledger's window or the provider's own long pause refuses it, the chain
    moves on instead — retries delay the fallback, they never replace it.
    """
    # case: one 503 — retried on the same provider, after a real pause.
    flaky = Flaky(failures=1)
    client = _client(settings, tmp_path, flaky, db="retry.db")
    result = await client.chat([{"role": "user", "content": "hi"}])
    assert flaky.calls == 2
    assert result.content == "ok"
    assert client.last_provider == "nim-flaky"
    assert len(paused) == 1 and paused[0] > 0

    # case: the ledger sees both attempts — the cap would otherwise be a lie.
    assert client.ledger.count("nim-flaky", 60) == 2

    # case: the retry budget is spent — fall through to the next entry.
    paused.clear()
    sick = Flaky(name="nim-sick", failures=99, retries=1)
    healthy = Flaky(name="zai", failures=0)
    result = await _client(settings, tmp_path, sick, healthy, db="fallthrough.db").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert sick.calls == 2
    assert healthy.calls == 1
    assert result.provider == "zai"

    # case: the ledger refuses the second try — the provider is not tried again.
    paused.clear()
    capped = Flaky(failures=99)
    capped.max_calls = 1
    with pytest.raises(NoProviderAvailable) as caught:
        await _client(settings, tmp_path, capped, db="capped.db").chat(
            [{"role": "user", "content": "hi"}]
        )
    assert capped.calls == 1, "the second try was refused before it was made"
    assert "rate-limited" in str(caught.value)

    # case: a zero retry budget means no retry at all.
    paused.clear()
    no_budget = Flaky(failures=99, retries=2, retry_budget_s=0.0)
    with pytest.raises(NoProviderAvailable):
        await _client(settings, tmp_path, no_budget, db="nobudget.db").chat(
            [{"role": "user", "content": "hi"}]
        )
    assert no_budget.calls == 1

    # case: "come back in 30 seconds" means the fallback is faster — no pause.
    paused.clear()
    busy = Flaky(
        failures=99,
        error=TransientProviderError("HTTP 429: slow down", status=429, retry_after=30.0),
    )
    healthy = Flaky(name="zai", failures=0)
    result = await _client(settings, tmp_path, busy, healthy, db="slowdown.db").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert busy.calls == 1
    assert paused == []
    assert result.provider == "zai"

    # case: a 400 is never tried twice, and the reason survives the chain.
    paused.clear()
    bad = Flaky(failures=99, error=ProviderError("HTTP 400: bad request"))
    with pytest.raises(NoProviderAvailable) as caught:
        await _client(settings, tmp_path, bad, db="badrequest.db").chat(
            [{"role": "user", "content": "hi"}]
        )
    assert bad.calls == 1
    assert "bad request" in str(caught.value)


# -- quota: not a blip, and not chosen again every step ----------------------


async def test_a_spent_key_is_never_retried_and_is_benched_for_later_turns(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """One turn is up to 16 steps; paying a spent key three tries each is death."""
    # case: a spent key is not retried — the next entry answers.
    spent = Flaky(
        failures=99,
        error=QuotaExhaustedError("HTTP 429: insufficient_quota", status=429),
    )
    healthy = Flaky(name="zai", failures=0)
    client = _client(settings, tmp_path, spent, healthy, db="quota.db")
    result = await client.chat([{"role": "user", "content": "hi"}])
    assert spent.calls == 1, "a spent key is not a pause; retrying it is the waste"
    assert result.provider == "zai"

    # case: the next turn skips the benched provider.
    await client.chat([{"role": "user", "content": "and again"}])
    assert spent.calls == 1, "the second turn skipped the benched provider"
    assert healthy.calls == 2
    assert client.cooling_for(spent) > 0

    # case: the bench believes a long retry_after rather than guessing.
    long_pause = Flaky(
        failures=99,
        error=QuotaExhaustedError("HTTP 429: quota", status=429, retry_after=900.0),
    )
    client = _client(settings, tmp_path, long_pause, db="longpause.db")
    with pytest.raises(NoProviderAvailable):
        await client.chat([{"role": "user", "content": "hi"}])
    assert client.cooling_for(long_pause) > 800, "the provider's own pause is believed"

    # case: an entry that knows its window can say how long to sit out.
    tuned = Flaky(
        failures=99,
        error=QuotaExhaustedError("HTTP 429: insufficient_quota", status=429),
        retries=0,
    )
    tuned.quota_cooldown_s = 42.0
    client = _client(settings, tmp_path, tuned, db="tuned.db")
    with pytest.raises(NoProviderAvailable):
        await client.chat([{"role": "user", "content": "hi"}])
    assert 40.0 < client.cooling_for(tuned) <= 42.0


# -- the credential, not the model, is what the cap belongs to ----------------


async def test_one_credential_is_one_budget_and_one_bench(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """NIM's two entries are one account; name-keyed accounting spent double.

    Two entries behind the same key share one rolling window, the stricter of
    the two ceilings holds for both, and a spent key benches its twin — the
    twin is never tried, because a request on that key is already known to fail.
    """
    # case: one API key, one budget — nothing is counted per model name.
    a = _provider(lambda request: _ok("a"), name="nim-a")
    b = _provider(lambda request: _ok("b"), name="nim-b")
    assert a.ledger_key == b.ledger_key, "one API key, one budget"

    client = _client(settings, tmp_path, a, b, db="sharedkey.db")
    await client.chat([{"role": "user", "content": "hi"}])
    assert client.ledger.count(a.ledger_key, 60) == 1
    assert client.ledger.count("nim-a", 60) == 0, "nothing is counted per model name"

    # case: a spent key benches its twin without the twin being tried.
    twin_calls: list[int] = []

    def spent_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "insufficient_quota"}})

    def twin_handler(request: httpx.Request) -> httpx.Response:
        twin_calls.append(1)
        return _ok("b")

    spent = _provider(spent_handler, name="nim-a")
    twin = _provider(twin_handler, name="nim-b")
    client = _client(settings, tmp_path, spent, twin, db="twin.db")
    with pytest.raises(NoProviderAvailable):
        await client.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(NoProviderAvailable):
        await client.chat([{"role": "user", "content": "and again"}])
    assert twin_calls == [], "the twin never shares the attempt, only the bench"
    assert client.cooling_for(twin) > 0, "the twin inherits the bench"
    assert client.cooling_for(spent) > 0

    # case: the strictest ceiling among entries sharing a key wins for both.
    strict = Flaky(name="nim-a", failures=0)
    strict.max_calls = 1
    loose = Flaky(name="nim-b", failures=0)
    loose.max_calls = 5
    strict.ledger_key = loose.ledger_key = "key:NVIDIA_API_KEY"
    client = _client(settings, tmp_path, strict, loose, db="strictest.db")
    await client.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(NoProviderAvailable) as caught:
        await client.chat([{"role": "user", "content": "again"}])
    assert "rate-limited" in str(caught.value), (
        "the looser twin must not lend its cap to the stricter one"
    )
    assert strict.calls == 1 and loose.calls == 0


# -- a half answer is already on the owner's screen --------------------------


async def test_a_stream_that_broke_after_output_is_never_repeated(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """Half an answer already reached the owner; a retry would say it twice.

    The next entry starts its own version, so the surface is told to restart
    rather than leaving two halves sitting together.
    """
    seen: list[str] = []
    restarts: list[int] = []

    class BreaksMidStream(Flaky):
        async def complete(self, messages, tools=None, on_delta=None, temperature=0.3):
            self.calls += 1
            if on_delta is not None:
                await on_delta("half an ans")
            raise TransientProviderError("HTTP 503: busy", status=503)

    broken = BreaksMidStream(failures=99, retries=2)
    fallback = Flaky(name="zai", failures=0)
    client = _client(settings, tmp_path, broken, fallback, db="halfanswer.db")

    async def collect(piece: str) -> None:
        seen.append(piece)

    async def restart() -> None:
        restarts.append(1)

    result = await client.chat([{"role": "user", "content": "hi"}], on_delta=collect, on_restart=restart)

    assert broken.calls == 1, "a retry would duplicate what is already on screen"
    assert seen == ["half an ans", "ok"]
    assert result.provider == "zai"
    assert restarts == [1], "the half answer was not cleared before the swap"


# -- the call has a duration, not just a silence ceiling ---------------------


class _Slow(Flaky):
    """Answers eventually — eventually is the problem."""

    async def complete(self, messages, tools=None, on_delta=None, temperature=0.3):  # noqa: ANN001
        self.calls += 1
        await asyncio.sleep(0.3)
        return ChatResult(content="late", provider=self.name)


async def test_a_trickling_provider_cannot_hold_the_call_past_its_deadline(
    settings: Settings, tmp_path: Path
) -> None:
    """`timeout_s` bounds silence; a slow drip would otherwise never trip it."""
    slow = _Slow(failures=0, retries=0)
    slow.deadline_s = 0.05
    client = _client(settings, tmp_path, slow, db="deadline.db")

    with pytest.raises(NoProviderAvailable) as caught:
        await client.chat([{"role": "user", "content": "hi"}])

    assert "call deadline" in str(caught.value)
    assert slow.calls == 1


# -- the pause itself --------------------------------------------------------


def test_the_pause_between_retries_doubles_and_never_exceeds_the_cap() -> None:
    # case: exponential backoff, jittered, capped.
    random.seed(0)
    first = _retry_delay(0, ProviderError("x"))
    second = _retry_delay(1, ProviderError("x"))
    assert 0.0 < first < second <= RETRY_CAP_S * 1.25

    # case: a server pause inside the cap is obeyed exactly.
    assert _retry_delay(0, TransientProviderError("429", status=429, retry_after=1.5)) == 1.5

    # case: a server pause beyond the cap is refused (None means do not wait).
    assert (
        _retry_delay(0, TransientProviderError("429", status=429, retry_after=RETRY_CAP_S + 1))
        is None
    )


# -- resilience has to be visible to be tunable -----------------------------


async def test_a_retry_and_a_failure_are_counted_and_named_in_the_record(
    settings: Settings, tmp_path: Path, paused: list[float]
) -> None:
    """A slow turn and a flaky turn used to look the same in the ledger.

    The record now names the attempt count, the retry count and the entry that
    died — without the chain prefixing a name the error already carries.
    """
    # case: an answered retry is counted and its failure named.
    flaky = Flaky(failures=1, error=TransientProviderError("HTTP 503: busy", status=503))
    client = _client(settings, tmp_path, flaky, db="stats.db")
    await client.chat([{"role": "user", "content": "hi"}])
    assert client.last_stats.attempts == 1
    assert client.last_stats.retries == 1
    assert client.last_stats.failure == "TransientProviderError 503"

    # case: a failed turn names the entry that died.
    doomed = Flaky(name="doomed", failures=99, error=ProviderError("HTTP 400: nope"))
    client = _client(settings, tmp_path, doomed, db="doomed.db")
    with pytest.raises(NoProviderAvailable):
        await client.chat([{"role": "user", "content": "hi"}])
    assert client.last_attempted == "doomed"
    assert client.last_stats.failure == "ProviderError"

    # case: the entry's name appears once in the error, not twice.
    bad = Flaky(name="nim-bad", failures=99, error=ProviderError("nim-bad HTTP 400: bad request"))
    client = _client(settings, tmp_path, bad, db="badname.db")
    with pytest.raises(NoProviderAvailable) as caught:
        await client.chat([{"role": "user", "content": "hi"}])
    text = str(caught.value)
    assert "nim-bad: nim-bad" not in text
    assert "nim-bad: HTTP 400" in text

    # case: an entry that just failed is tried last on the next turn.
    sick = Flaky(name="nim-sick", failures=99, retries=0)
    well = Flaky(name="zai", failures=0)
    client = _client(settings, tmp_path, sick, well, db="ordering.db")
    await client.chat([{"role": "user", "content": "hi"}])
    assert sick.calls == 1 and well.calls == 1
    await client.chat([{"role": "user", "content": "and again"}])
    assert sick.calls == 1, "the second turn did not pay the failed entry first"
    assert well.calls == 2
