"""Tasks: creation, monitoring, control, artifacts, and results."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from ..auth.routes import login_required
from ..extensions import get_settings
from ..services import log_service, result_service, task_service, websites
from ..utils import utcnow

bp = Blueprint("tasks", __name__)
log = logging.getLogger(__name__)
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _task_or_404(task_id):
    if not TASK_ID_RE.match(task_id):
        abort(404)
    t = task_service.get(task_id)
    if not t:
        abort(404)
    return t


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def pager(total: int, page: int, per_page: int) -> dict:
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    return {"page": page, "pages": pages, "total": total, "per_page": per_page,
            "has_prev": page > 1, "has_next": page < pages}


def _submit(task: dict) -> bool:
    from .jobs import enqueue
    try:
        enqueue(task["_id"])
        return True
    except Exception as exc:
        log.error("Could not enqueue task %s: %s", task["_id"], exc)
        task_service.mark_submit_failed(task["_id"], "Redis is unavailable, so the task could not be queued.")
        return False


# ------------------------------------------------------------------ create

@bp.route("/tasks/new", methods=["GET", "POST"])
@login_required
def new_task():
    s = get_settings()
    sites = websites.list_all()
    templates = task_service.list_templates()
    if request.method == "GET":
        form = {"max_actions": min(50, s.max_actions_per_task), "max_duration": min(600, s.max_task_duration_seconds),
                "website_id": request.args.get("website_id", "")}
        tid = request.args.get("template_id")
        if tid and (t := task_service.get_template(tid)):
            form.update(website_id=t["website_id"], name=t["name"], instructions=t["instructions"], params=t["params_text"],
                        max_actions=t["max_actions"], max_duration=t["max_duration"])
        return render_template("new_task.html", websites=sites, templates=templates, form=form, errors={}, s=s)

    f = request.form
    errors: dict[str, str] = {}
    site = websites.get(f.get("website_id", ""))
    if not site:
        errors["website_id"] = "Choose a website."
    try:
        params = websites.parse_params(f.get("params"))
    except ValueError as exc:
        params, errors["params"] = {}, str(exc)
    max_actions, max_duration = _int(f.get("max_actions"), 0), _int(f.get("max_duration"), 0)

    if f.get("intent") == "template":
        try:
            if not site:
                raise task_service.TaskError("Choose a website for the template.")
            task_service.save_template(f.get("name", ""), str(site["_id"]), f.get("instructions", ""), f.get("params", ""),
                                       max_actions or 50, max_duration or 300)
            flash("Template saved. Load it from the template list on this page.", "success")
            return redirect(url_for("tasks.new_task"))
        except task_service.TaskError as exc:
            errors["name"] = str(exc)
    elif not errors:
        try:
            task = task_service.create(site, f.get("name", ""), f.get("instructions", ""), params, max_actions, max_duration)
        except task_service.TaskError as exc:
            errors["form"] = str(exc)
        else:
            if _submit(task):
                flash("Task queued.", "success")
            else:
                flash("Redis is unreachable. The task was recorded as failed; check the worker/Redis settings.", "danger")
            return redirect(url_for("tasks.detail", task_id=task["_id"]))
    return render_template("new_task.html", websites=sites, templates=templates, form=f, errors=errors, s=s), 422


# ------------------------------------------------------------------ lists

@bp.route("/tasks/running")
@login_required
def running():
    task_service.reap_stale()
    return render_template("running_tasks.html", tasks=task_service.running())


@bp.route("/tasks")
@login_required
def history():
    status, wid = request.args.get("status") or None, request.args.get("website_id") or None
    q, page = request.args.get("q") or None, _int(request.args.get("page"), 1)
    docs, total = task_service.list_tasks(status, wid, q, page, 20)
    return render_template("task_history.html", tasks=docs, pager=pager(total, page, 20), statuses=task_service.STATUSES,
                           websites=websites.list_all(), f={"status": status or "", "website_id": wid or "", "q": q or ""})


# ------------------------------------------------------------------ detail & API

def _artifacts(task_id: str) -> list[dict]:
    d = Path(get_settings().artifact_dir) / task_id
    if not d.is_dir():
        return []
    return [{"name": p.name, "kind": "image" if p.suffix == ".png" else "data", "size": p.stat().st_size}
            for p in sorted(d.iterdir()) if p.is_file() and ARTIFACT_RE.match(p.name)]


@bp.route("/tasks/<task_id>")
@login_required
def detail(task_id):
    t = _task_or_404(task_id)
    return render_template(
        "task_detail.html", t=t, actions=task_service.list_actions(task_id), logs=log_service.for_task(task_id, limit=300),
        ocr_logs=log_service.for_task(task_id, events=["ocr"], limit=40), results=result_service.for_task(task_id, 100),
        artifacts=_artifacts(task_id), terminal=t["status"] in task_service.TERMINAL,
        retryable=t["status"] in task_service.RETRYABLE)


def _pub_task(t: dict) -> dict:
    keys = ("status", "current_action", "progress_message", "current_url", "result_count", "actions_executed", "throttled",
            "summary", "error", "token_usage", "retry_count")
    out = {k: t.get(k) for k in keys}
    out["id"] = t["_id"]
    for k in ("started_at", "completed_at", "updated_at"):
        out[k] = t[k].isoformat() if t.get(k) else None
    pp = t.get("pending_prompt")
    out["pending_prompt"] = {k: pp.get(k) for k in ("id", "kind", "message", "risk", "action")} if pp else None
    return out


def _pub_action(a: dict) -> dict:
    keys = ("seq", "action", "target", "reason", "outcome", "detail", "method", "url_after", "error", "verification", "params")
    out = {k: a.get(k) for k in keys}
    out["started_at"] = a["started_at"].isoformat() if a.get("started_at") else None
    return out


@bp.route("/api/tasks/running")
@login_required
def api_running():
    return jsonify(tasks=[{**_pub_task(t), "name": t["name"], "website_name": t.get("website_name"), "created_at": t["created_at"].isoformat()}
                          for t in task_service.running()])


@bp.route("/api/tasks/<task_id>/status")
@login_required
def api_status(task_id):
    t = _task_or_404(task_id)
    after = _int(request.args.get("after_seq"), 0)
    return jsonify(task=_pub_task(t), actions=[_pub_action(a) for a in task_service.list_actions(task_id, after, 100)])


@bp.route("/tasks/<task_id>/artifacts/<name>")
@login_required
def artifact(task_id, name):
    if not TASK_ID_RE.match(task_id) or not ARTIFACT_RE.match(name):
        abort(404)
    d = Path(get_settings().artifact_dir) / task_id
    if not (d / name).is_file():
        abort(404)
    return send_from_directory(d, name, as_attachment=name.endswith(".json"))


# ------------------------------------------------------------------ control

@bp.route("/tasks/<task_id>/cancel", methods=["POST"])
@login_required
def cancel(task_id):
    _task_or_404(task_id)
    state = task_service.request_cancel(task_id)
    if request.accept_mimetypes.best == "application/json":
        return jsonify(state=state)
    flash({"cancelled": "Task cancelled.", "cancel_requested": "Stop requested. The agent will stop at its next safe checkpoint.",
           "noop": "That task is not running."}[state], "info")
    return redirect(request.referrer or url_for("tasks.detail", task_id=task_id))


@bp.route("/tasks/<task_id>/respond", methods=["POST"])
@login_required
def respond(task_id):
    t = _task_or_404(task_id)
    prompt = t.get("pending_prompt")
    if not prompt:
        return jsonify(error="Nothing is waiting for a response."), 409
    data = request.get_json(silent=True) or request.form
    ok = task_service.respond(task_id, data.get("prompt_id", ""), str(data.get("approved", "")).lower() in ("1", "true", "yes"),
                              data.get("message", ""))
    return (jsonify(ok=True), 200) if ok else (jsonify(error="That request was already answered or expired."), 409)


@bp.route("/tasks/<task_id>/retry", methods=["POST"])
@login_required
def retry(task_id):
    t = _task_or_404(task_id)
    if t["status"] not in task_service.RETRYABLE:
        flash("Only failed or cancelled tasks can be retried.", "danger")
        return redirect(url_for("tasks.detail", task_id=task_id))
    site = websites.get(t["website_id"])
    if not site:
        flash("The website configuration was deleted, so this task cannot be retried.", "danger")
        return redirect(url_for("tasks.detail", task_id=task_id))
    try:
        new = task_service.create(site, t["name"], t["instructions"], {}, t["max_actions"], t["max_duration_seconds"],
                                  retry_of=task_id, retry_count=t.get("retry_count", 0) + 1, snapshot=t["website_snapshot"])
        new_params = t["params"]
        from ..extensions import get_db
        get_db().tasks.update_one({"_id": new["_id"]}, {"$set": {"params": new_params}})
    except task_service.TaskError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("tasks.detail", task_id=task_id))
    flash("Retry queued as a new task." if _submit(new) else "Redis is unreachable; the retry could not be queued.", "info")
    return redirect(url_for("tasks.detail", task_id=new["_id"]))


# ------------------------------------------------------------------ results

@bp.route("/results")
@login_required
def results():
    q = request.args.get("q") or None
    sort, direction = request.args.get("sort", "extracted_at"), request.args.get("dir", "desc")
    page, task_id = _int(request.args.get("page"), 1), request.args.get("task_id") or None
    docs, total = result_service.query(q, sort, direction, page, 25, task_id=task_id)
    return render_template("results.html", docs=docs, cols=result_service.columns_for(docs), pager=pager(total, page, 25),
                           f={"q": q or "", "sort": sort, "dir": direction, "task_id": task_id or ""})


@bp.route("/results/export.<fmt>")
@login_required
def export(fmt):
    if fmt not in ("json", "csv"):
        abort(404)
    q, task_id = request.args.get("q") or None, request.args.get("task_id") or None
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        body, mime = result_service.export_json(q, task_id), "application/json"
    else:
        body, mime = result_service.export_csv(q, task_id), "text/csv"
    return Response(body, mimetype=mime, headers={"Content-Disposition": f"attachment; filename=webpilot-results-{stamp}.{fmt}"})
