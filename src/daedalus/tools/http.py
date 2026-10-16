"""``http`` primitive — fetch, download, POST. Egress, so it asks first."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import httpx

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

MAX_BODY = 200 * 1024


class HttpTool(Tool):
    name = "http"
    title = "Make a network request"
    description = "Make an HTTP request (GET/POST/HEAD) and return the response body."
    risk = Risk.ASK
    scopes = {"net": ["*"]}
    parameters = params(
        url=({"type": "string"}, True),
        method=({"type": "string", "enum": ["GET", "POST", "HEAD", "PUT", "DELETE"], "default": "GET"}, False),
        headers=({"type": "object"}, False),
        json=({"type": "object", "description": "JSON request body"}, False),
        data=({"type": "string", "description": "raw request body"}, False),
        timeout_s=({"type": "integer", "default": 30}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        method = str(args.get("method", "GET")).upper()
        return Assessment(
            level=Level.CAUTION,
            summary=f"make a {method} request to a site outside this machine",
            reasons=("the request — and its body — leave your machine",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult.error("url must be http(s)")
        method = str(args.get("method", "GET")).upper()
        timeout = int(args.get("timeout_s", 30))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=args.get("headers") or None,
                    json=args.get("json"),
                    content=args.get("data"),
                )
            except httpx.HTTPError as exc:
                return ToolResult.error(f"request failed: {exc}")
        body = resp.text[:MAX_BODY]
        return ToolResult(
            ok=resp.status_code < 400,
            output=f"HTTP {resp.status_code}\n{body}",
            data={"status": resp.status_code, "url": str(resp.url), "untrusted": True},
        )
