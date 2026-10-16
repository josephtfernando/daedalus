"""``browser`` primitive — headless Chromium for JS-heavy pages.

Uses Playwright when installed; returns an actionable error otherwise (the
backend choice is an open detail in SPEC §13).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

MAX_TEXT = 200 * 1024


class BrowserTool(Tool):
    name = "browser"
    title = "Drive a browser"
    description = "Load a page in headless Chromium, return its text, or take a screenshot."
    risk = Risk.ASK
    scopes = {"net": ["*"]}
    parameters = params(
        op=({"type": "string", "enum": ["text", "html", "screenshot"]}, True),
        url=({"type": "string"}, True),
        selector=({"type": "string", "description": "optional CSS selector to narrow the result"}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        return Assessment(
            level=Level.CAUTION,
            summary="open a page in a browser on this machine",
            reasons=("the page and its scripts load and run locally",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ToolResult.error(
                "browser backend not installed — run `uv add playwright && uv run playwright install chromium`"
            )
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult.error("url must be http(s)")
        op = str(args.get("op", "text"))
        selector = args.get("selector")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
                target = page.locator(selector) if selector else page
                if op == "screenshot":
                    shot = ctx.settings.home / "state" / "screenshot.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    await (target if selector else page).screenshot(path=str(shot))
                    return ToolResult(output=f"screenshot saved to {shot}", data={"path": str(shot)})
                if op == "html":
                    content = await target.inner_html()
                else:
                    content = await target.inner_text()
            finally:
                await browser.close()
        return ToolResult(output=content[:MAX_TEXT], data={"url": url, "untrusted": True})
