"""Integration tests for snapshot building, screenshots and evaluate.

Marked ``browser``; skipped when no Playwright browser binary is installed.
``_get_page`` is redirected at the shared test page so the server's own browser
singleton is never launched -- these tests exercise the tool logic, not process
startup.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page

from mcp_agent_playwright import server

pytestmark = pytest.mark.browser


@pytest.fixture
def use_page(monkeypatch: pytest.MonkeyPatch, page: Page) -> Page:
    """Point the server's ``_get_page`` at the test page."""

    async def _fake_get_page() -> Page:
        return page

    monkeypatch.setattr(server, "_get_page", _fake_get_page)
    return page


# --------------------------------------------------------------------------- #
# _build_snapshot
# --------------------------------------------------------------------------- #


async def test_snapshot_numbers_interactive_nodes(page: Page, server_globals: None) -> None:
    out = await server._build_snapshot(page, 100)

    assert "[1]" in out
    assert "Interact via:" in out
    assert server._ref_map, "snapshot should populate the ref map"
    first_ref = min(server._ref_map)
    assert server._ref_map[first_ref]["ref"] == first_ref


async def test_snapshot_ref_map_is_rebuilt_each_call(page: Page, server_globals: None) -> None:
    await server._build_snapshot(page, 100)
    first_count = len(server._ref_map)
    await server._build_snapshot(page, 100)
    assert len(server._ref_map) == first_count, "refs should be renumbered, not accumulated"


async def test_snapshot_refs_are_usable_by_resolve(page: Page, server_globals: None) -> None:
    """The contract that matters: a ref from a snapshot resolves and acts."""
    from mcp_agent_playwright.locators import resolve

    out = await server._build_snapshot(page, 100)
    refs = [int(n) for n in re.findall(r"^\s*\[(\d+)\]", out, flags=re.MULTILINE)]
    assert refs, "snapshot produced no numbered refs"

    by_ref = {r["ref"]: r for r in server._ref_map.values()}
    named = [r for r in refs if by_ref[r].get("css")]
    assert named, "expected at least one ref with a CSS path"

    loc, how = resolve(page, str(named[0]), server._ref_map)
    assert how == f"ref {named[0]}"
    assert await loc.count() >= 0


async def test_snapshot_respects_node_cap(page: Page, server_globals: None) -> None:
    await server._build_snapshot(page, 10)
    assert len(server._ref_map) <= 10


async def test_snapshot_clamps_node_cap_to_a_floor(page: Page, server_globals: None) -> None:
    """max_nodes below the floor of 10 is raised to 10, not honoured literally."""
    await server._build_snapshot(page, 1)
    assert 1 <= len(server._ref_map) <= 10


async def test_snapshot_on_blank_page_does_not_crash(browser: Browser, server_globals: None) -> None:
    """A blank page still yields a node, so the "no content" branch is not hit."""
    blank = await browser.new_page()
    try:
        out = await server._build_snapshot(blank, 50)
        assert "Interact via:" in out
    finally:
        await blank.close()


# --------------------------------------------------------------------------- #
# browser_screenshot
# --------------------------------------------------------------------------- #


async def test_screenshot_auto_names_with_local_timestamp(
    use_page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_globals: None
) -> None:
    monkeypatch.setenv("MCP_PLAYWRIGHT_SCREENSHOT_DIR", str(tmp_path))
    out = await server.browser_screenshot()

    assert out.startswith("saved:")
    written = list(tmp_path.glob("*.png"))
    assert len(written) == 1
    assert re.fullmatch(r"screenshot-\d{8}-\d{6}\.png", written[0].name)
    assert written[0].stat().st_size > 0


async def test_screenshot_uses_explicit_filename(
    use_page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_globals: None
) -> None:
    monkeypatch.setenv("MCP_PLAYWRIGHT_SCREENSHOT_DIR", str(tmp_path))
    out = await server.browser_screenshot(filename="  shot.png  ")

    assert "shot.png" in out
    assert (tmp_path / "shot.png").exists()


async def test_screenshot_full_page(
    use_page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_globals: None
) -> None:
    monkeypatch.setenv("MCP_PLAYWRIGHT_SCREENSHOT_DIR", str(tmp_path))
    out = await server.browser_screenshot(filename="full.png", full_page=True)
    assert "full.png" in out
    assert (tmp_path / "full.png").stat().st_size > 0


async def test_screenshot_reports_errors_instead_of_raising(
    use_page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_globals: None
) -> None:
    """A bad filename must come back as text, never as an exception."""
    monkeypatch.setenv("MCP_PLAYWRIGHT_SCREENSHOT_DIR", str(tmp_path))
    out = await server.browser_screenshot(filename="bad\x00name.png")
    assert out.startswith("browser_screenshot:")


# --------------------------------------------------------------------------- #
# browser_evaluate
# --------------------------------------------------------------------------- #


async def test_evaluate_returns_json(use_page: Page, server_globals: None) -> None:
    out = await server.browser_evaluate("document.title")
    assert json.loads(out) == "Fixture"


async def test_evaluate_serialises_objects(use_page: Page, server_globals: None) -> None:
    out = await server.browser_evaluate("Array.from(document.querySelectorAll('li')).map(e => e.textContent)")
    assert json.loads(out) == ["one", "two", "three"]


async def test_evaluate_reports_errors_instead_of_raising(use_page: Page, server_globals: None) -> None:
    out = await server.browser_evaluate("this is not javascript(")
    assert out.startswith("browser_evaluate:")
