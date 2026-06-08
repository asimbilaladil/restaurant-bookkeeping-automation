import os
import logging

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

load_dotenv()

REVEL_USER = os.getenv("REVEL_USER")
REVEL_PASS = os.getenv("REVEL_PASS")
BASE_URL   = "https://laynes.revelup.com"
STATE_FILE = "/tmp/revel_session.json"

log = logging.getLogger(__name__)


def login_and_save(context: BrowserContext, page: Page) -> None:
    log.info("Logging into Revel...")
    page.goto(BASE_URL)
    page.wait_for_url("**authentication.revelup.com**", timeout=60000)
    page.wait_for_selector('input[name="username"]', timeout=60000)
    page.fill('input[name="username"]', REVEL_USER)
    page.click('button[type="submit"]')
    page.wait_for_selector('input[name="password"]', timeout=60000)
    page.fill('input[name="password"]', REVEL_PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=60000)
    context.storage_state(path=STATE_FILE)
    log.info("Login successful — session saved to %s", STATE_FILE)


def ensure_logged_in(context: BrowserContext, page: Page) -> None:
    if not os.path.exists(STATE_FILE):
        login_and_save(context, page)
        return

    page.goto(BASE_URL, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if "authentication" in page.url or "login" in page.url:
        log.info("Session expired — re-logging in")
        login_and_save(context, page)
    else:
        log.info("Session valid")
