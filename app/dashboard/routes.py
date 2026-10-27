"""Overview, settings and health endpoints."""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template

from ..auth.routes import login_required
from ..extensions import get_db, get_settings
from ..services import log_service, task_service, websites

bp = Blueprint("dashboard", __name__)
log = logging.getLogger(__name__)


@bp.route("/")
@login_required
def overview():
    task_service.reap_stale()
    counts = task_service.counts()
    recent, _ = task_service.list_tasks(page=1, per_page=8)
    return render_template(
        "dashboard.html", counts=counts, website_count=len(websites.list_all()), recent=recent,
        errors=log_service.latest_errors(6), activity=log_service.recent_activity(10),
        avg_duration=task_service.average_duration())


@bp.route("/settings")
@login_required
def settings_page():
    s = get_settings()
    checks = _health()
    import importlib.util
    # find_spec only checks the package is installed; importing paddleocr would load the whole Paddle runtime
    # (hundreds of MB) into every web worker.
    ocr = {"available": importlib.util.find_spec("paddleocr") is not None, "error": None}
    return render_template("settings.html", summary=s.public_summary(), checks=checks, ocr=ocr, admin_username=s.admin_username)


def _health() -> dict:
    out = {}
    try:
        get_db().command("ping")
        out["mongo"] = "ok"
    except Exception:
        out["mongo"] = "unavailable"
    try:
        import redis
        s = get_settings()
        redis.Redis(host=s.redis_host, port=s.redis_port, db=s.redis_db, password=s.redis_password or None,
                    socket_connect_timeout=2, socket_timeout=2).ping()
        out["redis"] = "ok"
    except Exception:
        out["redis"] = "unavailable"
    return out


@bp.route("/healthz")
def healthz():
    """Unauthenticated, minimal. Only MongoDB failing makes the web container unhealthy (Redis outages degrade submission only)."""
    h = _health()
    ok = h["mongo"] == "ok"
    return jsonify(status="ok" if ok and h["redis"] == "ok" else ("degraded" if ok else "unhealthy"), **h), (200 if ok else 503)
