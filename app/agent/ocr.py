"""Local OCR (PaddleOCR) with normalized output, text matching and coordinate mapping.

Everything here runs inside the worker container; screenshots never leave the VPS.
The heavy PaddleOCR import is lazy so the web container and the unit tests stay light.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)


class OcrUnavailable(RuntimeError):
    """OCR could not be initialised or run (missing deps, no memory, model files missing...)."""


class AmbiguousMatch(Exception):
    def __init__(self, message: str, candidates: list["OcrMatch"]):
        super().__init__(message)
        self.candidates = candidates


@dataclass
class OcrElement:
    text: str
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in ORIGINAL screenshot pixels
    confidence: float

    @property
    def center(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)

    def to_dict(self) -> dict:
        return {"text": self.text, "box": [round(v) for v in self.box], "confidence": round(self.confidence, 3)}


@dataclass
class OcrResult:
    screenshot_width: int
    screenshot_height: int
    elements: list[OcrElement]
    screenshot_id: str = ""
    timestamp: float = field(default_factory=time.time)
    duration_ms: int = 0
    scale: float = 1.0  # scale applied before OCR (1.0 = none)

    def to_dict(self) -> dict:
        return {
            "screenshot_id": self.screenshot_id,
            "screenshot_width": self.screenshot_width,
            "screenshot_height": self.screenshot_height,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "scale": self.scale,
            "elements": [e.to_dict() for e in self.elements],
        }


@dataclass
class OcrMatch:
    element: OcrElement
    score: float  # 0..1 text similarity tier

    @property
    def sort_key(self):
        return (-self.score, -self.element.confidence)


# Backend: image path/array -> [(box_xyxy, text, confidence)] in the coordinates of the image it was given.
Backend = Callable[[object], list[tuple[tuple[float, float, float, float], str, float]]]


# ------------------------------------------------------------------ PaddleOCR backend

def _poly_to_box(poly) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def normalize_paddle_output(raw) -> list[tuple[tuple[float, float, float, float], str, float]]:
    """Normalize PaddleOCR 2.x and 3.x result shapes to [(box, text, conf)]."""
    out: list = []
    if raw is None:
        return out
    # 3.x: list of dict-like results with rec_texts / rec_scores / rec_polys (or dt_polys)
    first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    if hasattr(first, "get") or isinstance(first, dict):
        for res in raw:
            data = res.get("res", res) if hasattr(res, "get") else res
            texts = data.get("rec_texts") or []
            scores = data.get("rec_scores") or []
            polys = data.get("rec_polys")
            if polys is None:
                polys = data.get("dt_polys")
            if polys is None:
                polys = []
            for text, score, poly in zip(texts, scores, polys):
                out.append((_poly_to_box(poly), str(text), float(score)))
        return out
    # 2.x: [[ [poly, (text, conf)], ... ]]  (one list per image; entries may be None)
    for page in raw:
        for line in page or []:
            poly, (text, conf) = line[0], line[1]
            out.append((_poly_to_box(poly), str(text), float(conf)))
    return out


def _paddle_backend_factory(lang: str) -> Backend:
    try:
        import numpy as np  # noqa: F401
        from importlib.metadata import version
        from paddleocr import PaddleOCR
    except Exception as exc:  # ImportError, but also binary incompatibilities
        raise OcrUnavailable(f"PaddleOCR is not importable: {exc}") from exc
    try:
        major = int(version("paddleocr").split(".")[0])
    except Exception:
        major = 2
    try:
        if major >= 3:
            engine = PaddleOCR(lang=lang, use_doc_orientation_classify=False, use_doc_unwarping=False,
                               use_textline_orientation=False)
        else:
            engine = PaddleOCR(use_angle_cls=False, lang=lang, show_log=False)
    except Exception as exc:
        raise OcrUnavailable(f"PaddleOCR failed to initialise (models missing or out of memory?): {exc}") from exc

    def run(image) -> list:
        try:
            if major >= 3:
                return normalize_paddle_output(engine.predict(image))
            return normalize_paddle_output(engine.ocr(image, cls=False))
        except Exception as exc:
            raise OcrUnavailable(f"PaddleOCR inference failed: {exc}") from exc

    return run


# ------------------------------------------------------------------ engine

class OcrEngine:
    """Thread-safe, lazily-initialised OCR wrapper (one model load per worker process)."""

    def __init__(self, lang: str = "en", max_dimension: int = 1600, min_confidence: float = 0.6,
                 backend: Backend | None = None):
        self.lang = lang
        self.max_dimension = max_dimension
        self.min_confidence = min_confidence
        self._backend = backend
        self._lock = threading.Lock()
        self._init_error: str | None = None

    @property
    def available(self) -> bool:
        try:
            self._ensure_backend()
            return True
        except OcrUnavailable:
            return False

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def _ensure_backend(self) -> Backend:
        with self._lock:
            if self._backend is None:
                if self._init_error:
                    raise OcrUnavailable(self._init_error)
                try:
                    self._backend = _paddle_backend_factory(self.lang)
                except OcrUnavailable as exc:
                    self._init_error = str(exc)
                    log.error("OCR initialisation failed: %s", exc)
                    raise
            return self._backend

    def run(self, image_path: str | Path, screenshot_id: str = "") -> OcrResult:
        """OCR a screenshot. Boxes are returned in the ORIGINAL screenshot's pixel space."""
        backend = self._ensure_backend()
        from PIL import Image
        t0 = time.monotonic()
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            width, height = im.size
            scale = min(1.0, self.max_dimension / max(width, height))
            if scale < 1.0:
                im = im.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
            import numpy as np
            arr = np.asarray(im)[:, :, ::-1].copy()  # RGB -> BGR (what PaddleOCR expects)
            raw = backend(arr)
        # Reverse the resize so callers can use the boxes directly against the screenshot / viewport.
        elements = [
            OcrElement(text=t.strip(), box=(b[0] / scale, b[1] / scale, b[2] / scale, b[3] / scale), confidence=c)
            for b, t, c in raw if t and t.strip()
        ]
        del arr
        return OcrResult(width, height, elements, screenshot_id=screenshot_id,
                         duration_ms=int((time.monotonic() - t0) * 1000), scale=scale)


# ------------------------------------------------------------------ matching

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$€£%.@#/-]", " ", text.lower())).strip()


def find_text(result: OcrResult, query: str, min_confidence: float = 0.6) -> list[OcrMatch]:
    """Find OCR elements matching `query`. Exact (1.0) > whole-word containment (0.85) > fuzzy (0.7)."""
    q = _norm(query)
    if not q:
        return []
    matches: list[OcrMatch] = []
    for el in result.elements:
        if el.confidence < min_confidence:
            continue
        t = _norm(el.text)
        if not t:
            continue
        if t == q:
            score = 1.0
        elif re.search(rf"(?<!\w){re.escape(q)}(?!\w)", t):
            score = 0.85
        elif len(q) >= 4 and difflib.SequenceMatcher(None, t, q).ratio() >= 0.88:
            score = 0.7
        else:
            continue
        matches.append(OcrMatch(el, score))
    matches.sort(key=lambda m: m.sort_key)
    return matches


def pick_unique(matches: Sequence[OcrMatch], result: OcrResult | None = None, near: str | None = None,
                min_confidence: float = 0.6) -> OcrMatch:
    """Choose one match or raise AmbiguousMatch. Never guesses between equally good candidates."""
    if not matches:
        raise AmbiguousMatch("No OCR text matched the target.", [])
    top_score = matches[0].score
    top = [m for m in matches if m.score == top_score]
    if len(top) == 1:
        return top[0]
    if near and result is not None:
        anchors = find_text(result, near, min_confidence)
        if anchors:
            ax, ay = anchors[0].element.center

            def dist(m: OcrMatch) -> float:
                cx, cy = m.element.center
                return ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5

            ranked = sorted(top, key=dist)
            if len(ranked) >= 2 and dist(ranked[0]) < 0.5 * dist(ranked[1]):
                return ranked[0]
    raise AmbiguousMatch(f"{len(top)} OCR regions match equally well.", top)


def to_viewport(point: tuple[float, float], result: OcrResult, viewport: tuple[int, int]) -> tuple[float, float]:
    """Map a point in screenshot pixels to viewport CSS pixels (identity when sizes agree)."""
    sx = viewport[0] / result.screenshot_width if result.screenshot_width else 1.0
    sy = viewport[1] / result.screenshot_height if result.screenshot_height else 1.0
    return (point[0] * sx, point[1] * sy)
