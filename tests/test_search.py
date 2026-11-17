"""The `search` primitive: two backends, and the truth about both.

`search` failed on 17 Oct in a way that looked identical from the outside for two
different reasons — the tool returned "(no results)". One was a parser that could
not see results that were there; the other was a bot challenge reported as an
empty web. These tests pin the shape of each, and the order of things: SearXNG
answers in JSON and leads, DuckDuckGo is the silent fallback behind it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from daedalus.runtime import Runtime
from daedalus.tools import search as search_mod
from daedalus.tools.search import SearchTool

#: The page as lite actually serves it now: `href` before `class`, single quotes,
#: and a snippet whose text is broken up by tags.
PAGE = """
<table border="0">
  <tr>
    <td valign="top">1.&nbsp;</td>
    <td><a rel="nofollow" href="https://duckdb.org/" class='result-link'>An analytical SQL database</a></td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'><b>DuckDB</b> is an OLAP database.<br>Fast &amp; open source.</td>
  </tr>
  <tr>
    <td valign="top">2.&nbsp;</td>
    <td><a class='result-link' rel="nofollow" href="https://en.wikipedia.org/wiki/DuckDB">DuckDB - Wikipedia</a></td>
  </tr>
</table>
"""

#: A page that answered with a challenge rather than results, as served to the
#: bare httpx User-Agent.
CHALLENGE = """
<!DOCTYPE html><html><body>
<div class="anomaly-modal__mask">
  <div class="anomaly-modal__box">Unfortunately, bots use DuckDuckGo too.</div>
</div>
</body></html>
"""

#: SearXNG's JSON answer, trimmed to the fields the tool reads.
SEARX = {
    "query": "duckdb",
    "results": [
        {
            "title": "DuckDB — an in-process OLAP database",
            "url": "https://duckdb.org/",
            "content": "DuckDB is a fast in-process analytical database.",
        },
        {
            "title": "DuckDB - Wikipedia",
            "url": "https://en.wikipedia.org/wiki/DuckDB",
            "content": "DuckDB is an open-source column-oriented RDBMS.",
        },
    ],
}

LOCAL = "http://127.0.0.1:8888"


class _Response:
    def __init__(self, status_code: int, text: str, payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Stands in for ``httpx.AsyncClient``: canned answers per route.

    ``routes`` maps a URL *prefix* to a ``(status, text, payload)`` triple, so one
    fake can be both a SearXNG that is up and a DuckDuckGo that is refusing.
    """

    def __init__(self, routes: dict[str, tuple[int, str, Any]]) -> None:
        self.routes = routes
        self.sent: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_Client":
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _reply(self, url: str) -> _Response:
        for prefix, (status, text, payload) in self.routes.items():
            if url.startswith(prefix):
                return _Response(status, text, payload)
        raise search_mod.httpx.ConnectError(f"nothing listening at {url}")

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.sent.append({"method": "GET", "url": url, **kwargs})
        return self._reply(url)

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.sent.append({"method": "POST", "url": url, **kwargs})
        return self._reply(url)


def _serve(monkeypatch: pytest.MonkeyPatch, routes: dict[str, tuple[int, str, Any]]) -> _Client:
    client = _Client(routes)
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", client)
    return client


def _searxng(runtime: Runtime, url: str = LOCAL) -> None:
    """Point the tool at a SearXNG, the way the owner's setting does."""
    runtime.settings.env["DAEDALUS_SEARCH_URL"] = url


def _no_searxng(runtime: Runtime) -> None:
    runtime.settings.env["DAEDALUS_SEARCH_URL"] = "off"


async def test_searxng_leads_in_json_and_duckduckgo_is_the_silent_fallback(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the local SearXNG answers in JSON and leads; no fallback call is made
    client = _serve(monkeypatch, {LOCAL: (200, json.dumps(SEARX), SEARX)})
    _searxng(runtime)

    result = await SearchTool().run({"query": "duckdb"}, runtime)

    assert result.ok
    assert "1. DuckDB — an in-process OLAP database" in result.output
    assert "https://duckdb.org/" in result.output
    assert "DuckDB is a fast in-process analytical database." in result.output
    assert result.data["backend"] == "the local SearXNG"
    assert [c["method"] for c in client.sent] == ["GET"], "the fallback was called anyway"
    assert client.sent[0]["params"]["format"] == "json"

    # one backend having nothing is not the web having nothing
    _serve(
        monkeypatch,
        {
            LOCAL: (200, "{}", {"results": []}),
            "https://lite.duckduckgo.com": (200, PAGE, None),
        },
    )
    second = await SearchTool().run({"query": "duckdb"}, runtime)
    assert second.ok
    assert "1. An analytical SQL database" in second.output
    assert second.data["backend"] == "DuckDuckGo", "the second opinion did not come"

    # a dead SearXNG still gets search, and no lecture
    _serve(monkeypatch, {"https://lite.duckduckgo.com": (200, PAGE, None)})
    dead = await SearchTool().run({"query": "duckdb"}, runtime)
    assert dead.ok
    assert "1. An analytical SQL database" in dead.output
    assert "SearXNG" not in dead.output, "the fallback narrated itself to the owner"
    assert dead.data["backend"] == "DuckDuckGo"

    # `off` means one backend, not a failed connection
    client = _serve(monkeypatch, {"https://lite.duckduckgo.com": (200, PAGE, None)})
    _no_searxng(runtime)
    off = await SearchTool().run({"query": "duckdb"}, runtime)
    assert off.ok
    assert all("127.0.0.1" not in call["url"] for call in client.sent)


async def test_a_searxng_that_is_not_speaking_json_names_the_setting_that_fixes_it(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`?format=json` is a 403 out of the box; the refusal has to name the knob."""
    _serve(monkeypatch, {LOCAL: (200, "<html>not json</html>", None)})
    _searxng(runtime)

    result = await SearchTool().run({"query": "duckdb"}, runtime)

    assert not result.ok
    assert "search.formats" in result.output, "the fix was not named"


async def test_results_are_read_whatever_order_the_attributes_come_in_and_the_tool_says_who_it_is(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regex expected `class` before `href` and double quotes; the page moved on.

    Every result was there and the tool saw none of them, five times in two turns,
    and reported it as an empty web. The honest User-Agent is also behaviour: the
    bot challenge goes to the bare ``httpx`` signature, not to a named client.
    """
    client = _serve(monkeypatch, {"https://lite.duckduckgo.com": (200, PAGE, None)})
    _no_searxng(runtime)

    result = await SearchTool().run({"query": "duckdb"}, runtime)

    assert result.ok
    assert "1. An analytical SQL database" in result.output
    assert "https://duckdb.org/" in result.output
    assert "DuckDB is an OLAP database. Fast & open source." in result.output
    assert "2. DuckDB - Wikipedia" in result.output
    assert client.sent[0]["headers"]["User-Agent"].startswith("Daedalus/")


async def test_a_refusal_is_a_refusal_not_an_empty_web(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a bot challenge is reported as a challenge, never as "(no results)"
    _serve(monkeypatch, {"https://lite.duckduckgo.com": (202, CHALLENGE, None)})
    _no_searxng(runtime)

    challenged = await SearchTool().run({"query": "seoul weather"}, runtime)

    assert not challenged.ok
    assert "challenge" in challenged.output
    assert "no results" not in challenged.output

    # two backends failing is two facts; reporting one is half a truth
    _serve(
        monkeypatch,
        {
            LOCAL: (500, "boom", None),
            "https://lite.duckduckgo.com": (202, CHALLENGE, None),
        },
    )
    _searxng(runtime)

    both = await SearchTool().run({"query": "duckdb"}, runtime)

    assert not both.ok
    assert "SearXNG answered HTTP 500" in both.output
    assert "challenge" in both.output

    # a genuine nothing is a result, so the repeat guard is not fed a strike
    _serve(
        monkeypatch,
        {"https://lite.duckduckgo.com": (200, "<html><p>No results.</p></html>", None)},
    )
    _no_searxng(runtime)

    empty = await SearchTool().run({"query": "asdfqwerzxcv"}, runtime)

    assert empty.ok
    assert empty.output == "(no results for 'asdfqwerzxcv')"

    # but an empty answer beside a refusal is inconclusive, not silence
    _serve(
        monkeypatch,
        {
            LOCAL: (200, "{}", {"results": []}),
            "https://lite.duckduckgo.com": (202, CHALLENGE, None),
        },
    )
    _searxng(runtime)

    mixed = await SearchTool().run({"query": "asdfqwerzxcv"}, runtime)

    assert mixed.ok
    assert "not conclusive" in mixed.output
