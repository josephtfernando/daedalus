"""`diagnose` — "why were you quiet?" answered from the machine's own record.

The tool is deliberately offline: a stat, a socket, a subprocess, and the usage
ledger. Nothing here spends model quota or reaches the network, so asking why
the agent is failing cannot itself fail on a provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from daedalus.config import ProviderConfig, Settings
from daedalus.loop.usage import record_usage
from daedalus.model.client import ModelClient
from daedalus.model.providers import HttpProvider
from daedalus.tools.diagnose import DiagnoseTool, _last_line, _pct, _usage_line


def _write(home: Path, *entries: dict) -> None:
    for entry in entries:
        record_usage(home, entry)


def _read(home: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (home / "state" / "usage.jsonl").read_text().splitlines()
    ]


def test_the_usage_summary_names_the_slow_provider_the_failures_and_the_window(
    tmp_path: Path,
) -> None:
    # The percentile is the observed one; no data is 0.0, not an error.
    values = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert _pct(values, 0.5) == 3.0
    assert _pct(values, 0.95) == 10.0
    assert _pct([], 0.5) == 0.0

    # An empty ledger says so honestly.
    assert "no turns recorded" in _usage_line([], 7)

    _write(
        tmp_path,
        {"seat": "daedalus", "provider": "nim", "seconds": 3.0, "retries": 1, "outcome": "ok", "tokens_in": 100, "tokens_out": 20},
        {"seat": "daedalus", "provider": "nim", "seconds": 5.0, "outcome": "ok", "tokens_in": 90, "tokens_out": 10},
        {"seat": "daedalus", "provider": "zai", "seconds": 30.0, "outcome": "error", "failure": "ProviderError"},
    )
    summary = _usage_line(_read(tmp_path), 7)
    assert "3 turn(s)" in summary and "1 ended in an error" in summary
    assert "1 retry(ies)" in summary
    assert "ProviderError ×1" in summary
    assert "zai" in summary and "p95 30.0s" in summary
    assert "nim" in summary

    # The window is respected: a month-old turn is not this week, but is in 40 days.
    old = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    windowed = [
        {"at": old, "provider": "nim", "seconds": 1.0, "outcome": "ok"},
        {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "provider": "nim", "seconds": 2.0, "outcome": "ok"},
    ]
    assert "no turns recorded" in _usage_line(windowed[:1], 7), "a month-old turn is not this week"
    assert "2 turn(s)" in _usage_line(windowed, 40)

    # An estimated turn is marked, not called measured.
    _write(
        tmp_path,
        {"provider": "nim", "seconds": 1.0, "outcome": "ok", "tokens_estimated": True, "tokens_in": 10, "tokens_out": 5},
    )
    assert "estimated" in _usage_line(_read(tmp_path), 7)

    # The last lines name the entry that failed.
    _write(
        tmp_path,
        {"provider": "nim", "steps": 2, "seconds": 1.5, "outcome": "error", "failure": "QuotaExhaustedError 429", "retries": 0},
    )
    line = _last_line(_read(tmp_path), 5)
    assert "nim" in line and "QuotaExhaustedError 429" in line


def _settings(home: Path) -> Settings:
    return Settings(
        home=home,
        env={"NVIDIA_API_KEY": "secret-value"},
        heavy=[
            ProviderConfig(
                name="nim-test",
                base_url="https://nim.test/v1",
                model="m",
                api_key_env="NVIDIA_API_KEY",
                timeout_s=5,
                max_calls=10,
            )
        ],
        cheap=None,
    )


def _client(settings: Settings) -> ModelClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    return ModelClient(
        settings,
        providers=[HttpProvider(settings.heavy[0], settings.env, transport=transport)],
    )


async def test_status_reports_the_cap_never_the_secret_and_marks_a_benched_key(
    runtime, monkeypatch  # noqa: ANN001
) -> None:
    from daedalus.model.providers import QuotaExhaustedError

    monkeypatch.setattr("daedalus.tools.diagnose._funnel", lambda: "off (tailnet only)")
    runtime.model = _client(_settings(runtime.settings.home))
    runtime.model.ledger.record(runtime.model.credential(runtime.model.providers[0]))

    got = await DiagnoseTool().run({"op": "status"}, runtime)

    assert got.ok
    assert "nim-test" in got.output
    assert "window 1/10" in got.output
    assert "secret-value" not in got.output, "the ledger is keyed by env name, never the value"
    assert "key=key:NVIDIA_API_KEY" in got.output
    assert "funnel: off" in got.output

    # A key that has exhausted its quota is marked, not hidden.
    provider = runtime.model.providers[0]
    runtime.model._cool_down(provider, QuotaExhaustedError("429", status=429))
    got = await DiagnoseTool().run({"op": "status"}, runtime)
    assert "BENCHED" in got.output


async def test_usage_and_last_run_through_the_tool_and_an_unknown_op_is_refused(
    runtime,  # noqa: ANN001
) -> None:
    _write(
        runtime.settings.home,
        {"provider": "nim", "seconds": 2.0, "outcome": "ok", "tokens_in": 5, "tokens_out": 5},
    )

    usage = await DiagnoseTool().run({"op": "usage", "days": 1}, runtime)
    last = await DiagnoseTool().run({"op": "last", "limit": 1}, runtime)

    assert usage.ok and "1 turn(s)" in usage.output
    assert last.ok and "nim" in last.output

    got = await DiagnoseTool().run({"op": "explain"}, runtime)
    assert not got.ok and "unknown diagnose op" in got.output


def test_the_tool_is_read_only_and_asks_nothing() -> None:
    from daedalus.approvals.risk import Risk

    assert DiagnoseTool().risk is Risk.AUTO
