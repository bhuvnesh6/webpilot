"""The controlled action protocol between the LLM and the browser.

The LLM can only ever emit one JSON object that validates against `Action`. Nothing it
says is executed as code: each action name maps to a fixed Python handler in HANDLERS.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import verification
from .policies import PolicyViolation, normalize_key, resolve_relative

ACTION_NAMES = (
    "navigate", "click", "type_text", "select_option", "scroll", "press_key", "wait",
    "extract_text", "extract_links", "screenshot", "go_back", "record_results", "done", "ask_admin",
)
ActionName = Literal[
    "navigate", "click", "type_text", "select_option", "scroll", "press_key", "wait",
    "extract_text", "extract_links", "screenshot", "go_back", "record_results", "done", "ask_admin",
]
Scalar = str | int | float | bool | None

MAX_RECORDS_PER_ACTION = 100
MAX_FIELDS_PER_RECORD = 30


class ActionValidationError(ValueError):
    """The LLM's output could not be parsed into a valid action."""


class ActionError(Exception):
    """A valid action failed while executing. `recoverable` means re-observing may help."""

    def __init__(self, code: str, message: str, recoverable: bool = True):
        super().__init__(message)
        self.code, self.message, self.recoverable = code, message, recoverable


class Target(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ref: Optional[str] = Field(default=None, max_length=12)
    text: Optional[str] = Field(default=None, max_length=300)
    label: Optional[str] = Field(default=None, max_length=300)
    placeholder: Optional[str] = Field(default=None, max_length=300)
    role: Optional[str] = Field(default=None, max_length=40)
    near: Optional[str] = Field(default=None, max_length=300)

    @field_validator("ref")
    @classmethod
    def _ref_shape(cls, v):
        if v is not None and not re.fullmatch(r"e\d{1,5}", v.strip()):
            raise ValueError("ref must look like 'e12' (copy it from the observation)")
        return v.strip() if v else v

    @model_validator(mode="after")
    def _has_identifier(self):
        if not any([self.ref, self.text, self.label, self.placeholder]):
            raise ValueError("target needs at least one of: ref, text, label, placeholder")
        return self

    def describe(self) -> str:
        bits = [f"{k}={v!r}" for k, v in self.model_dump(exclude_none=True).items()]
        return ", ".join(bits)


class Action(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: ActionName
    reason: Optional[str] = Field(default=None, max_length=400)
    target: Optional[Target] = None
    value: Optional[str] = Field(default=None, max_length=2000)
    url: Optional[str] = Field(default=None, max_length=2048)
    direction: Optional[Literal["up", "down", "top", "bottom"]] = None
    amount: Optional[int] = Field(default=None, ge=50, le=5000)
    key: Optional[str] = Field(default=None, max_length=30)
    condition: Optional[Literal["seconds", "load", "network_idle", "text_visible", "text_hidden"]] = None
    seconds: Optional[float] = Field(default=None, ge=0, le=10)
    expected_result: Optional[str] = Field(default=None, max_length=300)
    expect_text: Optional[str] = Field(default=None, max_length=200)
    ocr: Optional[bool] = None
    message: Optional[str] = Field(default=None, max_length=1000)
    summary: Optional[str] = Field(default=None, max_length=2000)
    records: Optional[list[dict[str, Any]]] = None
    results: Optional[list[dict[str, Any]]] = None

    @field_validator("records", "results")
    @classmethod
    def _clean_rows(cls, rows):
        if rows is None:
            return rows
        if len(rows) > MAX_RECORDS_PER_ACTION:
            raise ValueError(f"at most {MAX_RECORDS_PER_ACTION} records per action")
        return clean_records(rows)

    @model_validator(mode="after")
    def _required_fields(self):
        a = self.action
        need = {
            "navigate": ("url",), "click": ("target",), "type_text": ("target", "value"),
            "select_option": ("target", "value"), "press_key": ("key",),
            "ask_admin": ("message",), "record_results": ("records",),
        }.get(a, ())
        def absent(f: str) -> bool:
            v = getattr(self, f)
            if v is None:
                return True
            if f == "value":
                return False          # "" is a legitimate value: it clears a field
            return not v              # empty url / key / message / records / target
        missing = [f for f in need if absent(f)]
        if missing:
            raise ValueError(f"action '{a}' requires: {', '.join(missing)}")
        if a == "wait" and self.condition in {"text_visible", "text_hidden"} and not self.value:
            raise ValueError("wait with text_visible/text_hidden requires 'value' (the text)")
        if a == "press_key":
            normalize_key(self.key or "")  # raises PolicyViolation -> reported as invalid
        return self

    # ---- helpers
    def descriptors(self) -> list[str]:
        """Text used for risk classification."""
        out = [self.reason or ""]
        if self.target:
            out += [self.target.text, self.target.label, self.target.role, self.target.placeholder]
        if self.key:
            out.append(self.key)
        return [o for o in out if o]

    def log_params(self, mask_value: bool = False) -> dict:
        d = self.model_dump(exclude_none=True, exclude={"records", "results"})
        if mask_value and "value" in d:
            d["value"] = "[REDACTED]"
        if self.records:
            d["records"] = f"{len(self.records)} record(s)"
        if self.results:
            d["results"] = f"{len(self.results)} record(s)"
        return d

    def short(self) -> str:
        t = self.target.describe() if self.target else ""
        extra = self.url or self.direction or self.key or self.condition or ""
        return f"{self.action}({t or extra})".replace("\n", " ")[:160]


def clean_records(rows: list[dict]) -> list[dict]:
    """Normalise extracted rows to flat dicts of scalars; drop empty rows."""
    cleaned: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        flat: dict[str, Scalar] = {}
        for k, v in list(row.items())[:MAX_FIELDS_PER_RECORD]:
            key = str(k).strip()[:64]
            if not key or key.startswith("$") or "." in key:
                key = key.lstrip("$").replace(".", "_")[:64]
            if not key:
                continue
            if isinstance(v, str):
                v = v.strip()[:2000]
            elif isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)[:2000]
            elif not isinstance(v, (int, float, bool)) and v is not None:
                v = str(v)[:2000]
            flat[key] = v
        if any(v not in (None, "") for v in flat.values()):
            cleaned.append(flat)
    return cleaned


# ------------------------------------------------------------------ parsing LLM output

def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    raise ActionValidationError("Response did not contain a valid JSON object.")


def parse_action(text: str) -> Action:
    """Parse + validate one action. Raises ActionValidationError with a concise, fixable message."""
    obj = _extract_json_object(text)
    if isinstance(obj.get("action"), dict):  # tolerate {"action": {...}} wrappers
        obj = obj["action"]
    name = obj.get("action")
    if name not in ACTION_NAMES:
        raise ActionValidationError(f"Unsupported action {name!r}. Allowed actions: {', '.join(ACTION_NAMES)}.")
    try:
        return Action.model_validate(obj)
    except ValidationError as exc:
        msgs = []
        for err in exc.errors()[:4]:
            loc = ".".join(str(x) for x in err["loc"]) or "action"
            msgs.append(f"{loc}: {err['msg']}")
        raise ActionValidationError("; ".join(msgs)) from exc
    except PolicyViolation as exc:
        raise ActionValidationError(exc.message) from exc


# ------------------------------------------------------------------ prompt/schema docs

ACTION_PROMPT_SPEC = """\
Respond with EXACTLY ONE JSON object and nothing else. Allowed actions and their fields:

{"action":"navigate","url":"https://...","reason":"..."}                       open a permitted URL
{"action":"click","target":{...},"expected_result":"...","expect_text":"..."}  click a control
{"action":"type_text","target":{...},"value":"text to enter"}                  type into a field (replaces existing text)
{"action":"select_option","target":{...},"value":"option text"}                choose an option in a <select>
{"action":"scroll","direction":"down|up|top|bottom","amount":600}              scroll the page (or a container via "target")
{"action":"press_key","key":"Enter"}      allowed keys: Enter Tab Escape Arrows PageUp PageDown Home End Space Backspace
{"action":"wait","condition":"seconds|load|network_idle|text_visible|text_hidden","seconds":1,"value":"text"}
{"action":"extract_text","target":{...}}      read text (omit target for the whole page); text appears in the next observation
{"action":"extract_links","target":{...}}     list permitted links (omit target for the whole page)
{"action":"screenshot","ocr":true}            capture a screenshot; set ocr=true to read it with OCR
{"action":"go_back"}
{"action":"record_results","records":[{"field":"value",...}]}   save structured rows you have read (duplicates are ignored)
{"action":"ask_admin","message":"what you need from the administrator"}
{"action":"done","summary":"what was achieved","results":[{"field":"value",...}]}

"target" identifies the element. PREFER {"ref":"e12"} copied from the observation's elements list. Otherwise use
{"label":..}, {"placeholder":..}, {"text":..,"role":"button|link|..."}; add {"near":"nearby text"} to disambiguate.
Add "expect_text" (text that should be visible afterwards) to click actions whenever you know it.
Add a short "reason" to every action.
"""

ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTION_NAMES)},
        "reason": {"type": "string"},
        "target": {"type": "object", "properties": {k: {"type": "string"} for k in ("ref", "text", "label", "placeholder", "role", "near")}},
        "value": {"type": "string"}, "url": {"type": "string"},
        "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]},
        "amount": {"type": "integer"}, "key": {"type": "string"},
        "condition": {"type": "string", "enum": ["seconds", "load", "network_idle", "text_visible", "text_hidden"]},
        "seconds": {"type": "number"}, "expected_result": {"type": "string"}, "expect_text": {"type": "string"},
        "ocr": {"type": "boolean"}, "message": {"type": "string"}, "summary": {"type": "string"},
        "records": {"type": "array", "items": {"type": "object"}},
        "results": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["action"],
}


# ------------------------------------------------------------------ execution

@dataclass
class ActionOutcome:
    ok: bool
    detail: str
    verified: Optional[bool] = None
    verification: str = ""
    data: Any = None
    method: str = "dom"
    request_ocr: bool = False
    artifacts: list[str] = field(default_factory=list)


@dataclass
class ExecContext:
    ocr_allowed: bool = False
    extract_max_chars: int = 6000
    link_limit: int = 80


def resolve_target(session, action: Action, ctx: ExecContext):
    return session.resolve(action.target, ocr_allowed=ctx.ocr_allowed)


_resolve = resolve_target


def do_navigate(session, action, ctx, resolved=None) -> ActionOutcome:
    url = resolve_relative(session.current_url, action.url or "")
    session.goto(url)
    return ActionOutcome(True, f"Navigated to {session.current_url}")


def do_click(session, action, ctx, resolved=None) -> ActionOutcome:
    resolved = resolved or _resolve(session, action, ctx)
    before = session.page_state()
    session.click(resolved)
    v = verification.verify_click(session, before, action.expect_text)
    return ActionOutcome(True, f"Clicked {resolved.description}", v.ok, v.detail, method=resolved.method)


def do_type_text(session, action, ctx, resolved=None) -> ActionOutcome:
    resolved = resolved or _resolve(session, action, ctx)
    session.fill(resolved, action.value or "")
    v = verification.verify_typed(session, resolved, action.value or "")
    return ActionOutcome(True, f"Typed into {resolved.description}", v.ok, v.detail, method=resolved.method)


def do_select_option(session, action, ctx, resolved=None) -> ActionOutcome:
    resolved = resolved or _resolve(session, action, ctx)
    session.select(resolved, action.value or "")
    v = verification.verify_selected(session, resolved, action.value or "")
    return ActionOutcome(True, f"Selected {action.value!r} in {resolved.description}", v.ok, v.detail, method=resolved.method)


def do_scroll(session, action, ctx, resolved=None) -> ActionOutcome:
    scope = _resolve(session, action, ctx) if action.target else None
    before = session.scroll_position(scope)
    session.scroll(action.direction or "down", action.amount, scope)
    after = session.scroll_position(scope)
    v = verification.verify_scroll(before, after)
    return ActionOutcome(True, f"Scrolled {action.direction or 'down'}", v.ok, v.detail)


def do_press_key(session, action, ctx, resolved=None) -> ActionOutcome:
    before = session.page_state()
    key = normalize_key(action.key or "")
    session.press(key)
    v = verification.verify_click(session, before, action.expect_text, timeout=2.0)
    return ActionOutcome(True, f"Pressed {key}", v.ok, v.detail)


def do_wait(session, action, ctx, resolved=None) -> ActionOutcome:
    cond = action.condition or "seconds"
    session.wait(cond, action.value, action.seconds if action.seconds is not None else 1.0)
    return ActionOutcome(True, f"Waited ({cond})", True, "wait finished")


def do_extract_text(session, action, ctx, resolved=None) -> ActionOutcome:
    scope = _resolve(session, action, ctx) if action.target else None
    text = session.extract_text(scope, ctx.extract_max_chars)
    v = verification.verify_nonempty(text, "text")
    return ActionOutcome(True, f"Extracted {len(text)} characters", v.ok, v.detail, data=text)


def do_extract_links(session, action, ctx, resolved=None) -> ActionOutcome:
    scope = _resolve(session, action, ctx) if action.target else None
    links, excluded = session.extract_links(scope, ctx.link_limit)
    v = verification.verify_nonempty(links, "links")
    note = f" ({excluded} excluded by domain policy)" if excluded else ""
    return ActionOutcome(True, f"Extracted {len(links)} link(s){note}", v.ok, v.detail, data=links)


def do_screenshot(session, action, ctx, resolved=None) -> ActionOutcome:
    path = session.screenshot(label="requested")
    if path is None:
        return ActionOutcome(True, "Screenshot limit reached for this task; no new screenshot taken", None)
    return ActionOutcome(True, "Screenshot captured", True, "file written", request_ocr=bool(action.ocr),
                         artifacts=[path.name])


def do_go_back(session, action, ctx, resolved=None) -> ActionOutcome:
    if not session.go_back():
        raise ActionError("no_history", "There is no previous page to go back to.", recoverable=False)
    return ActionOutcome(True, f"Went back to {session.current_url}")


# Handlers that touch the browser. record_results / done / ask_admin are engine-level
# (they need results storage and administrator interaction, not a page).
HANDLERS: dict[str, Callable[..., ActionOutcome]] = {
    "navigate": do_navigate, "click": do_click, "type_text": do_type_text, "select_option": do_select_option,
    "scroll": do_scroll, "press_key": do_press_key, "wait": do_wait, "extract_text": do_extract_text,
    "extract_links": do_extract_links, "screenshot": do_screenshot, "go_back": do_go_back,
}
ENGINE_LEVEL = {"record_results", "done", "ask_admin"}
NEEDS_TARGET_RESOLUTION = {"click", "type_text", "select_option"}


def execute(session, action: Action, ctx: ExecContext, resolved=None) -> ActionOutcome:
    handler = HANDLERS.get(action.action)
    if handler is None:
        raise ActionError("unsupported", f"No handler for action {action.action!r}.", recoverable=False)
    return handler(session, action, ctx, resolved)
