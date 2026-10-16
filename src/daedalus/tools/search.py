"""``search`` primitive — web search. Egress, so it asks first."""

from __future__ import annotations

import html
import re
from typing import Any, TYPE_CHECKING

import httpx

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

LITE = "https://lite.duckduckgo.com/lite/"
_LINK = re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAGS.sub("", text)).strip()


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
                resp = await client.post(LITE, data={"q": query})
            except httpx.HTTPError as exc:
                return ToolResult.error(f"search failed: {exc}")
        if resp.status_code >= 400:
            return ToolResult.error(f"search HTTP {resp.status_code}")
        links = _LINK.findall(resp.text)
        snippets = [_clean(s) for s in _SNIPPET.findall(resp.text)]
        results: list[str] = []
        for i, (url, title) in enumerate(links[:limit]):
            snippet = snippets[i] if i < len(snippets) else ""
            results.append(f"{i + 1}. {_clean(title)}\n   {html.unescape(url)}\n   {snippet}")
        return ToolResult(
            ok=bool(results),
            output="\n".join(results) or "(no results)",
            data={"results": results, "untrusted": True},
        )
