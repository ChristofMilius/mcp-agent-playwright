"""MCP Playwright server: exposes browser automation as MCP tools over stdio.

The server lazily launches a Playwright browser on first use and keeps it alive
between tool calls, so LM Studio agents can navigate, read snapshots, click,
type, and screenshot pages.

Configuration via environment variables:
  MCP_PLAYWRIGHT_HEADLESS     "1" (default) or "0" for a visible browser window
  MCP_PLAYWRIGHT_BROWSER      chromium (default), firefox, or webkit
  MCP_PLAYWRIGHT_VIEWPORT     "1280,900" (default)
  MCP_PLAYWRIGHT_SCREENSHOT_DIR  where browser_screenshot saves files
                               (default: ./screenshots)
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import json
import os
from collections import deque
from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from playwright.async_api import Page, Playwright, async_playwright
from pydantic import Field

from mcp_agent_playwright import __version__
from mcp_agent_playwright.locators import TargetError, resolve

mcp = MCPServer(
    "mcp-agent-playwright",
    version=__version__,
    instructions=(
        "Browser automation via Playwright. Start with browser_navigate, then "
        "browser_snapshot to see the page as a numbered tree. Interact using a "
        "[ref] number or a locator string. Always snapshot again after actions."
    ),
)

# --------------------------------------------------------------------------- #
# Browser singleton
# --------------------------------------------------------------------------- #

_playwright: Playwright | None = None
_browser: Any = None
_context: Any = None
_pages: list[Page] = []
_ref_map: dict[int, dict[str, Any]] = {}
_console_log: deque[tuple[str, str]] = deque(maxlen=200)


def _env_int(name: str) -> bool:
    return os.getenv(name, "1").strip().lower() not in ("0", "false", "no")


def _viewport() -> dict[str, int]:
    w, h = os.getenv("MCP_PLAYWRIGHT_VIEWPORT", "1280,900").split(",")
    return {"width": int(w), "height": int(h)}


async def _ensure_browser() -> Any:
    global _playwright, _browser, _context
    if _browser is None:
        _playwright = await async_playwright().start()
        browser_type = os.getenv("MCP_PLAYWRIGHT_BROWSER", "chromium")
        _browser = await getattr(_playwright, browser_type).launch(
            headless=_env_int("MCP_PLAYWRIGHT_HEADLESS")
        )
        _context = await _browser.new_context(viewport=_viewport())
        _context.set_default_timeout(30_000)
    return _context


async def _get_page() -> Page:
    await _ensure_browser()
    if not _pages:
        await new_tab("about:blank")
    return _current()


def _current() -> Page:
    return _pages[-1]


async def new_tab(url: str) -> Page:
    await _ensure_browser()
    page = await _context.new_page()
    page.on("console", _on_console)
    if url and (url := url.strip()):
        await page.goto(url, wait_until="load")
    _pages.append(page)
    return page


def _on_console(msg: Any) -> None:
    _console_log.append((msg.type, msg.text))


async def _close_browser() -> None:
    global _playwright, _browser, _context, _pages
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()
    _browser = _context = _playwright = None
    _pages = []
    _ref_map.clear()
    _console_log.clear()


def _atexit_close() -> None:
    try:
        asyncio.run(_close_browser())
    except Exception:  # noqa: BLE001
        pass


atexit.register(_atexit_close)


def _fmt_err(tool: str, e: Exception) -> str:
    return f"{tool}: {e}"


_serial = asyncio.Lock()


def _serialize(fn):
    """Run each tool to completion before the next starts.

    MCP dispatches tool calls concurrently; a stateful browser must be touched
    by exactly one call at a time. functools.wraps keeps the original signature
    so the generated tool schema stays correct.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with _serial:
            return await fn(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #

_SNAPSHOT_JS = """({max}) => {
  const REFW = new Set(['button','link','checkbox','radio','combobox','textbox',
    'option','tab','switch','slider','spinbutton','menuitem','heading','img']);
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','LINK',
    'META','IFRAME','SVG','PATH','STOP','SCRIPT']);

  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1) {
      if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
      let sel = n.tagName.toLowerCase();
      const parent = n.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === n.tagName);
        if (sibs.length > 1) sel += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
      }
      parts.unshift(sel);
      n = parent;
      if (parts.length > 30) break;
    }
    return parts.join('>');
  };

  const labelFor = (el) => {
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return (lbl.innerText || '').trim();
    }
    return '';
  };

  const nameOf = (el) => {
    let n = (el.getAttribute('aria-label') || '').trim();
    if (n) return n;
    const lby = el.getAttribute('aria-labelledby');
    if (lby) {
      n = lby.split(/\\s+/).map(id => {
        const t = document.getElementById(id);
        return t ? (t.innerText || t.textContent || '').trim() : '';
      }).filter(Boolean).join(' ');
      if (n) return n.slice(0, 120);
    }
    if (el.tagName === 'IMG' || el.tagName === 'AREA') {
      return (el.getAttribute('alt') || el.title || '').trim();
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      const lbl = labelFor(el);
      if (lbl) return lbl;
      return (el.getAttribute('placeholder') || '').trim();
    }
    n = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    return n.length > 120 ? n.slice(0, 120) + '…' : n;
  };

  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName;
    if (t === 'A' || t === 'AREA') return 'link';
    if (t === 'BUTTON') return 'button';
    if (t === 'INPUT') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'submit' || ty === 'button' || ty === 'reset') return 'button';
      return ty === 'search' ? 'searchbox' : 'textbox';
    }
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'SELECT') return 'combobox';
    if (t === 'OPTION') return 'option';
    if (t === 'NAV') return 'navigation';
    if (t === 'HEADER') return 'banner';
    if (t === 'MAIN') return 'main';
    if (t === 'ARTICLE') return 'article';
    if (t === 'FOOTER') return 'contentinfo';
    if (t === 'ASIDE') return 'complementary';
    if (t === 'IMG') return 'img';
    if (t === 'SUMMARY') return 'button';
    if (t.length === 2 && t[0] === 'H') return 'heading';
    if (el.tabIndex >= 0) return 'generic';
    return 'paragraph';
  };

  const isVisible = (el) => {
    if (el.hidden) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0)
      return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  };

  const out = [];
  let cap = max;

  const walk = (el, depth) => {
    if (out.length >= cap || depth > 10) return;
    if (el.nodeType !== 1) return;
    if (SKIP_TAGS.has(el.tagName)) return;
    let visible = false;
    try { visible = isVisible(el); } catch (e) { visible = false; }
    if (!visible && el !== document.body) return;

    let role = 'paragraph';
    try { role = roleOf(el); } catch (e) { role = 'paragraph'; }
    let name = '';
    try { name = nameOf(el); } catch (e) { name = ''; }

    const ta = el.tagName;
    const interactiveTag = ta === 'INPUT' || ta === 'TEXTAREA' || ta === 'SELECT' ||
      ta === 'BUTTON' || ta === 'A' || ta === 'OPTION' || el.tabIndex >= 0;
    const refworthy = interactiveTag || REFW.has(role) ||
      (role === 'img' && Boolean(name));

    let node = { depth, role,
      name: name && name.length > 160 ? name.slice(0, 160) + '…' : name,
      refworthy, css: cssPath(el) };
    if (role === 'textbox' || role === 'searchbox' || role === 'spinbutton') {
      const ty = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();
      if (ty === 'password') node.value = '********';
      else node.value = el.value != null ? String(el.value) : '';
    } else if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      if (el.checked) node.checked = true;
    } else if (role === 'option') {
      if (el.selected) node.selected = true;
    } else if (role === 'heading') {
      node.level = parseInt(el.tagName[1], 10);
    }
    out.push(node);

    for (const child of Array.from(el.children)) walk(child, depth + 1);
  };

  try { walk(document.body, 0); } catch (e) { /* keep partial */ }
  return out;
}"""


async def _build_snapshot(page: Page, max_nodes: int) -> str:
    global _ref_map
    _ref_map = {}
    node_cap = max(10, min(int(max_nodes), 400))

    try:
        nodes = await page.evaluate(_SNAPSHOT_JS, {"max": node_cap})
    except Exception as e:  # noqa: BLE001
        return f"browser_snapshot: {e}"

    if not nodes:
        return f"Page has no content ({page.url}). Call browser_navigate to a real page first."

    lines: list[str] = []
    ordinal: dict[tuple[str, str], int] = {}
    for n in nodes:
        role = n.get("role") or "generic"
        name = (n.get("name") or "").strip()
        depth = n.get("depth", 0)
        indent = "  " * min(depth, 12)

        parts = []
        if name:
            parts.append(f'"{name}"')
        if n.get("value") is not None and n["value"] != "":
            parts.append(f'value="{n["value"]}"')
        if n.get("checked") is True:
            parts.append("checked=True")
        if n.get("selected") is True:
            parts.append("selected=True")
        if n.get("level"):
            parts.append(f"level={n['level']}")

        has_ref = bool(n.get("refworthy"))
        label = ""
        if has_ref and len(_ref_map) < node_cap:
            ref = len(_ref_map) + 1
            key = (role, name)
            ordinal[key] = ordinal.get(key, 0) + 1
            _ref_map[ref] = {
                "ref": ref, "css": n.get("css", ""),
                "role": role, "name": name, "nth": ordinal[key] - 1,
            }
            label = f"[{ref}] {role}"
            if parts:
                label += " " + " ".join(parts)
        else:
            label = f"- {role}"
            if parts:
                label += " " + " ".join(parts)
        lines.append(f"{indent}{label}")

    lines.append("")
    lines.append("Interact via: browser_click(12), browser_type(13, 'text'), "
                 "or locators: role=button,name=Submit, css=#id, text=Hello.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool()
@_serialize
async def browser_status() -> str:
    """Report browser state: current page URL, title, and open tab list.

Use this first if you are unsure whether a browser is running.
    """
    try:
        page = _current()
    except IndexError:
        return "No browser running. Call browser_navigate(url) to start one."
    return (f"url: {page.url}\n"
            f"title: {await page.title()}\n"
            f"tabs: {len(_pages)} (index {len(_pages) - 1} active)")


@mcp.tool()
@_serialize
async def browser_navigate(url: Annotated[str, Field(description="Full URL to open (https://...).")]) -> str:
    """Open a URL in the browser. Creates a page if none exists; otherwise navigates the active tab."""
    try:
        target = url.strip()
        if not target.startswith(("http://", "https://", "about:", "data:", "file:")):
            target = "https://" + target
        page = await _get_page()
        await page.goto(target, wait_until="load")
        return f"navigated to: {page.url}\ntitle: {await page.title()}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_navigate", e)


@mcp.tool()
@_serialize
async def browser_snapshot(max_nodes: Annotated[int, Field(description="Max lines to return (10-400).", ge=10, le=400)] = 240) -> str:
    """Read the current page as a numbered accessibility tree.

    Interactive elements are prefixed with [ref] numbers. Pass a ref to any
    browser_* tool that accepts a target. Re-run after every action.
    """
    try:
        page = await _get_page()
        return await _build_snapshot(page, max_nodes)
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_snapshot", e)


@mcp.tool()
@_serialize
async def browser_click(target: Annotated[str, Field(description='A [ref] number from browser_snapshot, or a locator (css=#id, role=button,name=Submit, text=...).')]) -> str:
    """Click an element: snapshot ref number or locator string."""
    try:
        page = await _get_page()
        loc, how = resolve(page, target, _ref_map)
        await loc.click(timeout=10_000)
        return f"clicked {how} on: {page.url}"
    except TargetError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_click", e)


@mcp.tool()
@_serialize
async def browser_type(
    target: Annotated[str, Field(description="Snapshot ref or locator of a text field.")],
    text: Annotated[str, Field(description="Text to enter.")],
    enter: Annotated[bool, Field(description="Press Enter afterwards.", default=False)] = False,
) -> str:
    """Type text into an input field, replacing its current content."""
    try:
        page = await _get_page()
        loc, how = resolve(page, target, _ref_map)
        await loc.fill(text, timeout=10_000)
        if enter:
            await page.keyboard.press("Enter")
            return f"typed into {how} and pressed Enter on: {page.url}"
        return f"typed into {how} on: {page.url}"
    except TargetError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_type", e)


@mcp.tool()
@_serialize
async def browser_press_key(key: Annotated[str, Field(description="Key name: Enter, Tab, Escape, ArrowDown, Backspace, F5, or a single char.")]) -> str:
    """Press a keyboard key on the page. For Enter after typing use browser_type(..., enter=True)."""
    try:
        await (await _get_page()).keyboard.press(key)
        return f"pressed key: {key}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_press_key", e)


@mcp.tool()
@_serialize
async def browser_hover(target: Annotated[str, Field(description="Snapshot ref or locator.")]) -> str:
    """Move the mouse cursor over an element (for hover menus/tooltips)."""
    try:
        page = await _get_page()
        loc, how = resolve(page, target, _ref_map)
        await loc.hover(timeout=10_000)
        return f"hovered {how}"
    except TargetError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_hover", e)


@mcp.tool()
@_serialize
async def browser_select_option(
    target: Annotated[str, Field(description="Snapshot ref or locator of the <select> element.")],
    values: Annotated[list[str], Field(description="Option values or labels to select.")],
) -> str:
    """Select an option in a dropdown (<select>) by value or label."""
    try:
        page = await _get_page()
        loc, how = resolve(page, target, _ref_map)
        await loc.select_option(values, timeout=10_000)
        return f"selected {', '.join(values)} in {how}"
    except TargetError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_select_option", e)


@mcp.tool()
@_serialize
async def browser_go_back() -> str:
    """Navigate the active tab back in history."""
    try:
        page = await _get_page()
        await page.go_back()
        return f"back to: {page.url}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_go_back", e)


@mcp.tool()
@_serialize
async def browser_go_forward() -> str:
    """Navigate the active tab forward in history."""
    try:
        page = await _get_page()
        await page.go_forward()
        return f"forward to: {page.url}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_go_forward", e)


@mcp.tool()
@_serialize
async def browser_wait_for_text(
    text: Annotated[str, Field(description="Text to wait for (substring match).")],
    timeout: Annotated[float, Field(description="Max wait in seconds.", gt=0.5, le=120)] = 15.0,
) -> str:
    """Wait until a piece of text becomes visible on the page, or the timeout runs out."""
    try:
        page = await _get_page()
        await page.get_by_text(text, exact=False).first.wait_for(
            state="visible", timeout=int(timeout * 1000)
        )
        return f"text visible: {text!r}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_wait_for_text (timed out)", e)


@mcp.tool()
@_serialize
async def browser_wait_for_timeout(ms: Annotated[float, Field(description="Milliseconds to wait.", gt=0, le=120_000)] = 500.0) -> str:
    """Wait a fixed amount of time (page sleeps/progress bars)."""
    await asyncio.sleep(ms / 1000)
    return f"waited {int(ms)} ms"


@mcp.tool()
@_serialize
async def browser_screenshot(
    filename: Annotated[str, Field(description="Optional file name; saved under the screenshot dir.")] = "",
    full_page: Annotated[bool, Field(description="Capture the whole scrollable page.", default=False)] = False,
) -> str:
    """Take a screenshot of the current viewport and save it as a PNG file.

    Returns the absolute file path and image size. Does NOT show the image to
    the model — use browser_snapshot for content.
    """
    try:
        out_dir = os.getenv("MCP_PLAYWRIGHT_SCREENSHOT_DIR", "screenshots")
        os.makedirs(out_dir, exist_ok=True)
        name = filename.strip() or datetime.now().strftime("screenshot-%Y%m%d-%H%M%S.png")
        path = os.path.join(out_dir, name)
        page = await _get_page()
        await page.screenshot(path=path, full_page=full_page)
        return f"saved: {os.path.abspath(path)}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_screenshot", e)


@mcp.tool()
@_serialize
async def browser_tabs() -> str:
    """List open browser tabs with index, title and URL."""
    if not _pages:
        return "No tabs open."
    lines = []
    for i, p in enumerate(_pages):
        cur = " <- active" if i == len(_pages) - 1 else ""
        title = await p.title()
        lines.append(f"[{i}] {title or '(untitled)'}  {p.url}{cur}")
    return "\n".join(lines)


@mcp.tool()
@_serialize
async def browser_tab_new(url: Annotated[str, Field(description="URL to open in the new tab.")] = "about:blank") -> str:
    """Open a new browser tab and switch to it."""
    try:
        page = await new_tab(url)
        return f"opened tab {len(_pages) - 1}: {page.url}"
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_tab_new", e)


@mcp.tool()
@_serialize
async def browser_tab_switch(index: Annotated[int, Field(description="Tab index from browser_tabs(), 0-based.", ge=0)]) -> str:
    """Switch to an existing tab by its index from browser_tabs()."""
    if not _pages or index >= len(_pages):
        return f"no tab at index {index}. Use browser_tabs() to list them."
    page = _pages[index]
    await page.bring_to_front()
    page = _pages.pop(index)
    _pages.append(page)
    title = await page.title()
    return f"switched to tab: {title or '(untitled)'}  {page.url}"


@mcp.tool()
@_serialize
async def browser_tab_close(index: Annotated[int, Field(description="Tab index to close; omit to close the active tab.", ge=0)] | None = None) -> str:
    """Close a browser tab. Defaults to the active tab."""
    if not _pages:
        return "No tabs open."
    if index is None:
        index = len(_pages) - 1
    if index >= len(_pages) or index < 0:
        return f"no tab at index {index}. Use browser_tabs() to list them."
    page = _pages.pop(index)
    await page.close()
    return f"closed tab {index}"


@mcp.tool()
@_serialize
async def browser_evaluate(js: Annotated[str, Field(description="JavaScript expression or statement body to run in the page. Return JSON-serializable values.")]) -> str:
    """Run arbitrary JavaScript in the page and return the result.

    Example: `document.title` or `({hrefs: [...document.querySelectorAll('a')].map(a => a.href)})`.
    Use for scraping data the accessibility snapshot cannot express.
    """
    try:
        page = await _get_page()
        result = await page.evaluate(js)
        return json.dumps(result, ensure_ascii=False, default=str)[:4000]
    except Exception as e:  # noqa: BLE001
        return _fmt_err("browser_evaluate", e)


@mcp.tool()
@_serialize
async def browser_console_messages(
    level: Annotated[str, Field(description='Lowest level to include: "debug", "info", "warning", "error".')] = "warning",
) -> str:
    """Return recent browser console messages (errors, warnings, logs)."""
    order = {"debug": 0, "log": 0, "info": 1, "warning": 2, "error": 3}
    min_level = order.get(level.strip().lower(), 2)
    rows = [f"{t}: {m}" for t, m in _console_log if order.get(t, 0) >= min_level]
    return "\n".join(rows[-50:]) or "No matching console messages."


@mcp.tool()
@_serialize
async def browser_close() -> str:
    """Shut down the browser and forget all state (tabs, refs, console log)."""
    await _close_browser()
    return "Browser closed. Call browser_navigate to start a new session."


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the MCP Playwright server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()