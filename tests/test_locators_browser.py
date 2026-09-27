"""Integration tests for ``resolve()`` against a real headless Chromium.

These are the tests that would have caught the sync/async annotation bug: each
one performs the exact production sequence -- ``resolve()`` then ``await`` the
action on the returned locator -- so a locator that is not awaitable fails here
rather than at the first user click.

Marked ``browser``; skipped when no Playwright browser binary is installed.
"""

from __future__ import annotations

import pytest
from playwright.async_api import Page

from mcp_agent_playwright.locators import TargetError, resolve

pytestmark = pytest.mark.browser

TIMEOUT = 10_000


async def test_css_selector_clicks(page: Page) -> None:
    loc, how = resolve(page, "css=#btn", {})
    await loc.click(timeout=TIMEOUT)
    assert how == "css=#btn"
    assert await page.evaluate("document.getElementById('btn') !== null")


async def test_role_with_name_clicks(page: Page) -> None:
    await page.evaluate("() => { window.__hits = []; }")
    await page.evaluate("() => { document.getElementById('btn').onclick = () => window.__hits.push('go'); }")
    loc, how = resolve(page, "role=button,name=Go", {})
    await loc.click(timeout=TIMEOUT)
    assert how == "role=button,name=Go"
    assert await page.evaluate("window.__hits") == ["go"]


async def test_text_selector_clicks(page: Page) -> None:
    loc, how = resolve(page, "text=Click me", {})
    await loc.click(timeout=TIMEOUT)
    assert how == "text=Click me"


async def test_label_fills_input(page: Page) -> None:
    loc, _ = resolve(page, "label=Name", {})
    await loc.fill("typed by test", timeout=TIMEOUT)
    assert await page.input_value("#nm") == "typed by test"


async def test_placeholder_fills_input(page: Page) -> None:
    loc, _ = resolve(page, "placeholder=Enter email", {})
    await loc.fill("via placeholder", timeout=TIMEOUT)
    assert await page.input_value("#nm") == "via placeholder"


async def test_title_selector_hovers(page: Page) -> None:
    loc, _ = resolve(page, "title=Tip", {})
    await loc.hover(timeout=TIMEOUT)


async def test_xpath_key_hover(page: Page) -> None:
    loc, _ = resolve(page, "xpath=//h1", {})
    await loc.hover(timeout=TIMEOUT)


async def test_xpath_prefix_form_routes_to_xpath(page: Page) -> None:
    """``xpath://h1`` reaches the xpath handler and resolves the heading.

    ``resolve`` used to test ``raw[:5]`` against six-character prefixes, so
    every prefix except ``text:`` was unreachable and this string was handed
    to Playwright as a bare CSS selector, which fails to parse.
    """
    loc, how = resolve(page, "xpath://h1", {})
    assert how == "xpath://h1"
    assert await loc.inner_text() == "Hello world"


async def test_bare_selector_clicks(page: Page) -> None:
    loc, how = resolve(page, "#btn", {})
    await loc.click(timeout=TIMEOUT)
    assert how == "#btn"


async def test_select_option_awaits(page: Page) -> None:
    loc, _ = resolve(page, "#sel", {})
    await loc.select_option(["b"], timeout=TIMEOUT)
    assert await loc.input_value() == "b"


async def test_nth_selects_the_right_element(page: Page) -> None:
    """``nth=`` works for the get_by_* forms."""
    loc, _ = resolve(page, "text=two,nth=1", {})
    assert await loc.inner_text() == "two"


async def test_nth_applies_to_css_and_xpath(page: Page) -> None:
    """``nth=`` is honoured by every form, including ``css=`` and ``xpath=``.

    Both used to hardcode ``.first``, so ``css=li,nth=3`` silently returned
    the first match while the module docstring advertised ``nth=`` generally.
    """
    css_loc, _ = resolve(page, "css=li,nth=3", {})
    assert await css_loc.inner_text() == "three"

    xpath_loc, _ = resolve(page, "xpath=//li,nth=3", {})
    assert await xpath_loc.inner_text() == "three"


async def test_css_without_nth_still_returns_the_first_match(page: Page) -> None:
    loc, _ = resolve(page, "css=li", {})
    assert await loc.inner_text() == "one"


async def test_ref_map_css_path(page: Page) -> None:
    ref_map = {7: {"ref": 7, "css": "#btn", "role": "button", "name": "Go", "nth": 0}}
    loc, how = resolve(page, "7", ref_map)
    await loc.click(timeout=TIMEOUT)
    assert how == "ref 7"


async def test_ref_map_role_path(page: Page) -> None:
    ref_map = {8: {"ref": 8, "css": "", "role": "button", "name": "Go", "nth": 0}}
    loc, how = resolve(page, "8", ref_map)
    await loc.click(timeout=TIMEOUT)
    assert how == "ref 8"


async def test_ref_map_name_only_path(page: Page) -> None:
    ref_map = {9: {"ref": 9, "css": "", "role": "", "name": "Click me", "nth": 0}}
    loc, _ = resolve(page, "9", ref_map)
    await loc.click(timeout=TIMEOUT)


async def test_ref_map_nth_disambiguates_repeats(page: Page) -> None:
    """Two identical list items: nth must pick the requested one."""
    ref_map = {
        1: {"ref": 1, "css": "", "role": "listitem", "name": "", "nth": 0},
        2: {"ref": 2, "css": "", "role": "listitem", "name": "", "nth": 1},
    }
    first, _ = resolve(page, "1", ref_map)
    second, _ = resolve(page, "2", ref_map)
    assert await first.inner_text() == "one"
    assert await second.inner_text() == "two"


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", ["", "   "])
async def test_empty_target_raises(page: Page, target: str) -> None:
    with pytest.raises(TargetError, match="Empty target"):
        resolve(page, target, {})


async def test_stale_ref_raises(page: Page) -> None:
    with pytest.raises(TargetError, match="stale"):
        resolve(page, "404", {})


async def test_unknown_role_matches_nothing(page: Page) -> None:
    """An invalid role is not rejected; it simply never matches.

    This is why ``locators`` casts the role to ``Any`` rather than validating
    it, and it is a known rough edge: the caller sees a timeout, not a clear
    "unknown role" message.
    """
    loc, _ = resolve(page, "role=notarole", {})
    with pytest.raises(Exception, match="Timeout"):
        await loc.click(timeout=1_500)
