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


# -------- direct IP:port access (no reverse proxy, no HTTPS)
def _prod(tmp_path, **over):
    return load_settings(make_env(tmp_path, APP_ENV="production", ALLOW_PRIVATE_NETWORKS="false", **over))


def _login_cookie_and_headers(app):
    c = app.test_client()
    tok = csrf_token(c)
    r = c.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok})
    return r.headers.get("Set-Cookie", ""), r


def test_plain_http_mode_sets_non_secure_cookie_and_no_hsts(tmp_path, db):
    app = create_app(_prod(tmp_path, SECURE_COOKIES="false"), db=db)
    cookie, r = _login_cookie_and_headers(app)
    assert r.status_code == 302 and "Secure" not in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Strict-Transport-Security" not in app.test_client().get("/login").headers


def test_https_mode_still_sets_secure_cookie_and_hsts(tmp_path, db):
    app = create_app(_prod(tmp_path, SECURE_COOKIES="true"), db=db)
    cookie, _ = _login_cookie_and_headers(app)
    assert "Secure" in cookie and "Strict-Transport-Security" in app.test_client().get("/login").headers


def test_secure_cookies_default_is_safe_in_production(tmp_path):
    assert _prod(tmp_path).secure_cookies is True and load_settings(make_env(tmp_path)).secure_cookies is False


def test_session_works_over_plain_http_without_secure_cookie(tmp_path, db):
    c = create_app(_prod(tmp_path, SECURE_COOKIES="false"), db=db).test_client()
    base = "http://203.0.113.7:8051"      # what you type in the browser: http://<server-ip>:8051
    html = c.get("/login", base_url=base).get_data(as_text=True)
    tok = __import__("re").search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = c.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok}, base_url=base)
    assert r.status_code == 302
    assert c.get("/", base_url=base).status_code == 200


def test_spoofed_forwarded_for_cannot_dodge_login_rate_limit_without_a_proxy(tmp_path, db):
    s = load_settings(make_env(tmp_path, LOGIN_RATE_LIMIT="3 per minute"))
    assert s.trusted_proxies == 0
    c = create_app(s, db=db).test_client()
    tok = csrf_token(c)
    codes = [c.post("/login", data={"username": "admin", "password": "bad", "csrf_token": tok},
                    headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]


# -------- MongoDB Atlas configuration
ATLAS = "mongodb+srv://appuser:S3cr3t%40pw@cluster0.ab1cd.mongodb.net/?retryWrites=true&w=majority&appName=webpilot"


@pytest.mark.parametrize("uri,needle", [
    ("mongodb+srv://<db_username>:<db_password>@cluster0.ab1cd.mongodb.net/?retryWrites=true", "placeholder"),
    ("mongodb+srv://u:<db_password>@c.mongodb.net", "placeholder"),
    ("postgres://u:p@host/db", "must start with"),
    ("cluster0.ab1cd.mongodb.net", "must start with"),
])
def test_bad_mongo_uri_is_rejected_with_a_clear_message(tmp_path, uri, needle):
    with pytest.raises(ConfigError) as ei:
        load_settings(make_env(tmp_path, MONGO_URI=uri))
    assert needle in str(ei.value)


def test_atlas_uri_accepted_and_target_never_leaks_credentials(tmp_path):
    s = load_settings(make_env(tmp_path, MONGO_URI=ATLAS))
    assert s.mongo_uri == ATLAS
    assert s.mongo_target == "cluster0.ab1cd.mongodb.net / webpilot_test"
    assert "S3cr3t" not in str(s.public_summary()) and "appuser" not in str(s.public_summary())
    assert ATLAS in s.secrets        # so the whole URI is scrubbed from logs


def test_missing_mongo_config_explains_atlas_first(tmp_path):
    env = make_env(tmp_path)
    del env["MONGO_URI"]
    with pytest.raises(ConfigError) as ei:
        load_settings(env)
    assert "MONGO_URI is required" in str(ei.value) and "Atlas" in str(ei.value)


def test_mongo_client_kwargs_pin_ca_bundle_for_tls_only():
    from app.extensions import mongo_client_kwargs
    import certifi
    assert mongo_client_kwargs(ATLAS)["tlsCAFile"] == certifi.where()
    assert mongo_client_kwargs("mongodb://h:27017/?tls=true")["tlsCAFile"] == certifi.where()
    assert "tlsCAFile" not in mongo_client_kwargs("mongodb://u:p@webpilot-mongo:27017/?authSource=admin")   # local, no TLS
    assert "tlsCAFile" not in mongo_client_kwargs(ATLAS + "&tls=false")
    assert "tlsCAFile" not in mongo_client_kwargs(ATLAS + "&tlsCAFile=/my/ca.pem")
    assert mongo_client_kwargs(ATLAS)["serverSelectionTimeoutMS"] == 10000


def test_pymongo_accepts_the_client_options_without_connecting():
    """Constructing the client is lazy: proves the option names are valid for the pinned pymongo."""
    from pymongo import MongoClient
    from app.extensions import mongo_client_kwargs
    uri = "mongodb://u:p@127.0.0.1:1/?tls=true"
    c = MongoClient(uri, **mongo_client_kwargs(uri))
    c.close()


def test_startup_survives_unreachable_mongo_and_logs_actionable_hint(tmp_path, caplog):
    s = load_settings(make_env(tmp_path, MONGO_URI="mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300&connectTimeoutMS=300"))
    from app import extensions
    extensions.configure(s, db=None)
    import logging
    with caplog.at_level(logging.ERROR):
        app = create_app(s)                     # must not crash: Mongo may simply be unreachable for a moment
    assert any("Network Access" in r.getMessage() for r in caplog.records)
    r = app.test_client().get("/healthz")
    assert r.status_code == 503 and r.get_json()["mongo"] == "unavailable"
    extensions.configure(s, db=None)
