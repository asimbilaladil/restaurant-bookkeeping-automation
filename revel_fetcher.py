"""
revel_fetcher.py
================
Logs into Revel (with session caching) and fetches the Operations Report
for each establishment for a given date range.

The operations report URL format:
  https://laynes.revelup.com/reports/operations/json/
    ?establishment=<id>
    &range_from=MM/DD/YYYY%2005:00:00
    &range_to=MM/DD/YYYY%2005:00:00      ← always start_date+1 day
    &show_unpaid=1
    &show_irregular=1

Usage (standalone):
    python revel_fetcher.py --date 2026-05-30
    python revel_fetcher.py --date 2026-05-30 --establishments 40,15

Environment variables (.env):
    REVEL_USER    Revel login username
    REVEL_PASS    Revel login password
"""

import os
import sys
import json
import logging
import argparse
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, BrowserContext, Page

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────
REVEL_USER = os.getenv("REVEL_USER")
REVEL_PASS = os.getenv("REVEL_PASS")
BASE_URL   = "https://laynes.revelup.com"
STATE_FILE = "/tmp/revel_session.json"

DEFAULT_ESTABLISHMENTS = [32, 14, 48, 7, 6, 25, 36, 26, 20, 40, 15]

# Revel establishment ID → R365 Location name (used to match DSS entity)
ESTABLISHMENT_NAMES = {
    32: "LCF Airtex",
    14: "LCF Beaumont",
    48: "LCF Downtown Houston",
    7:  "LCF Ella",
    6:  "LCF Katy",
    25: "LCF Mission Bend",
    36: "LCF Missouri City",
    26: "LCF Nederland",
    20: "LCF Pasadena",
    40: "LCF Rosenberg",
    15: "LCF Shepherd",
}

# ─── Logging ─────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)


# ─── Login ───────────────────────────────────────────────────────────────────
def login_and_save(context: BrowserContext, page: Page) -> None:
    """Login to Revel and save browser session to STATE_FILE."""
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
    """Check if session is valid, re-login if expired."""
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


# ─── Date helpers ────────────────────────────────────────────────────────────
def build_date_range(start_date: date) -> tuple[str, str]:
    """
    Returns (range_from, range_to) in Revel's expected format:
      MM/DD/YYYY 05:00:00  →  next_day 05:00:00
    Both are passed as URL query params so spaces/colons get encoded by Playwright.
    """
    end_date = start_date + timedelta(days=1)
    range_from = start_date.strftime("%m/%d/%Y") + " 05:00:00"
    range_to   = end_date.strftime("%m/%d/%Y") + " 05:00:00"
    return range_from, range_to


# ─── Fetch one establishment ──────────────────────────────────────────────────
def fetch_establishment_report(
    context: BrowserContext,
    establishment_id: int,
    start_date: date,
) -> dict:
    """
    Fetch the operations JSON report for a single establishment.
    Returns the parsed JSON dict, or an error dict.
    """
    range_from, range_to = build_date_range(start_date)

    log.info("  Fetching est=%d  %s → %s", establishment_id, range_from, range_to)

    resp = context.request.get(
        f"{BASE_URL}/reports/operations/json/",
        params={
            "establishment": establishment_id,
            "range_from":    range_from,
            "range_to":      range_to,
            "show_unpaid":   1,
            "show_irregular": 1,
        },
    )

    if resp.status != 200:
        log.error("  HTTP %d for establishment %d", resp.status, establishment_id)
        return {
            "establishment_id": establishment_id,
            "error": f"HTTP {resp.status}",
            "data": None,
        }

    try:
        data = resp.json()
        log.info("  est=%d — OK (%d bytes)", establishment_id, len(resp.body()))
        return {
            "establishment_id": establishment_id,
            "range_from": range_from,
            "range_to": range_to,
            "error": None,
            "data": data,
        }
    except Exception as exc:
        log.error("  Failed to parse JSON for establishment %d: %s", establishment_id, exc)
        return {
            "establishment_id": establishment_id,
            "error": str(exc),
            "data": None,
        }


# ─── Main fetch function ─────────────────────────────────────────────────────
def fetch_reports(
    start_date: date,
    establishments: list[int] = None,
    progress_callback=None,
) -> list[dict]:
    """
    Logs into Revel and fetches the operations report for each establishment.

    Args:
        start_date: The date to fetch data for (range_to = start_date + 1 day)
        establishments: List of establishment IDs (default: all 11)
        progress_callback: Optional callable(establishment_id, status, result)
                           called after each establishment completes.

    Returns:
        List of result dicts, one per establishment.
    """
    if not REVEL_USER or not REVEL_PASS:
        raise ValueError("REVEL_USER and REVEL_PASS must be set in environment")

    if establishments is None:
        establishments = DEFAULT_ESTABLISHMENTS

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--enable-features=DnsOverHttps"],
        )

        # Load saved session if available
        if os.path.exists(STATE_FILE):
            context = browser.new_context(storage_state=STATE_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()
        ensure_logged_in(context, page)
        page.close()

        for est_id in establishments:
            try:
                result = fetch_establishment_report(context, est_id, start_date)
            except Exception as exc:
                log.error("Unhandled error for establishment %d: %s", est_id, exc)
                result = {
                    "establishment_id": est_id,
                    "error": str(exc),
                    "data": None,
                }

            results.append(result)

            if progress_callback:
                status = "error" if result.get("error") else "success"
                progress_callback(est_id, status, result)

        browser.close()

    return results


# ─── CLI entry point ─────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Fetch Revel Operations Reports")
    parser.add_argument(
        "--date", type=str, required=True,
        help="Start date YYYY-MM-DD (range_to will be +1 day)"
    )
    parser.add_argument(
        "--establishments", type=str, default=None,
        help="Comma-separated establishment IDs (default: all 11)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Save results to this JSON file"
    )
    args = parser.parse_args()

    start_date = date.fromisoformat(args.date)

    establishments = DEFAULT_ESTABLISHMENTS
    if args.establishments:
        establishments = [int(x.strip()) for x in args.establishments.split(",")]

    log.info("Fetching reports for %s — establishments: %s", start_date, establishments)

    results = fetch_reports(start_date, establishments)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        log.info("Saved results to %s", args.output)
    else:
        print(json.dumps(results, indent=2, default=str))

    # Summary
    success = sum(1 for r in results if not r.get("error"))
    errors  = sum(1 for r in results if r.get("error"))
    log.info("Done — %d succeeded, %d failed", success, errors)


if __name__ == "__main__":
    main()
