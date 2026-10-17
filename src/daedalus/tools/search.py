"""``search`` primitive — web search. Egress, so it asks first."""

from __future__ import annotations

import html
from html.parser import HTMLParser
from typing import Any, TYPE_CHECKING

import httpx

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .. import __version__
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

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


def _refused(status: int, body: str) -> str:
    """Why the backend answered with a challenge instead of a page, or ``""``.

    It serves a 202 with an "anomaly" modal when it decides the caller is a
    robot. Calling that "(no results)" is not a gentler truth, it is a false
    one: the tool told the owner the web had nothing about a subject it had
    never managed to ask about, and the model believed it.
    """
    if "anomaly-modal" in body:
        return (
            "the search backend served a bot challenge instead of results, so this "
            "says nothing about the query — it is refusing this machine right now"
        )
    if status == 202:
        return "the search backend answered with a challenge (HTTP 202), not a page"
    return ""


class SearchTool(Tool):
    name = "search"
    title = "Search the web"
    description = "Search the web and return titles, URLs, and snippets."
    risk = Risk.ASK
    scopes = {"net": ["duckduckgo.com"]}
    parameters = params(
        query=({"type": "string"}, True),
        max_results=({"type": "integer", "default": 5}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        return Assessment(
            level=Level.CAUTION,
            summary="search the web",
            reasons=("your query leaves the machine",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.error("empty query")
        limit = int(args.get("max_results", 5))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                resp = await client.post(
                    LITE, data={"q": query}, headers={"User-Agent": USER_AGENT}
                )
            except httpx.HTTPError as exc:
                return ToolResult.error(f"search failed: {exc}")
        if resp.status_code >= 400:
            return ToolResult.error(f"search HTTP {resp.status_code}")
        refusal = _refused(resp.status_code, resp.text)
        if refusal:
            return ToolResult.error(f"search: {refusal}")
        parser = _LiteParser()
        parser.feed(resp.text)
        results: list[str] = []
        for i, (url, title) in enumerate(parser.links[:limit]):
            snippet = parser.snippets[i] if i < len(parser.snippets) else ""
            results.append(f"{i + 1}. {title}\n   {html.unescape(url)}\n   {snippet}")
        if not results:
            # The call worked and the answer is "nothing". That is a result, not
            # a failure, and dressing it as one is the other half of the same
            # mistake: it feeds the loop guard a strike for an honest answer.
            return ToolResult(
                output=f"(no results for {query!r})",
                data={"results": [], "untrusted": True},
            )
        return ToolResult(
            output="\n".join(results), data={"results": results, "untrusted": True}
        )
