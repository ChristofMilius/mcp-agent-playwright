"""Resolve tool ``target`` arguments to Playwright locators.

A target is either:
  * a snapshot reference number (e.g. ``"12"``) that was emitted by
    ``browser_snapshot`` and stored in the server-side ref map, or
  * a locator string with an optional prefix.

Supported locator string forms (single argument, whitespace-tolerant)::

    css=#main-button          (also bare: assumes CSS selector)
    role=button,name=Submit,nth=1
    text=Hello world
    label=Username
    placeholder=Enter email
    title=Documents
    xpath=//button

``role=`` values are Playwright accessibility roles; ``name=`` narrows by
accessible name; ``nth=`` picks the Nth matching element (1-based) when
several candidates exist.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Locator, Page


class TargetError(ValueError):
    """Raised when a target cannot be resolved to a locator."""


_REF_RE = re.compile(r"^\d+$")

_SUPPORTED = {
    "css",
    "text",
    "label",
    "placeholder",
    "title",
    "xpath",
    "role",
}


def resolve(page: Page, target: str, ref_map: dict[int, dict[str, Any]]) -> tuple[Locator, str]:
    """Return ``(locator, human_readable)`` for a target string."""
    raw = (target or "").strip()
    if not raw:
        raise TargetError("Empty target.")

    if _REF_RE.fullmatch(raw):
        ref = int(raw)
        spec = ref_map.get(ref)
        if spec is None:
            raise TargetError(
                f"Reference [{ref}] is stale. Run browser_snapshot again to re-number the page."
            )
        return _from_ref(page, spec), f"ref {ref}"

    if "=" in raw:
        key, _, value = raw.partition("=")
        key = key.strip().lower()
        if key in _SUPPORTED:
            return _from_key(page, key, value.strip()), raw

    if raw[:5] in ("css:", "text:", "xpath:", "label:", "title:"):
        key, _, value = raw.partition(":")
        return _from_key(page, key.strip().lower(), value.strip()), raw

    return page.locator(raw).first, raw


def _from_ref(page: Page, spec: dict[str, Any]) -> Locator:
    css = spec.get("css") or ""
    if css:
        return page.locator(css).first
    role = spec.get("role") or ""
    name = spec.get("name") or ""
    nth = int(spec.get("nth", 0))
    if role:
        return page.get_by_role(role, name=name if name else None, exact=True).nth(nth)
    if name:
        return page.get_by_text(name, exact=True).nth(nth)
    raise TargetError(f"Reference {spec.get('ref')} has no usable locator.")


def _from_key(page: Page, key: str, value: str) -> Locator:
    fields = _split_fields(value)

    if key == "role":
        parts = [p.strip().strip('"') for p in value.split(",")]
        role = parts[0]
        name, exact = "", False
        for part in parts[1:]:
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip().lower() == "name":
                    name = v.strip().strip('"')
                elif k.strip().lower() == "exact":
                    exact = v.strip().lower() == "1"
        loc = page.get_by_role(role, name=name if name else None, exact=exact)
        return loc.nth(_nth(fields))

    bare = _bare_value(value)

    if key == "css":
        return page.locator(bare).first
    if key == "xpath":
        return page.locator(f"xpath={bare}").first
    if key == "text":
        return page.get_by_text(bare, exact=_exact(fields)).nth(_nth(fields))
    if key == "label":
        return page.get_by_label(bare, exact=False).nth(_nth(fields))
    if key == "placeholder":
        return page.get_by_placeholder(bare, exact=False).nth(_nth(fields))
    if key == "title":
        return page.get_by_title(bare).nth(_nth(fields))
    raise TargetError(f"Unsupported locator key: {key}")


def _bare_value(value: str) -> str:
    """Value with trailing field tokens (``name=``, ``nth=``, ``exact=``) removed."""
    for marker in (",nth=", ",exact=", ", name=", ",Name="):
        if marker in value:
            return value.split(marker, 1)[0].strip().strip('"')
    return value.strip().strip('"')


def _split_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part and part.split("=", 1)[0].strip().lower() in ("name", "nth", "exact"):
            k, _, v = part.partition("=")
            fields[k.strip().lower()] = v.strip().strip('"')
    return fields


def _nth(fields: dict[str, str]) -> int:
    try:
        return max(0, int(fields.get("nth", "1")) - 1)
    except ValueError:
        return 0


def _exact(fields: dict[str, str]) -> bool:
    return fields.get("exact", "").lower() == "1"