"""Application-level task events (task_logs)."""
from __future__ import annotations

import logging

from ..extensions import get_db, get_settings
from ..utils import redact, utcnow

log = logging.getLogger(__name__)


def add(task_id: str, severity: str, event: str, message: str, details: dict | None = None) -> None:
    secrets = get_settings().secrets
    doc = {"task_id": task_id, "timestamp": utcnow(), "severity": severity, "event": event,
           "message": redact(message, secrets)[:1000]}
    if details:
        doc["details"] = _scrub(details, secrets)
    try:
        get_db().task_logs.insert_one(doc)
    except Exception:  # logging must never take a task down
        log.exception("Could not write task log")


def _scrub(obj, secrets):
    if isinstance(obj, str):
        return redact(obj, secrets)[:4000]
    if isinstance(obj, dict):
        return {k: _scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, secrets) for v in obj[:100]]
    return obj


def for_task(task_id: str, after=None, limit: int = 300, events: list[str] | None = None) -> list[dict]:
    q: dict = {"task_id": task_id}
    if after:
        q["timestamp"] = {"$gt": after}
    if events:
        q["event"] = {"$in": events}
    return list(get_db().task_logs.find(q).sort("timestamp", 1).limit(limit))


def latest_errors(limit: int = 8) -> list[dict]:
    return list(get_db().task_logs.find({"severity": "error"}).sort("timestamp", -1).limit(limit))


def recent_activity(limit: int = 10) -> list[dict]:
    return list(get_db().task_logs.find({"severity": {"$in": ["info", "warning", "error"]}}).sort("timestamp", -1).limit(limit))
