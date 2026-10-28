"""Playwright controller ("the hands").

One Chromium process per worker; one isolated, cookie-less context per task. Every request the
page makes is intercepted and validated against the SSRF / domain policy, and every response's
actual server address is checked afterwards to catch DNS-rebinding.
"""
from __future__ import annotations

import logging
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from ..utils import looks_sensitive
from .actions import ActionError, Target
from .ocr import AmbiguousMatch, OcrEngine, OcrUnavailable, find_text, pick_unique, to_viewport
from .policies import NavPolicy, PolicyViolation, is_public_ip

log = logging.getLogger(__name__)

MAX_PAGES = 4
ARIA_ROLES = {"button", "link", "textbox", "searchbox", "combobox", "listbox", "option", "checkbox", "radio",
              "tab", "menuitem", "switch", "heading", "cell", "row", "img", "slider", "spinbutton"}
TEXT_ROLES = ("button", "link", "tab", "menuitem", "checkbox", "radio", "option", "switch", "combobox", "textbox", "searchbox")

JS_PAGE_STATE = """() => {
  const t = document.body ? document.body.innerText : '';
  const hashStr = (s) => { let h = 0; for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; } return h; };
  const fs = [];
  document.querySelectorAll('input,select,textarea').forEach(e => {
    if (e.type === 'password') return;
    fs.push((e.type === 'checkbox' || e.type === 'radio') ? (e.checked ? '1' : '0') : (e.value || ''));
  });
  const se = document.scrollingElement || document.documentElement;
  return { url: location.href, title: document.title, text_hash: hashStr(t.slice(0, 20000)), text_len: t.length,
           form_hash: hashStr(fs.join('|')), y: Math.round(se.scrollTop),
           max: Math.max(0, se.scrollHeight - innerHeight), viewport_h: innerHeight };
}"""

_INFO_FN = """(el) => {
  const tag = el.tagName.toLowerCase();
  const attrType = (el.getAttribute('type') || '').toLowerCase();
  const type = attrType || (tag === 'button' ? 'submit' : '');
  const lab = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || '';
  const f = el.closest('form') || el.closest('[role=form]');
  const sens = !!f && !!f.querySelector('input[type=password], input[autocomplete^="cc-"], input[name*="card" i], input[name*="cvv" i], input[name*="cvc" i]');
  return { tag, type, name: (lab || el.innerText || el.value || el.placeholder || el.title || '').toString().trim().slice(0, 120),
           placeholder: el.getAttribute('placeholder') || '', autocomplete: el.getAttribute('autocomplete') || '',
           href: el.getAttribute('href') || '', form_sensitive: sens, disabled: !!el.disabled,
           editable: el.isContentEditable || ['input','textarea','select'].includes(tag) };
}"""
JS_ELEMENT_INFO = _INFO_FN
JS_INFO_AT_POINT = "([x, y]) => { const el = document.elementFromPoint(x, y); if (!el) return null; return (" + _INFO_FN + ")(el); }"
JS_READ_VALUE = ("(el) => { if (el.isContentEditable) return el.innerText; "
                 "if (el.tagName === 'SELECT' || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return String(el.value); return null; }")
JS_ACTIVE_VALUE = ("() => { const el = document.activeElement; if (!el) return null; "
                   "if (el.isContentEditable) return el.innerText; "
                   "if (['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) return String(el.value); return null; }")
JS_SELECTED = "(el) => { const o = el.options && el.options[el.selectedIndex]; return o ? {text: o.text.trim(), value: o.value} : null; }"
JS_OPTIONS = "(el) => [...el.options].map(o => o.text.trim())"
JS_LINKS = """(root) => {
  const nodes = [...root.querySelectorAll('a[href]')]; if (root.matches && root.matches('a[href]')) nodes.unshift(root);
  const seen = new Set(); const out = [];
  for (const a of nodes) {
    const r = a.getBoundingClientRect(); const s = getComputedStyle(a);
    if (r.width < 1 || r.height < 1 || s.visibility === 'hidden' || s.display === 'none') continue;
    const href = a.href; if (!href || seen.has(href)) continue; seen.add(href);
    out.push({ text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\\s+/g, ' ').slice(0, 120), href });
    if (out.length >= 400) break;
  }
  return out;
}"""
JS_NEAREST_BY_CONTAINER = """([a, cs]) => {
  const size = (n) => n.querySelectorAll('*').length;
  const common = (x, y) => { let n = x; while (n && !n.contains(y)) n = n.parentElement; return n; };
  const scored = cs.map((c, i) => { const n = common(a, c); return [n ? size(n) : 1e9, i]; });
  scored.sort((p, q) => p[0] - q[0]);
  if (!scored.length || (scored.length > 1 && scored[0][0] === scored[1][0])) return -1;
  return scored[0][1];
}"""
JS_FOCUSED_INFO = "() => { const el = document.activeElement; if (!el || el === document.body) return null; return (" + _INFO_FN + ")(el); }"


class BrowserStartError(RuntimeError):
    pass


def _short(exc: Exception, limit: int = 300) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return re.sub(r"\s+", " ", first)[:limit]


@dataclass
class Resolved:
    description: str
    method: str  # "dom" | "ocr"
    locator: Any = None
    point: tuple[float, float] | None = None
    info: dict = field(default_factory=dict)

    @property
    def sensitive(self) -> bool:
        i = self.info or {}
        return i.get("type") == "password" or any(
            looks_sensitive(i.get(k)) for k in ("name", "placeholder", "autocomplete")
        ) or str(i.get("autocomplete", "")).startswith("cc-")

    def descriptors(self) -> list[str]:
        i = self.info or {}
        return [i.get("name"), i.get("href"), i.get("type")]


class BrowserManager:
    """Owns the Playwright driver and the Chromium process for this worker process."""

    def __init__(self, settings):
        self.settings = settings
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()

    def _launch(self) -> None:
        s = self.settings
        self._pw = sync_playwright().start()
        args = ["--disable-extensions", "--disable-background-networking", "--no-first-run", "--mute-audio",
                "--disable-component-update", "--disable-sync", "--disable-setuid-sandbox",]
        try:
            self._browser = self._pw.chromium.launch(headless=True, chromium_sandbox=s.browser_sandbox, args=args)
        except Exception as exc:
            self.close()
            hint = ""
            if s.browser_sandbox and re.search(r"sandbox|namespace|zygote|No usable", str(exc), re.I):
                hint = (" The Chromium sandbox could not start in this container. Provide a seccomp profile that allows "
                        "user namespaces (see README) or, as a last resort, set BROWSER_SANDBOX=false.")
            raise BrowserStartError(f"Chromium failed to start: {_short(exc)}.{hint}") from exc
        log.info("Chromium %s started (sandbox=%s)", self._browser.version, s.browser_sandbox)

    def ensure(self) -> None:
        with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self.close()
                self._launch()

    def new_session(self, policy: NavPolicy, artifact_dir: Path, ocr: OcrEngine | None = None) -> "BrowserSession":
        self.ensure()
        s = self.settings
        ctx = self._browser.new_context(
            viewport={"width": s.viewport_width, "height": s.viewport_height},
            device_scale_factor=1, accept_downloads=False, service_workers="block", permissions=[],
            ignore_https_errors=False, java_script_enabled=True, locale="en-US",
        )
        return BrowserSession(ctx, policy, s, artifact_dir, ocr)

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._browser = self._pw = None


class BrowserSession:
    """One task's isolated browsing context plus the primitives the action handlers need."""

    def __init__(self, context, policy: NavPolicy, settings, artifact_dir: Path, ocr: OcrEngine | None):
        self.context = context
        self.policy = policy
        self.settings = settings
        self.artifact_dir = Path(artifact_dir)
        self.ocr = ocr
        self.violations: list[dict] = []
        self.fatal_violation: dict | None = None
        self.events: list[str] = []
        self.crashed = False
        self.last_status: int | None = None
        self.ocr_results: list[Any] = []
        self._screens = 0
        self._new_pages: list[Any] = []
        self._timeout = settings.browser_navigation_timeout_ms
        context.set_default_timeout(min(self._timeout, 15000))
        context.route("**/*", self._on_route)
        context.on("page", lambda p: self._attach(p, popup=True))
        self.page = context.new_page()
        self._attach(self.page, popup=False)

    # ------------------------------------------------------------ interception & events
    def _record_violation(self, v: PolicyViolation, url: str, fatal: bool = False) -> None:
        entry = {"code": v.code, "message": v.message, "url": url[:300]}
        self.violations.append(entry)
        if fatal and not self.fatal_violation:
            self.fatal_violation = entry
        log.warning("Policy violation (%s): %s [%s]", v.code, v.message, url[:120])

    def _on_route(self, route, request) -> None:
        url = request.url
        try:
            if urlsplit(url).scheme.lower() not in ("http", "https"):
                route.abort("blockedbyclient")
                return
            if request.is_navigation_request():
                self.policy.check_navigation(url)
            else:
                self.policy.check_subresource(url)
            route.continue_()
        except PolicyViolation as v:
            self._record_violation(v, url)
            self._safe_abort(route)
        except Exception:
            self._safe_abort(route)  # fail closed

    @staticmethod
    def _safe_abort(route) -> None:
        try:
            route.abort("blockedbyclient")
        except Exception:
            pass

    def _on_response(self, response) -> None:
        if self.policy.allow_private:
            return
        try:
            addr = response.server_addr()
        except Exception:
            return
        ip = (addr or {}).get("ipAddress", "")
        if ip and not is_public_ip(ip):
            self._record_violation(
                PolicyViolation("blocked_ip", f"Connected to non-public address {ip} (possible DNS rebinding)"),
                response.url, fatal=True)

    def _attach(self, page, popup: bool) -> None:
        page.set_default_timeout(min(self._timeout, 15000))
        page.on("response", self._on_response)
        page.on("crash", lambda *_: setattr(self, "crashed", True))
        page.on("dialog", self._on_dialog)
        if popup:
            self._new_pages.append(page)

    def _on_dialog(self, dialog) -> None:
        self.events.append(f"dialog {dialog.type} dismissed: {dialog.message[:120]}")
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _adopt_popups(self) -> None:
        """Switch to a newly opened tab if (and only if) the policy allows its URL."""
        while self._new_pages:
            p = self._new_pages.pop(0)
            try:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=3000)
                except PlaywrightError:
                    pass
                if len(self.context.pages) > MAX_PAGES:
                    p.close()
                    continue
                self.policy.check_navigation(p.url, resolve=False)
                self.page = p
                self.events.append(f"switched to new tab: {p.url[:120]}")
            except PolicyViolation:
                self.events.append("blocked a new tab that targeted a disallowed URL")
                try:
                    p.close()
                except Exception:
                    pass
            except PlaywrightError:
                pass

    # ------------------------------------------------------------ error translation
    @contextmanager
    def _translate(self, what: str):
        try:
            yield
        except PlaywrightTimeout as exc:
            raise ActionError(f"{what}_timeout", f"{what} timed out: {_short(exc)}") from exc
        except PlaywrightError as exc:
            msg = _short(exc)
            if re.search(r"has been closed|Target closed|crash", msg, re.I) or self.crashed:
                raise ActionError("browser_crashed", msg, recoverable=False) from exc
            raise ActionError(f"{what}_failed", msg) from exc

    # ------------------------------------------------------------ basic state
    @property
    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    def title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    def page_state(self) -> dict:
        try:
            return self.page.evaluate(JS_PAGE_STATE)
        except PlaywrightError:
            return {"url": self.current_url, "title": "", "text_hash": 0, "form_hash": 0, "y": 0, "max": 0}

    def text_visible(self, text: str) -> bool:
        try:
            loc = self.page.get_by_text(text)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return True
        except PlaywrightError:
            pass
        return False

    def settle(self, quick: bool = False) -> None:
        for state, tmo in (("domcontentloaded", 5000), ("networkidle", 0 if quick else 2500)):
            if tmo:
                try:
                    self.page.wait_for_load_state(state, timeout=tmo)
                except PlaywrightError:
                    pass
        try:
            self.page.wait_for_timeout(150)
        except PlaywrightError:
            pass
        self._adopt_popups()

    def check_health(self) -> None:
        """Called at every engine checkpoint."""
        if self.fatal_violation:
            raise PolicyViolation(self.fatal_violation["code"], self.fatal_violation["message"])
        if self.crashed:
            raise ActionError("browser_crashed", "The browser page crashed.", recoverable=False)

    # ------------------------------------------------------------ navigation
    def goto(self, url: str) -> None:
        url = self.policy.check_navigation(url)
        before = len(self.violations)
        with self._translate("navigation"):
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
            except PlaywrightTimeout:
                raise
            except PlaywrightError as exc:
                if "ERR_BLOCKED_BY_CLIENT" in str(exc) and len(self.violations) > before:
                    v = self.violations[-1]
                    raise PolicyViolation(v["code"], v["message"]) from exc
                raise
        self.last_status = resp.status if resp else None
        self.settle()
        self.check_health()
        self.policy.check_navigation(self.current_url, resolve=False)

    def go_back(self) -> bool:
        before = self.current_url
        with self._translate("go_back"):
            self.page.go_back(wait_until="domcontentloaded", timeout=self._timeout)
        self.settle()
        if self.current_url == before:
            return False
        self.policy.check_navigation(self.current_url, resolve=False)
        return True

    # ------------------------------------------------------------ target resolution
    def _info(self, locator) -> dict:
        try:
            return locator.evaluate(JS_ELEMENT_INFO) or {}
        except PlaywrightError:
            return {}

    def _from_locator(self, loc, desc: str) -> Resolved:
        return Resolved(description=desc, method="dom", locator=loc, info=self._info(loc))

    @staticmethod
    def _visible(loc, cap: int = 60) -> list:
        out = []
        for i in range(min(loc.count(), cap)):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    out.append(item)
            except PlaywrightError:
                continue
        return out

    def _strategies(self, t: Target) -> list[tuple[str, Any]]:
        p = self.page
        out: list[tuple[str, Any]] = []
        if t.label:
            out += [(f"label={t.label!r}", p.get_by_label(t.label, exact=True)), (f"label~{t.label!r}", p.get_by_label(t.label))]
        if t.placeholder:
            out += [(f"placeholder={t.placeholder!r}", p.get_by_placeholder(t.placeholder, exact=True)),
                    (f"placeholder~{t.placeholder!r}", p.get_by_placeholder(t.placeholder))]
        if t.text:
            role = (t.role or "").lower()
            roles = (role,) if role in ARIA_ROLES else TEXT_ROLES
            for exact in (True, False):
                combined = None
                for r in roles:
                    loc = p.get_by_role(r, name=t.text, exact=exact)
                    combined = loc if combined is None else combined.or_(loc)
                out.append((f"role/name {'=' if exact else '~'}{t.text!r}", combined))
            out += [(f"text={t.text!r}", p.get_by_text(t.text, exact=True)), (f"text~{t.text!r}", p.get_by_text(t.text))]
        return out

    def _disambiguate(self, candidates: list, near: str | None):
        """Pick the candidate living in the same DOM container as the `near` text (smallest shared ancestor).

        Structural, not geometric: in a dense list the neighbouring row's button can be almost as close in
        pixels, but it never shares a small container with the anchor. Ties are refused, never guessed.
        """
        if not near:
            return None
        try:
            anchors = self._visible(self.page.get_by_text(near, exact=True), 3) or self._visible(self.page.get_by_text(near), 3)
            if not anchors:
                return None
            handles = [c.element_handle() for c in candidates]
            idx = self.page.evaluate(JS_NEAREST_BY_CONTAINER, [anchors[0].element_handle(), handles])
            return candidates[idx] if isinstance(idx, int) and idx >= 0 else None
        except PlaywrightError:
            return None

    def resolve(self, target: Target, ocr_allowed: bool = False) -> Resolved:
        """DOM first (ref -> semantic locators), OCR only as a last resort, never guessing."""
        with self._translate("resolve"):
            if target.ref:
                loc = self.page.locator(f'[data-wp-ref="{target.ref}"]')
                if loc.count() == 1 and loc.first.is_visible():
                    return self._from_locator(loc.first, f"element {target.ref}")
                if not (target.text or target.label or target.placeholder):
                    raise ActionError("element_not_found",
                                      f"Element {target.ref} is gone or hidden (the page changed). Use a ref from the latest observation.")
            for name, loc in self._strategies(target):
                visible = self._visible(loc)
                if len(visible) == 1:
                    return self._from_locator(visible[0], f"{name}")
                if len(visible) > 1:
                    picked = self._disambiguate(visible, target.near)
                    if picked is not None:
                        return self._from_locator(picked, f"{name} near {target.near!r}")
                    raise ActionError("ambiguous_target",
                                      f"{len(visible)} visible elements match {name}. Use a specific ref from the observation or add 'near'.")
            if ocr_allowed and self.ocr is not None and (target.text or target.label or target.placeholder):
                return self._resolve_by_ocr(target)
        hint = "" if ocr_allowed else " (OCR fallback is disabled for this task)"
        raise ActionError("element_not_found", f"No visible element matches {target.describe()}.{hint}")

    def _ocr_screenshot(self) -> tuple[Path, bool]:
        path = self.screenshot(label="ocr")
        if path is not None:
            return path, False
        tmp = Path(tempfile.mkstemp(suffix=".png")[1])
        self.page.screenshot(path=str(tmp), scale="css", mask=[self.page.locator("input[type=password]")])
        return tmp, True

    def _resolve_by_ocr(self, target: Target) -> Resolved:
        path, temp = self._ocr_screenshot()
        try:
            result = self.ocr.run(path, screenshot_id=path.name)
        except OcrUnavailable as exc:
            raise ActionError("element_not_found", f"No DOM match, and OCR is unavailable: {exc}", recoverable=False) from exc
        finally:
            if temp:
                path.unlink(missing_ok=True)
        self.ocr_results.append(result)
        query = target.text or target.label or target.placeholder or ""
        minc = self.ocr.min_confidence
        try:
            match = pick_unique(find_text(result, query, minc), result, target.near, minc)
        except AmbiguousMatch as exc:
            if not exc.candidates:
                raise ActionError("element_not_found", f"No DOM match and no confident OCR match for {query!r}.") from exc
            raise ActionError("ambiguous_target", f"{exc} Add 'near' text or scroll so only one is visible.") from exc
        vp = self.page.viewport_size or {"width": self.settings.viewport_width, "height": self.settings.viewport_height}
        x, y = to_viewport(match.element.center, result, (vp["width"], vp["height"]))
        try:
            info = self.page.evaluate(JS_INFO_AT_POINT, [x, y]) or {}
        except PlaywrightError:
            info = {}
        return Resolved(f"OCR text {match.element.text!r} at ({round(x)},{round(y)})", "ocr", point=(x, y), info=info)

    # ------------------------------------------------------------ primitives
    def click(self, r: Resolved) -> None:
        with self._translate("click"):
            if r.locator is not None:
                r.locator.scroll_into_view_if_needed(timeout=3000)
                r.locator.click(timeout=5000)
            else:
                self.page.mouse.click(*r.point)
        self.settle(quick=True)

    def fill(self, r: Resolved, value: str) -> None:
        with self._translate("type"):
            if r.locator is not None:
                try:
                    r.locator.fill(value, timeout=5000)
                    return
                except PlaywrightTimeout:
                    raise
                except PlaywrightError:
                    r.locator.click(timeout=3000)  # not a fillable element: fall back to real typing
            else:
                self.page.mouse.click(*r.point)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            self.page.keyboard.type(value, delay=12)

    def read_value(self, r: Resolved) -> str | None:
        try:
            if r.locator is not None:
                return r.locator.evaluate(JS_READ_VALUE)
            return self.page.evaluate(JS_ACTIVE_VALUE)
        except PlaywrightError:
            return None

    def select(self, r: Resolved, value: str) -> None:
        if r.locator is None or r.info.get("tag") != "select":
            raise ActionError("not_a_select", "That element is not a <select>. For custom dropdowns, click it, then click the option text.")
        with self._translate("select"):
            for kw in ({"label": value}, {"value": value}):
                try:
                    r.locator.select_option(timeout=3000, **kw)
                    return
                except PlaywrightTimeout:
                    continue
                except PlaywrightError:
                    continue
            options = r.locator.evaluate(JS_OPTIONS)
            match = [o for o in options if o.lower() == value.strip().lower()] or \
                    [o for o in options if value.strip().lower() in o.lower()]
            if len(match) == 1:
                r.locator.select_option(label=match[0], timeout=3000)
                return
        raise ActionError("option_not_found", f"Option {value!r} not found. Available: {', '.join(options[:25])}")

    def read_selected(self, r: Resolved) -> dict | None:
        try:
            return r.locator.evaluate(JS_SELECTED) if r.locator is not None else None
        except PlaywrightError:
            return None

    def scroll_position(self, scope: Resolved | None = None) -> dict:
        try:
            if scope is not None and scope.locator is not None:
                return scope.locator.evaluate("(el) => ({y: Math.round(el.scrollTop), max: Math.max(0, el.scrollHeight - el.clientHeight)})")
            s = self.page_state()
            return {"y": s.get("y"), "max": s.get("max")}
        except PlaywrightError:
            return {"y": None, "max": None}

    def scroll(self, direction: str, amount: int | None, scope: Resolved | None = None) -> None:
        vh = (self.page.viewport_size or {"height": self.settings.viewport_height})["height"]
        dy = amount or int(vh * 0.8)
        with self._translate("scroll"):
            if scope is not None and scope.locator is not None:
                js = {"top": "(el) => el.scrollTo(0, 0)", "bottom": "(el) => el.scrollTo(0, el.scrollHeight)",
                      "up": f"(el) => el.scrollBy(0, -{dy})", "down": f"(el) => el.scrollBy(0, {dy})"}[direction]
                scope.locator.evaluate(js)
            elif direction in ("top", "bottom"):
                self.page.evaluate("(d) => window.scrollTo(0, d === 'top' ? 0 : (document.scrollingElement || document.documentElement).scrollHeight)", direction)
            else:
                vp = self.page.viewport_size or {"width": 800, "height": 600}
                self.page.mouse.move(vp["width"] / 2, vp["height"] / 2)
                self.page.mouse.wheel(0, -dy if direction == "up" else dy)
        self.page.wait_for_timeout(250)
        self.settle(quick=True)  # lets lazy-loaded / infinite-scroll content arrive

    def press(self, key: str) -> None:
        with self._translate("press_key"):
            self.page.keyboard.press(key)
        self.settle(quick=True)

    def wait(self, condition: str, value: str | None, seconds: float) -> None:
        with self._translate("wait"):
            if condition == "seconds":
                self.page.wait_for_timeout(int(min(max(seconds, 0), 10) * 1000))
            elif condition == "load":
                self.page.wait_for_load_state("load", timeout=10000)
            elif condition == "network_idle":
                self.page.wait_for_load_state("networkidle", timeout=10000)
            elif condition in ("text_visible", "text_hidden"):
                loc = self.page.get_by_text(value or "").first
                loc.wait_for(state="visible" if condition == "text_visible" else "hidden", timeout=int(max(seconds or 5, 1) * 1000))

    def extract_text(self, scope: Resolved | None, max_chars: int) -> str:
        with self._translate("extract"):
            if scope is not None and scope.locator is not None:
                text = scope.locator.inner_text(timeout=5000)
            elif scope is not None and scope.point is not None:
                text = self.page.evaluate("([x,y]) => { const e = document.elementFromPoint(x,y); return e ? e.innerText : ''; }", list(scope.point))
            else:
                text = self.page.locator("body").inner_text(timeout=8000)
        text = re.sub(r"[ \t]+", " ", text or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[... truncated, {len(text) - max_chars} more characters; scroll or target a smaller area]"
        return text

    def extract_links(self, scope: Resolved | None, limit: int) -> tuple[list[dict], int]:
        with self._translate("extract"):
            if scope is not None and scope.locator is not None:
                raw = scope.locator.evaluate(JS_LINKS)
            else:
                raw = self.page.locator("body").evaluate(JS_LINKS)
        links, excluded = [], 0
        for item in raw:
            if self.policy.link_permitted(item["href"]):
                links.append(item)
            else:
                excluded += 1
        return links[:limit], excluded + max(0, len(links) - limit)

    def screenshot(self, label: str = "step") -> Path | None:
        """Save a viewport screenshot (password fields masked). Returns None once the cap is reached."""
        if self._screens >= self.settings.max_screenshots_per_task:
            return None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._screens += 1
        path = self.artifact_dir / f"shot-{self._screens:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:20]}.png"
        with self._translate("screenshot"):
            self.page.screenshot(path=str(path), scale="css", full_page=False,
                                 mask=[self.page.locator("input[type=password]")], timeout=10000)
        return path

    def focused_info(self) -> dict:
        try:
            return self.page.evaluate(JS_FOCUSED_INFO) or {}
        except PlaywrightError:
            return {}

    def pop_ocr_results(self) -> list:
        out, self.ocr_results = self.ocr_results, []
        return out

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass
