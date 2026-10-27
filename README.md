# WebPilot AI

A private, single-admin dashboard where you save websites + natural-language instructions and an AI agent operates them for you.

- **Brain**: Groq LLM chooses one action at a time (JSON only; it can never run code).
- **Eyes**: Playwright DOM/accessibility inspection first, PaddleOCR (local) as the fallback.
- **Hands**: Playwright/Chromium executes validated actions inside an isolated, policy-enforced browser context.

Stack: Flask · MongoDB · Celery + your **existing Redis** · Playwright/Chromium · PaddleOCR · Docker Compose · Nginx Proxy Manager.

---

## Contents

1. [What has and has not been verified](#1-what-has-and-has-not-been-verified)
2. [System requirements](#2-system-requirements)
3. [First deployment](#3-first-deployment)
4. [Nginx Proxy Manager and HTTPS](#4-nginx-proxy-manager-and-https)
5. [Using it](#5-using-it)
6. [Operations: logs, restarts, updates, rollback](#6-operations)
7. [Backup and restore](#7-backup-and-restore)
8. [Admin password rotation](#8-admin-password-rotation)
9. [Resource monitoring and tuning](#9-resource-monitoring-and-tuning)
10. [Troubleshooting](#10-troubleshooting)
11. [Security model and residual risks](#11-security-model-and-residual-risks)
12. [Configuration reference](#12-configuration-reference)
13. [Development and tests](#13-development-and-tests)
14. [Deployment acceptance checklist](#14-deployment-acceptance-checklist)

---

## 1. What has and has not been verified

Read this first. It is the honest state of the project.

**Verified by automated tests (177 tests, real Chromium, real Redis, real Celery worker):**

- Login, CSRF, rate limiting, session invalidation on password rotation, startup refusal on missing or placeholder secrets.
- SSRF policy: loopback, private, link-local, metadata endpoints, internal service names, numeric-encoded IPs, IPv4-mapped IPv6, bad schemes, credentials in URLs, DNS answers that resolve privately.
- The whole agent loop against a local test site: type, select, click, scroll, wait, extract, record, done; verification of each action; recovery from failures; action/time limits; cancellation at checkpoints; malformed LLM output; loop detection.
- Request interception blocking a redirect to a non-allowlisted host; the DNS-rebinding detector.
- Approval gate: a "Buy now" click pauses the task as `waiting_for_admin`, resumes on approval, and is not retried after denial.
- OCR coordinate mapping, including a screenshot that was downscaled before recognition (using a stub OCR backend).
- Groq error handling (429 with `Retry-After`, bounded backoff, quota-sized waits fail fast, 401, 5xx, JSON-mode failures) using simulated SDK errors.
- Full path `enqueue → Redis → Celery worker → engine → Chromium → MongoDB`, duplicate delivery safety, and fast failure when Redis is down.
- Results search/sort/pagination/export, including CSV formula-injection neutralisation, and artifact path-traversal protection.

**NOT verified. You must verify these on the VPS** (section 14):

| Not verified | Why | How you check |
|---|---|---|
| `docker compose build` / `up` | No Docker in the build sandbox | Section 3 |
| **PaddleOCR actually recognising text** | Needs the real package and models. Tests use a stub backend and sample PaddleOCR 2.x/3.x output shapes | `docker compose run --rm worker python scripts/check_ocr.py` |
| Live Groq calls with `openai/gpt-oss-120b`, JSON mode, real rate limits | Needs your API key | Run the "Run test task" button on a website |
| LLM quality: how well the model follows instructions and resists prompt injection | Not testable offline. The design contains the damage (fixed action set, domain policy, approval gate) but cannot guarantee good behaviour | Try your real tasks with small action budgets first |
| Chromium sandbox inside the container on your host kernel | Depends on host kernel and AppArmor settings | Worker logs on first task; see 10 |
| NPM routing, HTTPS, Hostinger specifics | Environment-specific | Section 4 |
| Egress firewall recipe (section 11) | Needs root on your host | Test it deliberately |

---

## 2. System requirements

- Hostinger VPS (or any Linux host) with **Docker Engine 24+** and **Docker Compose v2.20+** (`docker compose version`).
- **RAM: 4 GB minimum, 8 GB comfortable.** Chromium plus PaddleOCR is the big consumer; the worker is capped at 3 GB by default (`WORKER_MEM_LIMIT`). Add swap if you have less.
- ~6 GB free disk for images and models; more for screenshots (auto-pruned after `ARTIFACT_RETENTION_DAYS`).
- An existing **Redis** container and an existing **Nginx Proxy Manager (NPM)** container.
- A Groq API key and a DNS name for the dashboard (for example `pilot.example.com`).

---

## 3. First deployment

### 3.1 Clone and configure

```bash
git clone <YOUR_REPOSITORY_URL> webpilot-ai
cd webpilot-ai

cp .env.example .env
chmod 600 .env
nano .env
```

Generate secrets:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY, MONGO_ROOT_PASSWORD
```

The app **refuses to start** if `SECRET_KEY` is short or a placeholder, `ADMIN_PASSWORD` is missing, weak, or under 12 characters in production, or the Groq key is a placeholder. No default admin account is ever created.

### 3.2 Discover the existing Redis and NPM networks

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"
docker network ls
```

Find your Redis container and NPM container, then see which networks each is on:

```bash
docker inspect <redis_container> --format '{{json .NetworkSettings.Networks}}' | python3 -m json.tool
docker inspect <npm_container>   --format '{{json .NetworkSettings.Networks}}' | python3 -m json.tool
```

Set in `.env`:

- `REDIS_HOST`: the Redis container's name or network alias **on the network you choose**.
- `REDIS_NETWORK`: the Docker network Redis is on.
- `PROXY_NETWORK`: the Docker network NPM is on.
- `REDIS_PASSWORD`: if your Redis requires one.

**If NPM and Redis share one network**, use that name for both variables **and** delete the `redis` network entries in `compose.yaml` (the comment on `web`'s `networks:` line shows where). Attaching one container to the same network under two names can fail.

### 3.3 Pick a free Redis database

WebPilot uses one logical database (`REDIS_DB=4`). Confirm it is empty so you do not collide with another app:

```bash
docker exec <redis_container> redis-cli -n 4 dbsize      # add -a "$PASSWORD" if Redis needs auth (avoid shell history: use REDISCLI_AUTH)
```

A different logical DB separates keys but is **not** a security boundary. Anyone who can reach Redis can read every DB.

### 3.4 Check Redis connectivity (no password is printed)

```bash
docker compose run --rm --no-deps worker python - <<'PY'
import redis
from app.config import load_settings
s = load_settings()
r = redis.Redis(host=s.redis_host, port=s.redis_port, db=s.redis_db,
                password=s.redis_password or None, socket_connect_timeout=3)
print("PING ->", r.ping(), "| keys in db", s.redis_db, "->", r.dbsize())
PY
```

If this fails, fix the network or host before going further. Do not echo the password into your shell history.

### 3.5 Validate, build, start

```bash
docker compose config            # catches typos and unset variables
docker compose build             # slow the first time (Chromium + Paddle); watch for the OCR pre-download warning
docker compose up -d
docker compose ps                # all three should become "healthy"
docker compose logs --tail=200 web
docker compose logs --tail=200 worker
```

MongoDB initialises with your `MONGO_ROOT_*` credentials **only on first start**. Changing them later in `.env` does not change the existing database; see section 10.

### 3.5.1 Smoke tests inside the worker

```bash
docker compose run --rm worker python scripts/check_ocr.py      # OCR works, reports time and memory
docker compose exec worker python scripts/healthcheck.py worker && echo worker OK
```

---

## 4. Nginx Proxy Manager and HTTPS

In NPM: **Hosts → Proxy Hosts → Add Proxy Host**.

| Field | Value |
|---|---|
| Domain Names | your WebPilot subdomain |
| Scheme | `http` |
| Forward Hostname / IP | `webpilot-web` |
| Forward Port | `8000` |
| Block Common Exploits | on |
| Websockets Support | optional (the dashboard polls; it does not need WebSockets) |

**SSL tab:** request a Let's Encrypt certificate, enable **Force SSL** and **HTTP/2**. Production session cookies are `Secure`, so **login only works over HTTPS**; opening `http://<ip>:8000` directly will not hold a session (and port 8000 is deliberately not published).

If `webpilot-web` does not resolve from NPM, the two are on different networks. Check `PROXY_NETWORK` and that `docker inspect webpilot-web` lists that network. Never publish MongoDB (27017) or Redis ports to the internet.

`TRUSTED_PROXIES=1` makes the login rate limiter see the real client IP from NPM's `X-Forwarded-For`. If you put another proxy (e.g. Cloudflare) in front, raise it accordingly.

---

## 5. Using it

1. **Websites → Add website.** Give a name, start URL, saved instructions, allowed domains (the start host is always allowed; nothing else is), optional default parameters, OCR preference.
   Instructions are versioned; every task stores a snapshot of the version it ran with.
2. **Websites → More → Test connectivity** / **Run test task** (opens the site and reports the title/headings, 8 actions max).
3. **New Task:** choose the website, write instructions, optional inputs (`keywords: laptops`), set limits, **Start**. Use **Save as template** for reuse.
4. Watch it on **Running Tasks** or the task page: live action history, verification results, current URL, tokens used.
5. **Results:** searchable, sortable, paginated, exportable as CSV/JSON.

**Approvals.** If the agent is about to do something consequential (purchase/payment, deletion, sending or publishing, security settings, file upload, accepting legal terms, submitting a form containing password/payment fields), the task switches to `waiting_for_admin` and shows **Approve / Deny** on the task page. Denial is final for that action. Unanswered prompts time out after `ADMIN_WAIT_TIMEOUT_SECONDS` and the task fails. The agent can also use `ask_admin` to ask you a question.

**Stopping.** *Request stop* takes effect at the worker's next checkpoint (between actions, during LLM waits, while waiting for you), then the browser context is closed.

**Credentials.** This version has **no secure credential or session storage**. Parameters whose names look like secrets (`password`, `token`, `api_key`, ...) are rejected. Use it on public pages or pages that need no login.

Example task: *"Search for laptops in Canada under Electronics. Extract product name, displayed price and URL from the first 20 results."*

---

## 6. Operations

```bash
docker compose ps
docker compose logs -f                      # everything
docker compose logs -f web
docker compose logs -f worker
docker compose logs --tail=300 web worker
docker compose restart web
docker compose restart worker
docker inspect webpilot-web
docker inspect webpilot-worker
```

> `docker compose restart` does **not** re-read `.env`. After editing `.env`, run `docker compose up -d --force-recreate web worker`.

### Update after a `git pull`

```bash
cd /path/to/webpilot-ai
git status
git pull --ff-only
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=200 web worker
```

Indexes are created idempotently at start-up. There are no schema migrations in this version; if a later version adds any they will be listed in the release notes with when to run them. **Never** use `docker compose down -v` to update: `-v` deletes the database and artifact volumes.

Tasks that were running when the worker restarted are marked `failed` (`worker_restarted`) on worker boot, and a heartbeat reaper marks silently-dead tasks `failed` (`worker_lost`). They are not auto-retried because their actions may already have taken effect; use **Retry as new task** after checking.

### Rollback

```bash
git log --oneline -10
git checkout <previous_good_commit_or_tag>
docker compose build
docker compose up -d
```

The data model only gains fields, so rolling code back is safe. Take a backup before major updates (section 7).

---

## 7. Backup and restore

```bash
# MongoDB -> compressed archive (credentials come from the container's own env; nothing in shell history)
docker compose exec -T mongo sh -c \
  'mongodump -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --db "${MONGO_DATABASE:-webpilot}" --archive --gzip' \
  > webpilot-$(date +%F).archive.gz

# Screenshots / OCR JSON
docker run --rm -v webpilot_artifacts:/a -v "$PWD":/b alpine tar czf /b/artifacts-$(date +%F).tgz -C /a .
```

Restore (this **replaces** existing collections in that database):

```bash
docker compose exec -T mongo sh -c \
  'mongorestore -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip --drop' \
  < webpilot-YYYY-MM-DD.archive.gz
```

Test a restore on a scratch host before you rely on it. Backups contain extracted data and screenshots: store them as carefully as the data itself.

---

## 8. Admin password rotation

1. Edit `.env`: set a new `ADMIN_PASSWORD` (12+ characters).
2. `docker compose up -d --force-recreate web worker`

All existing sessions are invalidated automatically (the session carries a fingerprint of the credentials). The password is compared as a hash held in memory only.

To rotate `SECRET_KEY` the same way, change it and recreate; every session is signed out.

---

## 9. Resource monitoring and tuning

```bash
docker stats --no-stream
docker compose top
free -h && df -h
docker inspect webpilot-worker --format '{{.State.OOMKilled}} {{.RestartCount}}'
```

- `OOMKilled=true` means raise `WORKER_MEM_LIMIT` or lower concurrency.
- Start with `MAX_CONCURRENT_BROWSER_TASKS=1`. Raise it one step at a time while watching memory during a real task (OCR plus Chromium can reach 1.5-2.5 GB each).
- **Groq pacing is per worker process.** With concurrency N, each process sends up to `GROQ_REQUESTS_PER_MINUTE`, so set it to (your quota ÷ N).
- The worker recycles each child process after 25 tasks to cap Chromium/OCR memory growth.
- Screenshots are capped per task (`MAX_SCREENSHOTS_PER_TASK`) and pruned by age.

---

## 10. Troubleshooting

| Problem | Likely causes | Initial checks |
|---|---|---|
| Container exits immediately with `Invalid WebPilot AI configuration` | Placeholder or missing secret in `.env` | `docker compose logs web`: every problem is listed |
| Dashboard unavailable | Container stopped, NPM can't reach it, wrong forward host/port | `docker compose ps`, `docker compose logs web`, NPM target `webpilot-web:8000` |
| Login page loads, login "does nothing" | Using plain HTTP (Secure cookie) | Use the HTTPS NPM hostname |
| `docker compose config` fails on `PROXY_NETWORK` / `REDIS_NETWORK` | Variables unset | Set them in `.env` (section 3.2) |
| Worker not processing jobs; tasks stay `queued` | Redis unreachable, wrong `REDIS_HOST`/`REDIS_DB`/password, queue name mismatch | Section 3.4; `docker compose logs worker`; same `CELERY_QUEUE` on both services |
| Task fails instantly: *"Redis is unavailable"* | Same as above, seen by the web container | Settings page shows Redis status |
| Groq requests failing | Bad key, quota, rate limit, model not available to your account | Task event log: `llm_auth`, `llm_rate_limited`, `llm_throttled`; try another `GROQ_MODEL` |
| `invalid_llm_output` failures | Model ignores JSON mode | Set `GROQ_RESPONSE_FORMAT=none`, clear `GROQ_REASONING_EFFORT` for non-gpt-oss models, or use a stronger model |
| Browser will not start | Missing libs, sandbox failure, low memory | Worker logs: `Chromium failed to start...` |
| *"Chromium sandbox could not start"* | Host blocks unprivileged user namespaces (common on Ubuntu 24.04: `sysctl kernel.apparmor_restrict_unprivileged_userns`) | Prefer fixing the host. Last resort: `BROWSER_SANDBOX=false`, which weakens isolation (see 11) |
| OCR fails to initialise | Dependency conflict, models not downloaded, memory | `scripts/check_ocr.py`; rebuild looking for the pre-download warning; OCR failure degrades to DOM-only |
| MongoDB authentication fails | Credentials changed after first init | `MONGO_INITDB_*` apply only on first start. Either restore the old password or reset the volume (this deletes data: back up first) |
| Website cannot be reached | DNS/TLS/blocked by policy | Websites → Test connectivity; task log `policy_blocked` shows the exact rule |
| `domain_not_allowed` | Page links or redirects to another domain | Add that domain to the website's allowed domains if you trust it |
| NPM cannot reach the app | Different Docker networks, wrong port | `docker network inspect $PROXY_NETWORK` lists `webpilot-web` |
| Container restarts repeatedly | Startup exception, bad env | `docker compose logs --tail=200 <service>` |
| Task stays `running` forever | Worker died | Reaper marks it `failed (worker_lost)` after `STALE_TASK_SECONDS`; check OOM (section 9) |
| Task waits in `waiting_for_admin` | Awaiting you | Open the task page and respond |

---

## 11. Security model and residual risks

**Enforced in code**

- Admin only; every route and API is behind login; CSRF on all state-changing requests; login rate limiting (Redis-backed with in-memory fallback); `HttpOnly`, `Secure`, `SameSite=Lax` cookies; strict CSP (no inline scripts, no third-party hosts: Bootstrap and fonts are vendored); security headers; non-root containers; `no-new-privileges`; all capabilities dropped; MongoDB not reachable from the proxy or Redis networks.
- The LLM emits **one JSON action** from a closed set. Anything else is rejected and re-requested a bounded number of times. There is no code execution path. Keys are allowlisted (no shortcuts like Ctrl+…).
- Page text, OCR text and extracted data are labelled **untrusted** in every prompt; the fixed rules instruct the model to ignore instructions found there. This reduces but cannot eliminate prompt-injection risk, which is why the hard controls below exist.
- **Hard controls the model cannot talk its way past:** every document request (including redirects and iframes) must be on the website's allowlist; every request is checked for private/loopback/link-local/metadata destinations; each response's *actual* server IP is re-checked to detect DNS rebinding (the task is aborted if a non-public address was reached); downloads, permissions and service workers are disabled; one fresh cookie-less context per task; consequential actions need your approval.
- Secrets never go to logs: known secrets and credential patterns are scrubbed from log records and task logs; screenshots mask password fields; values typed into sensitive fields are not recorded.
- Scraped content is untrusted in exports too: CSV cells beginning with `=`, `+`, `-`, `@` are neutralised.

**Residual risks: be aware**

1. **DNS rebinding is detected, not prevented.** Chromium does its own DNS lookup after our check. We catch it when the response arrives and abort, but the first request may already have reached the internal host. **Close this at the network layer.**
2. **Chromium shares a container with the worker**, which can reach MongoDB and Redis at the TCP level. URL policy stops the browser from being *pointed* there, but a browser exploit would not respect it. Mitigations: keep the sandbox on, keep Chromium patched (rebuild regularly), and add host egress rules. A starting recipe (verify it on your host; replace addresses with real ones from `docker inspect`):
   ```bash
   # Worker may talk to Mongo + Redis + the public internet, but not to other private ranges.
   W=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' webpilot-worker)   # one IP per network
   for ip in $W; do
     sudo iptables -I DOCKER-USER -s $ip -d <MONGO_IP> -j ACCEPT
     sudo iptables -I DOCKER-USER -s $ip -d <REDIS_IP> -j ACCEPT
     sudo iptables -I DOCKER-USER -s $ip -d 10.0.0.0/8 -j DROP
     sudo iptables -I DOCKER-USER -s $ip -d 172.16.0.0/12 -j DROP
     sudo iptables -I DOCKER-USER -s $ip -d 192.168.0.0/16 -j DROP
     sudo iptables -I DOCKER-USER -s $ip -d 169.254.0.0/16 -j DROP
   done
   ```
   `-I` inserts at the top, so insert the DROPs first and the ACCEPTs last. Container IPs change on recreation: automate it or pin addresses. Also set Redis `requirepass`.
3. **Prompt injection cannot be fully solved.** A malicious page can still mislead the model within what the hard controls permit, e.g. extract the wrong data. Review results from untrusted sites.
4. **Approval gating is heuristic** (text patterns, sensitive-form detection). It will miss unusual wording. Do not point this at sites where one mistaken click is costly or irreversible.
5. `BROWSER_SANDBOX=false` removes Chromium's sandbox. Do not use it on untrusted sites.
6. Single admin, no audit trail beyond task logs, no secure credential vault.
7. Groq receives page text, OCR text and your instructions. Do not automate pages containing data you cannot send to a third-party LLM.

Seccomp profile `docker/seccomp_chromium.json` is Microsoft's Playwright profile (Apache-2.0), used so Chromium's sandbox works without `--no-sandbox`.

---

## 12. Configuration reference

All settings are environment variables, validated at start-up (every problem is reported at once).

| Variable | Purpose | Default |
|---|---|---|
| `APP_ENV` | `production` / `development` / `testing` | `production` |
| `SECRET_KEY` | Flask session signing (≥32 chars) | required |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Admin login (password ≥12 chars in production) | required |
| `MONGO_ROOT_USERNAME`, `MONGO_ROOT_PASSWORD` | Mongo init user | required unless `MONGO_URI` |
| `MONGO_DATABASE` | Database name | `webpilot` |
| `MONGO_URI` | Optional full URI (overrides the above for the app) | built from parts |
| `MONGO_HOST` | Host for the built URI | `webpilot-mongo` |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD` | Existing Redis | host required; `6379`, `4` |
| `CELERY_QUEUE` | Dedicated queue | `webpilot` |
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq credential and model | model `openai/gpt-oss-120b` |
| `GROQ_REQUESTS_PER_MINUTE` | Client-side pacing per worker process | `20` |
| `GROQ_RESPONSE_FORMAT` | `json_object` / `json_schema` / `none` | `json_object` |
| `GROQ_REASONING_EFFORT` | `low`/`medium`/`high` for gpt-oss, empty otherwise | empty |
| `GROQ_MAX_COMPLETION_TOKENS` | Includes reasoning tokens | `2048` |
| `APP_PORT` | Internal web port | `8000` |
| `MAX_ACTIONS_PER_TASK` | Upper bound for any task | `100` |
| `MAX_TASK_DURATION_SECONDS` | Upper bound; approval wait time is excluded | `900` |
| `MAX_RECOVERY_ATTEMPTS` | Consecutive failed attempts before the task fails | `3` |
| `MAX_LLM_RETRIES` | Transport retries and malformed-output retries | `3` |
| `MAX_CONCURRENT_BROWSER_TASKS` | Celery concurrency | `1` |
| `BROWSER_NAVIGATION_TIMEOUT_MS` | Navigation timeout | `30000` |
| `BROWSER_SANDBOX` | Chromium sandbox | `true` |
| `VIEWPORT_WIDTH`, `VIEWPORT_HEIGHT` | Browser viewport | `1366`×`768` |
| `OCR_LANG`, `OCR_MAX_DIMENSION`, `OCR_MIN_CONFIDENCE` | OCR language, resize cap, match threshold | `en`, `1600`, `0.6` |
| `MAX_SCREENSHOTS_PER_TASK` | Evidence screenshot cap | `40` |
| `ARTIFACT_DIR`, `ARTIFACT_RETENTION_DAYS` | Artifact path and retention | `/app/artifacts`, `14` |
| `ADMIN_WAIT_TIMEOUT_SECONDS` | How long to wait for your approval | `600` |
| `STALE_TASK_SECONDS` | Heartbeat age before a task is declared lost | `300` |
| `LOGIN_RATE_LIMIT` | Flask-Limiter syntax | `5 per minute;30 per hour` |
| `TRUSTED_PROXIES` | Proxy hops to trust for client IP | `1` |
| `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |
| `ALLOW_PRIVATE_NETWORKS` | Dev/tests only; **startup fails if set in production** | `false` |
| `PROXY_NETWORK`, `REDIS_NETWORK` | Existing Docker networks | required by Compose |
| `WORKER_MEM_LIMIT`, `WORKER_CPUS` | Worker container caps | `3g`, `2` |

**OCR modes** (per website): *automatic*: OCR runs only when needed (target not found in the DOM, an action failed, or the page is nearly empty); *enabled*: also on the first page and when the model requests it; *disabled*: never. OCR runs locally; screenshots are never sent to Groq. Without a vision model, OCR gives text and positions only; it cannot tell what a control *is*.

---

## 13. Development and tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # core + pytest; deliberately excludes the heavy OCR packages
playwright install chromium
python -m pytest tests -q                    # browser tests skip if Chromium is missing; the Celery test skips without `redis-server`
```

Layout: `app/agent/` (engine, browser, observer, ocr, llm, actions, verification, policies) is independent of Flask/Mongo and driven through `AgentHooks`; `app/services/` holds persistence; `app/tasks/jobs.py` is the Celery glue; `tests/site/` is the local site the agent tests drive.

Adding a vision model later means giving `Observer` another source of observations and `llm.py` another client; the action protocol and policies do not change.

---

## 14. Deployment acceptance checklist

Passing local unit tests does not satisfy acceptance. On the VPS:

- [ ] `docker compose ps` shows `web`, `worker`, `mongo` healthy.
- [ ] `docker compose ps` / `docker ps` shows **no** Redis container created by this project; section 3.4 proved the existing Redis is used (`dbsize` rises while a task runs).
- [ ] Recreate containers (`docker compose up -d --force-recreate`): websites, tasks and results are still there.
- [ ] NPM serves the dashboard over HTTPS; login works; `http://` redirects to `https://`.
- [ ] **Websites → Run test task** completes (proves Groq + Chromium + Redis + Mongo).
- [ ] `docker compose run --rm worker python scripts/check_ocr.py` prints `text found: True`.
- [ ] A real task from **New Task** completes and results appear under **Results**.
- [ ] Trigger an approval (a page with a "Buy"/"Delete" button on a *harmless* test site) and confirm it pauses.
- [ ] Add `http://localhost` / a private IP as a website: it must be rejected.
- [ ] `docker compose logs web worker` show no secrets (`grep -i gsk_ ...` returns nothing).
- [ ] `git pull` + rebuild + `up -d` keeps all data.
- [ ] Backup and restore tried once (section 7).
