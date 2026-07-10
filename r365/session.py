import os
import logging

from dotenv import load_dotenv
from playwright.sync_api import Page

load_dotenv()

R365_USER   = os.getenv("R65_USER")
R365_PASS   = os.getenv("R65_PASS")
R365_URL    = os.getenv("R365_URL", "https://ayg.restaurant365.com")
PROFILE_DIR = os.path.expanduser("~/.r365_browser_profile")

log = logging.getLogger(__name__)


def _dismiss_chrome_dialogs(page: Page) -> None:
    try:
        ok_btn = page.locator(
            'div[role="dialog"] button:has-text("OK"), '
            'div[role="alertdialog"] button:has-text("OK")'
        ).first
        if ok_btn.count() > 0:
            ok_btn.click(timeout=2_000)
            log.info("Dismissed Chrome dialog (OK clicked)")
            page.wait_for_timeout(500)
    except Exception:
        pass


# OIDC handshake cookies that ASP.NET Core creates on every login attempt and is
# supposed to delete once the /signin-oidc callback completes. Aborted/failed
# logins leave them behind; they accumulate run-over-run until the cookie header
# overflows nginx's buffer and R365 answers every request with
# "400 Bad Request — Request Header Or Cookie Too Large". They carry no session
# state, so pruning them is always safe. (R365 issues its auth cookie as a session
# cookie, so it never reaches disk and every run logs in afresh — one nonce per
# run, which is exactly what this prune has to keep up with.)
# Matched case-insensitively: ASP.NET Core ships ".AspNetCore.OpenIdConnect.Nonce."
# with a capital N, so these are kept lowercase and compared against name.lower().
_TRANSIENT_COOKIE_PREFIXES = (".aspnetcore.correlation", ".aspnetcore.openidconnect.nonce")


def _page_is_cookie_400(page: Page) -> bool:
    """True if the current page is nginx's 'Request Header Or Cookie Too Large' 400."""
    try:
        txt = page.inner_text("body", timeout=2_000)
    except Exception:
        return False
    return "Cookie Too Large" in txt or ("400 Bad Request" in txt and "nginx" in txt.lower())


def _prune_transient_cookies(context) -> int:
    """Drop accumulated OIDC correlation/nonce cookies; keep everything else
    (notably the auth/session cookie). Returns how many were removed."""
    try:
        cookies = context.cookies()
    except Exception as e:
        log.warning("Could not read cookies to prune: %s", e)
        return 0
    keep = [c for c in cookies
            if not c.get("name", "").lower().startswith(_TRANSIENT_COOKIE_PREFIXES)]
    removed = len(cookies) - len(keep)
    if removed:
        try:
            context.clear_cookies()
            if keep:
                context.add_cookies(keep)
            log.info("Pruned %d stale OIDC handshake cookie(s); kept %d", removed, len(keep))
        except Exception as e:
            log.warning("Cookie prune failed (continuing): %s", e)
    return removed


def login_r365(page: Page, context, _retry: bool = True) -> None:
    log.info("Logging into R365 at: %s", page.url)
    page.wait_for_selector("#Username", timeout=30_000)
    page.fill("#Username", R365_USER)
    page.fill("#Password", R365_PASS)
    page.click('button[type="submit"]')

    # The OIDC handshake redirects: identity.restaurant365.com → /NetCore/signin-oidc
    # → the app. domcontentloaded fires on every hop (and on nginx error pages),
    # so waiting on it alone mistakes an intermediate/error page for a finished
    # login. Wait for the URL to actually settle off the identity provider and the
    # callback before declaring success.
    try:
        page.wait_for_url(
            lambda u: "identity.restaurant365.com" not in u and "/signin-oidc" not in u,
            timeout=60_000,
        )
    except Exception:
        log.warning("Login did not leave identity/callback URL within 60s — at: %s", page.url)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)
    _dismiss_chrome_dialogs(page)

    # Verify we genuinely reached the app — not an nginx 400 or a bounce back to
    # the login page. Raising here surfaces a real failure instead of a fake
    # "Login complete" that later dies as "DSS iframe not found".
    # The handshake mints a fresh nonce on top of whatever the jar already holds,
    # so the header can tip over nginx's buffer here even though the opening
    # goto() was fine. A full clear costs one re-login and always fits; _retry
    # bounds it to a single attempt so a genuinely broken login still fails loudly.
    if _page_is_cookie_400(page):
        if _retry:
            log.warning("nginx 400 'Cookie Too Large' after submit — clearing cookies, retrying login")
            context.clear_cookies()
            page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)
            return login_r365(page, context, _retry=False)
        raise RuntimeError("Login failed — nginx 400 'Cookie Too Large' after submit")
    if "identity.restaurant365.com" in page.url or "/Account/Login" in page.url.lower():
        raise RuntimeError(f"Login failed — still on the identity provider at {page.url}")
    log.info("Login complete — now at: %s", page.url)


def ensure_logged_in_r365(page: Page, context) -> None:
    # Prune the accumulated OIDC handshake cookies before touching R365 — this is
    # what was overflowing nginx's header buffer and breaking every request.
    _prune_transient_cookies(context)

    page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    _dismiss_chrome_dialogs(page)

    # If the jar is still oversized for any other reason, nginx 400s before R365
    # ever sees us — only a full clear recovers (costs one re-login).
    if _page_is_cookie_400(page):
        log.warning("nginx 400 'Cookie Too Large' — clearing ALL cookies and retrying")
        context.clear_cookies()
        page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)
        _dismiss_chrome_dialogs(page)

    if ("identity.restaurant365.com" in page.url
            or "login" in page.url.lower()
            or "logout" in page.url.lower()):
        log.info("Not logged in — logging in now (first time or session expired)")
        page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        try:
            login_r365(page, context)
        finally:
            # The login just created a new correlation/nonce pair — prune it now so
            # it never accumulates. A *failed* login orphans one too, hence finally.
            _prune_transient_cookies(context)
    else:
        log.info("Already logged in — reusing existing session at %s", page.url)
