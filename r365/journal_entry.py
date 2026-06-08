"""
Navigate R365 to the Daily Sales Summary Journal Entry and fill it from Revel data.

CLI usage:
    python -m r365.journal_entry --date 2026-05-30
"""

import os
import re
import logging
from datetime import date

from playwright.sync_api import sync_playwright, Page

from .session import R365_USER, R365_PASS, PROFILE_DIR, ensure_logged_in_r365

log = logging.getLogger(__name__)

DSS_URL = "https://ayg.restaurant365.com/react/sales-and-forecasting/legacy/DailySalesSummary"


# ─── JE grid helpers ──────────────────────────────────────────────────────────

def _find_je_table(frame):
    return frame.evaluate("""
        () => {
            const byId = document.querySelector('#DSSJournalEntryGrid table[role="grid"]');
            if (byId) return 'id';
            const activeCell = document.getElementById('DSSJournalEntryGrid_active_cell');
            if (activeCell) return 'active-cell';
            const byAria = document.querySelector('[aria-activedescendant*="DSSJournalEntryGrid"]');
            if (byAria) return 'aria';
            return 'none';
        }
    """)


def _fill_je_cell(frame, account_text: str, field_name: str, value: float) -> bool:
    # col indices confirmed by DOM inspection: col2=debit, col3=credit
    col_idx = 3 if field_name == "credit" else 2

    try:
        click_result = frame.evaluate(f"""
            (() => {{
                const scope = document.querySelector('#DSSJournalEntryGrid') || document;
                const rows = Array.from(scope.querySelectorAll('tr[role="row"]'));
                const row = rows.find(r => {{
                    const tds = r.querySelectorAll('td');
                    if (tds.length < 4) return false;
                    const clone = tds[1].cloneNode(true);
                    clone.querySelectorAll('select').forEach(s => s.remove());
                    return clone.textContent.trim().includes({repr(account_text)});
                }});
                if (!row) return 'row-not-found (searched ' + rows.length + ' rows)';
                const cell = row.querySelectorAll('td')[{col_idx}];
                if (!cell) return 'cell-not-found at col {col_idx}';
                cell.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                return 'clicked col{col_idx} in ' + rows.length + ' rows';
            }})()
        """)
        log.info("  Click '%s' col%d: %s", account_text, col_idx, click_result)
        if "not-found" in click_result:
            return False

        frame.wait_for_timeout(800)

        inp = frame.locator(f'input[name="{field_name}"]').first
        if inp.count() > 0:
            inp.fill(f"{value:.2f}")
            inp.press("Tab")
            frame.wait_for_timeout(400)
            log.info("  Filled '%s' %s = %.2f", account_text, field_name, value)
            return True

        inputs_debug = frame.evaluate(
            "() => Array.from(document.querySelectorAll('tr.k-grid-edit-row input'))"
            ".map(i => (i.name||'noname') + '=' + i.value).join(', ')"
        )
        log.warning(
            "  No input[name='%s'] for '%s' — edit-row inputs: %s",
            field_name, account_text, inputs_debug or "(none)",
        )
        return False
    except Exception as e:
        log.warning("  Could not fill '%s' %s: %s", account_text, field_name, e)
        return False


def fill_journal_entry(active, revel_values: dict, screenshot_path: str = "/tmp/r365_je_filled.png") -> None:
    """
    Fill R365 Journal Entry from Revel data.

    Expected revel_values keys:
        food_sales              → 4000-01 Food Sales (Credit)
        beverage_sales          → 4000-02 Beverage Sales (Credit)
        delivery_food_sales     → 4000-08 Food Delivery Sales (Credit)
        sales_tax               → 2240-000 Sales Tax Payable (Credit)
        credit_cards_ar         → 1200-000 A/R Credit Cards Receivable (Debit)
        uber_eats               → 1245-12 A/R-UberEats (Debit)
        doordash                → 1245-03 A/R-DoorDash (Debit)
        grubhub                 → 1245-08 A/R-GrubHub (Debit)
        undeposited_funds       → 1255 Undeposited Funds (Debit)
        comps                   → 4500-02 Comps (Debit)
        item_discounts          → 4500-01 Discounts (Debit)
        employee_discount       → 5000-17 Employee Discount (Debit)
        promotions              → 4500-03 Promotions (Debit)
        employee_tips           → 2301 Employee Tips Payable (Credit)
        cash_over_short         → 8000-06 Cash Over/Short (Debit if negative, Credit if positive)
        cash_over_short_sign    → 'debit' or 'credit'
    """
    active.wait_for_timeout(3_000)

    je_frame = None
    for f in active.frames:
        try:
            if f.locator('td:has-text("Food Delivery Sales")').count() > 0:
                visible = f.evaluate(
                    "() => document.documentElement.offsetHeight > 0 && "
                    "window.getComputedStyle(document.documentElement).visibility !== 'hidden'"
                )
                if visible:
                    je_frame = f
                    log.info("JE frame found (visible): %s", f.url)
                    break
        except Exception:
            continue
    if je_frame is None:
        je_frame = active
        log.warning("JE frame not found — trying main page")

    try:
        je_frame.locator('tr[role="row"]').first.wait_for(state="attached", timeout=15_000)
        log.info("JE grid rows detected")
    except Exception as e:
        log.warning("JE grid rows not detected within 15s: %s", e)

    log.info("Filling Journal Entry — values: %s", revel_values)

    def _fill(account, field, key):
        val = revel_values.get(key) or 0
        if val:
            _fill_je_cell(je_frame, account, field, float(val))

    # Account labels must match substrings of the exact td text in R365 DOM.

    # ── Credits ──────────────────────────────────────────────────────────────
    _fill("4000-01 - Food Sales",            "credit", "food_sales")
    _fill("4000-02 - Beverage Sales",         "credit", "beverage_sales")
    _fill("4000-08 - Food Delivery Sales",    "credit", "delivery_food_sales")
    _fill("2240-000 - Sales Tax Payable",     "credit", "sales_tax")

    # ── Debits ───────────────────────────────────────────────────────────────
    _fill("70250 - Credit Card Fees",                   "credit", "credit_card_fees")
    _fill("1200-000 - A/R Credit Cards Receivable",     "debit",  "credit_cards_ar")
    _fill("1245-12 - A/R-UberEats",                     "debit",  "uber_eats")
    _fill("1245-03 - A/R-DoorDash",                     "debit",  "doordash")
    _fill("1245-08 - A/R-GrubHub",                      "debit",  "grubhub")
    _fill("4500-01 - Discounts",                        "debit",  "item_discounts")
    _fill("4500-02 - Comps",                            "debit",  "comps")
    _fill("5000-17 - Employee Discount",                "debit",  "employee_discount")
    _fill("4500-03 - Promotions",                       "debit",  "promotions")
    # 2301 Employee Tips Payable (first/editable row — debit)
    _fill("2301 - Employee Tips Payable",               "debit",  "employee_tips")

    # NOTE: 1255 - Undeposited Funds, 8000-06 - Cash Over/Short, and the
    # second 2301 - Employee Tips Payable row are read-only — R365 fills them.

    active.screenshot(path=screenshot_path)
    log.info("Journal Entry fields filled — screenshot saved: %s", screenshot_path)


# ─── Navigation ───────────────────────────────────────────────────────────────

def go_to_daily_sales_summary(
    page: Page,
    target_date: date | None = None,
    context=None,
    location_name: str | None = None,
    revel_values: dict | None = None,
    screenshot_path: str = "/tmp/r365_je_after.png",
    before_screenshot_path: str = "/tmp/r365_je_before.png",
) -> None:
    log.info("Navigating directly to Daily Sales Summary...")
    page.goto(DSS_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(15_000)  # let legacy iframe fully settle
    log.info("DSS page loaded — at: %s", page.url)

    dss_frame = next(
        (f for f in page.frames if "DailySalesSummariesGrid" in f.url),
        None,
    )
    if not dss_frame:
        log.warning("DSS iframe not found — saving screenshot")
        page.screenshot(path="/tmp/r365_dss_list.png")
        return

    if target_date:
        date_str = target_date.strftime("%-m/%-d/%Y")
        log.info("Setting date filter to: %s", date_str)

        date_filter = dss_frame.locator('input[type="text"]').nth(4)
        date_filter.click(click_count=3)
        date_filter.fill(date_str)
        date_filter.press("Tab")
        page.wait_for_timeout(3_000)
        log.info("Date filter applied")
        page.screenshot(path="/tmp/r365_dss_filtered.png")

        if location_name:
            log.info("Clicking entity for location: %s", location_name)
            entity = dss_frame.locator(
                f'table tbody tr:has(td:nth-child(3):has-text("{location_name}")) td:nth-child(5)'
            ).first
        else:
            log.info("Clicking first entity Name cell...")
            entity = dss_frame.locator('table tbody tr td:nth-child(5)').first

        try:
            entity.wait_for(timeout=15_000)

            pages_before = len(context.pages) if context else 1
            entity.click()

            active = None
            for _ in range(15):  # poll up to 15 seconds
                page.wait_for_timeout(1_000)
                if context and len(context.pages) > pages_before:
                    active = context.pages[-1]
                    active.wait_for_load_state("domcontentloaded", timeout=30_000)
                    active.wait_for_timeout(2_000)
                    log.info("Entity opened in new tab — at: %s", active.url)
                    break
                if "DailySalesSummaryForm" in page.url or "#/form" in page.url:
                    active = page
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(2_000)
                    log.info("Entity opened in same tab — at: %s", page.url)
                    break
            if active is None:
                active = page
                log.warning("Entity navigation unclear after 15s — at: %s", page.url)

            active.screenshot(path="/tmp/r365_entity.png")
            active.wait_for_timeout(8_000)

            log.info("Looking for Journal Entry tab...")
            found_je = False
            for frame in active.frames + [active]:
                try:
                    je = frame.locator('li[role="tab"] span.k-link').filter(has_text="Journal Entry").first
                    je.wait_for(timeout=3_000)
                    je.scroll_into_view_if_needed()
                    je.click()
                    active.wait_for_timeout(5_000)
                    log.info("Journal Entry tab clicked — frame: %s", frame.url)
                    active.screenshot(path=before_screenshot_path)
                    found_je = True

                    if revel_values and found_je:
                        fill_journal_entry(active, revel_values, screenshot_path=screenshot_path)

                    break
                except Exception:
                    continue

            if not found_je:
                log.warning("Journal Entry tab not found — saving screenshot for diagnosis")
                active.screenshot(path="/tmp/r365_entity_no_je.png")

        except Exception as e:
            log.warning("Could not open entity: %s", e)
            try:
                page.screenshot(path="/tmp/r365_dss_list.png")
            except Exception:
                pass


# ─── Main entry (used by server.py) ──────────────────────────────────────────

def open_r365_journal_entry(
    target_date: date | None = None,
    location_name: str | None = None,
    revel_values: dict | None = None,
) -> dict:
    """
    Launch a headless browser using a persistent profile (so login is remembered),
    navigate to the DSS entity for the given date/location, open Journal Entry,
    and fill in values from Revel.
    """
    if not R365_USER or not R365_PASS:
        return {"error": "R65_USER and R65_PASS must be set in .env"}

    os.makedirs(PROFILE_DIR, exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", location_name or "unknown")
    date_str = target_date.strftime("%Y-%m-%d") if target_date else "nodate"
    before_filename = f"r365_je_{safe_name}_{date_str}_before.png"
    after_filename  = f"r365_je_{safe_name}_{date_str}_after.png"

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=True,
                viewport={"width": 1440, "height": 900},
                args=["--enable-features=DnsOverHttps"],
            )

            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in_r365(page, context)
            go_to_daily_sales_summary(
                page, target_date, context, location_name, revel_values,
                screenshot_path=f"/tmp/{after_filename}",
                before_screenshot_path=f"/tmp/{before_filename}",
            )

            active_pages = context.pages
            url = active_pages[-1].url if active_pages else DSS_URL
            return {
                "status": "ok",
                "url": url,
                "before_screenshot_filename": before_filename,
                "screenshot_filename": after_filename,
            }

    except Exception as exc:
        log.error("R365 navigation error: %s", exc)
        return {"error": str(exc)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Open R365 Daily Sales Summary Journal Entry")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    result = open_r365_journal_entry(target)
    print(result)
