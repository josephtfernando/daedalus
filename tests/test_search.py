"""The `search` primitive's two jobs: read the page, and tell the truth about it.

Both were broken on 17 Oct and both failures looked identical from the outside —
the tool returned "(no results)". One was a parser that could not see results
that were there; the other was a bot challenge reported as an empty web. The
tests below pin the shape of both pages.
"""

from __future__ import annotations

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


class _Response:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _Client:
    """Stands in for ``httpx.AsyncClient`` and records what the tool sent."""

    sent: list[dict[str, Any]] = []

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def __call__(self, *args: Any, **kwargs: Any) -> "_Client":  # the constructor
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, **kwargs: Any) -> _Response:
        _Client.sent.append({"url": url, **kwargs})
        return _Response(self.status_code, self.text)


def _serve(monkeypatch: pytest.MonkeyPatch, status_code: int, text: str) -> None:
    _Client.sent = []
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _Client(status_code, text))


async def test_results_are_read_whatever_order_the_attributes_come_in(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regex expected `class` before `href` and double quotes. The page moved on.

    Every result was there and the tool saw none of them, five times in two
    turns, and reported it as an empty web.
    """
    _serve(monkeypatch, 200, PAGE)

    result = await SearchTool().run({"query": "duckdb"}, runtime)

    assert result.ok
    assert "1. An analytical SQL database" in result.output
    assert "https://duckdb.org/" in result.output
    assert "DuckDB is an OLAP database. Fast & open source." in result.output
    assert "2. DuckDB - Wikipedia" in result.output


async def test_the_tool_says_who_it_is(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An honest User-Agent is the one the endpoint answers.

    The bot challenge goes to the bare ``httpx`` signature, not to a client that
    names itself, so this is a behaviour and not a courtesy.
    """
    _serve(monkeypatch, 200, PAGE)

    await SearchTool().run({"query": "duckdb"}, runtime)

    assert _Client.sent[0]["headers"]["User-Agent"].startswith("Daedalus/")


async def test_a_bot_challenge_is_reported_as_a_challenge_not_as_an_empty_web(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is not an answer, and saying it is one is the worse failure.

    The model read "(no results)" as "the web has nothing" and told the owner so.
    """
    _serve(monkeypatch, 202, CHALLENGE)

    result = await SearchTool().run({"query": "seoul weather"}, runtime)

    assert not result.ok
    assert "challenge" in result.output
    assert "no results" not in result.output


async def test_a_page_with_genuinely_nothing_is_a_result_not_a_failure(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An honest "nothing" must not feed the repeat guard a strike."""
    _serve(monkeypatch, 200, "<html><body><p>No results.</p></body></html>")

    result = await SearchTool().run({"query": "asdfqwerzxcv"}, runtime)

    assert result.ok
    assert result.output == "(no results for 'asdfqwerzxcv')"
