import pytest

from app import create_app
from app.config import ConfigError, load_settings
from tests.conftest import csrf_token, make_env, post

PROTECTED = ["/", "/websites", "/websites/new", "/tasks/new", "/tasks/running", "/tasks", "/results", "/settings",
             "/results/export.csv", "/results/export.json"]


def test_login_success_and_failure(client):
    tok = csrf_token(client)
    bad = client.post("/login", data={"username": "admin", "password": "nope", "csrf_token": tok})
    assert bad.status_code == 401 and b"not correct" in bad.data
    bad2 = client.post("/login", data={"username": "root", "password": "correct-horse-battery-staple", "csrf_token": tok})
    assert bad2.status_code == 401
    ok = client.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok})
    assert ok.status_code == 302 and client.get("/").status_code == 200


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_redirect_when_anonymous(client, path):
    r = client.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_api_routes_return_401_json(client):
    for path in ("/api/tasks/running", "/api/tasks/" + "a" * 32 + "/status"):
        r = client.get(path)
        assert r.status_code == 401 and r.is_json


def test_state_changing_routes_reject_anonymous_and_missing_csrf(client, admin):
    assert admin.post("/websites/new", data={"name": "x"}).status_code == 400   # no CSRF token
    assert admin.post("/logout").status_code == 400
    anon = client.application.test_client()
    r = anon.post("/tasks/" + "a" * 32 + "/cancel")
    assert r.status_code in (302, 400, 401)


def test_logout_invalidates_session(admin):
    assert admin.get("/").status_code == 200
    post(admin, "/logout")
    assert admin.get("/").status_code == 302


def test_open_redirect_is_blocked(client):
    tok = csrf_token(client)
    r = client.post("/login?next=https://evil.example/", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok})
    assert r.headers["Location"].startswith("/") and "evil" not in r.headers["Location"]


def test_password_rotation_invalidates_existing_sessions(tmp_path, db, admin):
    cookie_jar = admin
    new_settings = load_settings(make_env(tmp_path, ADMIN_PASSWORD="a-different-long-password-123"))
    app2 = create_app(new_settings, db=db)
    c2 = app2.test_client()
    for cookie in cookie_jar._cookies.values() if hasattr(cookie_jar, "_cookies") else []:
        pass
    # replay the old cookie against the app with the rotated password
    old = cookie_jar.get_cookie("webpilot_session")
    c2.set_cookie("webpilot_session", old.value)
    assert c2.get("/").status_code == 302


def test_security_headers_and_cookie_flags(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    tok = csrf_token(client)
    r = client.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok})
    sc = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in sc and "SameSite=Lax" in sc


def test_login_rate_limit(tmp_path, db):
    s = load_settings(make_env(tmp_path, LOGIN_RATE_LIMIT="3 per minute"))
    c = create_app(s, db=db).test_client()
    tok = csrf_token(c)
    codes = [c.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": tok}).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_secrets_never_rendered(admin, settings):
    for path in ("/settings", "/"):
        body = admin.get(path).get_data(as_text=True)
        for secret in (settings.admin_password, settings.secret_key, settings.groq_api_key):
            assert secret not in body


# -------- startup validation
@pytest.mark.parametrize("over,needle", [
    ({"SECRET_KEY": ""}, "SECRET_KEY"), ({"SECRET_KEY": "short"}, "32 characters"),
    ({"ADMIN_PASSWORD": ""}, "ADMIN_PASSWORD"), ({"ADMIN_PASSWORD": "replace_with_a_strong_unique_password"}, "placeholder"),
    ({"ADMIN_PASSWORD": "admin"}, "weak"), ({"GROQ_API_KEY": "replace_with_your_groq_api_key"}, "GROQ_API_KEY"),
    ({"REDIS_HOST": ""}, "REDIS_HOST"), ({"MAX_ACTIONS_PER_TASK": "abc"}, "integer"),
    ({"APP_ENV": "production", "ADMIN_PASSWORD": "tooshort1"}, "12 characters"),
    ({"APP_ENV": "production"}, "ALLOW_PRIVATE_NETWORKS"),
])
def test_startup_fails_on_bad_config(tmp_path, over, needle):
    with pytest.raises(ConfigError) as ei:
        load_settings(make_env(tmp_path, **over))
    assert needle in str(ei.value)


def test_no_default_admin_is_created(tmp_path):
    env = make_env(tmp_path)
    del env["ADMIN_USERNAME"], env["ADMIN_PASSWORD"]
    with pytest.raises(ConfigError):
        load_settings(env)


def test_mongo_uri_built_from_parts_and_quoted(tmp_path):
    env = make_env(tmp_path, MONGO_ROOT_USERNAME="u", MONGO_ROOT_PASSWORD="p@ss/word:1x")
    del env["MONGO_URI"]
    s = load_settings(env)
    assert "p%40ss%2Fword%3A1x" in s.mongo_uri and s.mongo_uri.endswith("authSource=admin")
