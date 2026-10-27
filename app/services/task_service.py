"""Task lifecycle: creation, atomic claiming, progress, cancellation, admin prompts, recovery."""
from __future__ import annotations

import uuid
from datetime import timedelta

from pymongo import ReturnDocument

from ..extensions import get_db, get_settings
from ..utils import as_aware, redact, utcnow
from . import log_service, websites

STATUSES = ("queued", "starting", "running", "waiting_for_llm", "waiting_for_admin", "retrying",
            "completed", "failed", "cancel_requested", "cancelled")
ACTIVE = ("starting", "running", "waiting_for_llm", "waiting_for_admin", "retrying", "cancel_requested")
RUNNING_VIEW = ("queued",) + ACTIVE
TERMINAL = ("completed", "failed", "cancelled")
LOCKED_FOR_STATUS_UPDATE = TERMINAL + ("cancel_requested",)
RETRYABLE = ("failed", "cancelled")


class TaskError(ValueError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def create(site: dict, name: str, instructions: str, params: dict, max_actions: int, max_duration: int,
           retry_of: str | None = None, retry_count: int = 0, snapshot: dict | None = None) -> dict:
    s = get_settings()
    if not site.get("is_active", True):
        raise TaskError("This website configuration is disabled. Enable it before starting tasks.")
    name, instructions = (name or "").strip(), (instructions or "").strip()
    if not 2 <= len(name) <= 120:
        raise TaskError("Task name must be 2-120 characters.")
    if len(instructions) < 5:
        raise TaskError("Describe what the agent should do.")
    if len(instructions) > 8000:
        raise TaskError("Task instructions are limited to 8,000 characters.")
    if not 1 <= max_actions <= s.max_actions_per_task:
        raise TaskError(f"Maximum actions must be between 1 and {s.max_actions_per_task}.")
    if not 30 <= max_duration <= s.max_task_duration_seconds:
        raise TaskError(f"Maximum duration must be between 30 and {s.max_task_duration_seconds} seconds.")
    snap = snapshot or websites.snapshot(site)
    merged = {**snap["default_params"], **params}
    now = utcnow()
    doc = {
        "_id": new_id(), "website_id": snap["website_id"], "website_name": snap["name"], "name": name,
        "instructions": instructions, "params": merged, "website_snapshot": snap,
        "status": "queued", "current_action": None, "progress_message": "Waiting in the queue", "current_url": None,
        "max_actions": max_actions, "max_duration_seconds": max_duration,
        "retry_count": retry_count, "retry_of": retry_of, "cancel_requested": False,
        "pending_prompt": None, "admin_response": None, "throttled": False,
        "actions_executed": 0, "consequential_actions": 0, "token_usage": {"prompt": 0, "completion": 0, "total": 0, "calls": 0},
        "error": None, "summary": None, "result_count": 0,
        "created_at": now, "updated_at": now, "heartbeat_at": now, "started_at": None, "completed_at": None,
    }
    get_db().tasks.insert_one(doc)
    log_service.add(doc["_id"], "info", "task_created", f"Task queued for {snap['name']}")
    return doc


def get(task_id: str) -> dict | None:
    return get_db().tasks.find_one({"_id": task_id})


def mark_submit_failed(task_id: str, message: str) -> None:
    finish(task_id, "failed", error={"code": "queue_unavailable", "message": message})


def claim(task_id: str, worker: str = "") -> dict | None:
    """Atomically move queued -> starting. Returns None if the task is missing or no longer queued."""
    now = utcnow()
    return get_db().tasks.find_one_and_update(
        {"_id": task_id, "status": "queued", "cancel_requested": False},
        {"$set": {"status": "starting", "started_at": now, "updated_at": now, "heartbeat_at": now,
                  "worker": worker, "progress_message": "Starting browser"}},
        return_document=ReturnDocument.AFTER)


def update_progress(task_id: str, status: str | None = None, current_action: str | None = None,
                    message: str | None = None, url: str | None = None, **extra) -> None:
    """Worker progress. Never overwrites cancel_requested or a terminal state."""
    db = get_db()
    now = utcnow()
    fields = {"updated_at": now, "heartbeat_at": now}
    if current_action is not None:
        fields["current_action"] = current_action
    if message is not None:
        fields["progress_message"] = redact(message, get_settings().secrets)[:300]
    if url is not None:
        fields["current_url"] = url[:500]
    if "throttled" in extra:
        fields["throttled"] = bool(extra["throttled"])
    db.tasks.update_one({"_id": task_id, "status": {"$nin": list(TERMINAL)}}, {"$set": fields})
    if status:
        db.tasks.update_one({"_id": task_id, "status": {"$nin": list(LOCKED_FOR_STATUS_UPDATE)}}, {"$set": {"status": status}})


def heartbeat(task_id: str) -> None:
    now = utcnow()
    get_db().tasks.update_one({"_id": task_id}, {"$set": {"heartbeat_at": now}})


def add_tokens(task_id: str, usage: dict) -> None:
    if not usage:
        return
    get_db().tasks.update_one({"_id": task_id}, {"$inc": {
        "token_usage.prompt": usage.get("prompt_tokens", 0), "token_usage.completion": usage.get("completion_tokens", 0),
        "token_usage.total": usage.get("total_tokens", 0), "token_usage.calls": 1}})


def is_cancel_requested(task_id: str) -> bool:
    doc = get_db().tasks.find_one({"_id": task_id}, {"cancel_requested": 1, "status": 1})
    return bool(doc and (doc.get("cancel_requested") or doc.get("status") in ("cancel_requested", "cancelled")))


def request_cancel(task_id: str) -> str:
    """Queued tasks are cancelled immediately; active ones stop at the worker's next safe checkpoint."""
    db = get_db()
    now = utcnow()
    done = db.tasks.find_one_and_update(
        {"_id": task_id, "status": "queued"},
        {"$set": {"status": "cancelled", "cancel_requested": True, "completed_at": now, "updated_at": now,
                  "progress_message": "Cancelled before it started"}})
    if done:
        log_service.add(task_id, "info", "task_cancelled", "Cancelled while queued")
        return "cancelled"
    res = db.tasks.update_one(
        {"_id": task_id, "status": {"$in": list(ACTIVE)}},
        {"$set": {"status": "cancel_requested", "cancel_requested": True, "updated_at": now,
                  "progress_message": "Stop requested; the agent will stop at its next safe checkpoint"}})
    if res.modified_count:
        log_service.add(task_id, "info", "cancel_requested", "Administrator requested a stop")
        return "cancel_requested"
    return "noop"


def finish(task_id: str, status: str, summary: str | None = None, error: dict | None = None, **fields) -> None:
    assert status in TERMINAL
    db = get_db()
    now = utcnow()
    doc = db.tasks.find_one({"_id": task_id}, {"started_at": 1})
    started = as_aware((doc or {}).get("started_at"))
    update = {"status": status, "completed_at": now, "updated_at": now, "pending_prompt": None,
              "summary": summary, "error": error, "throttled": False, **fields}
    if started:
        update["duration_seconds"] = round((now - started).total_seconds(), 1)
    update["progress_message"] = {"completed": "Completed", "cancelled": "Cancelled",
                                  "failed": (error or {}).get("message", "Failed")[:300]}[status]
    db.tasks.update_one({"_id": task_id}, {"$set": update})


# -------------------------------------------------------------- actions history

def add_action(task_id: str, doc: dict) -> None:
    s = get_settings().secrets
    doc = {**doc, "task_id": task_id}
    for k in ("detail", "reason"):
        if isinstance(doc.get(k), str):
            doc[k] = redact(doc[k], s)[:600]
    get_db().task_actions.insert_one(doc)


def list_actions(task_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
    return list(get_db().task_actions.find({"task_id": task_id, "seq": {"$gt": after_seq}}).sort("seq", 1).limit(limit))


# -------------------------------------------------------------- administrator interaction

def set_pending_prompt(task_id: str, prompt: dict) -> str:
    pid = uuid.uuid4().hex[:12]
    get_db().tasks.update_one({"_id": task_id}, {"$set": {
        "pending_prompt": {**prompt, "id": pid, "asked_at": utcnow()}, "admin_response": None}})
    return pid


def clear_prompt(task_id: str) -> None:
    get_db().tasks.update_one({"_id": task_id}, {"$set": {"pending_prompt": None, "admin_response": None}})


def respond(task_id: str, prompt_id: str, approved: bool, message: str = "") -> bool:
    res = get_db().tasks.update_one(
        {"_id": task_id, "status": "waiting_for_admin", "pending_prompt.id": prompt_id, "admin_response": None},
        {"$set": {"admin_response": {"approved": approved, "message": (message or "")[:1000], "at": utcnow()}}})
    return res.modified_count == 1


def get_response(task_id: str, prompt_id: str) -> dict | None:
    doc = get_db().tasks.find_one({"_id": task_id}, {"admin_response": 1, "pending_prompt": 1})
    if doc and doc.get("pending_prompt", {}) and doc["pending_prompt"].get("id") == prompt_id:
        return doc.get("admin_response")
    return None


# -------------------------------------------------------------- queries

def list_tasks(status: str | None = None, website_id: str | None = None, search: str | None = None,
               page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    q: dict = {}
    if status in STATUSES:
        q["status"] = status
    if website_id:
        q["website_id"] = website_id
    if search:
        import re
        q["name"] = {"$regex": re.escape(search[:80]), "$options": "i"}
    col = get_db().tasks
    total = col.count_documents(q)
    docs = list(col.find(q, {"website_snapshot": 0, "params": 0}).sort("created_at", -1).skip((max(1, page) - 1) * per_page).limit(per_page))
    return docs, total


def running() -> list[dict]:
    return list(get_db().tasks.find({"status": {"$in": list(RUNNING_VIEW)}}, {"website_snapshot": 0, "params": 0}).sort("created_at", -1).limit(200))


def counts() -> dict:
    col = get_db().tasks
    return {
        "total": col.count_documents({}),
        "running": col.count_documents({"status": {"$in": list(ACTIVE)}}),
        "queued": col.count_documents({"status": "queued"}),
        "completed": col.count_documents({"status": "completed"}),
        "failed": col.count_documents({"status": "failed"}),
    }


def average_duration(sample: int = 50) -> float | None:
    docs = list(get_db().tasks.find({"status": "completed", "duration_seconds": {"$exists": True}}, {"duration_seconds": 1})
                .sort("completed_at", -1).limit(sample))
    if len(docs) < 3:
        return None
    return sum(d["duration_seconds"] for d in docs) / len(docs)


# -------------------------------------------------------------- recovery

def reap_stale() -> int:
    """Fail tasks whose worker stopped heartbeating (crash / OOM / restart) instead of leaving them 'running' forever."""
    s = get_settings()
    now = utcnow()
    db = get_db()
    n = 0
    stale_before = now - timedelta(seconds=s.stale_task_seconds + s.admin_wait_timeout_seconds * 0)
    for t in db.tasks.find({"status": {"$in": list(ACTIVE)}, "heartbeat_at": {"$lt": stale_before}}, {"status": 1, "heartbeat_at": 1}):
        if t["status"] == "waiting_for_admin":
            continue  # the worker is alive and polling; the admin wait has its own timeout
        finish(t["_id"], "failed", error={"code": "worker_lost", "message": "The worker stopped responding (crash, restart or out of memory)."})
        log_service.add(t["_id"], "error", "worker_lost", "Task marked failed: no heartbeat from the worker")
        n += 1
    queued_before = now - timedelta(hours=6)
    for t in db.tasks.find({"status": "queued", "created_at": {"$lt": queued_before}}, {"_id": 1}):
        finish(t["_id"], "failed", error={"code": "queue_timeout", "message": "The task waited in the queue for over 6 hours; is the worker running?"})
        n += 1
    return n


def recover_abandoned() -> int:
    """Run once when a worker boots: whatever was mid-flight was killed with the previous process."""
    n = 0
    for t in get_db().tasks.find({"status": {"$in": list(ACTIVE)}}, {"_id": 1}):
        finish(t["_id"], "failed", error={"code": "worker_restarted", "message": "The worker restarted while this task was running. Retry it if the actions were safe to repeat."})
        log_service.add(t["_id"], "error", "worker_restarted", "Task abandoned by a worker restart")
        n += 1
    return n


# -------------------------------------------------------------- templates

def save_template(name: str, website_id: str, instructions: str, params_text: str, max_actions: int, max_duration: int) -> str:
    name = (name or "").strip()
    if not 2 <= len(name) <= 120:
        raise TaskError("Give the template a name (2-120 characters).")
    doc = {"name": name, "website_id": website_id, "instructions": instructions.strip()[:8000], "params_text": params_text[:4000],
           "max_actions": max_actions, "max_duration": max_duration, "created_at": utcnow()}
    return str(get_db().task_templates.insert_one(doc).inserted_id)


def list_templates() -> list[dict]:
    return list(get_db().task_templates.find().sort("name", 1).limit(100))


def get_template(tid) -> dict | None:
    o = websites.oid(tid)
    return get_db().task_templates.find_one({"_id": o}) if o else None
