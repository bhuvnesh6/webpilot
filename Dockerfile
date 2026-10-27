# syntax=docker/dockerfile:1
# One image for both the web app and the Celery worker (same code, same dependencies).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/webpilot

# Runtime libraries for OpenCV / Paddle (Chromium's own libraries are installed by `playwright install --with-deps`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 webpilot \
    && useradd --system --uid 10001 --gid webpilot --create-home --home-dir /home/webpilot webpilot

WORKDIR /app

# Dependencies first so code changes don't invalidate the heavy layers.
COPY requirements.txt requirements-core.txt ./
RUN pip install -r requirements.txt

# Chromium + its system packages (version is tied to the pinned playwright package).
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN mkdir -p /app/artifacts /app/instance \
    && chown -R webpilot:webpilot /app/artifacts /app/instance /home/webpilot

USER webpilot

# Pre-download OCR models at build time so nothing is installed or fetched when the container starts.
# Non-fatal on purpose: if it fails the build says so loudly, and the worker degrades to DOM-only operation.
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=False, lang='en', show_log=False)" \
    || echo "WARNING: OCR model pre-download failed. Run 'docker compose run --rm worker python scripts/check_ocr.py' to diagnose."

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=8s --start-period=40s --retries=3 CMD ["python", "scripts/healthcheck.py", "web"]

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${APP_PORT:-8050} --workers 2 --threads 4 --timeout 60 --access-logfile - wsgi:app"]
