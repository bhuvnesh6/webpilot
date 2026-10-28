"""WebPilot AI application factory."""
from __future__ import annotations

import logging
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

from . import extensions
from .config import Settings, load_settings
from .utils import as_aware, utcnow

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


def create_app(settings: Settings | None = None, db=None) -> Flask:
    settings = settings or load_settings()          # raises ConfigError: the app refuses to start misconfigured
    extensions.configure(settings, db=db)
    extensions.configure_logging(settings.log_level)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="webpilot_session",
        PERMANENT_SESSION_LIFETIME=12 * 3600,
        MAX_CONTENT_LENGTH=256 * 1024,
        WTF_CSRF_TIME_LIMIT=None,
        RATELIMIT_STORAGE_URI=settings.redis_url if settings.is_production else "memory://",
        RATELIMIT_IN_MEMORY_FALLBACK_ENABLED=True,
        RATELIMIT_KEY_PREFIX="webpilot-rl",
        JSON_SORT_KEYS=False,
        ADMIN_PW_HASH=generate_password_hash(settings.admin_password),   # compared with a hash, never in plaintext
    )
    if settings.trusted_proxies:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=settings.trusted_proxies, x_proto=settings.trusted_proxies,
                                x_host=settings.trusted_proxies)
    extensions.csrf.init_app(app)
    extensions.limiter.init_app(app)

    from .auth.routes import bp as auth_bp
    from .dashboard.routes import bp as dash_bp
    from .tasks.routes import bp as tasks_bp
    from .websites.routes import bp as sites_bp
    for bp in (auth_bp, dash_bp, sites_bp, tasks_bp):
        app.register_blueprint(bp)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production and settings.secure_cookies:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @app.template_filter("dt")
    def fmt_dt(v, fmt="%Y-%m-%d %H:%M:%S"):
        v = as_aware(v) if isinstance(v, datetime) else None
        return v.strftime(fmt) + " UTC" if v else "-"

    @app.template_filter("duration")
    def fmt_duration(seconds):
        if seconds is None:
            return "-"
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

    @app.template_filter("elapsed")
    def fmt_elapsed(start):
        start = as_aware(start)
        return fmt_duration((utcnow() - start).total_seconds()) if start else "-"

    @app.context_processor
    def inject():
        return {"app_name": "WebPilot AI", "now": utcnow()}

    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        if wants_json():
            return jsonify(error="CSRF validation failed. Reload the page and try again."), 400
        return render_template("error.html", code=400, message="Your form expired or was invalid. Reload the page and try again."), 400

    for code, msg in ((403, "You do not have access to that."), (404, "That page does not exist."),
                      (413, "That request was too large."), (429, "Too many attempts. Wait a minute and try again.")):
        def handler(e, code=code, msg=msg):
            if wants_json():
                return jsonify(error=msg), code
            if code == 429 and request.endpoint == "auth.login":
                return render_template("login.html", error=msg), 429
            return render_template("error.html", code=code, message=msg), code
        app.register_error_handler(code, handler)

    @app.errorhandler(500)
    def server_error(e):
        log.exception("Unhandled error")
        if wants_json():
            return jsonify(error="Internal server error"), 500
        return render_template("error.html", code=500, message="Something went wrong on our side. Check the web logs."), 500

    if settings.is_production and not settings.secure_cookies:
        log.warning("SECURE_COOKIES is off: the dashboard is being served over plain HTTP, so the admin password and session "
                    "cookie travel unencrypted. Restrict port access (firewall / SSH tunnel) or put HTTPS in front of it.")
    try:
        extensions.ensure_indexes()
    except Exception as exc:  # DB may still be starting; /healthz will report it
        log.error("Could not create indexes at startup: %s", exc)
    return app
