import pytest

from app import create_app
from app.config import load_settings
from app.services import task_service, websites
from tests.conftest import make_env, post

FORM = {"name": "Product Research Test", "start_url": "http://127.0.0.1:9/", "description": "d",
        "instructions": "Find the search field.", "allowed_domains": "", "default_params": "country: Canada",
        "ocr_preference": "automatic", "is_active": "1"}


def test_create_edit_versioning_and_persistence(admin, db):
    r = post(admin, "/websites/new", FORM, page="/websites/new")
    assert r.status_code == 302
    site = db.websites.find_one({})
    assert site["instructions_version"] == 1 and site["default_params"] == {"country": "Canada"} and site["is_active"] is True
    # edit instructions -> version 2, revision history kept
    r = post(admin, f"/websites/{site['_id']}/edit", {**FORM, "instructions": "New instructions"}, page="/websites/new")
    assert r.status_code == 302
    site = db.websites.find_one({})
    assert site["instructions_version"] == 2 and db.website_revisions.count_documents({}) == 2
    # editing something else doesn't bump the version
    post(admin, f"/websites/{site['_id']}/edit", {**FORM, "instructions": "New instructions", "name": "Renamed"}, page="/websites/new")
    assert db.websites.find_one({})["instructions_version"] == 2
    body = admin.get("/websites").get_data(as_text=True)
    assert "Renamed" in body


def test_persists_across_app_restart(settings, db, admin):
    post(admin, "/websites/new", FORM, page="/websites/new")
    app2 = create_app(settings, db=db)                          # "restart": new app object, same database
    c2 = app2.test_client()
    from tests.conftest import csrf_token
    c2.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": csrf_token(c2)})
    assert "Product Research Test" in c2.get("/websites").get_data(as_text=True)


@pytest.mark.parametrize("field,value", [("start_url", "not a url"), ("start_url", "file:///etc/passwd"), ("name", "x"),
                                          ("allowed_domains", "localhost"), ("default_params", "{bad json"),
                                          ("default_params", "password: hunter2"), ("ocr_preference", "sometimes")])
def test_invalid_input_is_rejected(admin, db, field, value):
    r = post(admin, "/websites/new", {**FORM, field: value}, page="/websites/new")
    assert r.status_code == 422 and db.websites.count_documents({}) == 0


def test_ssrf_urls_rejected_in_production_policy(tmp_path, db):
    s = load_settings(make_env(tmp_path, ALLOW_PRIVATE_NETWORKS="false"))
    app = create_app(s, db=db)
    with app.app_context():
        for url in ("http://127.0.0.1/", "http://localhost:8000", "http://169.254.169.254/", "http://redis:6379", "http://10.0.0.2"):
            _, errors = websites.validate_form({**FORM, "start_url": url})
            assert "start_url" in errors, url


def test_inactive_site_cannot_start_tasks_and_delete_keeps_history(admin, db, monkeypatch):
    monkeypatch.setattr("app.tasks.jobs.enqueue", lambda tid: None)
    post(admin, "/websites/new", {**FORM, "is_active": ""}, page="/websites/new")
    site = db.websites.find_one({})
    r = post(admin, "/tasks/new", {"website_id": str(site["_id"]), "name": "t1", "instructions": "do a thing", "max_actions": "10",
                                    "max_duration": "60", "intent": "start"}, page="/tasks/new")
    assert r.status_code == 422 and b"disabled" in r.data and db.tasks.count_documents({}) == 0
    post(admin, f"/websites/{site['_id']}/toggle", page="/websites")
    post(admin, "/tasks/new", {"website_id": str(site["_id"]), "name": "t1", "instructions": "do a thing", "max_actions": "10",
                                "max_duration": "60", "intent": "start"}, page="/tasks/new")
    assert db.tasks.count_documents({}) == 1
    post(admin, f"/websites/{site['_id']}/delete", page="/websites")
    assert db.websites.count_documents({}) == 0 and db.tasks.count_documents({}) == 1
    assert db.tasks.find_one({})["website_snapshot"]["instructions"] == "Find the search field."


def test_params_parsing():
    assert websites.parse_params('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}
    assert websites.parse_params("a: 1\n# c\nb = two") == {"a": "1", "b": "two"}
    for bad in ('["list"]', "no separator", '{"api_key": "x"}', "Password: x", "card_number: 4111"):
        with pytest.raises(ValueError):
            websites.parse_params(bad)
