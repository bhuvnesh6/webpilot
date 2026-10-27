"""Structured extraction results (task_results)."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Iterator

from bson import ObjectId  # noqa: F401  (re-exported convenience)

from ..extensions import get_db
from ..utils import utcnow

KEY_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")
EXPORT_LIMIT = 20000


def _search_text(data: dict, source_url: str) -> str:
    return (" ".join(str(v) for v in data.values() if v is not None) + " " + source_url).lower()[:4000]


def save_many(task_id: str, website_id: str, rows: list[dict], source_url: str) -> int:
    """Insert extracted rows. Raises on persistence failure so the caller never reports a false success."""
    if not rows:
        return 0
    now = utcnow()
    docs = [{"task_id": task_id, "website_id": website_id, "data": r, "source_url": source_url,
             "extracted_at": now, "search_text": _search_text(r, source_url)} for r in rows]
    get_db().task_results.insert_many(docs)
    return len(docs)


def _query(search: str | None, task_id: str | None, website_id: str | None) -> dict:
    q: dict = {}
    if task_id:
        q["task_id"] = task_id
    if website_id:
        q["website_id"] = website_id
    if search:
        q["search_text"] = {"$regex": re.escape(search.lower()[:100])}
    return q


def query(search=None, sort="extracted_at", direction="desc", page=1, per_page=25, task_id=None, website_id=None):
    q = _query(search, task_id, website_id)
    col = get_db().task_results
    sort_field = "extracted_at"
    if sort in ("source_url", "extracted_at", "task_id"):
        sort_field = sort
    elif sort and sort.startswith("data.") and KEY_RE.match(sort[5:]):
        sort_field = sort
    total = col.count_documents(q)
    page = max(1, page)
    cur = col.find(q, {"search_text": 0}).sort([(sort_field, 1 if direction == "asc" else -1), ("_id", -1)]) \
        .skip((page - 1) * per_page).limit(per_page)
    return list(cur), total


def columns_for(docs: list[dict], max_cols: int = 8) -> list[str]:
    freq: dict[str, int] = {}
    for d in docs:
        for k in d.get("data", {}):
            freq[k] = freq.get(k, 0) + 1
    return [k for k, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))][:max_cols]


def count_for_task(task_id: str) -> int:
    return get_db().task_results.count_documents({"task_id": task_id})


def for_task(task_id: str, limit: int = 500) -> list[dict]:
    return list(get_db().task_results.find({"task_id": task_id}, {"search_text": 0}).sort("_id", 1).limit(limit))


def _iter_docs(search, task_id, website_id) -> Iterator[dict]:
    yield from get_db().task_results.find(_query(search, task_id, website_id), {"search_text": 0}).sort("extracted_at", -1).limit(EXPORT_LIMIT)


def _csv_safe(v) -> str:
    """Scraped content is untrusted: neutralise spreadsheet formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_json(search=None, task_id=None, website_id=None) -> str:
    out = [{"task_id": d["task_id"], "source_url": d.get("source_url"), "extracted_at": d["extracted_at"].isoformat(),
            **{"data": d["data"]}} for d in _iter_docs(search, task_id, website_id)]
    return json.dumps(out, ensure_ascii=False, indent=2)


def export_csv(search=None, task_id=None, website_id=None) -> str:
    docs = list(_iter_docs(search, task_id, website_id))
    keys: list[str] = []
    for d in docs:
        for k in d["data"]:
            if k not in keys:
                keys.append(k)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["task_id", "source_url", "extracted_at", *[_csv_safe(k) for k in keys]])
    for d in docs:
        w.writerow([d["task_id"], _csv_safe(d.get("source_url")), d["extracted_at"].isoformat(),
                    *[_csv_safe(d["data"].get(k)) for k in keys]])
    return buf.getvalue()
