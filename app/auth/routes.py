"""Single-admin authentication (credentials come from environment variables)."""
from __future__ import annotations

import hashlib
import hmac
import logging
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from ..extensions import get_settings, limiter

bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)


def credential_fingerprint() -> str:
    """Changes whenever ADMIN_USERNAME/ADMIN_PASSWORD change, which invalidates every existing session (rotation)."""
    s = get_settings()
    return hmac.new(s.secret_key.encode(), f"{s.admin_username}\0{s.admin_password}".encode(), hashlib.sha256).hexdigest()[:32]


def is_authenticated() -> bool:
    got = session.get("auth")
    return bool(got) and hmac.compare_digest(str(got), credential_fingerprint())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def _safe_next(target: str | None) -> str:
    if target:
        parts = urlsplit(target)
        if not parts.scheme and not parts.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return url_for("dashboard.overview")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: get_settings().login_rate_limit, methods=["POST"])
def login():
    if request.method == "GET":
        if is_authenticated():
            return redirect(url_for("dashboard.overview"))
        return render_template("login.html", error=None)
    s = get_settings()
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    # Evaluate both checks every time so response timing doesn't reveal which one failed.
    user_ok = hmac.compare_digest(username.encode(), s.admin_username.encode())
    pass_ok = check_password_hash(current_app.config["ADMIN_PW_HASH"], password)
    if user_ok and pass_ok:
        session.clear()                       # new session on login (prevents fixation)
        session["auth"] = credential_fingerprint()
        session.permanent = True
        log.info("Admin login from %s", request.remote_addr)
        return redirect(_safe_next(request.args.get("next")))
    log.warning("Failed login attempt from %s", request.remote_addr)
    return render_template("login.html", error="That username or password is not correct."), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
