"""The ``browser`` primitive (SPEC §3.4).

``op=text`` and ``op=html`` with no selector used to raise
``TypeError: Page.inner_text() missing 1 required positional argument``: both
Playwright methods *demand* a selector, and the tool passed none for the common
case of "read this page". A tool that crashes on its default argument is worse
than one that is missing — it is present enough to be chosen.

These tests drive the tool against a stub Playwright, so they pin the contract
offline and need no Chromium.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from daedalus.runtime import Runtime
from daedalus.tools.browser import BrowserTool

URL = "https://example.org/"


class _Locator:
    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    async def inner_text(self) -> str:
        self._log.append(("locator.inner_text",))
        return "locator text"

    async def inner_html(self) -> str:
        self._log.append(("locator.inner_html",))
        return "<b>locator html</b>"

    async def screenshot(self, *, path: str) -> None:
        self._log.append(("locator.screenshot",))
        Path(path).write_bytes(b"x")


class _Page:
    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    async def goto(self, url: str, **kwargs: object) -> None:
        self._log.append(("goto", url))

    # Signature kept exact: if the tool ever calls these with no selector again,
    # the stub raises the same TypeError the real Playwright did.
    async def inner_text(self, selector: str) -> str:
        self._log.append(("page.inner_text", selector))
        return "page text"

    async def content(self) -> str:
        self._log.append(("page.content",))
        return "<html>page html</html>"

    async def screenshot(self, *, path: str) -> None:
        self._log.append(("page.screenshot",))
        Path(path).write_bytes(b"x")

    def locator(self, selector: str) -> _Locator:
        self._log.append(("page.locator", selector))
        return _Locator(self._log)


class _Browser:
    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    async def new_page(self) -> _Page:
        return _Page(self._log)

    async def close(self) -> None:
        self._log.append(("browser.close",))


class _Chromium:
    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    async def launch(self) -> _Browser:
        return _Browser(self._log)


class _Playwright:
    def __init__(self, log: list[tuple]) -> None:
        self.chromium = _Chromium(log)


class _Manager:
    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    async def __aenter__(self) -> _Playwright:
        return _Playwright(self._log)

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def playwright_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Install a stub ``playwright.async_api`` and record what the tool called."""
    log: list[tuple] = []
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: _Manager(log)  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.async_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return log


async def test_text_without_a_selector_reads_the_body(
    runtime: Runtime, playwright_calls: list[tuple]
) -> None:
    result = await BrowserTool().run({"op": "text", "url": URL}, runtime)

    assert result.ok, result.output
    assert result.output == "page text"
    assert ("page.inner_text", "body") in playwright_calls


async def test_html_without_a_selector_reads_the_document(
    runtime: Runtime, playwright_calls: list[tuple]
) -> None:
    result = await BrowserTool().run({"op": "html", "url": URL}, runtime)

    assert result.ok, result.output
    assert result.output == "<html>page html</html>"
    assert ("page.content",) in playwright_calls


async def test_a_selector_narrows_the_read(runtime: Runtime, playwright_calls: list[tuple]) -> None:
    result = await BrowserTool().run({"op": "text", "url": URL, "selector": "#main"}, runtime)

    assert result.ok, result.output
    assert result.output == "locator text"
    assert ("page.locator", "#main") in playwright_calls


async def test_screenshots_land_under_state(
    runtime: Runtime, playwright_calls: list[tuple]
) -> None:
    result = await BrowserTool().run({"op": "screenshot", "url": URL}, runtime)

    assert result.ok, result.output
    assert (runtime.settings.home / "state" / "screenshot.png").exists()


async def test_a_page_reads_as_untrusted(runtime: Runtime, playwright_calls: list[tuple]) -> None:
    """A page's text is third-party: it may inform, never authorize (SPEC §6)."""
    result = await BrowserTool().run({"op": "text", "url": URL}, runtime)

    assert result.data.get("untrusted") is True


async def test_a_non_http_url_is_refused(runtime: Runtime, playwright_calls: list[tuple]) -> None:
    result = await BrowserTool().run({"op": "text", "url": "file:///etc/passwd"}, runtime)

    assert not result.ok
    assert "http" in result.output
