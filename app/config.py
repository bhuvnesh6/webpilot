"""Environment-driven configuration with strict startup validation."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote_plus


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or unsafe."""


WEAK_PASSWORDS = {"admin", "password", "123456", "12345678", "letmein", "qwerty", "changeme"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    secret_key: str
    admin_username: str
    admin_password: str
    mongo_uri: str
    mongo_database: str
    redis_host: str
    redis_port: int
    redis_db: int
    redis_password: str
    celery_queue: str
    groq_api_key: str
    groq_model: str
    groq_requests_per_minute: int
    groq_response_format: str
    groq_reasoning_effort: str
    groq_max_completion_tokens: int
    app_port: int
    max_actions_per_task: int
    max_task_duration_seconds: int
    max_recovery_attempts: int
    max_llm_retries: int
    max_concurrent_browser_tasks: int
    browser_navigation_timeout_ms: int
    browser_sandbox: bool
    viewport_width: int
    viewport_height: int
    artifact_dir: str
    artifact_retention_days: int
    log_level: str
    allow_private_networks: bool
    trusted_proxies: int
    secure_cookies: bool
    stale_task_seconds: int
    admin_wait_timeout_seconds: int
    login_rate_limit: str
    ocr_lang: str
    ocr_max_dimension: int
    ocr_min_confidence: float
    max_screenshots_per_task: int

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.groq_api_key, self.admin_password, self.secret_key,
                                 self.redis_password, self.mongo_uri) if s)

    def public_summary(self) -> dict:
        """Safe-to-display configuration (no secrets)."""
        return {
            "Environment": self.app_env,
            "Groq model": self.groq_model,
            "Groq requests / minute": self.groq_requests_per_minute,
            "Groq response format": self.groq_response_format,
            "Redis": f"{self.redis_host}:{self.redis_port} (db {self.redis_db})",
            "Celery queue": self.celery_queue,
            "Max actions / task": self.max_actions_per_task,
            "Max task duration (s)": self.max_task_duration_seconds,
            "Max recovery attempts": self.max_recovery_attempts,
            "Max LLM retries": self.max_llm_retries,
            "Concurrent browser tasks": self.max_concurrent_browser_tasks,
            "Navigation timeout (ms)": self.browser_navigation_timeout_ms,
            "Browser sandbox": "on" if self.browser_sandbox else "off",
            "Viewport": f"{self.viewport_width}x{self.viewport_height}",
            "Artifact retention (days)": self.artifact_retention_days,
            "Secure (HTTPS-only) cookies": "on" if self.secure_cookies else "OFF (plain HTTP access)",
            "Private networks allowed": "yes (development only)" if self.allow_private_networks else "no",
        }


def _int(env: Mapping[str, str], key: str, default: int, errors: list[str], lo: int = 0, hi: int = 10**9) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        errors.append(f"{key} must be an integer (got {raw!r}).")
        return default
    if not lo <= val <= hi:
        errors.append(f"{key} must be between {lo} and {hi} (got {val}).")
    return val


def _float(env: Mapping[str, str], key: str, default: float, errors: list[str], lo: float, hi: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        errors.append(f"{key} must be a number (got {raw!r}).")
        return default
    if not lo <= val <= hi:
        errors.append(f"{key} must be between {lo} and {hi}.")
    return val


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _looks_placeholder(value: str) -> bool:
    low = value.strip().lower()
    return (
        low in WEAK_PASSWORDS
        or any(low.startswith(p) for p in ("replace_with", "changeme", "change_me", "your_", "<"))
        or "replace_with" in low
    )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate configuration. Raises ConfigError listing *every* problem."""
    env = os.environ if env is None else env
    errors: list[str] = []

    app_env = env.get("APP_ENV", "production").strip().lower()
    if app_env not in {"production", "development", "testing"}:
        errors.append("APP_ENV must be one of: production, development, testing.")
    prod = app_env == "production"

    secret_key = env.get("SECRET_KEY", "")
    if not secret_key:
        errors.append("SECRET_KEY is required.")
    elif len(secret_key) < 32:
        errors.append("SECRET_KEY must be at least 32 characters (try: python -c \"import secrets; print(secrets.token_urlsafe(48))\").")
    elif _looks_placeholder(secret_key):
        errors.append("SECRET_KEY is still a placeholder value.")

    admin_username = env.get("ADMIN_USERNAME", "").strip()
    admin_password = env.get("ADMIN_PASSWORD", "")
    if not admin_username:
        errors.append("ADMIN_USERNAME is required.")
    if not admin_password:
        errors.append("ADMIN_PASSWORD is required (no default admin account is ever created).")
    elif _looks_placeholder(admin_password):
        errors.append("ADMIN_PASSWORD is a placeholder or a well-known weak password.")
    elif prod and len(admin_password) < 12:
        errors.append("ADMIN_PASSWORD must be at least 12 characters in production.")

    mongo_database = env.get("MONGO_DATABASE", "webpilot").strip() or "webpilot"
    mongo_uri = env.get("MONGO_URI", "").strip()
    if not mongo_uri:
        user, pw = env.get("MONGO_ROOT_USERNAME", ""), env.get("MONGO_ROOT_PASSWORD", "")
        host = env.get("MONGO_HOST", "webpilot-mongo").strip()
        port = env.get("MONGO_PORT", "27017").strip()
        if not user or not pw:
            errors.append("Set MONGO_URI, or both MONGO_ROOT_USERNAME and MONGO_ROOT_PASSWORD.")
        elif _looks_placeholder(pw):
            errors.append("MONGO_ROOT_PASSWORD is still a placeholder value.")
        else:
            mongo_uri = f"mongodb://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/?authSource=admin"

    redis_host = env.get("REDIS_HOST", "").strip()
    if not redis_host:
        errors.append("REDIS_HOST is required (hostname/alias of your existing Redis container).")

    groq_key = env.get("GROQ_API_KEY", "").strip()
    if not groq_key:
        errors.append("GROQ_API_KEY is required.")
    elif _looks_placeholder(groq_key):
        errors.append("GROQ_API_KEY is still a placeholder value.")
    groq_model = env.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    if not groq_model:
        errors.append("GROQ_MODEL must not be empty.")
    response_format = env.get("GROQ_RESPONSE_FORMAT", "json_object").strip().lower()
    if response_format not in {"json_object", "json_schema", "none"}:
        errors.append("GROQ_RESPONSE_FORMAT must be json_object, json_schema or none.")
    effort = env.get("GROQ_REASONING_EFFORT", "").strip().lower()
    if effort not in {"", "low", "medium", "high"}:
        errors.append("GROQ_REASONING_EFFORT must be empty, low, medium or high.")

    allow_private = _bool(env, "ALLOW_PRIVATE_NETWORKS", False)
    if allow_private and prod:
        errors.append("ALLOW_PRIVATE_NETWORKS may not be enabled when APP_ENV=production.")

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.")

    settings = Settings(
        app_env=app_env,
        secret_key=secret_key,
        admin_username=admin_username,
        admin_password=admin_password,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        redis_host=redis_host,
        redis_port=_int(env, "REDIS_PORT", 6379, errors, 1, 65535),
        redis_db=_int(env, "REDIS_DB", 4, errors, 0, 255),
        redis_password=env.get("REDIS_PASSWORD", ""),
        celery_queue=env.get("CELERY_QUEUE", "webpilot").strip() or "webpilot",
        groq_api_key=groq_key,
        groq_model=groq_model,
        groq_requests_per_minute=_int(env, "GROQ_REQUESTS_PER_MINUTE", 20, errors, 1, 10000),
        groq_response_format=response_format,
        groq_reasoning_effort=effort,
        groq_max_completion_tokens=_int(env, "GROQ_MAX_COMPLETION_TOKENS", 2048, errors, 128, 32768),
        app_port=_int(env, "APP_PORT", 8000, errors, 1, 65535),
        max_actions_per_task=_int(env, "MAX_ACTIONS_PER_TASK", 100, errors, 1, 1000),
        max_task_duration_seconds=_int(env, "MAX_TASK_DURATION_SECONDS", 900, errors, 30, 86400),
        max_recovery_attempts=_int(env, "MAX_RECOVERY_ATTEMPTS", 3, errors, 0, 20),
        max_llm_retries=_int(env, "MAX_LLM_RETRIES", 3, errors, 0, 10),
        max_concurrent_browser_tasks=_int(env, "MAX_CONCURRENT_BROWSER_TASKS", 1, errors, 1, 16),
        browser_navigation_timeout_ms=_int(env, "BROWSER_NAVIGATION_TIMEOUT_MS", 30000, errors, 1000, 300000),
        browser_sandbox=_bool(env, "BROWSER_SANDBOX", True),
        viewport_width=_int(env, "VIEWPORT_WIDTH", 1366, errors, 320, 3840),
        viewport_height=_int(env, "VIEWPORT_HEIGHT", 768, errors, 240, 2160),
        artifact_dir=env.get("ARTIFACT_DIR", "/app/artifacts").strip(),
        artifact_retention_days=_int(env, "ARTIFACT_RETENTION_DAYS", 14, errors, 1, 3650),
        log_level=log_level,
        allow_private_networks=allow_private,
        trusted_proxies=_int(env, "TRUSTED_PROXIES", 0, errors, 0, 5),
        secure_cookies=_bool(env, "SECURE_COOKIES", app_env == "production"),
        stale_task_seconds=_int(env, "STALE_TASK_SECONDS", 300, errors, 30, 86400),
        admin_wait_timeout_seconds=_int(env, "ADMIN_WAIT_TIMEOUT_SECONDS", 600, errors, 30, 86400),
        login_rate_limit=env.get("LOGIN_RATE_LIMIT", "5 per minute;30 per hour").strip(),
        ocr_lang=env.get("OCR_LANG", "en").strip() or "en",
        ocr_max_dimension=_int(env, "OCR_MAX_DIMENSION", 1600, errors, 320, 4096),
        ocr_min_confidence=_float(env, "OCR_MIN_CONFIDENCE", 0.6, errors, 0.0, 1.0),
        max_screenshots_per_task=_int(env, "MAX_SCREENSHOTS_PER_TASK", 40, errors, 0, 500),
    )
    if errors:
        bullet = "\n  - ".join(errors)
        raise ConfigError(f"Invalid WebPilot AI configuration:\n  - {bullet}")
    return settings
