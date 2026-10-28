"""Container health checks. Usage: healthcheck.py web | worker  (exit 0 = healthy)."""
import os
import socket
import sys
import urllib.request


def web() -> bool:
    port = os.environ.get("APP_PORT", "8051")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return r.status == 200       # 200 = MongoDB reachable ("degraded" when only Redis is down still counts)
    except Exception as exc:
        print(f"web unhealthy: {exc}", file=sys.stderr)
        return False


def worker() -> bool:
    try:
        sys.path.insert(0, os.getcwd())
        from celery_app import celery
        replies = celery.control.inspect(timeout=6).ping() or {}
        me = f"webpilot@{socket.gethostname()}"
        ok = me in replies
        if not ok:
            print(f"worker {me} did not answer ping (got {list(replies)})", file=sys.stderr)
        return ok
    except Exception as exc:
        print(f"worker unhealthy: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "web"
    sys.exit(0 if {"web": web, "worker": worker}[target]() else 1)
