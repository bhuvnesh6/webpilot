import json

import httpx
import groq
import pytest

from app.agent import actions as A
from app.agent import llm as L
from app.utils import redact


def test_parse_plain_fenced_and_chatty_output():
    a = A.parse_action('{"action":"click","target":{"text":"Search"}}')
    assert a.action == "click" and a.target.text == "Search"
    assert A.parse_action('```json\n{"action":"go_back"}\n```').action == "go_back"
    assert A.parse_action('Sure! Here you go: {"action":"scroll","direction":"down"} hope it helps').direction == "down"
    assert A.parse_action('{"action": {"action":"done","summary":"ok"}}').summary == "ok"


@pytest.mark.parametrize("raw,needle", [
    ("no json here", "JSON"), ('{"action":"run_python","code":"1"}', "Unsupported"), ('{"action":"shell","cmd":"rm -rf /"}', "Unsupported"),
    ('{"action":"click"}', "target"), ('{"action":"type_text","target":{"label":"x"}}', "value"),
    ('{"action":"navigate"}', "url"), ('{"action":"click","target":{"ref":"not-a-ref"}}', "ref"),
    ('{"action":"click","target":{}}', "target needs"), ('{"action":"press_key","key":"Control+C"}', "not permitted"),
    ('{"action":"scroll","amount":999999}', "amount"), ('{"action":"wait","condition":"text_visible"}', "value"),
    ('{"action":"record_results","records":[]}', "records"),
])
def test_invalid_actions_are_rejected_with_actionable_messages(raw, needle):
    with pytest.raises(A.ActionValidationError) as ei:
        A.parse_action(raw)
    assert needle.lower() in str(ei.value).lower()


def test_extra_fields_cannot_smuggle_code():
    a = A.parse_action('{"action":"click","target":{"text":"Go","js":"alert(1)"},"script":"import os","eval":"1+1"}')
    dumped = a.model_dump()
    assert "script" not in dumped and "eval" not in dumped and "js" not in dumped["target"]


def test_empty_value_allowed_for_clearing_fields():
    assert A.parse_action('{"action":"type_text","target":{"label":"Q"},"value":""}').value == ""


def test_clean_records_flattens_and_drops_empty():
    rows = A.clean_records([{"a": " x ", "b": {"n": 1}, "$bad": 2, "c.d": 3}, {"a": "", "b": None}, "junk", {"p": 5}])
    assert rows[0]["a"] == "x" and rows[0]["b"] == '{"n": 1}' and "bad" in rows[0] and "c_d" in rows[0]
    assert len(rows) == 2 and rows[1] == {"p": 5}


def test_log_params_mask_values():
    a = A.parse_action('{"action":"type_text","target":{"label":"Password"},"value":"hunter2"}')
    assert a.log_params(mask_value=True)["value"] == "[REDACTED]" and "hunter2" not in json.dumps(a.log_params(mask_value=True))
    assert "hunter2" not in a.short()


def test_registry_has_no_code_execution_actions():
    assert set(A.HANDLERS) | A.ENGINE_LEVEL == set(A.ACTION_NAMES)
    assert not any(n in A.ACTION_NAMES for n in ("eval", "execute_js", "run_python", "shell", "evaluate"))


def test_redaction():
    text = "key gsk_abcdefghijklmnopqrstuv and mongodb://user:s3cretpw@host:27017/x Authorization: Bearer abc.def.ghijklmnop mine=topsecret1"
    out = redact(text, ["topsecret1"])
    for leak in ("gsk_abcdefghijkl", "s3cretpw", "abc.def.ghij", "topsecret1"):
        assert leak not in out


# ---------------------------------------------------------------- LLM client

class FakeClient:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        self.chat = type("C", (), {"completions": self})()

    def create(self, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        msg = type("M", (), {"content": o})()
        usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})()
        return type("R", (), {"choices": [type("Ch", (), {"message": msg})()], "usage": usage})()


def status_error(cls, code, headers=None, body=None):
    req = httpx.Request("POST", "http://x")
    return cls("boom", response=httpx.Response(code, headers=headers or {}, request=req), body=body)


@pytest.fixture
def fast(monkeypatch, settings):
    sleeps = []   # only provider backoff waits; the client-side rate limiter's micro-sleeps are ignored
    monkeypatch.setattr(L, "interruptible_sleep", lambda s, c=None: sleeps.append(s) if s >= 1 else None)
    return sleeps


def client_with(settings, outcomes):
    fc = FakeClient(outcomes)
    return L.GroqClient(settings, client=fc, limiter=L.RateLimiter(100000)), fc


def test_rate_limit_retries_with_retry_after_then_succeeds(settings, fast):
    c, fc = client_with(settings, [status_error(groq.RateLimitError, 429, {"retry-after": "7"}), '{"action":"done"}'])
    throttles = []
    out = c.complete([{"role": "user", "content": "x"}], on_throttle=lambda d, a, r: throttles.append((d, a)))
    assert out.text == '{"action":"done"}' and out.usage["total_tokens"] == 15
    assert fast == [7.0] and throttles == [(7.0, 1)] and fc.calls == 2


def test_rate_limit_gives_up_after_bounded_retries(settings, fast):
    c, fc = client_with(settings, [status_error(groq.RateLimitError, 429)] * 10)
    with pytest.raises(L.LLMRateLimited):
        c.complete([{"role": "user", "content": "x"}])
    assert fc.calls == settings.max_llm_retries + 1          # bounded, never a retry storm
    assert fast == sorted(fast) and fast[0] >= 2              # exponential backoff


def test_huge_retry_after_fails_fast_instead_of_sleeping(settings, fast):
    c, fc = client_with(settings, [status_error(groq.RateLimitError, 429, {"retry-after": "86400"})])
    with pytest.raises(L.LLMRateLimited) as ei:
        c.complete([{"role": "user", "content": "x"}])
    assert fc.calls == 1 and not fast and "quota" in str(ei.value)


def test_auth_error_is_not_retried(settings, fast):
    c, fc = client_with(settings, [status_error(groq.AuthenticationError, 401)])
    with pytest.raises(L.LLMAuthError):
        c.complete([{"role": "user", "content": "x"}])
    assert fc.calls == 1


def test_server_errors_are_retried_then_reported_unavailable(settings, fast):
    c, fc = client_with(settings, [status_error(groq.InternalServerError, 503)] * 10)
    with pytest.raises(L.LLMUnavailable):
        c.complete([{"role": "user", "content": "x"}])
    assert fc.calls == settings.max_llm_retries + 1


def test_json_validate_failed_returns_text_for_repair_loop(settings, fast):
    err = status_error(groq.BadRequestError, 400, body={"error": {"failed_generation": "{bad json"}})
    c, _ = client_with(settings, [err])
    assert c.complete([{"role": "user", "content": "x"}]).text == "{bad json"


def test_unsupported_response_format_downgrades_once(settings, fast):
    err = status_error(groq.BadRequestError, 400, body={"error": {"message": "response_format not supported"}})
    err.args = ("response_format is not supported by this model",)
    c, fc = client_with(settings, [err, '{"action":"done"}'])
    c.response_format = "json_schema"
    assert c.complete([{"role": "user", "content": "x"}]).text == '{"action":"done"}' and c.response_format == "json_object"


def test_cancel_interrupts_waiting(settings):
    c, _ = client_with(settings, ['{"action":"done"}'])
    with pytest.raises(L.TaskCancelled):
        c.complete([{"role": "user", "content": "x"}], cancel_check=lambda: True)


def test_prompt_marks_page_content_untrusted_and_caps_history():
    ctx = L.DecisionContext("site rules", "do task", {"k": "v"}, ["example.com"], '{"url":"x"}', [f"step-{i:02d}" for i in range(20)],
                            "OK", "extracted text", 0, [], 10, 100)
    sys_msg, user_msg = L.build_messages(ctx)
    assert "UNTRUSTED" in sys_msg["content"] and "never reveal" in sys_msg["content"].lower()
    assert user_msg["content"].count("<page_content>") == 2 and "step-19" in user_msg["content"] and "step-05" not in user_msg["content"]
