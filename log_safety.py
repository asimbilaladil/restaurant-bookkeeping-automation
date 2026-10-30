"""
log_safety.py
=============
Sanitizes exception text before it reaches logs, the terminal, SSE events, or
the dss_runs.db `error` column.

Why this exists (Phase 4.1): a real Playwright `APIRequestContext` timeout
against Revel surfaced an exception whose message embeds the ENTIRE outgoing
HTTP request, including the `Cookie:` header (session id, CSRF token) and a
full JWT `id_token`. Every exception handler in this codebase that does
`str(exc)` or logs `"%s", exc` was, until this fix, passing that raw text
straight through to logs/stdout/the database unmodified.

Use `safe_exc(exc)` anywhere an exception from Revel or R365 network/browser
code might be logged, printed, or stored — never `str(exc)` directly for
those call sites.
"""

from __future__ import annotations

import re

# Each pattern either has one capture group (the safe prefix to keep, e.g.
# "Cookie: ") with everything after it redacted, or none (the whole match is
# replaced, e.g. a bare JWT appearing without a labeled header).
_LABELED_PATTERNS = [
    re.compile(r"(?i)(cookie:\s*)\S.*"),
    re.compile(r"(?i)(set-cookie:\s*)\S.*"),
    re.compile(r"(?i)(authorization:\s*)\S.*"),
    re.compile(r"(?i)(x-api-token:\s*)\S.*"),
    re.compile(r"(?i)(sessionid=)[^;&\s]+"),
    re.compile(r"(?i)(csrftoken=)[^;&\s]+"),
    re.compile(r"(?i)(id_token=)[^;&\s]+"),
    re.compile(r"(?i)(access_token=)[^;&\s]+"),
    re.compile(r"(?i)(refresh_token=)[^;&\s]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_.]+"),
]

# A bare JWT (three base64url segments separated by '.') with no preceding
# label — e.g. one embedded mid-URL as an id_token query param value already
# caught above, but also covers one appearing unlabeled in a stack trace.
_BARE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

_MAX_LEN = 300


def sanitize_text(text: str) -> str:
    """Redact known secret-shaped substrings from arbitrary text. Idempotent
    and safe to call on text that has no secrets in it at all."""
    out = text
    for pat in _LABELED_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
    out = _BARE_JWT.sub("[REDACTED-JWT]", out)
    return out


def safe_exc(exc: BaseException, max_len: int = _MAX_LEN) -> str:
    """Return a bounded, sanitized string for an exception.

    Never returns the raw `str(exc)` for network/browser exceptions, whose
    messages (Playwright APIRequestContext timeouts in particular) can embed
    the full outgoing request including Cookie and Authorization headers.
    Truncated in addition to sanitized, since a redacted-but-still-enormous
    header dump is still not useful log output.
    """
    sanitized = sanitize_text(str(exc))
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "...[truncated]"
    return f"{type(exc).__name__}: {sanitized}"
