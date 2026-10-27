"""Observation layer ("the eyes"): DOM + accessibility first, OCR as fallback.

Page content is UNTRUSTED. Observations are rendered into the prompt inside clearly delimited
blocks, and everything is length-capped so one call never ships the whole page.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .ocr import OcrEngine, OcrResult, OcrUnavailable

log = logging.getLogger(__name__)

JS_OBSERVE = r"""() => {
  const MAX_VIEW = 60, MAX_OFF = 25;
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const inView = (r) => r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  document.querySelectorAll('[data-wp-ref]').forEach(e => e.removeAttribute('data-wp-ref'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],' +
              '[role=menuitem],[role=checkbox],[role=radio],[role=option],[role=combobox],[role=switch],[contenteditable=""],[contenteditable="true"]';
  const implicitRole = (el) => { const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (t === 'a') return 'link'; if (t === 'button' || t === 'summary') return 'button'; if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') { if (['button','submit','reset','image'].includes(ty)) return 'button'; if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio'; if (ty === 'search') return 'searchbox'; return 'textbox'; }
    return el.isContentEditable ? 'textbox' : t; };
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label'); if (aria) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) return [...el.labels].map(l => l.innerText).join(' ').trim();
    const t = (el.innerText || '').trim(); if (t) return t;
    return el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || (['submit','button'].includes(el.type) ? el.value : '') || ''; };
  const view = [], off = []; let n = 0; let total = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue; total++;
    const r = el.getBoundingClientRect(); const bucket = inView(r) ? view : off;
    if ((bucket === view && view.length >= MAX_VIEW) || (bucket === off && off.length >= MAX_OFF)) continue;
    const ref = 'e' + (++n); el.setAttribute('data-wp-ref', ref);
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    const o = { ref, role: implicitRole(el), name: nameOf(el).replace(/\s+/g, ' ').trim().slice(0, 90) };
    if (tag === 'input' || tag === 'textarea' || tag === 'select') { o.tag = tag; if (type) o.type = type; }
    const ph = el.getAttribute('placeholder'); if (ph) o.placeholder = ph.slice(0, 60);
    if (tag === 'a') o.href = (el.href || '').slice(0, 140);
    if (tag === 'input' || tag === 'textarea') { if (type !== 'password' && el.value && !['checkbox','radio','submit','button'].includes(type)) o.value = String(el.value).slice(0, 60); }
    if (type === 'checkbox' || type === 'radio') o.checked = !!el.checked;
    if (tag === 'select') { o.options = [...el.options].slice(0, 30).map(x => x.text.trim().slice(0, 50)); o.selected = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim().slice(0, 50) : ''; }
    if (el.disabled) o.disabled = true;
    if (bucket === off) o.offscreen = true;
    bucket.push(o);
  }
  const blocks = new Map(); const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let node, chars = 0;
  while ((node = walker.nextNode()) && chars < 9000) {
    const t = node.nodeValue.replace(/\s+/g, ' ').trim(); if (!t) continue;
    const p = node.parentElement; if (!p || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(p.tagName)) continue;
    const r = p.getBoundingClientRect(); if (!inView(r) || !vis(p)) continue;
    const blk = p.closest('li,tr,p,h1,h2,h3,h4,h5,h6,article,section,dd,dt,label,td,th,div') || p;
    if (!blocks.has(blk)) blocks.set(blk, []); blocks.get(blk).push(t); chars += t.length;
  }
  const lines = []; const seen = new Set();
  for (const parts of blocks.values()) { const line = parts.join(' '); if (!seen.has(line)) { seen.add(line); lines.push(line.slice(0, 220)); } }
  const heads = [...document.querySelectorAll('h1,h2,h3')].filter(vis).slice(0, 10).map(h => h.innerText.trim().replace(/\s+/g, ' ').slice(0, 100));
  const se = document.scrollingElement || document.documentElement;
  const loading = !!document.querySelector('[aria-busy="true"],[role=progressbar],.loading,.spinner,.skeleton');
  return { url: location.href, title: document.title, elements: view.concat(off), total_interactive: total,
           headings: heads, visible_text: lines, loading,
           scroll: { y: Math.round(se.scrollTop), max: Math.max(0, se.scrollHeight - innerHeight), viewport_h: innerHeight } };
}"""


@dataclass
class Observation:
    url: str
    title: str
    elements: list[dict]
    total_interactive: int
    headings: list[str]
    visible_text: list[str]
    loading: bool
    scroll: dict
    http_status: int | None = None
    events: list[str] = field(default_factory=list)
    ocr: OcrResult | None = None
    ocr_note: str = ""
    screenshot: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def sparse(self) -> bool:
        """DOM gave us too little to act on: a hint that OCR may help (canvas apps, image-only UIs)."""
        return len(self.elements) < 3 and sum(len(t) for t in self.visible_text) < 80

    def to_prompt(self, max_chars: int = 7500) -> str:
        """Render to compact JSON, trimming the least valuable parts until it fits the budget."""
        elements = list(self.elements)
        text = list(self.visible_text)
        ocr = [e.to_dict() for e in (self.ocr.elements if self.ocr else [])][:150]
        for _ in range(12):
            data = self._payload(elements, text, ocr)
            out = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(out) <= max_chars:
                return out
            if ocr and len(ocr) > 30:
                ocr = ocr[: len(ocr) * 2 // 3]
            elif len(text) > 8:
                text = text[: len(text) * 2 // 3]
            elif len(elements) > 15:
                elements = elements[: len(elements) * 3 // 4]
            else:
                break
        return out[:max_chars]

    def _payload(self, elements, text, ocr) -> dict:
        d: dict[str, Any] = {
            "url": self.url, "title": self.title, "scroll": self.scroll,
            "interactive_elements": elements,
            "interactive_elements_total": self.total_interactive,
            "headings": self.headings,
            "visible_text": text,
        }
        if self.http_status and self.http_status >= 400:
            d["http_status"] = self.http_status
        if self.loading:
            d["page_is_loading"] = True
        if self.events:
            d["browser_events"] = self.events[-4:]
        if ocr:
            d["ocr_text"] = ocr
            d["ocr_note"] = "boxes are [x1,y1,x2,y2] in viewport pixels; OCR has no notion of element roles"
        elif self.ocr_note:
            d["ocr_note"] = self.ocr_note
        return d


class Observer:
    def __init__(self, ocr: OcrEngine | None):
        self.ocr = ocr

    def observe(self, session, *, use_ocr: bool = False, auto_sparse: bool = False) -> Observation:
        try:
            raw = session.page.evaluate(JS_OBSERVE)
        except PlaywrightError as exc:
            log.warning("DOM observation failed: %s", str(exc)[:200])
            raw = {"url": session.current_url, "title": "", "elements": [], "total_interactive": 0,
                   "headings": [], "visible_text": [], "loading": False, "scroll": {}}
        obs = Observation(
            url=raw["url"], title=raw["title"], elements=raw["elements"], total_interactive=raw["total_interactive"],
            headings=raw["headings"], visible_text=raw["visible_text"], loading=raw["loading"], scroll=raw["scroll"],
            http_status=session.last_status, events=list(session.events),
        )
        session.events.clear()
        if use_ocr or (auto_sparse and obs.sparse):
            self._attach_ocr(session, obs)
        return obs

    def _attach_ocr(self, session, obs: Observation) -> None:
        if self.ocr is None:
            obs.ocr_note = "OCR is not configured."
            return
        path = None
        try:
            path = session.screenshot(label="observe")
            if path is None:
                obs.ocr_note = "Screenshot limit reached; OCR skipped."
                return
            result = self.ocr.run(path, screenshot_id=path.name)
            result.elements = [e for e in result.elements if e.confidence >= self.ocr.min_confidence]
            obs.ocr, obs.screenshot = result, path.name
            session.ocr_results.append(result)
        except OcrUnavailable as exc:
            obs.ocr_note = f"OCR unavailable ({str(exc)[:120]}); continuing with DOM only."
            log.warning("OCR unavailable during observation: %s", exc)
        except Exception as exc:  # screenshots can fail on crashed pages; never kill the loop for OCR
            obs.ocr_note = f"OCR failed: {str(exc)[:120]}"
            log.warning("OCR observation failed: %s", exc)
