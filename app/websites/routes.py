"""Website configuration management."""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth.routes import login_required
from ..extensions import get_db, get_settings
from ..services import task_service, websites

bp = Blueprint("websites", __name__, url_prefix="/websites")


def _site_or_404(wid):
    site = websites.get(wid)
    if not site:
        abort(404)
    return site


@bp.route("")
@login_required
def index():
    items = websites.list_all()
    for it in items:
        it["task_count"] = get_db().tasks.count_documents({"website_id": str(it["_id"])})
    return render_template("websites.html", websites=items)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        data, errors = websites.validate_form(request.form)
        if not errors:
            wid = websites.create(data)
            flash(f"Saved “{data['name']}”.", "success")
            return redirect(url_for("websites.index"))
        return render_template("website_form.html", form=request.form, errors=errors, site=None), 422
    return render_template("website_form.html", form={"is_active": "1", "ocr_preference": "automatic"}, errors={}, site=None)


@bp.route("/<wid>/edit", methods=["GET", "POST"])
@login_required
def edit(wid):
    site = _site_or_404(wid)
    if request.method == "POST":
        data, errors = websites.validate_form(request.form)
        if not errors:
            websites.update(site["_id"], data)
            flash("Website updated." + (" Instructions saved as a new version." if data["instructions"] != site.get("instructions") else ""), "success")
            return redirect(url_for("websites.index"))
        return render_template("website_form.html", form=request.form, errors=errors, site=site,
                               revisions=websites.revisions(site["_id"])), 422
    import json
    form = {**site, "allowed_domains": "\n".join(site.get("allowed_domains", [])),
            "default_params": json.dumps(site.get("default_params") or {}, indent=2) if site.get("default_params") else "",
            "is_active": "1" if site.get("is_active") else ""}
    return render_template("website_form.html", form=form, errors={}, site=site, revisions=websites.revisions(site["_id"]))


@bp.route("/<wid>/toggle", methods=["POST"])
@login_required
def toggle(wid):
    site = _site_or_404(wid)
    websites.set_active(site["_id"], not site.get("is_active", True))
    flash(f"“{site['name']}” is now {'enabled' if not site.get('is_active', True) else 'disabled'}.", "success")
    return redirect(url_for("websites.index"))


@bp.route("/<wid>/delete", methods=["POST"])
@login_required
def delete(wid):
    site = _site_or_404(wid)
    websites.delete(site["_id"])
    flash(f"Deleted “{site['name']}”. Its task history was kept.", "success")
    return redirect(url_for("websites.index"))


@bp.route("/<wid>/test-connection", methods=["POST"])
@login_required
def test_connection(wid):
    site = _site_or_404(wid)
    r = websites.test_connectivity(site)
    flash(f"{site['name']}: {'reachable' if r['ok'] else 'not reachable'}. {r['message']}", "success" if r["ok"] else "danger")
    return redirect(url_for("websites.index"))


@bp.route("/<wid>/run-test", methods=["POST"])
@login_required
def run_test(wid):
    from ..tasks.jobs import enqueue
    site = _site_or_404(wid)
    try:
        task = task_service.create(
            site, f"Connectivity test: {site['name']}",
            "Open the website, read the page title and the main visible headings, then finish with done and one result row "
            "containing 'title' and 'headings'. Do not click anything.", {}, 8, min(120, get_settings().max_task_duration_seconds))
    except task_service.TaskError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("websites.index"))
    try:
        enqueue(task["_id"])
    except Exception:
        task_service.mark_submit_failed(task["_id"], "Could not reach Redis to queue the task.")
        flash("Redis is unreachable, so the test task could not be queued.", "danger")
    return redirect(url_for("tasks.detail", task_id=task["_id"]))
