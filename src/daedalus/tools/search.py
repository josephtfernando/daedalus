"""``search`` primitive — web search. Egress, so it asks first.

Two backends on purpose, and the order is the point:

1. **SearXNG**, if the owner runs one. It is a *service that answers in JSON*, so
   nothing here breaks when someone else's HTML changes — which is exactly what
   happened on 17 Oct, when DuckDuckGo moved an attribute and every page full of
   results parsed to zero.
2. **DuckDuckGo**, as the fallback. So an owner with no SearXNG still has search,
   and so a first backend that comes back empty gets a second opinion.

Neither is a single point of failure, and a fallback that succeeds is silent —
it does its job in the journal, not in the owner's face.
"""

from __future__ import annotations

import html
import logging
from functools import partial
from html.parser import HTMLParser
from typing import Any, TYPE_CHECKING

import httpx

from .. import __version__
from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

LITE = "https://lite.duckduckgo.com/lite/"

#: How the tool says who it is. This is not decoration. DuckDuckGo's lite
#: endpoint serves a bot challenge — HTTP 202 carrying an "anomaly" modal — to
#: the bare ``httpx`` User-Agent, and answers a client that names itself. Saying
#: plainly what this is also happens to be the thing that works, which is the
#: only kind of camouflage worth having.
USER_AGENT = f"Daedalus/{__version__} (local chief-of-staff agent)"

#: Elements that never take a closing tag. Counting them as open would leave the
#: parser collecting a snippet that never ends.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


class Refused(Exception):
    """A backend answered, and its answer was a refusal — not "nothing found"."""


class Unusable(Exception):
    """A backend did not answer, or answered with something that is not results."""


def _render(rows: list[tuple[str, str, str]], limit: int) -> list[str]:
    """``(title, url, snippet)`` triples as the numbered block the model reads."""
    out: list[str] = []
    for i, (title, url, snippet) in enumerate(rows[:limit], 1):
        out.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return out


# -- SearXNG -----------------------------------------------------------------


async def _searxng(
    client: httpx.AsyncClient, query: str, limit: int, *, base: str
) -> list[str]:
    """Search a SearXNG instance and read its JSON. The preferred backend."""
    try:
        resp = await client.get(
            f"{base.rstrip('/')}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as exc:
        raise Unusable(f"the local SearXNG at {base} did not answer ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise Unusable(f"the local SearXNG answered HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unusable(
            f"the local SearXNG did not answer with JSON — is `json` enabled in its "
            f"`search.formats`?"
        ) from exc
    rows: list[tuple[str, str, str]] = []
    for item in payload.get("results") or []:
        url = str(item.get("url") or "").strip()
        if url:
            rows.append(
                (
                    str(item.get("title") or url).strip(),
                    url,
                    str(item.get("content") or "").strip(),
                )
            )
    return _render(rows, limit)


# -- DuckDuckGo --------------------------------------------------------------


class _LiteParser(HTMLParser):
    """Results out of a lite page, by structure rather than by pattern.

    This was a regex, and the regex is why `search` spent a day returning
    "(no results)". It expected ``class="result-link"`` *before* ``href`` and
    double-quoted; the page now writes ``href`` first in single quotes. Every
    page full of results parsed to zero, the tool called that an empty web, and
    the model told the owner there was nothing to find. Markup is not a
    contract — attribute order and quoting are the page's business — so the
    results are collected by element and class instead.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._open = 0
        self._want: str | None = None
        self._href = ""
        self._text: list[str] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if self._want is not None:
            return False
        classes = (dict(attrs).get("class") or "").split()
        if tag == "a" and "result-link" in classes:
            self._want, self._href, self._text, self._open = "link", dict(attrs).get("href") or "", [], 1
        elif "result-snippet" in classes:
            self._want, self._text, self._open = "snippet", [], 1
        return self._want is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._start(tag, attrs) or self._want is None:
            return
        # A tag inside the text is a boundary: `<b>x</b>` and `<br>` both end a
        # word, and gluing the pieces together reads as a typo the model repeats.
        self._text.append(" ")
        if tag not in _VOID:
            self._open += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._want is None:
            return
        self._open -= 1
        if self._open > 0:
            return
        text = " ".join("".join(self._text).split())
        if self._want == "link":
            self.links.append((self._href, text))
        elif text:
            self.snippets.append(text)
        self._want = None

    def handle_data(self, data: str) -> None:
        if self._want is not None:
            self._text.append(data)


def _duckduckgo_refusal(status: int, body: str) -> str:
    """Why the backend answered with a challenge instead of a page, or ``""``.

    It serves a 202 with an "anomaly" modal when it decides the caller is a
    robot. Calling that "(no results)" is not a gentler truth, it is a false
    one: the tool told the owner the web had nothing about a subject it had
    never managed to ask about, and the model believed it.
    """
    if "anomaly-modal" in body:
        return (
            "DuckDuckGo served a bot challenge instead of results, so this says "
            "nothing about the query — it is refusing this machine right now"
        )
    if status == 202:
        return "DuckDuckGo answered with a challenge (HTTP 202), not a page"
    return ""


async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> list[str]:
    """Search DuckDuckGo's lite endpoint and parse its page. The fallback."""
    try:
        resp = await client.post(
            LITE, data={"q": query}, headers={"User-Agent": USER_AGENT}
        )
    except httpx.HTTPError as exc:
        raise Unusable(f"DuckDuckGo did not answer ({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        raise Unusable(f"DuckDuckGo answered HTTP {resp.status_code}")
    refusal = _duckduckgo_refusal(resp.status_code, resp.text)
    if refusal:
        raise Refused(refusal)
    parser = _LiteParser()
    parser.feed(resp.text)
    rows = [
        (
            title,
            html.unescape(url),
            parser.snippets[i] if i < len(parser.snippets) else "",
        )
        for i, (url, title) in enumerate(parser.links)
    ]
    return _render(rows, limit)


# -- the tool ----------------------------------------------------------------


class SearchTool(Tool):
    name = "search"
    title = "Search the web"
    description = "Search the web and return titles, URLs, and snippets."
    risk = Risk.ASK
    #: Both routes out. The local SearXNG is reached on loopback and *it* is what
    #: asks the public engines, which is why the approval prompt says where the
    #: query goes rather than pretending there is one destination.
    scopes = {"net": ["duckduckgo.com", "127.0.0.1"]}
    parameters = params(
        query=({"type": "string"}, True),
        max_results=({"type": "integer", "default": 5}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        return Assessment(
            level=Level.CAUTION,
            summary="search the web",
            reasons=("your query leaves the machine, via your own SearXNG when it is up",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.error("empty query")
        limit = int(args.get("max_results", 5))

        tried = self._backends(ctx)
        problems: list[str] = []
        nothing: list[str] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for label, backend in tried:
                try:
                    results = await backend(client, query, limit)
                except (Unusable, Refused) as exc:
                    problems.append(str(exc))
                    log.info("search: %s; trying the next backend", exc)
                    continue
                if results:
                    # Which backend answered is provenance, not decoration: the
                    # fallback is silent, so this is the only place the journal
                    # can show that a dead SearXNG was quietly worked around.
                    return ToolResult(
                        output="\n".join(results),
                        data={"results": results, "untrusted": True, "backend": label},
                    )
                nothing.append(label)

        if nothing:
            # A backend answered and had nothing. That is a result, not a failure,
            # and dressing it as one feeds the loop guard a strike for an honest
            # answer. A backend that *refused* is kept as a caveat, though: "no
            # results" from one plus a bot challenge from the other is not the
            # same as the web having nothing.
            caveat = f" — but {problems[0]}, so this is not conclusive" if problems else ""
            return ToolResult(
                output=f"(no results for {query!r}{caveat})",
                data={"results": [], "untrusted": True},
            )
        reason = "; ".join(problems) or f"no backend was reachable via {tried!r}"
        return ToolResult.error(f"search: {reason}")

    def _backends(self, ctx: "Runtime") -> list[tuple[str, Any]]:
        """The backends to try and what to call them, in order.

        SearXNG leads when one is configured — it answers in JSON, so nothing
        here depends on another site's HTML staying still. DuckDuckGo is always
        there behind it, so search survives a SearXNG that is not running, and a
        first backend that comes back empty gets a second opinion.
        """
        chosen: list[tuple[str, Any]] = []
        if ctx.settings.search_url:
            chosen.append(("the local SearXNG", partial(_searxng, base=ctx.settings.search_url)))
        chosen.append(("DuckDuckGo", _duckduckgo))
        return chosen
