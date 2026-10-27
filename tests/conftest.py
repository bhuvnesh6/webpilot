import functools
import http.server
import threading
from pathlib import Path

import mongomock
import pytest

from app import extensions
from app.config import load_settings

SITE_DIR = Path(__file__).parent / "site"


def make_env(tmp_path, **over):
    env = {
        "APP_ENV": "testing",
        "SECRET_KEY": "k3y-for-tests-only-0123456789abcdef0123456789abcdef",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "correct-horse-battery-staple",
        "MONGO_URI": "mongodb://localhost:27017/test",
        "MONGO_DATABASE": "webpilot_test",
        "REDIS_HOST": "localhost",
        "GROQ_API_KEY": "gsk_testtesttesttesttesttest",
        "ALLOW_PRIVATE_NETWORKS": "true",
        "ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "LOGIN_RATE_LIMIT": "1000 per minute",
    }
    env.update(over)
    return env


@pytest.fixture
def settings(tmp_path):
    return load_settings(make_env(tmp_path))


@pytest.fixture
def db(settings):
    client = mongomock.MongoClient(tz_aware=True)
    database = client["webpilot_test"]
    extensions.configure(settings, db=database)
    extensions.ensure_indexes(database)
    yield database
    extensions.configure(settings, db=None)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # serve index.html with the real port substituted for the partner link
        if self.path == "/redir":   # redirect to a host that is NOT on the allowlist
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/other.html")
            self.end_headers()
        elif self.path in ("/", "/index.html"):
            body = (SITE_DIR / "index.html").read_text().replace("__PORT__", str(self.server.server_port)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


@pytest.fixture(scope="session")
def site():
    handler = functools.partial(_Handler, directory=str(SITE_DIR))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


import re as _re

from app import create_app


@pytest.fixture
def app(settings, db):
    return create_app(settings, db=db)


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return _re.search(r'name="csrf-token" content="([^"]+)"|name="csrf_token" value="([^"]+)"', html).groups()[0] or \
        _re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def admin(client):
    tok = csrf_token(client)
    r = client.post("/login", data={"username": "admin", "password": "correct-horse-battery-staple", "csrf_token": tok})
    assert r.status_code == 302
    return client


def post(client, url, data=None, page="/", **kw):
    data = dict(data or {})
    data["csrf_token"] = csrf_token(client, page)
    return client.post(url, data=data, **kw)
