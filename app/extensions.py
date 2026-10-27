"""Shared singletons: settings, Mongo handle, CSRF, rate limiter."""
from __future__ import annotations

import logging
import os
import threading

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from pymongo import ASCENDING, DESCENDING, MongoClient

from .config import Settings
from .utils import redact

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])

_settings: Settings | None = None
_client: MongoClient | None = None
_client_pid: int | None = None
_db_override = None
_lock = threading.Lock()


def configure(settings: Settings, db=None) -> None:
    """Install settings (and optionally a pre-built DB handle, used by tests)."""
    global _settings, _db_override, _client, _client_pid
    _settings = settings
    _db_override = db
    if db is None:
        _client, _client_pid = None, None


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not configured; call extensions.configure() first.")
    return _settings


def get_db():
    """Return the Mongo database. The client is re-created after fork (gunicorn / celery prefork)."""
    global _client, _client_pid
    if _db_override is not None:
        return _db_override
    s = get_settings()
    with _lock:
        if _client is None or _client_pid != os.getpid():
            _client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, appname="webpilot")
            _client_pid = os.getpid()
        return _client[s.mongo_database]


def close_db() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None


def ensure_indexes(db=None) -> None:
    db = db if db is not None else get_db()
    db.websites.create_index([("is_active", ASCENDING), ("name", ASCENDING)])
    db.website_revisions.create_index([("website_id", ASCENDING), ("version", DESCENDING)])
    db.tasks.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    db.tasks.create_index([("website_id", ASCENDING), ("created_at", DESCENDING)])
    db.task_actions.create_index([("task_id", ASCENDING), ("seq", ASCENDING)])
    db.task_results.create_index([("task_id", ASCENDING)])
    db.task_results.create_index([("extracted_at", DESCENDING)])
    db.task_logs.create_index([("task_id", ASCENDING), ("timestamp", ASCENDING)])
    db.task_logs.create_index([("severity", ASCENDING), ("timestamp", DESCENDING)])
    db.task_templates.create_index([("name", ASCENDING)])


class SecretScrubFilter(logging.Filter):
    """Last line of defence: scrub known secrets from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            secrets = get_settings().secrets
            msg = record.getMessage()
            scrubbed = redact(msg, secrets)
            if scrubbed != msg:
                record.msg, record.args = scrubbed, ()
        except Exception:  # never let logging break the app
            pass
        return True


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    for h in root.handlers:
        if not any(isinstance(f, SecretScrubFilter) for f in h.filters):
            h.addFilter(SecretScrubFilter())
    logging.getLogger("werkzeug").setLevel("WARNING")
    logging.getLogger("httpx").setLevel("WARNING")


def is_configured() -> bool:
    return _settings is not None
