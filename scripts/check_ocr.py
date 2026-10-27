"""Run inside the worker container to verify OCR end-to-end:
    docker compose run --rm worker python scripts/check_ocr.py
Renders a test image, runs PaddleOCR on it and reports timing and peak memory."""
import resource
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, ".")
from app.agent.ocr import OcrEngine, OcrUnavailable, find_text  # noqa: E402


def main() -> int:
    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 42)
    except OSError:
        font = ImageFont.load_default()
    d.text((60, 60), "Search laptops", fill="black", font=font)
    d.text((60, 160), "Price $799", fill="black", font=font)
    path = tempfile.mktemp(suffix=".png")
    img.save(path)
    engine = OcrEngine()
    t0 = time.time()
    try:
        result = engine.run(path)
    except OcrUnavailable as exc:
        print(f"FAIL: OCR unavailable: {exc}")
        return 1
    print(f"OK: {len(result.elements)} regions in {time.time() - t0:.1f}s (first call includes model load)")
    for el in result.elements:
        print(f"  {el.text!r} box={[round(v) for v in el.box]} conf={el.confidence:.2f}")
    print(f"peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    ok = bool(find_text(result, "Search laptops")) and bool(find_text(result, "799"))
    print("text found:" , ok)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
