"""Unit tests for target parsing and locator dispatch in ``locators``.

These use a recording fake page rather than a real browser, so each test can
assert *which* Playwright call was made with *which* arguments. That pins the
dispatch table -- the part most likely to regress -- without needing Chromium.
``test_locators_browser.py`` covers the same forms against a real page.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_agent_playwright.locators import (
    TargetError,
    _bare_value,
    _exact,
    _nth,
    _split_fields,
    resolve,
)


class FakeLocator:
    """Records the ``.first`` / ``.nth()`` chain applied to it."""

    def __init__(self, call: tuple[str, ...]) -> None:
        self.call = call
        self.chain: list[Any] = []

    @property
    def first(self) -> FakeLocator:
        self.chain.append("first")
        return self

    def nth(self, index: int) -> FakeLocator:
        self.chain.append(("nth", index))
        return self


class FakePage:
    """A Playwright page stand-in that records locator construction calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.last: FakeLocator | None = None

    def _record(self, *call: Any) -> FakeLocator:
        self.calls.append(call)
        self.last = FakeLocator(call)
        return self.last

    def locator(self, selector: str) -> FakeLocator:
        return self._record("locator", selector)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False) -> FakeLocator:
        return self._record("get_by_role", role, name, exact)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return self._record("get_by_text", text, exact)

    def get_by_label(self, text: str, exact: bool = False) -> FakeLocator:
        return self._record("get_by_label", text, exact)

    def get_by_placeholder(self, text: str, exact: bool = False) -> FakeLocator:
        return self._record("get_by_placeholder", text, exact)

    def get_by_title(self, title: str) -> FakeLocator:
        return self._record("get_by_title", title)


# --------------------------------------------------------------------------- #
# _split_fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Submit", {}),
        ("Submit,nth=2", {"nth": "2"}),
        ("Go,name=Send,exact=1", {"name": "Send", "exact": "1"}),
        ("Go,Name=Send", {"name": "Send"}),
        ("Go,NTH=3", {"nth": "3"}),
        ("Go,unknown=x", {}),
        ("Go,=1", {}),
        ("", {}),
    ],
)
def test_split_fields(value: str, expected: dict[str, str]) -> None:
    assert _split_fields(value) == expected


# --------------------------------------------------------------------------- #
# _bare_value
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Hello world", "Hello world"),
        ("Hello world,nth=2", "Hello world"),
        ("Hello world,exact=1", "Hello world"),
        ("Hello world, name=Send", "Hello world"),
        ("Hello world ,Name=Send", "Hello world"),
        ('"quoted"', "quoted"),
        ("", ""),
        ("   spaced   ", "spaced"),
    ],
)
def test_bare_value(value: str, expected: str) -> None:
    assert _bare_value(value) == expected


# --------------------------------------------------------------------------- #
# _nth / _exact
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, 0),  # 1-based default
        ({"nth": "1"}, 0),
        ({"nth": "2"}, 1),
        ({"nth": "9"}, 8),
        ({"nth": "0"}, 0),  # clamped, never negative
        ({"nth": "-5"}, 0),  # clamped
        ({"nth": "abc"}, 0),  # ValueError swallowed
    ],
)
def test_nth(fields: dict[str, str], expected: int) -> None:
    assert _nth(fields) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [({}, False), ({"exact": "1"}, True), ({"exact": "0"}, False), ({"exact": "true"}, False)],
)
def test_exact(fields: dict[str, str], expected: bool) -> None:
    assert _exact(fields) is expected


# --------------------------------------------------------------------------- #
# resolve(): error paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", ["", "   "])
def test_resolve_rejects_empty_target(target: str) -> None:
    with pytest.raises(TargetError, match="Empty target"):
        resolve(FakePage(), target, {})  # type: ignore[arg-type]


def test_resolve_rejects_stale_ref() -> None:
    with pytest.raises(TargetError, match="stale"):
        resolve(FakePage(), "12", {})  # type: ignore[arg-type]


def test_resolve_rejects_ref_without_usable_locator() -> None:
    with pytest.raises(TargetError, match="no usable locator"):
        resolve(FakePage(), "3", {3: {"ref": 3, "css": "", "role": "", "name": ""}})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# resolve(): key= dispatch
# --------------------------------------------------------------------------- #


def test_resolve_css() -> None:
    page = FakePage()
    _, how = resolve(page, "css=#main", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "#main")]
    assert page.last is not None and page.last.chain == ["first"]
    assert how == "css=#main"


def test_resolve_text() -> None:
    page = FakePage()
    resolve(page, "text=Hello world", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_text", "Hello world", False)]
    assert page.last is not None and page.last.chain == [("nth", 0)]


def test_resolve_text_with_exact_and_nth() -> None:
    """Known bug: with several trailing fields, only the first is stripped.

    ``_bare_value`` checks its markers in a fixed order and splits on the first
    match, so for ``"Hello,exact=1,nth=3"`` it splits at ``,nth=`` and leaves
    ``exact=1`` glued to the value. The caller then searches for the literal
    text "Hello,exact=1", which matches nothing. Same for any
    ``name=``/``exact=`` combination ordered before ``nth=``.

    Characterisation test: it pins the current behaviour. If this ever starts
    receiving ``"Hello"``, the bug is fixed and this expectation is wrong.
    """
    page = FakePage()
    resolve(page, "text=Hello,exact=1,nth=3", {})  # type: ignore[arg-type]
    # nth=3 -> index 2 is applied correctly; only the value is mangled.
    assert page.calls == [("get_by_text", "Hello,exact=1", True)]
    assert page.last is not None and page.last.chain == [("nth", 2)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Hello,exact=1", "Hello"),  # single trailing field: fine
        ("Hello,nth=2", "Hello"),  # single trailing field: fine
    ],
)
def test_bare_value_single_trailing_field_is_correct(value: str, expected: str) -> None:
    assert _bare_value(value) == expected


def test_resolve_label() -> None:
    page = FakePage()
    resolve(page, "label=Username", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_label", "Username", False)]


def test_resolve_placeholder() -> None:
    page = FakePage()
    resolve(page, "placeholder=Enter email", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_placeholder", "Enter email", False)]


def test_resolve_title() -> None:
    page = FakePage()
    resolve(page, "title=Documents", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_title", "Documents")]


def test_resolve_xpath_key() -> None:
    page = FakePage()
    resolve(page, "xpath=//button", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "xpath=//button")]


def test_resolve_role_with_name() -> None:
    page = FakePage()
    resolve(page, "role=button,name=Submit", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_role", "button", "Submit", False)]


def test_resolve_role_exact_flag() -> None:
    page = FakePage()
    resolve(page, "role=button,name=Submit,exact=1,nth=2", {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_role", "button", "Submit", True)]
    assert page.last is not None and page.last.chain == [("nth", 1)]


def test_resolve_role_quoted_and_spaced() -> None:
    page = FakePage()
    resolve(page, ' role = button , name = "Save now" ', {})  # type: ignore[arg-type]
    assert page.calls == [("get_by_role", "button", "Save now", False)]


def test_resolve_is_case_insensitive_on_key() -> None:
    page = FakePage()
    resolve(page, "CSS=#main", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "#main")]


# --------------------------------------------------------------------------- #
# resolve(): fall-through paths
# --------------------------------------------------------------------------- #


def test_resolve_bare_selector_is_css() -> None:
    page = FakePage()
    _, how = resolve(page, "#main-button", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "#main-button")]
    assert page.last is not None and page.last.chain == ["first"]
    assert how == "#main-button"


@pytest.mark.parametrize("prefix", ["css:", "text:", "label:", "title:"])
def test_resolve_prefix_form(prefix: str) -> None:
    """``css:#id`` style input is routed to the same handler as ``css=#id``."""
    page = FakePage()
    resolve(page, f"{prefix}value", {})  # type: ignore[arg-type]
    assert page.calls, f"{prefix} produced no locator call"


def test_resolve_prefix_form_xpath() -> None:
    """``xpath://button`` -- documents what the prefix branch actually does.

    ``resolve`` tests ``raw[:5]`` against 6-character prefixes, so ``xpath:``
    (six chars) never matches and falls through to the bare-CSS path.
    """
    page = FakePage()
    resolve(page, "xpath://button", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "xpath://button")]


def test_resolve_unknown_key_falls_through_to_css() -> None:
    page = FakePage()
    resolve(page, "weird=thing", {})  # type: ignore[arg-type]
    assert page.calls == [("locator", "weird=thing")]


# --------------------------------------------------------------------------- #
# resolve(): ref map
# --------------------------------------------------------------------------- #


def test_resolve_ref_prefers_css() -> None:
    page = FakePage()
    _, how = resolve(page, "5", {5: {"ref": 5, "css": "#from-css", "role": "button", "name": "Go"}})  # type: ignore[arg-type]
    assert page.calls == [("locator", "#from-css")]
    assert page.last is not None and page.last.chain == ["first"]
    assert how == "ref 5"


def test_resolve_ref_by_role_and_name() -> None:
    page = FakePage()
    resolve(page, "5", {5: {"ref": 5, "css": "", "role": "button", "name": "Go", "nth": 2}})  # type: ignore[arg-type]
    assert page.calls == [("get_by_role", "button", "Go", True)]
    assert page.last is not None and page.last.chain == [("nth", 2)]


def test_resolve_ref_by_name_only_uses_text() -> None:
    page = FakePage()
    resolve(page, "5", {5: {"ref": 5, "css": "", "role": "", "name": "Go", "nth": 0}})  # type: ignore[arg-type]
    assert page.calls == [("get_by_text", "Go", True)]


def test_resolve_ref_map_is_not_mutated() -> None:
    ref_map = {5: {"ref": 5, "css": "#x", "role": "", "name": "", "nth": 0}}
    snapshot = {"ref": 5, "css": "#x", "role": "", "name": "", "nth": 0}
    resolve(FakePage(), "5", ref_map)  # type: ignore[arg-type]
    assert ref_map[5] == snapshot
