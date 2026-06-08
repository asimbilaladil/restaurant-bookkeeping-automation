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


def login_r365(page: Page) -> None:
    log.info("Logging into R365 at: %s", page.url)
    page.wait_for_selector("#Username", timeout=30_000)
    page.fill("#Username", R365_USER)
    page.fill("#Password", R365_PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)
    _dismiss_chrome_dialogs(page)
    log.info("Login complete — now at: %s", page.url)


def ensure_logged_in_r365(page: Page, context) -> None:
    page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    _dismiss_chrome_dialogs(page)

    if "identity.restaurant365.com" in page.url or "login" in page.url.lower():
        log.info("Not logged in — logging in now (first time or session expired)")
        login_r365(page)
    else:
        log.info("Already logged in — at %s", page.url)
