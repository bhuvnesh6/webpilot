"""End-to-end agent tests against the local test site with a scripted LLM and real Chromium."""
import json

import pytest

from app.agent.engine import AdminDecision, AgentEngine, AgentHooks, AgentTask, Limits
from app.agent.llm import Completion
from app.agent.ocr import OcrEngine

pw = pytest.importorskip("playwright.sync_api")


class ScriptedLLM:
    """Replays a list of actions (dicts or callables(messages)->dict) and records the prompts it saw."""

    def __init__(self, script):
        self.script, self.i, self.seen = list(script), 0, []

    def complete(self, messages, cancel_check=None, on_throttle=None):
        self.seen.append(messages)
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if callable(item):
            item = item(messages)
        text = item if isinstance(item, str) else json.dumps(item)
        return Completion(text=text, usage={"total_tokens": 10})


@pytest.fixture(scope="module")
def browsers(tmp_path_factory):
    from tests.conftest import make_env
    from app.config import load_settings
    from app.agent.browser import BrowserManager
    settings = load_settings(make_env(tmp_path_factory.mktemp("b"), BROWSER_SANDBOX="false"))
    mgr = BrowserManager(settings)
    try:
        mgr.ensure()
    except Exception as exc:  # no Chromium on this machine
        pytest.skip(f"Chromium unavailable: {exc}")
    yield mgr
    mgr.close()


def run_task(settings, browsers, site, script, *, ocr=None, instructions="test", ocr_mode="automatic",
             start="/", hooks=None, limits=None, tmp_path=None, domains=None):
    saved, actions, logs = [], [], []
    h = hooks or AgentHooks()
    h.save_results = lambda rows, url: saved.extend(rows)
    h.record_action = actions.append
    h.log = lambda sev, ev, msg, details=None: logs.append((sev, ev, msg))
    llm = ScriptedLLM(script)
    engine = AgentEngine(settings, browsers, llm, ocr, h)
    task = AgentTask(task_id="t1", start_url=site + start, website_instructions="", task_instructions=instructions,
                     params={}, allowed_domains=domains or ["127.0.0.1"], ocr_mode=ocr_mode,
                     limits=limits or Limits(max_actions=40, max_duration_s=120, max_recovery=3, max_llm_retries=2),
                     artifact_dir=tmp_path / "art")
    result = engine.run(task)
    return result, saved, actions, logs, llm


def test_full_search_and_extract_flow(settings, browsers, site, tmp_path):
    script = [
        {"action": "type_text", "target": {"label": "Search keywords"}, "value": "laptops", "reason": "enter keywords"},
        {"action": "select_option", "target": {"label": "Category"}, "value": "Electronics"},
        {"action": "select_option", "target": {"label": "Country"}, "value": "Canada"},
        {"action": "click", "target": {"text": "Search", "role": "button"}, "expect_text": "30 found"},
        {"action": "scroll", "direction": "down"},
        {"action": "extract_text", "target": {"ref": "e999"}},   # nonexistent ref on purpose: engine must recover
        {"action": "extract_text"},
        {"action": "record_results", "records": [{"name": "Laptop Pro 14 #1", "price": "$325"}]},
        {"action": "record_results", "records": [{"name": "Laptop Pro 14 #1", "price": "$325"}, {"name": "Laptop Air 13 #2", "price": "$350"}]},
        {"action": "done", "summary": "Extracted 2 products", "results": [{"name": "Laptop Air 13 #2", "price": "$350"}]},
    ]
    result, saved, actions, logs, llm = run_task(settings, browsers, site, script, tmp_path=tmp_path)
    assert result.status == "completed", (result.error_code, result.error_message)
    assert [r["name"] for r in saved] == ["Laptop Pro 14 #1", "Laptop Air 13 #2"]   # duplicates dropped
    verified = {a["action"]: a["verification"].get("ok") for a in actions if a["outcome"] == "success"}
    assert verified["type_text"] is True and verified["select_option"] is True and verified["click"] is True
    assert verified["scroll"] is True
    assert any(a["outcome"] == "failed" for a in actions)           # the stale ref failed, didn't crash
    assert result.actions_executed >= 9
    # prompt-injection text is only ever shown inside the untrusted block
    last_user = llm.seen[-1][1]["content"]
    assert "<page_content>" in last_user and "SECRET" not in llm.seen[-1][0]["content"]


def test_buy_button_requires_admin_and_denial_is_respected(settings, browsers, site, tmp_path):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return AdminDecision(approved=False, message="no")

    hooks = AgentHooks(ask_admin=ask)
    script = [
        {"action": "click", "target": {"text": "Search", "role": "button"}},
        {"action": "wait", "condition": "text_visible", "value": "30 found", "seconds": 5},
        {"action": "click", "target": {"text": "Buy now", "near": "Laptop Gamer X #2"}},
        {"action": "done", "summary": "stopped"},
    ]
    result, _, actions, _, _ = run_task(settings, browsers, site, script, hooks=hooks, tmp_path=tmp_path)
    assert result.status == "completed"
    assert len(prompts) == 1 and "purchase" in prompts[0]["risk"]
    assert [a["outcome"] for a in actions if a["action"] == "click"][-1] == "denied"
    assert result.consequential_actions == 0


def test_domain_policy_blocks_navigation_and_counts_as_failure(settings, browsers, site, tmp_path):
    port = site.rsplit(":", 1)[1]
    script = [{"action": "navigate", "url": f"http://localhost:{port}/other.html"}] * 6
    result, _, actions, _, _ = run_task(settings, browsers, site, script, tmp_path=tmp_path,
                                        limits=Limits(max_actions=20, max_duration_s=60, max_recovery=2, max_llm_retries=1))
    assert result.status == "failed" and result.error_code in {"too_many_failures", "stuck_in_loop"}
    assert all(a["outcome"] == "blocked" for a in actions)


def test_action_limit_and_invalid_output(settings, browsers, site, tmp_path):
    result, *_ = run_task(settings, browsers, site, [{"action": "scroll", "direction": "down"},
                                                     {"action": "scroll", "direction": "up"}] * 5,
                          tmp_path=tmp_path, limits=Limits(max_actions=3, max_duration_s=60, max_recovery=3, max_llm_retries=1))
    assert result.status == "failed" and result.error_code == "action_limit"
    result, *_ = run_task(settings, browsers, site, ["not json at all", "still {nope", "{\"action\": \"rm -rf\"}"],
                          tmp_path=tmp_path, limits=Limits(max_actions=5, max_duration_s=60, max_recovery=3, max_llm_retries=2))
    assert result.status == "failed" and result.error_code == "invalid_llm_output"


def test_cancellation_stops_at_checkpoint(settings, browsers, site, tmp_path):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    hooks = AgentHooks(cancel_requested=cancel)
    result, *_ = run_task(settings, browsers, site, [{"action": "scroll", "direction": "down"}] * 20, hooks=hooks, tmp_path=tmp_path)
    assert result.status == "cancelled"


def test_ocr_fallback_clicks_canvas_button_even_when_screenshot_is_downscaled(settings, browsers, site, tmp_path):
    seen_shapes = []

    def fake_backend(arr):
        h, w = arr.shape[:2]
        seen_shapes.append((w, h))
        s = w / 1366.0                                  # what OCR "sees" is scaled; boxes are in ITS pixel space
        return [((200 * s, 100 * s, 320 * s, 140 * s), "Launch", 0.97)]

    ocr = OcrEngine(max_dimension=683, min_confidence=0.6, backend=fake_backend)   # forces a 0.5x resize
    script = [
        {"action": "click", "target": {"text": "Launch"}, "expect_text": "Launched!"},
        {"action": "done", "summary": "clicked"},
    ]
    result, _, actions, logs, _ = run_task(settings, browsers, site, script, ocr=ocr, start="/canvas.html", tmp_path=tmp_path)
    assert result.status == "completed", (result.error_code, result.error_message)
    click = [a for a in actions if a["action"] == "click"][0]
    assert click["method"] == "ocr" and click["verification"]["ok"] is True
    assert max(w for w, _ in seen_shapes) <= 683                      # screenshot really was downscaled
    assert any(ev == "ocr" for _, ev, _ in logs)


def test_ocr_disabled_means_no_ocr_fallback(settings, browsers, site, tmp_path):
    ocr = OcrEngine(backend=lambda arr: [((200, 100, 320, 140), "Launch", 0.97)])
    script = [{"action": "click", "target": {"text": "Launch"}}] * 5
    result, _, actions, *_ = run_task(settings, browsers, site, script, ocr=ocr, ocr_mode="disabled", start="/canvas.html",
                                      tmp_path=tmp_path, limits=Limits(max_actions=10, max_duration_s=60, max_recovery=1, max_llm_retries=1))
    assert result.status == "failed"
    assert all(a["method"] != "ocr" for a in actions)


def test_redirect_to_non_allowlisted_host_is_blocked_by_request_interception(settings, browsers, site, tmp_path):
    script = [{"action": "navigate", "url": site + "/redir"}] * 4
    result, _, actions, logs, _ = run_task(settings, browsers, site, script, tmp_path=tmp_path,
                                           limits=Limits(max_actions=10, max_duration_s=60, max_recovery=1, max_llm_retries=1))
    assert result.status == "failed"
    assert [a["outcome"] for a in actions][0] == "blocked"
    assert any(ev == "policy_blocked" for _, ev, _ in logs)


def test_production_policy_blocks_private_start_url(settings, browsers, site, tmp_path):
    from dataclasses import replace
    prod_like = replace(settings, allow_private_networks=False)
    saved = []
    engine = AgentEngine(prod_like, browsers, ScriptedLLM([{"action": "done"}]), None, AgentHooks())
    task = AgentTask("t", site + "/", "", "x", {}, ["127.0.0.1"], "disabled", Limits(), tmp_path / "a")
    result = engine.run(task)
    assert result.status == "failed" and result.error_code == "policy_violation" and not saved


def test_dns_rebinding_detector_marks_session_fatal():
    from app.agent.browser import BrowserSession
    from app.agent.policies import NavPolicy, PolicyViolation
    import pytest as _pt

    class FakeResp:
        url = "https://rebind.example.com/"
        def server_addr(self):
            return {"ipAddress": "10.0.0.5", "port": 443}

    sess = object.__new__(BrowserSession)
    sess.policy, sess.violations, sess.fatal_violation, sess.crashed = NavPolicy(("example.com",)), [], None, False
    sess._on_response(FakeResp())
    assert sess.fatal_violation and sess.fatal_violation["code"] == "blocked_ip"
    with _pt.raises(PolicyViolation):
        sess.check_health()          # the engine checkpoint aborts the task
