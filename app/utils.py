"""Small shared helpers (time, redaction)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """Mongo (and mongomock) may hand back naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_URI_CREDS = re.compile(r"(?i)\b(mongodb(?:\+srv)?|rediss?)://[^\s:@/]*:[^\s@/]+@")
_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),                                   # Groq API keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),                      # bearer tokens
    re.compile(r"(?i)\b(?:authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+"),
]


def redact(text: str | None, secrets: Iterable[str] = ()) -> str:
    """Scrub anything that looks like a credential from text bound for logs/DB."""
    if not text:
        return text or ""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[REDACTED]")
    out = _URI_CREDS.sub(lambda m: f"{m.group(1)}://[REDACTED]@", out)
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


SENSITIVE_NAME_RE = re.compile(
    r"pass(word|wd|code)?|secret|token|api[_\- ]?key|credential|cvv|cvc|card[_\- ]?(number|no)|ssn|otp|pin\b",
    re.I,
)


def looks_sensitive(name: str | None) -> bool:
    return bool(name and SENSITIVE_NAME_RE.search(name))
