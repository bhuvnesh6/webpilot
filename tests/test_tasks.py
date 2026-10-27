import json
import threading
import time
from datetime import timedelta

import pytest

from app.agent.llm import Completion
from app.services import log_service, result_service, task_service
from app.tasks import jobs
from app.utils import utcnow
from tests.conftest import post

pytest.importorskip("playwright.sync_api")


def make_site(db, url="http://127.0.0.1:9/", **kw):
    doc = {"name": "S", "start_url": url, "instructions": "site rules", "allowed_domains": [], "default_params": {"country": "Canada"},
           "ocr_preference": "automatic", "is_active": True, "instructions_version": 3, "created_at": utcnow(), "updated_at": utcnow(), **kw}
    doc["_id"] = db.websites.insert_one(doc).inserted_id
    return doc


def make_task(db, **kw):
    site = make_site(db)
    return task_service.create(site, "Task", "do something useful", {"k": "v"}, 20, 120, **kw)


# ---------------------------------------------------------------- service layer
def test_create_snapshots_instructions_and_merges_params(db):
    site = make_site(db)
    t = task_service.create(site, "Task", "do something useful", {"k": "v"}, 20, 120)
    assert t["params"] == {"country": "Canada", "k": "v"} and t["website_snapshot"]["instructions_version"] == 3
    db.websites.update_one({"_id": site["_id"]}, {"$set": {"instructions": "changed later"}})
    assert task_service.get(t["_id"])["website_snapshot"]["instructions"] == "site rules"     # reproducible


@pytest.mark.parametrize("kw", [{"max_actions": 0}, {"max_actions": 10**6}, {"max_duration": 5}, {"name": "x"}, {"instructions": "hi"}])
def test_create_validates_limits(db, kw):
    site = make_site(db)
    args = dict(name="T", instructions="do something useful", params={}, max_actions=20, max_duration=120)
    args.update(kw)
    with pytest.raises(task_service.TaskError):
        task_service.create(site, args["name"], args["instructions"], args["params"], args["max_actions"], args["max_duration"])


def test_claim_is_atomic(db):
    t = make_task(db)
    assert task_service.claim(t["_id"], "w1") is not None
    assert task_service.claim(t["_id"], "w2") is None            # duplicate delivery can't start it twice


def test_cancel_semantics_and_progress_cannot_overwrite_cancel(db):
    q = make_task(db)
    assert task_service.request_cancel(q["_id"]) == "cancelled"
    assert task_service.claim(q["_id"]) is None                  # a cancelled queued task never starts
    r = make_task(db)
    task_service.claim(r["_id"])
    task_service.update_progress(r["_id"], status="running", current_action="click")
    assert task_service.request_cancel(r["_id"]) == "cancel_requested"
    task_service.update_progress(r["_id"], status="running", message="still going")   # worker racing the request
    doc = task_service.get(r["_id"])
    assert doc["status"] == "cancel_requested" and task_service.is_cancel_requested(r["_id"])
    task_service.finish(r["_id"], "cancelled")
    task_service.update_progress(r["_id"], status="running")
    assert task_service.get(r["_id"])["status"] == "cancelled"                         # terminal is final
    assert task_service.request_cancel(r["_id"]) == "noop"


def test_finish_records_duration_and_progress(db):
    t = make_task(db)
    task_service.claim(t["_id"])
    task_service.finish(t["_id"], "failed", error={"code": "x", "message": "boom"})
    doc = task_service.get(t["_id"])
    assert doc["status"] == "failed" and doc["duration_seconds"] >= 0 and doc["progress_message"] == "boom"


def test_reaper_fails_silent_workers_but_not_live_or_waiting_ones(db):
    old = utcnow() - timedelta(hours=1)
    a, b, c = make_task(db), make_task(db), make_task(db)
    for t, st in ((a, "running"), (b, "waiting_for_admin"), (c, "running")):
        db.tasks.update_one({"_id": t["_id"]}, {"$set": {"status": st, "heartbeat_at": old if t is not c else utcnow()}})
    assert task_service.reap_stale() == 1
    assert task_service.get(a["_id"])["error"]["code"] == "worker_lost"
    assert task_service.get(b["_id"])["status"] == "waiting_for_admin" and task_service.get(c["_id"])["status"] == "running"


def test_recover_abandoned_on_worker_boot(db):
    a, q = make_task(db), make_task(db)
    db.tasks.update_one({"_id": a["_id"]}, {"$set": {"status": "running"}})
    assert task_service.recover_abandoned() == 1
    assert task_service.get(a["_id"])["error"]["code"] == "worker_restarted" and task_service.get(q["_id"])["status"] == "queued"


def test_admin_response_is_bound_to_prompt_id(db):
    t = make_task(db)
    task_service.claim(t["_id"])
    pid = task_service.set_pending_prompt(t["_id"], {"kind": "approval", "message": "ok?"})
    assert not task_service.respond(t["_id"], pid, True)          # not waiting_for_admin yet
    task_service.update_progress(t["_id"], status="waiting_for_admin")
    assert not task_service.respond(t["_id"], "wrong-id", True)
    assert task_service.respond(t["_id"], pid, True, "go")
    assert not task_service.respond(t["_id"], pid, False)         # first answer wins
    assert task_service.get_response(t["_id"], pid)["approved"] is True


def test_logs_are_scrubbed(db, settings):
    log_service.add("t", "error", "x", f"leak {settings.groq_api_key} and Authorization: Bearer abcdefghijklmnop", {"k": settings.admin_password})
    d = db.task_logs.find_one({})
    assert settings.groq_api_key not in d["message"] and "abcdefghijklmnop" not in d["message"] and "[REDACTED]" in d["details"]["k"]


# ---------------------------------------------------------------- results
def test_results_search_sort_pagination_and_safe_csv(admin, db):
    result_service.save_many("t" * 32, "w", [{"name": "Laptop A", "price": "$900"}, {"name": "=HYPERLINK(\"http://evil\")", "price": "$1"},
                                             {"name": "Phone", "price": "$300"}], "http://s/x")
    body = admin.get("/results?q=laptop").get_data(as_text=True)
    assert "Laptop A" in body and "Phone" not in body
    asc = admin.get("/results?sort=data.price&dir=asc").get_data(as_text=True)
    assert asc.index("$1") < asc.index("$300") < asc.index("$900")
    csv_text = admin.get("/results/export.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in csv_text and "\n=HYPERLINK" not in csv_text and csv_text.splitlines()[0].startswith("task_id,source_url,extracted_at")
    data = json.loads(admin.get("/results/export.json?q=phone").get_data(as_text=True))
    assert len(data) == 1 and data[0]["data"]["name"] == "Phone"
    assert admin.get("/results/export.csv").headers["Content-Disposition"].startswith("attachment")
    assert admin.get("/results?sort=data.$where&dir=asc").status_code == 200          # hostile sort key is ignored


# ---------------------------------------------------------------- routes
def test_new_task_flow_detail_status_cancel_retry(admin, db, monkeypatch):
    queued = []
    monkeypatch.setattr("app.tasks.jobs.enqueue", lambda tid: queued.append(tid))
    site = make_site(db)
    r = post(admin, "/tasks/new", {"website_id": str(site["_id"]), "name": "Find laptops", "instructions": "search laptops",
                                    "params": "keywords: laptops", "max_actions": "30", "max_duration": "120", "intent": "start"}, page="/tasks/new")
    assert r.status_code == 302 and len(queued) == 1
    tid = queued[0]
    assert "Find laptops" in admin.get(f"/tasks/{tid}").get_data(as_text=True)
    assert admin.get("/tasks/running").status_code == 200 and "Find laptops" in admin.get("/tasks/running").get_data(as_text=True)
    st = admin.get(f"/api/tasks/{tid}/status").get_json()
    assert st["task"]["status"] == "queued" and st["actions"] == []
    assert "Find laptops" in admin.get("/tasks").get_data(as_text=True)
    assert "Find laptops" not in admin.get("/tasks?status=failed").get_data(as_text=True)
    post(admin, f"/tasks/{tid}/cancel", page="/tasks/running")
    assert task_service.get(tid)["status"] == "cancelled"
    post(admin, f"/tasks/{tid}/retry", page=f"/tasks/{tid}")
    assert len(queued) == 2 and task_service.get(queued[1])["retry_of"] == tid and task_service.get(queued[1])["params"]["keywords"] == "laptops"


def test_redis_down_marks_task_failed_instead_of_hanging(admin, db, monkeypatch):
    def boom(tid):
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.tasks.jobs.enqueue", boom)
    site = make_site(db)
    r = post(admin, "/tasks/new", {"website_id": str(site["_id"]), "name": "Task", "instructions": "do something", "max_actions": "5",
                                    "max_duration": "60", "intent": "start"}, page="/tasks/new", follow_redirects=True)
    t = db.tasks.find_one({})
    assert t["status"] == "failed" and t["error"]["code"] == "queue_unavailable" and b"Redis is unreachable" in r.data


def test_template_save_and_load(admin, db):
    site = make_site(db)
    post(admin, "/tasks/new", {"website_id": str(site["_id"]), "name": "My template", "instructions": "do x y z", "params": "a: 1",
                                "max_actions": "9", "max_duration": "99", "intent": "template"}, page="/tasks/new")
    t = db.task_templates.find_one({})
    page = admin.get(f"/tasks/new?template_id={t['_id']}").get_data(as_text=True)
    assert "do x y z" in page and 'value="9"' in page and db.tasks.count_documents({}) == 0


def test_artifact_route_blocks_traversal_and_serves_files(admin, settings, db):
    import pathlib
    tid = "a" * 32
    d = pathlib.Path(settings.artifact_dir) / tid
    d.mkdir(parents=True)
    (d / "shot-001-x.png").write_bytes(b"\x89PNG")
    (pathlib.Path(settings.artifact_dir) / "secret.txt").write_text("nope")
    assert admin.get(f"/tasks/{tid}/artifacts/shot-001-x.png").status_code == 200
    for bad in ("..%2Fsecret.txt", "..%5Csecret.txt", "%2e%2e%2fsecret.txt"):
        assert admin.get(f"/tasks/{tid}/artifacts/{bad}").status_code == 404
    assert admin.get("/tasks/..%2F..%2Fetc/artifacts/passwd").status_code == 404


def test_running_page_reaps_stale(admin, db):
    t = make_task(db)
    db.tasks.update_one({"_id": t["_id"]}, {"$set": {"status": "running", "heartbeat_at": utcnow() - timedelta(hours=2)}})
    admin.get("/tasks/running")
    assert task_service.get(t["_id"])["status"] == "failed"


# ---------------------------------------------------------------- full pipeline: job -> engine -> Chromium -> MongoDB
class Script:
    def __init__(self, items):
        self.items, self.i = list(items), 0

    def complete(self, messages, cancel_check=None, on_throttle=None):
        item = self.items[min(self.i, len(self.items) - 1)]
        self.i += 1
        return Completion(json.dumps(item), {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})


class Runtime:
    def __init__(self, browsers, llm):
        self.browsers, self.llm, self.ocr = browsers, llm, None


@pytest.fixture(scope="module")
def mgr(tmp_path_factory):
    from app.agent.browser import BrowserManager
    from app.config import load_settings
    from tests.conftest import make_env
    m = BrowserManager(load_settings(make_env(tmp_path_factory.mktemp("m"), BROWSER_SANDBOX="false")))
    try:
        m.ensure()
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")
    yield m
    m.close()


def test_job_runs_task_end_to_end_and_persists_everything(db, settings, site, mgr):
    s = make_site(db, url=site + "/")
    t = task_service.create(s, "Laptops", "search for laptops", {}, 30, 120)
    llm = Script([
        {"action": "type_text", "target": {"label": "Search keywords"}, "value": "laptops"},
        {"action": "click", "target": {"text": "Search", "role": "button"}, "expect_text": "30 found"},
        {"action": "record_results", "records": [{"name": "Laptop Air 13 #1", "price": "$325", "url": site + "/product/1.html"}]},
        {"action": "done", "summary": "Found laptops", "results": [{"name": "Laptop Pro 14 #2", "price": "$350"}]},
    ])
    jobs.execute_task(t["_id"], "test-worker", runtime=Runtime(mgr, llm))
    doc = task_service.get(t["_id"])
    assert doc["status"] == "completed", doc["error"]
    assert doc["result_count"] == 2 and doc["actions_executed"] == 3 and doc["token_usage"]["calls"] == 4 and doc["duration_seconds"] >= 0
    assert [r["data"]["name"] for r in result_service.for_task(t["_id"])] == ["Laptop Air 13 #1", "Laptop Pro 14 #2"]
    acts = task_service.list_actions(t["_id"])
    assert [a["seq"] for a in acts] == [1, 2, 3, 4] and acts[1]["verification"]["ok"] is True
    assert any(l["event"] == "task_completed" for l in log_service.for_task(t["_id"]))
    import pathlib
    assert list((pathlib.Path(settings.artifact_dir) / t["_id"]).glob("shot-*final*.png"))     # evidence screenshot kept


def test_job_skips_tasks_that_are_not_queued(db, mgr):
    t = make_task(db)
    task_service.request_cancel(t["_id"])
    jobs.execute_task(t["_id"], "w", runtime=Runtime(mgr, Script([{"action": "done"}])))
    assert task_service.get(t["_id"])["status"] == "cancelled" and db.task_actions.count_documents({}) == 0


def test_job_records_failure_when_start_url_blocked(db, mgr):
    s = make_site(db, url="http://127.0.0.1:1/")      # nothing listens -> unreachable
    t = task_service.create(s, "Down", "open it please", {}, 10, 60)
    from app.config import load_settings  # shorten retries: use the real settings but one recovery attempt
    jobs.execute_task(t["_id"], "w", runtime=Runtime(mgr, Script([{"action": "done"}])))
    doc = task_service.get(t["_id"])
    assert doc["status"] == "failed" and doc["error"]["code"] in ("website_unavailable", "navigation_failed")


def test_admin_approval_flow_through_the_job(db, settings, site, mgr):
    s = make_site(db, url=site + "/")
    t = task_service.create(s, "Buy", "search then buy the first", {}, 30, 120)
    llm = Script([
        {"action": "click", "target": {"text": "Search", "role": "button"}, "expect_text": "30 found"},
        {"action": "click", "target": {"text": "Buy now", "near": "Laptop Gamer X #2"}, "expect_text": "ORDER PLACED"},
        {"action": "done", "summary": "ordered"},
    ])
    seen = {}

    def approver():
        deadline = time.time() + 40
        while time.time() < deadline:
            d = task_service.get(t["_id"])
            if d["status"] == "waiting_for_admin" and d.get("pending_prompt"):
                seen["prompt"] = d["pending_prompt"]
                assert task_service.respond(t["_id"], d["pending_prompt"]["id"], True, "approved by test")
                return
            time.sleep(0.2)

    th = threading.Thread(target=approver)
    th.start()
    jobs.execute_task(t["_id"], "w", runtime=Runtime(mgr, llm))
    th.join()
    doc = task_service.get(t["_id"])
    assert doc["status"] == "completed", doc["error"]
    assert "purchase" in seen["prompt"]["risk"] and doc["consequential_actions"] == 1 and doc["pending_prompt"] is None
    assert any(l["event"] == "action_approved" for l in log_service.for_task(t["_id"]))


def test_cancel_requested_mid_run_stops_the_job(db, settings, site, mgr):
    s = make_site(db, url=site + "/")
    t = task_service.create(s, "Long", "scroll forever please", {}, 60, 120)

    class Canceller(Script):
        def complete(self, messages, cancel_check=None, on_throttle=None):
            if self.i == 2:
                task_service.request_cancel(t["_id"])
            return super().complete(messages, cancel_check, on_throttle)

    jobs.execute_task(t["_id"], "w", runtime=Runtime(mgr, Canceller([{"action": "scroll", "direction": "down"}] * 50)))
    doc = task_service.get(t["_id"])
    assert doc["status"] == "cancelled" and doc["actions_executed"] <= 4
