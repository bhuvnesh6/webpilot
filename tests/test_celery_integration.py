"""Real broker path: jobs.enqueue -> Redis -> Celery worker -> engine -> Chromium -> MongoDB (mongomock)."""
import json
import shutil
import socket
import subprocess
import time

import pytest

from app import extensions
from app.agent.llm import Completion
from app.config import load_settings
from app.services import task_service
from tests.conftest import make_env
from tests.test_tasks import Runtime, Script, make_site

pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server not installed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def redis_port():
    port = free_port()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()


def test_enqueue_to_worker_to_completion(tmp_path, redis_port, site, monkeypatch):
    import mongomock
    s = load_settings(make_env(tmp_path, REDIS_PORT=str(redis_port), REDIS_DB="4", CELERY_QUEUE="webpilot-test", BROWSER_SANDBOX="false"))
    db = mongomock.MongoClient(tz_aware=True)["celery_it"]
    extensions.configure(s, db=db)
    import celery_app
    celery = celery_app.celery
    celery.conf.broker_url = s.redis_url
    celery.conf.task_default_queue = "webpilot-test"
    celery.set_default()
    from app.tasks import jobs
    from app.agent.browser import BrowserManager

    llm = Script([{"action": "click", "target": {"text": "Search", "role": "button"}, "expect_text": "30 found"},
                  {"action": "done", "summary": "via celery", "results": [{"ok": "yes"}]}])
    monkeypatch.setattr(jobs, "Runtime", lambda settings: Runtime(BrowserManager(settings), llm))
    jobs._runtime = None

    t = task_service.create(make_site(db, url=site + "/"), "Via Celery", "click search then finish", {}, 20, 120)
    from celery.contrib.testing.worker import start_worker
    with start_worker(celery, pool="solo", perform_ping_check=False, loglevel="WARNING", queues=["webpilot-test"]):
        jobs.enqueue(t["_id"])
        deadline = time.time() + 60
        while time.time() < deadline and task_service.get(t["_id"])["status"] not in ("completed", "failed"):
            time.sleep(0.3)
    doc = task_service.get(t["_id"])
    jobs.shutdown_runtime()
    assert doc["status"] == "completed", doc["error"]
    assert doc["result_count"] == 1 and doc["worker"]
    # a second delivery of the same message must not run the task again
    assert task_service.claim(t["_id"]) is None


def test_enqueue_fails_fast_when_redis_is_down(tmp_path):
    """Fresh interpreter so no connection pool from other tests is reused."""
    import os
    import sys
    env = {**os.environ, **make_env(tmp_path, REDIS_PORT=str(free_port()))}
    code = ("import time; t=time.time()\n"
            "from app.tasks import jobs\n"
            "try:\n    jobs.enqueue('a'*32); print('NO-ERROR')\n"
            "except Exception as e:\n    print('RAISED', type(e).__name__)\n"
            "print('elapsed', round(time.time()-t, 1))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, cwd=str(__import__("pathlib").Path(__file__).parent.parent))
    assert "RAISED" in out.stdout, out.stdout + out.stderr
    assert float(out.stdout.split("elapsed")[1]) < 15
