"""The agent loop: observe -> ask the LLM -> validate -> execute -> verify -> repeat.

This module has no knowledge of Flask, Celery or MongoDB. Persistence and administrator
interaction are injected through `AgentHooks`, which keeps the loop unit-testable.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import actions as A
from .browser import BrowserStartError
from .llm import (DecisionContext, LLMError, TaskCancelled, build_messages, correction_messages,
                  interruptible_sleep)
from .observer import Observation, Observer
from ..utils import looks_sensitive
from .policies import NavPolicy, PolicyViolation, classify_risk

log = logging.getLogger(__name__)


class AgentFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Limits:
    max_actions: int = 100
    max_duration_s: int = 900
    max_recovery: int = 3
    max_llm_retries: int = 3
    admin_wait_s: int = 600


@dataclass
class AdminDecision:
    approved: bool
    message: str = ""
    timed_out: bool = False


def _noop(*_a, **_k):
    return None


@dataclass
class AgentHooks:
    cancel_requested: Callable[[], bool] = lambda: False
    progress: Callable[..., None] = _noop            # (status, current_action, message, url=None, **extra)
    record_action: Callable[[dict], None] = _noop    # one document per decided action
    log: Callable[..., None] = _noop                 # (severity, event, message, details=None)
    ask_admin: Callable[[dict], AdminDecision] = lambda prompt: AdminDecision(False, "no administrator channel", True)
    save_results: Callable[[list, str], None] = _noop
    add_tokens: Callable[[dict], None] = _noop


@dataclass
class AgentTask:
    task_id: str
    start_url: str
    website_instructions: str
    task_instructions: str
    params: dict
    allowed_domains: list[str]
    ocr_mode: str  # automatic | enabled | disabled
    limits: Limits
    artifact_dir: Path


@dataclass
class AgentResult:
    status: str  # completed | failed | cancelled
    summary: str = ""
    results: list[dict] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    actions_executed: int = 0
    consequential_actions: int = 0
    final_url: str = ""


@dataclass
class _State:
    started: float
    deadline: float
    seq: int = 0
    actions_used: int = 0
    consecutive_failures: int = 0
    consequential: int = 0
    records: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    history: list = field(default_factory=list)
    last_outcome: str = ""
    extracted: str | None = None
    force_ocr: bool = False
    admin_notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    recent_sigs: deque = field(default_factory=lambda: deque(maxlen=8))
    repeat_count: int = 0
    summary: str = ""
    finished: bool = False
    ocr_files: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentEngine:
    def __init__(self, settings, browsers, llm, ocr, hooks: AgentHooks):
        self.s, self.browsers, self.llm, self.ocr, self.hooks = settings, browsers, llm, ocr, hooks
        self.observer = Observer(ocr)

    # ================================================================ public
    def run(self, task: AgentTask) -> AgentResult:
        now = time.monotonic()
        st = _State(started=now, deadline=now + task.limits.max_duration_s)
        session = None
        res = AgentResult(status="failed")
        try:
            policy = NavPolicy(tuple(task.allowed_domains), allow_private=self.s.allow_private_networks)
            policy.check_navigation(task.start_url)
            ocr_on = task.ocr_mode != "disabled" and self.ocr is not None
            session = self.browsers.new_session(policy, task.artifact_dir, self.ocr if ocr_on else None)
            self._open_start(session, task, st)
            obs = self._observe(session, task, st, force=(task.ocr_mode == "enabled" and ocr_on))
            ectx = A.ExecContext(ocr_allowed=ocr_on)
            while not st.finished:
                self._checkpoint(session, task, st)
                action = self._decide(task, st, obs)
                obs = self._step(session, task, st, action, obs, ectx, ocr_on)
            res = AgentResult("completed", summary=st.summary)
        except TaskCancelled:
            res = AgentResult("cancelled", summary="Stopped at a safe checkpoint on the administrator's request.")
        except AgentFailure as f:
            res = AgentResult("failed", error_code=f.code, error_message=f.message)
        except PolicyViolation as v:
            res = AgentResult("failed", error_code="policy_violation", error_message=v.message)
        except LLMError as e:
            res = AgentResult("failed", error_code=e.code, error_message=str(e))
        except A.ActionError as e:
            res = AgentResult("failed", error_code=e.code, error_message=e.message)
        except BrowserStartError as e:
            res = AgentResult("failed", error_code="browser_start_failed", error_message=str(e))
        except Exception as e:  # noqa: BLE001 - last-resort guard; details go to the admin-only log
            log.exception("Unexpected agent error")
            self.hooks.log("error", "internal_error", f"{e.__class__.__name__}: {str(e)[:300]}",
                           {"traceback": traceback.format_exc()[-3500:]})
            res = AgentResult("failed", error_code="internal_error", error_message=f"{e.__class__.__name__}: {str(e)[:300]}")
        finally:
            if session is not None:
                res.final_url = session.current_url
                if res.status != "cancelled":
                    try:
                        session.screenshot(label="final")
                    except Exception:
                        pass
                session.close()
        res.results = list(st.records)
        res.actions_executed, res.consequential_actions = st.actions_used, st.consequential
        return res

    # ================================================================ phases
    def _open_start(self, session, task: AgentTask, st: _State) -> None:
        attempts = 1 + task.limits.max_recovery
        for i in range(attempts):
            self._checkpoint(session, task, st, count_budget=False)
            try:
                session.goto(task.start_url)
                if session.last_status and session.last_status >= 500:
                    raise A.ActionError("website_unavailable", f"The website answered HTTP {session.last_status}.")
                self.hooks.log("info", "start_page_loaded", f"Opened {session.current_url}")
                return
            except A.ActionError as e:
                if not e.recoverable or e.code == "browser_crashed":
                    raise
                if i == attempts - 1:
                    raise AgentFailure("website_unavailable", f"Could not open the website after {attempts} attempts: {e.message}")
                delay = min(30, 2 ** (i + 1))
                self.hooks.progress("retrying", "navigate", f"Website not reachable ({e.message[:100]}); retry {i + 1}/{attempts - 1} in {delay}s", task.start_url)
                self.hooks.log("warning", "start_retry", e.message, {"attempt": i + 1})
                interruptible_sleep(delay, self.hooks.cancel_requested)

    def _checkpoint(self, session, task: AgentTask, st: _State, count_budget: bool = True) -> None:
        if self.hooks.cancel_requested():
            raise TaskCancelled()
        session.check_health()
        if time.monotonic() > st.deadline:
            raise AgentFailure("duration_limit", f"Task exceeded its {task.limits.max_duration_s}s time limit.")
        if count_budget and st.actions_used >= task.limits.max_actions:
            raise AgentFailure("action_limit", f"Task reached its limit of {task.limits.max_actions} actions.")

    def _observe(self, session, task: AgentTask, st: _State, force: bool = False) -> Observation:
        use_ocr = (force or st.force_ocr) and task.ocr_mode != "disabled" and self.ocr is not None
        auto = task.ocr_mode != "disabled" and self.ocr is not None
        st.force_ocr = False
        obs = self.observer.observe(session, use_ocr=use_ocr, auto_sparse=auto)
        for res in session.pop_ocr_results():
            st.ocr_files += 1
            name = f"ocr-{st.ocr_files:03d}.json"
            try:
                task.artifact_dir.mkdir(parents=True, exist_ok=True)
                (task.artifact_dir / name).write_text(json.dumps(res.to_dict(), ensure_ascii=False), encoding="utf-8")
            except OSError:
                name = ""
            self.hooks.log("info", "ocr", f"OCR found {len(res.elements)} text regions in {res.duration_ms} ms",
                           {"file": name, "screenshot": res.screenshot_id, "duration_ms": res.duration_ms,
                            "scale": round(res.scale, 3), "sample": [e.text[:60] for e in res.elements[:40]]})
        return obs

    # ================================================================ decision
    def _decide(self, task: AgentTask, st: _State, obs: Observation) -> A.Action:
        ctx = DecisionContext(
            website_instructions=task.website_instructions, task_instructions=task.task_instructions,
            params=task.params, allowed_domains=task.allowed_domains, observation=obs.to_prompt(),
            history=st.history, last_outcome=st.last_outcome, extracted=st.extracted,
            records_count=len(st.records), records_tail=st.records[-3:],
            actions_left=task.limits.max_actions - st.actions_used,
            seconds_left=max(0, int(st.deadline - time.monotonic())),
            warnings=list(st.warnings), admin_notes=list(st.admin_notes),
        )
        st.warnings.clear()
        st.extracted = None
        messages = build_messages(ctx)

        def on_throttle(delay: float, attempt: int, reason: str) -> None:
            self.hooks.progress("waiting_for_llm", "waiting", f"{reason}; retrying in {delay:.0f}s (attempt {attempt})", obs.url, throttled=True)
            self.hooks.log("warning", "llm_throttled", reason, {"delay_s": round(delay, 1), "attempt": attempt})

        self.hooks.progress("waiting_for_llm", "thinking", "Asking the model for the next action", obs.url, throttled=False)
        bad = ""
        for attempt in range(task.limits.max_llm_retries + 1):
            completion = self.llm.complete(messages, cancel_check=self.hooks.cancel_requested, on_throttle=on_throttle)
            self.hooks.add_tokens(completion.usage)
            try:
                if not completion.text.strip():
                    raise A.ActionValidationError("Empty response (the model may have run out of tokens while reasoning).")
                return A.parse_action(completion.text)
            except A.ActionValidationError as e:
                bad = str(e)
                self.hooks.log("warning", "invalid_llm_output", bad[:300], {"attempt": attempt + 1, "raw": completion.text[:400]})
                messages = correction_messages(messages, completion.text, bad)
        raise AgentFailure("invalid_llm_output", f"The model kept returning invalid actions: {bad[:200]}")

    # ================================================================ one step
    def _step(self, session, task: AgentTask, st: _State, action: A.Action, obs: Observation,
              ectx: A.ExecContext, ocr_on: bool) -> Observation:
        st.seq += 1
        rec = {"seq": st.seq, "action": action.action, "target": action.target.describe() if action.target else "",
               "params": action.log_params(), "reason": action.reason or "", "started_at": _now(),
               "url_before": session.current_url, "artifacts": []}
        self.hooks.progress("running", action.short(), action.reason or f"Running {action.action}", session.current_url)
        status, detail, verification, method = "success", "", {}, "dom"
        outcome: A.ActionOutcome | None = None
        failure_code = ""
        sensitive = looks_sensitive(" ".join(action.descriptors()))
        try:
            if action.action == "done":
                new = self._add_records(st, action.results or [], session.current_url)
                st.summary, st.finished = action.summary or "Task finished.", True
                detail = f"Finished with {len(st.records)} saved row(s) ({new} new)."
            elif action.action == "record_results":
                st.actions_used += 1
                new = self._add_records(st, action.records or [], session.current_url)
                detail = f"Recorded {new} new row(s), {len(action.records or []) - new} duplicate/empty ignored; total {len(st.records)}."
                st.last_outcome = f"OK: {detail}"
            elif action.action == "ask_admin":
                st.actions_used += 1
                decision = self._ask_admin(st, {"kind": "question", "message": action.message, "action": action.short()})
                if decision.timed_out:
                    raise AgentFailure("admin_timeout", "The administrator did not respond in time.")
                st.admin_notes.append(decision.message or ("Continue." if decision.approved else "No."))
                detail = f"Administrator replied: {decision.message[:200] or '(continue)'}"
                st.last_outcome = f"The administrator replied: {decision.message or 'continue'}"
            else:
                st.actions_used += 1
                outcome, status, detail, failure_code, sens = self._run_browser_action(session, task, st, action, ectx)
                sensitive = sensitive or sens
                if outcome is not None:
                    method = outcome.method
                    verification = {"ok": outcome.verified, "detail": outcome.verification}
                    rec["artifacts"] = outcome.artifacts
                    if outcome.data is not None:
                        st.extracted = (outcome.data if isinstance(outcome.data, str)
                                        else json.dumps(outcome.data, ensure_ascii=False)[:6000])
                    if outcome.request_ocr:
                        st.force_ocr = True
        except (TaskCancelled, AgentFailure):
            rec.update(outcome="aborted", finished_at=_now())
            self.hooks.record_action(rec)
            raise
        # ---- bookkeeping
        if sensitive and "value" in rec["params"]:
            rec["params"]["value"] = "[REDACTED]"
        rec.update(outcome=status, detail=detail, verification=verification, method=method,
                   finished_at=_now(), url_after=session.current_url)
        if failure_code:
            rec["error"] = {"code": failure_code, "message": detail}
        self.hooks.record_action(rec)
        line_status = {"success": "ok", "failed": "FAILED", "blocked": "BLOCKED", "denied": "DENIED"}.get(status, status)
        if outcome is not None and outcome.verified is False and status == "success":
            line_status = "UNVERIFIED"
        st.history.append(f"{st.seq}. {action.short()} -> {line_status}: {detail[:140]}")
        if action.action not in ("record_results", "ask_admin", "done"):
            st.last_outcome = self._outcome_text(status, detail, outcome, failure_code)
        if st.finished:
            return obs

        # ---- recovery accounting
        soft_fail = status in ("failed", "blocked") or (outcome is not None and outcome.verified is False and action.action in ("click", "type_text", "select_option"))
        if soft_fail:
            st.consecutive_failures += 1
            if st.consecutive_failures > task.limits.max_recovery:
                raise AgentFailure("too_many_failures",
                                   f"{st.consecutive_failures} consecutive failed attempts. Last: {detail[:200]}")
            if status == "failed" and ocr_on:
                st.force_ocr = True  # "re-examine the page": a fresh observation with OCR
            self.hooks.progress("retrying", action.short(), f"Recovering from: {detail[:120]}", session.current_url)
        elif status == "success":
            st.consecutive_failures = 0

        new_obs = self._observe(session, task, st)
        self._detect_loop(st, action, new_obs, task)
        return new_obs

    def _run_browser_action(self, session, task, st, action, ectx):
        """Returns (outcome|None, status, detail, failure_code, sensitive_field)."""
        sensitive = False
        try:
            resolved = None
            if action.action in A.NEEDS_TARGET_RESOLUTION:
                resolved = A.resolve_target(session, action, ectx)
                sensitive = resolved.sensitive
            info = resolved.info if resolved else (session.focused_info() if action.action == "press_key" else {})
            descriptors = action.descriptors() + (resolved.descriptors() if resolved else [info.get("name")])
            risk = classify_risk(action.action, descriptors, info)
            if risk:
                decision = self._ask_admin(st, {
                    "kind": "approval", "risk": risk, "action": action.short(),
                    "message": f"The agent wants to {action.short()}, which looks like {risk}. Approve?"})
                if decision.timed_out:
                    raise AgentFailure("admin_timeout", "The administrator did not respond to an approval request in time.")
                if not decision.approved:
                    self.hooks.log("warning", "action_denied", f"Administrator denied: {action.short()} ({risk})")
                    return None, "denied", f"The administrator DENIED this action ({risk}). Do not retry it.", "", sensitive
                st.consequential += 1
                self.hooks.log("warning", "action_approved", f"Administrator approved: {action.short()} ({risk})")
            outcome = A.execute(session, action, ectx, resolved)
            if sensitive and action.action == "type_text":
                outcome.detail = "Typed into a sensitive field (value not logged)"
            return outcome, "success", outcome.detail, "", sensitive
        except PolicyViolation as v:
            self.hooks.log("warning", "policy_blocked", v.message, {"code": v.code})
            return None, "blocked", f"Blocked by security policy: {v.message}", v.code, sensitive
        except A.ActionError as e:
            if e.code == "browser_crashed":
                raise
            self.hooks.log("warning", "action_failed", f"{action.short()}: {e.message}", {"code": e.code})
            return None, "failed", e.message, e.code, sensitive

    @staticmethod
    def _outcome_text(status: str, detail: str, outcome, code: str) -> str:
        if status == "success":
            text = f"OK: {detail}."
            if outcome is not None:
                if outcome.verified is True:
                    text += f" Verified: {outcome.verification}."
                elif outcome.verified is False:
                    text += f" WARNING, not confirmed: {outcome.verification}."
            return text
        if status == "blocked":
            return f"BLOCKED: {detail}"
        if status == "denied":
            return f"DENIED: {detail}"
        return f"FAILED ({code}): {detail}"

    # ================================================================ helpers
    def _add_records(self, st: _State, rows: list[dict], source_url: str) -> int:
        fresh = []
        for row in A.clean_records(rows):
            key = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if key not in st.seen:
                st.seen.add(key)
                fresh.append(row)
        if fresh:
            try:
                self.hooks.save_results(fresh, source_url)
            except Exception as exc:
                raise AgentFailure("persistence_error", f"Could not save extracted results: {str(exc)[:200]}") from exc
            st.records.extend(fresh)
        return len(fresh)

    def _ask_admin(self, st: _State, prompt: dict) -> AdminDecision:
        waited = time.monotonic()
        self.hooks.progress("waiting_for_admin", "waiting", prompt["message"][:200])
        try:
            decision = self.hooks.ask_admin(prompt)
        finally:
            st.deadline += time.monotonic() - waited  # administrator think-time doesn't consume the task's budget
        return decision

    def _detect_loop(self, st: _State, action: A.Action, obs: Observation, task: AgentTask) -> None:
        sig = (action.action, action.target.describe() if action.target else "", action.value, action.url, action.direction,
               obs.url, hash(tuple(obs.visible_text[:30])))
        if st.recent_sigs and st.recent_sigs[-1] == sig:
            st.repeat_count += 1
        else:
            st.repeat_count = 0
        st.recent_sigs.append(sig)
        if st.repeat_count >= 4:
            raise AgentFailure("stuck_in_loop", "The agent repeated the same action with no change on the page.")
        if st.repeat_count >= 2:
            st.warnings.append("You have repeated the same action several times and the page did not change. Try something different, or finish with done.")
