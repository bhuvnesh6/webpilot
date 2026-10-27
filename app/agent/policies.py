"""Security policy: URL/SSRF validation, domain allowlists, risky-action classification."""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

ALLOWED_SCHEMES = {"http", "https"}
MAX_URL_LENGTH = 2048

BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost",
    "metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal",
    "kubernetes.default", "host.docker.internal", "gateway.docker.internal",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain", ".intranet", ".corp")
METADATA_IPS = {
    ipaddress.ip_address(x)
    for x in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254", "192.0.0.192")
}


class PolicyViolation(Exception):
    """A navigation or action was blocked by the security policy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- IP / DNS checks

def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%")[0].strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
    if ip in METADATA_IPS:
        return False
    return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_loopback
                and not ip.is_link_local and not ip.is_private and not ip.is_unspecified)


_dns_cache: dict[str, tuple[float, list[str]]] = {}
_dns_lock = threading.Lock()
DNS_CACHE_TTL = 10.0


def resolve_host(host: str, port: int = 443) -> list[str]:
    """Resolve host -> IP list (short-lived cache). Raises PolicyViolation on failure."""
    now = time.monotonic()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and now - hit[0] < DNS_CACHE_TTL:
            return hit[1]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PolicyViolation("dns_failure", f"Could not resolve host {host!r}: {exc}") from exc
    ips = sorted({i[4][0] for i in infos})
    with _dns_lock:
        _dns_cache[host] = (now, ips)
        if len(_dns_cache) > 2048:
            _dns_cache.clear()
    return ips


# ---------------------------------------------------------------- domains

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9\-_]{0,61}[a-z0-9_])?)(\.[a-z0-9_]([a-z0-9\-_]{0,61}[a-z0-9_])?)*$")


def normalize_host(host: str) -> str:
    host = host.strip().strip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise PolicyViolation("bad_host", f"Invalid hostname {host!r}.")


def parse_domain_list(text: str | Iterable[str] | None) -> list[str]:
    """Parse user input ('a.com, *.b.com' / newline separated) into validated domains."""
    if text is None:
        return []
    items = re.split(r"[,\s]+", text) if isinstance(text, str) else list(text)
    out: list[str] = []
    for raw in items:
        raw = (raw or "").strip()
        if not raw:
            continue
        raw = re.sub(r"^https?://", "", raw, flags=re.I).split("/")[0].split(":")[0]
        raw = raw.lstrip("*").lstrip(".")
        host = normalize_host(raw)
        if not _HOST_RE.match(host) or "." not in host:
            raise PolicyViolation("bad_domain", f"{raw!r} is not a valid domain.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise PolicyViolation("bad_domain", "Use domain names, not IP addresses, in the allowlist.")
        if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
            raise PolicyViolation("bad_domain", f"{host!r} is an internal-only name and cannot be allowed.")
        if host not in out:
            out.append(host)
    return out


def domain_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower().strip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


# ---------------------------------------------------------------- URL validation

def validate_url(
    url: str,
    allowed_domains: Iterable[str] | None = None,
    *,
    allow_private: bool = False,
    resolve: bool = True,
    enforce_allowlist: bool = True,
) -> str:
    """Validate a URL against the SSRF + domain policy. Returns the normalized URL."""
    if not isinstance(url, str) or not url.strip():
        raise PolicyViolation("bad_url", "URL is empty.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise PolicyViolation("bad_url", "URL is too long.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise PolicyViolation("bad_url", "URL could not be parsed.")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise PolicyViolation("bad_scheme", f"Scheme {parts.scheme!r} is not allowed; only http and https.")
    if parts.username or parts.password:
        raise PolicyViolation("bad_url", "URLs containing credentials are not allowed.")
    if not parts.hostname:
        raise PolicyViolation("bad_url", "URL has no hostname.")
    host = normalize_host(parts.hostname)

    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # Not a dotted/colon literal. Numeric shorthands (http://2130706433, 0x7f.1) are caught
        # below because they resolve to loopback and the resolved address is checked.
        pass

    if not allow_private:
        if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
            raise PolicyViolation("blocked_host", f"Host {host!r} is internal and blocked.")
        if literal_ip is None and "." not in host:
            raise PolicyViolation("blocked_host", f"Single-label host {host!r} looks like an internal service name.")
        if literal_ip is not None and not is_public_ip(str(literal_ip)):
            raise PolicyViolation("blocked_ip", f"Address {literal_ip} is private, loopback, link-local or a metadata endpoint.")

    if enforce_allowlist and allowed_domains is not None:
        allowed = list(allowed_domains)
        if literal_ip is not None and not allow_private:
            raise PolicyViolation("domain_not_allowed", "Direct IP navigation is not allowed.")
        if literal_ip is not None and allow_private:
            if str(literal_ip) not in allowed:
                raise PolicyViolation("domain_not_allowed", f"Address {literal_ip} is not in the allowed domains.")
        elif not domain_allowed(host, allowed):
            raise PolicyViolation("domain_not_allowed", f"Domain {host!r} is not in the allowed domains for this website.")

    if resolve and literal_ip is None and not allow_private:
        ips = resolve_host(host, port or (443 if parts.scheme.lower() == "https" else 80))
        bad = [ip for ip in ips if not is_public_ip(ip)]
        if bad:
            raise PolicyViolation("blocked_ip", f"Host {host!r} resolves to a non-public address ({bad[0]}).")
    return url


def resolve_relative(base: str, url: str) -> str:
    return urljoin(base, url) if base else url


def effective_allowed_domains(start_url: str, configured: Iterable[str]) -> list[str]:
    """The start URL's own host is always allowed; configured domains extend it."""
    host = normalize_host(urlsplit(start_url).hostname or "")
    items = list(configured or [])
    if host:
        items.append(host)
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------- keys

ALLOWED_KEYS = {
    "enter": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "arrowdown": "ArrowDown", "arrowup": "ArrowUp", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pagedown": "PageDown", "pageup": "PageUp", "home": "Home", "end": "End",
    "space": "Space", "backspace": "Backspace",
}


def normalize_key(key: str) -> str:
    k = ALLOWED_KEYS.get((key or "").strip().lower())
    if not k:
        raise PolicyViolation("key_not_allowed", f"Key {key!r} is not permitted. Allowed: {', '.join(sorted(set(ALLOWED_KEYS.values())))}.")
    return k


# ---------------------------------------------------------------- risky (consequential) actions

_RISK_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(buy|purchase|check\s?out|place\s+(?:your\s+)?order|pay(?:\s+now)?|complete\s+(?:order|purchase)|"
                r"confirm\s+(?:order|purchase|payment)|subscribe|donate|add\s+payment)\b", re.I), "a purchase or payment"),
    (re.compile(r"\b(delete|remove\s+(?:account|all)|erase|destroy|close\s+account|deactivate|terminate|cancel\s+(?:subscription|account))\b", re.I),
     "deleting data"),
    (re.compile(r"\b(send(?:\s+(?:message|email|mail|reply|invite|request))?|post(?:\s+(?:comment|reply|message))?|publish|tweet|share|reply)\b", re.I),
     "sending or publishing content"),
    (re.compile(r"\b(?:change|update|reset|set)\s+(?:your\s+)?(?:password|email|security|2fa|two[- ]factor|recovery)", re.I),
     "changing account security settings"),
    (re.compile(r"\b(upload|attach\s+file|choose\s+file)\b", re.I), "uploading files"),
    (re.compile(r"\b(i\s+agree|accept\s+(?:all\s+)?(?:the\s+)?(?:terms|agreement|conditions|policy)|agree\s+(?:and|&)\s+continue)\b", re.I),
     "accepting a legal agreement"),
]


def classify_risk(action: str, descriptors: Iterable[str | None], element_info: dict | None = None) -> str | None:
    """Return a human-readable reason if the action is consequential and needs admin approval."""
    if action not in {"click", "press_key"}:
        return None
    info = element_info or {}
    text = " ".join(d for d in descriptors if d)
    for pattern, reason in _RISK_RULES:
        if pattern.search(text):
            return reason
    if info.get("type") == "file":
        return "uploading files"
    if info.get("form_sensitive"):
        is_submit = (
            info.get("type") in {"submit", "image"}
            or (info.get("tag") == "button" and info.get("type") in (None, "", "submit"))
            or (action == "press_key" and info.get("tag") in {"input", "textarea"})
        )
        if is_submit:
            return "submitting a form that contains sensitive fields (password / payment details)"
    return None


# ---------------------------------------------------------------- navigation policy object

@dataclass(frozen=True)
class NavPolicy:
    allowed_domains: tuple[str, ...]
    allow_private: bool = False

    def check_navigation(self, url: str, *, resolve: bool = True) -> str:
        return validate_url(url, self.allowed_domains, allow_private=self.allow_private, resolve=resolve)

    def check_subresource(self, url: str) -> str:
        return validate_url(url, None, allow_private=self.allow_private, resolve=True, enforce_allowlist=False)

    def link_permitted(self, url: str) -> bool:
        try:
            validate_url(url, self.allowed_domains, allow_private=self.allow_private, resolve=False)
            return True
        except PolicyViolation:
            return False
