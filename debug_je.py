"""
debug_je.py - Inspect JE grid DOM structure: where data rows live and what inputs they have
"""
import os, logging
from datetime import date
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

R365_USER   = os.getenv("R65_USER")
R365_PASS   = os.getenv("R65_PASS")
R365_URL    = os.getenv("R365_URL", "https://ayg.restaurant365.com")
PROFILE_DIR = os.path.expanduser("~/.r365_browser_profile")
DSS_URL     = "https://ayg.restaurant365.com/react/sales-and-forecasting/legacy/DailySalesSummary"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

TARGET_DATE = date(2026, 6, 2)
LOCATION    = "LCF Airtex"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=True, viewport={"width": 1440, "height": 900}
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # Login if needed
    page.goto(R365_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    if "identity" in page.url or "login" in page.url.lower():
        page.wait_for_selector("#Username", timeout=30_000)
        page.fill("#Username", R365_USER)
        page.fill("#Password", R365_PASS)
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)
        log.info("Logged in")

    # Navigate to DSS
    page.goto(DSS_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(15_000)

    dss_frame = next((f for f in page.frames if "DailySalesSummariesGrid" in f.url), None)
    if not dss_frame:
        log.error("DSS iframe not found!")
        page.screenshot(path="/tmp/debug_dss.png")
        ctx.close()
        raise SystemExit(1)

    # Apply date filter and click entity
    date_str = TARGET_DATE.strftime("%-m/%-d/%Y")
    date_filter = dss_frame.locator('input[type="text"]').nth(4)
    date_filter.click(click_count=3)
    date_filter.fill(date_str)
    date_filter.press("Tab")
    page.wait_for_timeout(3_000)

    pages_before = len(ctx.pages)
    entity = dss_frame.locator(f'table tbody tr:has(td:nth-child(3):has-text("{LOCATION}")) td:nth-child(5)').first
    entity.wait_for(timeout=15_000)
    entity.click()

    active = None
    for _ in range(15):
        page.wait_for_timeout(1_000)
        if len(ctx.pages) > pages_before:
            active = ctx.pages[-1]
            active.wait_for_load_state("domcontentloaded", timeout=30_000)
            active.wait_for_timeout(2_000)
            break
    if active is None:
        active = page

    active.wait_for_timeout(8_000)

    # Click Journal Entry tab
    for frame in active.frames + [active]:
        try:
            je = frame.locator('li[role="tab"] span.k-link').filter(has_text="Journal Entry").first
            je.wait_for(timeout=3_000)
            je.click()
            active.wait_for_timeout(5_000)
            log.info("JE tab clicked")
            break
        except Exception:
            continue

    active.wait_for_timeout(3_000)

    # ── INSPECT #DSSJournalEntryGrid ──────────────────────────────────
    log.info("=" * 60)
    info = active.evaluate("""
        () => {
            const grid = document.querySelector('#DSSJournalEntryGrid');
            if (!grid) return {error: 'no #DSSJournalEntryGrid'};
            const allRows = grid.querySelectorAll('tr[role="row"]');
            const tbodyRows = grid.querySelectorAll('tbody tr[role="row"]');
            const theadRows = grid.querySelectorAll('thead tr[role="row"]');
            return {
                childCount: grid.children.length,
                childClasses: Array.from(grid.children).map(c => c.className.substring(0,40)).join(' | '),
                totalRoleRows: allRows.length,
                tbodyRoleRows: tbodyRows.length,
                theadRoleRows: theadRows.length,
                // Sample first 3 rows: td count + first input values
                rowSamples: Array.from(allRows).slice(0,5).map((r,i) => {
                    const tds = r.querySelectorAll('td');
                    const inputs = r.querySelectorAll('input');
                    return i + ': tds=' + tds.length +
                        ' inputs=[' + Array.from(inputs).map(inp => (inp.name||'?') + '=' + inp.value.substring(0,15)).join(',') + ']' +
                        ' td1text=' + (tds[1] ? tds[1].textContent.trim().substring(0,20) : '');
                })
            };
        }
    """)
    log.info("DSSJournalEntryGrid structure: %s", info)

    # ── FIND ROWS WITH ACCOUNT NAMES (whole document) ─────────────────
    log.info("=" * 60)
    account_rows = active.evaluate("""
        () => {
            const accounts = ['Sales Tax Payable', 'Credit Card Fees', '4500-01', '4500-02', '4500-03'];
            const allRows = document.querySelectorAll('tr[role="row"]');
            return Array.from(allRows).map((r, idx) => {
                const tds = r.querySelectorAll('td');
                if (tds.length < 2) return null;
                // Strip select options from col1 textContent
                const clone = tds[1].cloneNode(true);
                clone.querySelectorAll('select').forEach(s => s.remove());
                const col1text = clone.textContent.trim();
                const matched = accounts.find(a => col1text.includes(a));
                if (!matched) return null;
                // Is this row inside #DSSJournalEntryGrid?
                const inGrid = !!r.closest('#DSSJournalEntryGrid');
                // What inputs does col1 have?
                const col1inputs = Array.from(tds[1].querySelectorAll('input')).map(i => i.name + '=' + i.value.substring(0,15));
                // What container is this row in?
                const container = r.closest('[id]') ? r.closest('[id]').id : 'no-id-parent';
                return {idx, matched, inGrid, container, col1text: col1text.substring(0,30), col1inputs, tdCount: tds.length};
            }).filter(Boolean);
        }
    """)
    log.info("Account rows found: %s", account_rows)

    # ── INSPECT DATA ROW (click Sales Tax Payable row and see inputs) ──
    log.info("=" * 60)
    log.info("Clicking Sales Tax Payable row col2 (debit) to see edit inputs...")
    click_result = active.evaluate("""
        () => {
            // Find ALL rows (whole doc), look for Sales Tax Payable in col1 (stripped)
            const rows = Array.from(document.querySelectorAll('tr[role="row"]'));
            for (const r of rows) {
                const tds = r.querySelectorAll('td');
                if (tds.length < 4) continue;
                const clone = tds[1].cloneNode(true);
                clone.querySelectorAll('select').forEach(s => s.remove());
                if (clone.textContent.trim().includes('Sales Tax Payable')) {
                    // Click col3 (credit)
                    const creditCell = tds[3];
                    creditCell.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                    return 'clicked credit cell, row has ' + tds.length + ' cols, col1=' + clone.textContent.trim().substring(0,30);
                }
            }
            return 'row not found';
        }
    """)
    log.info("Click result: %s", click_result)
    active.wait_for_timeout(1_500)

    # What inputs appeared?
    inputs_after = active.evaluate("""
        () => {
            // Look specifically in k-grid-edit-row
            const editRow = document.querySelector('tr.k-grid-edit-row');
            if (!editRow) {
                // Fallback: all inputs in any row that just got inputs
                return 'no k-grid-edit-row; all inputs: ' +
                    Array.from(document.querySelectorAll('tr input[type="text"]'))
                        .filter(i => !i.readOnly && !i.disabled)
                        .map(i => (i.name||'?') + '=' + i.value.substring(0,15) + ' col=' +
                            (i.closest('td') ? Array.from(i.closest('tr').children).indexOf(i.closest('td')) : '?'))
                        .join(', ');
            }
            const tds = editRow.querySelectorAll('td');
            return 'edit-row tds=' + tds.length + '; inputs: ' +
                Array.from(editRow.querySelectorAll('input')).map(i =>
                    (i.name||'noname') + '=' + i.value.substring(0,15) +
                    ' col=' + Array.from(editRow.children).indexOf(i.closest('td'))
                ).join(', ');
        }
    """)
    log.info("Inputs after click: %s", inputs_after)

    active.screenshot(path="/tmp/debug_je_final.png")
    log.info("Screenshot: /tmp/debug_je_final.png")
    log.info("=" * 60)
    input("Press ENTER to close...")
    ctx.close()
