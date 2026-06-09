"""
Fetch Revel Operations Reports.

CLI usage:
    python -m revel.operations --date 2026-05-30
    python -m revel.operations --date 2026-05-30 --establishments 40,15
"""

import os
import json
import logging
import argparse
from datetime import date, timedelta

from playwright.sync_api import sync_playwright, BrowserContext

from .establishments import DEFAULT_ESTABLISHMENTS
from .session import STATE_FILE, BASE_URL, ensure_logged_in

log = logging.getLogger(__name__)


def build_date_range(start_date: date) -> tuple[str, str]:
    end_date   = start_date + timedelta(days=1)
    range_from = start_date.strftime("%m/%d/%Y") + " 05:00:00"
    range_to   = end_date.strftime("%m/%d/%Y") + " 05:00:00"
    return range_from, range_to


def _switch_establishment(context: BrowserContext, establishment_id: int) -> None:
    csrftoken = next(
        (c["value"] for c in context.cookies() if c["name"] == "csrftoken"), ""
    )
    resp = context.request.post(
        f"{BASE_URL}/navigation/load_establishment_tree/",
        form={
            "establishments": str(establishment_id),
            "establishment":  str(establishment_id),
            "node_type":      "1",
            "node_id":        str(establishment_id),
            "location":       "/reports/operations/",
        },
        headers={"X-CSRFToken": csrftoken, "Referer": f"{BASE_URL}/dashboard/"},
    )
    if resp.status != 200:
        raise RuntimeError(f"Switch to est={establishment_id} failed: HTTP {resp.status}")
    result = resp.json()
    if result.get("errors"):
        raise RuntimeError(f"Switch to est={establishment_id} errors: {result['errors']}")
    log.info("  Switched session to est=%d", establishment_id)


def fetch_establishment_report(
    context: BrowserContext,
    establishment_id: int,
    start_date: date,
) -> dict:
    _switch_establishment(context, establishment_id)
    range_from, range_to = build_date_range(start_date)
    log.info("  Fetching est=%d  %s → %s", establishment_id, range_from, range_to)

    resp = context.request.get(
        f"{BASE_URL}/reports/operations/json/",
        params={
            "establishment":       establishment_id,
            "employee":            "",
            "online_app":          "",
            "online_app_type":     "",
            "online_app_platform": "",
            "show_unpaid":         1,
            "show_irregular":      1,
            "range_from":          range_from,
            "range_to":            range_to,
        },
    )

    if resp.status != 200:
        log.error("  HTTP %d for establishment %d", resp.status, establishment_id)
        return {"establishment_id": establishment_id, "error": f"HTTP {resp.status}", "data": None}

    try:
        data = resp.json()
        log.info("  est=%d — OK (%d bytes)", establishment_id, len(resp.body()))
        return {
            "establishment_id": establishment_id,
            "range_from": range_from,
            "range_to":   range_to,
            "error":      None,
            "data":       data,
        }
    except Exception as exc:
        log.error("  Failed to parse JSON for establishment %d: %s", establishment_id, exc)
        return {"establishment_id": establishment_id, "error": str(exc), "data": None}


def fetch_reports(
    start_date: date,
    establishments: list[int] = None,
    progress_callback=None,
) -> list[dict]:
    """
    Log into Revel and fetch the operations report for each establishment.

    Args:
        start_date: Date to fetch (range_to = start_date + 1 day).
        establishments: List of establishment IDs (default: all 11).
        progress_callback: Optional callable(establishment_id, status, result).

    Returns:
        List of result dicts, one per establishment.
    """
    from .session import REVEL_USER, REVEL_PASS
    if not REVEL_USER or not REVEL_PASS:
        raise ValueError("REVEL_USER and REVEL_PASS must be set in environment")

    if establishments is None:
        establishments = DEFAULT_ESTABLISHMENTS

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--enable-features=DnsOverHttps"])

        context = (
            browser.new_context(storage_state=STATE_FILE)
            if os.path.exists(STATE_FILE)
            else browser.new_context()
        )

        page = context.new_page()
        ensure_logged_in(context, page)
        page.close()

        for est_id in establishments:
            try:
                result = fetch_establishment_report(context, est_id, start_date)
            except Exception as exc:
                log.error("Unhandled error for establishment %d: %s", est_id, exc)
                result = {"establishment_id": est_id, "error": str(exc), "data": None}

            results.append(result)

            if progress_callback:
                status = "error" if result.get("error") else "success"
                progress_callback(est_id, status, result)

        browser.close()

    return results


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Fetch Revel Operations Reports")
    parser.add_argument("--date", type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--establishments", type=str, default=None, help="Comma-separated establishment IDs")
    parser.add_argument("--output", type=str, default=None, help="Save results to JSON file")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.date)
    estabs = DEFAULT_ESTABLISHMENTS
    if args.establishments:
        estabs = [int(x.strip()) for x in args.establishments.split(",")]

    log.info("Fetching reports for %s — establishments: %s", start_date, estabs)
    results = fetch_reports(start_date, estabs)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        log.info("Saved results to %s", args.output)
    else:
        print(json.dumps(results, indent=2, default=str))

    success = sum(1 for r in results if not r.get("error"))
    errors  = sum(1 for r in results if r.get("error"))
    log.info("Done — %d succeeded, %d failed", success, errors)


if __name__ == "__main__":
    main()
