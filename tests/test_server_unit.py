"""Unit tests for the server's pure helpers and the tool-call serialiser.

No browser required. The server's tools are plain functions even though they
are registered with ``@mcp.tool()``, so they can be called directly.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_agent_playwright import server

# --------------------------------------------------------------------------- #
# _env_int -- despite the name, this returns a bool
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["0", "false", "no", "FALSE", " no ", "No"])
def test_env_int_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MCP_TEST_FLAG", raw)
    assert server._env_int("MCP_TEST_FLAG") is False


@pytest.mark.parametrize("raw", ["1", "0.0", "yes", "true", "", "anything"])
def test_env_int_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MCP_TEST_FLAG", raw)
    assert server._env_int("MCP_TEST_FLAG") is True


def test_env_int_defaults_to_true_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_TEST_FLAG", raising=False)
    assert server._env_int("MCP_TEST_FLAG") is True


def test_env_int_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name says int; the contract is bool. Pinned so a refactor is visible."""
    monkeypatch.setenv("MCP_TEST_FLAG", "0")
    assert isinstance(server._env_int("MCP_TEST_FLAG"), bool)


# --------------------------------------------------------------------------- #
# _viewport
# --------------------------------------------------------------------------- #


def test_viewport_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_PLAYWRIGHT_VIEWPORT", raising=False)
    assert server._viewport() == {"width": 1280, "height": 900}


def test_viewport_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PLAYWRIGHT_VIEWPORT", "800,600")
    assert server._viewport() == {"width": 800, "height": 600}


@pytest.mark.parametrize("raw", ["", "1280", "a,b", "1280,", ",900"])
def test_viewport_rejects_malformed_env(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Malformed viewport input raises rather than silently defaulting."""
    monkeypatch.setenv("MCP_PLAYWRIGHT_VIEWPORT", raw)
    with pytest.raises(ValueError):
        server._viewport()


# --------------------------------------------------------------------------- #
# _fmt_err
# --------------------------------------------------------------------------- #


def test_fmt_err_prefixes_tool_name() -> None:
    assert server._fmt_err("browser_click", ValueError("boom")) == "browser_click: boom"


def test_fmt_err_with_empty_message() -> None:
    assert server._fmt_err("browser_click", ValueError()) == "browser_click: "


# --------------------------------------------------------------------------- #
# _serialize
# --------------------------------------------------------------------------- #


async def test_serialize_runs_calls_one_at_a_time() -> None:
    """MCP dispatches tools concurrently; a stateful browser must not be."""
    order: list[str] = []

    @server._serialize
    async def work(tag: str) -> str:
        order.append(f"enter-{tag}")
        await asyncio.sleep(0.01)
        order.append(f"exit-{tag}")
        return tag

    results = await asyncio.gather(work("a"), work("b"), work("c"))

    assert results == ["a", "b", "c"]
    # No interleaving: every enter is immediately followed by its own exit.
    assert order == ["enter-a", "exit-a", "enter-b", "exit-b", "enter-c", "exit-c"]


async def test_serialize_preserves_metadata() -> None:
    """functools.wraps keeps the signature so the generated tool schema is right."""

    @server._serialize
    async def documented(target: str, times: int = 1) -> str:
        """Docstring survives."""
        return target * times

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Docstring survives."


async def test_serialize_propagates_exceptions() -> None:
    @server._serialize
    async def boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await boom()


# --------------------------------------------------------------------------- #
# console ring buffer
# --------------------------------------------------------------------------- #


class _FakeConsoleMessage:
    def __init__(self, type_: str, text: str) -> None:
        self.type = type_
        self.text = text


def test_on_console_appends(server_globals: None) -> None:
    server._on_console(_FakeConsoleMessage("error", "boom"))  # type: ignore[arg-type]
    server._on_console(_FakeConsoleMessage("warning", "careful"))  # type: ignore[arg-type]
    assert list(server._console_log) == [("error", "boom"), ("warning", "careful")]


def test_console_log_is_capped(server_globals: None) -> None:
    assert server._console_log.maxlen == 200
    for i in range(250):
        server._on_console(_FakeConsoleMessage("log", f"m{i}"))  # type: ignore[arg-type]
    assert len(server._console_log) == 200
    # The oldest entries were dropped, the newest kept.
    assert server._console_log[-1] == ("log", "m249")
    assert server._console_log[0] == ("log", "m50")


async def test_console_messages_filters_by_level(server_globals: None) -> None:
    for type_, text in [("debug", "d"), ("info", "i"), ("warning", "w"), ("error", "e")]:
        server._on_console(_FakeConsoleMessage(type_, text))  # type: ignore[arg-type]

    assert await server.browser_console_messages("error") == "error: e"
    assert await server.browser_console_messages("warning") == "warning: w\nerror: e"
    assert await server.browser_console_messages("info") == "info: i\nwarning: w\nerror: e"
    assert await server.browser_console_messages("debug") == "debug: d\ninfo: i\nwarning: w\nerror: e"


async def test_console_messages_unknown_level_defaults_to_warning(server_globals: None) -> None:
    server._on_console(_FakeConsoleMessage("error", "e"))  # type: ignore[arg-type]
    assert await server.browser_console_messages("nonsense") == "error: e"


async def test_console_messages_empty(server_globals: None) -> None:
    assert await server.browser_console_messages() == "No matching console messages."


async def test_console_messages_caps_output_at_50(server_globals: None) -> None:
    for i in range(60):
        server._on_console(_FakeConsoleMessage("error", f"e{i}"))  # type: ignore[arg-type]
    out = await server.browser_console_messages("error")
    assert len(out.splitlines()) == 50
    assert out.splitlines()[-1] == "error: e59"


# --------------------------------------------------------------------------- #
# _current
# --------------------------------------------------------------------------- #


def test_current_raises_when_no_tabs(server_globals: None) -> None:
    server._pages.clear()
    with pytest.raises(IndexError):
        server._current()


def test_current_returns_last_tab(server_globals: None) -> None:
    server._pages.clear()
    server._pages.extend(["first", "second"])  # type: ignore[arg-type]
    assert server._current() == "second"
