"""Post-action verification. Never assume an action worked just because Playwright did not raise."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Verification:
    ok: Optional[bool]  # True = confirmed, False = checked and failed, None = could not be checked
    detail: str


def state_changed(before: dict, after: dict) -> bool:
    return any(before.get(k) != after.get(k) for k in ("url", "title", "text_hash", "form_hash"))


def verify_click(session, before: dict, expect_text: str | None = None, timeout: float = 3.0) -> Verification:
    """Success = the expected text appeared, or (without expectation) the page observably changed."""
    deadline = time.monotonic() + timeout
    while True:
        if expect_text and session.text_visible(expect_text):
            return Verification(True, f"expected text {expect_text!r} is visible")
        after = session.page_state()
        if not expect_text and state_changed(before, after):
            what = "URL changed" if before.get("url") != after.get("url") else "page content changed"
            return Verification(True, what)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if expect_text:
        return Verification(False, f"expected text {expect_text!r} did not appear within {timeout:.0f}s")
    return Verification(False, "no observable change after the action (it may not have had an effect)")


def verify_typed(session, resolved, expected: str) -> Verification:
    actual = session.read_value(resolved)
    if actual is None:
        return Verification(None, "could not read the field value back")
    if actual.strip() == expected.strip():
        return Verification(True, "field contains the expected value")
    if resolved.info.get("type") == "number" and _num_equal(actual, expected):
        return Verification(True, "numeric field contains the expected value")
    return Verification(False, f"field contains {len(actual)} characters that do not match what was typed")


def _num_equal(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def verify_selected(session, resolved, expected: str) -> Verification:
    sel = session.read_selected(resolved)
    if not sel:
        return Verification(None, "could not read the selected option back")
    want = expected.strip().lower()
    if want in {str(sel.get("text", "")).strip().lower(), str(sel.get("value", "")).strip().lower()}:
        return Verification(True, f"selected option is {sel.get('text')!r}")
    return Verification(False, f"selected option is {sel.get('text')!r}, expected {expected!r}")


def verify_scroll(before: dict, after: dict) -> Verification:
    if before.get("y") != after.get("y"):
        return Verification(True, f"scrolled from {before.get('y')} to {after.get('y')} (max {after.get('max')})")
    return Verification(False, "the view did not move; this is probably the end (or start) of the scrollable area")


def verify_nonempty(data: Any, noun: str) -> Verification:
    if data:
        return Verification(True, f"{noun} extracted")
    return Verification(False, f"no {noun} found in the requested scope")
