"""Website configurations, prompt revisions, and validation."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import requests
from bson import ObjectId
from bson.errors import InvalidId

from ..agent.policies import (PolicyViolation, effective_allowed_domains, parse_domain_list, validate_url)
from ..extensions import get_db, get_settings
from ..utils import looks_sensitive, utcnow

OCR_CHOICES = ("automatic", "enabled", "disabled")


def oid(value) -> ObjectId | None:
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def parse_params(text: str | None) -> dict:
    """Task/default parameters: JSON object, or 'key: value' / 'key=value' lines. Credentials are refused."""
    text = (text or "").strip()
    if not text:
        return {}
    params: dict = {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Parameters are not valid JSON: {exc.msg}")
        if not isinstance(obj, dict):
            raise ValueError("Parameters must be a JSON object.")
        params = obj
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([^:=]{1,64})\s*[:=]\s*(.*)$", line)
            if not m:
                raise ValueError(f"Could not read parameter line: {line[:60]!r} (use key: value).")
            params[m.group(1).strip()] = m.group(2).strip()
    if len(params) > 50:
        raise ValueError("At most 50 parameters are allowed.")
    clean = {}
    for k, v in params.items():
        k = str(k)[:64]
        if looks_sensitive(k):
            raise ValueError(f"Parameter {k!r} looks like a credential. This version has no secure credential storage; "
                             "do not put passwords, tokens or card numbers in parameters.")
        clean[k] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:1000]
    return clean


def validate_form(form: dict) -> tuple[dict, dict]:
    s = get_settings()
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()
    if not 2 <= len(name) <= 100:
        errors["name"] = "Enter a name between 2 and 100 characters."
    url = (form.get("start_url") or "").strip()
    try:
        url = validate_url(url, None, allow_private=s.allow_private_networks, resolve=True, enforce_allowlist=False)
    except PolicyViolation as exc:
        errors["start_url"] = exc.message
    instructions = (form.get("instructions") or "").strip()
    if len(instructions) > 8000:
        errors["instructions"] = "Instructions are limited to 8,000 characters."
    domains: list[str] = []
    try:
        domains = parse_domain_list(form.get("allowed_domains") or "")
    except PolicyViolation as exc:
        errors["allowed_domains"] = exc.message
    params: dict = {}
    try:
        params = parse_params(form.get("default_params"))
    except ValueError as exc:
        errors["default_params"] = str(exc)
    ocr = form.get("ocr_preference", "automatic")
    if ocr not in OCR_CHOICES:
        errors["ocr_preference"] = "Choose automatic, enabled or disabled."
    desc = (form.get("description") or "").strip()[:500]
    clean = {"name": name, "start_url": url, "description": desc, "instructions": instructions,
             "allowed_domains": domains, "default_params": params, "ocr_preference": ocr,
             "is_active": bool(form.get("is_active"))}
    return clean, errors


def create(data: dict) -> ObjectId:
    now = utcnow()
    doc = {**data, "instructions_version": 1, "created_at": now, "updated_at": now}
    wid = get_db().websites.insert_one(doc).inserted_id
    _revision(wid, 1, data["instructions"])
    return wid


def _revision(wid, version: int, instructions: str) -> None:
    get_db().website_revisions.insert_one({"website_id": wid, "version": version, "instructions": instructions,
                                           "changed_at": utcnow()})


def update(wid, data: dict) -> None:
    db = get_db()
    old = db.websites.find_one({"_id": wid})
    if not old:
        raise KeyError("website not found")
    version = old.get("instructions_version", 1)
    if data["instructions"] != old.get("instructions", ""):
        version += 1
        _revision(wid, version, data["instructions"])
    db.websites.update_one({"_id": wid}, {"$set": {**data, "instructions_version": version, "updated_at": utcnow()}})


def get(wid) -> dict | None:
    o = oid(wid)
    return get_db().websites.find_one({"_id": o}) if o else None


def list_all(active_only: bool = False) -> list[dict]:
    q = {"is_active": True} if active_only else {}
    return list(get_db().websites.find(q).sort("name", 1))


def set_active(wid, active: bool) -> None:
    get_db().websites.update_one({"_id": wid}, {"$set": {"is_active": active, "updated_at": utcnow()}})


def delete(wid) -> None:
    """Deletes the configuration only; historical tasks keep their own snapshot."""
    db = get_db()
    db.websites.delete_one({"_id": wid})
    db.website_revisions.delete_many({"website_id": wid})


def revisions(wid, limit: int = 20) -> list[dict]:
    return list(get_db().website_revisions.find({"website_id": wid}).sort("version", -1).limit(limit))


def snapshot(site: dict) -> dict:
    """Frozen copy stored on every task so old runs stay reproducible."""
    return {"website_id": str(site["_id"]), "name": site["name"], "start_url": site["start_url"],
            "instructions": site.get("instructions", ""), "instructions_version": site.get("instructions_version", 1),
            "allowed_domains": effective_allowed_domains(site["start_url"], site.get("allowed_domains", [])),
            "default_params": site.get("default_params", {}), "ocr_preference": site.get("ocr_preference", "automatic")}


def test_connectivity(site: dict, timeout: float = 8.0) -> dict:
    """Lightweight reachability probe from the web container (validates every redirect hop)."""
    s = get_settings()
    allowed = effective_allowed_domains(site["start_url"], site.get("allowed_domains", []))
    url, hops = site["start_url"], []
    try:
        for _ in range(5):
            validate_url(url, allowed, allow_private=s.allow_private_networks)
            r = requests.get(url, timeout=timeout, allow_redirects=False, stream=True,
                             headers={"User-Agent": "WebPilot-connectivity-check/1.0"})
            r.close()
            hops.append(f"{r.status_code} {url}")
            if r.is_redirect and r.headers.get("location"):
                from urllib.parse import urljoin
                url = urljoin(url, r.headers["location"])
                continue
            ok = r.status_code < 400
            return {"ok": ok, "status": r.status_code, "message": f"HTTP {r.status_code}" + ("" if ok else " (the site answered with an error)"), "hops": hops}
        return {"ok": False, "message": "Too many redirects.", "hops": hops}
    except PolicyViolation as exc:
        return {"ok": False, "message": f"Blocked by policy: {exc.message}", "hops": hops}
    except requests.RequestException as exc:
        return {"ok": False, "message": f"Could not connect: {exc.__class__.__name__}", "hops": hops}
