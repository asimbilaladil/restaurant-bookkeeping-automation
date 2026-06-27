"""
Navigate R365: My Reports → Accounting tab → GL Account Detail Export Customize
→ set Account to the selected receivable account (default 1245-12 A/R-UberEats;
caller picks one of UberEats / DoorDash / GrubHub / EzCater / Lunchdrop / Fooda /
Event / Credit Cards / Undeposited Funds / Square).
"""

import json
import logging
import time
import uuid
from datetime import date as date_type
from pathlib import Path

from playwright.sync_api import sync_playwright, Frame

from .session import PROFILE_DIR, ensure_logged_in_r365

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"

log = logging.getLogger(__name__)

MY_REPORTS_URL = "https://ayg.restaurant365.com/react/reports-management/legacy/MyReports"
TARGET_REPORT  = "GL Account Detail Export"
BTN_ID         = f"customizeViewer-{TARGET_REPORT}"


_EXCLUDE_FRAME_DOMAINS = (
    "document360.io",     # Help widget
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "fullstory.com",
    "intercom.io",
    "hotjar.com",
)


def _find_customize_ctx(page):
    """Search main page + all frames for the Customize button.
    Returns (ctx, info_dict). ctx may be a Page or Frame; None if nothing found."""
    contexts = [("main", page)]
    for f in page.frames:
        if f == page.main_frame:
            continue
        url = f.url or ""
        if any(dom in url for dom in _EXCLUDE_FRAME_DOMAINS):
            continue
        contexts.append((url[:80], f))

    for label, ctx in contexts:
        try:
            r = ctx.evaluate(f"""
                () => {{
                    const byId = !!document.getElementById({repr(BTN_ID)});
                    const btns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.textContent.trim() === 'Customize').length;
                    const links = Array.from(document.querySelectorAll('a, [role="button"]'))
                        .filter(e => e.textContent.trim() === 'Customize').length;
                    return {{ byId, btns, links }};
                }}
            """)
            if r.get("byId") or r.get("btns", 0) > 0 or r.get("links", 0) > 0:
                log.info("Customize found in [%s]: %s", label, r)
                return ctx, r
        except Exception as ex:
            log.debug("evaluate failed for [%s]: %s", label, str(ex)[:60])
    return None, {}


def _set_datepicker(ctx, page, placeholder: str, date_str: str) -> str:
    """Fill an AngularJS Material datepicker input (format: M/D/YYYY)."""
    try:
        inp = ctx.locator(f'input.md-datepicker-input[placeholder="{placeholder}"]')
        inp.click(timeout=5_000)
        inp.select_text()
        inp.type(date_str, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(600)
        return f"set-{placeholder}-to-{date_str}"
    except Exception as e:
        log.warning("Datepicker [%s] failed: %s", placeholder, e)
        return f"failed: {e}"


def _click_button_group(ctx, label_text: str, option_text: str) -> str:
    """Click a specific option inside an AngularJS ButtonGroup by its label span text."""
    result = ctx.evaluate(f"""
        () => {{
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const labelSpan = spans.find(s => s.textContent.trim() === {repr(label_text)});
            if (!labelSpan) return 'label-not-found: ' + {repr(label_text)};
            let parent = labelSpan;
            for (let i = 0; i < 10; i++) {{
                parent = parent.parentElement;
                if (!parent) return 'parent-exhausted';
                const btns = Array.from(parent.querySelectorAll('button.groupX'));
                if (btns.length === 0) continue;
                const target = btns.find(b => b.textContent.trim() === {repr(option_text)});
                if (target) {{ target.click(); return 'clicked: ' + {repr(option_text)}; }}
                return 'option-not-found: ' + {repr(option_text)} + ' in [' + btns.map(b => b.textContent.trim()).join(', ') + ']';
            }}
            return 'not-found';
        }}
    """)
    log.info("ButtonGroup [%s=%s]: %s", label_text, option_text, result)
    return result


def _snap(page, prefix: str) -> str:
    """Take a screenshot, save to /tmp, return filename (empty string on failure)."""
    name = f"{prefix}_{uuid.uuid4().hex[:6]}.png"
    try:
        page.screenshot(path=f"/tmp/{name}", full_page=False)
    except Exception as e:
        log.warning("Screenshot [%s] failed: %s", prefix, e)
        return ""
    return name


def _select_entity_via_autocomplete(viewer_page, entity: str) -> None:
    """Change the Legal Entity filter on the ReportViewer customize panel.

    The ReportViewer uses an md-autocomplete (not a modal dialog) for the
    Filter parameter. We click the autocomplete input, clear it, type the
    entity name, wait for the dropdown, then click the matching option.
    """
    log.info("Selecting entity %r via autocomplete", entity)

    # Click the Filter autocomplete input — the label "Filter" (not "Filter By")
    # lives in a div[style*="min-width"] left-side label column.
    click_result = viewer_page.evaluate("""
        () => {
            // Find the Filter label (not Filter By) to locate the right row
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterLabel = spans.find(s =>
                s.textContent.trim() === 'Filter' &&
                s.closest('div[style*="min-width"]')
            );
            if (!filterLabel) return 'label-not-found';
            let node = filterLabel;
            for (let i = 0; i < 10; i++) {
                node = node.parentElement;
                if (!node) break;
                if (node.tagName === 'SECTION') {
                    const inp = node.querySelector('input[type="search"]');
                    if (inp) {
                        inp.click();
                        inp.select();
                        return 'input-clicked:' + inp.id;
                    }
                }
            }
            return 'input-not-found';
        }
    """)
    log.info("Filter autocomplete click: %s", click_result)
    viewer_page.wait_for_timeout(500)

    # Clear the current value and type the new entity
    try:
        inp = viewer_page.locator('input[type="search"]').nth(3)  # input-4 = 4th search input
        inp.click(timeout=3_000)
        inp.select_text()
        inp.fill("")
        inp.type(entity, delay=60)
        log.info("Typed %r into filter autocomplete", entity)
    except Exception as e:
        log.warning("Autocomplete locator failed (%s) — keyboard fallback", e)
        viewer_page.keyboard.select_all()
        viewer_page.keyboard.type(entity, delay=60)
    viewer_page.wait_for_timeout(2_000)

    # Click the matching item in the autocomplete dropdown
    select_result = viewer_page.evaluate(f"""
        () => {{
            const label = {repr(entity)};
            // md-autocomplete dropdown items
            const candidates = [
                ...document.querySelectorAll('li[md-autocomplete-list-item], md-autocomplete-parent-scope li'),
                ...document.querySelectorAll('ul[role="presentation"] li'),
            ];
            const match = candidates.find(li => li.textContent.trim().includes(label));
            if (match) {{ match.click(); return 'dropdown-item-clicked: ' + match.textContent.trim().slice(0,50); }}
            // Broader fallback: any visible li containing the entity text
            const anyLi = Array.from(document.querySelectorAll('li')).find(li =>
                li.offsetParent !== null && li.textContent.trim().includes(label)
            );
            if (anyLi) {{ anyLi.click(); return 'li-fallback: ' + anyLi.textContent.trim().slice(0,50); }}
            return 'not-found';
        }}
    """)
    log.info("Entity autocomplete select (%s): %s", entity, select_result)
    viewer_page.wait_for_timeout(1_500)

    # Verify the filter input now shows the selected entity
    verify = viewer_page.evaluate(f"""
        () => {{
            const spans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterLabel = spans.find(s =>
                s.textContent.trim() === 'Filter' &&
                s.closest('div[style*="min-width"]')
            );
            if (!filterLabel) return 'label-not-found';
            let node = filterLabel;
            for (let i = 0; i < 10; i++) {{
                node = node.parentElement;
                if (!node) break;
                if (node.tagName === 'SECTION') {{
                    const inp = node.querySelector('input[type="search"]');
                    return inp ? 'filter-value: ' + inp.value : 'input-not-found';
                }}
            }}
            return 'section-not-found';
        }}
    """)
    log.info("Filter value after select: %s", verify)


def _select_legal_entity(ctx, page, entity: str, skip_filter_type: bool = False) -> None:
    """Open Legal Entity filter dialog, clear existing selections, select entity, confirm OK.

    skip_filter_type: set True when Filter By is already set to 'Legal Entity'
    (e.g. subsequent entities on the ReportViewer page) so we don't re-click
    FILTER BY, which would reset the current filter value to empty.
    """

    if not skip_filter_type:
        # ── Step 12: open Legal Entity filter on Customize panel ─────────
        # Try direct "LEGAL ENTITY" button first; fall back to "FILTER BY"
        log.info("Opening Legal Entity filter")
        fbtn = ctx.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('button'))
                    .filter(b => b.offsetParent !== null);
                let target = btns.find(b => b.textContent.trim().toUpperCase() === 'LEGAL ENTITY');
                if (target) { target.scrollIntoView({block:'center'}); target.click(); return 'legal-entity-direct'; }
                target = btns.find(b => {
                    const t = b.textContent.trim().toUpperCase();
                    return t === 'FILTER BY' || t.startsWith('FILTER BY');
                });
                if (target) { target.scrollIntoView({block:'center'}); target.click(); return 'filter-by-clicked'; }
                return 'not-found';
            }
        """)
        log.info("Filter button: %s", fbtn)
        page.wait_for_timeout(2_000)

        if fbtn == "filter-by-clicked":
            log.info("Clicking 'Legal Entity' option")
            le = ctx.evaluate("""
                () => {
                    const btn = document.querySelector('button[aria-label="Legal Entity"]');
                    if (!btn) return 'not-found';
                    btn.scrollIntoView({block:'center'});
                    btn.click();
                    return 'legal-entity-clicked';
                }
            """)
            log.info("Legal Entity option: %s", le)
            page.wait_for_timeout(2_500)
    else:
        log.info("Skipping Filter By step (already set to Legal Entity on viewer page)")

    # ── Step 12c: open the FILTER multi-select dialog ─────────────────
    # On MyReports panel the row has id="Filter"; on the ReportViewer
    # panel it doesn't — fall back to finding the "Filter" label span.
    log.info("Opening Filter (entity multi-select) dialog")
    open_filter = ctx.evaluate("""
        () => {
            // Try by id first (works on MyReports customize panel)
            const filterRow = document.getElementById('Filter');
            if (filterRow) {
                const btn = filterRow.querySelector('button');
                if (btn) {
                    btn.scrollIntoView({block:'center'});
                    btn.click();
                    return 'filter-button-clicked';
                }
                const section = filterRow.querySelector('section[role="button"]');
                if (section) { section.click(); return 'filter-section-clicked'; }
            }
            // Fallback: find span.spanTop with text "Filter" (not "Filter By")
            // and click its ancestor section[role="button"] — works on ReportViewer.
            const labelSpans = Array.from(document.querySelectorAll('span.spanTop'));
            const filterSpan = labelSpans.find(
                s => s.textContent.trim() === 'Filter'
            );
            if (filterSpan) {
                let node = filterSpan;
                for (let i = 0; i < 10; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    if (node.tagName === 'SECTION' && node.getAttribute('role') === 'button') {
                        node.scrollIntoView({block:'center'});
                        node.click();
                        return 'filter-span-section-clicked';
                    }
                }
            }
            return 'filter-not-found';
        }
    """)
    log.info("Filter open: %s", open_filter)
    page.wait_for_timeout(2_500)

    # ── Step 13: clear any existing legal entity selections ──────────
    log.info("Clearing legal entity selections (Select All x2)")
    for i in (1, 2):
        res = ctx.evaluate("""
            () => {
                const sa = document.querySelector('md-checkbox[ng-model="selectAll"]');
                if (!sa) return 'select-all-not-found';
                sa.scrollIntoView({block:'center'});
                sa.click();
                return 'select-all-clicked aria-checked=' + sa.getAttribute('aria-checked');
            }
        """)
        log.info("Entity Select All click %d: %s", i, res)
        page.wait_for_timeout(1_000)

    # ── Step 14: type the chosen legal entity in search input ─────────
    log.info("Typing %r in entity search", entity)
    try:
        if ctx != page:
            inp = ctx.locator('input:not([disabled]):not([type="hidden"])').last
        else:
            inp = page.locator('input:not([disabled]):not([type="hidden"])').last
        inp.click(timeout=5_000)
        inp.fill("")
        inp.type(entity, delay=60)
    except Exception as e:
        log.warning("Entity search input failed (%s) — keyboard fallback", e)
        page.keyboard.type(entity, delay=60)
    page.wait_for_timeout(2_500)

    # ── Step 15: click the entity's wrapper button ───────────────────
    log.info("Selecting %r", entity)
    le_click = ctx.evaluate(f"""
        () => {{
            const label = {repr(entity)};
            const btn = document.querySelector(`button[aria-label="${{label}}"]`);
            if (btn) {{
                btn.scrollIntoView({{block:'center'}});
                btn.click();
                return 'wrapper-button-clicked';
            }}
            const cb = document.querySelector(
                `md-checkbox[aria-label="${{label}} "], ` +
                `md-checkbox[aria-label="${{label}}"]`
            );
            if (cb) {{ cb.scrollIntoView({{block:'center'}}); cb.click(); return 'md-checkbox-clicked'; }}
            return 'not-found';
        }}
    """)
    log.info("Entity click (%s): %s", entity, le_click)
    page.wait_for_timeout(1_500)

    # ── Step 16: click OK on entity dialog ───────────────────────────
    log.info("Clicking OK on entity dialog")
    le_ok = ctx.evaluate(r"""
        () => {
            const ok = document.querySelector("button[ng-click=\"closeDialog('OK')\"]");
            if (ok) { ok.scrollIntoView({block:'center'}); ok.click(); return 'ok-ng-click'; }
            const actions = document.querySelector('md-dialog-actions');
            if (actions) {
                const btn = Array.from(actions.querySelectorAll('button'))
                    .find(b => b.textContent.trim().toUpperCase() === 'OK');
                if (btn) { btn.click(); return 'ok-dialog-actions'; }
            }
            return 'ok-not-found';
        }
    """)
    log.info("Entity OK: %s", le_ok)
    page.wait_for_timeout(2_000)


DEFAULT_ACCOUNT = "1245-12 - A/R-UberEats"


def open_report_viewer(
    legal_entities: list = None,
    start_date: date_type | None = None,
    end_date: date_type | None = None,
    show_unapproved: str = "Yes",
    calendar: str = "Fiscal",
    account: str | None = None,
    progress_cb=None,
    entity_cb=None,
) -> dict:
    # Back-compat: allow single string passed positionally
    if isinstance(legal_entities, str):
        legal_entities = [legal_entities]
    if not legal_entities:
        legal_entities = ["LCF Airtex LLC"]

    # Receivable account to select in the picker (default: UberEats)
    account = (account or "").strip() or DEFAULT_ACCOUNT

    def _emit(message: str, screenshot: str = ""):
        log.info("[rv] %s", message)
        if progress_cb:
            progress_cb(message, screenshot or None)

    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
            accept_downloads=True,
        )
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            ensure_logged_in_r365(page, browser)
            _emit("Logged into R365")

            # ── Step 1: wait for React sidebar ───────────────────────────────
            log.info("Waiting for React sidebar…")
            for i in range(30):
                page.wait_for_timeout(1_000)
                if "/react/" in page.url:
                    has_nav = page.evaluate(
                        "() => !!document.querySelector('aside, nav, [class*=\"sidebar\"]')"
                    )
                    if has_nav:
                        log.info("Sidebar ready after %ds at %s", i + 1, page.url)
                        break

            page.wait_for_timeout(2_000)

            # ── Step 2: navigate via sidebar link (client-side React Router) ─
            log.info("Clicking Reports sidebar link")
            nav_result = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const byHref = links.find(a =>
                        (a.href || '').includes('reports-management') ||
                        (a.href || '').includes('MyReports')
                    );
                    if (byHref) { byHref.click(); return 'href: ' + byHref.href; }
                    const byLabel = document.querySelector('[aria-label="Reports"] a, a[aria-label="Reports"]');
                    if (byLabel) { byLabel.click(); return 'aria-label'; }
                    return 'not-found';
                }
            """)
            log.info("Sidebar nav: %s", nav_result)

            if "not-found" in str(nav_result):
                log.warning("Sidebar link not found — falling back to direct goto")
                try:
                    page.goto(MY_REPORTS_URL, timeout=60_000, wait_until="commit")
                except Exception as e:
                    log.warning("goto warning: %s", e)
            else:
                # React Router navigation is client-side — wait for URL to settle
                # rather than triggering a hard goto (which breaks AngularJS bootstrap)
                for _ in range(20):
                    page.wait_for_timeout(500)
                    if "MyReports" in page.url or "reports-management" in page.url:
                        break
                if "MyReports" not in page.url and "reports-management" not in page.url:
                    log.warning("URL didn't change after sidebar click (at %s) — waiting longer", page.url)
                    page.wait_for_timeout(3_000)

            log.info("At: %s", page.url)
            page.wait_for_timeout(5_000)

            # ── Step 3: log all frames so we can see what's available ────────
            log.info("Total frames: %d", len(page.frames))
            for f in page.frames:
                log.info("  frame: %s", (f.url or "<blank>")[:120])

            # ── Step 3a: click Accounting TAB (inside Reports page, NOT sidebar) ──
            # The Reports page has an AngularJS Material tab bar:
            #   <md-tabs>...<li aria-label="Accounting" name="Accounting">
            # We MUST avoid clicking the sidebar "Accounting" nav item which
            # navigates to /react/accounting/legacy/AllTransactions.
            log.info("Waiting for Accounting TAB (md-tabs nav, NOT sidebar)…")
            tab_clicked = False
            for i in range(60):
                page.wait_for_timeout(1_000)
                contexts = [("main", page)] + [
                    ((f.url or "<blank>")[:60], f) for f in page.frames
                    if f != page.main_frame
                    and not any(d in (f.url or "") for d in _EXCLUDE_FRAME_DOMAINS)
                ]
                for label, c in contexts:
                    try:
                        r = c.evaluate("""
                            () => {
                                // Strict selectors: AngularJS Material md-tabs only.
                                // These never match the React sidebar.
                                const candidates = [
                                    'md-tabs li[name="Accounting"] a',
                                    'md-tabs li[aria-label="Accounting"] a',
                                    'md-tabs-canvas li[name="Accounting"] a',
                                    'md-tabs-canvas li[aria-label="Accounting"] a',
                                    'li[name="Accounting"][role="tab"] a',
                                    'li[name="Accounting"]._md-nav-item a',
                                    '[role="tablist"] li[name="Accounting"] a',
                                    'ul._md-nav-bar li[name="Accounting"] a',
                                ];
                                for (const sel of candidates) {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null) {
                                        el.scrollIntoView({block:'center'});
                                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                        el.click();
                                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                        return 'clicked: ' + sel;
                                    }
                                }
                                // Restricted fallback: only li[name="Accounting"] anywhere — never <a> alone.
                                // This still excludes the React sidebar (which uses <a> directly).
                                const li = document.querySelector('li[name="Accounting"], li[aria-label="Accounting"]');
                                if (li && li.offsetParent !== null) {
                                    const a = li.querySelector('a') || li;
                                    a.scrollIntoView({block:'center'});
                                    a.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                    a.click();
                                    a.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                    return 'clicked-li: ' + (li.outerHTML || '').slice(0, 100);
                                }
                                return null;
                            }
                        """)
                        if r:
                            log.info("Accounting TAB clicked in [%s]: %s", label, r)
                            tab_clicked = True
                            break
                    except Exception:
                        pass
                if tab_clicked:
                    break
                if i % 5 == 0:
                    log.info("Accounting TAB not yet visible (%ds) — waiting for AngularJS tabs to render", i + 1)

            if not tab_clicked:
                log.warning("Accounting TAB never appeared — proceeding without clicking (sidebar would navigate away, avoided)")
            else:
                page.wait_for_timeout(5_000)  # let the tab content render
                ts = _snap(page, "after_acct")
                log.info("After Accounting TAB screenshot: %s", ts)
                _emit("Opened Accounting tab in Report Viewer", ts)
                # Sanity check: URL must still be MyReports — if it changed to /accounting/legacy/, we hit the sidebar
                if "MyReports" not in page.url and "reports-management" not in page.url:
                    log.error("URL changed away from Reports after tab click! Now at: %s", page.url)
                    log.info("Navigating back to My Reports…")
                    try:
                        page.go_back(timeout=10_000)
                        page.wait_for_timeout(3_000)
                    except Exception:
                        pass

            # ── Step 4: poll up to 90s, searching ALL frames (excl. help widgets) ──
            log.info("Polling for Customize button for '%s'…", TARGET_REPORT)
            ctx = page  # default
            found = False
            for i in range(90):
                page.wait_for_timeout(1_000)
                found_ctx, info = _find_customize_ctx(page)
                if found_ctx is not None:
                    ctx = found_ctx
                    log.info("Customize found after %ds: %s", i + 1, info)
                    found = True
                    break
                if i % 5 == 0:
                    log.info("Poll %ds: still searching… (%d frames)", i + 1, len(page.frames))
                if i % 15 == 0 and i > 0:
                    snap = f"poll_{i}s_{uuid.uuid4().hex[:4]}.png"
                    page.screenshot(path=f"/tmp/{snap}", full_page=False)
                    log.info("Poll screenshot: %s", snap)

            if not found:
                dbg = _snap(page, "debug")
                return {"error": "Customize button never appeared", "screenshot_filename": dbg}

            _emit("GL Account Detail Export dialog opened — configuring filters…")

            # ── Step 5: click Accounting tab if GL Account Detail Export not yet visible ──
            # Try to click Customize for GL Account Detail Export directly first.
            # Only fall back to Accounting tab if it's not on the current view.
            log.info("Attempting to click Customize for '%s'", TARGET_REPORT)
            clicked = ctx.evaluate(f"""
                () => {{
                    // Try by ID first
                    const byId = document.getElementById({repr(BTN_ID)});
                    if (byId) {{ byId.click(); return 'clicked-by-id'; }}

                    // Find the report row and click its Customize button
                    const reportEls = Array.from(document.querySelectorAll('*')).filter(el =>
                        el.textContent.trim() === {repr(TARGET_REPORT)} && el.children.length === 0
                    );
                    for (const el of reportEls) {{
                        let node = el;
                        for (let j = 0; j < 6; j++) {{
                            node = node.parentElement;
                            if (!node) break;
                            const btn = Array.from(node.querySelectorAll('button'))
                                .find(b => b.textContent.trim() === 'Customize');
                            if (btn) {{ btn.click(); return 'clicked-near-report'; }}
                        }}
                    }}
                    return 'not-found-direct';
                }}
            """)
            log.info("Direct Customize click: %s", clicked)

            if "not-found" in str(clicked):
                # Need to switch to Accounting tab first
                log.info("Clicking Accounting tab to find report")
                tab = ctx.evaluate("""
                    () => {
                        // Confirmed selector from live HTML: li[name="Accounting"] a
                        // AngularJS Material tabs use li[name] and li[aria-label]
                        const exact = document.querySelector(
                            'li[name="Accounting"] a, li[aria-label="Accounting"] a'
                        );
                        if (exact) {
                            exact.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            exact.click();
                            exact.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            return 'clicked-exact: ' + exact.className;
                        }
                        // Fallback: find by trimmed text content (HTML has surrounding whitespace)
                        const byText = Array.from(document.querySelectorAll('a, button, li'))
                            .find(e => e.textContent.trim() === 'Accounting' && e.offsetParent !== null);
                        if (byText) {
                            byText.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            byText.click();
                            byText.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            return 'clicked-text: ' + byText.tagName + ' ' + byText.className;
                        }
                        return 'not-found';
                    }
                """)
                log.info("Accounting tab: %s", tab)
                page.wait_for_timeout(4_000)

                # Re-poll for Customize after tab switch
                log.info("Waiting for Customize after Accounting tab switch…")
                for i in range(30):
                    page.wait_for_timeout(1_000)
                    ctx = _get_legacy_frame(page) or page
                    try:
                        present = ctx.evaluate(f"""
                            () => {{
                                const byId = document.getElementById({repr(BTN_ID)});
                                const anyCustomize = Array.from(document.querySelectorAll('button'))
                                    .filter(b => b.textContent.trim() === 'Customize').length;
                                return {{ byId: !!byId, anyCustomize }};
                            }}
                        """)
                    except Exception:
                        present = {"byId": False, "anyCustomize": 0}
                    if present.get("byId") or present.get("anyCustomize", 0) > 0:
                        log.info("Customize ready after tab switch (%ds): %s", i + 1, present)
                        break

                clicked = ctx.evaluate(f"""
                    () => {{
                        const btn = document.getElementById({repr(BTN_ID)});
                        if (btn) {{ btn.click(); return 'clicked-by-id'; }}
                        const reportEls = Array.from(document.querySelectorAll('*')).filter(el =>
                            el.textContent.trim() === {repr(TARGET_REPORT)} && el.children.length === 0
                        );
                        for (const el of reportEls) {{
                            let node = el;
                            for (let j = 0; j < 6; j++) {{
                                node = node.parentElement;
                                if (!node) break;
                                const btn2 = Array.from(node.querySelectorAll('button'))
                                    .find(b => b.textContent.trim() === 'Customize');
                                if (btn2) {{ btn2.click(); return 'clicked-near-report'; }}
                            }}
                        }}
                        // Last resort: first Customize button
                        const any = Array.from(document.querySelectorAll('button'))
                            .find(b => b.textContent.trim() === 'Customize');
                        if (any) {{ any.click(); return 'clicked-fallback'; }}
                        return 'not-found';
                    }}
                """)
                log.info("Customize click after tab switch: %s", clicked)

            if clicked == "not-found":
                dbg = _snap(page, "debug")
                return {"error": "Customize click failed", "screenshot_filename": dbg}
            page.wait_for_timeout(3_000)

            # ── Step 8: click ACCOUNT button (not ACCOUNTS AVAILABLE) ─────────
            log.info("Clicking ACCOUNT button")
            acct = ctx.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button')).find(b => {
                        const t = b.textContent.trim().toUpperCase();
                        return t === 'ACCOUNT' ||
                               (t.includes('ACCOUNT') && !t.includes('ACCOUNTS') && !t.includes('AVAILABLE'));
                    });
                    if (!btn) return 'not-found';
                    btn.scrollIntoView();
                    btn.click();
                    return 'clicked: ' + btn.textContent.trim();
                }
            """)
            log.info("ACCOUNT btn: %s", acct)
            page.wait_for_timeout(3_000)

            # ── Step 8.5: clear all selections via Select All toggle ─────────
            # Click Select All twice: first click selects everything, second
            # click deselects everything. This always ends at "nothing selected"
            # regardless of the initial state (none / partial / all selected).
            log.info("Clearing selections: clicking Select All twice")
            for i in (1, 2):
                res = ctx.evaluate("""
                    () => {
                        const sa = document.querySelector('md-checkbox[ng-model="selectAll"]');
                        if (!sa) return 'select-all-not-found';
                        sa.scrollIntoView({block:'center'});
                        sa.click();
                        return 'select-all-clicked aria-checked=' + sa.getAttribute('aria-checked');
                    }
                """)
                log.info("Select All click %d: %s", i, res)
                page.wait_for_timeout(1_000)

            # ── Step 9: type the receivable account into the search input ────
            SEARCH_TERM = account
            log.info("Finding search input")
            try:
                if ctx != page:
                    inp = ctx.locator('input:not([disabled]):not([type="hidden"])').last
                else:
                    inp = page.locator('input:not([disabled]):not([type="hidden"])').last
                inp.click(timeout=5_000)
                inp.fill("")
                inp.type(SEARCH_TERM, delay=60)
                log.info("Typed %r via frame locator", SEARCH_TERM)
            except Exception as e:
                log.warning("Frame locator input failed (%s) — keyboard fallback", e)
                page.keyboard.type(SEARCH_TERM, delay=60)
            page.wait_for_timeout(2_500)

            # Pre-select screenshot
            pre = _snap(page, "pre_select")
            log.info("Pre-select screenshot: %s", pre)
            _emit(f"Searching for account {account}…", pre)

            # ── Step 10: check the account's checkbox ────────────────────────
            # The dialog is AngularJS Material inside the iframe (ctx). Each
            # row has a wrapper <button aria-label="<account>"
            # ng-click="wantedItem(item[0], true)"> that is the canonical way
            # to toggle selection — clicking it calls the AngularJS handler
            # which sets `wanted=true` and triggers a digest cycle. The
            # md-checkbox fallback handles aria-labels rendered with a trailing
            # space.
            log.info("Selecting '%s' via AngularJS wantedItem()", account)

            click_result = ctx.evaluate(f"""
                () => {{
                    const acct = {json.dumps(account)};
                    // Prefer the wrapper button (canonical AngularJS handler)
                    const btn = document.querySelector(
                        'button[aria-label="' + acct + '"]'
                    );
                    if (btn) {{
                        btn.scrollIntoView({{block:'center'}});
                        btn.click();
                        return 'wrapper-button-clicked';
                    }}
                    // Fallback: click the md-checkbox directly (note trailing space in aria-label)
                    const cb = document.querySelector(
                        'md-checkbox[aria-label="' + acct + ' "], ' +
                        'md-checkbox[aria-label="' + acct + '"]'
                    );
                    if (cb) {{
                        cb.scrollIntoView({{block:'center'}});
                        cb.click();
                        return 'md-checkbox-clicked aria-checked=' + cb.getAttribute('aria-checked');
                    }}
                    return 'no-target-found';
                }}
            """)
            log.info("Checkbox click: %s", click_result)
            page.wait_for_timeout(1_500)

            # Verify the checkbox is actually checked now
            verify = ctx.evaluate(f"""
                () => {{
                    const acct = {json.dumps(account)};
                    const cb = document.querySelector(
                        'md-checkbox[aria-label="' + acct + ' "], ' +
                        'md-checkbox[aria-label="' + acct + '"]'
                    );
                    return cb ? cb.getAttribute('aria-checked') : 'not-found';
                }}
            """)
            log.info("Checkbox aria-checked after click: %s", verify)

            # Screenshot after checking — should show checkbox ticked
            check_shot = _snap(page, "checked")
            log.info("After-check screenshot: %s", check_shot)
            _emit(f"Account {account} selected", check_shot)

            # ── Step 11: click OK to confirm selection ───────────────────────
            # Use the exact ng-click selector — there are two buttons with
            # text "OK"/"Cancel" but only one has ng-click="closeDialog('OK')".
            log.info("Clicking OK to confirm (ng-click=closeDialog('OK'))")
            ok_result = ctx.evaluate(r"""
                () => {
                    // Match by ng-click attribute (most reliable)
                    const ok = document.querySelector(
                        "button[ng-click=\"closeDialog('OK')\"]"
                    );
                    if (ok) {
                        ok.scrollIntoView({block:'center'});
                        ok.click();
                        return 'ok-ng-click';
                    }
                    // Fallback: find by exact text inside md-dialog-actions
                    const actions = document.querySelector('md-dialog-actions');
                    if (actions) {
                        const btn = Array.from(actions.querySelectorAll('button'))
                            .find(b => b.textContent.trim().toUpperCase() === 'OK');
                        if (btn) { btn.click(); return 'ok-dialog-actions'; }
                    }
                    return 'ok-not-found';
                }
            """)
            log.info("OK: %s", ok_result)
            page.wait_for_timeout(2_500)

            # ── Steps 17-19: Set dates and toggles (runs once) ───────────────
            if start_date:
                s_str = f"{start_date.month}/{start_date.day}/{start_date.year}"
                log.info("Setting Start date to %s", s_str)
                r = _set_datepicker(ctx, page, "Start", s_str)
                log.info("Start date result: %s", r)

            if end_date:
                e_str = f"{end_date.month}/{end_date.day}/{end_date.year}"
                log.info("Setting End date to %s", e_str)
                r = _set_datepicker(ctx, page, "End", e_str)
                log.info("End date result: %s", r)

            log.info("Setting Show Unapproved to %r", show_unapproved)
            _click_button_group(ctx, "Show Unapproved", show_unapproved)
            page.wait_for_timeout(400)

            log.info("Setting Calendar to %r", calendar)
            _click_button_group(ctx, "Calendar", calendar)
            page.wait_for_timeout(400)

            filters_shot = _snap(page, "filters_set")
            _emit(
                f"Filters set — dates: {start_date or 'default'} → {end_date or 'default'}, "
                f"Show Unapproved: {show_unapproved}, Calendar: {calendar}",
                filters_shot,
            )

            # ── FIND_AND_CLICK_RUN_JS shared across iterations ───────────────
            FIND_AND_CLICK_RUN_JS = """
                (reportTitle) => {
                    function visible(el) {
                        if (!el || !el.offsetParent) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }
                    const titleNodes = Array.from(document.querySelectorAll('*'))
                        .filter(el => visible(el)
                            && el.children.length === 0
                            && el.textContent.trim() === reportTitle);
                    const found = new Set();
                    for (const t of titleNodes) {
                        let cur = t;
                        for (let i = 0; i < 15 && cur; i++) {
                            const rb = cur.querySelector
                                && cur.querySelector('button.runBTN');
                            if (rb && visible(rb)) { found.add(rb); break; }
                            cur = cur.parentElement;
                        }
                    }
                    const runs = [...found].sort((a,b) =>
                        b.getBoundingClientRect().x - a.getBoundingClientRect().x
                    );
                    const runBtn = runs[0];
                    if (!runBtn) return JSON.stringify({
                        result: 'run-not-found',
                        titleCount: titleNodes.length,
                    });
                    runBtn.scrollIntoView({block:'center'});
                    runBtn.click();
                    return JSON.stringify({
                        result: 'run-clicked',
                        ngClick: runBtn.getAttribute('ng-click') || '',
                    });
                }
            """

            def _contexts(p):
                yield "page", p
                for fr in p.frames:
                    if fr is not p.main_frame:
                        yield f"frame[{(fr.url or '')[:60]}]", fr

            # ── Per-entity loop ───────────────────────────────────────────────
            viewer_page = None

            for i, entity in enumerate(legal_entities):
                _emit(f"Processing entity {i + 1}/{len(legal_entities)}: {entity}…")

                if i == 0:
                    # First entity: select legal entity then click Run from customize panel
                    _select_legal_entity(ctx, page, entity)
                    _emit(f"Legal entity '{entity}' selected", _snap(page, "entity_ok"))

                    # ── Step 20: Click RUN to open ReportViewer ───────────────
                    new_tabs: list = []
                    _on_page = lambda p: new_tabs.append(p)
                    browser.on("page", _on_page)

                    log.info("Clicking RUN button (customize dialog) to open ReportViewer")
                    run_result = ctx.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                    log.info("Run button (ctx): %s", run_result)
                    if "run-not-found" in (run_result or ""):
                        run_result = page.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                        log.info("Run button (page fallback): %s", run_result)

                    _emit("Clicked RUN — waiting for ReportViewer tab…",
                          _snap(page, "run_clicked"))

                    # ── Set up download capture ───────────────────────────────
                    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
                    captured_download = {"dl": None}

                    def _on_download(dl, _cap=captured_download):
                        if _cap["dl"] is None:
                            _cap["dl"] = dl
                            log.info("Download event captured: %s", dl.suggested_filename)

                    popup_pages: list = []

                    def _on_popup(p):
                        popup_pages.append(p)
                        log.info("Popup page opened: %s", p.url[:120])
                        try:
                            p.on("download", _on_download)
                        except Exception:
                            pass

                    browser.on("page", _on_popup)
                    browser.on("download", _on_download)
                    try:
                        page.on("download", _on_download)
                    except Exception:
                        pass

                    page.wait_for_timeout(3_000)
                    REPORT_VIEWER_URL = "https://ayg.restaurant365.com/#/ReportViewer"

                    # Prefer a tab R365 spawned itself; otherwise open one ourselves.
                    for p in list(browser.pages):
                        try:
                            if "ReportViewer" in (p.url or ""):
                                viewer_page = p
                                break
                        except Exception:
                            continue

                    if viewer_page is None:
                        log.info("Opening ReportViewer in new tab: %s", REPORT_VIEWER_URL)
                        viewer_page = browser.new_page()
                        try:
                            viewer_page.goto(REPORT_VIEWER_URL, wait_until="domcontentloaded")
                        except Exception as ge:
                            log.warning("ReportViewer goto warning: %s", ge)

                    log.info("ReportViewer: %s", viewer_page.url[:120])
                    try:
                        viewer_page.bring_to_front()
                        viewer_page.on("download", _on_download)
                    except Exception:
                        pass
                    try:
                        viewer_page.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass

                    try:
                        browser.remove_listener("page", _on_page)
                    except Exception as e:
                        log.warning("remove_listener failed (non-fatal): %s", e)

                else:
                    # Subsequent entities: click Customize on viewer_page, change entity, run
                    log.info("Clicking Customize on ReportViewer for entity %r", entity)
                    cust_clicked = False
                    try:
                        cust_btn = viewer_page.locator('button[ng-click="toggleLeft()"]').first
                        if cust_btn.count() > 0 and cust_btn.is_visible(timeout=3_000):
                            cust_btn.click()
                            cust_clicked = True
                            log.info("Customize clicked via ng-click selector")
                    except Exception as ce:
                        log.warning("Customize ng-click selector failed: %s", ce)

                    if not cust_clicked:
                        # Fallback: find by button text
                        try:
                            cust_result = viewer_page.evaluate("""
                                () => {
                                    const btn = Array.from(document.querySelectorAll('button'))
                                        .find(b => b.textContent.trim() === 'Customize');
                                    if (btn) { btn.click(); return 'clicked-text'; }
                                    return 'not-found';
                                }
                            """)
                            log.info("Customize fallback: %s", cust_result)
                            cust_clicked = cust_result != "not-found"
                        except Exception as fe:
                            log.warning("Customize fallback failed: %s", fe)

                    viewer_page.wait_for_timeout(2_000)

                    # On viewer_page the Filter parameter is an md-autocomplete,
                    # not a modal dialog — use the autocomplete selection path.
                    _select_entity_via_autocomplete(viewer_page, entity)
                    _emit(f"Legal entity '{entity}' selected", _snap(viewer_page, "entity_ok"))

                    # Click Run on viewer_page
                    log.info("Clicking RUN on ReportViewer for entity %r", entity)
                    run_result = viewer_page.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                    log.info("Run button (viewer_page): %s", run_result)
                    if "run-not-found" in (run_result or ""):
                        # Try frames within viewer_page
                        for _flabel, _fc in _contexts(viewer_page):
                            try:
                                run_result = _fc.evaluate(FIND_AND_CLICK_RUN_JS, TARGET_REPORT)
                                if "run-clicked" in (run_result or ""):
                                    log.info("Run button in %s: %s", _flabel, run_result)
                                    break
                            except Exception:
                                continue

                    viewer_page.wait_for_timeout(3_000)
                    try:
                        viewer_page.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass

                    # Fresh download capture for this entity
                    captured_download = {"dl": None}
                    popup_pages = []

                    def _on_download(dl, _cap=captured_download):
                        if _cap["dl"] is None:
                            _cap["dl"] = dl
                            log.info("Download event captured: %s", dl.suggested_filename)

                    viewer_page.on("download", _on_download)
                    browser.on("download", _on_download)

                _emit("Opened ReportViewer — locating Export dropdown…",
                      _snap(viewer_page, "report_viewer_loaded"))

                # ── Export Excel (same logic for all entities) ────────────────
                save_dropdown_clicked = None
                deadline = time.time() + 30
                while time.time() < deadline and save_dropdown_clicked is None:
                    for label, c in _contexts(viewer_page):
                        try:
                            trigger = c.locator(
                                'a[title="Export drop down menu"]'
                            ).first
                            if trigger.count() > 0 and trigger.is_visible(timeout=500):
                                trigger.click()
                                save_dropdown_clicked = label
                                log.info("Save/Export dropdown clicked in %s", label)
                                break
                        except Exception:
                            continue
                    if save_dropdown_clicked is None:
                        viewer_page.wait_for_timeout(500)

                download_filename = None

                if save_dropdown_clicked is None:
                    log.warning("Save/Export dropdown trigger never appeared")
                    _emit("Save/Export dropdown not found",
                          _snap(viewer_page, "save_missing"))
                else:
                    viewer_page.wait_for_timeout(1_500)
                    _emit("Save dropdown opened — clicking Excel…",
                          _snap(viewer_page, "save_opened"))

                    EXCEL_SEARCH_JS = """
                        () => {
                            const els = Array.from(document.querySelectorAll(
                                'a, li, td, div[role="menuitem"], ' +
                                'li[role="menuitem"], span, input[type="button"]'
                            ));
                            const excelEl = els.find(e => {
                                const t = e.textContent.trim().toLowerCase();
                                return t.includes('excel') && t.length < 60;
                            });
                            if (excelEl) {
                                excelEl.scrollIntoView({block:'center'});
                                excelEl.click();
                                return 'clicked:' + excelEl.textContent.trim();
                            }
                            const visible = els
                                .filter(e => e.offsetParent !== null
                                          && e.textContent.trim().length < 60)
                                .map(e => e.textContent.trim())
                                .filter((v, i, a) => v && a.indexOf(v) === i)
                                .slice(0, 40);
                            return 'not-found|' + visible.join(';');
                        }
                    """

                    excel_clicked_in = None
                    # Retry opening the dropdown up to 3 times in case the report
                    # was still rendering when the dropdown was first opened.
                    for _attempt in range(3):
                        if _attempt > 0:
                            log.info("Excel not found on attempt %d — re-opening dropdown", _attempt + 1)
                            # Re-click the export dropdown trigger
                            for _rlabel, _rc in _contexts(viewer_page):
                                try:
                                    trigger = _rc.locator('a[title="Export drop down menu"]').first
                                    if trigger.count() > 0 and trigger.is_visible(timeout=500):
                                        trigger.click()
                                        log.info("Re-opened export dropdown in %s", _rlabel)
                                        viewer_page.wait_for_timeout(1_500)
                                        break
                                except Exception:
                                    continue

                        deadline = time.time() + 20
                        while time.time() < deadline and excel_clicked_in is None:
                            for label, c in _contexts(viewer_page):
                                try:
                                    result = c.evaluate(EXCEL_SEARCH_JS)
                                    if result and result.startswith("clicked:"):
                                        excel_clicked_in = label
                                        log.info("Excel option clicked in %s: %s", label, result)
                                        break
                                    elif result:
                                        log.info("Excel search in %s: %s", label, result[:300])
                                except Exception as ex:
                                    log.info("Excel search error in %s: %s", label, ex)
                                    continue
                            if excel_clicked_in is None:
                                viewer_page.wait_for_timeout(500)

                        if excel_clicked_in is not None:
                            break
                        # Wait for report to finish loading before next attempt
                        log.info("Waiting 10s for report to finish rendering before retry…")
                        viewer_page.wait_for_timeout(10_000)

                    if excel_clicked_in is None:
                        log.warning("Excel option not found after opening dropdown")
                        _emit("Excel option not found",
                              _snap(viewer_page, "excel_missing"))
                    else:
                        deadline = time.time() + 90
                        while time.time() < deadline and captured_download["dl"] is None:
                            viewer_page.wait_for_timeout(500)

                        dl = captured_download["dl"]
                        if dl is not None:
                            suggested = (
                                dl.suggested_filename
                                or f"gl_export_{uuid.uuid4().hex[:8]}.xlsx"
                            )
                            # Embed entity name so files don't overwrite each other
                            stem = Path(suggested).stem
                            ext = Path(suggested).suffix or ".xlsx"
                            safe_entity = entity.replace(" ", "_").replace("/", "-")
                            download_filename = f"{stem}_{safe_entity}{ext}"
                            save_path = DOWNLOADS_DIR / download_filename
                            try:
                                dl.save_as(str(save_path))
                                log.info("Saved download: %s", save_path)
                                _emit(f"Export saved: {download_filename}",
                                      _snap(viewer_page, "after_download"))
                            except Exception as se:
                                log.warning("save_as failed: %s", se)
                        else:
                            log.warning(
                                "Excel clicked but no download in 90 s "
                                "(popups seen: %d)", len(popup_pages),
                            )
                            _emit("Excel clicked — download not captured",
                                  _snap(viewer_page, "after_excel"))

                results.append({"entity": entity, "download_filename": download_filename})
                if entity_cb:
                    try:
                        entity_cb(entity, download_filename)
                    except Exception as ecb_err:
                        log.warning("entity_cb raised: %s", ecb_err)

                # Clean up download listener for this entity before next iteration
                try:
                    browser.remove_listener("download", _on_download)
                except Exception:
                    pass
                try:
                    viewer_page.remove_listener("download", _on_download)
                except Exception:
                    pass

            try:
                browser.remove_listener("page", _on_popup)
            except Exception:
                pass

            screenshot_name = _snap(viewer_page or page, "report_viewer_final")
            log.info("Final screenshot: %s", screenshot_name)
            return {
                "results": results,
                "screenshot_filename": screenshot_name,
            }

        except Exception as exc:
            log.error("Error: %s", exc)
            try:
                dbg = _snap(page, "debug")
                return {"error": str(exc), "screenshot_filename": dbg or None}
            except Exception:
                return {"error": str(exc)}
        finally:
            browser.close()
