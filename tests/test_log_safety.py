"""
log_safety.py: exception/text sanitization against the real Cookie/JWT
exposure discovered in Phase 4 testing.
"""

import log_safety


def test_sanitize_redacts_cookie_header_line():
    text = "Call log:\n  - cookie: csrftoken=abc123; sessionid=deadbeef1234567890\n"
    out = log_safety.sanitize_text(text)
    assert "csrftoken=abc123" not in out
    assert "sessionid=deadbeef1234567890" not in out
    assert "[redacted]" in out.lower()


def test_sanitize_redacts_authorization_header():
    text = "headers: authorization: Bearer sk-fake-example-not-real-12345"
    out = log_safety.sanitize_text(text)
    assert "sk-fake-example-not-real-12345" not in out
    assert "[redacted]" in out.lower()


def test_sanitize_redacts_bare_jwt():
    fake_jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.fakesigvaluehere123"
    out = log_safety.sanitize_text(f"token in url: {fake_jwt}")
    assert fake_jwt not in out
    assert "[REDACTED-JWT]" in out


def test_sanitize_redacts_id_token_query_param():
    text = "GET /x?id_token=eyFakeTokenValueXYZ123&other=1"
    out = log_safety.sanitize_text(text)
    assert "eyFakeTokenValueXYZ123" not in out
    assert "other=1" in out  # unrelated params survive


def test_sanitize_leaves_ordinary_text_unchanged():
    text = "Timeout 30000ms exceeded while fetching establishment 54"
    assert log_safety.sanitize_text(text) == text


def test_safe_exc_includes_exception_type():
    exc = TimeoutError("plain message, no secrets here")
    out = log_safety.safe_exc(exc)
    assert out.startswith("TimeoutError:")
    assert "plain message" in out


def test_safe_exc_redacts_and_truncates_long_message():
    # Non-secret filler that survives redaction, so truncation (not just
    # redaction) is what's actually being exercised here.
    huge_message = "detail: " + ("y" * 500) + " cookie: sessionid=realsecretvalue123"
    exc = RuntimeError(huge_message)
    out = log_safety.safe_exc(exc, max_len=50)
    assert "realsecretvalue123" not in out
    assert "y" * 500 not in out
    assert len(out) < 100  # type prefix + truncated body, well under the raw 500+ chars
    assert "truncated" in out


def test_safe_exc_full_realistic_playwright_timeout_message():
    """Models the exact shape of exception that triggered this fix: a
    Playwright APIRequestContext timeout embedding the full request."""
    msg = (
        "APIRequestContext.get: Timeout 30000ms exceeded.\n"
        "Call log:\n"
        "  - → GET https://laynes.revelup.com/reports/operations/json/?establishment=54\n"
        "    - cookie: csrftoken=realtoken123; sessionid=realsession456; "
        "id_token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sigvalue\n"
    )
    exc = TimeoutError(msg)
    out = log_safety.safe_exc(exc)
    assert "realtoken123" not in out
    assert "realsession456" not in out
    assert "eyJhbGciOiJSUzI1NiJ9" not in out
    assert "Timeout 30000ms exceeded" in out  # the useful part survives
