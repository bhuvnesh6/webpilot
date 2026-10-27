import numpy as np
import pytest
from PIL import Image

from app.agent.ocr import (AmbiguousMatch, OcrElement, OcrEngine, OcrResult, OcrUnavailable, find_text,
                           normalize_paddle_output, pick_unique, to_viewport)


def result(*items, w=1000, h=600):
    return OcrResult(w, h, [OcrElement(t, b, c) for t, b, c in items])


def test_normalizes_paddle_2x_shape():
    raw = [[[[[10, 20], [110, 20], [110, 50], [10, 50]], ("Search", 0.98)], [[[0, 0], [5, 0], [5, 5], [0, 5]], ("x", 0.5)]]]
    out = normalize_paddle_output(raw)
    assert out[0] == ((10.0, 20.0, 110.0, 50.0), "Search", 0.98) and len(out) == 2
    assert normalize_paddle_output([None]) == [] and normalize_paddle_output(None) == []


def test_normalizes_paddle_3x_shape():
    raw = [{"res": {"rec_texts": ["Hello"], "rec_scores": [0.9], "rec_polys": [np.array([[1, 2], [11, 2], [11, 9], [1, 9]])]}}]
    assert normalize_paddle_output(raw)[0] == ((1.0, 2.0, 11.0, 9.0), "Hello", 0.9)
    raw2 = [{"rec_texts": ["A"], "rec_scores": [0.8], "dt_polys": [[[0, 0], [4, 0], [4, 4], [0, 4]]]}]
    assert normalize_paddle_output(raw2)[0][1] == "A"


def make_image(path, size=(1200, 800)):
    Image.new("RGB", size, "white").save(path)


def test_boxes_are_mapped_back_after_downscale(tmp_path):
    p = tmp_path / "s.png"
    make_image(p, (1200, 800))
    seen = {}

    def backend(arr):
        seen["shape"] = arr.shape
        return [((100, 50, 200, 90), "Search", 0.99)]        # coordinates in the DOWNSCALED image

    r = OcrEngine(max_dimension=600, backend=backend).run(p)  # 0.5x
    assert seen["shape"][:2] == (400, 600) and r.scale == 0.5
    assert (r.screenshot_width, r.screenshot_height) == (1200, 800)
    assert r.elements[0].box == (200.0, 100.0, 400.0, 180.0)   # reversed to original pixels
    assert r.elements[0].center == (300.0, 140.0)


def test_no_resize_when_small_enough(tmp_path):
    p = tmp_path / "s.png"
    make_image(p, (800, 600))
    r = OcrEngine(max_dimension=1600, backend=lambda a: [((1, 2, 3, 4), "x", 0.9)]).run(p)
    assert r.scale == 1.0 and r.elements[0].box == (1.0, 2.0, 3.0, 4.0)


def test_empty_and_blank_text_results(tmp_path):
    p = tmp_path / "s.png"
    make_image(p)
    r = OcrEngine(backend=lambda a: [((0, 0, 1, 1), "   ", 0.9)]).run(p)
    assert r.elements == []
    assert find_text(r, "Search") == []


def test_to_viewport_scales_when_screenshot_differs_from_viewport():
    r = result(("x", (0, 0, 1, 1), 1.0), w=2732, h=1536)         # e.g. a 2x device-scale screenshot
    assert to_viewport((1366, 768), r, (1366, 768)) == (683.0, 384.0)
    r2 = result(("x", (0, 0, 1, 1), 1.0), w=1366, h=768)
    assert to_viewport((100, 50), r2, (1366, 768)) == (100.0, 50.0)


def test_matching_tiers_and_confidence_filter():
    r = result(("Search", (0, 0, 10, 10), 0.99), ("Search results", (0, 20, 10, 30), 0.95), ("Serach", (0, 40, 10, 50), 0.95),
               ("Search", (0, 60, 10, 70), 0.30))
    m = find_text(r, "search", 0.6)
    assert [x.element.text for x in m][:2] == ["Search", "Search results"] and m[0].score == 1.0
    assert all(x.element.confidence >= 0.6 for x in m)
    assert find_text(r, "nothing like it") == []


def test_ambiguous_matches_are_refused_not_guessed():
    r = result(("Buy", (0, 0, 10, 10), 0.99), ("Buy", (0, 100, 10, 110), 0.97))
    with pytest.raises(AmbiguousMatch) as ei:
        pick_unique(find_text(r, "Buy"), r)
    assert len(ei.value.candidates) == 2


def test_near_text_disambiguates_only_when_clearly_closer():
    r = result(("Buy", (0, 0, 10, 10), 0.99), ("Buy", (0, 500, 10, 510), 0.99), ("Camera", (20, 0, 80, 12), 0.99))
    assert pick_unique(find_text(r, "Buy"), r, near="Camera").element.box[1] == 0
    r2 = result(("Buy", (0, 0, 10, 10), 0.99), ("Buy", (0, 20, 10, 30), 0.99), ("Camera", (20, 10, 80, 20), 0.99))
    with pytest.raises(AmbiguousMatch):
        pick_unique(find_text(r2, "Buy"), r2, near="Camera")       # equidistant: still refuses


def test_unavailable_backend_reports_clearly(tmp_path):
    p = tmp_path / "s.png"
    make_image(p)
    eng = OcrEngine()           # real PaddleOCR is not installed here: must degrade, not crash
    if eng.available:
        pytest.skip("PaddleOCR is installed in this environment")
    with pytest.raises(OcrUnavailable):
        eng.run(p)
    assert eng.init_error
