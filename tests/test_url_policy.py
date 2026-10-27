import pytest

from app.agent.policies import (NavPolicy, PolicyViolation, classify_risk, domain_allowed, effective_allowed_domains,
                                is_public_ip, normalize_key, parse_domain_list, validate_url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.1/", "http://localhost/", "http://LOCALHOST:8000", "http://[::1]/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data", "http://metadata.google.internal/", "http://10.1.2.3/", "http://192.168.0.1/",
    "http://172.16.5.5/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "http://[fd00:ec2::254]/",
    "http://redis:6379/", "http://webpilot-mongo:27017/", "http://printer.local/", "http://svc.internal/",
    "http://2130706433/", "http://0x7f.0.0.1/", "http://017700000001/",
])
def test_ssrf_targets_are_blocked(url):
    with pytest.raises(PolicyViolation):
        validate_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "data:text/html,hi",
                                 "gopher://example.com", "http://user:pw@example.com/", "http://", "", "   "])
def test_bad_schemes_and_shapes_are_rejected(url):
    with pytest.raises(PolicyViolation):
        validate_url(url, resolve=False)


def test_overlong_url_rejected():
    with pytest.raises(PolicyViolation):
        validate_url("https://example.com/" + "a" * 3000, resolve=False)


def test_public_literal_ip_ok_but_not_in_allowlist():
    assert validate_url("http://93.184.216.34/", resolve=False, enforce_allowlist=False)
    with pytest.raises(PolicyViolation):
        validate_url("http://93.184.216.34/", ["example.com"], resolve=False)


def test_allowlist_matches_domain_and_subdomains_only():
    allowed = ["example.com"]
    assert domain_allowed("example.com", allowed) and domain_allowed("shop.example.com", allowed)
    assert not domain_allowed("evilexample.com", allowed) and not domain_allowed("example.com.evil.io", allowed)
    for url in ("https://shop.example.com/x", "https://example.com"):
        assert validate_url(url, allowed, resolve=False)
    with pytest.raises(PolicyViolation) as ei:
        validate_url("https://evilexample.com", allowed, resolve=False)
    assert ei.value.code == "domain_not_allowed"


def test_dns_resolving_to_private_address_is_blocked(monkeypatch):
    import app.agent.policies as p
    monkeypatch.setattr(p, "resolve_host", lambda host, port=443: ["10.0.0.7"])
    with pytest.raises(PolicyViolation) as ei:
        validate_url("https://rebind.example.com/", ["rebind.example.com"])
    assert ei.value.code == "blocked_ip"
    monkeypatch.setattr(p, "resolve_host", lambda host, port=443: ["93.184.216.34", "127.0.0.1"])   # mixed answers
    with pytest.raises(PolicyViolation):
        validate_url("https://rebind.example.com/", ["rebind.example.com"])


@pytest.mark.parametrize("ip,expected", [("8.8.8.8", True), ("1.1.1.1", True), ("127.0.0.1", False), ("10.0.0.1", False),
                                         ("169.254.169.254", False), ("::1", False), ("::ffff:10.0.0.1", False), ("2606:4700::1111", True), ("garbage", False)])
def test_is_public_ip(ip, expected):
    assert is_public_ip(ip) is expected


def test_private_allowed_only_when_explicit():
    assert validate_url("http://127.0.0.1:5000/", ["127.0.0.1"], allow_private=True)
    pol = NavPolicy(("example.com",))
    assert not pol.link_permitted("https://other.org/x") and pol.link_permitted("https://a.example.com/x")


def test_parse_domain_list():
    assert parse_domain_list("Example.com, *.shop.io\nhttps://foo.org/path") == ["example.com", "shop.io", "foo.org"]
    for bad in ("localhost", "10.0.0.1", "intranet.local", "not a domain!", "a..b.com"):
        with pytest.raises(PolicyViolation):
            parse_domain_list(bad)


def test_effective_domains_always_include_start_host():
    assert effective_allowed_domains("https://shop.example.com/x", ["cdn.example.net"]) == ["cdn.example.net", "shop.example.com"]


def test_key_allowlist():
    assert normalize_key("enter") == "Enter" and normalize_key("PageDown") == "PageDown"
    for bad in ("Control+A", "F12", "Alt+F4", "Meta+R", "Delete"):
        with pytest.raises(PolicyViolation):
            normalize_key(bad)


@pytest.mark.parametrize("text,expected", [
    ("Buy now", "purchase"), ("Place your order", "purchase"), ("Pay now", "purchase"), ("Delete account", "deleting"),
    ("Send message", "sending"), ("Change password", "security"), ("Upload file", "uploading"), ("I agree and continue", "legal"),
    ("Search", None), ("Next page", None), ("Add to wishlist", None), ("Details", None)])
def test_risk_classification(text, expected):
    r = classify_risk("click", [text])
    assert (expected is None and r is None) or (expected and expected in r)


def test_submit_in_sensitive_form_is_risky_but_search_is_not():
    assert "sensitive" in classify_risk("click", ["Continue"], {"tag": "button", "type": "submit", "form_sensitive": True})
    assert classify_risk("click", ["Search"], {"tag": "button", "type": "submit", "form_sensitive": False}) is None
    assert classify_risk("type_text", ["Delete"]) is None   # typing a word is not a consequential click
