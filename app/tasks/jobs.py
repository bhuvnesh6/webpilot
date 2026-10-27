"""Celery jobs. The browser never runs inside a Flask request; it runs here."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery import shared_task

from ..agent.engine import AdminDecision, AgentEngine, AgentHooks, AgentTask, Limits
from ..agent.llm import TaskCancelled
from ..extensions import get_settings
from ..services import log_service, result_service, task_service

log = logging.getLogger(__name__)
TASK_NAME = "webpilot.run_browser_task"


# ------------------------------------------------------------------ submission (web side)

def enqueue(task_id: str) -> None:
    """Hand a task to the worker via the existing Redis. Raises if the broker is unreachable."""
    from celery_app import celery
    celery.send_task(TASK_NAME, args=[task_id], queue=get_settings().celery_queue,
                     retry=True, retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5})


# ------------------------------------------------------------------ per-process runtime (worker side)

class Runtime:
    def __init__(self, settings):
        from ..agent.browser import BrowserManager
        from ..agent.llm import GroqClient
        from ..agent.ocr import OcrEngine
        self.browsers = BrowserManager(settings)
        self.llm = GroqClient(settings)
        self.ocr = OcrEngine(settings.ocr_lang, settings.ocr_max_dimension, settings.ocr_min_confidence)


_runtime: Runtime | None = None
_rt_lock = threading.Lock()


def get_runtime() -> Runtime:
    global _runtime
    with _rt_lock:
        if _runtime is None:
            _runtime = Runtime(get_settings())
        return _runtime


def shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.browsers.close()
        _runtime = None


# ------------------------------------------------------------------ the job

@shared_task(name=TASK_NAME, bind=True, ignore_result=True)
def run_browser_task(self, task_id: str) -> None:
    execute_task(task_id, worker=getattr(self.request, "hostname", "") or "")


def execute_task(task_id: str, worker: str = "", runtime: Runtime | None = None) -> None:
    s = get_settings()
    task = task_service.claim(task_id, worker)
    if task is None:
        log.info("Task %s is not queued any more; skipping", task_id)
        return
    snap = task["website_snapshot"]
    cancel = lambda: task_service.is_cancel_requested(task_id)  # noqa: E731
    try:
        rt = runtime or get_runtime()
        hooks = _make_hooks(task_id, snap["website_id"], s, cancel)
        engine = AgentEngine(s, rt.browsers, rt.llm, rt.ocr, hooks)
        agent_task = AgentTask(
            task_id=task_id, start_url=snap["start_url"], website_instructions=snap["instructions"],
            task_instructions=task["instructions"], params=task["params"], allowed_domains=snap["allowed_domains"],
            ocr_mode=snap["ocr_preference"],
            limits=Limits(max_actions=task["max_actions"], max_duration_s=task["max_duration_seconds"],
                          max_recovery=s.max_recovery_attempts, max_llm_retries=s.max_llm_retries,
                          admin_wait_s=s.admin_wait_timeout_seconds),
            artifact_dir=Path(s.artifact_dir) / task_id)
        task_service.update_progress(task_id, status="running", current_action="navigate", message="Opening the website", url=snap["start_url"])
        result = engine.run(agent_task)
        _finalize(task_id, result)
    except Exception as exc:  # noqa: BLE001
        log.exception("Task %s crashed", task_id)
        try:
            log_service.add(task_id, "error", "job_crashed", f"{exc.__class__.__name__}: {exc}")
            task_service.finish(task_id, "failed", error={"code": "job_crashed", "message": f"{exc.__class__.__name__}: {str(exc)[:300]}"})
        except Exception:
            log.exception("Could not even record the failure of task %s", task_id)
    finally:
        try:
            cleanup_artifacts()
        except Exception:
            log.exception("Artifact cleanup failed")


def _finalize(task_id: str, result) -> None:
    saved = result_service.count_for_task(task_id)     # reads back from MongoDB: "completed" means *persisted*
    common = dict(actions_executed=result.actions_executed, consequential_actions=result.consequential_actions,
                  result_count=saved, current_url=result.final_url or None)
    if result.status == "completed":
        if saved < len(result.results):
            task_service.finish(task_id, "failed", error={"code": "persistence_error", "message": "Extracted rows were not found in the database after saving."}, **common)
            return
        task_service.finish(task_id, "completed", summary=result.summary, **common)
        log_service.add(task_id, "info", "task_completed", f"Completed with {saved} result row(s)")
    elif result.status == "cancelled":
        task_service.finish(task_id, "cancelled", summary=result.summary, **common)
        log_service.add(task_id, "info", "task_cancelled", "Stopped on request")
    else:
        task_service.finish(task_id, "failed", summary=f"Partial results saved: {saved}" if saved else None,
                            error={"code": result.error_code, "message": result.error_message}, **common)
        log_service.add(task_id, "error", "task_failed", f"{result.error_code}: {result.error_message}")


def _make_hooks(task_id: str, website_id: str, s, cancel) -> AgentHooks:
    def ask_admin(prompt: dict) -> AdminDecision:
        pid = task_service.set_pending_prompt(task_id, prompt)
        deadline = time.monotonic() + s.admin_wait_timeout_seconds
        try:
            while True:
                if cancel():
                    raise TaskCancelled()
                resp = task_service.get_response(task_id, pid)
                if resp:
                    return AdminDecision(bool(resp.get("approved")), resp.get("message", ""))
                if time.monotonic() > deadline:
                    return AdminDecision(False, timed_out=True)
                task_service.heartbeat(task_id)
                time.sleep(2)
        finally:
            task_service.clear_prompt(task_id)
            task_service.update_progress(task_id, status="running", message="Resuming")

    return AgentHooks(
        cancel_requested=cancel,
        progress=lambda status, action=None, message=None, url=None, **extra: task_service.update_progress(task_id, status, action, message, url, **extra),
        record_action=lambda doc: task_service.add_action(task_id, doc),
        log=lambda sev, event, msg, details=None: log_service.add(task_id, sev, event, msg, details),
        ask_admin=ask_admin,
        save_results=lambda rows, url: result_service.save_many(task_id, website_id, rows, url),
        add_tokens=lambda usage: task_service.add_tokens(task_id, usage),
    )


# ------------------------------------------------------------------ artifact retention

_last_cleanup = 0.0


def cleanup_artifacts(force: bool = False) -> int:
    """Delete task artifact folders older than ARTIFACT_RETENTION_DAYS (runs at most hourly per process)."""
    global _last_cleanup
    now = time.time()
    if not force and now - _last_cleanup < 3600:
        return 0
    _last_cleanup = now
    s = get_settings()
    root = Path(s.artifact_dir)
    if not root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=s.artifact_retention_days)
    removed = 0
    for d in root.iterdir():
        try:
            if d.is_dir() and datetime.fromtimestamp(d.stat().st_mtime, timezone.utc) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("Removed %d expired artifact folder(s)", removed)
    return removed
