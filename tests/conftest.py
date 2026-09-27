"""Shared fixtures for the mcp-agent-playwright test suite.

Two tiers of test:

* plain unit tests that need no browser and run everywhere;
* tests marked ``browser``, which drive a real headless Chromium and are
  **skipped, not failed**, when no browser binary is installed. That keeps
  ``uv run pytest -q`` useful on a machine that never ran
  ``playwright install``, while CI installs Chromium so they do run there.

The browser fixtures are session-scoped and share one event loop: a
function-scoped loop cannot drive a session-scoped Playwright object.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page

# One of everything resolve() knows how to target, so each locator form has
# something real to match.
PAGE_HTML = """<!doctype html>
<html><head><title>Fixture</title></head><body>
  <h1>Hello world</h1>
  <button id="btn">Go</button>
  <label for="nm">Name</label><input id="nm" placeholder="Enter email" />
  <select id="sel"><option value="a">Alpha</option><option value="b">Beta</option></select>
  <div title="Tip">titled thing</div>
  <span>Click me</span>
  <ul><li>one</li><li>two</li><li>three</li></ul>
</body></html>"""


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A session-wide headless Chromium, or a skip if none is installed."""
    from playwright.async_api import async_playwright

    playwright = None
    try:
        playwright = await async_playwright().start()
        instance = await playwright.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - any launch failure means "unavailable"
        # Stop the driver if it started, or we leak a node process per run.
        if playwright is not None:
            await playwright.stop()
        # Keep the reason to one line: Playwright's own message appends a
        # multi-line ASCII install banner, which would otherwise be repeated
        # verbatim for every skipped test.
        detail = " ".join(str(exc).splitlines()[0].split())[:160]
        pytest.skip(f"no Playwright browser available ({detail}) -- run `playwright install chromium`")

    yield instance

    await instance.close()
    await playwright.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """A fresh page per test, preloaded with :data:`PAGE_HTML`."""
    tab = await browser.new_page(viewport={"width": 1280, "height": 900})
    await tab.set_content(PAGE_HTML)
    yield tab
    await tab.close()


@pytest.fixture
def server_globals() -> Iterator[None]:
    """Restore the server's module-level browser state around a test.

    The server keeps a browser singleton, the tab list, the snapshot ref map
    and the console ring buffer in module globals. Tests that touch them
    would otherwise leak into each other.
    """
    from mcp_agent_playwright import server

    saved = {
        "_playwright": server._playwright,
        "_browser": server._browser,
        "_context": server._context,
        "_cdp_mode": server._cdp_mode,
        "_pages": list(server._pages),
        "_ref_map": dict(server._ref_map),
        "_console_log": list(server._console_log),
        "_fallback_msgs": list(server._fallback_msgs),
    }
    try:
        yield
    finally:
        server._playwright = saved["_playwright"]
        server._browser = saved["_browser"]
        server._context = saved["_context"]
        server._cdp_mode = saved["_cdp_mode"]
        server._pages[:] = saved["_pages"]
        server._ref_map.clear()
        server._ref_map.update(saved["_ref_map"])
        server._console_log.clear()
        server._console_log.extend(saved["_console_log"])
        server._fallback_msgs[:] = saved["_fallback_msgs"]
